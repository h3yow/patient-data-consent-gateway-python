"""同意状态与拒绝原因分类测试（通过服务对外接口验证）。"""

from __future__ import annotations


def _grant(client, **overrides):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": ["medical"],
    }
    payload.update(overrides)
    resp = client.post("/consents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _data(client, **overrides):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": ["medical"],
    }
    payload.update(overrides)
    return client.post("/access/data", json=payload)


def test_no_consent_is_denied(client):
    resp = _data(client, patient_id="P-002")
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is False
    assert body["data"] is None
    assert body["denial_reason"] == "consent_not_found"


def test_grant_allows_access_then_revoke_takes_immediate_effect(client):
    consent = _grant(client)
    ok = _data(client)
    assert ok.json()["allowed"] is True

    revoked = client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False

    after = _data(client)
    assert after.json()["allowed"] is False
    assert after.json()["denial_reason"] == "consent_revoked"


def test_expired_consent_is_denied(client):
    _grant(client, expires_at="2020-01-01T00:00:00Z", now="2019-12-31T00:00:00Z")
    body = _data(client, now="2026-09-23T00:00:00Z").json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "consent_expired"


def test_not_yet_expired_consent_is_allowed(client):
    _grant(client, expires_at="2099-01-01T00:00:00Z")
    body = _data(client, now="2026-09-23T00:00:00Z").json()
    assert body["allowed"] is True


def test_cross_purpose_is_denied(client):
    _grant(client, purpose="treatment")
    body = _data(client, purpose="research").json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "purpose_mismatch"


def test_cross_recipient_is_denied(client):
    _grant(client, recipient_id="R-HOSPITAL")
    body = _data(client, recipient_id="R-INSURER").json()
    assert body["allowed"] is False
    assert body["denial_reason"] == "recipient_mismatch"


def test_decision_endpoint_matches_data_endpoint(client):
    consent = _grant(client)
    decision = client.post(
        "/access/decision",
        json={
            "patient_id": "P-001",
            "recipient_id": "R-HOSPITAL",
            "purpose": "treatment",
            "requested_fields": ["medical"],
        },
    ).json()
    assert decision["allowed"] is True
    assert decision["consent_id"] == consent["consent_id"]
    assert decision["allowed_fields"] == ["medical"]


def test_revoke_unknown_consent_returns_distinguishable_error(client):
    resp = client.post("/consents/revoke", json={"consent_id": "nope"})
    assert resp.status_code == 403
    assert resp.json()["denial_reason"] == "consent_not_found"


def test_denial_reasons_are_all_distinguishable(client):
    """同一存储内依次制造不同拒绝原因，确认原因码互不混淆。"""
    reasons = []

    # 未授权（P-002 尚无任何同意）
    reasons.append(_data(client, patient_id="P-002").json()["denial_reason"])

    # 在 P-001 上累积同一 (接收方,用途) 的同意
    _grant(client)
    # 跨接收方
    reasons.append(_data(client, recipient_id="R-INSURER").json()["denial_reason"])
    # 跨用途
    reasons.append(_data(client, purpose="marketing").json()["denial_reason"])
    # 越权范围
    reasons.append(_data(client, requested_fields=["identity"]).json()["denial_reason"])

    # 已过期：在 P-002 上授予一条过期同意
    _grant(
        client,
        patient_id="P-002",
        expires_at="2026-09-22T00:00:00Z",
        now="2026-09-20T00:00:00Z",
    )
    reasons.append(
        _data(client, patient_id="P-002", now="2026-09-23T00:00:00Z").json()[
            "denial_reason"
        ]
    )

    # 已撤销：在 P-002 上再授予一条更新的同意并撤销（最近授予优先）
    consent = _grant(client, patient_id="P-002")
    client.post("/consents/revoke", json={"consent_id": consent["consent_id"]})
    reasons.append(_data(client, patient_id="P-002").json()["denial_reason"])

    assert reasons == [
        "consent_not_found",
        "recipient_mismatch",
        "purpose_mismatch",
        "scope_exceeded",
        "consent_expired",
        "consent_revoked",
    ]
