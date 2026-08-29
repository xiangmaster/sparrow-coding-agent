"""pytest 公共配置，包括显式启用的付费 API 测试开关。"""

from collections.abc import Sequence

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-api-smoke",
        action="store_true",
        default=False,
        help="显式运行会访问真实 DeepSeek API 的冒烟测试",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: Sequence[pytest.Item]
) -> None:
    if config.getoption("--run-api-smoke"):
        return
    skipped = pytest.mark.skip(reason="需要显式传入 --run-api-smoke")
    for item in items:
        if "api_smoke" in item.keywords:
            item.add_marker(skipped)
