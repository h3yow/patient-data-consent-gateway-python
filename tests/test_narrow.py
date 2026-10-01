"""范围收窄：立即生效、只减不增、可追溯。"""

from __future__ import annotations


def _grant(client, fields=("identity", "medical"), **over):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": list(fields),
    }
    payload.update(over)
    resp = client.post("/consents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _access(client, fields):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": list(fields),
    })


def _narrow(client, consent_id, fields):
    return client.post("/consents/narrow", json={
        "consent_id": consent_id,
        "allowed_fields": list(fields),
    })


def test_narrow_takes_immediate_effect_and_keeps_remaining_scope(client):
    consent = _grant(client, ["identity", "medical"])
    assert _access(client, ["identity", "medical"]).json()["allowed"] is True

    resp = _narrow(client, consent["consent_id"], ["medical"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed_fields"] == ["medical"]
    # 患者、接收方、用途、有效期都不变
    assert body["patient_id"] == consent["patient_id"]
    assert body["recipient_id"] == consent["recipient_id"]
    assert body["purpose"] == consent["purpose"]
    assert body["granted_at"] == consent["granted_at"]

    # 被移掉的类别立即按超出范围拒绝，且不返回任何数据
    denied = _access(client, ["identity"]).json()
    assert denied["allowed"] is False
    assert denied["denial_reason"] == "scope_exceeded"
    assert denied["data"] is None
    # 混合请求（含被移掉类别）整体拒绝
    mixed = _access(client, ["identity", "medical"]).json()
    assert mixed["allowed"] is False
    assert mixed["denial_reason"] == "scope_exceeded"
    assert mixed["data"] is None
    # 保留的类别仍可正常访问
    kept = _access(client, ["medical"]).json()
    assert kept["allowed"] is True
    assert kept["data"] is not None


def test_narrow_cannot_expand_scope(client):
    consent = _grant(client, ["medical"])
    # 借收窄扩大范围：拒绝
    resp = _narrow(client, consent["consent_id"], ["medical", "identity"])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "scope_not_narrowed"
    # 完全不重叠的新范围同样拒绝
    resp = _narrow(client, consent["consent_id"], ["contact"])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "scope_not_narrowed"
    # 范围未变（非真子集）：拒绝
    resp = _narrow(client, consent["consent_id"], ["medical"])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "scope_not_narrowed"
    # 空范围：拒绝
    resp = _narrow(client, consent["consent_id"], [])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "scope_not_narrowed"
    # 原范围未被任何失败尝试改变
    assert _access(client, ["medical"]).json()["allowed"] is True


def test_more_fields_require_new_grant(client):
    """需要更多字段时的正规路径：重新授予一条新同意，决策取最近一条。"""
    _grant(client, ["medical"])
    wider = _grant(client, ["medical", "profile"])
    assert wider["allowed_fields"] == ["medical", "profile"]
    assert _access(client, ["profile"]).json()["allowed"] is True


def test_narrow_is_recorded_with_before_and_after_scope(client):
    consent = _grant(client, ["identity", "contact", "medical"])
    _narrow(client, consent["consent_id"], ["medical"])

    events = client.get("/audit").json()
    narrowed = [e for e in events if e["event_type"] == "consent_narrowed"]
    assert len(narrowed) == 1
    event = narrowed[0]
    assert event["consent_id"] == consent["consent_id"]
    assert event["previous_allowed_fields"] == ["identity", "contact", "medical"]
    assert event["allowed_fields"] == ["medical"]


def test_narrow_can_be_applied_repeatedly_but_only_shrinking(client):
    consent = _grant(client, ["identity", "contact", "medical"])
    assert _narrow(client, consent["consent_id"], ["contact", "medical"]).status_code == 200
    assert _narrow(client, consent["consent_id"], ["medical"]).status_code == 200
    # 已经收窄掉的类别不能再“收窄回来”
    resp = _narrow(client, consent["consent_id"], ["medical", "identity"])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "scope_not_narrowed"


def test_narrow_unknown_or_revoked_consent_is_rejected(client):
    resp = _narrow(client, "nope", ["medical"])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_not_found"

    consent = _grant(client, ["identity", "medical"])
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    resp = _narrow(client, consent["consent_id"], ["medical"])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_revoked"
