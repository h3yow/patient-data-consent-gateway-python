"""基础授权流程：授予 -> 访问 -> 撤销 -> 拒绝。"""
from __future__ import annotations

from tests.conftest import logger, seed_patient


def test_grant_access_then_revoke_takes_immediate_effect(client) -> None:
    seed_patient(client)
    grant_req = {
        "patient_id": "P-0001", "recipient_id": "R-ONC", "purpose": "treatment",
        "fields": ["diagnosis", "medications"], "expires_at": None,
    }
    r = client.post("/consents", json=grant_req)
    assert r.status_code == 201, r.text
    consent_id = r.json()["consent_id"]
    logger.info("授予同意输入=%s 判定=ACTIVE consent_id=%s", grant_req, consent_id)

    access_req = {
        "patient_id": "P-0001", "recipient_id": "R-ONC", "purpose": "treatment",
        "fields": ["diagnosis"],
    }
    r = client.post("/access", json=access_req)
    logger.info("访问输入=%s 判定依据=ACTIVE同意%s 状态=%s", access_req, consent_id, r.status_code)
    assert r.status_code == 200
    assert r.json()["fields"] == {"diagnosis": "原发性高血压"}
    assert r.json()["masked_fields"] == []

    r = client.post("/consents/revoke", json={"consent_id": consent_id, "recipient_id": "R-ONC"})
    assert r.status_code == 200
    logger.info("撤销同意=%s 状态=%s", consent_id, r.json()["status"])

    r = client.post("/access", json=access_req)
    logger.info("撤销后访问输入=%s 判定依据=同意状态REVOKED 响应=%s", access_req, r.json())
    assert r.status_code == 403
    assert r.json()["error"] == "CONSENT_REVOKED"

    # 重复撤销是可区分的冲突错误
    r = client.post("/consents/revoke", json={"consent_id": consent_id, "recipient_id": "R-ONC"})
    assert r.status_code == 409 and r.json()["error"] == "CONFLICT"


def test_unknown_patient_is_denied_and_audited(client) -> None:
    req = {"patient_id": "NOPE", "recipient_id": "R1", "purpose": "research", "fields": ["diagnosis"]}
    r = client.post("/access", json=req)
    logger.info("未知患者访问输入=%s 判定=PATIENT_NOT_FOUND", req)
    assert r.status_code == 404 and r.json()["error"] == "PATIENT_NOT_FOUND"
    audit = client.get("/audit").json()["entries"]
    assert audit[-1]["deny_reason"] == "PATIENT_NOT_FOUND"
