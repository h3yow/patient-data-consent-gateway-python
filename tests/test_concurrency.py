"""并发撤销与访问：结论必须与审计串行顺序一致、可解释。"""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from main import app
from tests.conftest import logger, seed_patient

ROUNDS = 100
ACCESSORS = 6


def test_concurrent_revoke_vs_access_is_linearizable() -> None:
    with TestClient(app) as client:
        client.post("/internal/reset")
        seed_patient(client)
        r = client.post("/consents", json={
            "patient_id": "P-0001", "recipient_id": "R-ONC", "purpose": "treatment",
            "fields": ["diagnosis", "full_name"],
        })
        assert r.status_code == 201
        consent_id = r.json()["consent_id"]

        barrier = threading.Barrier(ACCESSORS + 1)
        outcomes: list[tuple[int, str, str]] = []  # (worker, status, error)
        out_lock = threading.Lock()

        def accessor(worker_id: int) -> None:
            barrier.wait()
            local = TestClient(app)
            req = {
                "patient_id": "P-0001", "recipient_id": "R-ONC",
                "purpose": "treatment", "fields": ["diagnosis"],
            }
            for _ in range(ROUNDS):
                resp = local.post("/access", json=req)
                with out_lock:
                    if resp.status_code == 200:
                        outcomes.append((worker_id, "200", ""))
                    else:
                        outcomes.append((worker_id, str(resp.status_code), resp.json()["error"]))

        def revoker() -> None:
            barrier.wait()
            local = TestClient(app)
            resp = local.post("/consents/revoke", json={
                "consent_id": consent_id, "recipient_id": "R-ONC",
            })
            with out_lock:
                outcomes.append((-1, str(resp.status_code), ""))
            logger.info("并发: 撤销返回 %s", resp.status_code)

        threads = [threading.Thread(target=accessor, args=(i,)) for i in range(ACCESSORS)]
        threads.append(threading.Thread(target=revoker))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed = [o for o in outcomes if o[0] != -1 and o[1] == "200"]
        denied = [o for o in outcomes if o[0] != -1 and o[1] == "403" and o[2] == "CONSENT_REVOKED"]
        other = [o for o in outcomes if o[0] != -1 and o not in allowed and o not in denied]
        logger.info("并发结果: 允许=%d 撤销拒绝=%d 其他=%s", len(allowed), len(denied), other)

        # 撤销线程自身的 200 不属于访问结论
        revoke_results = [o for o in outcomes if o[0] == -1]
        access_outcomes = [o for o in outcomes if o[0] != -1]

        # 1) 访问只有两种结论：成功（仅含授权字段）或 CONSENT_REVOKED；不允许其他错误
        assert len(access_outcomes) == ACCESSORS * ROUNDS
        assert all(o[1] == "200" or (o[1] == "403" and o[2] == "CONSENT_REVOKED")
                   for o in access_outcomes)

        # 2) 撤销必须恰好成功一次
        assert revoke_results == [(-1, "200", "")]

        # 3) 与审计串行顺序一致：revoked 之后不得再出现 access_allowed
        entries = client.get("/audit").json()["entries"]
        revoke_idx = next(i for i, e in enumerate(entries) if e["action"] == "consent_revoked")
        after = entries[revoke_idx + 1:]
        post_revoke_access = [e for e in after if e["action"].startswith("access_")]
        assert all(e["result"] == "denied" for e in post_revoke_access), "撤销后仍有成功访问"
        assert all(e["deny_reason"] == "CONSENT_REVOKED" for e in post_revoke_access)

        before = entries[:revoke_idx]
        pre_revoke_allowed = [e for e in before if e["action"] == "access_allowed"]
        # 每个访问结论都能在审计中重放：撤销前允许数量与成功访问数对齐
        assert len(pre_revoke_allowed) == len(allowed)
        logger.info(
            "线性化校验通过: 撤销前允许访问=%d, 撤销后拒绝访问=%d, 撤销审计序号=%d",
            len(pre_revoke_allowed), len(post_revoke_access),
            entries[revoke_idx]["seq"],
        )
