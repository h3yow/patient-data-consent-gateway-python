"""测试夹具：每个用例获得独立的应用与存储，全部走 HTTP 对外接口。

同时在测试日志中打印“业务域 -> 用例 -> 结果”，便于从日志确认
每类业务覆盖了哪些用例以及多轮运行的稳定性。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("consent_gateway.tests")

# 测试模块 -> 业务域（日志中展示覆盖范围）
BUSINESS_AREAS = {
    "test_consent_decisions": "同意状态与拒绝原因分类",
    "test_deidentification": "最小必要与脱敏",
    "test_audit": "审计可追溯与失败关闭",
    "test_concurrency": "并发撤销与访问一致性",
    "test_narrow": "范围收窄（只减不增/可追溯）",
    "test_suspend_resume": "暂停/恢复状态流转与原因划分",
    "test_fail_closed": "写操作失败关闭一致性",
    "test_change_concurrency": "变更与访问并发自洽性",
    "test_edge_cases": "边界与用例隔离",
}

_OUTCOME_LABEL = {"passed": "通过", "failed": "失败", "skipped": "跳过"}


@pytest.fixture()
def client() -> TestClient:
    # seed=True 仅写入患者/接收方基础数据，同意由用例自行授予，保证可重复
    app = create_app(seed=True)
    return TestClient(app)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when != "call":
        return
    nodeid = report.nodeid
    module = nodeid.split("::")[0].rsplit("/", 1)[-1].removesuffix(".py")
    area = BUSINESS_AREAS.get(module, module)
    case = nodeid.split("::", 1)[1] if "::" in nodeid else nodeid
    logger.info(
        "[用例结果] 业务=%s 用例=%s 结果=%s",
        area,
        case,
        _OUTCOME_LABEL.get(report.outcome, report.outcome),
    )
