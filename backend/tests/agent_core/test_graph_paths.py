"""图的两条主路径 + §4.7 的步数不变量。

覆盖 t2 验收清单的 (d) 与 (e)：

- (d) 无异常 → 不进审批节点、直接 END、`status=done`；
- (e) 带审批的一次 run：JSONL 节点事件数 == `agent_traces.step_count` == 10（v1.6.0 起 9 节点 + 重放一次）
  （8 个节点 + `approval_gate` 被重放一次）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent import resume_agent, run_agent
from app.agent.state import Decision
from app.enums import RunStatus
from app.errors import RunStateConflict
from app.observability.logging import JsonlLogSink
from app.services.doudian_client import DoudianClient

from .conftest import (
    FIXED_NOW,
    SHOP_ID,
    FakeDoudianClient,
    action_log_rows,
    approval_rows,
    decisions_for,
    effect_rows,
    history_then_today,
    node_events,
    trace_row,
)


async def test_no_anomaly_ends_without_approval(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """无异常 = 无提议 = 不进审批：`propose_actions` 之后直接到 finalize（§4.2 的"直通"）。

    v1.6.0 起 `detect_anomalies` 与 `propose_actions` 之间多了一个旁路节点 `explain`
    （模型只写解释与建议草稿，不产生动作），因此这条链多一步。
    """
    fake_client.orders = history_then_today(history_gmv=1_000_000, today_gmv=2_000_000)

    result = await run_agent(SHOP_ID, "run_no_anomaly")

    assert result.status == RunStatus.DONE.value
    assert result.pending_approvals == []
    assert result.effect_report is None
    assert approval_rows(result.thread_id) == []

    events = node_events(sink, "run_no_anomaly")
    assert [row["node"] for row in events] == [
        "collect",
        "compute_metrics",
        "detect_anomalies",
        "explain",
        "propose_actions",
        "finalize",
    ]
    # 没进 approval_gate / execute / record_effect：这三条一次都没执行
    assert "approval_gate" not in {row["node"] for row in events}
    assert all(row["data"]["outcome"] == "ok" for row in events)
    assert (await trace_row(SHOP_ID, "run_no_anomaly"))["step_count"] == len(events)

    # 已终态的 thread 没有挂起点：再 resume 必须是 409 而不是"假装成功"
    with pytest.raises(RunStateConflict):
        await resume_agent(result.thread_id, [])


async def test_approval_run_step_count_is_ten(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """带审批的 run：9 个节点 + `approval_gate` 重放一次 = 10 条事件 = step_count 10。

    v1.6.0 起节点数 8 → 9（插入旁路节点 `explain`），因此这条不变量从 9 变 10。
    """
    run_id = "run_with_approval"
    result = await run_agent(SHOP_ID, run_id)
    assert result.status == RunStatus.AWAITING_APPROVAL.value
    assert len(result.pending_approvals) == 1
    pending = result.pending_approvals[0]
    assert pending.status == "pending"
    assert pending.action_type == "price_update"
    assert pending.params == {"price_cents": 9603}  # 9900 * (1 - 3%)
    # §5.5 的 risk 判据：跌幅 >15% → high，>5% → medium，否则 low。
    # 9900 → 9603 的跌幅是 297/9900 = 3%，落在"否则"分支，因此是 low（不是 medium）。
    assert pending.risk == "low"

    # 挂起阶段：6 次执行（collect/compute/detect/explain/propose/approval_gate 的那一次）
    events = node_events(sink, run_id)
    assert len(events) == 6
    interrupted = events[-1]
    assert interrupted["node"] == "approval_gate"
    assert interrupted["data"]["outcome"] == "interrupted"
    assert interrupted["level"] == "info"
    assert interrupted["data"]["error"] is None
    assert interrupted["data"]["output_digest"] is None
    assert (await trace_row(SHOP_ID, run_id))["step_count"] == 6

    resumed = await resume_agent(
        result.thread_id,
        decisions_for([pending.action_id]),  # type: ignore[arg-type]
        expected_interrupt_id=pending.interrupt_id,
    )
    assert resumed.status == RunStatus.DONE.value
    assert resumed.applied[0].status == "succeeded"
    assert resumed.effect_report is not None

    events = node_events(sink, run_id)
    assert [row["node"] for row in events] == [
        "collect",
        "compute_metrics",
        "detect_anomalies",
        "explain",
        "propose_actions",
        "approval_gate",  # 第一次：interrupt 挂起
        "approval_gate",  # 第二次：恢复后从节点开头重放
        "execute",
        "record_effect",
        "finalize",
    ]
    assert (await trace_row(SHOP_ID, run_id))["step_count"] == 10
    assert len(events) == 10

    # 执行真的发生了一次，且参数是审批后的生效参数
    assert fake_client.price_updates == [
        {"product_id": "9001", "sku_id": "3400000000000000002", "price_cents": 9603}
    ]
    logs = action_log_rows(run_id)
    assert len(logs) == 1
    assert logs[0]["status"] == "succeeded"
    # 效果窗口只登记不等待：T+0/1/3/7 四行 scheduled（§5.9.1）
    windows = effect_rows(run_id)
    assert [row["offset_days"] for row in windows] == [0, 1, 3, 7]
    assert {row["status"] for row in windows} == {"scheduled"}
    assert all(row["due_at"] >= FIXED_NOW.replace(tzinfo=None) for row in windows)

    # 审批行被裁决并回填了 interrupt_id（§5.8 的时序）
    rows = approval_rows(result.thread_id)
    assert rows[0]["status"] == "approved"
    assert rows[0]["verdict"] == "approve"
    assert rows[0]["interrupt_id"] == pending.interrupt_id
    assert pending.interrupt_id  # 入口必须把 interrupt_id 回填出去，否则审批台无法提交


async def test_graph_has_expected_nodes_and_retry_policy(runtime: Any) -> None:
    """拓扑与节点级策略是"能追问"的部分：人、超时、重试都挂在图上而不是散在代码里。"""
    from app.agent.graph import build_graph
    from app.agent.nodes import is_retryable_doudian_error

    graph = build_graph()
    nodes = set(graph.get_graph().nodes)
    assert nodes == {
        "__start__",
        "__end__",
        "collect",
        "compute_metrics",
        "detect_anomalies",
        "explain",
        "propose_actions",
        "approval_gate",
        "execute",
        "record_effect",
        "finalize",
    }
    # 采集与执行两个"外部世界"节点必须带重试与超时（重试只对可重试的抖店错误生效）
    for name in ("collect", "execute"):
        retry, timeout = _node_policy(graph, name)
        assert retry is not None, f"{name} 缺少节点级 RetryPolicy"
        assert retry.retry_on is is_retryable_doudian_error, (
            f"{name} 的重试判据必须是 is_retryable_doudian_error（§9.1/§9.2：不可重试错误不许重试）"
        )
        assert timeout is not None, f"{name} 缺少节点级 TimeoutPolicy"
    # 纯计算/收口节点不该有重试预算：它们没有"外部世界"，重试只会重复同一个确定性错误
    for name in ("compute_metrics", "finalize"):
        retry, _ = _node_policy(graph, name)
        assert retry is None, f"{name} 不该有 RetryPolicy"


def _node_policy(graph: Any, name: str) -> tuple[Any, Any]:
    """取编译后节点名的 `(retry_policy, timeout)`。

    从**编译产物**取而不是从 `StateGraph.builder.nodes` 的 `StateNodeSpec` 取：

    - `StateNodeSpec` 没有 `.name` 属性（节点名是它所在 dict 的 key），把它当契约用等于
      把测试绑死在某个版本的内部数据类上；
    - 真正被执行的是编译出来的节点，它携带的策略才是"运行时确实生效的那一份"。

    节点名来自 `graph.nodes` / `graph.get_graph().nodes` 这两个按名字索引的映射（同一份
    节点集合）；不同 langgraph 版本可能把策略存成单值或元组，这里统一归一化成单值。
    """
    node = graph.nodes[name]
    policy = getattr(node, "retry_policy", None)
    if isinstance(policy, tuple):
        policy = policy[0] if policy else None
    return policy, getattr(node, "timeout", None)


async def test_fake_client_matches_protocol(fake_client: FakeDoudianClient) -> None:
    """夹具不能靠"长得像"：它必须覆盖节点真正调用的那 5 个上游方法。"""
    for name in ("get_shop_info", "list_products", "search_orders", "edit_price", "update_stock"):
        assert callable(getattr(fake_client, name))
        assert hasattr(DoudianClient, name)

