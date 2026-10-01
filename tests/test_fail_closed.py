"""失败关闭一致性：审计写不进去时，所有改变同意状态的写操作整体失败，
且不留半生效状态——失败前后同一访问请求的结论必须一模一样。"""

from __future__ import annotations

import pytest


def _grant(client, **over):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["identity", "medical"],
    }
    payload.update(over)
    resp = client.post("/consents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _access(client, fields=("medical",)):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": list(fields),
    })


def _inject(client, enabled: bool):
    client.post(f"/test/audit-failure?enabled={'true' if enabled else 'false'}")


def test_audit_failure_blocks_grant_without_half_applied_state(client):
    before = _access(client).json()
    assert before["denial_reason"] == "consent_not_found"

    _inject(client, True)
    resp = client.post("/consents", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["medical"],
    })
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    _inject(client, False)

    # 授予不能半生效：同一访问请求结论与失败前完全一致
    after = _access(client).json()
    assert after["allowed"] == before["allowed"]
    assert after["denial_reason"] == before["denial_reason"]
    assert after["data"] is None
    # 审计流中也没有留下授予记录（失败前的访问审计不受影响）
    types = [e["event_type"] for e in client.get("/audit").json()]
    assert "consent_granted" not in types


def test_audit_failure_blocks_revoke_without_half_applied_state(client):
    consent = _grant(client)
    before = _access(client).json()
    assert before["allowed"] is True

    _inject(client, True)
    resp = client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    _inject(client, False)

    # 撤销不能半生效：同意仍然有效，访问结论与失败前一致
    after = _access(client).json()
    assert after["allowed"] is True
    assert after["data"] == before["data"]
    # 审计中没有留下任何撤销记录
    types = [e["event_type"] for e in client.get("/audit").json()]
    assert "consent_revoked" not in types


def test_audit_failure_blocks_narrow_without_half_applied_state(client):
    consent = _grant(client)
    before = _access(client, ["identity"]).json()
    assert before["allowed"] is True

    _inject(client, True)
    resp = client.post("/consents/narrow", json={
        "consent_id": consent["consent_id"],
        "allowed_fields": ["medical"],
    })
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    _inject(client, False)

    # 收窄不能半生效：identity 仍在范围内
    after = _access(client, ["identity"]).json()
    assert after["allowed"] is True
    assert after["data"] == before["data"]


def test_audit_failure_blocks_suspend_without_half_applied_state(client):
    consent = _grant(client)
    before = _access(client).json()
    assert before["allowed"] is True

    _inject(client, True)
    resp = client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    _inject(client, False)

    # 暂停不能半生效：访问仍放行
    after = _access(client).json()
    assert after["allowed"] is True
    assert after["data"] == before["data"]


def test_audit_failure_blocks_resume_without_half_applied_state(client):
    consent = _grant(client)
    client.post("/consents/suspend", json={"consent_id": consent["consent_id"]})
    before = _access(client).json()
    assert before["denial_reason"] == "consent_suspended"

    _inject(client, True)
    resp = client.post("/consents/resume", json={"consent_id": consent["consent_id"]})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    _inject(client, False)

    # 恢复不能半生效：仍处于暂停
    after = _access(client).json()
    assert after["allowed"] is False
    assert after["denial_reason"] == "consent_suspended"
    assert after["data"] is None


@pytest.mark.parametrize("endpoint,payload_key", [
    ("revoke", None),
    ("narrow", ["medical"]),
    ("suspend", None),
])
def test_failed_write_leaves_no_audit_trace(client, endpoint, payload_key):
    """失败的写操作既不改变状态，也不在审计流中留下任何记录。"""
    consent = _grant(client)
    payload = {"consent_id": consent["consent_id"]}
    if payload_key is not None:
        payload["allowed_fields"] = payload_key

    _inject(client, True)
    resp = client.post(f"/consents/{endpoint}", json=payload)
    assert resp.status_code == 403
    _inject(client, False)

    types = [e["event_type"] for e in client.get("/audit").json()]
    assert types == ["consent_granted"]
