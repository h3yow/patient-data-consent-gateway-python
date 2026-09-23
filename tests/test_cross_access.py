"""跨用途与跨接收方访问：拒绝原因必须可区分。"""
from __future__ import annotations

from tests.conftest import logger, seed_patient


def _grant(client, recipient: str, purpose: str, fields=None):
    fields = fields or ["diagnosis"]
    req = {"patient_id": "P-0001", "recipient_id": recipient, "purpose": purpose, "fields": fields}
    r = client.post("/consents", json=req)
    assert r.status_code == 201, r.text
    return r.json()


def _access(client, recipient: str, purpose: str, fields=None):
    req = {
        "patient_id": "P-0001", "recipient_id": recipient,
        "purpose": purpose, "fields": fields or ["diagnosis"],
    }
    r = client.post("/access", json=req)
    return req, r


def test_cross_purpose_is_denied(client) -> None:
    seed_patient(client)
    _grant(client, "R-RES", "research")
    req, r = _access(client, "R-RES", "marketing")
    logger.info("跨用途输入=%s 判定依据=接收方仅有research授权 PURPOSE_MISMATCH", req)
    assert r.status_code == 403
    assert r.json()["error"] == "PURPOSE_MISMATCH"
    audit = client.get("/audit").json()["entries"]
    assert audit[-1]["deny_reason"] == "PURPOSE_MISMATCH"


def test_cross_recipient_is_denied(client) -> None:
    seed_patient(client)
    _grant(client, "R-RES", "research")
    req, r = _access(client, "R-OTHER", "research")
    logger.info("跨接收方输入=%s 判定依据=research用途仅授权R-RES RECIPIENT_MISMATCH", req)
    assert r.status_code == 403
    assert r.json()["error"] == "RECIPIENT_MISMATCH"


def test_no_consent_at_all_is_consent_not_found(client) -> None:
    seed_patient(client)
    req, r = _access(client, "R-NEW", "payment")
    logger.info("无任何同意输入=%s 判定=CONSENT_NOT_FOUND", req)
    assert r.status_code == 404
    assert r.json()["error"] == "CONSENT_NOT_FOUND"


def test_unknown_pair_is_consent_not_found(client) -> None:
    # 用途与接收方在该患者上都从未出现过授权：无任何同意
    seed_patient(client)
    _grant(client, "R-RES", "research")
    req, r = _access(client, "R-OTHER", "marketing")
    logger.info("双维度均无授权输入=%s 判定=CONSENT_NOT_FOUND", req)
    assert r.status_code == 404
    assert r.json()["error"] == "CONSENT_NOT_FOUND"


def test_recipient_mismatch_takes_priority_over_purpose(client) -> None:
    # R-OTHER 已在 treatment 用途上获授权，同时 research 用途授权给了 R-RES；
    # R-OTHER 请求 research 时两种“不匹配”同时成立，固定优先报告 RECIPIENT_MISMATCH
    seed_patient(client)
    _grant(client, "R-RES", "research")
    _grant(client, "R-OTHER", "treatment")
    req, r = _access(client, "R-OTHER", "research")
    logger.info("双不匹配输入=%s 判定依据=同用途存在他人授权 RECIPIENT_MISMATCH优先", req)
    assert r.status_code == 403
    assert r.json()["error"] == "RECIPIENT_MISMATCH"
