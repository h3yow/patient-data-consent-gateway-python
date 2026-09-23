"""边界情况：未知患者/接收方、空字段请求、字段去重。"""

from __future__ import annotations


def test_unknown_patient_denied_not_found_and_no_data(client):
    body = client.post("/access/data", json={
        "patient_id": "P-NOPE", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["medical"],
    }).json()
    assert body["allowed"] is False
    assert body["data"] is None
    assert body["denial_reason"] == "consent_not_found"


def test_unknown_recipient_treated_as_recipient_mismatch(client):
    # P-001 对 R-HOSPITAL 有同意，对完全未知的接收方应判接收方不匹配
    client.post("/consents", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "allowed_fields": ["medical"],
    })
    body = client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-GHOST",
        "purpose": "treatment", "requested_fields": ["medical"],
    }).json()
    assert body["denial_reason"] == "recipient_mismatch"
    assert body["data"] is None


def test_empty_requested_fields_within_scope_returns_empty_data(client):
    client.post("/consents", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "allowed_fields": ["medical"],
    })
    body = client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": [],
    }).json()
    assert body["allowed"] is True
    assert body["data"] == {}
    assert body["returned_fields"] == []
    assert body["masked_fields"] == []


def test_duplicate_requested_fields_are_deduplicated(client):
    client.post("/consents", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "allowed_fields": ["medical"],
    })
    body = client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["medical", "medical"],
    }).json()
    assert body["allowed"] is True
    assert body["returned_fields"] == ["diagnosis", "medications"]


def test_each_test_gets_isolated_store(client):
    """夹具隔离性：新客户端看不到其他用例写入的同意。"""
    body = client.post("/access/data", json={
        "patient_id": "P-001", "recipient_id": "R-HOSPITAL",
        "purpose": "treatment", "requested_fields": ["medical"],
    }).json()
    assert body["denial_reason"] == "consent_not_found"
