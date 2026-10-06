"""测试夹具：每个用例获得独立的应用与存储，全部走 HTTP 对外接口。

另外提供业务级日志：
- 每个测试文件（一类业务）开始时打印“业务横幅”，说明覆盖范围；
- 每个用例结束后打印用例名与结果；
- 汇总阶段按业务文件打印通过/失败计数。
这样在 -s 日志里可以直接看出覆盖了哪些业务、各业务有哪些用例及测试结果。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

logging.basicConfig(level=logging.INFO)

# 测试文件 -> 业务覆盖说明（打印在业务横幅中）
BUSINESS_COVERAGE = {
    "test_consent_decisions.py": "业务：同意状态与拒绝原因分类（未授权/撤销/过期/用途/接收方/范围）",
    "test_deidentification.py": "业务：最小必要投影与敏感字段脱敏边界",
    "test_audit.py": "业务：审计有序可重放、去标识化记录、访问失败关闭",
    "test_concurrency.py": "业务：并发撤销与访问的快照自洽与单调生效",
    "test_narrow.py": "业务：范围收窄（立即生效/只减不增/逐次可追溯）",
    "test_suspend_resume.py": "业务：暂停/恢复流转，以及暂停与过期叠加时的原因划分",
    "test_fail_closed.py": "业务：写操作失败关闭（状态不变、审计无痕、结论前后一致）",
    "test_change_concurrency.py": "业务：收窄/暂停/恢复与访问并发的自洽性",
    "test_fail_closed_concurrency.py": "业务：并发下审计故障的失败关闭一致性",
    "test_edge_cases.py": "业务：边界输入与用例间存储隔离",
}


@pytest.fixture()
def client() -> TestClient:
    # seed=True 仅写入患者/接收方基础数据，同意由用例自行授予，保证可重复
    app = create_app(seed=True)
    return TestClient(app)


def pytest_report_header(config):  # noqa: ANN001
    return [
        "患者数据共享同意与访问网关 · 自动化测试",
        "覆盖业务：同意决策 / 脱敏 / 审计与失败关闭 / 收窄 / 暂停恢复 / 并发自洽 / 边界",
    ]


_banner_printed: set[str] = set()


def pytest_runtest_setup(item) -> None:  # noqa: ANN001
    module_file = item.location[0].split("/")[-1]
    if module_file in BUSINESS_COVERAGE and module_file not in _banner_printed:
        _banner_printed.add(module_file)
        line = "=" * 78
        print(
            f"\n{line}\n# {BUSINESS_COVERAGE[module_file]}\n"
            f"# 文件: tests/{module_file}\n{line}"
        )


def pytest_runtest_logreport(report) -> None:  # noqa: ANN001
    if report.when == "call":
        mark = "PASS" if report.passed else "FAIL"
        print(f"[{mark}] {report.nodeid.split('::')[-1]}  ({report.duration:.3f}s)")


def pytest_terminal_summary(terminalreporter) -> None:  # noqa: ANN001
    """按业务文件（测试模块）汇总通过/失败计数。"""
    stats: dict[str, dict[str, int]] = {}
    for reports in terminalreporter.stats.values():
        for report in reports:
            if getattr(report, "when", None) != "call":
                continue
            module_file = report.location[0].split("/")[-1]
            bucket = stats.setdefault(module_file, {"passed": 0, "failed": 0})
            if report.passed:
                bucket["passed"] += 1
            elif report.failed:
                bucket["failed"] += 1

    terminalreporter.write_sep("=", "按业务汇总")
    for module_file in sorted(stats):
        bucket = stats[module_file]
        desc = BUSINESS_COVERAGE.get(module_file, module_file)
        terminalreporter.write_line(
            f"{module_file:<38} 通过 {bucket['passed']:>2}  失败 {bucket['failed']:>2}  | {desc}"
        )

