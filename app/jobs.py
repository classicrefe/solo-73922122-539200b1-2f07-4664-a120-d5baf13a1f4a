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
from .models import (
    ConflictReport,
    GenerateRequest,
    JobResult,
    JobStatus,
)
from .selector import validate_request
from .solver import Generator, SolverTimeout, Unsatisfiable
from .sqlgen import build_package
from .verifier import verify_package


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobResult] = {}
        self._packages: dict[str, dict[str, str]] = {}
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

    def publish(self, job_id: str, package: dict[str, str]) -> None:
        with self._lock:
            self._packages[job_id] = package

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

        # Only now is the package stored and downloadable.
        store.publish(job_id, package)
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
