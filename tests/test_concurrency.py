"""并发撤销与访问测试：撤销必须对后续访问立即生效，且决策一致可解释。

单调性只在**单个工作线程的请求序列**上断言（状态变更单调 + 服务端串行化
保证）；跨线程按完成时刻聚合的顺序不作假设，只要求每个响应自身自洽。
"""

from __future__ import annotations

import os
import threading
import time

from fastapi.testclient import TestClient

# 可用环境变量 ITERATIONS/WORKERS 调整并发压力规模。
ITERATIONS = int(os.environ.get("ITERATIONS", "200"))
WORKERS = int(os.environ.get("WORKERS", "4"))


def _grant(client):
    resp = client.post("/consents", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["medical"],
    })
    return resp.json()["consent_id"]


def _access(client):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": ["medical"],
    })


def test_concurrent_revoke_and_accesses_never_observe_inconsistent_state(client):
    consent_id = _grant(client)
    start = threading.Barrier(WORKERS + 1)
    per_worker: list[list[dict]] = [[] for _ in range(WORKERS)]

    def revoke():
        # 每个线程使用各自的 TestClient（TestClient 非线程安全，不可共享）。
        local = TestClient(client.app)
        start.wait()
        # 稍等片刻再撤销，使撤销确定地穿插在大量访问的中间时刻
        time.sleep(0.05)
        local.post("/consents/revoke", json={"consent_id": consent_id})

    def hammer_access(idx: int):
        local = TestClient(client.app)
        start.wait()
        for _ in range(ITERATIONS):
            body = _access(local).json()
            per_worker[idx].append(body)

    threads = [threading.Thread(target=revoke)]
    threads += [
        threading.Thread(target=hammer_access, args=(i,)) for i in range(WORKERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "并发线程未正常结束（疑似死锁）"

    results = [body for seq in per_worker for body in seq]
    assert len(results) == ITERATIONS * WORKERS
    # 撤销穿插在访问中间：前后两种快照都应被观察到
    assert any(r["allowed"] for r in results)
    assert any(not r["allowed"] for r in results)

    allowed = [r for r in results if r["allowed"]]
    denied = [r for r in results if not r["allowed"]]

    # 所有被拒访问都必须给出同一可区分原因
    assert all(r["denial_reason"] == "consent_revoked" for r in denied)
    # 所有放行访问都必须带数据；所有被拒访问都绝无数据
    assert all(r["data"] is not None for r in allowed)
    assert all(r["data"] is None for r in denied)

    # 每个工作线程内：一旦出现拒绝，其后的所有访问都必须拒绝
    # （撤销立即生效、不可反复；该线程的请求与写操作在服务端串行）
    for seq in per_worker:
        first_deny = next(
            (i for i, r in enumerate(seq) if not r["allowed"]), None
        )
        if first_deny is not None:
            assert all(r["allowed"] is False for r in seq[first_deny:])

    # 审计中每个成功访问都有 access_granted，每个拒绝都有 access_denied
    events = client.get("/audit").json()
    granted = [e for e in events if e["event_type"] == "access_granted"]
    denied_events = [e for e in events if e["event_type"] == "access_denied"]
    assert len(granted) == len(allowed)
    assert len(denied_events) == len(denied)
    assert all(
        e["denial_reason"] == "consent_revoked" for e in denied_events
    )


def test_strictly_sequential_revoke_then_access(client):
    consent_id = _grant(client)
    assert _access(client).json()["allowed"] is True
    client.post("/consents/revoke", json={"consent_id": consent_id})
    for _ in range(5):
        body = _access(client).json()
        assert body["allowed"] is False
        assert body["denial_reason"] == "consent_revoked"
        assert body["data"] is None


def test_parallel_revoke_only_wins_once_decision_is_consistent(client):
    """两个接收方并发访问 + 撤销：每个响应都必须是自洽的二值结论。"""
    cid1 = _grant(client)
    outcomes = []
    lock = threading.Lock()

    def worker(revoke_first: bool):
        local = TestClient(client.app)
        if revoke_first:
            local.post("/consents/revoke", json={"consent_id": cid1})
        body = _access(local).json()
        with lock:
            outcomes.append(body)

    threads = [threading.Thread(target=worker, args=(i == 0,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for body in outcomes:
        if body["allowed"]:
            assert body["data"] is not None
        else:
            assert body["data"] is None
            assert body["denial_reason"] in {"consent_revoked"}
