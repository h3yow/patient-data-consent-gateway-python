"""测试夹具：每个用例获得独立的应用与存储，全部走 HTTP 对外接口。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

logging.basicConfig(level=logging.INFO)


@pytest.fixture()
def client() -> TestClient:
    # seed=True 仅写入患者/接收方基础数据，同意由用例自行授予，保证可重复
    app = create_app(seed=True)
    return TestClient(app)
