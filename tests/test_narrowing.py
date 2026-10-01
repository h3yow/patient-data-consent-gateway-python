"""同意范围收窄：立即生效、只减不增、可追溯。"""

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


def _narrow(client, consent_id, fields):
    return client.post("/consents/narrow", json={
        "consent_id": consent_id,
        "allowed_fields": fields,
    })


def _access(client, fields):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": fields,
    })


def test_narrowing_takes_immediate_effect_and_kept_fields_still_work(client):
    consent = _grant(client)
    # 收窄前两类都能访问
    assert _access(client, ["identity", "medical"]).json()["allowed"] is True

    resp = _narrow(client, consent["consent_id"], ["medical"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allowed_fields"] == ["medical"]
    assert body["active"] is True

    # 保留下来的类别照常访问
    ok = _access(client, ["medical"]).json()
    assert ok["allowed"] is True
    assert set(ok["data"].keys()) == {"diagnosis", "medications"}

    # 被移除类别再请求：超范围拒绝，且不返回任何受保护数据
    denied = _access(client, ["identity"]).json()
    assert denied["allowed"] is False
    assert denied["denial_reason"] == "scope_exceeded"
    assert denied["data"] is None

    # 混合请求（含被移除类别）整体拒绝，medical 也不能搭车返回
    mixed = _access(client, ["medical", "identity"]).json()
    assert mixed["allowed"] is False
    assert mixed["denial_reason"] == "scope_exceeded"
    assert mixed["data"] is None


def test_narrowing_is_subset_only_widening_is_rejected(client):
    consent = _grant(client, ["medical"])
    # 试图借收窄扩大范围：拒绝（HTTP 400，可区分错误码），范围不变
    resp = _narrow(client, consent["consent_id"], ["medical", "identity"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "scope_widening_rejected"

    # 范围维持原样：identity 仍超范围、medical 仍可用
    assert _access(client, ["identity"]).json()["denial_reason"] == "scope_exceeded"
    assert _access(client, ["medical"]).json()["allowed"] is True


def test_narrowing_to_same_scope_is_rejected(client):
    consent = _grant(client, ["identity", "medical"])
    resp = _narrow(client, consent["consent_id"], ["medical", "identity"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "scope_unchanged"
    # 范围与顺序不变
    after = client.post("/access/decision", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["identity", "medical"],
    }).json()
    assert after["allowed"] is True


def test_need_more_fields_after_narrowing_requires_new_consent(client):
    consent = _grant(client, ["identity", "medical"])
    _narrow(client, consent["consent_id"], ["medical"])

    # 重新授予一条更大范围的新同意（三元组相同），最近授予优先
    new_consent = _grant(client, ["identity", "contact", "medical"])
    body = _access(client, ["identity", "contact", "medical"]).json()
    assert body["allowed"] is True
    assert body["consent_id"] == new_consent["consent_id"]


def test_narrowing_is_traceable_with_before_and_after_scope(client):
    consent = _grant(client, ["identity", "contact", "medical"])
    _narrow(client, consent["consent_id"], ["contact", "medical"])

    events = client.get("/audit").json()
    narrowed = [e for e in events if e["event_type"] == "consent_scope_narrowed"]
    assert len(narrowed) == 1
    evt = narrowed[0]
    assert evt["previous_fields"] == ["identity", "contact", "medical"]
    assert evt["allowed_fields"] == ["contact", "medical"]
    assert evt["consent_id"] == consent["consent_id"]


def test_narrowing_unknown_consent_is_not_found(client):
    resp = _narrow(client, "nope", ["medical"])
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_not_found"


def test_narrowing_revoked_consent_is_rejected(client):
    consent = _grant(client, ["medical"])
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    resp = _narrow(client, consent["consent_id"], [])
    assert resp.status_code == 400
    assert resp.json()["error"] == "consent_revoked"
