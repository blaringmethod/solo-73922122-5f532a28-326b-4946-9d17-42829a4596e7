"""测值修订 API 自测：乱序、并发、幂等、冲突、失败回滚与可见值一致性。"""
from __future__ import annotations

import threading
import time

import psycopg
import pytest
from a2wsgi import ASGIMiddleware
import httpx

from app import db as dbmod
from app.config import settings
from app.main import app


_SHARED_ASGI = ASGIMiddleware(app)


def _new_client() -> httpx.Client:
    """每个线程独立的客户端。

    所有客户端共享同一个 ASGI 适配器（其中只有一个事件循环线程）：
    既避免并发共享同一连接对象，也避免为每次请求新建事件循环线程
    （受限沙箱的进程/线程配额很小）。FastAPI 的同步端点仍会在线程池中
    并发执行，因此并发语义不受影响。
    """
    return httpx.Client(transport=httpx.WSGITransport(app=_SHARED_ASGI),
                        base_url="http://test")


def _submit_isolated(rid, rev, value):
    with _new_client() as c:
        return c.post(f"/reports/{rid}/revisions",
                      json={"revision_no": rev, "measured": value})


def _make_report(client, lab="L", no="R", measured=100) -> int:
    r = client.post("/reports", json={"lab_no": lab, "report_no": no, "measured": measured})
    assert r.status_code == 201
    return r.json()["id"]


def _submit(client, rid, rev, value):
    return client.post(f"/reports/{rid}/revisions",
                       json={"revision_no": rev, "measured": value})


def _visible(client, rid) -> dict:
    items = client.get("/reports", params={"limit": 500}).json()["items"]
    row = next(i for i in items if i["id"] == rid)
    return row


def _state(client, rid) -> dict:
    return client.get(f"/reports/{rid}/revisions").json()


def test_out_of_order_revisions_apply_contiguously(client):
    rid = _make_report(client, measured=100)

    # 2 先到：保存但不改当前值，提示下一个待补号是 1。
    r = _submit(client, rid, 2, 220)
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "pending"
    assert body["effective_revision"] == 0
    assert body["effective_measured"] == 100
    assert body["next_required_revision"] == 1
    assert _visible(client, rid)["measured"] == 100

    # 3 再到：仍不改当前值，待补号仍为 1（不得越过缺口）。
    body = _submit(client, rid, 3, 330).json()
    assert body["outcome"] == "pending"
    assert body["effective_revision"] == 0
    assert body["next_required_revision"] == 1
    assert _visible(client, rid)["measured"] == 100

    # 1 补齐：2 与 3 在同一次提交中连续生效，可见值一次到位。
    body = _submit(client, rid, 1, 110).json()
    assert body["outcome"] == "applied"
    assert body["applied_now"] == [1, 2, 3]
    assert body["effective_revision"] == 3
    assert body["effective_measured"] == 330
    assert body["next_required_revision"] == 4
    assert _visible(client, rid)["measured"] == 330

    state = _state(client, rid)
    assert state["effective_revision"] == 3
    assert [r["revision_no"] for r in state["revisions"]] == [1, 2, 3]
    assert all(r["applied"] for r in state["revisions"])


def test_gap_stops_further_application(client):
    rid = _make_report(client, measured=10)
    assert _submit(client, rid, 1, 11).json()["outcome"] == "applied"
    # 3 先存，2 缺：3 不得生效
    assert _submit(client, rid, 3, 13).json()["outcome"] == "pending"
    assert _visible(client, rid)["measured"] == 11
    # 5 也存下：仍在缺口后
    body = _submit(client, rid, 5, 15).json()
    assert body["outcome"] == "pending"
    assert body["effective_revision"] == 1
    assert body["next_required_revision"] == 2
    # 补 2：2 生效，3 连续生效，5 仍等待 4
    body = _submit(client, rid, 2, 12).json()
    assert body["applied_now"] == [2, 3]
    assert body["effective_revision"] == 3
    assert body["effective_measured"] == 13
    assert body["next_required_revision"] == 4
    state = _state(client, rid)
    assert [(r["revision_no"], r["applied"]) for r in state["revisions"]] == [
        (1, True), (2, True), (3, True), (5, False)]


def test_same_revision_same_value_is_idempotent(client):
    rid = _make_report(client, measured=0)
    first = _submit(client, rid, 1, 50).json()
    assert first["outcome"] == "applied" and first["newly_recorded"] is True

    # 网络重试：同号同值重发，不得重复应用
    again = _submit(client, rid, 1, 50)
    assert again.status_code == 200
    body = again.json()
    assert body["outcome"] == "duplicate"
    assert body["newly_recorded"] is False
    assert body["applied_now"] == []
    assert body["effective_revision"] == 1
    assert body["effective_measured"] == 50

    with dbmod.pool().connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM report_revisions WHERE report_id=%s",
            (rid,)).fetchone()["n"]
    assert n == 1

    # 缺口后的待补修订同样支持幂等重发
    assert _submit(client, rid, 3, 70).json()["outcome"] == "pending"
    assert _submit(client, rid, 3, 70).json()["outcome"] == "duplicate"


def test_same_revision_different_value_is_rejected(client):
    rid = _make_report(client, measured=0)
    _submit(client, rid, 1, 10)
    _submit(client, rid, 3, 30)  # 缺口后待补

    r = _submit(client, rid, 3, 99)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "revision_conflict"
    assert detail["revision_no"] == 3
    assert detail["stored_measured"] == 30
    assert detail["submitted_measured"] == 99
    assert detail["effective_revision"] == 1
    assert detail["next_required_revision"] == 2

    # 被拒绝的提交没有改动任何数据
    state = _state(client, rid)
    assert [(r["revision_no"], r["measured"], r["applied"]) for r in state["revisions"]] == [
        (1, 10, True), (3, 30, False)]
    assert _visible(client, rid)["measured"] == 10


def test_unknown_report_returns_404(client):
    assert _submit(client, 987654321, 1, 5).status_code == 404
    assert client.get("/reports/987654321/revisions").status_code == 404


def test_validation_rejects_non_positive_revision(client):
    rid = _make_report(client)
    assert _submit(client, rid, 0, 5).status_code == 422
    assert _submit(client, rid, -1, 5).status_code == 422
    # 超出 PostgreSQL INTEGER 范围应在入口被拒，而不是落库时报错
    assert _submit(client, rid, 2**31, 5).status_code == 422
    assert _submit(client, rid, 1, 2**31).status_code == 422


def test_concurrent_out_of_order_submissions_are_consistent(client):
    """并发补交：所有提交返回后，修订记录、生效号与可见测值必须一致。"""
    rid = _make_report(client, measured=0)
    total = 12
    values = {n: n * 10 for n in range(1, total + 1)}
    # 打乱到达顺序，并由多个线程同时提交（含部分重复重发）。
    order = [4, 1, 7, 2, 12, 5, 3, 9, 6, 11, 8, 10, 1, 4, 12]
    results: dict[int, list] = {}
    errors: list = []
    lock = threading.Lock()

    def worker(n):
        try:
            r = _submit_isolated(rid, n, values[n])
            with lock:
                results.setdefault(n, []).append((r.status_code, r.json()))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    # 用固定大小的工作线程池消费提交队列：乱序、并发且不超出进程/线程配额。
    queue = list(order)
    qlock = threading.Lock()

    def consume():
        while True:
            with qlock:
                if not queue:
                    return
                n = queue.pop()
            worker(n)

    threads = [threading.Thread(target=consume) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    for n, responses in results.items():
        for code, body in responses:
            assert code == 200, body
    # 至少有一次提交把 1 到 total 全部应用。
    assert any(body["effective_revision"] == total for _, body in
               [x for rs in results.values() for x in rs])

    state = _state(client, rid)
    assert state["effective_revision"] == total
    assert state["effective_measured"] == values[total]
    assert state["next_required_revision"] == total + 1
    assert [r["revision_no"] for r in state["revisions"]] == list(range(1, total + 1))
    assert all(r["applied"] for r in state["revisions"])
    assert _visible(client, rid)["measured"] == values[total]

    # 数据库层不变量：生效号 = 连续 applied 的最大号；可见值 = 该号的值。
    with dbmod.pool().connection() as conn:
        row = conn.execute(
            "SELECT measured, effective_revision FROM reports WHERE id=%s",
            (rid,)).fetchone()
        applied = conn.execute(
            "SELECT max(revision_no) AS m, count(*) AS n FROM report_revisions "
            "WHERE report_id=%s AND applied", (rid,)).fetchone()
    assert applied["n"] == total and applied["m"] == total
    assert row["effective_revision"] == applied["m"]
    assert row["measured"] == values[applied["m"]]


def test_concurrent_conflicting_values_only_one_wins(client):
    """同一号、不同值并发提交：恰好一个成功写入，其余 409，数据不撕裂。"""
    rid = _make_report(client, measured=0)
    codes: list[int] = []
    lock = threading.Lock()

    def worker(value):
        r = _submit_isolated(rid, 1, value)
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(v,)) for v in (11, 12, 13, 14, 15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(200) == 1, codes
    assert codes.count(409) == len(codes) - 1, codes
    state = _state(client, rid)
    assert len(state["revisions"]) == 1
    assert state["effective_revision"] == 1
    assert state["effective_measured"] == state["revisions"][0]["measured"]
    assert _visible(client, rid)["measured"] == state["effective_measured"]


def _hold_report_lock(rid: int):
    """在另一连接上持有该报告的串行化 advisory lock，制造并发提交窗口。"""
    from app.revisions import revision_lock_key

    conn = psycopg.connect(settings.database_url)
    conn.execute("SELECT pg_advisory_lock(%s)", (revision_lock_key(rid),))
    return conn


def test_failed_submission_leaves_no_partial_effect(client):
    """并发提交持锁期间，另一方看不到任何部分生效；提交本身保持原子。"""
    rid = _make_report(client, measured=5)
    _submit(client, rid, 2, 20)
    _submit(client, rid, 3, 30)

    blocker = _hold_report_lock(rid)
    try:
        outcome: dict = {}

        def attempt():
            outcome["resp"] = _submit_isolated(rid, 1, 10)

        t = threading.Thread(target=attempt)
        t.start()
        time.sleep(1.5)
        # 阻塞期间：修订未落库、生效号与可见测值均保持不变。
        state = _state(client, rid)
        assert state["effective_revision"] == 0
        assert _visible(client, rid)["measured"] == 5
        assert [r["applied"] for r in state["revisions"]] == [False, False]

        blocker.commit()
        blocker.close()
        t.join(timeout=20)
        assert not t.is_alive()
        resp = outcome["resp"]
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied_now"] == [1, 2, 3]
        assert body["effective_revision"] == 3
        assert _visible(client, rid)["measured"] == 30
    finally:
        try:
            blocker.close()
        except Exception:  # noqa: BLE001
            pass


def test_busy_lock_returns_503_without_writing(client, monkeypatch):
    """同一报告的锁迟迟不释放时，提交快速返回 503，不写任何数据、不无限挂起。"""
    from app import revisions

    rid = _make_report(client, measured=9)
    monkeypatch.setattr(revisions, "LOCK_WAIT_TIMEOUT_S", 0.5)
    blocker = _hold_report_lock(rid)
    try:
        r = _submit(client, rid, 1, 90)
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "revision_busy"
        assert r.headers.get("retry-after") == "1"
        state = _state(client, rid)
        assert state["effective_revision"] == 0
        assert state["revisions"] == []
        assert _visible(client, rid)["measured"] == 9
    finally:
        blocker.commit()
        blocker.close()

    # 释放后重传成功
    body = _submit(client, rid, 1, 90).json()
    assert body["outcome"] == "applied" and body["effective_measured"] == 90


def test_lock_is_per_report_not_global(client, monkeypatch):
    """串行化锁按报告粒度：一份报告上的并发提交不会阻塞其它报告。"""
    from app import revisions

    held = _make_report(client, lab="A", no="1", measured=0)
    other = _make_report(client, lab="B", no="1", measured=0)
    monkeypatch.setattr(revisions, "LOCK_WAIT_TIMEOUT_S", 0.5)
    blocker = _hold_report_lock(held)
    try:
        blocked = _submit(client, held, 1, 10)
        assert blocked.status_code == 503
        # 另一份报告不受影响：若锁是全局的，这里会等满 0.5s 后返回 503。
        other_resp = _submit(client, other, 1, 20)
        assert other_resp.status_code == 200
        assert other_resp.json()["outcome"] == "applied"
    finally:
        blocker.commit()
        blocker.close()


def test_failed_transaction_rolls_back_all_rows(client, monkeypatch):
    """注入失败：应用记录过程中抛错时，插入的修订与生效号一并回滚。"""
    from app import revisions

    rid = _make_report(client, measured=7)
    _submit(client, rid, 2, 22)

    real_apply = revisions._apply_contiguous

    def boom(conn, report_id, effective):
        # 先执行真实应用，再抛错：验证已写入的 applied/effective 也被回滚。
        real_apply(conn, report_id, effective)
        raise RuntimeError("injected failure after apply")

    monkeypatch.setattr(revisions, "_apply_contiguous", boom)
    with pytest.raises(RuntimeError):
        revisions.submit_revision(rid, 1, 11)

    state = _state(client, rid)
    assert state["effective_revision"] == 0
    assert state["effective_measured"] == 7
    assert [(r["revision_no"], r["applied"]) for r in state["revisions"]] == [(2, False)]
    assert _visible(client, rid)["measured"] == 7


def test_revision_does_not_break_unique_index_build(client):
    """修订功能与在线建唯一索引共存：建索引后仍未生效的修订保持不变。"""
    rid = _make_report(client, lab="IDX", no="1", measured=0)
    _submit(client, rid, 2, 20)
    client.post("/index/build")
    from conftest import poll_status
    poll_status(client, "complete")
    body = _submit(client, rid, 1, 10).json()
    assert body["effective_revision"] == 2 and body["effective_measured"] == 20
    assert _visible(client, rid)["measured"] == 20
    state = _state(client, rid)
    assert state["next_required_revision"] == 3
