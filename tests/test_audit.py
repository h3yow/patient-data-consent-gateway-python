"""审计可追溯：可重放、只含脱敏值；无法写入时一律失败关闭。"""
from __future__ import annotations

from tests.conftest import RAW_SECRET_TOKENS, logger, seed_patient


def test_audit_trail_is_replayable_and_contains_only_masked_values(client) -> None:
    seed_patient(client)
    client.post("/consents", json={
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["full_name", "diagnosis"],
    })
    req = {
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["full_name", "diagnosis"],
    }
    r = client.post("/access", json=req)
    assert r.status_code == 200

    entries = client.get("/audit").json()["entries"]
    actions = [e["action"] for e in entries]
    logger.info("审计重放动作序列=%s", actions)
    assert actions == ["consent_granted", "access_allowed"]

    access_entry = entries[-1]
    # 可重放地还原“看到了什么”：只有去标识化结果
    assert access_entry["masked_fields"] == ["full_name"]
    assert access_entry["masked_values"] == {"full_name": "张**"}
    assert access_entry["authorized_fields"] == ["diagnosis", "full_name"]
    assert access_entry["requested_fields"] == ["diagnosis", "full_name"]
    # 序号单调
    assert [e["seq"] for e in entries] == [1, 2]

    # 整条审计序列化内容中不含原始敏感值
    audit_text = client.get("/audit").text
    for token in RAW_SECRET_TOKENS:
        assert token not in audit_text


def test_denied_access_is_recorded_without_data(client) -> None:
    seed_patient(client)
    client.post("/consents", json={
        "patient_id": "P-0001", "recipient_id": "R-RES", "purpose": "research",
        "fields": ["diagnosis"],
    })
    req = {
        "patient_id": "P-0001", "recipient_id": "R-RES", "purpose": "research",
        "fields": ["id_card"],  # 越权
    }
    r = client.post("/access", json=req)
    assert r.status_code == 403 and r.json()["error"] == "SCOPE_EXCEEDED"
    entry = client.get("/audit").json()["entries"][-1]
    logger.info("拒绝审计输入=%s 记录=%s", req, entry)
    assert entry["action"] == "access_denied"
    assert entry["deny_reason"] == "SCOPE_EXCEEDED"
    assert entry["masked_values"] == {}


def test_access_fails_closed_when_audit_unwritable(client) -> None:
    seed_patient(client)
    client.post("/consents", json={
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["diagnosis"],
    })
    # 打开审计故障注入：允许的访问也必须失败关闭
    client.post("/internal/audit/failing", json={"failing": True})
    req = {
        "patient_id": "P-0001", "recipient_id": "R-INS", "purpose": "payment",
        "fields": ["diagnosis"],
    }
    r = client.post("/access", json=req)
    logger.info("审计不可写访问输入=%s 判定=FAIL_CLOSED 响应=%s", req, r.json())
    assert r.status_code == 500
    assert r.json()["error"] == "AUDIT_WRITE_FAILED"
    assert "fields" not in r.json()

    # 授予/撤销同样失败关闭
    r = client.post("/consents", json={
        "patient_id": "P-0001", "recipient_id": "R2", "purpose": "treatment",
        "fields": ["diagnosis"],
    })
    assert r.status_code == 500 and r.json()["error"] == "AUDIT_WRITE_FAILED"

    # 恢复后：故障期间未产生授权，访问仍被拒（且审计恢复后可正常记录）
    client.post("/internal/audit/failing", json={"failing": False})
    r = client.post("/access", json={
        "patient_id": "P-0001", "recipient_id": "R2", "purpose": "treatment",
        "fields": ["diagnosis"],
    })
    assert r.status_code == 404 and r.json()["error"] == "CONSENT_NOT_FOUND"
