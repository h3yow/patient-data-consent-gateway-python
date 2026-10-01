"""收窄/暂停与访问并发：每个访问要么看到变更前、要么看到变更后，结果必须自洽。"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient


def _access(client, fields):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": fields,
    })


def _run_change_against_accesses(client, change_callable):
    """启动一个变更线程与一个高频访问线程，返回全部访问响应（按完成顺序）。"""
    barrier = threading.Barrier(2)
    results: list[dict] = []
    results_lock = threading.Lock()

    def change():
        barrier.wait()
        change_callable()

    def hammer():
        local = TestClient(client.app)
        barrier.wait()
        for _ in range(100):
            body = _access(local, ["identity", "medical"]).json()
            with results_lock:
                results.append(body)

    t1 = threading.Thread(target=change)
    t2 = threading.Thread(target=hammer)
    t1.start(); t2.start()
    t1.join(); t2.join()
    return results


def test_concurrent_narrawing_never_returns_removed_categories(client):
    consent = client.post("/consents", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["identity", "medical"],
    }).json()

    def narrow():
        client.post("/consents/narrow", json={
            "consent_id": consent["consent_id"],
            "allowed_fields": ["medical"],
        })

    results = _run_change_against_accesses(client, narrow)
    assert results, "至少应观测到访问"

    for body in results:
        if body["allowed"]:
            # 变更前：两类都返回（identity 已掩码，medical 原值）
            assert set(body["data"]) == {"name", "id_card", "diagnosis", "medications"}
            assert set(body["masked_fields"]) == {"name", "id_card"}
        else:
            # 变更后：请求含被移除类别 -> 整体超范围，绝无任何数据
            assert body["denial_reason"] == "scope_exceeded"
            assert body["data"] is None

    # 收窄单向生效：一旦出现拒绝，其后所有访问都必须拒绝（不允许反复）
    outcomes = [b["allowed"] for b in results]
    first_deny = next((i for i, ok in enumerate(outcomes) if not ok), None)
    if first_deny is not None:
        assert all(ok is False for ok in outcomes[first_deny:])

    # 每个访问都恰好对应一条审计（granted/denied 数量对齐）
    events = client.get("/audit").json()
    granted = [e for e in events if e["event_type"] == "access_granted"]
    denied = [e for e in events if e["event_type"] == "access_denied"]
    assert len(granted) == sum(outcomes)
    assert len(denied) == len(outcomes) - sum(outcomes)
    # 收窄审计中能看出前后范围
    narrowed = [e for e in events if e["event_type"] == "consent_scope_narrowed"][0]
    assert narrowed["previous_fields"] == ["identity", "medical"]
    assert narrowed["allowed_fields"] == ["medical"]


def test_concurrent_suspend_never_serves_data_after_suspend(client):
    consent = client.post("/consents", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["identity", "medical"],
    }).json()

    def suspend():
        client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})

    results = _run_change_against_accesses(client, suspend)
    assert results

    for body in results:
        if body["allowed"]:
            assert body["data"] is not None
            assert set(body["data"]) == {"name", "id_card", "diagnosis", "medications"}
        else:
            # 暂停后访问一律以独立原因拒绝，且无数据
            assert body["denial_reason"] == "consent_suspended"
            assert body["data"] is None

    # 不允许出现“已暂停但仍拿到数据”：首个拒绝之后必须全部拒绝
    outcomes = [b["allowed"] for b in results]
    first_deny = next((i for i, ok in enumerate(outcomes) if not ok), None)
    if first_deny is not None:
        assert all(ok is False for ok in outcomes[first_deny:])


def test_concurrent_resume_only_needs_two_self_consistent_states(client):
    consent = client.post("/consents", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["medical"],
    }).json()
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})

    barrier = threading.Barrier(2)
    results: list[dict] = []
    lock = threading.Lock()

    def resume():
        barrier.wait()
        client.post("/consents/resume", json={"consent_id": consent["consent_id"]})

    def hammer():
        local = TestClient(client.app)
        barrier.wait()
        for _ in range(100):
            body = _access(local, ["medical"]).json()
            with lock:
                results.append(body)

    t1 = threading.Thread(target=resume)
    t2 = threading.Thread(target=hammer)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 恢复前：consent_suspended 且无数据；恢复后：allowed 且有数据。两种状态自洽
    for body in results:
        if body["allowed"]:
            assert body["data"] is not None
            assert body["denial_reason"] is None
        else:
            assert body["denial_reason"] == "consent_suspended"
            assert body["data"] is None

    # 恢复单向：首个放行之后必须全部放行
    outcomes = [b["allowed"] for b in results]
    first_allow = next((i for i, ok in enumerate(outcomes) if ok), None)
    if first_allow is not None:
        assert all(ok is True for ok in outcomes[first_allow:])
