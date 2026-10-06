"""并发场景下的失败关闭一致性：

- 收窄/暂停/恢复/撤销与访问并发时，若审计写不进去：
  写操作整体失败——同意状态不变、审计流中也不留该写事件的痕迹；
- 故障注入期间的每个访问要么是一个完整的正常结论（放行/业务拒绝），
  要么是 audit_write_failed 且不带任何数据；不允许“报错但状态已半生效”；
- 故障解除后，之前失败的写操作必须可以原样重试成功，且结论确定。
"""

from __future__ import annotations

import os
import threading
import time
from itertools import count

from fastapi.testclient import TestClient

# 可用环境变量 ITERATIONS/WORKERS 调整并发压力规模。
ITERATIONS = int(os.environ.get("ITERATIONS", "100"))
WORKERS = int(os.environ.get("WORKERS", "4"))


def _grant(client, fields=("identity", "medical"), **over):
    payload = {
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": list(fields),
    }
    payload.update(over)
    resp = client.post("/consents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _access(client, fields=("identity",)):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": list(fields),
    })


def _set_failure(client, enabled: bool):
    resp = client.post(
        f"/test/audit-failure?enabled={'true' if enabled else 'false'}"
    )
    assert resp.status_code == 200


def _audit_types(client):
    return [e["event_type"] for e in client.get("/audit").json()]


def test_failed_narrow_under_concurrency_leaves_no_half_state(client):
    consent = _grant(client)
    cid = consent["consent_id"]
    start = threading.Barrier(WORKERS + 1)
    responses: list = []
    responses_lock = threading.Lock()

    def failed_narrow():
        local = TestClient(client.app)
        start.wait()
        time.sleep(0.02)
        _set_failure(local, True)
        try:
            resp = local.post("/consents/narrow", json={
                "consent_id": cid, "allowed_fields": ["medical"],
            })
            with responses_lock:
                responses.append(("narrow", resp.status_code, resp.json()))
        finally:
            _set_failure(local, False)

    def hammer(idx: int):
        local = TestClient(client.app)
        start.wait()
        for _ in range(ITERATIONS):
            resp = _access(local)
            with responses_lock:
                responses.append(("access", resp.status_code, resp.json()))

    threads = [threading.Thread(target=failed_narrow)]
    threads += [threading.Thread(target=hammer, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    narrow_responses = [r for r in responses if r[0] == "narrow"]
    access_responses = [r for r in responses if r[0] == "access"]
    assert len(narrow_responses) == 1
    assert narrow_responses[0][1] == 403
    assert narrow_responses[0][2]["denial_reason"] == "audit_write_failed"
    assert len(access_responses) == ITERATIONS * WORKERS

    for _, status, body in access_responses:
        if status == 403:
            # 审计故障期间的访问：失败关闭，无数据、无半生效
            assert body["denial_reason"] == "audit_write_failed"
            assert "data" not in body or body.get("data") is None
        else:
            # 审计可用期间的访问：收窄从未生效，identity 始终在范围内
            assert body["allowed"] is True
            assert body["data"] is not None
            assert set(body["returned_fields"]) == {"name", "id_card"}

    # 不允许出现 scope_exceeded：失败的收窄没有产生任何效果
    business_denials = {
        body.get("denial_reason")
        for _, status, body in access_responses if status == 200 and not body["allowed"]
    }
    assert business_denials == set()

    # 收窄既未改状态，也没有留下 consent_narrowed 审计痕迹
    assert _audit_types(client).count("consent_narrowed") == 0
    after = _access(client).json()
    assert after["allowed"] is True
    assert set(after["returned_fields"]) == {"name", "id_card"}

    # 故障解除后原样重试收窄：确定成功且立即生效
    ok = client.post("/consents/narrow", json={
        "consent_id": cid, "allowed_fields": ["medical"],
    })
    assert ok.status_code == 200
    assert _access(client).json()["denial_reason"] == "scope_exceeded"
    assert _audit_types(client).count("consent_narrowed") == 1


def test_failed_suspend_resume_under_concurrency_leaves_no_half_state(client):
    consent = _grant(client, fields=("medical",))
    cid = consent["consent_id"]
    start = threading.Barrier(WORKERS + 1)
    write_outcomes: list = []
    write_lock = threading.Lock()
    accesses: list = []
    access_lock = threading.Lock()

    def failed_writes():
        local = TestClient(client.app)
        start.wait()
        time.sleep(0.02)

        # 阶段一：对有效同意执行“会失败的暂停”
        _set_failure(local, True)
        resp = local.post("/consents/suspend", json={"consent_id": cid})
        _set_failure(local, False)
        with write_lock:
            write_outcomes.append(("suspend", resp.status_code, resp.json()))

        # 失败的暂停未生效：先成功暂停，再制造“会失败的恢复”
        ok = local.post("/consents/suspend", json={"consent_id": cid})
        assert ok.status_code == 200
        _set_failure(local, True)
        resp = local.post("/consents/resume", json={"consent_id": cid})
        _set_failure(local, False)
        with write_lock:
            write_outcomes.append(("resume", resp.status_code, resp.json()))

    def hammer(idx: int):
        local = TestClient(client.app)
        start.wait()
        for _ in range(ITERATIONS):
            resp = _access(local, fields=("medical",))
            with access_lock:
                accesses.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=failed_writes)]
    threads += [threading.Thread(target=hammer, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    # 两次失败的写都因审计不可用而失败关闭（业务校验本身均通过）
    assert [(op, code) for op, code, _ in write_outcomes] == [
        ("suspend", 403),
        ("resume", 403),
    ]
    assert all(
        body["denial_reason"] == "audit_write_failed"
        for _, _, body in write_outcomes
    )

    for status, body in accesses:
        if status == 403:
            # 审计故障窗口：失败关闭
            assert body["denial_reason"] == "audit_write_failed"
            assert body.get("data") is None
        elif body["allowed"]:
            # 有效态快照：带数据
            assert body["data"] is not None
        else:
            # 暂停态快照（成功暂停之后、失败恢复未生效期间）：只可能是暂停
            assert body["denial_reason"] == "consent_suspended"
            assert body["data"] is None

    # 失败的暂停未留痕；成功暂停有 1 条；失败的恢复未留痕、也未解除暂停
    types = _audit_types(client)
    assert types.count("consent_suspended") == 1
    assert types.count("consent_resumed") == 0

    # 失败的恢复没有半生效：仍处暂停
    still = _access(client, fields=("medical",)).json()
    assert still["denial_reason"] == "consent_suspended"

    # 故障解除后恢复成功，访问重新放行
    ok = client.post("/consents/resume", json={"consent_id": cid})
    assert ok.status_code == 200
    after = _access(client, fields=("medical",)).json()
    assert after["allowed"] is True
    assert after["data"] is not None


def test_failed_access_leaves_no_trace_and_same_conclusion_repeats(client):
    _grant(client, fields=("medical",))
    start = threading.Barrier(WORKERS + 1)
    statuses: list = []
    lock = threading.Lock()

    def toggle():
        local = TestClient(client.app)
        start.wait()
        # 交替注入/解除故障，与访问并发
        for i in count():
            _set_failure(local, i % 2 == 0)
            if i > 10:
                break
            time.sleep(0.005)
        _set_failure(local, False)

    def hammer(idx: int):
        local = TestClient(client.app)
        start.wait()
        for _ in range(ITERATIONS):
            resp = _access(local, fields=("medical",))
            with lock:
                statuses.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=toggle)]
    threads += [threading.Thread(target=hammer, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert len(statuses) == ITERATIONS * WORKERS

    granted = denied = failed_closed = 0
    for status, body in statuses:
        if status == 403:
            assert body["denial_reason"] == "audit_write_failed"
            failed_closed += 1
        elif body["allowed"]:
            assert body["data"] is not None
            granted += 1
        else:
            # 本用例中不存在任何业务拒绝的前提，因此不应出现
            denied += 1
    assert denied == 0
    # 故障确实穿插发生，两种结论都被观察到
    assert failed_closed > 0
    assert granted > 0

    # 失败关闭的访问绝不写入审计；审计事件数恰好等于成功访问数 + 授予
    events = client.get("/audit").json()
    assert len([e for e in events if e["event_type"] == "access_granted"]) == granted
    assert not any(
        e["event_type"] == "access_denied"
        and e["denial_reason"] == "audit_write_failed"
        for e in events
    )

    # 故障彻底解除后，重复同一访问结论稳定一致（连跑多次）
    conclusions = {
        _access(client, fields=("medical",)).json()["allowed"] for _ in range(20)
    }
    assert conclusions == {True}


def test_revoke_failure_mid_concurrency_keeps_consent_active(client):
    consent = _grant(client, fields=("medical",))
    cid = consent["consent_id"]
    start = threading.Barrier(WORKERS + 1)
    bodies: list = []
    lock = threading.Lock()

    def failed_revoke():
        local = TestClient(client.app)
        start.wait()
        time.sleep(0.02)
        _set_failure(local, True)
        resp = local.post("/consents/revoke", json={"consent_id": cid})
        _set_failure(local, False)
        assert resp.status_code == 403
        assert resp.json()["denial_reason"] == "audit_write_failed"

    def hammer(idx: int):
        local = TestClient(client.app)
        start.wait()
        for _ in range(ITERATIONS):
            resp = _access(local, fields=("medical",))
            with lock:
                bodies.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=failed_revoke)]
    threads += [threading.Thread(target=hammer, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    # 不存在 consent_revoked 业务结论；审计可用期间访问一律放行
    for status, body in bodies:
        if status == 200:
            assert body["allowed"] is True
            assert body["data"] is not None
        else:
            assert body["denial_reason"] == "audit_write_failed"
    assert _audit_types(client).count("consent_revoked") == 0

    # 同意仍可被正式撤销
    ok = client.post("/consents/revoke", json={"consent_id": cid})
    assert ok.status_code == 200
    assert _access(client, fields=("medical",)).json()[
        "denial_reason"
    ] == "consent_revoked"
