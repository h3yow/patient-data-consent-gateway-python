"""同意暂停 / 恢复：独立拒绝原因、不改变有效期与范围、撤销不可恢复。"""

from __future__ import annotations


def _grant(client, **over):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["medical"],
    }
    payload.update(over)
    resp = client.post("/consents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _access(client):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": ["medical"],
    })


def test_suspend_denies_with_distinct_reason_and_no_data(client):
    consent = _grant(client)
    assert _access(client).json()["allowed"] is True

    resp = client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 200, resp.text
    suspended = resp.json()
    assert suspended["suspended"] is True
    assert suspended["active"] is True  # 暂停不是撤销
    assert suspended["suspended_at"] is not None

    body = _access(client).json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "consent_suspended"
    assert body["data"] is None


def test_resume_restores_same_consent_with_same_scope_and_expiry(client):
    consent = _grant(client, expires_at="2099-01-01T00:00:00Z")
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert _access(client).json()["denial_reason"] == "consent_suspended"

    resp = client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 200, resp.text
    resumed = resp.json()
    assert resumed["suspended"] is False
    assert resumed["resumed_at"] is not None
    assert resumed["consent_id"] == consent["consent_id"]  # 同一条同意
    assert resumed["allowed_fields"] == ["medical"]
    assert resumed["expires_at"] == "2099-01-01T00:00:00Z"  # 有效期不变

    body = _access(client).json()
    assert body["allowed"] is True
    assert body["consent_id"] == consent["consent_id"]
    assert set(body["data"].keys()) == {"diagnosis", "medications"}


def test_suspend_does_not_change_expiry_window(client):
    # 在有效期内暂停、恢复，恢复后仍按原有效期生效
    consent = _grant(client, expires_at="2099-01-01T00:00:00Z")
    client.post("/consents/suspend", json={
        "consent_id": consent["consent_id"], "now": "2026-10-01T00:00:00Z",
    })
    resumed = client.post("/consents/resume", json={
        "consent_id": consent["consent_id"], "now": "2026-10-02T00:00:00Z",
    }).json()
    assert resumed["expires_at"] == "2099-01-01T00:00:00Z"
    body = _access(client).json()
    assert body["allowed"] is True


def test_expired_consent_resumed_still_expired(client):
    consent = _grant(client, expires_at="2020-01-01T00:00:00Z", now="2019-12-01T00:00:00Z")
    # 过期后暂停再恢复（历史回放：暂停于过期前，恢复于过期后）
    client.post("/consents/suspend", json={
        "consent_id": consent["consent_id"], "now": "2019-12-15T00:00:00Z",
    })
    client.post("/consents/resume", json={
        "consent_id": consent["consent_id"], "now": "2026-10-01T00:00:00Z",
    })
    body = _access(client).json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "consent_expired"


def test_revoked_consent_cannot_be_resumed(client):
    consent = _grant(client)
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})

    resp = client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "consent_revoked"

    body = _access(client).json()
    assert body["denial_reason"] == "consent_revoked"


def test_suspend_revoked_consent_is_rejected(client):
    consent = _grant(client)
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    resp = client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "consent_revoked"


def test_suspend_and_resume_are_idempotent(client):
    consent = _grant(client)
    for _ in range(2):
        r = client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
        assert r.status_code == 200
    # 只落一条暂停审计
    events = client.get("/audit").json()
    assert [e["event_type"] for e in events].count("consent_suspended") == 1

    for _ in range(2):
        r = client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
        assert r.status_code == 200
    events = client.get("/audit").json()
    assert [e["event_type"] for e in events].count("consent_resumed") == 1


def test_suspend_resume_are_traceable(client):
    consent = _grant(client)
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    client.post("/consents/resume", json={"consent_id": consent["consent_id"]})

    events = client.get("/audit").json()
    types = [e["event_type"] for e in events]
    assert types == ["consent_granted", "consent_suspended", "consent_resumed"]
    assert all(e["consent_id"] == consent["consent_id"] for e in events)
    stamps = [e["timestamp"] for e in events]
    assert stamps == sorted(stamps)


def test_suspend_unknown_consent_is_not_found(client):
    resp = client.post("/consents/suspend", json={"consent_id": "nope"})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_not_found"


def test_suspended_reason_is_distinct_from_all_other_denials(client):
    """暂停原因必须与没授权/已撤销/已过期/用途错/接收方错/超范围分开。"""
    consent = _grant(client)
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    reason = _access(client).json()["denial_reason"]
    others = {
        "consent_not_found", "consent_revoked", "consent_expired",
        "purpose_mismatch", "recipient_mismatch", "scope_exceeded",
        "audit_write_failed",
    }
    assert reason == "consent_suspended"
    assert reason not in others
