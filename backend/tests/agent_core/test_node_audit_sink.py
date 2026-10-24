"""审计出口 + 成本口径的**节点级**用例（v1.5.1）。

为什么必须单独一组：上一版所有用例都停在纯函数层（`compute_metrics` / `_aggregate_orders` /
`evaluate_rules`），于是下面这几处**在节点里**删掉之后，全量用例依然是绿的：

1. `compute_metrics_node` 把 `cost_table` 传进 `compute_metrics`（**两条分支各一次**：
   "今天的桶已在 by_date 里" 与 "用 raw 现算"）；
2. 成本表坏掉时的 `ConfigError` 分支（节点记 CONFIG 错并让 run 失败，而不是降级成"没有成本"）；
3. 审计条目写进 JSONL 汇（`metrics.computed` / `anomaly.detected` / `sku_costs.unmatched`）。

第 3 条是**根因**级的：`state.audit` 只在 checkpoint 里，而 `/api/logs` 只读 JSONL。
两处都不写 = `loss_making` 这类"只告警、不产生 approval_request"的规则**没有任何出口**
（审批台看不到，日志页也看不到）。所以这一组用例的核心断言是"**文件里真的出现了**"，
而不是"返回值里带着它"。

调用方式：走节点本体（`__wrapped__`），绕开 `traced()` 包装。包装器负责写 `node.trace`
并给 `agent_traces.step_count` +1（那要先插好 run 摘要行）；这组用例测的是节点的业务返回值
与审计出口，不该被一次 DB 写入绑架——包装器本身由 `test_graph_paths.py` /
`test_resume_contract.py` 覆盖，最后一条用例还会再用整跑一次确认 `node.trace` 没被污染。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from app.agent import adapters, resume_agent, run_agent
from app.agent.adapters import AgentRuntime
from app.agent.nodes import collect, compute_metrics_node, detect_anomalies_node
from app.enums import ErrorCategory, RunStatus
from app.errors import ConfigError
from app.observability.logging import JsonlLogSink, get_logger
from app.services import costs
from app.services.notifier import DryRunNotifier
from app.services.rules import load_rules

from .conftest import (
    FIXED_NOW,
    SHOP_ID,
    TODAY,
    FakeDoudianClient,
    FakeResponse,
    decisions_for,
    node_events,
)

RUN_ID = "run_node_audit"

#: 一条能触发 `gmv_drop_day` 的指标（当天 GMV 只有近 7 日均值的 10%）。
METRICS: dict[str, Any] = {
    "date": TODAY,
    "gmv_cents": 100_000,
    "order_count": 5,
    "paid_order_count": 5,
    "refund_order_count": 0,
    "refund_amount_cents": 0,
    "aov_cents": 20_000,
    "refund_rate": 0.0,
    "uv": None,
    "cvr": None,
    "gmv_7d_avg_cents": 1_000_000,
    "gross_margin": None,
    "cost_coverage_ratio": None,
    "computed_at": "2026-10-23T12:00:00Z",
}


# --------------------------------------------------------------------------------------
# 构造入参（只造被测节点真正读的字段；其余留空，避免"顺手跑了别的逻辑"）
# --------------------------------------------------------------------------------------


def _raw(
    *,
    by_sku: Mapping[str, Mapping[str, int]],
    products: Sequence[Mapping[str, Any]] = (),
    include_today_in_by_date: bool = True,
) -> dict[str, Any]:
    """一份 `RawSnapshot`：`order_summary` 带 `by_sku`（成本口径的唯一数据源）。

    `include_today_in_by_date=False` 时 `by_date` 里只有历史日——用来把 `compute_metrics_node`
    逼到"没有今天的桶 → 用 `raw` 现算"那条分支（`nodes.py:816` 的 `cost_table=`）。
    """
    summary: dict[str, Any] = {
        "gmv_cents": sum(int(entry["gmv_cents"]) for entry in by_sku.values()),
        "order_count": 1,
        "paid_order_count": 1,
        "refund_order_count": 0,
        "refund_amount_cents": 0,
        "uv": None,
        "by_sku": {sku: dict(entry) for sku, entry in by_sku.items()},
    }
    by_date: dict[str, Any] = {
        "2026-10-22": {
            "gmv_cents": 1_000_000,
            "order_count": 10,
            "paid_order_count": 10,
            "refund_order_count": 0,
            "refund_amount_cents": 0,
            "uv": None,
            "by_sku": {},
        }
    }
    if include_today_in_by_date:
        by_date[TODAY] = dict(summary)
    return {
        "collected_at": "2026-10-23T10:00:00.000Z",
        "window": {"start": "2026-10-16", "end": TODAY},
        "shop": {"shop_id": SHOP_ID},
        "products": [dict(item) for item in products],
        "order_summary": {**summary, "by_date": by_date},
        "source": {"methods": ["order.searchList"], "request_ids": [], "partial": False},
    }


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "trace_id": "trace_node_audit",
        "shop_id": SHOP_ID,
        "thread_id": f"{SHOP_ID}:{RUN_ID}",
        "trigger": "manual",
        "started_at": "2026-10-23T12:00:00Z",
        "status": RunStatus.RUNNING.value,
        "iteration": 0,
        "raw": None,
        "metrics": None,
        "history": [],
        "anomalies": [],
        "pending_actions": [],
        "approved_actions": [],
        "rejected_actions": [],
        "approval": None,
        "execution_results": [],
        "effect_report": None,
        "errors": [],
        "audit": [],
    }
    state.update(overrides)
    return state


def _config(runtime: Any = None) -> dict[str, Any]:
    configurable: dict[str, Any] = {"now": FIXED_NOW}
    if runtime is not None:
        configurable["runtime"] = runtime
    return {"configurable": configurable}


# --------------------------------------------------------------------------------------
# 1) 节点确实把 cost_table 传进了指标（两个调用点各钉一条）
# --------------------------------------------------------------------------------------


async def test_compute_metrics_node_injects_cost_table_on_the_today_branch(
    monkeypatch: pytest.MonkeyPatch, sink: JsonlLogSink
) -> None:
    """`by_date` 里有今天 → 走 `history[-1]` 分支（`nodes.py:810` 的 `cost_table=`）。

    删掉那个 `cost_table=`，这里的 `gross_margin` 就会是 `None`（"这家店没配成本"），
    而纯函数用例仍然全绿——这正是"缺节点级测试"的具体形态。
    """
    monkeypatch.setattr(costs, "_sku_costs", {"sku_a": 28_000})
    update = await compute_metrics_node.__wrapped__(  # type: ignore[attr-defined]
        _state(
            raw=_raw(
                by_sku={"sku_a": {"gmv_cents": 199_000, "units": 1}},
                products=[{"sku_id": "sku_a"}],
            )
        ),
        _config(),
    )

    metrics = update["metrics"]
    assert metrics["gross_margin"] is not None, "cost_table 没有传进 compute_metrics"
    # (199000 - 28000) / 199000 = 0.859296（成本 28000 分/件 × 1 件）
    assert metrics["gross_margin"] == round((199_000 - 28_000) / 199_000, 6)
    assert metrics["cost_coverage_ratio"] == 1.0


async def test_compute_metrics_node_injects_cost_table_on_the_spot_branch(
    monkeypatch: pytest.MonkeyPatch, sink: JsonlLogSink
) -> None:
    """`by_date` 里没有今天 → 走"用 `raw` 现算"分支（`nodes.py:816` 的 `cost_table=`）。

    两条分支各有一次 `compute_metrics(..., cost_table=cost_table)`：只钉住一条，另一条删掉
    照样全绿。
    """
    monkeypatch.setattr(costs, "_sku_costs", {"sku_a": 28_000})
    update = await compute_metrics_node.__wrapped__(  # type: ignore[attr-defined]
        _state(
            raw=_raw(
                by_sku={"sku_a": {"gmv_cents": 199_000, "units": 1}},
                products=[{"sku_id": "sku_a"}],
                include_today_in_by_date=False,
            )
        ),
        _config(),
    )

    metrics = update["metrics"]
    assert metrics["date"] == TODAY
    assert metrics["gross_margin"] == round((199_000 - 28_000) / 199_000, 6)
    # 历史桶也带着同一份成本口径（不是只有今天算对了）：2026-10-22 没有 by_sku → None
    assert update["history"][0]["gross_margin"] is None
    assert update["history"][-1]["gross_margin"] == metrics["gross_margin"]


# --------------------------------------------------------------------------------------
# 2) 成本表配置坏掉：节点必须"响亮地失败"，不许降级成"没有成本"
# --------------------------------------------------------------------------------------


async def test_compute_metrics_node_fails_loudly_when_the_cost_table_is_broken(
    monkeypatch: pytest.MonkeyPatch, sink: JsonlLogSink
) -> None:
    """`ConfigError` 分支（`nodes.py:776-794`）删掉后，这条会红。

    为什么不能降级：把"成本表读坏了"当成"这家店没配成本"，会让 `gross_margin` 变成 `None`、
    `loss_making` 永不触发——**一次配置事故被伪装成正常状态**，比直接失败危险得多。
    """
    detail = "config/sku_costs.yaml: costs 必须是映射（sku_id -> 整数分），收到 list"

    def _broken() -> dict[str, int]:
        raise ConfigError(detail)

    monkeypatch.setattr(costs, "get_sku_costs", _broken)

    update = await compute_metrics_node.__wrapped__(  # type: ignore[attr-defined]
        _state(raw=_raw(by_sku={"sku_a": {"gmv_cents": 199_000, "units": 1}})),
        _config(),
    )

    assert update["status"] == RunStatus.FAILED.value
    assert "metrics" not in update, "配置坏了不许照样给出（缺成本口径的）指标"
    messages = [str(item["message"]) for item in update["errors"]]
    assert any("成本表加载失败" in text and detail in text for text in messages), messages
    # 分类必须是 CONFIG（运维据此知道该改文件，而不是重试一次上游调用）
    assert {str(item["category"]) for item in update["errors"]} == {
        ErrorCategory.CONFIG.value
    }


# --------------------------------------------------------------------------------------
# 3) 审计条目**确实落进** JSONL 汇（根因修复的验收点）
# --------------------------------------------------------------------------------------


async def test_detected_anomaly_is_written_to_the_jsonl_sink(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """`anomaly.detected` 必须**在文件里**出现，且带上 `rule_id` / `observed` / `threshold`。

    这条就是缺陷 1 的验收：`loss_making` 只告警、不产生 `approval_request`，所以审批台看不见；
    如果它也不进 JSONL，这条 critical 告警在整个系统里**没有任何出口**。
    level 按 severity 映射（critical→error / warn→warn / info→info），
    否则 `/api/logs?run_id=…&level=error` 找不到它。
    """
    update = await detect_anomalies_node.__wrapped__(  # type: ignore[attr-defined]
        _state(metrics=dict(METRICS)),
        _config(runtime),
    )
    assert [item["rule_id"] for item in update["anomalies"]] == ["gmv_drop_day"]

    # (a) `state.audit` 原样保留（checkpoint 里的审计留痕），且那条是**汇总**（带 kind）。
    assert [entry["event"] for entry in update["audit"]] == ["anomaly.detected"]
    assert update["audit"][0]["data"]["kind"] == "summary"
    assert "rule_id" not in update["audit"][0]["data"]

    # (b) JSONL 里有一行**明细**（不是只有 count/rule_ids 的汇总）。
    #     按 `kind` 区分两种 schema（v1.5.2）：不再用"data 里有没有 rule_id"来猜，
    #     那种猜法一旦上游给明细补上 `count` 就会静默把明细当汇总（或反过来）。
    rows = sink.read(run_id=RUN_ID, event_prefix="anomaly.")
    summary_rows = [row for row in rows if row["data"].get("kind") == "summary"]
    detail_rows = [row for row in rows if row["data"].get("kind") == "detail"]
    assert len(summary_rows) == 1, [row["data"] for row in rows]
    assert len(detail_rows) == 1, [row["data"] for row in rows]

    # 两种 schema 的判别键必须真的能判别（补上 kind 之前只能靠 .get() 猜）
    summary_data = summary_rows[0]["data"]
    assert summary_data["count"] == 1 and summary_data["rule_ids"] == ["gmv_drop_day"]
    assert "rule_id" not in summary_data
    assert summary_rows[0]["level"] == "info"
    detail_data = detail_rows[0]["data"]
    assert detail_data["rule_id"] == "gmv_drop_day"
    assert "count" not in detail_data

    row = detail_rows[0]
    assert row["event"] == "anomaly.detected"
    assert row["node"] == "detect_anomalies"
    assert row["level"] == "warn"  # 夹具规则是 warn
    assert row["data"]["observed"] == 100_000
    assert row["data"]["threshold"] == 700_000.0  # gmv_7d_avg_cents * 0.7
    assert row["data"]["anomaly_id"] == update["anomalies"][0]["anomaly_id"]

    # (c) 真的落盘了（不是只在内存返回值里）
    text = Path(sink.path).read_text(encoding="utf-8")
    assert '"event":"anomaly.detected"' in text
    assert '"rule_id":"gmv_drop_day"' in text
    assert "gmv_drop_day" in text
    assert '"kind":"summary"' in text and '"kind":"detail"' in text


async def test_critical_anomaly_is_queryable_as_error_level(
    settings: Any, sink: JsonlLogSink, rules_path: Path
) -> None:
    """critical 的异常必须落 `level=error`（`loss_making` 就是这一档）。

    用一个 critical 的临时规则走同一条路：`/api/logs?level=error` 的口径是运营找
    "今天哪里红了"的入口，把 critical 写成 info 等于让日志页和告警台互相矛盾。
    """
    rules_path.write_text(
        """
version: "1"
rules:
  - id: loss_making_probe
    enabled: true
    severity: critical
    metric: gross_margin
    when: "gross_margin is not None and gross_margin < 0.0"
    window: day
    description: "探针：成本覆盖的成交额整体低于成本价"
""",
        encoding="utf-8",
    )
    runtime = AgentRuntime(
        settings=settings,
        client=None,
        notifier=DryRunNotifier(logger=get_logger("tests.agent_core"), sink=sink),
        rules=load_rules(rules_path),
        clock=lambda: FIXED_NOW,
    )

    update = await detect_anomalies_node.__wrapped__(  # type: ignore[attr-defined]
        _state(metrics={**METRICS, "gross_margin": -0.113106, "cost_coverage_ratio": 1.0}),
        _config(runtime),
    )
    assert [item["rule_id"] for item in update["anomalies"]] == ["loss_making_probe"]

    errors = sink.read(run_id=RUN_ID, level="error")
    assert [row["event"] for row in errors] == ["anomaly.detected"]
    assert errors[0]["data"]["rule_id"] == "loss_making_probe"
    assert errors[0]["data"]["observed"] == -0.113106


async def test_metrics_and_unmatched_cost_audits_are_written_to_the_sink(
    monkeypatch: pytest.MonkeyPatch, sink: JsonlLogSink
) -> None:
    """`metrics.computed` 与 `sku_costs.unmatched` 同样要能被 grep / `/api/logs?event=` 查到。"""
    monkeypatch.setattr(costs, "_sku_costs", {"sku_a": 28_000, "sku_gone": 5_000})
    update = await compute_metrics_node.__wrapped__(  # type: ignore[attr-defined]
        _state(
            raw=_raw(
                by_sku={"sku_a": {"gmv_cents": 199_000, "units": 1}},
                # 商品列表里只有 sku_a：`sku_gone` 在成本表里却对不上商品 → 必须可见
                products=[{"sku_id": "sku_a"}],
            )
        ),
        _config(),
    )

    assert {entry["event"] for entry in update["audit"]} == {
        "metrics.computed",
        "sku_costs.unmatched",
    }

    computed = sink.read(run_id=RUN_ID, event_prefix="metrics.")
    assert len(computed) == 1
    assert computed[0]["event"] == "metrics.computed"
    assert computed[0]["level"] == "info"
    # `None`（没配成本）与真实 0.0 必须在这个出口上也能区分
    assert computed[0]["data"]["gross_margin"] == round((199_000 - 28_000) / 199_000, 6)

    unmatched = sink.read(run_id=RUN_ID, event_prefix="sku_costs.")
    assert len(unmatched) == 1
    assert unmatched[0]["event"] == "sku_costs.unmatched"
    assert unmatched[0]["level"] == "warn"
    assert unmatched[0]["data"]["sku_ids"] == ["sku_gone"]
    assert "sku_gone" in Path(sink.path).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# 4) units<=0 的脏数据：降级 + warn 审计，不许打死整轮
# --------------------------------------------------------------------------------------


async def test_collect_degrades_zero_units_instead_of_killing_the_run(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """`product_count: 0` → 按 1 件 + 一条 warn 审计；节点**不许**抛异常（否则整轮 500）。

    旧实现：采集层接受 `0`、指标层拒绝 `0` → 异常从 `collect` 逃出 → 图标 failed 并 re-raise
    → `POST /api/agent/run` 500。一条脏数据打死当天所有指标，而且埋点只在 `node.trace` 的
    `error` 里，看不出"是件数脏"。
    """
    fake_client.orders = [
        {
            "order_id": "o_zero_units",
            "sku_id": "sku_a",
            "pay_amount": 199_000,
            "product_count": 0,
            "create_time": f"{TODAY} 09:00:00",
        }
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    # 采集成功（没有 status=failed），件数按 1 件计入成本口径
    assert update.get("status") != RunStatus.FAILED.value
    raw = update["raw"]
    assert raw["order_summary"]["by_sku"] == {"sku_a": {"gmv_cents": 199_000, "units": 1}}

    degraded = [entry for entry in update["audit"] if entry["event"] == "collect.units_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["data"]["reasons"] == {"non_positive": 1}
    assert degraded[0]["data"]["samples"][0]["value"] == 0
    # F5：样本必须能被跨 run 定位（页内下标 order[0] 换个 run 就指向别的单了）
    assert degraded[0]["data"]["samples"][0]["order_id"] == "o_zero_units"

    rows = sink.read(run_id=RUN_ID, event_prefix="collect.units_degraded")
    assert len(rows) == 1
    assert rows[0]["level"] == "warn"
    assert rows[0]["data"]["kind"] == "units"

    # 指标层随后仍然能算出毛利率（这正是"不许打死当天指标"的含义）：
    # 采集侧降级成 1 件之后，指标层的"成交额 > 0 但 units = 0"防御性校验不会再把整轮打断。
    monkeypatch.setattr(costs, "_sku_costs", {"sku_a": 28_000})
    metrics_update = await compute_metrics_node.__wrapped__(  # type: ignore[attr-defined]
        _state(raw=raw), _config(runtime)
    )
    assert metrics_update["metrics"]["gross_margin"] == round(
        (199_000 - 28_000) / 199_000, 6
    )


async def test_collect_reports_zero_revenue_paid_rows(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """有 `pay_time` 但成交额为 0 的已支付行：不进成本口径，但必须留痕。

    F5 的两处文案/样本修正：

    - 判据是"成交额为 0"，它同时涵盖**真实的 0 元赠品单**与"上游漏了 `pay_amount`"，
      所以 warn 文案不能只写"（缺 pay_amount）"——那会把赠品单误报成上游故障；
    - 样本补 `units` / `unit_cost_cents`：只有"有 N 行没进成本口径"是算不出漏了多少成本的，
      事后无法排序"先修哪一单"。
    """
    monkeypatch.setattr(costs, "_sku_costs", {"sku_a": 80_000})
    fake_client.orders = [
        {
            "order_id": "o_zero_revenue",
            "sku_id": "sku_a",
            "pay_time": f"{TODAY} 10:00:00",
            "product_count": 1,
            "create_time": f"{TODAY} 10:00:00",
        }
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    summary = update["raw"]["order_summary"]
    assert summary["paid_order_count"] == 1  # GMV 口径不变：它确实是一笔已支付订单
    assert summary["gmv_cents"] == 0
    assert summary["by_sku"] == {}, "0 元行不许给成本口径添一件成本"

    rows = sink.read(run_id=RUN_ID, event_prefix="collect.zero_revenue_paid")
    assert len(rows) == 1
    assert rows[0]["level"] == "warn"
    assert rows[0]["data"]["kind"] == "zero_revenue_paid"
    assert rows[0]["data"]["samples"] == [
        {
            "order_id": "o_zero_revenue",
            "where": "order[0]",
            "sku_id": "sku_a",
            "units": 1,
            "unit_cost_cents": 80_000,
        }
    ], "样本必须能算出漏了多少成本（units × unit_cost_cents = 80000 分）"
    # 文案不能把"真实的 0 元单"说成"上游缺字段"：两种原因都要提到。
    assert "可能缺 pay_amount" in rows[0]["message"]
    assert "也可能真是 0 元单" in rows[0]["message"]


# --------------------------------------------------------------------------------------
# 5) 行级脏数据（类型不对/负金额）：整行拒绝 + 审计，**不许**打死整轮（v1.5.2 F1）
# --------------------------------------------------------------------------------------


async def _collect_then_metrics(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    raw_owner: dict[str, Any],
    *,
    unit_cost_cents: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一次 `collect`（返回值已在 `raw_owner["update"]` 里），再用它的 `raw` 算当天指标。

    为什么这两步要写在同一个 helper 里：F1 的验收点是"**逐行拒绝之后，当天指标仍然算得出来**"。
    只断言"节点没 failed"是不够的——真正会被人看见的后果是"毛利率/成交额还算不算得出"。
    """
    update = raw_owner["update"]
    monkeypatch.setattr(costs, "_sku_costs", {"sku_a": unit_cost_cents})
    metrics_update = await compute_metrics_node.__wrapped__(  # type: ignore[attr-defined]
        _state(raw=update["raw"]), _config(runtime)
    )
    return update, metrics_update


async def test_collect_rejects_a_string_product_count_row_instead_of_killing_the_run(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """`product_count: "3"`（字符串）→ **整行拒绝**，不是整轮 500，也不是"按 1 件猜"。

    这是"脏数据不许打死整轮"没修到的那一半：上一版只对 `units<=0` 降级，类型错仍然从
    `collect` 逃出 → `graph` 标 failed 再 re-raise → `POST /api/agent/run` **500**，
    一条脏行打死当天所有指标，而且全仓没有任何用例钉住它。

    本用例钉四件事：① 节点不 failed；② 审计里有 `collect.row_rejected` 且计数/原因/样本对；
    ③ 指标里**不含**这一行（GMV / 订单数 / by_sku 都不含）；④ 当天指标仍然算得完。
    """
    fake_client.orders = [
        {
            "order_id": "o_bad_units",
            "sku_id": "sku_a",
            "pay_amount": 199_000,
            "product_count": "3",  # 上游把 int 变成了 str 的典型回归
            "create_time": f"{TODAY} 09:00:00",
        },
        {
            "order_id": "o_good",
            "sku_id": "sku_a",
            "pay_amount": 9_900,
            "product_count": 1,
            "create_time": f"{TODAY} 10:00:00",
        },
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    # ① 节点不许 failed（旧实现：ValueError 逃出 → 标 failed 并 re-raise → API 500）
    assert update.get("status") != RunStatus.FAILED.value
    assert "raw" in update

    # ③ 指标里不含被拒的那一行（注意 order_count 也只算 1：**原子拒绝**，不留半行）
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 9_900
    assert summary["order_count"] == 1
    assert summary["paid_order_count"] == 1
    assert summary["by_sku"] == {"sku_a": {"gmv_cents": 9_900, "units": 1}}

    # ② 审计：state.audit 一条汇总，计数/原因/样本都要对
    rejected = [entry for entry in update["audit"] if entry["event"] == "collect.row_rejected"]
    assert len(rejected) == 1
    data = rejected[0]["data"]
    assert data["count"] == 1
    assert data["reasons"] == {"bad_units": 1}
    assert data["excluded_from_metrics"] is True
    sample = data["samples"][0]
    assert sample["order_id"] == "o_bad_units"
    assert sample["where"] == "order[0]"
    assert sample["reason"] == "bad_units"
    assert "product_count" in sample["error"]

    rows = sink.read(run_id=RUN_ID, event_prefix="collect.row_rejected")
    assert len(rows) == 1
    assert rows[0]["level"] == "warn"
    assert rows[0]["data"]["kind"] == "row_rejected"
    assert "没有进入 GMV / 订单数 / by_sku" in rows[0]["message"]

    # ④ 仍然能算完当天指标
    update, metrics_update = await _collect_then_metrics(
        monkeypatch, runtime, {"update": update}, unit_cost_cents=3_000
    )
    metrics = metrics_update["metrics"]
    assert metrics["gmv_cents"] == 9_900
    assert metrics["gross_margin"] == round((9_900 - 3_000) / 9_900, 6)


async def test_collect_rejects_a_string_pay_amount_row_instead_of_killing_the_run(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """`pay_amount: "199.00"`（字符串）→ 同样整行拒绝 + 审计，不许打死整轮。

    金额一律整数分是本仓的硬口径（DECISIONS 附 A），所以"元/分"的字符串既不降级成 19900
    也不按 float 解析——**猜错一次就是资损级歧义**。能做的是：这一行不进任何指标，但留下
    可定位的审计（`order_id` + 原始异常原文），让人回去修上游/补数据。
    """
    fake_client.orders = [
        {
            "order_id": "o_bad_amount",
            "sku_id": "sku_a",
            "pay_amount": "199.00",  # 元为单位的字符串：既不是整数分，也不该被猜
            "product_count": 1,
            "create_time": f"{TODAY} 09:00:00",
        },
        {
            "order_id": "o_good",
            "sku_id": "sku_a",
            "pay_amount": 19_900,
            "product_count": 1,
            "create_time": f"{TODAY} 11:00:00",
        },
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    # ① 节点不许 failed
    assert update.get("status") != RunStatus.FAILED.value

    # ③ 被拒行的 19900/199000 分都没有进指标
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 19_900
    assert summary["order_count"] == 1
    assert summary["by_sku"] == {"sku_a": {"gmv_cents": 19_900, "units": 1}}

    # ② 审计计数/原因/样本
    rejected = [entry for entry in update["audit"] if entry["event"] == "collect.row_rejected"]
    assert len(rejected) == 1
    data = rejected[0]["data"]
    assert data["count"] == 1
    assert data["reasons"] == {"bad_pay_amount": 1}
    assert data["excluded_from_metrics"] is True
    sample = data["samples"][0]
    assert sample["order_id"] == "o_bad_amount"
    assert sample["reason"] == "bad_pay_amount"
    assert "pay_amount" in sample["error"]

    rows = sink.read(run_id=RUN_ID, event_prefix="collect.row_rejected")
    assert len(rows) == 1 and rows[0]["level"] == "warn"

    # ④ 仍然能算完当天指标
    update, metrics_update = await _collect_then_metrics(
        monkeypatch, runtime, {"update": update}, unit_cost_cents=3_000
    )
    assert metrics_update["metrics"]["gmv_cents"] == 19_900
    assert metrics_update["metrics"]["gross_margin"] == round(
        (19_900 - 3_000) / 19_900, 6
    )


# --------------------------------------------------------------------------------------
# 5b) 整批契约坏了：硬失败（记错 + failed 走 finalize），**不是** 500、也不是"0 单 0 元"
# --------------------------------------------------------------------------------------


async def test_collect_fails_hard_when_the_whole_order_batch_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """`data` 形状不对（不是"某一行脏"）→ 记一条具体错 + `failed`，且不产出任何指标。

    这是 F1 判据的另一半：行级脏数据降级，整批契约坏了硬失败。两种都**不许**变成 500——
    `collect` 返回 `status=failed` 交给 `finalize`，API 侧看到的是"这一轮失败了"的正常结果，
    而不是一个栈。若这里退化成"空订单列表"，今天的所有规则都会因为指标为 0 而静默失效。
    """

    async def _malformed(**_kwargs: Any) -> FakeResponse:
        return FakeResponse(data={"oops": 1})

    monkeypatch.setattr(fake_client, "search_orders", _malformed)

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") == RunStatus.FAILED.value
    assert "raw" not in update, "整批坏了不许照样交出一份全 0 的 raw"
    assert [entry["event"] for entry in update["audit"]] == ["collect.failed"]
    messages = [str(item["message"]) for item in update["errors"]]
    assert any("订单数据结构不合法" in text for text in messages), messages
    assert any("缺少订单列表字段" in text for text in messages), messages


# --------------------------------------------------------------------------------------
# 6) 审计写盘失败：旁路失败不许弄死整轮（v1.5.2 F2）
# --------------------------------------------------------------------------------------


class _ExplodingSink:
    """`write` 必抛的汇（模拟磁盘满 / 文件句柄失效）。"""

    def write(self, event: str, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    def close(self) -> None:  # pragma: no cover - 夹具替身没有真实句柄
        return None


async def test_audit_sink_write_failure_does_not_kill_the_run(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`sink.write` 抛 `OSError` → 只留 warning，节点不 failed，`state.audit` 一份不丢。

    JSONL 是**旁路**（`/api/logs` 的入口）。让它的写盘异常逃出节点，等于把"日志写不进去"
    升级成"当天指标算不出来 + `POST /api/agent/run` 500"；而调用点先 `append_audits(...)`
    再 `_log_audit(...)`，异常一抛，节点的返回值根本构造不出来——`state.audit` 那一份也一起没了。
    """
    fake_client.orders = [
        {
            "order_id": "o_ok",
            "sku_id": "sku_a",
            # 故意不给件数：`collect` 会走一次 `_log_audit`（collect.units_degraded），
            # 让"写 JSONL"这一步真的发生——否则本用例什么都没测到。
            "pay_amount": 199_000,
            "create_time": f"{TODAY} 09:00:00",
        }
    ]
    monkeypatch.setattr(adapters, "_sink", _ExplodingSink())

    with caplog.at_level(logging.WARNING, logger="app.agent.nodes"):
        update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    # ① 节点不 failed，业务结果完整
    assert update.get("status") != RunStatus.FAILED.value
    assert update["raw"]["order_summary"]["gmv_cents"] == 199_000

    # ② state.audit 仍然保留（旁路失败不许连累 checkpoint 里的那一份）
    events = [entry["event"] for entry in update["audit"]]
    assert "collect.done" in events
    assert "collect.units_degraded" in events

    # ③ 有 warning，且带 event 名与异常类型（不许静默吞掉缺陷）
    warnings_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "JSONL 审计写入失败" in warnings_text
    assert "OSError" in warnings_text
    assert "collect.units_degraded" in warnings_text
    assert "state.audit 仍然保留" in warnings_text
    assert all(record.levelno == logging.WARNING for record in caplog.records)


# --------------------------------------------------------------------------------------
# 7) 新事件是**独立事件**，不是往 node.trace 里塞键
# --------------------------------------------------------------------------------------


async def test_new_audit_events_do_not_pollute_the_frozen_node_trace_keys(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`node.trace` 的 `data` 键集合是 §7.6.1 冻结的五个：审计出口走**新事件**。

    这一条同时是一次整跑：整轮跑完必须能在 JSONL 里同时看到 `node.trace`（老事件，键不变）
    和 `anomaly.detected`（新事件，带 rule_id）。
    """
    result = await run_agent(SHOP_ID, RUN_ID)
    assert result.status == RunStatus.AWAITING_APPROVAL.value

    traces = node_events(sink, RUN_ID)
    assert [row["node"] for row in traces][:3] == ["collect", "compute_metrics", "detect_anomalies"]
    frozen = {"outcome", "duration_ms", "input_digest", "output_digest", "error"}
    for row in traces:
        assert set(row["data"]) == frozen, "node.trace 的 data 键集合被改动了（§7.6.1 冻结）"

    audited = sink.read(run_id=RUN_ID, event_prefix="anomaly.")
    assert [
        row["data"].get("rule_id")
        for row in audited
        if row["data"].get("kind") == "detail"
    ] == ["gmv_drop_day"], "整跑里也必须有一条带 rule_id 的异常事件"

    # 收尾时把审批走完（verdict=reject：本用例只验证日志出口，不真的改价），
    # 避免留下挂起的 thread 影响同一文件里的其它用例。
    approved = await resume_agent(
        result.thread_id,
        decisions_for([item.action_id for item in result.pending_approvals], verdict="reject"),
    )
    assert approved.status == RunStatus.DONE.value
