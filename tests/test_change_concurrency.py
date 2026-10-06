"""变更（收窄/暂停/恢复）与访问并发的自洽性：
每个访问要么看到完整的变更前状态，要么看到完整的变更后状态，
绝不返回已被移掉或已暂停的字段，也绝不出现半生效的中间态。

单调性断言基于服务端审计流：访问的「决策 + 写审计」在同一临界区内完成，
审计事件顺序即访问的线性化顺序，因此多轮运行都稳定可重复；
客户端侧的结果收集顺序只用于逐条自洽性校验，不参与顺序断言。
"""

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


def _access_events(client):
    """审计流中的访问事件（线性化顺序）。"""
    return [
        e for e in client.get("/audit").json()
        if e["event_type"] in ("access_granted", "access_denied")
    ]


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
        resp = client.post("/consents/narrow", json={
            "consent_id": consent_id, "allowed_fields": ["medical"],
        })
        assert resp.status_code == 200, resp.text

    def hammer(barrier):
        local = TestClient(client.app)
        barrier.wait()
        for _ in range(50):
            body = _access(local, ["identity"])
            with lock:
                results.append(body)

    _run_concurrent(narrow, hammer)

    # 逐条自洽：每个响应都是完整的变更前或变更后结论
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

    # 单调性按审计流（线性化顺序）验证：收窄生效后不得再放行 identity
    events = _access_events(client)
    assert len(events) == len(results)  # 每个访问恰好留下一条审计，无丢失无重复
    seen_denied = False
    for e in events:
        if e["event_type"] == "access_denied":
            assert e["denial_reason"] == "scope_exceeded"
            seen_denied = True
        else:
            assert not seen_denied, "收窄生效后仍放行了已移除的 identity 字段"
            assert set(e["returned_fields"]) == {"name", "id_card"}

    # 变更已确定提交：之后的访问必须看到变更后状态
    after = _access(client, ["identity"])
    assert after["allowed"] is False
    assert after["denial_reason"] == "scope_exceeded"
    assert after["data"] is None

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
        resp = client.post("/consents/suspend", json={"consent_id": consent_id})
        assert resp.status_code == 200, resp.text

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

    # 单调性按审计流（线性化顺序）验证：暂停生效后不得再放行
    events = _access_events(client)
    assert len(events) == len(results)
    seen_denied = False
    for e in events:
        if e["event_type"] == "access_denied":
            assert e["denial_reason"] == "consent_suspended"
            seen_denied = True
        else:
            assert not seen_denied, "暂停生效后仍放行了访问"

    # 暂停已确定提交：之后的访问一律以 consent_suspended 拒绝
    after = _access(client, ["medical"])
    assert after["allowed"] is False
    assert after["denial_reason"] == "consent_suspended"


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

    # 每个访问都恰好留下一条与结论一致的审计事件
    events = _access_events(client)
    assert len(events) == len(results)
    for e in events:
        if e["event_type"] == "access_granted":
            assert e["returned_fields"]
        else:
            assert e["denial_reason"] == "consent_suspended"

    # 最终处于已恢复状态，访问放行
    assert _access(client, ["medical"])["allowed"] is True
