"""变更（收窄/暂停/恢复）与访问并发的自洽性：
每个访问要么看到完整的变更前状态，要么看到完整的变更后状态，
绝不返回已被移掉或已暂停的字段。

关于“单调性”的断言口径（修复历史上偶发失败的关键）：
- 服务端把每次访问与每次状态变更串行化在同一把存储锁上，因此对**同一
  工作线程内顺序发出的请求**，其结论与状态提交顺序一致：一旦该线程看到
  变更后状态，就不可能再看到变更前状态（状态变更本身单调）。
- 但**跨线程按客户端完成时刻聚合**的序列不保证单调：线程 A 可能在线程 B
  的变更提交前就已完成决策，只是响应被调度器延后到 B 之后才入列。这正是
  “每个访问看到完整前态或完整后态”的快照语义，不属于不一致。
  因此用例只对单线程序列断言单调，对所有响应逐一断言自洽。
"""

from __future__ import annotations

import os
import threading

from fastapi.testclient import TestClient

# 并发压力重复次数：次数越大越能稳定暴露竞态，且断言本身不依赖调度时序，
# 因而多轮运行结果确定、不会偶发失败。可用环境变量 ITERATIONS/WORKERS 调整。
ITERATIONS = int(os.environ.get("ITERATIONS", "200"))
WORKERS = int(os.environ.get("WORKERS", "4"))


def _grant(client, fields):
    resp = client.post("/consents", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "allowed_fields": list(fields),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["consent_id"]


def _access(client, fields):
    return client.post("/access/data", json={
        "patient_id": "P-001",
        "recipient_id": "R-HOSPITAL",
        "purpose": "treatment",
        "requested_fields": list(fields),
    }).json()


def _assert_monotonic_per_worker(per_worker: list[list[dict]]) -> None:
    """每个工作线程内：一旦拒绝，后续必须继续拒绝（对单调的状态变更成立）。"""
    for seq in per_worker:
        first_deny = next(
            (i for i, body in enumerate(seq) if not body["allowed"]), None
        )
        if first_deny is not None:
            assert all(
                body["allowed"] is False for body in seq[first_deny:]
            ), "同一线程内状态回退：变更后又看到变更前状态"


def _run_concurrent(mutate, hammer, workers=WORKERS):
    barrier = threading.Barrier(workers + 1)
    threads = [
        threading.Thread(target=hammer, args=(barrier,)) for _ in range(workers)
    ]
    for t in threads:
        t.start()
    barrier.wait()
    mutate()
    for t in threads:
        t.join()


def test_concurrent_narrow_and_access_is_self_consistent(client):
    consent_id = _grant(client, ["identity", "medical"])
    per_worker: list[list[dict]] = [[] for _ in range(WORKERS)]

    def narrow():
        resp = client.post("/consents/narrow", json={
            "consent_id": consent_id, "allowed_fields": ["medical"],
        })
        assert resp.status_code == 200, resp.text

    def hammer(barrier):
        local = TestClient(client.app)
        idx = barrier.wait() - 1
        for _ in range(ITERATIONS):
            body = _access(local, ["identity"])
            per_worker[idx].append(body)

    _run_concurrent(narrow, hammer)

    results = [body for seq in per_worker for body in seq]
    assert len(results) == ITERATIONS * WORKERS
    for body in results:
        if body["allowed"]:
            # 看到完整变更前状态：返回的是脱敏后的 identity 数据
            assert body["data"] is not None
            assert set(body["returned_fields"]) == {"name", "id_card"}
            assert body["denial_reason"] is None
        else:
            # 看到完整变更后状态：超出范围且绝无数据
            assert body["denial_reason"] == "scope_exceeded"
            assert body["data"] is None
            assert body["returned_fields"] == []
            assert body["masked_fields"] == []

    _assert_monotonic_per_worker(per_worker)

    # 收窄本身留下一条带前后范围的审计
    events = client.get("/audit").json()
    narrowed = [e for e in events if e["event_type"] == "consent_narrowed"]
    assert len(narrowed) == 1
    assert narrowed[0]["previous_allowed_fields"] == ["identity", "medical"]
    assert narrowed[0]["allowed_fields"] == ["medical"]

    # 审计自洽：每个放行的访问都对应一条 access_granted，且该事件记录的
    # 返回字段只可能来自变更前快照（identity）；每个拒绝都有 access_denied。
    granted_events = [e for e in events if e["event_type"] == "access_granted"]
    denied_events = [e for e in events if e["event_type"] == "access_denied"]
    allowed = [b for b in results if b["allowed"]]
    denied = [b for b in results if not b["allowed"]]
    assert len(granted_events) == len(allowed)
    assert len(denied_events) == len(denied)
    for e in granted_events:
        assert set(e["returned_fields"]) == {"name", "id_card"}
        assert set(e["masked_fields"]) == {"name", "id_card"}
    for e in denied_events:
        assert e["denial_reason"] == "scope_exceeded"
        assert e["returned_fields"] is None

    # 最终状态确定为变更后：再访问 identity 一律超范围
    assert _access(client, ["identity"])["denial_reason"] == "scope_exceeded"


def test_concurrent_suspend_and_access_is_self_consistent(client):
    consent_id = _grant(client, ["medical"])
    per_worker: list[list[dict]] = [[] for _ in range(WORKERS)]

    def suspend():
        resp = client.post("/consents/suspend", json={"consent_id": consent_id})
        assert resp.status_code == 200, resp.text

    def hammer(barrier):
        local = TestClient(client.app)
        idx = barrier.wait() - 1
        for _ in range(ITERATIONS):
            body = _access(local, ["medical"])
            per_worker[idx].append(body)

    _run_concurrent(suspend, hammer)

    results = [body for seq in per_worker for body in seq]
    assert len(results) == ITERATIONS * WORKERS
    for body in results:
        if body["allowed"]:
            assert body["data"] is not None
            assert body["denial_reason"] is None
        else:
            # 暂停期间只能以 consent_suspended 拒绝，且绝无数据
            assert body["denial_reason"] == "consent_suspended"
            assert body["data"] is None
            assert body["returned_fields"] == []

    _assert_monotonic_per_worker(per_worker)

    # 审计数量与结论一一对应
    events = client.get("/audit").json()
    granted_events = [e for e in events if e["event_type"] == "access_granted"]
    denied_events = [e for e in events if e["event_type"] == "access_denied"]
    assert len(granted_events) == len([b for b in results if b["allowed"]])
    assert len(denied_events) == len([b for b in results if not b["allowed"]])
    assert all(
        e["denial_reason"] == "consent_suspended" for e in denied_events
    )
    suspended = [e for e in events if e["event_type"] == "consent_suspended"]
    assert len(suspended) == 1

    # 最终确定处于暂停态
    assert _access(client, ["medical"])["denial_reason"] == "consent_suspended"


def test_concurrent_suspend_resume_churn_and_access_is_self_consistent(client):
    """暂停/恢复交替进行时的每个访问结论都必须自洽。

    交替状态变更不单调，因此这里不断言序列单调性，只断言：
    每个响应自洽（放行必带数据；拒绝必为 consent_suspended 且无数据），
    且最终状态与随后的访问结论一致。
    """
    consent_id = _grant(client, ["medical"])
    per_worker: list[list[dict]] = [[] for _ in range(WORKERS)]
    stop = threading.Event()

    def churn():
        # 暂停 -> 恢复 -> ... 多轮交替
        for _ in range(20):
            r1 = client.post(
                "/consents/suspend", json={"consent_id": consent_id}
            )
            assert r1.status_code == 200, r1.text
            r2 = client.post(
                "/consents/resume", json={"consent_id": consent_id}
            )
            assert r2.status_code == 200, r2.text
        stop.set()

    def hammer(barrier):
        local = TestClient(client.app)
        idx = barrier.wait() - 1
        while not stop.is_set():
            body = _access(local, ["medical"])
            per_worker[idx].append(body)

    barrier = threading.Barrier(WORKERS + 1)
    threads = [
        threading.Thread(target=hammer, args=(barrier,)) for _ in range(WORKERS)
    ]
    for t in threads:
        t.start()
    barrier.wait()
    churn()
    for t in threads:
        t.join()

    results = [body for seq in per_worker for body in seq]
    assert len(results) > 0
    for body in results:
        if body["allowed"]:
            # 看到未暂停快照：必须带数据
            assert body["data"] is not None
            assert body["denial_reason"] is None
        else:
            # 看到已暂停快照：原因可区分且绝无数据
            assert body["denial_reason"] == "consent_suspended"
            assert body["data"] is None

    # 写操作两两串行：暂停/恢复次数恰好成对，无重复暂停/恢复被接受
    events = client.get("/audit").json()
    assert (
        len([e for e in events if e["event_type"] == "consent_suspended"]) == 20
    )
    assert (
        len([e for e in events if e["event_type"] == "consent_resumed"]) == 20
    )
    # 每个访问结论都有且仅有一条对应审计
    access_events = [
        e for e in events
        if e["event_type"] in ("access_granted", "access_denied")
    ]
    assert len(access_events) == len(results)

    # 最终处于已恢复状态，访问放行
    assert _access(client, ["medical"])["allowed"] is True
