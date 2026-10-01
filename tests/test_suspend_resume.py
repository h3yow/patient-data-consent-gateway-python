"""暂停/恢复：状态流转、可区分的拒绝原因、不改变有效期、不等于撤销。"""

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


def _access(client, **over):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": ["medical"],
    }
    payload.update(over)
    return client.post("/access/data", json=payload)


def test_suspend_denies_with_distinguishable_reason_and_no_data(client):
    consent = _grant(client)
    assert _access(client).json()["allowed"] is True

    resp = client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 200
    assert resp.json()["suspended_at"] is not None
    # 暂停不是撤销
    assert resp.json()["active"] is True
    assert resp.json()["revoked_at"] is None

    body = _access(client).json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "consent_suspended"
    assert body["data"] is None


def test_resume_restores_original_scope_and_validity(client):
    consent = _grant(client, expires_at="2099-01-01T00:00:00Z")
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    resp = client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["suspended_at"] is None
    # 有效期与范围保持原样
    assert body["expires_at"] == consent["expires_at"]
    assert body["allowed_fields"] == consent["allowed_fields"]

    after = _access(client).json()
    assert after["allowed"] is True
    assert after["data"] is not None


def test_suspend_does_not_extend_validity(client):
    """暂停期间时间照常流逝：暂停中过期的同意，恢复后仍按过期拒绝。"""
    consent = _grant(
        client,
        now="2026-01-01T00:00:00Z",
        expires_at="2026-06-01T00:00:00Z",
    )
    client.post(
        "/consents/suspend",
        json={"consent_id": consent["consent_id"], "now": "2026-02-01T00:00:00Z"},
    )
    # 暂停期间（未过期）拒绝原因是已暂停
    body = _access(client, now="2026-03-01T00:00:00Z").json()
    assert body["denial_reason"] == "consent_suspended"
    # 过期之后恢复：仍按过期处理
    client.post(
        "/consents/resume",
        json={"consent_id": consent["consent_id"], "now": "2026-07-01T00:00:00Z"},
    )
    body = _access(client, now="2026-07-02T00:00:00Z").json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "consent_expired"


def test_suspended_reason_is_distinct_from_all_existing_reasons(client):
    """同一存储内制造全部拒绝原因，确认 consent_suspended 与它们互不混淆。"""
    reasons = set()

    reasons.add(_access(client, patient_id="P-002").json()["denial_reason"])
    consent = _grant(client)
    reasons.add(_access(client, recipient_id="R-INSURER").json()["denial_reason"])
    reasons.add(_access(client, purpose="marketing").json()["denial_reason"])
    reasons.add(_access(client, requested_fields=["identity"]).json()["denial_reason"])

    expired = _grant(
        client,
        patient_id="P-002",
        now="2026-01-01T00:00:00Z",
        expires_at="2026-02-01T00:00:00Z",
    )
    reasons.add(
        _access(client, patient_id="P-002", now="2026-03-01T00:00:00Z").json()[
            "denial_reason"
        ]
    )

    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    reasons.add(_access(client).json()["denial_reason"])

    revoked = _grant(client, patient_id="P-002", recipient_id="R-RESEARCH")
    client.post("/consents/revoke", json={"consent_id": revoked["consent_id"]})
    reasons.add(
        _access(client, patient_id="P-002", recipient_id="R-RESEARCH").json()[
            "denial_reason"
        ]
    )

    assert reasons == {
        "consent_not_found",
        "recipient_mismatch",
        "purpose_mismatch",
        "scope_exceeded",
        "consent_expired",
        "consent_suspended",
        "consent_revoked",
    }


def test_revoked_consent_cannot_be_suspended_or_resumed(client):
    consent = _grant(client)
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})

    resp = client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_revoked"

    resp = client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_revoked"

    # 访问结论不受影响，仍是已撤销
    assert _access(client).json()["denial_reason"] == "consent_revoked"


def test_invalid_suspend_resume_transitions_are_rejected(client):
    consent = _grant(client)
    cid = consent["consent_id"]

    # 未暂停就恢复
    resp = client.post("/consents/resume", json={"consent_id": cid})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_not_suspended"

    # 重复暂停
    client.post("/consents/suspend", json={"consent_id": cid})
    resp = client.post("/consents/suspend", json={"consent_id": cid})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_suspended"

    # 不存在的同意
    for endpoint in ("suspend", "resume"):
        resp = client.post(f"/consents/{endpoint}", json={"consent_id": "nope"})
        assert resp.status_code == 403
        assert resp.json()["denial_reason"] == "consent_not_found"

    # 失败的状态流转没有改变真实状态：仍处暂停，恢复后可用
    assert _access(client).json()["denial_reason"] == "consent_suspended"
    client.post("/consents/resume", json={"consent_id": cid})
    assert _access(client).json()["allowed"] is True


def test_suspend_and_resume_are_recorded_in_audit(client):
    consent = _grant(client)
    cid = consent["consent_id"]
    client.post("/consents/suspend", json={"consent_id": cid})
    _access(client)
    client.post("/consents/resume", json={"consent_id": cid})
    _access(client)

    events = client.get("/audit").json()
    types = [e["event_type"] for e in events]
    assert types == [
        "consent_granted",
        "consent_suspended",
        "access_denied",
        "consent_resumed",
        "access_granted",
    ]
    suspended = events[1]
    assert suspended["consent_id"] == cid
    denied = events[2]
    assert denied["denial_reason"] == "consent_suspended"
    # 时间戳单调，可重放
    stamps = [e["timestamp"] for e in events]
    assert stamps == sorted(stamps)


def test_narrow_still_applies_while_suspended(client):
    """暂停期间可以收窄；恢复后按收窄后的范围生效。"""
    consent = _grant(client, allowed_fields=["identity", "medical"])
    cid = consent["consent_id"]
    client.post("/consents/suspend", json={"consent_id": cid})
    resp = client.post("/consents/narrow", json={
        "consent_id": cid, "allowed_fields": ["medical"],
    })
    assert resp.status_code == 200
    client.post("/consents/resume", json={"consent_id": cid})
    denied = _access(client, requested_fields=["identity"]).json()
    assert denied["denial_reason"] == "scope_exceeded"
    assert _access(client).json()["allowed"] is True
