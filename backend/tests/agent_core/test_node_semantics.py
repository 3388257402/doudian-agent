"""节点语义：重试预算、审批重放幂等、幂等键落在"参数定稿之后"。

契约依据：§9.1 / §9.2（重试只对可重试分类）、ADR-010（`approval_gate` 会被重放）、
ADR-006（幂等键必须在审批之后按生效参数生成）、§5.9.1（图内不等待 T+N）。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.agent import resume_agent, run_agent
from app.agent.state import Decision
from app.enums import RunStatus
from app.errors import ApprovalValidationError
from app.observability.logging import JsonlLogSink
from app.services.actions import idempotency_key
from app.services.approvals import upsert_proposals
from app.services.errors import (
    DoudianAmbiguousOutcomeError,
    DoudianBusinessError,
    DoudianUpstreamError,
)

from .conftest import (
    SHOP_ID,
    FakeDoudianClient,
    action_log_rows,
    approval_rows,
    decisions_for,
    effect_rows,
    node_events,
)


# ======================================================================================
# §9.2 重试预算：只对可重试错误重试
# ======================================================================================


async def test_retryable_collect_error_is_retried(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """5xx（retryable）两次后成功：节点级 RetryPolicy 必须真的重试，run 最终成功。"""
    fake_client.fail("order.searchList", DoudianUpstreamError(
        "upstream 503", category="server_5xx", retryable=True
    ), times=2)

    result = await run_agent(SHOP_ID, "run_retry_ok")

    assert result.status == RunStatus.AWAITING_APPROVAL.value
    assert fake_client.calls.count("order.searchList") == 3
    # 三次采集尝试 = 三条 node.trace（前两条 outcome=error，第三条 ok）
    events = [row for row in node_events(sink, "run_retry_ok") if row["node"] == "collect"]
    assert [row["data"]["outcome"] for row in events] == ["error", "error", "ok"]
    assert [row["level"] for row in events] == ["error", "error", "info"]


async def test_non_retryable_collect_error_is_not_retried(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """business 错误（retryable=False）一次就够：重试一个"参数写错了"只会重复四遍。"""
    fake_client.fail("order.searchList", DoudianBusinessError(
        "err_no=10001 参数非法", category="business", retryable=False
    ))

    result = await run_agent(SHOP_ID, "run_retry_no")

    assert result.status == RunStatus.FAILED.value
    assert fake_client.calls.count("order.searchList") == 1
    events = node_events(sink, "run_retry_no")
    # 采集失败 → 直接 finalize（§4.2 的 collect --(fail)--> finalize）
    assert [row["node"] for row in events] == ["collect", "finalize"]
    # collect 是"写 failed 后正常返回"（走条件边），不是抛异常；traced() 必须认这条显式失败信号，
    # 否则一次采集硬失败在日志页上会是 outcome="ok" 的绿灯（§4.7 规则 5 + §4.2 的 fail 边）。
    assert events[0]["data"]["outcome"] == "error"
    assert events[0]["level"] == "error"
    assert events[0]["data"]["output_digest"] is None
    failure = events[0]["data"]["error"]
    assert failure["type"] == "NodeDeclaredFailure"  # 没有异常类可写时的固定标记
    assert "订单数据缺失" in failure["message"]  # 原因取 collect 自己记的那条 AgentError
    # 收口不算失败：finalize 在**已 failed** 的 run 上复述 status=failed 是"run 的结果"，
    # 不是"这次执行失败"——判据是状态**迁移**，否则每个失败 run 都会出现第二个红灯。
    assert events[1]["data"]["outcome"] == "ok"
    assert events[1]["level"] == "info"


async def test_retryable_execute_error_is_retried_and_skips_duplicate(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """执行遇限流重试：第一次失败的记录保留为 failed，重放后成功（不是改两次价）。"""
    result = await run_agent(SHOP_ID, "run_exec_retry")
    pending = result.pending_approvals[0]
    fake_client.fail("product.editPrice", DoudianUpstreamError(
        "upstream 502", category="server_5xx", retryable=True
    ))

    resumed = await resume_agent(result.thread_id, decisions_for([pending.action_id]))  # type: ignore[arg-type]

    assert resumed.status == RunStatus.DONE.value
    assert len(fake_client.price_updates) == 1  # 上游只被真正改了一次
    logs = action_log_rows("run_exec_retry")
    assert len(logs) == 1  # 幂等键去重：同一动作只有一行
    assert logs[0]["status"] == "succeeded"
    assert logs[0]["attempt"] == 2


async def test_ambiguous_execute_result_is_unknown_and_not_retried(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """mutating + 超时 = ambiguous：写 unknown、**不重发**（ADR-011 / §9.3）。"""
    result = await run_agent(SHOP_ID, "run_ambiguous")
    pending = result.pending_approvals[0]
    fake_client.fail("product.editPrice", DoudianAmbiguousOutcomeError(
        "改价请求超时，结果不确定", category="ambiguous", retryable=False
    ))

    resumed = await resume_agent(result.thread_id, decisions_for([pending.action_id]))  # type: ignore[arg-type]

    assert resumed.status == RunStatus.DONE.value
    assert len(fake_client.price_updates) == 0
    logs = action_log_rows("run_ambiguous")
    assert logs[0]["status"] == "unknown"
    assert logs[0]["error_category"] == "ambiguous"
    assert resumed.applied[0].status == "unknown"
    # ambiguous 不重试：上游只被调用了一次
    assert fake_client.calls.count("product.editPrice") == 1
    # 也没有为它登记效果窗口（成功执行才登记）
    assert effect_rows("run_ambiguous") == []


# ======================================================================================
# ADR-010 审批重放：insert-if-absent，绝不覆盖已裁决行
# ======================================================================================


async def test_approval_upsert_never_resets_decided_row(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`approval_gate` 重放过一次之后，再 upsert 同一批提议也不能把 approved 改回 pending。"""
    result = await run_agent(SHOP_ID, "run_replay")
    pending = result.pending_approvals[0]
    await resume_agent(result.thread_id, decisions_for([pending.action_id]))  # type: ignore[arg-type]

    rows_before = approval_rows(result.thread_id)
    assert rows_before[0]["status"] == "approved"

    from app.db import session_scope

    proposals = [dict(item) for item in (result.state or {}).get("pending_actions") or []]
    with session_scope() as session:
        await upsert_proposals(
            session=session,
            run_id="run_replay",
            shop_id=SHOP_ID,
            thread_id=result.thread_id,
            proposals=proposals,  # type: ignore[arg-type]
        )

    rows_after = approval_rows(result.thread_id)
    assert len(rows_after) == len(rows_before) == 1  # 行数不变
    assert rows_after[0]["status"] == "approved"  # 状态不被回写
    assert rows_after[0]["params"] == rows_before[0]["params"]


# ======================================================================================
# ADR-006 幂等键：落在"参数已经定稿"之后
# ======================================================================================


async def test_idempotency_key_changes_when_params_edited(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """9003 与 9600 是两个不同的键：否则"改价 A"会被"改价 B"错误去重。"""
    result = await run_agent(SHOP_ID, "run_key_edit")
    pending = result.pending_approvals[0]
    edited = [
        {
            "action_id": pending.action_id,
            "verdict": "edit",
            "edited_params": {"price_cents": 9600},
            "operator": "ops_zhang",
            "reason": "降幅收敛",
            "decided_at": "2026-10-23T12:05:00Z",
        }
    ]
    await resume_agent(result.thread_id, edited)  # type: ignore[arg-type]

    logs = action_log_rows("run_key_edit")
    expected_edited = idempotency_key(
        shop_id=SHOP_ID,
        action_type="price_update",
        target=pending.target,
        params={"price_cents": 9600},
    )
    expected_original = idempotency_key(
        shop_id=SHOP_ID,
        action_type="price_update",
        target=pending.target,
        params={"price_cents": 9603},
    )
    assert logs[0]["idempotency_key"] == expected_edited
    assert expected_edited != expected_original
    assert fake_client.price_updates[0]["price_cents"] == 9600
    # 审批行里记的是**生效参数**（证据链要能说明"改过"）
    assert approval_rows(result.thread_id)[0]["params"] == {"price_cents": 9600}


async def test_same_params_in_new_run_is_skipped_duplicate(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """幂等键**不含 run_id**：同一店铺同一参数第二次执行必须是 skipped_duplicate。"""
    first = await run_agent(SHOP_ID, "run_first")
    await resume_agent(first.thread_id, decisions_for([first.pending_approvals[0].action_id]))  # type: ignore[arg-type]
    assert len(fake_client.price_updates) == 1

    second = await run_agent(SHOP_ID, "run_second")
    assert second.pending_approvals[0].params == {"price_cents": 9603}
    resumed = await resume_agent(
        second.thread_id, decisions_for([second.pending_approvals[0].action_id])  # type: ignore[arg-type]
    )

    assert resumed.status == RunStatus.DONE.value
    assert resumed.applied[0].status == "skipped_duplicate"
    assert len(fake_client.price_updates) == 1  # 上游没有被改第二次
    assert action_log_rows("run_second") == []  # 没为它新开一行
    assert effect_rows("run_second") == []  # 也没有效果窗口（没有新的成功执行）


# ======================================================================================
# §5.9.1 图内不等待
# ======================================================================================


async def test_record_effect_does_not_wait(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """整个图执行（含 T+0/1/3/7 登记）必须在一秒内结束：等待在调度器里，不在图里。"""
    result = await run_agent(SHOP_ID, "run_fast")
    pending = result.pending_approvals[0]
    started = time.perf_counter()
    await resume_agent(result.thread_id, decisions_for([pending.action_id]))  # type: ignore[arg-type]
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"执行路径耗时 {elapsed:.2f}s，说明图里在等待"
    windows = effect_rows("run_fast")
    assert [row["offset_days"] for row in windows] == [0, 1, 3, 7]
    assert {row["status"] for row in windows} == {"scheduled"}
