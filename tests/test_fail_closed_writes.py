"""追溯写不进去时，所有状态变更写操作失败关闭且不留半生效状态。"""

from __future__ import annotations


def _grant(client, fields=("medical",), **over):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": list(fields),
    }
    payload.update(over)
    return client.post("/consents", json=payload)


def _access(client, fields=("medical",)):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": list(fields),
    })


STATE_EVENTS = {
    "consent_granted", "consent_revoked", "consent_scope_narrowed",
    "consent_suspended", "consent_resumed",
}


def _audit_types(client):
    return [
        e["event_type"] for e in client.get("/audit").json()
        if e["event_type"] in STATE_EVENTS
    ]


def test_failed_grant_leaves_no_consent_and_identical_access(client):
    before = _access(client).json()
    assert before["allowed"] is False

    client.post("/test/audit-failure?enabled=true")
    resp = _grant(client)
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    client.post("/test/audit-failure?enabled=false")

    # 失败后：同意并未半生效，访问结论与失败前完全一致
    after = _access(client).json()
    assert after["allowed"] is before["allowed"]
    assert after["denial_reason"] == before["denial_reason"] == "consent_not_found"
    # 失败的授予不落任何痕迹（连审计都没有）
    assert _audit_types(client) == []


def test_failed_revoke_keeps_consent_effective(client):
    consent = _grant(client).json()
    assert _access(client).json()["allowed"] is True

    client.post("/test/audit-failure?enabled=true")
    resp = client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    client.post("/test/audit-failure?enabled=false")

    # 撤销未半生效：同意仍可用
    body = _access(client).json()
    assert body["allowed"] is True
    assert body["consent_id"] == consent["consent_id"]
    # 审计流中只有授予，没有撤销
    assert _audit_types(client) == ["consent_granted"]


def test_failed_narrowing_keeps_original_scope(client):
    consent = _grant(client, ["identity", "medical"]).json()

    client.post("/test/audit-failure?enabled=true")
    resp = client.post("/consents/narrow", json={
        "consent_id": consent["consent_id"], "allowed_fields": ["medical"],
    })
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    client.post("/test/audit-failure?enabled=false")

    # 收窄未半生效：identity 仍在范围内，访问成功且数据含 name/id_card（掩码）
    body = _access(client, ["identity", "medical"]).json()
    assert body["allowed"] is True
    assert "name" in body["data"] and "diagnosis" in body["data"]
    assert _audit_types(client) == ["consent_granted"]


def test_failed_suspend_keeps_consent_accessible(client):
    consent = _grant(client).json()

    client.post("/test/audit-failure?enabled=true")
    resp = client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    client.post("/test/audit-failure?enabled=false")

    # 暂停未半生效：既不拒绝，也不落暂停标记
    body = _access(client).json()
    assert body["allowed"] is True
    assert _audit_types(client) == ["consent_granted"]


def test_failed_resume_keeps_consent_suspended(client):
    consent = _grant(client).json()
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert _access(client).json()["denial_reason"] == "consent_suspended"

    client.post("/test/audit-failure?enabled=true")
    resp = client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    client.post("/test/audit-failure?enabled=false")

    # 恢复未半生效：仍按暂停拒绝
    body = _access(client).json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "consent_suspended"
    assert _audit_types(client) == ["consent_granted", "consent_suspended"]


def test_recovered_writes_succeed_after_audit_recovery(client):
    """故障解除后，同一存储上的写操作恢复正常，无残留影响。"""
    client.post("/test/audit-failure?enabled=true")
    failed = _grant(client)
    assert failed.status_code == 403
    client.post("/test/audit-failure?enabled=false")

    consent = _grant(client).json()
    assert _access(client).json()["allowed"] is True
    client.post("/consents/narrow", json={
        "consent_id": consent["consent_id"], "allowed_fields": [],
    })
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    assert _access(client).json()["denial_reason"] == "consent_revoked"

    types = _audit_types(client)
    assert types == [
        "consent_granted",
        "consent_scope_narrowed",
        "consent_suspended",
        "consent_resumed",
        "consent_revoked",
    ]
