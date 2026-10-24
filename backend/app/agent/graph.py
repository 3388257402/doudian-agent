"""LangGraph 状态机的组装与四个公开入口（CONTRACT §4.1 / §4.2 / §4.4 / §4.6）。

## 图拓扑（§4.2 冻结）

```
START → collect ──(ok)──> compute_metrics → detect_anomalies → propose_actions
           │                                                        │
           │                                          (无提议)      ├──> finalize → END
           │                                                        └──> approval_gate ← interrupt()
           └──(fail)──> finalize → END                                     │
                                                            (恢复后) → execute → record_effect → finalize
```

`approval_gate` 只有一条入边和一条出边：它就是"等人"这件事本身，不靠边上的条件判断
要不要等人——要不要进审批由 `propose_actions` 之后的条件边决定（无提议 = 不进审批）。

## 为什么 checkpointer 不是可选项

HITL 的价值是"人可以几小时后再决定"。checkpoint 落内存等于把审批窗口限制在"进程不重启"
这个事实上不可控的区间，所以 `app/` 下禁止 `MemorySaver`/`InMemorySaver`（§5.7）；
`resolve_checkpointer()` 在没人注入时也会**落盘开一个**，而不是退回内存。

## 状态与返回值的分工

`AgentState.status` 在 `approval_gate` 挂起时会**落后一步**：`interrupt()` 靠抛异常挂起，
节点的返回值（含 `status=awaiting_approval`）会被丢弃，所以那次状态迁移只能直接写
`agent_traces`（§4.2 的迁移表本来就是按 run 说的）。`run_agent` 因此用
`snapshot.interrupts` 判定 `awaiting_approval`，而不是读 state 里的那一份。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, RetryPolicy, TimeoutPolicy

from app.agent import nodes
from app.agent.checkpoint import resolve_checkpointer
from app.agent.state import (
    AgentState,
    AppliedDecision,
    Decision,
    PendingApproval,
    ResumeResult,
    RunResult,
    thread_id_for,
    utc_iso,
    utcnow,
)
from app.agent.tracing import get_run_trace, insert_run_trace
from app.config import Settings, get_settings
from app.enums import ActionLogStatus, ApprovalStatus, RunStatus
from app.errors import (
    ApprovalNotFound,
    NoPendingInterrupt,
    RunInProgress,
    RunStateConflict,
    ThreadNotFound,
)
from app.observability.logging import new_trace_id

__all__ = [
    "build_graph",
    "list_pending_approvals",
    "resume_agent",
    "run_agent",
]

#: run 正在跑（同 `(shop_id, run_id)` 再来一次 = 409，§4.6）
_ACTIVE_STATUSES = frozenset(
    {
        RunStatus.RUNNING.value,
        RunStatus.AWAITING_APPROVAL.value,
        RunStatus.EXECUTING.value,
    }
)

#: 需要审批的终态之外的状态：这些状态下 `resume_agent` 才有意义
_RESUMABLE_STATUSES = frozenset({RunStatus.AWAITING_APPROVAL.value, RunStatus.EXECUTING.value})


# ======================================================================================
# 图组装
# ======================================================================================


def _collect_retry_policy(settings: Settings) -> RetryPolicy:
    """采集节点的重试预算（§9.2）：只对 `retryable` 的抖店错误重试。

    `max_attempts` 含首次，退避 `backoff_base_s * 2**n`（上限 `backoff_max_s`）+ 抖动。
    数字全部来自配置而不是写死：这个预算要和 `DoudianClient` 的预算一起调，写死两处必然走散。
    """
    rate = settings.doudian.rate_limit
    return RetryPolicy(
        max_attempts=int(rate.max_attempts),
        initial_interval=float(rate.backoff_base_s),
        backoff_factor=2.0,
        max_interval=float(rate.backoff_max_s),
        jitter=True,
        retry_on=nodes.is_retryable_doudian_error,
    )


def _execute_retry_policy(settings: Settings) -> RetryPolicy:
    """执行节点的重试预算：同上，但**只有**可重试错误才有第二次机会。

    `ambiguous`（mutating 调用遇到超时/连接/5xx）不在可重试集合里（ADR-011）：
    抖店没有服务端幂等键，重发等于赌"上一次没生效"。它走 `status="unknown"` + 对账（§9.3）。
    """
    return _collect_retry_policy(settings)


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    """构建并编译状态机。`checkpointer=None` 时不启用持久化（仅限内部工具/单测）。"""
    settings = get_settings()
    retry = _collect_retry_policy(settings)
    timeout = TimeoutPolicy(run_timeout=timedelta(seconds=int(settings.app.request_timeout_s)))

    builder: StateGraph = StateGraph(AgentState)
    builder.add_node(
        "collect", nodes.collect, retry_policy=retry, timeout=timeout
    )
    builder.add_node("compute_metrics", nodes.compute_metrics_node)
    builder.add_node("detect_anomalies", nodes.detect_anomalies_node)
    builder.add_node("propose_actions", nodes.propose_actions_node)
    builder.add_node("approval_gate", nodes.approval_gate)
    builder.add_node(
        "execute", nodes.execute, retry_policy=_execute_retry_policy(settings), timeout=timeout
    )
    builder.add_node("record_effect", nodes.record_effect)
    builder.add_node("finalize", nodes.finalize)

    builder.add_edge(START, "collect")
    builder.add_conditional_edges(
        "collect",
        nodes.route_after_collect,
        {"compute_metrics": "compute_metrics", "finalize": "finalize"},
    )
    builder.add_edge("compute_metrics", "detect_anomalies")
    builder.add_edge("detect_anomalies", "propose_actions")
    builder.add_conditional_edges(
        "propose_actions",
        nodes.route_after_propose_actions,
        {"approval_gate": "approval_gate", "finalize": "finalize"},
    )
    builder.add_edge("approval_gate", "execute")
    builder.add_edge("execute", "record_effect")
    builder.add_edge("record_effect", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


def _config(thread_id: str, settings: Settings, **configurable: Any) -> dict[str, Any]:
    """统一的运行配置：`thread_id` 必须给（checkpointer 靠它认线程）+ 递归上限。"""
    payload: dict[str, Any] = {"thread_id": thread_id}
    payload.update(configurable)
    return {
        "configurable": payload,
        "recursion_limit": int(settings.agent.recursion_limit),
    }


def _status_of(snapshot: Any) -> str:
    """以快照为准判定 run 状态：挂起中优先（§4.2 的 `awaiting_approval`）。"""
    if getattr(snapshot, "interrupts", None):
        return RunStatus.AWAITING_APPROVAL.value
    values = getattr(snapshot, "values", None) or {}
    return str(values.get("status") or RunStatus.RUNNING.value)


def _initial_state(*, shop_id: str, run_id: str, thread_id: str, trace_id: str,
                   trigger: str, started_at: Any) -> AgentState:
    """§3.2 全字段初始值：一个字段都不省（缺字段的 state 会在节点里变成 KeyError）。"""
    return AgentState(
        run_id=run_id,
        trace_id=trace_id,
        shop_id=shop_id,
        thread_id=thread_id,
        trigger=trigger,
        started_at=started_at,
        status=RunStatus.RUNNING.value,
        iteration=0,
        raw=None,
        metrics=None,
        history=[],
        anomalies=[],
        pending_actions=[],
        approved_actions=[],
        rejected_actions=[],
        approval=None,
        execution_results=[],
        effect_report=None,
        errors=[],
        audit=[],
    )


async def _pending_for_run(run_id: str) -> list[PendingApproval]:
    from app.services import approvals

    with _session() as session:
        return await approvals.list_pending_for_run(session=session, run_id=run_id)


def _session() -> Any:
    """节点/入口共用的事务上下文（同步 Session + async 仓储函数，见 nodes 模块 docstring）。"""
    from app.db import session_scope

    return session_scope()


# ======================================================================================
# 入口 1：run_agent（§4.6 的持久化顺序）
# ======================================================================================


async def run_agent(
    shop_id: str,
    run_id: str,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    trigger: str = "manual",
) -> RunResult:
    """跑一次完整 run。需要审批时返回 `status="awaiting_approval"`（**不是** `done`）。

    幂等（§4.6）：`(shop_id, run_id)` 已在跑 → `RunInProgress`(409)；已终态 → 直接回放已存结果。
    """
    thread_id = thread_id_for(shop_id, run_id)
    settings = get_settings()

    existing = await get_run_trace(shop_id=shop_id, run_id=run_id)
    if existing is not None:
        status = str(existing["status"])
        if status in _ACTIVE_STATUSES:
            raise RunInProgress(
                f"run 正在执行中：{thread_id}",
                details={"thread_id": thread_id, "status": status},
            )
        # 终态 → 回放（不重新跑图，也不再写一行 agent_traces）
        async with resolve_checkpointer(checkpointer, settings=settings) as saver:
            graph = build_graph(saver)
            config = _config(thread_id, settings)
            snapshot = await graph.aget_state(config)
            return RunResult(
                run_id=run_id,
                thread_id=thread_id,
                trace_id=str(existing["trace_id"]),
                status=status,
                pending_approvals=[],
                effect_report=_effect_report_of(snapshot),
                state=_values_of(snapshot),
            )

    trace_id = new_trace_id()
    started_at = utcnow()
    await insert_run_trace(
        run_id=run_id,
        trace_id=trace_id,
        shop_id=shop_id,
        thread_id=thread_id,
        trigger=trigger,
        status=RunStatus.RUNNING.value,
        started_at=started_at,
    )
    _ensure_log_sink(settings)

    async with resolve_checkpointer(checkpointer, settings=settings) as saver:
        graph = build_graph(saver)
        config = _config(thread_id, settings)
        initial = _initial_state(
            shop_id=shop_id,
            run_id=run_id,
            thread_id=thread_id,
            trace_id=trace_id,
            trigger=trigger,
            started_at=utc_iso(started_at),
        )
        try:
            await graph.ainvoke(initial, config=config)
        except Exception as exc:  # noqa: BLE001 - 兜住后**记失败再抛**，绝不吞
            await _mark_failed(shop_id=shop_id, run_id=run_id, exc=exc)
            raise
        snapshot = await graph.aget_state(config)

    status = _status_of(snapshot)
    if status == RunStatus.AWAITING_APPROVAL.value:
        await _backfill_interrupt_id(snapshot)
    return RunResult(
        run_id=run_id,
        thread_id=thread_id,
        trace_id=trace_id,
        status=status,
        pending_approvals=(
            await _pending_for_run(run_id) if status == RunStatus.AWAITING_APPROVAL.value else []
        ),
        effect_report=_effect_report_of(snapshot),
        state=_values_of(snapshot),
    )


async def _backfill_interrupt_id(snapshot: Any) -> None:
    """把当前挂起的 `interrupt_id` 回填到 `approval_request`（`WHERE interrupt_id=''`）。

    为什么入口也要回填（§5.8 只写了 `resume_agent` 回填）：`POST /api/approve` 要求请求体带
    `interrupt_id`（§7.4），而 `GET /api/alerts` 与审批台只能从库里读到它。不回填 =
    审批台手里的 id 永远是空串 = 审批按钮永远 404。回填条件是幂等的（`AND interrupt_id=''`），
    多回填一次不会覆盖任何值。
    """
    from app.services import approvals

    interrupts = tuple(getattr(snapshot, "interrupts", ()) or ())
    if not interrupts:
        return
    values = _values_of(snapshot) or {}
    action_ids = [str(item["action_id"]) for item in values.get("pending_actions") or []]
    if not action_ids:
        return
    interrupt_id = str(interrupts[0].id)
    with _session() as session:
        await approvals.backfill_interrupt_id(
            session=session, action_ids=action_ids, interrupt_id=interrupt_id
        )


def _ensure_log_sink(settings: Settings) -> None:
    """没人装配 JSONL 汇时按 settings 装配一个：节点级明细的事实源不能是空的。"""
    from app.agent.adapters import get_log_sink, refresh_log_sink

    if get_log_sink() is None:
        refresh_log_sink(settings)


async def _mark_failed(*, shop_id: str, run_id: str, exc: BaseException) -> None:
    """节点异常逃出图之后：把 run 标成 failed 并留错误（§4.3 规则 4 的"兜住"）。

    刻意**不**尝试在异常之后补跑 `finalize` 节点：那时图已经不在可执行状态（LangGraph
    的 `Pregel` 任务已失败），硬调会在 trace 里造出一条"没发生过的 finalize"。失败信息由
    `agent_traces.error` + JSONL 里的 `node.trace(outcome="error")` 两处承载。
    """
    from app.agent.tracing import update_run_trace

    await update_run_trace(
        shop_id=shop_id,
        run_id=run_id,
        status=RunStatus.FAILED.value,
        finished=True,
        error=f"{type(exc).__name__}: {exc}",
    )


def _values_of(snapshot: Any) -> AgentState | None:
    values = getattr(snapshot, "values", None)
    # 空的 values = 没有 checkpoint（thread 不存在），必须回 None 而不是 {}：
    # `{}` 会让"线程不存在"看起来像一个刚起步的空 run。
    return values if isinstance(values, dict) and values else None


def _warn_interrupt_not_checked(*, thread_id: str, interrupt_id: str) -> None:
    """`expected_interrupt_id=None` = 放弃了"审批的是不是当前这次中断"的闸门。

    不抛异常（这是契约允许的调用形状），但必须留痕：静默放行等于把这条唯一的防重放闸门
    交给调用方的记性，日志是它唯一会被发现的地方。
    """
    from app.agent.adapters import require_log_sink

    require_log_sink().write(
        "approval.interrupt_unchecked",
        level="warn",
        message="resume_agent 未传 expected_interrupt_id：本次恢复跳过了 interrupt_id 校验",
        data={"thread_id": thread_id, "current_interrupt_id": interrupt_id},
    )


def _effect_report_of(snapshot: Any) -> Any:
    values = _values_of(snapshot) or {}
    report = values.get("effect_report")
    return report if isinstance(report, dict) else None


# ======================================================================================
# 入口 2：resume_agent（§4.4 冻结顺序）
# ======================================================================================


async def resume_agent(
    thread_id: str,
    decisions: Sequence[Decision],
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    expected_interrupt_id: str | None = None,
) -> ResumeResult:
    """用人工裁决恢复挂起的 run（§4.4）。

    `expected_interrupt_id` 是 API 层的 `POST /api/approve` 传进来的（请求体里带
    `interrupt_id`）：它必须等于当前挂起的那个 id，否则 404 `APPROVAL_NOT_FOUND`——
    "拿旧审批页面点第二次"必须被挡住，而不是把裁决喂给新的挂起点。

    **传 None 等于放弃这道闸门**（不是"校验通过"）：会放行并写一条 `warn` 级日志。API 层
    必须总是传它；不传意味着"审批的是不是当前这次中断"交给了运气。
    """
    from app.services import approvals

    settings = get_settings()
    async with resolve_checkpointer(checkpointer, settings=settings) as saver:
        graph = build_graph(saver)
        config = _config(thread_id, settings)
        snapshot = await graph.aget_state(config)
        values = _values_of(snapshot)
        if not values:
            # thread 查不到 checkpoint：404（§4.4）
            raise ThreadNotFound(f"找不到 thread 的检查点：{thread_id}",
                                 details={"thread_id": thread_id})
        interrupts = tuple(getattr(snapshot, "interrupts", ()) or ())
        if not interrupts:
            raise RunStateConflict(
                f"thread 存在但没有挂起的中断：{thread_id}",
                details={"thread_id": thread_id, "status": _status_of(snapshot)},
            )
        interrupt_id = str(interrupts[0].id)
        if expected_interrupt_id is None:
            _warn_interrupt_not_checked(thread_id=thread_id, interrupt_id=interrupt_id)
        elif expected_interrupt_id != interrupt_id:
            raise ApprovalNotFound(
                "提交的 interrupt_id 不是当前挂起的中断（可能已处理或已过期）",
                details={"thread_id": thread_id, "current_interrupt_id": interrupt_id},
            )
        pending = list(values.get("pending_actions") or [])
        pending_ids = [str(item["action_id"]) for item in pending]

        # 第二道闸：API 层已校验过一次，这里再校验一次（防"绕过 API 直接调 resume_agent"）。
        payload = nodes.validate_resume_payload(
            {"type": "approval_decisions", "decisions": list(decisions)}, expected=pending
        )

        shop_id = str(values.get("shop_id"))
        run_id = str(values.get("run_id"))
        with _session() as session:
            await approvals.assert_all_pending(
                session=session, thread_id=thread_id, action_ids=pending_ids
            )
            await approvals.backfill_interrupt_id(
                session=session, action_ids=pending_ids, interrupt_id=interrupt_id
            )

        # §4.2：校验通过后 run 进入 executing
        from app.agent.tracing import update_run_trace

        await update_run_trace(
            shop_id=shop_id, run_id=run_id, status=RunStatus.EXECUTING.value
        )

        try:
            await graph.ainvoke(Command(resume={interrupt_id: payload}), config=config)
        except Exception as exc:  # noqa: BLE001 - 记失败再抛
            await _mark_failed(shop_id=shop_id, run_id=run_id, exc=exc)
            raise
        snapshot = await graph.aget_state(config)

    values = _values_of(snapshot) or values
    status = _status_of(snapshot)
    return ResumeResult(
        run_id=str(values.get("run_id")),
        thread_id=thread_id,
        trace_id=str(values.get("trace_id")),
        status=status,
        applied=_applied_of(values, decisions),
        pending_approvals=(
            await _pending_for_run(str(values.get("run_id")))
            if status == RunStatus.AWAITING_APPROVAL.value
            else []
        ),
        effect_report=_effect_report_of(snapshot),
        state=values,
    )


def _applied_of(values: Mapping[str, Any], decisions: Sequence[Decision]) -> list[AppliedDecision]:
    """`applied[]`：拒绝的动作不写 `action_log`，其 status 固定 `"rejected"`（§7.4）。"""
    results = {
        str(item["action_id"]): item for item in values.get("execution_results") or []
    }
    verdicts = {str(item["action_id"]): str(item["verdict"]) for item in decisions}
    applied: list[AppliedDecision] = []
    for action_id, verdict in verdicts.items():
        if verdict == "reject":
            applied.append(
                AppliedDecision(
                    action_id=action_id,
                    verdict=verdict,
                    status="rejected",
                    action_log_id=None,
                    error=None,
                )
            )
            continue
        result = results.get(action_id)
        if result is None:
            applied.append(
                AppliedDecision(
                    action_id=action_id,
                    verdict=verdict,
                    status=ActionLogStatus.PENDING.value,
                    action_log_id=None,
                    error="未执行（本轮没有产生执行回执）",
                )
            )
            continue
        status = str(result["status"])
        applied.append(
            AppliedDecision(
                action_id=action_id,
                verdict=verdict,
                status=status,
                # `action_log_id` 就等于 idempotency_key（§6.1），没有第二套 ID
                action_log_id=None
                if status
                in {ActionLogStatus.SKIPPED_DUPLICATE.value, "rejected"}
                else str(result["idempotency_key"]),
                error=result.get("error"),
            )
        )
    return applied


# ======================================================================================
# 入口 3：list_pending_approvals
# ======================================================================================


async def list_pending_approvals(
    *, shop_id: str | None = None, limit: int = 50, before_id: int | None = None
) -> list[PendingApproval]:
    """所有 `pending` 的审批提议（审批台首屏；`/api/alerts` 复用同一仓储）。"""
    from app.services import approvals

    with _session() as session:
        return await approvals.list_by_status(
            session=session,
            status=ApprovalStatus.PENDING,
            shop_id=shop_id,
            limit=limit,
            before_id=before_id,
        )
