"""合装（co-load）测试：求解规则、资格校验、编排报告与端到端真实试装。"""
from __future__ import annotations

import copy
import json
import time
import zipfile
from io import BytesIO

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.jobs import MERGE_STORE, STORE, run_job, run_merge
from app.main import app
from app.merger import (
    MergeSolver,
    MergeUnsatisfiable,
    check_merge_eligible,
    check_structure_compatible,
)
from app.models import (
    Column,
    ForeignKey,
    GenerateRequest,
    JobStatus,
    MatchType,
    TableSchema,
    UniqueConstraint,
)

client = TestClient(app)


# --------------------------------------------------------------------------- helpers
def _col(name, pg_type="integer", nullable=False):
    return Column(
        name=name,
        pg_type=pg_type,
        ordinal=1,
        nullable=nullable,
        has_default=False,
        is_identity=False,
        is_generated=False,
    )


def _uk(name, cols, primary=False):
    return UniqueConstraint(name=name, columns=cols, is_primary=primary)


def _fk(name, cols, ref_table, ref_cols, match=MatchType.SIMPLE):
    return ForeignKey(
        name=name,
        columns=cols,
        ref_table=ref_table,
        ref_columns=ref_cols,
        match=match,
        on_update="NO ACTION",
        on_delete="NO ACTION",
        not_deferrable=True,
        initially_deferred=False,
    )


def _table(name, cols, uniques=None, fks=None):
    return TableSchema(
        schema_name="public",
        name=name,
        columns=cols,
        unique_constraints=uniques or [],
        foreign_keys=fks or [],
    )


def _single_pk_table(name="t", pg_type="integer"):
    return _table(
        name,
        [_col("id", pg_type=pg_type)],
        uniques=[_uk(f"{name}_pk", ["id"], primary=True)],
    )


def _plant_job(schemas, data, selected=None, schema_name="public"):
    """Register a SUCCEEDED job with structured artifacts (no DB needed)."""
    jid = STORE.create()
    STORE.update(jid, status=JobStatus.SUCCEEDED)
    STORE.publish(
        jid,
        {"placeholder.sql": "-- planted test package"},
        artifacts={
            "schema_name": schema_name,
            "selected": list(selected or schemas.keys()),
            "schemas": schemas,
            "data": data,
        },
    )
    return jid


def _load(name):
    raw = json.load(open(f"/workspace/examples/{name}"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _run(req_dict):
    req = GenerateRequest(**req_dict)
    jid = STORE.create()
    run_job(jid, req)
    return jid, STORE.get(jid)


def _wait_merge(mid, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = MERGE_STORE.get(mid)
        if m.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            return m
        time.sleep(0.05)
    return MERGE_STORE.get(mid)


# --------------------------------------------------------------------------- solver rules
def test_merge_first_sample_verbatim_and_colliding_pk_remapped():
    ts = _table("t", [_col("id"), _col("val")], uniques=[_uk("t_pk", ["id"], True)])
    data_a = {"t": [{"id": 1, "val": 10}, {"id": 2, "val": 20}]}
    data_b = {"t": [{"id": 1, "val": 70}, {"id": 3, "val": 30}]}  # id=1 撞号
    merged = MergeSolver({"t": ts}, data_a, data_b).solve()["t"]
    # 第一份全部值原样保留
    assert merged[0] == {"id": 1, "val": 10}
    assert merged[1] == {"id": 2, "val": 20}
    # 第二份：非键列原值保留；撞号主键被改写；未撞号的保持原值
    assert merged[2]["val"] == 70
    assert merged[2]["id"] not in (1, 2, 3)
    assert merged[3] == {"id": 3, "val": 30}
    assert len({r["id"] for r in merged}) == 4


def test_merge_null_positions_preserved():
    ts = _table(
        "t",
        [_col("id"), _col("v", nullable=True)],
        uniques=[_uk("t_pk", ["id"], True)],
    )
    data_a = {"t": [{"id": 1, "v": 10}, {"id": 2, "v": 20}]}
    data_b = {"t": [{"id": 2, "v": None}, {"id": 5, "v": 50}]}  # id=2 撞号且 v 为 NULL
    merged = MergeSolver({"t": ts}, data_a, data_b).solve()["t"]
    assert merged[2]["id"] not in (1, 2, 5)
    assert merged[2]["v"] is None  # NULL 位置不变
    assert merged[3] == {"id": 5, "v": 50}


def test_merge_reference_follows_merged_target_record():
    p = _table("p", [_col("id")], uniques=[_uk("p_pk", ["id"], True)])
    c = _table(
        "c",
        [_col("id"), _col("pid")],
        uniques=[_uk("c_pk", ["id"], True)],
        fks=[_fk("c_p_fk", ["pid"], "p", ["id"])],
    )
    schemas = {"p": p, "c": c}
    data_a = {"p": [{"id": 1}, {"id": 2}], "c": [{"id": 10, "pid": 1}]}
    data_b = {
        "p": [{"id": 1}, {"id": 3}],  # id=1 与第一份撞号
        "c": [{"id": 20, "pid": 1}, {"id": 21, "pid": 3}],
    }
    merged = MergeSolver(schemas, data_a, data_b).solve()
    # 第一份原样保留
    assert merged["p"][:2] == [{"id": 1}, {"id": 2}]
    assert merged["c"][0] == {"id": 10, "pid": 1}
    # 第二份 p 的撞号行被改写，引用它的 c 行跟随到合并版主键
    remapped_p0 = merged["p"][2]["id"]
    assert remapped_p0 not in (1, 2, 3)
    assert merged["p"][3] == {"id": 3}
    assert merged["c"][1] == {"id": 20, "pid": remapped_p0}
    assert merged["c"][2] == {"id": 21, "pid": 3}


def test_merge_composite_pk_and_match_full_fk():
    p = _table(
        "p",
        [_col("k1"), _col("k2")],
        uniques=[_uk("p_pk", ["k1", "k2"], True)],
    )
    c = _table(
        "c",
        [_col("id"), _col("f1", nullable=True), _col("f2", nullable=True)],
        uniques=[_uk("c_pk", ["id"], True)],
        fks=[_fk("c_p_fk", ["f1", "f2"], "p", ["k1", "k2"], match=MatchType.FULL)],
    )
    schemas = {"p": p, "c": c}
    data_a = {"p": [{"k1": 1, "k2": 10}], "c": [{"id": 100, "f1": 1, "f2": 10}]}
    data_b = {
        "p": [{"k1": 1, "k2": 10}],  # 复合主键整体撞号
        "c": [
            {"id": 200, "f1": 1, "f2": 10},      # 有效引用 -> 跟随合并版
            {"id": 201, "f1": None, "f2": None},  # MATCH FULL 全 NULL：不是有效引用
        ],
    }
    merged = MergeSolver(schemas, data_a, data_b).solve()
    assert merged["p"][0] == {"k1": 1, "k2": 10}
    new_key = (merged["p"][1]["k1"], merged["p"][1]["k2"])
    assert new_key != (1, 10)
    # 有效引用仍指向原记录（p 第二行）的合并版
    assert (merged["c"][1]["f1"], merged["c"][1]["f2"]) == new_key
    # 全 NULL 键原样保留
    assert merged["c"][2] == {"id": 201, "f1": None, "f2": None}


def test_merge_cyclic_two_tables():
    d = _table(
        "d",
        [_col("id"), _col("mgr", nullable=True)],
        uniques=[_uk("d_pk", ["id"], True)],
        fks=[_fk("d_mgr_fk", ["mgr"], "e", ["id"])],
    )
    e = _table(
        "e",
        [_col("id"), _col("d_id")],
        uniques=[_uk("e_pk", ["id"], True)],
        fks=[_fk("e_d_fk", ["d_id"], "d", ["id"])],
    )
    schemas = {"d": d, "e": e}
    data_a = {"d": [{"id": 1, "mgr": None}], "e": [{"id": 10, "d_id": 1}]}
    data_b = {"d": [{"id": 1, "mgr": 10}], "e": [{"id": 10, "d_id": 1}]}  # 双向撞号
    merged = MergeSolver(schemas, data_a, data_b).solve()
    assert merged["d"][0] == {"id": 1, "mgr": None}
    assert merged["e"][0] == {"id": 10, "d_id": 1}
    d2, e2 = merged["d"][1], merged["e"][1]
    assert d2["id"] != 1 and e2["id"] != 10
    # 循环引用在合并版内部闭合
    assert d2["mgr"] == e2["id"]
    assert e2["d_id"] == d2["id"]


def test_merge_self_reference():
    n = _table(
        "n",
        [_col("id"), _col("parent", nullable=True)],
        uniques=[_uk("n_pk", ["id"], True)],
        fks=[_fk("n_parent_fk", ["parent"], "n", ["id"])],
    )
    data_a = {"n": [{"id": 1, "parent": None}]}
    data_b = {"n": [{"id": 1, "parent": None}, {"id": 2, "parent": 1}]}
    merged = MergeSolver({"n": n}, data_a, data_b).solve()["n"]
    assert merged[0] == {"id": 1, "parent": None}
    assert merged[1]["parent"] is None
    assert merged[1]["id"] not in (1, 2)
    # 自引用跟随被引用行（第二份第一行）的合并版主键
    assert merged[2] == {"id": 2, "parent": merged[1]["id"]}


def test_merge_without_collision_keeps_second_sample_verbatim():
    p = _table("p", [_col("id")], uniques=[_uk("p_pk", ["id"], True)])
    c = _table(
        "c",
        [_col("id"), _col("pid")],
        uniques=[_uk("c_pk", ["id"], True)],
        fks=[_fk("c_p_fk", ["pid"], "p", ["id"])],
    )
    schemas = {"p": p, "c": c}
    data_a = {"p": [{"id": 1}], "c": []}
    data_b = {"p": [{"id": 5}], "c": [{"id": 7, "pid": 5}]}
    merged = MergeSolver(schemas, data_a, data_b).solve()
    assert merged["p"] == [{"id": 1}, {"id": 5}]
    assert merged["c"] == [{"id": 7, "pid": 5}]


def test_merge_unique_collision_on_non_rewritable_column_is_unsat():
    ts = _table(
        "t",
        [_col("id"), _col("code")],
        uniques=[_uk("t_pk", ["id"], True), _uk("t_code_uk", ["code"])],
    )
    # code 不是主键也不是外键，不允许改写；两份样本 code=100 撞号 -> 无解
    data_a = {"t": [{"id": 1, "code": 100}]}
    data_b = {"t": [{"id": 2, "code": 100}]}
    with pytest.raises(MergeUnsatisfiable) as exc:
        MergeSolver({"t": ts}, data_a, data_b).solve()
    kinds = {g.kind for g in exc.value.groups}
    assert "unique" in kinds


def test_merge_rewritten_values_stay_within_integer_type_bounds():
    ts = _single_pk_table(pg_type="smallint")
    data_a = {"t": [{"id": 1}, {"id": 2}]}
    data_b = {"t": [{"id": 1}, {"id": 2}]}
    merged = MergeSolver({"t": ts}, data_a, data_b).solve()["t"]
    ids = [r["id"] for r in merged]
    assert ids[:2] == [1, 2]
    assert len(set(ids)) == 4
    assert all(-32768 <= i <= 32767 for i in ids)


def test_merge_does_not_mutate_source_data():
    ts = _single_pk_table()
    data_a = {"t": [{"id": 1}]}
    data_b = {"t": [{"id": 1}]}
    snapshot_a = copy.deepcopy(data_a)
    snapshot_b = copy.deepcopy(data_b)
    MergeSolver({"t": ts}, data_a, data_b).solve()
    assert data_a == snapshot_a
    assert data_b == snapshot_b


# --------------------------------------------------------------------------- eligibility
def test_structure_compatible_checks():
    ts = _single_pk_table()
    assert check_structure_compatible("public", {"t": ts}, "public", {"t": ts}) == []
    assert check_structure_compatible("s1", {"t": ts}, "s2", {"t": ts})
    other = _table(
        "t",
        [_col("id"), _col("extra")],
        uniques=[_uk("t_pk", ["id"], True)],
    )
    assert check_structure_compatible("public", {"t": ts}, "public", {"t": other})
    assert check_structure_compatible("public", {"t": ts}, "public", {"u": ts})


def test_eligibility_rejects_fk_not_pointing_to_pk():
    p = _table(
        "p",
        [_col("id"), _col("a"), _col("b")],
        uniques=[_uk("p_pk", ["id"], True), _uk("p_ab_uk", ["a", "b"])],
    )
    c = _table(
        "c",
        [_col("id"), _col("x"), _col("y")],
        uniques=[_uk("c_pk", ["id"], True)],
        fks=[_fk("c_p_fk", ["x", "y"], "p", ["a", "b"])],  # 指向普通唯一键而非主键
    )
    schemas = {"p": p, "c": c}
    data = {"p": [], "c": []}
    problems = check_merge_eligible(schemas, data, data, 2, 8)
    assert any("主键" in reason for _, reason in problems)


def test_eligibility_limits_tables_and_rows():
    schemas = {f"t{i}": _single_pk_table(name=f"t{i}") for i in range(3)}
    data = {f"t{i}": [] for i in range(3)}
    problems = check_merge_eligible(schemas, data, data, 2, 8)
    assert any("至多 2 张表" in reason for _, reason in problems)

    one = {"t": _single_pk_table()}
    nine_rows = {"t": [{"id": i} for i in range(9)]}
    empty = {"t": []}
    problems = check_merge_eligible(one, nine_rows, empty, 2, 8)
    assert any("至多 8 行" in reason for _, reason in problems)
    assert check_merge_eligible(one, empty, empty, 2, 8) == []


# --------------------------------------------------------------------------- orchestration
def test_run_merge_reports_no_solution_separately():
    ts = _table(
        "t",
        [_col("id"), _col("code")],
        uniques=[_uk("t_pk", ["id"], True), _uk("t_code_uk", ["code"])],
    )
    j1 = _plant_job({"t": ts}, {"t": [{"id": 1, "code": 100}]})
    j2 = _plant_job({"t": ts}, {"t": [{"id": 2, "code": 100}]})
    mid = MERGE_STORE.create(j1, j2)
    run_merge(mid, j1, j2)
    m = MERGE_STORE.get(mid)
    assert m.status == JobStatus.FAILED
    assert m.conflict.kind == "no_solution"
    assert "unique" in m.conflict.details["kinds"]
    assert MERGE_STORE.package(mid) is None  # 不产出部分包


def test_run_merge_reports_verification_failure_separately():
    ts = _single_pk_table()
    j1 = _plant_job({"t": ts}, {"t": [{"id": 1}]})
    j2 = _plant_job({"t": ts}, {"t": [{"id": 1}]})
    mid = MERGE_STORE.create(j1, j2)
    original = settings.verify_dsn
    # 指向一个拒绝连接的地址，强制试装失败
    object.__setattr__(settings, "verify_dsn", "postgresql://postgres@127.0.0.1:1/postgres")
    try:
        run_merge(mid, j1, j2)
    finally:
        object.__setattr__(settings, "verify_dsn", original)
    m = MERGE_STORE.get(mid)
    assert m.status == JobStatus.FAILED
    assert m.conflict.kind == "verification"
    assert MERGE_STORE.package(mid) is None


def test_run_merge_structure_mismatch():
    t1 = _table("t1", [_col("id")], uniques=[_uk("t1_pk", ["id"], True)])
    t2 = _table("t2", [_col("id")], uniques=[_uk("t2_pk", ["id"], True)])
    j1 = _plant_job({"t1": t1}, {"t1": [{"id": 1}]})
    j2 = _plant_job({"t2": t2}, {"t2": [{"id": 1}]})
    mid = MERGE_STORE.create(j1, j2)
    run_merge(mid, j1, j2)
    m = MERGE_STORE.get(mid)
    assert m.status == JobStatus.FAILED
    assert m.conflict.kind == "structure_mismatch"


def test_run_merge_unsupported_limits():
    ts = _single_pk_table()
    j1 = _plant_job({"t": ts}, {"t": [{"id": i} for i in range(1, 10)]})  # 9 行 > 8
    j2 = _plant_job({"t": ts}, {"t": [{"id": 100}]})
    mid = MERGE_STORE.create(j1, j2)
    run_merge(mid, j1, j2)
    m = MERGE_STORE.get(mid)
    assert m.status == JobStatus.FAILED
    assert m.conflict.kind == "unsupported"


def test_run_merge_source_job_not_succeeded():
    ts = _single_pk_table()
    j1 = _plant_job({"t": ts}, {"t": [{"id": 1}]})
    j2 = STORE.create()  # PENDING，无产物
    mid = MERGE_STORE.create(j1, j2)
    run_merge(mid, j1, j2)
    m = MERGE_STORE.get(mid)
    assert m.status == JobStatus.FAILED
    assert m.conflict.kind == "source_jobs"


# --------------------------------------------------------------------------- end to end
def test_merge_end_to_end_two_independent_jobs(fresh_verify_database):
    req = _load("request_mergeable_dept_employee.json")
    jid1, job1 = _run(req)
    jid2, job2 = _run(req)
    assert job1.status == JobStatus.SUCCEEDED
    assert job2.status == JobStatus.SUCCEEDED
    pkg1_before = dict(STORE.package(jid1))
    pkg2_before = dict(STORE.package(jid2))
    data1_before = copy.deepcopy(STORE.artifacts(jid1)["data"])
    data2_before = copy.deepcopy(STORE.artifacts(jid2)["data"])

    mid = MERGE_STORE.create(jid1, jid2)
    run_merge(mid, jid1, jid2)
    merge = MERGE_STORE.get(mid)
    assert merge.status == JobStatus.SUCCEEDED, merge.conflict
    assert merge.row_counts == {"dept": 6, "employee": 8}

    # 两个来源包与来源数据均未改动
    assert STORE.package(jid1) == pkg1_before
    assert STORE.package(jid2) == pkg2_before
    assert STORE.artifacts(jid1)["data"] == data1_before
    assert STORE.artifacts(jid2)["data"] == data2_before

    pkg = MERGE_STORE.package(mid)
    assert set(pkg) == {
        "01_schema.sql",
        "02_data.sql",
        "03_constraints.sql",
        "manifest.json",
        "README.md",
    }
    manifest = json.loads(pkg["manifest.json"])
    assert manifest["package_kind"] == "coload_merge"
    assert manifest["source_jobs"] == {"first": jid1, "second": jid2}
    assert manifest["row_counts"] == {"dept": 6, "employee": 8}
    assert manifest["row_counts_by_source"]["employee"] == {"first": 4, "second": 4}
    assert manifest["contains_real_patient_data"] is False

    # 合并包在全新空库真实装载成功（约束负向探测已在 run_merge 内通过）
    dsn, _ = fresh_verify_database
    art_a = STORE.artifacts(jid1)
    art_b = STORE.artifacts(jid2)
    with psycopg.connect(dsn, autocommit=False) as conn:
        for f in ("01_schema.sql", "02_data.sql", "03_constraints.sql"):
            conn.cursor().execute(pkg[f])
        conn.commit()
        cur = conn.cursor()
        for t in ("dept", "employee"):
            cols = [c.name for c in art_a["schemas"][t].columns]
            cols_sql = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f"SELECT {cols_sql} FROM public.{t}")
            merged_rows = [tuple(r) for r in cur.fetchall()]
            assert len(merged_rows) == len(art_a["data"][t]) + len(art_b["data"][t])
            # 第一份样本全部值原样保留：每一行都能逐值找到
            remaining = list(merged_rows)
            for row in art_a["data"][t]:
                tup = tuple(row[c] for c in cols)
                assert tup in remaining
                remaining.remove(tup)
            # 剩余即第二份样本的合并版：NULL 位置与原第二份完全一致
            assert len(remaining) == len(art_b["data"][t])
            pattern = lambda r: tuple(v is None for v in r)  # noqa: E731
            assert sorted(pattern(r) for r in remaining) == sorted(
                pattern(tuple(row[c] for c in cols)) for row in art_b["data"][t]
            )


def test_merge_http_flow_and_download():
    req = _load("request_mergeable_dept_employee.json")
    jid1, job1 = _run(req)
    jid2, job2 = _run(req)
    assert job1.status == JobStatus.SUCCEEDED
    assert job2.status == JobStatus.SUCCEEDED

    r = client.post("/merges", json={"job_a": jid1, "job_b": jid2})
    assert r.status_code == 202
    mid = r.json()["merge_id"]
    merge = _wait_merge(mid)
    assert merge.status == JobStatus.SUCCEEDED, merge.conflict

    r2 = client.get(f"/merges/{mid}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "SUCCEEDED"
    assert body["job_a"] == jid1 and body["job_b"] == jid2
    assert body["row_counts"] == {"dept": 6, "employee": 8}

    r3 = client.get(f"/merges/{mid}/download")
    assert r3.status_code == 200
    zf = zipfile.ZipFile(BytesIO(r3.content))
    assert set(zf.namelist()) == {
        "01_schema.sql",
        "02_data.sql",
        "03_constraints.sql",
        "manifest.json",
        "README.md",
    }
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["source_jobs"] == {"first": jid1, "second": jid2}


def test_merge_rejects_unknown_jobs():
    r = client.post("/merges", json={"job_a": "nope1", "job_b": "nope2"})
    assert r.status_code == 404


def test_merge_rejects_same_job_twice():
    jid = STORE.create()
    r = client.post("/merges", json={"job_a": jid, "job_b": jid})
    assert r.status_code == 422


def test_merge_requires_succeeded_jobs():
    pending = STORE.create()
    jid, job = _run(_load("request_mergeable_dept_employee.json"))
    assert job.status == JobStatus.SUCCEEDED
    r = client.post("/merges", json={"job_a": jid, "job_b": pending})
    assert r.status_code == 409


def test_merge_unknown_merge_id_is_404():
    assert client.get("/merges/nope").status_code == 404
    assert client.get("/merges/nope/download").status_code == 404


def test_merge_download_blocked_until_success():
    ts = _table(
        "t",
        [_col("id"), _col("code")],
        uniques=[_uk("t_pk", ["id"], True), _uk("t_code_uk", ["code"])],
    )
    j1 = _plant_job({"t": ts}, {"t": [{"id": 1, "code": 100}]})
    j2 = _plant_job({"t": ts}, {"t": [{"id": 2, "code": 100}]})
    mid = MERGE_STORE.create(j1, j2)
    run_merge(mid, j1, j2)  # 无解 -> FAILED
    assert MERGE_STORE.get(mid).status == JobStatus.FAILED
    r = client.get(f"/merges/{mid}/download")
    assert r.status_code == 409


def test_concurrent_jobs_and_merges_do_not_corrupt_z3():
    """z3 上下文非线程安全：并发生成/合装必须串行化求解段（回归测试）。"""
    import threading

    req = _load("request_mergeable_dept_employee.json")
    outcomes: dict[str, JobStatus] = {}

    def gen(tag):
        jid, job = _run(req)
        outcomes[tag] = job.status
        return jid

    jobs: dict[str, str] = {}

    def gen_keep(tag):
        jobs[tag], _ = _run(req)
        outcomes[tag] = STORE.get(jobs[tag]).status

    threads = [threading.Thread(target=gen_keep, args=(f"g{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(st == JobStatus.SUCCEEDED for st in outcomes.values())

    def merge_one():
        mid = MERGE_STORE.create(jobs["g0"], jobs["g1"])
        run_merge(mid, jobs["g0"], jobs["g1"])
        outcomes["merge"] = MERGE_STORE.get(mid).status

    t1 = threading.Thread(target=merge_one)
    t2 = threading.Thread(target=gen, args=("g3",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert outcomes["merge"] == JobStatus.SUCCEEDED
    assert outcomes["g3"] == JobStatus.SUCCEEDED
