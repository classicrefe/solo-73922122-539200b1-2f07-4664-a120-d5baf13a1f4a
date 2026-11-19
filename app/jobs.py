"""Job orchestration: introspect -> solve -> render -> verify -> publish.

A package becomes downloadable only after the verification cluster reports a
successful load of the full SQL package. Failures retain a structured conflict
report and logs, never a partial package.
"""
from __future__ import annotations

import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO

from .config import settings
from .db import source_connection
from .introspect import IntrospectionError, introspect_tables, read_fk_graph
from .merger import (
    MergeSolver,
    MergeTimeout,
    MergeUnsatisfiable,
    check_merge_eligible,
    check_structure_compatible,
)
from .models import (
    ConflictReport,
    GenerateRequest,
    JobResult,
    JobStatus,
    MergeResult,
)
from .selector import validate_request
from .solver import Generator, SolverTimeout, Unsatisfiable
from .sqlgen import build_merge_package, build_package
from .verifier import verify_package


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobResult] = {}
        self._packages: dict[str, dict[str, str]] = {}
        self._artifacts: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = JobResult(
                job_id=job_id,
                status=JobStatus.PENDING,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        return job_id

    def get(self, job_id: str) -> JobResult | None:
        return self._jobs.get(job_id)

    def package(self, job_id: str) -> dict[str, str] | None:
        return self._packages.get(job_id)

    def artifacts(self, job_id: str) -> dict | None:
        """Structured solve output (schemas + data) kept for co-loading."""
        return self._artifacts.get(job_id)

    def update(self, job_id: str, **kw) -> None:
        with self._lock:
            job = self._jobs[job_id]
            data = job.model_dump()
            data.update(kw)
            self._jobs[job_id] = JobResult(**data)

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.log.append(f"{datetime.now(timezone.utc).time().isoformat()} {line}")

    def publish(self, job_id: str, package: dict[str, str], artifacts: dict | None = None) -> None:
        with self._lock:
            self._packages[job_id] = package
            if artifacts is not None:
                self._artifacts[job_id] = artifacts

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                jid
                for jid, job in self._jobs.items()
                if now - datetime.fromisoformat(job.created_at).timestamp()
                > settings.job_ttl_seconds
            ]
            for jid in stale:
                self._jobs.pop(jid, None)
                self._packages.pop(jid, None)
                self._artifacts.pop(jid, None)


STORE = JobStore()


def _conflict_from_groups(groups) -> ConflictReport:
    kinds = sorted({g.kind for g in groups})
    table_hint = sorted({r for g in groups for r in g.refs if r})
    lines = [g.label for g in groups]
    summary = "、".join(
        {
            "rows": "行数",
            "range": "取值区间/可空性",
            "notnull": "NOT NULL",
            "unique": "主键/唯一键",
            "fk": "外键",
        }.get(k, k)
        for k in kinds
    )
    return ConflictReport(
        kind="no_solution",
        message=(
            f"无解：以下{summary}约束相互冲突，不存在同时满足指定行数、区间与全部约束的样本。"
            f"涉及对象: {', '.join(table_hint) if table_hint else '(见详情)'}"
        ),
        details={"conflicting_constraints": lines, "kinds": kinds},
    )


def run_job(job_id: str, req: GenerateRequest) -> None:
    store = STORE
    try:
        store.update(job_id, status=JobStatus.GENERATING)
        store.append_log(job_id, "opening read-only source connection (catalogs only)")
        with source_connection(settings.source_dsn) as conn:
            requested = req.table_names()

            # Step 1: compute the FK closure by expanding the catalog FK graph
            # one level at a time, so transitive targets (and cycles) are all
            # reached, not just direct references of the requested tables.
            fk_graph: dict[str, list[str]] = {}
            selected: list[str] = []
            seen: set[str] = set()
            queue = list(requested)
            closure_ok = True
            while queue:
                t = queue.pop(0)
                if t in seen:
                    continue
                seen.add(t)
                selected.append(t)
                fk_graph.update(read_fk_graph(conn, req.schema_name, [t]))
                for target in fk_graph.get(t, []):
                    if target not in seen:
                        queue.append(target)
                if len(selected) > settings.max_tables:
                    closure_ok = False
                    break
            if not closure_ok or len(selected) > settings.max_tables:
                store.update(
                    job_id,
                    status=JobStatus.FAILED,
                    conflict=ConflictReport(
                        kind="closure",
                        message=(
                            f"外键闭包需要 {len(set(selected) | set(queue))} 张表，"
                            f"超过至多 {settings.max_tables} 张表的限制；请缩小选择范围"
                        ),
                        details={"reached_so_far": selected, "pending": queue},
                    ),
                )
                return
            store.append_log(job_id, f"selected closure: {selected}")
            store.update(job_id, selected_tables=selected)

            # Step 2: full strict introspection of the whole closed set.
            try:
                schemas = introspect_tables(conn, req.schema_name, selected)
            except IntrospectionError as exc:
                store.update(
                    job_id,
                    status=JobStatus.FAILED,
                    conflict=ConflictReport(
                        kind="structure",
                        message="源表结构包含不受支持的对象，已明确拒绝（不会忽略任何结构）",
                        details={"rejections": [
                            {"object": o, "reason": r} for o, r in exc.rejections
                        ]},
                    ),
                )
                return

            # Step 3: validate the user-supplied specs for requested tables.
            requested_req = GenerateRequest(
                schema_name=req.schema_name,
                tables=[s for s in req.tables if s.table in requested],
            )
            problems = validate_request(
                requested_req, schemas, settings.max_rows_per_table
            )
            if problems:
                store.update(
                    job_id,
                    status=JobStatus.FAILED,
                    conflict=ConflictReport(
                        kind="request",
                        message="请求参数与目录结构不一致",
                        details={"problems": [
                            {"object": o, "reason": r} for o, r in problems
                        ]},
                    ),
                )
                return

            # Step 4: closure-added tables get bounded default specs so the
            # solver still constrains every cell, while user tables keep the
            # exact requested row counts and intervals.
            from .models import IntRange, TableSpec, TYPE_BOUNDS

            spec_by_name = {s.table: s for s in req.tables}
            for t in selected:
                if t in spec_by_name:
                    continue
                ts = schemas[t]
                spec_by_name[t] = TableSpec(
                    table=t,
                    rows=min(5, settings.max_rows_per_table),
                    ranges={
                        c.name: IntRange(
                            low=TYPE_BOUNDS[c.pg_type][0],
                            high=TYPE_BOUNDS[c.pg_type][1],
                        )
                        for c in ts.columns
                    },
                )
            req = GenerateRequest(
                schema_name=req.schema_name,
                tables=[spec_by_name[t] for t in selected],
            )

        specs = {s.table: s for s in req.tables}

        store.append_log(job_id, "solving constraints with z3 (hard: rows, ranges, PK/UK/FK)")
        gen = Generator({t: schemas[t] for t in selected}, specs)
        try:
            data = gen.solve()
        except Unsatisfiable as unsat:
            store.update(
                job_id,
                status=JobStatus.FAILED,
                conflict=_conflict_from_groups(unsat.groups),
            )
            return
        except SolverTimeout as exc:
            store.append_log(job_id, f"solver timeout: {exc}")
            store.update(
                job_id,
                status=JobStatus.FAILED,
                conflict=ConflictReport(
                    kind="solver_timeout",
                    message=str(exc),
                    details={"note": "这不是无解结论，而是限时内未能判定"},
                ),
            )
            return

        store.append_log(job_id, "rendering SQL package")
        package = build_package(
            {t: schemas[t] for t in selected}, selected, specs, data
        )

        store.update(job_id, status=JobStatus.VERIFYING)
        store.append_log(job_id, "loading package into isolated empty verification database")
        # verify_package manages its own short-lived autocommit connections.
        ok, vlog = verify_package(
            settings.verify_dsn,
            settings.verify_db_prefix,
            req.schema_name,
            {t: schemas[t] for t in selected},
            selected,
            package,
        )
        for line in vlog:
            store.append_log(job_id, f"verify: {line}")
        if not ok:
            store.update(
                job_id,
                status=JobStatus.FAILED,
                conflict=ConflictReport(
                    kind="verification",
                    message="生成的 SQL 在隔离空库装载/负向校验失败，不提供下载（无部分包）",
                    details={"verify_log": vlog},
                ),
            )
            return

        # Only now is the package stored and downloadable. The structured
        # solve output (schemas + data) is kept alongside so a later co-load
        # can merge this sample without touching the source database.
        store.publish(
            job_id,
            package,
            artifacts={
                "schema_name": req.schema_name,
                "selected": list(selected),
                "schemas": {t: schemas[t] for t in selected},
                "data": data,
            },
        )
        store.append_log(job_id, "package verified and published")
        store.update(job_id, status=JobStatus.SUCCEEDED)
    except Exception as exc:  # defensive: never leave a job dangling
        store.append_log(job_id, f"unexpected error: {exc.__class__.__name__}: {exc}")
        store.update(
            job_id,
            status=JobStatus.FAILED,
            conflict=ConflictReport(
                kind="internal",
                message=f"服务内部错误: {exc.__class__.__name__}: {exc}",
            ),
        )


def package_zip(package: dict[str, str]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in package.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Co-load (merge) orchestration.
#
# A merge combines the verified samples of two succeeded jobs into one new
# package. Source jobs and their packages are never modified; the merged
# package is published only after it loads cleanly in an isolated empty
# database. No-solution, timeout and trial-load failure are reported as
# distinct conflict kinds, and no partial package is ever exposed.
# ---------------------------------------------------------------------------


class MergeStore:
    """Status and artifacts of co-load jobs, isolated from generation jobs."""

    def __init__(self) -> None:
        self._merges: dict[str, MergeResult] = {}
        self._packages: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def create(self, job_a: str, job_b: str) -> str:
        merge_id = uuid.uuid4().hex
        with self._lock:
            self._merges[merge_id] = MergeResult(
                merge_id=merge_id,
                status=JobStatus.PENDING,
                created_at=datetime.now(timezone.utc).isoformat(),
                job_a=job_a,
                job_b=job_b,
            )
        return merge_id

    def get(self, merge_id: str) -> MergeResult | None:
        return self._merges.get(merge_id)

    def package(self, merge_id: str) -> dict[str, str] | None:
        return self._packages.get(merge_id)

    def update(self, merge_id: str, **kw) -> None:
        with self._lock:
            merge = self._merges[merge_id]
            data = merge.model_dump()
            data.update(kw)
            self._merges[merge_id] = MergeResult(**data)

    def append_log(self, merge_id: str, line: str) -> None:
        with self._lock:
            merge = self._merges[merge_id]
            merge.log.append(f"{datetime.now(timezone.utc).time().isoformat()} {line}")

    def publish(self, merge_id: str, package: dict[str, str]) -> None:
        with self._lock:
            self._packages[merge_id] = package

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                mid
                for mid, merge in self._merges.items()
                if now - datetime.fromisoformat(merge.created_at).timestamp()
                > settings.job_ttl_seconds
            ]
            for mid in stale:
                self._merges.pop(mid, None)
                self._packages.pop(mid, None)


MERGE_STORE = MergeStore()


def _merge_conflict_from_groups(groups) -> ConflictReport:
    kinds = sorted({g.kind for g in groups})
    lines = [g.label for g in groups]
    return ConflictReport(
        kind="no_solution",
        message=(
            "无解：在「第一份样本全部值保留、第二份样本仅可改写主键与有效引用"
            "外键列、NULL 位置不变、每条有效引用仍指向原记录合并版」的限制下，"
            "不存在同时满足全部约束的合装结果。"
        ),
        details={"conflicting_constraints": lines, "kinds": kinds},
    )


def run_merge(merge_id: str, job_a_id: str, job_b_id: str) -> None:
    store = MERGE_STORE
    try:
        store.update(merge_id, status=JobStatus.GENERATING)
        store.append_log(merge_id, f"loading artifacts of source jobs {job_a_id}, {job_b_id}")
        job_a, job_b = STORE.get(job_a_id), STORE.get(job_b_id)
        art_a, art_b = STORE.artifacts(job_a_id), STORE.artifacts(job_b_id)
        if (
            job_a is None
            or job_b is None
            or art_a is None
            or art_b is None
            or job_a.status != JobStatus.SUCCEEDED
            or job_b.status != JobStatus.SUCCEEDED
        ):
            store.update(
                merge_id,
                status=JobStatus.FAILED,
                conflict=ConflictReport(
                    kind="source_jobs",
                    message="来源作业不存在、已过期或未验证成功，无法合装",
                ),
            )
            return

        # Step 1: the two jobs must share exactly the same structure.
        problems = check_structure_compatible(
            art_a["schema_name"], art_a["schemas"],
            art_b["schema_name"], art_b["schemas"],
        )
        if problems:
            store.update(
                merge_id,
                status=JobStatus.FAILED,
                conflict=ConflictReport(
                    kind="structure_mismatch",
                    message="两份作业的结构不相同，无法合装",
                    details={"problems": [
                        {"object": o, "reason": r} for o, r in problems
                    ]},
                ),
            )
            return

        schemas = art_a["schemas"]
        selected = list(art_a["selected"])

        # Step 2: co-load eligibility (table/row limits, FKs must target PKs).
        problems = check_merge_eligible(
            schemas,
            art_a["data"],
            art_b["data"],
            settings.merge_max_tables,
            settings.merge_max_rows_per_table,
        )
        if problems:
            store.update(
                merge_id,
                status=JobStatus.FAILED,
                conflict=ConflictReport(
                    kind="unsupported",
                    message=(
                        "超出合装支持范围（限结构相同、外键均指向主键、"
                        "各至多两表且每表最多 8 行）"
                    ),
                    details={"problems": [
                        {"object": o, "reason": r} for o, r in problems
                    ]},
                ),
            )
            return
        store.update(merge_id, selected_tables=selected)

        # Step 3: solve the merge. The first sample is kept verbatim; only
        # PK and validly-referencing FK cells of the second sample may be
        # rewritten, within integer type bounds, NULL positions preserved.
        store.append_log(
            merge_id,
            "solving merge with z3 (second sample: only PK/valid-FK cells rewritable)",
        )
        try:
            merged = MergeSolver(schemas, art_a["data"], art_b["data"]).solve()
        except MergeUnsatisfiable as unsat:
            store.update(
                merge_id,
                status=JobStatus.FAILED,
                conflict=_merge_conflict_from_groups(unsat.groups),
            )
            return
        except MergeTimeout as exc:
            store.append_log(merge_id, f"merge solver timeout: {exc}")
            store.update(
                merge_id,
                status=JobStatus.FAILED,
                conflict=ConflictReport(
                    kind="solver_timeout",
                    message=str(exc),
                    details={"note": "这不是无解结论，而是限时内未能判定"},
                ),
            )
            return

        counts_a = {t: len(art_a["data"][t]) for t in selected}
        counts_b = {t: len(art_b["data"][t]) for t in selected}
        store.append_log(merge_id, "rendering merged SQL package")
        package = build_merge_package(
            schemas, selected, merged, job_a_id, job_b_id, counts_a, counts_b
        )

        # Step 4: the merged package must load in an isolated empty database
        # before it is returned; on failure nothing is published.
        store.update(merge_id, status=JobStatus.VERIFYING)
        store.append_log(merge_id, "loading merged package into isolated empty verification database")
        ok, vlog = verify_package(
            settings.verify_dsn,
            settings.verify_db_prefix,
            art_a["schema_name"],
            schemas,
            selected,
            package,
        )
        for line in vlog:
            store.append_log(merge_id, f"verify: {line}")
        if not ok:
            store.update(
                merge_id,
                status=JobStatus.FAILED,
                conflict=ConflictReport(
                    kind="verification",
                    message="合装 SQL 包在隔离空库试装失败，不返回部分包",
                    details={"verify_log": vlog},
                ),
            )
            return

        store.publish(merge_id, package)
        store.append_log(merge_id, "merged package verified and published")
        store.update(
            merge_id,
            status=JobStatus.SUCCEEDED,
            row_counts={t: len(merged[t]) for t in selected},
        )
    except Exception as exc:  # defensive: never leave a merge dangling
        store.append_log(merge_id, f"unexpected error: {exc.__class__.__name__}: {exc}")
        store.update(
            merge_id,
            status=JobStatus.FAILED,
            conflict=ConflictReport(
                kind="internal",
                message=f"服务内部错误: {exc.__class__.__name__}: {exc}",
            ),
        )
