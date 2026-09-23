"""并发撤销与访问测试：撤销必须对后续访问立即生效，且决策一致可解释。"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient


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
    barrier = threading.Barrier(2)
    results: list[dict] = []
    results_lock = threading.Lock()

    def revoke():
        barrier.wait()
        client.post("/consents/revoke", json={"consent_id": consent_id})

    def hammer_access():
        barrier.wait()
        for _ in range(50):
            body = _access(client).json()
            with results_lock:
                results.append(body)

    t1 = threading.Thread(target=revoke)
    t2 = threading.Thread(target=hammer_access)
    t1.start(); t2.start()
    t1.join(); t2.join()

    allowed = [r for r in results if r["allowed"]]
    denied = [r for r in results if not r["allowed"]]

    # 所有被拒访问都必须给出同一可区分原因
    assert all(r["denial_reason"] == "consent_revoked" for r in denied)
    # 所有放行访问都必须带数据；所有被拒访问都绝无数据
    assert all(r["data"] is not None for r in allowed)
    assert all(r["data"] is None for r in denied)

    # 一旦出现拒绝，其后的所有访问都必须拒绝（撤销立即生效，不可反复）
    outcomes = [r["allowed"] for r in results]
    first_deny = next((i for i, ok in enumerate(outcomes) if not ok), None)
    if first_deny is not None:
        assert all(ok is False for ok in outcomes[first_deny:])

    # 审计中每个成功访问都有 access_granted，每个拒绝都有 access_denied
    events = client.get("/audit").json()
    granted = [e for e in events if e["event_type"] == "access_granted"]
    denied_events = [e for e in events if e["event_type"] == "access_denied"]
    assert len(granted) == len(allowed)
    assert len(denied_events) == len(denied)


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
