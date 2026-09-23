"""审计可追溯、去标识化记录与失败关闭测试。"""

from __future__ import annotations

RAW_SECRETS = ["张三丰", "110101199001017890", "13812345678", "sanfeng@example.com"]


def _grant(client, fields=("medical",), **over):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": list(fields),
    }
    payload.update(over)
    return client.post("/consents", json=payload).json()


def test_grant_revoke_access_are_all_recorded_in_order(client):
    consent = _grant(client, ["medical"])
    client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["medical"],
    })
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})

    events = client.get("/audit").json()
    types = [e["event_type"] for e in events]
    assert types == ["consent_granted", "access_granted", "consent_revoked"]
    assert all(e["patient_id"] == "P-001" for e in events)
    assert all(e["consent_id"] == consent["consent_id"] for e in events)
    # 时间戳单调（可重放）
    stamps = [e["timestamp"] for e in events]
    assert stamps == sorted(stamps)


def test_denied_access_is_recorded_with_reason(client):
    client.post("/access/data", json={
        "patient_id": "P-002", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["medical"],
    })
    event = client.get("/audit").json()[-1]
    assert event["event_type"] == "access_denied"
    assert event["denial_reason"] == "consent_not_found"


def test_masking_result_recorded_without_raw_values(client):
    _grant(client, ["identity", "contact"])
    client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": ["identity", "contact"],
    })
    events = client.get("/audit").json()
    granted = [e for e in events if e["event_type"] == "access_granted"][0]
    assert set(granted["masked_fields"]) == {"name", "id_card", "phone", "email"}
    assert set(granted["returned_fields"]) == {"name", "id_card", "phone", "email"}
    # 审计任何字段中都不得出现原始敏感值
    import json
    blob = json.dumps(events, ensure_ascii=False)
    for secret in RAW_SECRETS:
        assert secret not in blob


def test_audit_failure_fails_closed_for_data_access(client):
    _grant(client, ["medical"])
    client.post("/test/audit-failure?enabled=true")
    resp = client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["medical"],
    })
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "audit_write_failed"
    client.post("/test/audit-failure?enabled=false")


def test_audit_failure_blocks_grant_and_revoke(client):
    client.post("/test/audit-failure?enabled=true")
    grant = client.post("/consents", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "allowed_fields": ["medical"],
    })
    assert grant.status_code == 403
    assert grant.json()["denial_reason"] == "audit_write_failed"
    client.post("/test/audit-failure?enabled=false")


def test_audit_remains_readable_for_replay_after_recovery(client):
    consent = _grant(client, ["medical"])
    client.post("/test/audit-failure?enabled=true")
    failed = client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["medical"],
    })
    assert failed.status_code == 403
    client.post("/test/audit-failure?enabled=false")
    # 恢复后：之前的授予记录仍在，且失败的访问未写入（落盘失败）
    events = client.get("/audit").json()
    assert [e["event_type"] for e in events] == ["consent_granted"]
    assert events[0]["consent_id"] == consent["consent_id"]
