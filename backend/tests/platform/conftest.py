"""platform 测试的 fixture 装配（真正的工具在 `platform_kit.py`）。

单独一个 conftest 是为了让 fixture 被自动发现；工具函数放在 `platform_kit`
是为了测试模块能显式 `import`（conftest 不应被测试模块 import）。
"""

from __future__ import annotations

import logging

import pytest

from platform_kit import FakeClock, LogCapture, _CollectorHandler


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def capture_logger() -> tuple[logging.Logger, LogCapture]:
    """返回 (logger, collector)：collector 里有 `extra=` 的全部结构化字段。"""
    collector = LogCapture()
    logger = logging.getLogger("tests.platform.doudian_client")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.addHandler(_CollectorHandler(collector))
    logger.propagate = False
    return logger, collector
