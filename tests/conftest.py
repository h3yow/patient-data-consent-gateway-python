"""pytest 公共夹具：每个用例通过 /internal/reset 相互隔离。"""
from __future__ import annotations

import logging
import sys

import pytest
from fastapi.testclient import TestClient

from main import app

# 让“输入 + 授权判定依据”在单测日志中直接可见
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
for name in ("consent-test", "consent-gateway"):
    lg = logging.getLogger(name)
    if not lg.handlers:
        lg.addHandler(handler)
    lg.setLevel(logging.INFO)
logger = logging.getLogger("consent-test")


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        c.post("/internal/reset")
        yield c


PATIENT_PAYLOAD = {
    "patient_id": "P-0001",
    "full_name": "张三丰",
    "phone": "13812341234",
    "id_card": "110101199003071234",
    "email": "zhang.sanfeng@example.com",
    "address": "北京市海淀区中关村大街1号",
    "date_of_birth": "1990-03-07",
    "diagnosis": "原发性高血压",
    "medications": ["氨氯地平", "阿司匹林"],
    "blood_type": "O",
}

# 任何对外响应/错误/审计序列化结果中都不得出现的原始敏感片段
RAW_SECRET_TOKENS = (
    "张三丰",
    "13812341234",
    "110101199003071234",
    "zhang.sanfeng",
    "中关村大街1号",
)


def seed_patient(client: TestClient, patient_id: str = "P-0001") -> dict:
    payload = dict(PATIENT_PAYLOAD, patient_id=patient_id)
    resp = client.post("/patients", json=payload)
    assert resp.status_code == 201, resp.text
    return payload
