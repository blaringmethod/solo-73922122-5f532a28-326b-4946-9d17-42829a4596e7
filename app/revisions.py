"""按 reports.id 提交测值修订：顺序生效、缺口等待、幂等与冲突拒绝。

一致性要点
----------
* 每次提交在**单个数据库事务**内完成。事务开始时先取该报告的 advisory lock
  （``pg_try_advisory_xact_lock``，非阻塞重试），同一报告的并发提交因此
  串行化；不同报告使用不同锁，互不阻塞。选择 advisory lock 而不是
  ``SELECT ... FOR UPDATE``：两者语义等价，但前者不排入行锁等待队列，
  不会因环境限制（如沙箱禁止 PostgreSQL 发送锁唤醒信号）而永久挂起。
* 修订号从 1 起递增。只有当 ``effective_revision + 1`` 对应的修订已经存在时，
  才应用它并把 ``reports.effective_revision`` / ``reports.measured`` 一起推进；
  缺口之后的修订只保存为 ``applied = false``，绝不越过缺口、也不部分生效。
* 同号同值重发：识别为幂等重复，不重复应用；同号异值：拒绝（冲突）。
* 任一语句失败都会回滚整个事务，修订记录、生效号与可见测值不会出现半更新。
"""
from __future__ import annotations

import time
from typing import Any

import psycopg

from . import db

# 单次提交等待同一报告上的并发提交结束的最长时间（秒）。
LOCK_WAIT_TIMEOUT_S = 15.0
# 重试间隔从 5ms 起指数退避，上限 100ms，避免空转。
LOCK_RETRY_MIN_S = 0.005
LOCK_RETRY_MAX_S = 0.1
# advisory lock 键为 64 位：高 32 位是命名空间，低 32 位是报告主键（超出 2^32 的
# 主键会与低 32 位相同的报告共用一把锁——仅增加串行度，不影响正确性）。
REVISION_LOCK_NAMESPACE = 0x52455631  # "REV1"


def revision_lock_key(report_id: int) -> int:
    return (REVISION_LOCK_NAMESPACE << 32) | (report_id & 0xFFFFFFFF)


class ReportNotFound(Exception):
    """报告主键不存在。"""


class RevisionConflict(Exception):
    """同一修订号已被不同测值占用。"""

    def __init__(self, revision_no: int, stored_measured: int, submitted_measured: int,
                 effective_revision: int = 0):
        self.revision_no = revision_no
        self.stored_measured = stored_measured
        self.submitted_measured = submitted_measured
        self.effective_revision = effective_revision
        self.next_required_revision = effective_revision + 1
        super().__init__(
            f"revision {revision_no} already stored with measured={stored_measured}, "
            f"refusing different measured={submitted_measured}"
        )


class RevisionBusy(Exception):
    """同一报告上的并发提交长时间未结束，本次提交未能取得串行化锁。"""

    def __init__(self, report_id: int, effective_revision: int = 0):
        self.report_id = report_id
        self.effective_revision = effective_revision
        self.next_required_revision = effective_revision + 1
        super().__init__(
            f"report {report_id} is busy with another revision submit "
            f"(effective_revision={effective_revision})"
        )


def _lock_report(conn: psycopg.Connection, report_id: int) -> None:
    """在事务内取报告级串行化锁；非阻塞重试，超时抛 RevisionBusy。

    ``pg_try_advisory_xact_lock`` 在事务提交/回滚时自动释放。
    """
    deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_S
    delay = LOCK_RETRY_MIN_S
    key = revision_lock_key(report_id)
    while True:
        got = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS ok", (key,)
        ).fetchone()["ok"]
        if got:
            return
        if time.monotonic() >= deadline:
            # 尽力给出当前生效号供实验室参考（不加锁读取，可能略滞后）。
            row = conn.execute(
                "SELECT effective_revision FROM reports WHERE id = %s", (report_id,)
            ).fetchone()
            raise RevisionBusy(report_id, row["effective_revision"] if row else 0)
        time.sleep(delay)
        delay = min(delay * 2, LOCK_RETRY_MAX_S)


def _get_report(conn: psycopg.Connection, report_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, lab_no, report_no, measured, effective_revision "
        "FROM reports WHERE id = %s",
        (report_id,),
    ).fetchone()
    if row is None:
        raise ReportNotFound(report_id)
    return dict(row)


def _apply_contiguous(conn: psycopg.Connection, report_id: int,
                      effective: int) -> tuple[int, int, list[int]]:
    """从 effective+1 起连续应用已存在的修订，返回 (新生效号, 测值, 本轮应用号列表)。

    调用方已持有报告级串行化锁，且处于同一事务；任何一步失败整体回滚。
    """
    applied_now: list[int] = []
    effective_measured: int | None = None
    while True:
        next_no = effective + 1
        rev = conn.execute(
            "SELECT revision_no, measured FROM report_revisions "
            "WHERE report_id = %s AND revision_no = %s",
            (report_id, next_no),
        ).fetchone()
        if rev is None:
            break
        conn.execute(
            "UPDATE report_revisions SET applied = TRUE, applied_at = now() "
            "WHERE report_id = %s AND revision_no = %s",
            (report_id, next_no),
        )
        # 生效号与可见测值在同一条 UPDATE 中推进，二者不会分离。
        conn.execute(
            "UPDATE reports SET measured = %s, effective_revision = %s WHERE id = %s",
            (rev["measured"], next_no, report_id),
        )
        effective = next_no
        effective_measured = rev["measured"]
        applied_now.append(next_no)

    if effective_measured is None:
        # 没有推进时以报告当前生效测值为准（持锁后读取，值稳定）。
        row = conn.execute(
            "SELECT measured FROM reports WHERE id = %s", (report_id,)
        ).fetchone()
        effective_measured = row["measured"]
    return effective, effective_measured, applied_now


def submit_revision(report_id: int, revision_no: int, measured: int) -> dict[str, Any]:
    """保存并（在连续时）应用一条修订。返回对外视图。

    ``outcome`` 取值：
    * ``applied``   —— 本次提交使它（以及此前暂存的连续后续修订）生效；
    * ``pending``   —— 已保存，但前序修订缺失，当前生效值不变；
    * ``duplicate`` —— 同号同值重发，识别为幂等重复，未重复应用。

    ``effective_revision`` / ``effective_measured`` 为当前已生效修订号和测值；
    ``next_required_revision`` 为下一个待补号，供实验室重传。
    """
    with db.pool().connection() as conn:
        _lock_report(conn, report_id)
        report = _get_report(conn, report_id)
        effective = report["effective_revision"]

        # 已持有报告级锁，无需行锁（行锁在受限环境下可能无法被唤醒）。
        existing = conn.execute(
            "SELECT measured, applied FROM report_revisions "
            "WHERE report_id = %s AND revision_no = %s",
            (report_id, revision_no),
        ).fetchone()

        newly_recorded = False
        if existing is not None:
            if existing["measured"] != measured:
                # 同号异值：拒绝且不改动任何数据。
                raise RevisionConflict(revision_no, existing["measured"], measured,
                                       effective)
            outcome = "duplicate"
        else:
            if revision_no <= effective:
                # 正常情况下不会发生（已生效的号必有记录）；保守拒绝，避免破坏不变量。
                raise RevisionConflict(revision_no, report["measured"], measured,
                                       effective)
            conn.execute(
                "INSERT INTO report_revisions (report_id, revision_no, measured, applied) "
                "VALUES (%s, %s, %s, FALSE)",
                (report_id, revision_no, measured),
            )
            newly_recorded = True
            outcome = "pending"

        effective, effective_measured, applied_now = _apply_contiguous(
            conn, report_id, effective
        )
        if newly_recorded and revision_no <= effective:
            outcome = "applied"

        # 显式提交；任何异常都会让连接上下文整体回滚，不会留下部分生效。
        conn.commit()
        return {
            "report_id": report_id,
            "revision_no": revision_no,
            "measured": measured,
            "outcome": outcome,
            "newly_recorded": newly_recorded,
            "applied_now": applied_now,
            "effective_revision": effective,
            "effective_measured": effective_measured,
            "next_required_revision": effective + 1,
        }


def list_revisions(report_id: int) -> dict[str, Any]:
    """查看一份报告的生效状态与全部修订（含尚未生效的缺口后修订）。"""
    with db.pool().connection() as conn:
        report = conn.execute(
            "SELECT id, lab_no, report_no, measured, effective_revision "
            "FROM reports WHERE id = %s",
            (report_id,),
        ).fetchone()
        if report is None:
            raise ReportNotFound(report_id)
        rows = conn.execute(
            "SELECT revision_no, measured, applied, created_at, applied_at "
            "FROM report_revisions WHERE report_id = %s ORDER BY revision_no",
            (report_id,),
        ).fetchall()
    return {
        "report_id": report["id"],
        "lab_no": report["lab_no"],
        "report_no": report["report_no"],
        "effective_revision": report["effective_revision"],
        "effective_measured": report["measured"],
        "next_required_revision": report["effective_revision"] + 1,
        "revisions": [dict(r) for r in rows],
    }
