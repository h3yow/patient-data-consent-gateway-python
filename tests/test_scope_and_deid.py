"""最小必要范围与去标识化边界。"""
from __future__ import annotations

from tests.conftest import RAW_SECRET_TOKENS, logger, seed_patient


def test_only_authorized_fields_returned_and_sensitive_masked(client) -> None:
    seed_patient(client)
    grant = {
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["full_name", "phone", "diagnosis"],
    }
    r = client.post("/consents", json=grant)
    assert r.status_code == 201
    req = {
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["full_name", "phone", "diagnosis"],
    }
    r = client.post("/access", json=req)
    logger.info("最小必要访问输入=%s 判定依据=授权字段%s", req, grant["fields"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["fields"].keys()) == {"full_name", "phone", "diagnosis"}
    # 非敏感字段原样
    assert body["fields"]["diagnosis"] == "原发性高血压"
    # 敏感字段已掩码，且掩码值不可还原
    assert body["fields"]["full_name"] == "张**"
    assert body["fields"]["phone"] == "138****1234"
    assert body["masked_fields"] == ["full_name", "phone"]
    # 未授权字段绝不出现在结果里
    assert "id_card" not in body["fields"]
    assert "medications" not in body["fields"]


def test_out_of_scope_request_returns_no_protected_data(client) -> None:
    seed_patient(client)
    r = client.post("/consents", json={
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["diagnosis"],
    })
    assert r.status_code == 201
    # 一个越权字段即整体拒绝（all-or-nothing），且不返回任何字段
    req = {
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["diagnosis", "id_card"],
    }
    r = client.post("/access", json=req)
    logger.info("越权请求输入=%s 判定依据=id_card不在授权范围 响应=%s", req, r.json())
    assert r.status_code == 403
    assert r.json()["error"] == "SCOPE_EXCEEDED"
    assert "fields" not in r.json()

    # 错误体与审计中均不得出现原始敏感值
    for token in RAW_SECRET_TOKENS:
        assert token not in r.text
    audit_text = client.get("/audit").text
    for token in RAW_SECRET_TOKENS:
        assert token not in audit_text


def test_each_sensitive_field_has_masking_boundary(client) -> None:
    seed_patient(client)
    sensitive = ["full_name", "phone", "id_card", "email", "address", "date_of_birth"]
    r = client.post("/consents", json={
        "patient_id": "P-0001", "recipient_id": "R-ALL", "purpose": "treatment",
        "fields": sensitive + ["diagnosis", "medications", "blood_type"],
    })
    assert r.status_code == 201
    req = {
        "patient_id": "P-0001", "recipient_id": "R-ALL", "purpose": "treatment",
        "fields": sensitive,
    }
    r = client.post("/access", json=req)
    assert r.status_code == 200, r.text
    fields = r.json()["fields"]
    logger.info("去标识化边界输入=%s 判定=全部敏感字段掩码 输出=%s", sensitive, fields)
    assert fields == {
        "full_name": "张**",
        "phone": "138****1234",
        "id_card": "110101********1234",
        "email": "z************@example.com",
        "address": "北京市海淀区****",
        "date_of_birth": "1990-**-**",
    }
    assert sorted(r.json()["masked_fields"]) == sorted(sensitive)
    # 任何原始片段都不得出现在返回体中
    for token in RAW_SECRET_TOKENS:
        assert token not in r.text


def test_unknown_field_name_rejected(client) -> None:
    seed_patient(client)
    req = {"patient_id": "P-0001", "recipient_id": "R1", "purpose": "treatment", "fields": ["ssn"]}
    r = client.post("/access", json=req)
    logger.info("未知字段输入=%s 判定=INVALID_REQUEST(400)", req)
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_REQUEST"

    req2 = {"patient_id": "P-0001", "recipient_id": "R1", "purpose": "treatment", "fields": ["diagnosis", "ssn"]}
    r = client.post("/consents", json=req2)
    logger.info("未知字段授予输入=%s 判定=INVALID_REQUEST", req2)
    assert r.status_code == 400 and r.json()["error"] == "INVALID_REQUEST"
