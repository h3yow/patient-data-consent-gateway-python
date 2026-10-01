"""变更（收窄/暂停/恢复）与访问并发的自洽性：
每个访问要么看到变更前的状态，要么看到变更后的状态，
绝不返回已被移掉或已暂停的字段。"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient


def _grant(client, fields):
    resp = client.post("/consents", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": list(fields),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["consent_id"]


def _access(client, fields):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": list(fields),
    }).json()


def _run_concurrent(mutate, hammer, workers=2):
    barrier = threading.Barrier(workers + 1)
    threads = [threading.Thread(target=hammer, args=(barrier,)) for _ in range(workers)]
    for t in threads:
        t.start()
    barrier.wait()
    mutate()
    for t in threads:
        t.join()


def test_concurrent_narrow_and_access_is_self_consistent(client):
    consent_id = _grant(client, ["identity", "medical"])
    results: list[dict] = []
    lock = threading.Lock()

    def narrow():
        client.post("/consents/narrow", json={
            "consent_id": consent_id, "allowed_fields": ["medical"],
        })

    def hammer(barrier):
        local = TestClient(client.app)
        barrier.wait()
        for _ in range(50):
            body = _access(local, ["identity"])
            with lock:
                results.append(body)

    _run_concurrent(narrow, hammer)

    for body in results:
        if body["allowed"]:
            # 看到变更前状态：返回的是脱敏后的 identity 数据
            assert body["data"] is not None
            assert set(body["returned_fields"]) == {"name", "id_card"}
        else:
            # 看到变更后状态：超出范围且绝无数据
            assert body["denial_reason"] == "scope_exceeded"
            assert body["data"] is None
            assert body["returned_fields"] == []

    # 收窄单调生效：一旦出现超范围拒绝，之后不得再放行 identity
    outcomes = [b["allowed"] for b in results]
    first_deny = next((i for i, ok in enumerate(outcomes) if not ok), None)
    if first_deny is not None:
        assert all(ok is False for ok in outcomes[first_deny:])

    # 收窄本身留下一条带前后范围的审计
    narrowed = [
        e for e in client.get("/audit").json()
        if e["event_type"] == "consent_narrowed"
    ]
    assert len(narrowed) == 1
    assert narrowed[0]["previous_allowed_fields"] == ["identity", "medical"]
    assert narrowed[0]["allowed_fields"] == ["medical"]


def test_concurrent_suspend_and_access_is_self_consistent(client):
    consent_id = _grant(client, ["medical"])
    results: list[dict] = []
    lock = threading.Lock()

    def suspend():
        client.post("/consents/suspend", json={"consent_id": consent_id})

    def hammer(barrier):
        local = TestClient(client.app)
        barrier.wait()
        for _ in range(50):
            body = _access(local, ["medical"])
            with lock:
                results.append(body)

    _run_concurrent(suspend, hammer)

    for body in results:
        if body["allowed"]:
            assert body["data"] is not None
        else:
            # 暂停期间只能以 consent_suspended 拒绝，且绝无数据
            assert body["denial_reason"] == "consent_suspended"
            assert body["data"] is None

    # 暂停单调生效（期间无人恢复）
    outcomes = [b["allowed"] for b in results]
    first_deny = next((i for i, ok in enumerate(outcomes) if not ok), None)
    if first_deny is not None:
        assert all(ok is False for ok in outcomes[first_deny:])


def test_concurrent_suspend_resume_churn_and_access_is_self_consistent(client):
    """暂停/恢复交替进行时的每个访问结论都必须自洽。"""
    consent_id = _grant(client, ["medical"])
    results: list[dict] = []
    lock = threading.Lock()
    stop = threading.Event()

    def churn():
        # 暂停 -> 恢复 -> 暂停 -> 恢复，交替若干轮
        for _ in range(10):
            client.post("/consents/suspend", json={"consent_id": consent_id})
            client.post("/consents/resume", json={"consent_id": consent_id})
        stop.set()

    def hammer(barrier):
        local = TestClient(client.app)
        barrier.wait()
        while not stop.is_set():
            body = _access(local, ["medical"])
            with lock:
                results.append(body)

    barrier = threading.Barrier(2)
    t = threading.Thread(target=hammer, args=(barrier,))
    t.start()
    barrier.wait()
    churn()
    t.join()

    assert len(results) > 0
    for body in results:
        if body["allowed"]:
            # 看到未暂停快照：必须带数据
            assert body["data"] is not None
            assert body["denial_reason"] is None
        else:
            # 看到已暂停快照：原因可区分且绝无数据
            assert body["denial_reason"] == "consent_suspended"
            assert body["data"] is None

    # 最终处于已恢复状态，访问放行
    assert _access(client, ["medical"])["allowed"] is True
