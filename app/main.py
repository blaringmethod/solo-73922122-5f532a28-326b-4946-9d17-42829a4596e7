"""FastAPI 入口：补录、测值修订、唯一索引构建的发起/查询/取消。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg
import psycopg_pool
from fastapi import FastAPI, HTTPException, status

from . import db, revisions
from .responses import AppJSONResponse
from .schemas import (
    BatchReportIn,
    ReportIn,
    ReportOut,
    RevisionIn,
    RevisionListOut,
    RevisionOut,
)
from .service import service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_pool()
    db.ensure_revision_schema()
    service.reconcile_on_startup()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="检验报告唯一性保障与测值修订 API",
    description=(
        "对持续补录的 reports 表在线构建 (lab_no, report_no) 唯一索引；"
        "支持发起、查询、取消 CREATE UNIQUE INDEX CONCURRENTLY，"
        "构建期间补录不阻塞，API 重启后接续跟踪。\n\n"
        "同时提供按报告主键提交测值修订：修订号从 1 起递增、按序连续生效，"
        "缺口后的修订只暂存不改当前值，同号同值重发幂等、同号异值拒绝；"
        "并发提交按报告串行化，失败整体回滚，响应给出生效号与下一待补号。"
    ),
    version="1.1.0",
    default_response_class=AppJSONResponse,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    with db.pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ------------------------------ 补录 ------------------------------------

@app.post("/reports", response_model=ReportOut, status_code=201)
def insert_report(payload: ReportIn):
    """单条补录。构建期间（CONCURRENTLY）可正常提交；唯一索引生效后重复键返回 409。"""
    try:
        with db.pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO reports (lab_no, report_no, measured) "
                "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                (payload.lab_no, payload.report_no, payload.measured),
            ).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_report",
                "message": "同一实验室下报告号已存在，本条补录被拒绝，原有数据保持不变",
                "lab_no": payload.lab_no,
                "report_no": payload.report_no,
                "pg_error": str(exc).strip(),
            },
        )
    return dict(row)


@app.post("/reports/batch")
def insert_reports_batch(payload: BatchReportIn):
    """批量补录：逐条保存，重复键跳过且不影响其它行，返回成功/跳过明细。"""
    inserted, skipped = [], []
    with db.pool().connection() as conn:
        for item in payload.items:
            try:
                row = conn.execute(
                    "INSERT INTO reports (lab_no, report_no, measured) "
                    "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                    (item.lab_no, item.report_no, item.measured),
                ).fetchone()
                inserted.append(dict(row))
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                skipped.append(
                    {"lab_no": item.lab_no, "report_no": item.report_no,
                     "reason": "duplicate_report"}
                )
        conn.commit()
    return {"inserted_count": len(inserted), "skipped_count": len(skipped),
            "inserted": inserted, "skipped": skipped}


@app.get("/reports")
def list_reports(limit: int = 50, offset: int = 0):
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, lab_no, report_no, measured, effective_revision FROM reports "
            "ORDER BY id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}


# ------------------------------ 测值修订 ---------------------------------

@app.post("/reports/{report_id}/revisions", response_model=RevisionOut)
def submit_revision(report_id: int, payload: RevisionIn):
    """按报告主键提交一条测值修订。

    * 修订号从 1 起递增；前序缺失时保存为待补，不改当前生效值；
    * 前序补齐后在本事务内连续应用，缺口后的修订不会越过缺口生效；
    * 同号同值重发幂等返回 ``duplicate``，不重复应用；同号异值返回 409；
    * 响应给出当前生效号与下一个待补号，供实验室重传。
    """
    try:
        return revisions.submit_revision(report_id, payload.revision_no, payload.measured)
    except revisions.ReportNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "report_not_found", "message": "报告主键不存在",
                    "report_id": report_id},
        )
    except revisions.RevisionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "revision_conflict",
                "message": "同一修订号已保存不同测值，本次提交被拒绝，已有数据保持不变",
                "report_id": report_id,
                "revision_no": exc.revision_no,
                "stored_measured": exc.stored_measured,
                "submitted_measured": exc.submitted_measured,
                # 同时给出当前状态，便于实验室直接重传正确号段。
                "effective_revision": exc.effective_revision,
                "next_required_revision": exc.next_required_revision,
            },
        )
    except revisions.RevisionBusy as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
            detail={
                "code": "revision_busy",
                "message": "该报告上有并发修订提交尚未结束，本次未写入，请稍后重传",
                "report_id": report_id,
                # 当前状态一并给出，实验室可据此重传。
                "effective_revision": exc.effective_revision,
                "next_required_revision": exc.next_required_revision,
            },
        )
    except psycopg_pool.PoolTimeout:
        # 连接池在超时窗口内没有可用连接：本次提交未执行，可直接重传。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
            detail={
                "code": "revision_pool_exhausted",
                "message": "数据库连接繁忙，本次修订未写入，请稍后重传",
                "report_id": report_id,
            },
        )
    except psycopg.Error as exc:
        # 事务已在访问层整体回滚：修订记录、生效号、可见测值保持一致。
        log.exception("revision submit failed for report %s", report_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "revision_submit_failed",
                "message": "修订提交失败，已整体回滚，未产生部分生效",
                "report_id": report_id,
                "pg_error": str(exc).strip(),
            },
        )


@app.get("/reports/{report_id}/revisions", response_model=RevisionListOut)
def get_revisions(report_id: int):
    """查看报告的生效状态与全部修订记录（含尚未生效的缺口后修订）。"""
    try:
        return revisions.list_revisions(report_id)
    except revisions.ReportNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "report_not_found", "message": "报告主键不存在",
                    "report_id": report_id},
        )


# --------------------------- 唯一索引构建 --------------------------------

@app.post("/index/build", status_code=202)
def start_build():
    """发起（或复用）CREATE UNIQUE INDEX CONCURRENTLY。

    * 已有活跃轮次：复用同一轮，不新开；
    * 索引已有效：直接返回 complete；
    * 失败后的无效残留索引会先安全清理，绝不删除有效索引。
    """
    view, code = service.start_build()
    return AppJSONResponse(view, status_code=code)


@app.get("/index/status")
def index_status(round_id: str | None = None):
    """查询构建轮次状态，并附带 pg_index 中的真实索引有效性。"""
    return service.get_status(round_id)


@app.post("/index/cancel", status_code=202)
def cancel_build(round_id: str | None = None):
    """取消构建：先受理（cancel_requested），后端 SQL 确认停止后才转 cancelled。

    只向记录在案的构建后端 PID 发 pg_cancel_backend，不影响补录连接；
    若索引已有效则报 complete。
    """
    view, code = service.request_cancel(round_id)
    return AppJSONResponse(view, status_code=code)
