"""`collect` 对上游 `uv` 的透传（v1.3.2）：**有就传、没有就 None、判不了作用域也不摊派**。

三条边界一起钉住（少钉一条就会出现"看板永远空两列"或"cvr 被摊薄成 1/8"这两种反向故障）：

- 上游给了当天的 uv → `metrics.uv` 有值，`cvr` 按 §5.3 公式随之可算；
- 上游没给 uv → `uv` / `cvr` 都是 `None`（**不是 0**，§5.3 的硬规则）；
- 上游给的是**整段查询窗**（近 8 个业务日）的汇总 → 不当成今天的 uv（不按天数摊派），
  只有它显式声明作用域等于当天时才认。

契约依据：§5.3（`uv` 可缺、分母为 0 → `None`）、§7.5（缺数据不补零）、CONTRACT v1.3.2 §0.1。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent import run_agent
from app.observability.logging import JsonlLogSink

from .conftest import SHOP_ID, TODAY, FakeDoudianClient, history_then_today, order, trace_row


def _today_only() -> list[dict[str, Any]]:
    """只有今天两笔已支付订单：单日响应 → 上游 summary 不可能是"别的日子"的汇总。"""
    return [
        order(order_id="t1", create_time=f"{TODAY} 09:00:00", pay_amount_cents=19_900),
        order(order_id="t2", create_time=f"{TODAY} 11:00:00", pay_amount_cents=25_900),
    ]


async def test_uv_from_upstream_summary_reaches_metrics(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """单日响应 + `summary.uv` → `metrics.uv` 有值，`cvr = paid_order_count / uv`（§5.3）。"""
    fake_client.orders = _today_only()
    fake_client.order_summary = {"uv": 480}

    result = await run_agent(SHOP_ID, "run_uv_ok")

    metrics = (result.state or {})["metrics"]
    assert metrics["paid_order_count"] == 2
    assert metrics["uv"] == 480
    assert metrics["cvr"] == pytest.approx(2 / 480, abs=1e-6)


async def test_uv_missing_stays_none_not_zero(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """上游没给 uv → `uv` / `cvr` 都是 `None`。补 0 会把"没数据"画成"转化率 0%"（§5.3）。"""
    fake_client.orders = _today_only()

    result = await run_agent(SHOP_ID, "run_uv_absent")

    metrics = (result.state or {})["metrics"]
    assert metrics["uv"] is None
    assert metrics["cvr"] is None
    assert metrics["uv"] != 0  # 显式反向断言：None 与 0 是两件事


async def test_window_aggregate_uv_is_not_attributed_to_today(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """订单跨 8 个业务日 + summary 未声明作用域 → 不当成今天的 uv（摊派会让 cvr 缩水约 8 倍）。"""
    fake_client.orders = history_then_today(history_gmv=1_000_000, today_gmv=200_000)
    fake_client.order_summary = {"uv": 480}

    result = await run_agent(SHOP_ID, "run_uv_window")

    metrics = (result.state or {})["metrics"]
    assert metrics["uv"] is None
    assert metrics["cvr"] is None


async def test_summary_declared_scope_wins_on_multi_day_payload(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """上游用 `summary.date` 明确声明"这段汇总就是今天" → 即使回了多天订单也认这个 uv。"""
    fake_client.orders = history_then_today(history_gmv=1_000_000, today_gmv=200_000)
    fake_client.order_summary = {"uv": 480, "date": TODAY}

    result = await run_agent(SHOP_ID, "run_uv_scoped")

    metrics = (result.state or {})["metrics"]
    assert metrics["uv"] == 480
    assert metrics["cvr"] is not None


async def test_malformed_uv_fails_loudly_instead_of_silently_dropping(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`uv` 有值但不是非负整数 → 抛错（run 记 failed）。

    静默降级成 `None` 会把"上游改了数据结构"伪装成"这家店没有访客数据"——那是一个永远
    不会被告警发现、却能让所有 uv/cvr 规则静默失效的状态，比一次响亮的失败糟得多。
    """
    fake_client.orders = _today_only()
    fake_client.order_summary = {"uv": "480"}  # 字符串：上游把 int 变成了 str 的典型回归

    with pytest.raises(ValueError, match="uv"):
        await run_agent(SHOP_ID, "run_uv_bad")

    assert (await trace_row(SHOP_ID, "run_uv_bad"))["status"] == "failed"
