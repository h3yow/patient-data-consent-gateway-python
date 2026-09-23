"""同意有效期：未到期可访问，到期后可区分地拒绝，且状态视图同步。"""
from __future__ import annotations

from tests.conftest import logger, seed_patient


def _grant_short(client, *, purpose: str = "research", recipient: str = "R-RES"):
    req = {
        "patient_id": "P-0001", "recipient_id": recipient, "purpose": purpose,
        "fields": ["diagnosis"], "expires_at": "2026-09-23T12:00:00+00:00",
    }
    r = client.post("/consents", json=req)
    assert r.status_code == 201, r.text
    return r.json()["consent_id"], req


def test_access_before_expiry_succeeds(client) -> None:
    seed_patient(client)
    consent_id, grant = _grant_short(client)
    logger.info("过期用例授予输入=%s 判定依据=expires_at晚于当前时钟", grant)
    req = {"patient_id": "P-0001", "recipient_id": "R-RES", "purpose": "research", "fields": ["diagnosis"]}
    r = client.post("/access", json=req)
    logger.info("过期前访问输入=%s 判定=EXPIRED判断为false 状态=%s", req, r.status_code)
    assert r.status_code == 200


def test_access_after_expiry_denied_with_distinct_reason(client) -> None:
    seed_patient(client)
    consent_id, _ = _grant_short(client)
    # 把可测试时钟推到有效期之后
    client.post("/internal/clock/advance", json={"seconds": 60 * 60 * 24 * 2})
    req = {"patient_id": "P-0001", "recipient_id": "R-RES", "purpose": "research", "fields": ["diagnosis"]}
    r = client.post("/access", json=req)
    logger.info("过期后访问输入=%s 判定依据=now>expires_at(%s) 响应=%s", req, consent_id, r.json())
    assert r.status_code == 403
    assert r.json()["error"] == "CONSENT_EXPIRED"

    # 状态视图与拒绝原因一致
    r = client.get("/patients/P-0001/consents")
    assert r.json()["consents"][0]["status"] == "expired"

    # 审计中的拒绝原因可重放
    audit = client.get("/audit").json()["entries"]
    assert audit[-1]["deny_reason"] == "CONSENT_EXPIRED"


def test_grant_with_past_expiry_is_rejected(client) -> None:
    seed_patient(client)
    req = {
        "patient_id": "P-0001", "recipient_id": "R-RES", "purpose": "research",
        "fields": ["diagnosis"], "expires_at": "2000-01-01T00:00:00+00:00",
    }
    r = client.post("/consents", json=req)
    logger.info("过去时间授予输入=%s 判定=INVALID_REQUEST", req)
    assert r.status_code == 400 and r.json()["error"] == "INVALID_REQUEST"
