"""模块级自测：不依赖 HTTP 层，直接验证修订服务的顺序/缺口/幂等/冲突语义。

主要用于本地快速验证；完整 API 场景见 tests/test_api.py 与 tests/test_revisions.py。
运行：DATABASE_URL=... python3 -m app.revisions_selfcheck
"""
from __future__ import annotations

import sys
import uuid

from . import db, revisions


def main() -> int:
    db.init_pool()
    db.ensure_schema()
    try:
        # 每次使用唯一报告号，避免在已有唯一索引/历史数据的库上重复运行冲突。
        with db.pool().connection() as conn:
            rid = conn.execute(
                "INSERT INTO reports(lab_no, report_no, measured) "
                "VALUES ('SELF', %s, 0) RETURNING id",
                (f"CHECK-{uuid.uuid4().hex[:12]}",),
            ).fetchone()["id"]
            conn.commit()
        failures = []
        for no, val in ((2, 20), (3, 30)):
            out = revisions.submit_revision(rid, no, val)
            if out["outcome"] != "pending" or out["effective_revision"] != 0:
                failures.append(f"rev {no} should be pending, got {out}")
            with db.pool().connection() as conn:
                visible = conn.execute(
                    "SELECT measured FROM reports WHERE id=%s", (rid,)
                ).fetchone()["measured"]
            if visible != 0:
                failures.append(f"visible changed before gap filled: {visible}")
        out = revisions.submit_revision(rid, 1, 10)
        if out["outcome"] != "applied" or out["effective_revision"] != 3 \
                or out["effective_measured"] != 30 or out["applied_now"] != [1, 2, 3]:
            failures.append(f"gap fill failed: {out}")
        if revisions.submit_revision(rid, 2, 20)["outcome"] != "duplicate":
            failures.append("same revision+value must be duplicate")
        try:
            revisions.submit_revision(rid, 2, 999)
            failures.append("same revision different value must be rejected")
        except revisions.RevisionConflict:
            pass
        state = revisions.list_revisions(rid)
        if state["next_required_revision"] != 4:
            failures.append(f"next_required_revision wrong: {state}")
        if failures:
            print("\n".join(failures))
            return 1
        print("revision self-check ok")
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main())
