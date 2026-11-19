"""FastAPI application.

Endpoints
----------
GET  /healthz                 liveness
GET  /schemas/{schema}/tables list ordinary base tables (catalog read only)
GET  /schemas/{schema}/tables/{table}/structure
                              full supported-structure view + rejections
POST /jobs                    create a generation/verification job
GET  /jobs/{job_id}           status, conflict report, logs
GET  /jobs/{job_id}/download  zip package, available only after SUCCEEDED
POST /merges                  co-load the verified samples of two succeeded jobs
GET  /merges/{merge_id}       merge status, conflict report, logs
GET  /merges/{merge_id}/download
                              merged zip package, available only after SUCCEEDED
GET  /docs                    OpenAPI Swagger UI
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from .config import settings
from .db import source_connection
from .introspect import (
    IntrospectionError,
    introspect_tables,
    list_base_tables,
)
from .jobs import MERGE_STORE, STORE, package_zip, run_job, run_merge
from .models import GenerateRequest, JobStatus, MergeRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="数仓关联样本生成服务",
    version="1.1.0",
    description=(
        "只读源库表结构（不读行数据），基于 z3 生成同时满足指定行数、整数闭区间与"
        "主键/唯一键/可空/MATCH SIMPLE/FULL 复合及循环外键约束的合成样本，"
        "并在隔离空库实际装载验证通过后才开放 SQL 包下载。"
        "合装 API 把两个成功作业的样本合并为一个 SQL 包：第一份全部值保留，"
        "第二份仅主键与有效引用外键列可在整数类型边界内改写，NULL 位置不变，"
        "合并包同样须在隔离空库试装成功后才返回。"
    ),
    lifespan=lifespan,
)


@app.get("/healthz", tags=["ops"])
def healthz():
    return {"status": "ok"}


@app.get("/schemas/{schema}/tables", tags=["catalog"])
def list_tables(schema: str):
    """List ordinary base tables; non-base relations are flagged, not read."""
    try:
        with source_connection(settings.source_dsn) as conn:
            base, others = list_base_tables(conn, schema)
    except Exception as exc:
        raise HTTPException(502, f"无法连接源库: {exc.__class__.__name__}: {exc}")
    return {
        "schema": schema,
        "base_tables": base,
        "excluded_non_base_relations": others,
        "max_selectable_tables": settings.max_tables,
        "max_rows_per_table": settings.max_rows_per_table,
    }


@app.get("/schemas/{schema}/tables/{table}/structure", tags=["catalog"])
def table_structure(schema: str, table: str):
    """Return the supported structural view, or explicit rejections."""
    try:
        with source_connection(settings.source_dsn) as conn:
            found = introspect_tables(conn, schema, [table])
    except IntrospectionError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "table": f"{schema}.{table}",
                "supported": False,
                "rejections": [
                    {"object": o, "reason": r} for o, r in exc.rejections
                ],
            },
        )
    except Exception as exc:
        raise HTTPException(502, f"无法连接源库: {exc.__class__.__name__}: {exc}")
    ts = found[table]
    return {"table": f"{schema}.{table}", "supported": True, "structure": ts.model_dump()}


@app.post("/jobs", tags=["jobs"], status_code=202)
def create_job(req: GenerateRequest, background: BackgroundTasks):
    if not req.tables:
        raise HTTPException(422, "tables 不能为空")
    if len({t.table for t in req.tables}) != len(req.tables):
        raise HTTPException(422, "请求中存在重复表")
    if len(req.tables) > settings.max_tables:
        raise HTTPException(
            422, f"至多选择 {settings.max_tables} 张表（不含外键闭包扩展）"
        )
    job_id = STORE.create()
    background.add_task(run_job, job_id, req)
    return {"job_id": job_id, "status": JobStatus.PENDING.value}


@app.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str):
    job = STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "job 不存在或已过期")
    return job.model_dump()


@app.get("/jobs/{job_id}/download", tags=["jobs"])
def download_job(job_id: str):
    job = STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "job 不存在或已过期")
    if job.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            409,
            f"作业尚未验证成功（当前状态 {job.status.value}）；失败作业不提供部分包下载",
        )
    package = STORE.package(job_id)
    if package is None:
        raise HTTPException(409, "SQL 包不可用")
    data = package_zip(package)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="sample_{job_id}.zip"',
            "X-Contains-Real-Patient-Data": "false",
        },
    )


@app.post("/merges", tags=["merge"], status_code=202)
def create_merge(req: MergeRequest, background: BackgroundTasks):
    """合装两个已成功作业的样本（后台执行），返回 `merge_id`。

    限结构相同、外键均指向主键、各至多两表且每表最多 8 行。第一份样本
    全部值保留；第二份仅主键与有效引用外键列可在整数类型边界内改写，
    NULL 位置不变，每条有效引用仍指向原记录的合并版。合并包在隔离空库
    试装成功后才可下载；无解、超时、试装失败分别报告，不返回部分包，
    也不修改两个来源包。
    """
    if req.job_a == req.job_b:
        raise HTTPException(422, "两个来源作业号必须不同（合装要求两份独立生成的样本）")
    job_a = STORE.get(req.job_a)
    job_b = STORE.get(req.job_b)
    if job_a is None or job_b is None:
        raise HTTPException(404, "来源作业不存在或已过期")
    if job_a.status != JobStatus.SUCCEEDED or job_b.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            409,
            "两个来源作业都必须已验证成功（SUCCEEDED）才能合装；"
            f"当前状态: {req.job_a}={job_a.status.value}, {req.job_b}={job_b.status.value}",
        )
    if STORE.artifacts(req.job_a) is None or STORE.artifacts(req.job_b) is None:
        raise HTTPException(409, "来源作业的结构化产物不可用，无法合装")
    merge_id = MERGE_STORE.create(req.job_a, req.job_b)
    background.add_task(run_merge, merge_id, req.job_a, req.job_b)
    return {"merge_id": merge_id, "status": JobStatus.PENDING.value}


@app.get("/merges/{merge_id}", tags=["merge"])
def get_merge(merge_id: str):
    """合装状态、冲突报告（无解/超时/试装失败分别报告）与逐步日志。"""
    merge = MERGE_STORE.get(merge_id)
    if merge is None:
        raise HTTPException(404, "合装作业不存在或已过期")
    return merge.model_dump()


@app.get("/merges/{merge_id}/download", tags=["merge"])
def download_merge(merge_id: str):
    """仅在合装验证成功后下载合并 zip 包（含 5 个文件）。"""
    merge = MERGE_STORE.get(merge_id)
    if merge is None:
        raise HTTPException(404, "合装作业不存在或已过期")
    if merge.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            409,
            f"合装尚未验证成功（当前状态 {merge.status.value}）；失败不返回部分包",
        )
    package = MERGE_STORE.package(merge_id)
    if package is None:
        raise HTTPException(409, "合装 SQL 包不可用")
    data = package_zip(package)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="coload_{merge_id}.zip"',
            "X-Contains-Real-Patient-Data": "false",
        },
    )
