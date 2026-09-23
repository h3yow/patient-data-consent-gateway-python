"""最小必要范围与去标识化边界测试。"""

from __future__ import annotations

RAW_SECRETS = ["张三丰", "110101199001017890", "13812345678", "sanfeng@example.com"]


def _grant(client, fields, purpose="treatment", recipient="R-HOSPITAL", patient="P-001"):
    resp = client.post(
        "/consents",
        json={
            "patient_id": patient,
            "recipient_id": recipient,
            "purpose": purpose,
            "allowed_fields": fields,
        },
    )
    assert resp.status_code == 201
    return resp.json()


def _access(client, fields, purpose="treatment", recipient="R-HOSPITAL", patient="P-001"):
    return client.post(
        "/access/data",
        json={
            "patient_id": patient,
            "recipient_id": recipient,
            "purpose": purpose,
            "requested_fields": fields,
        },
    )


def test_only_authorized_fields_returned(client):
    _grant(client, ["medical"])
    body = _access(client, ["medical"]).json()
    assert body["allowed"] is True
    assert set(body["data"].keys()) == {"diagnosis", "medications"}
    assert body["returned_fields"] == ["diagnosis", "medications"]
    assert body["masked_fields"] == []


def test_sensitive_fields_are_masked_even_when_authorized(client):
    _grant(client, ["identity", "contact", "profile", "medical"])
    body = _access(client, ["identity", "contact", "profile", "medical"]).json()
    data = body["data"]

    # 原始值绝不出现
    raw = client.get("/audit").text + str(data)
    for secret in RAW_SECRETS:
        assert secret not in raw

    # 掩码边界
    assert data["name"] == "张**"
    assert data["id_card"].endswith("7890") and set(data["id_card"][:-4]) == {"*"}
    assert data["phone"].startswith("138") and data["phone"].endswith("5678")
    assert data["email"].endswith("@example.com") and "sanfeng" not in data["email"]
    assert set(body["masked_fields"]) == {"name", "id_card", "phone", "email"}

    # 非敏感字段保持原值
    assert data["gender"] == "男"
    assert data["diagnosis"] == "2型糖尿病"


def test_scope_exceeded_returns_no_protected_data(client):
    _grant(client, ["medical"])
    body = _access(client, ["medical", "identity"]).json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "scope_exceeded"
    assert body["data"] is None
    assert body["returned_fields"] == []
    assert body["masked_fields"] == []


def test_partial_field_not_leaked_when_sibling_category_denied(client):
    # 只授 medical，请求 profile：任何人口学字段都不得泄露
    _grant(client, ["medical"])
    body = _access(client, ["profile"]).json()
    assert body["data"] is None
    audit = client.get("/audit").text
    for secret in RAW_SECRETS:
        assert secret not in audit


def test_identity_only_does_not_return_medical(client):
    _grant(client, ["identity"])
    body = _access(client, ["identity"]).json()
    assert set(body["data"].keys()) == {"name", "id_card"}
    assert "diagnosis" not in body["data"]


def test_error_response_contains_no_raw_values(client):
    _grant(client, ["medical"])
    resp = _access(client, ["identity"])
    assert resp.status_code == 200
    text = resp.text
    for secret in RAW_SECRETS:
        assert secret not in text


def test_medical_scope_research_purpose(client):
    _grant(client, ["medical"], purpose="research", recipient="R-RESEARCH")
    body = _access(
        client, ["medical"], purpose="research", recipient="R-RESEARCH"
    ).json()
    assert body["allowed"] is True
    assert set(body["data"].keys()) == {"diagnosis", "medications"}
