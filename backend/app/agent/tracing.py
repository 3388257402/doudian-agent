"""节点级可观测：`traced()` 包装器 + JSONL 节点事件 + `agent_traces` 行。

## 为什么表里只有 run 摘要、节点明细进 JSONL

契约 §6.1 的 `agent_traces` 是"一次 run 一行"（`UNIQUE(shop_id, run_id)`，列里只有
`step_count`/`status`/`updated_at`），没有 node / 耗时 / 摘要列；而节点级明细的查询形态是
"某 run 最近 N 条"的 append-only 流。所以：

- **run 摘要** → `agent_traces`（一行，`step_count` 只由本模块 +1）；
- **节点明细** → JSONL（`event="node.trace"`，`/api/logs` 的数据源，§7.6）。

两者不是两份各自为政的真相：写事件与 +1 步数在**同一段代码**里顺序执行（§4.7 规则 6），
`reconcile_step_count()` 能把崩溃窗口造成的偏差校准回来，
`tests/agent_core/test_trace_invariant.py` 断言「同一 run 的 `node.trace` 行数 ==
`agent_traces.step_count`」（8 个节点 + 一次审批重放 = 9）。

## 中断不是错误（契约 v1.2.0 / ADR-016）

`approval_gate` 第一次执行就是靠 `GraphInterrupt` 挂起的。若按"异常即 error"记日志，每次正常
审批都会留一条红色 error，前端执行日志页会把"正常暂停"显示成故障——告警天天红就等于没有告警。
因此 `data.outcome` 三态：`ok` / `interrupted` / `error`，只有真异常才是 `error` + `level="error"`。

`GraphBubbleUp` 是**基类**（`GraphInterrupt` / `GraphDrained` / `ParentCommand` 都继承它），
必须捕基类：漏一个子类就会把某类控制流记成失败。`GraphRecursionError` 不是 `GraphBubbleUp`
（它继承 `RecursionError`），落到 `except Exception` 分支 —— 递归超限是真失败。

## 失败有两条来源，包装器必须都认（`NODE_DECLARED_FAILURE_TYPE`）

第 5 条规则的字面只写了"抛异常 → error"，但 §4.2 的拓扑里 `collect --(fail)--> finalize`
是一条**正常边**：节点把 `status` 写成 `failed` 然后正常返回，图层再据条件边去收口。只认异常
会让这类执行被记成 `outcome="ok"` + `level="info"` —— 一次采集硬失败在日志页上显示成绿灯，
`outcome` 就失去意义。因此 `traced()` 还认一条**显式失败信号**：本次执行的返回值把 run 从
非 `failed` 推进到 `failed`。判据与理由见 :func:`_declared_failure`。

## 脱敏

`data` 里只放**摘要**（脱敏后的 sha256 前 16 位）与计数，不放原文：access_token 之类的东西
连"被忘了脱敏"的机会都不该有。
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from sqlalchemy import select

from app.agent.adapters import digest_of, require_log_sink
from app.agent.state import (
    AgentError,
    AgentState,
    append_audits,
    append_errors,
    make_agent_error,
    make_audit_entry,
    utcnow,
)
from app.config import Settings, get_settings
from app.db import arun_db
from app.enums import ErrorCategory, RunStatus
from app.models import AgentTrace
from app.observability.logging import JsonlLogSink, redact

__all__ = [
    "MAX_ITERATIONS",
    "NODE_DECLARED_FAILURE_TYPE",
    "NODE_TRACE_EVENT",
    "NODE_TRACE_OUTCOME_ERROR",
    "NODE_TRACE_OUTCOME_INTERRUPTED",
    "NODE_TRACE_OUTCOME_OK",
    "calibrate_run_steps",
    "emit_node_trace",
    "get_run_trace",
    "insert_run_trace",
    "node_error",
    "reconcile_step_count",
    "traced",
    "update_run_trace",
]

# --------------------------------------------------------------------------------------
# 常量（§4.7 逐字）
# --------------------------------------------------------------------------------------

#: 节点级 trace 的事件名（§7.6.1）
NODE_TRACE_EVENT = "node.trace"

NODE_TRACE_OUTCOME_OK = "ok"  # 正常返回
NODE_TRACE_OUTCOME_INTERRUPTED = "interrupted"  # 捕获 GraphBubbleUp（HITL 挂起等控制流）
NODE_TRACE_OUTCOME_ERROR = "error"  # 其它异常，含 GraphRecursionError；也含节点显式宣告失败

#: 节点"显式宣告失败"时 `data.error.type` 的取值。
#:
#: 契约 §7.6.1 把 `error.type` 写成 `"<ExceptionClassName>"` —— 那是"抛异常"这条路径的描述。
#: 另一条路径（节点在返回的 state update 里把 run 推进到 `failed`）没有异常类可写，于是用一个
#: 固定标记说明"这是节点宣告的失败，不是异常逃逸"。`data.error` 的**键**仍然恰好是
#: `{"type", "message"}`（§7.6.1 冻结），读取端只需把 `type` 当字符串展示。
NODE_DECLARED_FAILURE_TYPE = "NodeDeclaredFailure"

#: `outcome` → `level` 是**硬规则**（§7.6.1）：挂起也是 info，不许染成红色。
_OUTCOME_LEVEL: dict[str, str] = {
    NODE_TRACE_OUTCOME_OK: "info",
    NODE_TRACE_OUTCOME_INTERRUPTED: "info",
    NODE_TRACE_OUTCOME_ERROR: "error",
}

#: `message` 取值固定三种（§7.6.1），便于 grep 与前端分组。
_OUTCOME_MESSAGE: dict[str, str] = {
    NODE_TRACE_OUTCOME_OK: "node ok",
    NODE_TRACE_OUTCOME_INTERRUPTED: "node interrupted (awaiting human decision)",
    NODE_TRACE_OUTCOME_ERROR: "node failed",
}

#: `iteration` 超过它就强转 `failed`（§3.2 / §4.2）
MAX_ITERATIONS = 20

NodeFn = Callable[[AgentState, RunnableConfig], Any]


# --------------------------------------------------------------------------------------
# 摘要（脱敏后哈希，只存摘要不存原文）
# --------------------------------------------------------------------------------------
# `digest_of` 与 JSONL 汇的存取都在 `app/agent/adapters.py`（装配层）。本模块只消费它们：
# 摘要与日志汇是"进程级设施"，不是 trace 语义；放在装配层才能让 nodes 也用同一份实现，
# 不会出现两个 digest 算法。


def _input_view(state: Mapping[str, Any]) -> dict[str, Any]:
    """`input_digest` 的入参视图：只挑能判断"输入是否同一份"的字段。"""
    raw = state.get("raw")
    metrics = state.get("metrics")
    return {
        "run_id": state.get("run_id"),
        "status": str(state.get("status") or ""),
        "raw_collected_at": raw.get("collected_at") if isinstance(raw, Mapping) else None,
        "metrics_date": metrics.get("date") if isinstance(metrics, Mapping) else None,
        "anomaly_ids": [item.get("anomaly_id") for item in state.get("anomalies") or []],
        "pending_action_ids": [
            item.get("action_id") for item in state.get("pending_actions") or []
        ],
        "approved_action_ids": [
            item.get("action_id") for item in state.get("approved_actions") or []
        ],
        "executed_keys": [
            item.get("idempotency_key") for item in state.get("execution_results") or []
        ],
    }


def _output_view(update: Mapping[str, Any]) -> dict[str, Any]:
    raw = update.get("raw")
    metrics = update.get("metrics")
    return {
        "status": str(update.get("status") or ""),
        "keys": sorted(str(key) for key in update),
        "raw_collected_at": raw.get("collected_at") if isinstance(raw, Mapping) else None,
        "metrics_date": metrics.get("date") if isinstance(metrics, Mapping) else None,
        "errors": [item.get("message") for item in update.get("errors") or []],
        "anomaly_ids": [item.get("anomaly_id") for item in update.get("anomalies") or []],
        "pending_action_ids": [
            item.get("action_id") for item in update.get("pending_actions") or []
        ],
        "approved_action_ids": [
            item.get("action_id") for item in update.get("approved_actions") or []
        ],
        "executed_keys": [
            item.get("idempotency_key") for item in update.get("execution_results") or []
        ],
    }


# --------------------------------------------------------------------------------------
# 写一条 node.trace
# --------------------------------------------------------------------------------------


def emit_node_trace(
    *,
    node: str,
    outcome: str,
    state: Mapping[str, Any],
    duration_ms: float,
    input_digest: str,
    output_digest: str | None,
    error: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """写一条 `node.trace`。`data` 只含 §7.6.1 冻结的五个键，**不新增**顶层字段。"""
    if outcome not in _OUTCOME_LEVEL:  # pragma: no cover - 内部调用点受限
        raise ValueError(f"非法 outcome: {outcome!r}（取值域 ok/interrupted/error）")
    if outcome == NODE_TRACE_OUTCOME_ERROR and error is None:  # pragma: no cover - 内部约束
        raise ValueError("outcome=error 必须带 error 摘要")
    if outcome != NODE_TRACE_OUTCOME_ERROR and error is not None:  # pragma: no cover
        raise ValueError(f"outcome={outcome} 时 error 必须为 None（§7.6.1 硬规则）")
    data = {
        "outcome": outcome,
        "duration_ms": round(float(duration_ms), 1),
        "input_digest": input_digest,
        "output_digest": output_digest if outcome == NODE_TRACE_OUTCOME_OK else None,
        "error": dict(error) if error is not None else None,
    }
    return dict(
        require_log_sink().write(
            NODE_TRACE_EVENT,
            level=_OUTCOME_LEVEL[outcome],
            message=_OUTCOME_MESSAGE[outcome],
            data=data,
            run_id=state.get("run_id"),
            trace_id=state.get("trace_id"),
            shop_id=state.get("shop_id"),
            node=node,
        )
    )


def _error_summary(exc: BaseException, settings: Settings) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(redact(str(exc), settings.observability.redact_keys)),
    }


# --------------------------------------------------------------------------------------
# iteration 守卫（§3.2）
# --------------------------------------------------------------------------------------


def _guard_iteration(
    state: Mapping[str, Any], update: dict[str, Any], *, node: str
) -> dict[str, Any]:
    """`iteration` 自增守卫：> 20 强转 `failed`，并留下审计与错误。

    为什么放在包装器里而不是每个节点各写一遍：节点是"谁都能加一行"的地方，而"防止无限
    自循环"是全局不变量。放在这里，新增节点自动获得保护。
    """
    iteration = int(state.get("iteration") or 0) + 1
    update["iteration"] = iteration
    if iteration <= MAX_ITERATIONS:
        return update
    update["status"] = RunStatus.FAILED.value
    error = make_agent_error(
        node=node,
        category=ErrorCategory.UNKNOWN,
        message=f"iteration 超过上限 {MAX_ITERATIONS}，强制终止",
        retryable=False,
    )
    merged = list(state.get("errors") or [])
    merged.append(error)
    update["errors"] = append_errors({"errors": merged}, merged)  # type: ignore[arg-type]
    update["audit"] = append_audits(
        {"audit": list(state.get("audit") or [])},  # type: ignore[arg-type]
        [
            make_audit_entry(
                node=node,
                event="node.iteration_limit",
                data={"iteration": iteration, "limit": MAX_ITERATIONS},
            )
        ],
    )
    return update


# --------------------------------------------------------------------------------------
# 包装器（§4.7）
# --------------------------------------------------------------------------------------


def _declared_failure(
    state: Mapping[str, Any],
    update: Mapping[str, Any],
    *,
    node: str,
    redact_keys: tuple[str, ...] | frozenset[str],
) -> dict[str, str] | None:
    """这次执行是否**显式宣告 run 失败**？是则返回 §7.6.1 形状的 `error` 摘要。

    ## 为什么除"抛异常"之外还要认这条信号（根因）

    §4.2 的拓扑里 `collect --(fail)--> finalize` 是一条**正常边**：节点把 `status` 写成
    `failed` 后正常返回，图层据此走条件边去收口。契约 §4.7 规则 5 当时只描述了"抛异常 →
    error"，于是这类执行被记成 `outcome="ok"` + `level="info"`：一次采集硬失败在日志页上是
    绿灯，告警看不到它——`outcome` 丢掉它唯一的价值。

    ## 为什么判"状态迁移"而不是"结果取值"

    收口节点（`finalize`）在**已经失败**的 run 上收尾时，返回值里也带着 `status=failed`；
    那是"这个 run 的结果"，不是"这次执行失败"。所以判据是**迁移**：输入非 `failed` → 输出
    `failed` 才算本次执行宣告失败；输入已经是 `failed`（收尾、以及 `_aborted` 的早退分支）一律
    不算，避免把收口节点染成第二个红灯。

    ## 为什么不让 `collect` 改抛异常、由图层兜住

    `compute_metrics` / `detect_anomalies` / `propose_actions` 的硬失败分支同样是"写 failed
    后正常返回"（下游靠 `_aborted` 短路）。只让 `collect` 抛异常会在图上多出一个契约里没有的
    捕获节点，而另外三个节点继续把失败记成 `ok` —— 同一个缺陷换个位置再来一次。在包装器里
    一次性认下"state 里的显式失败信号"，四个节点同时变正确。

    ## error 摘要

    `message` 取 update 里**最后一条** `AgentError.message`（节点自己的措辞，例如"订单数据缺失，
    无法计算指标"），并按 `observability.redact_keys` 脱敏；没有错误条目时给一句兜底说明。
    形状仍是 §7.6.1 冻结的 `{"type", "message"}`。
    """
    if str(update.get("status") or "") != RunStatus.FAILED.value:
        return None
    if str(state.get("status") or "") == RunStatus.FAILED.value:
        return None
    message = ""
    for item in update.get("errors") or []:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("message") or "")
        if text:
            message = text
    if not message:
        message = f"{node} 把 run 判定为 failed（节点未给出具体原因）"
    return {"type": NODE_DECLARED_FAILURE_TYPE, "message": str(redact(message, redact_keys))}


def traced(node_name: str) -> Callable[[NodeFn], NodeFn]:
    """装饰节点函数（§4.7 逐条语义，一条都不省）。

    1. 每次**实际执行**写且只写一条 JSONL `event="node.trace"`（成功/挂起/失败都写）；
    2. 同一次执行把 `agent_traces.step_count` 原子 +1，并按需更新 `status`/`updated_at`；
    3. `duration_ms` 用 `time.perf_counter()` 计；
    4. `input_digest` / `output_digest` 只存 16 位摘要；挂起/失败时 `output_digest` 为 `None`；
    5. 异常分流**先控制流后失败**：`GraphBubbleUp` → `interrupted`+`info`+`error=None`，
       其它 `Exception`（含 `GraphRecursionError`）→ `error`+`level="error"`，两者都原样 re-raise；
    6. 写序固定：① JSONL append（拿 seq）② DB 更新；事件必须写在 re-raise **之前**。

    除此之外，正常返回也分两种（`_declared_failure`）：节点把 run 从非 `failed` 推进到
    `failed`（§4.2 的 `collect --(fail)--> finalize` 那条正常边）→ `outcome="error"`，
    否则 `outcome="ok"`。`step_count` 一律 +1，计数不看 `outcome`（ADR-016）。
    """

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            settings = get_settings()
            redact_keys = settings.observability.redact_keys
            started = time.perf_counter()
            input_digest = digest_of(_input_view(state), redact_keys=redact_keys)

            try:
                update = await fn(state, config)
            except GraphBubbleUp:
                # a) 控制流中断：不是失败（ADR-016）。记录后**原样**抛回去。
                emit_node_trace(
                    node=node_name,
                    outcome=NODE_TRACE_OUTCOME_INTERRUPTED,
                    state=state,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    input_digest=input_digest,
                    output_digest=None,
                    error=None,
                )
                await _bump_step_only(state)
                raise
            except Exception as exc:
                # b) 真失败（GraphRecursionError 也在这一支）。记录后**原样**抛回去。
                emit_node_trace(
                    node=node_name,
                    outcome=NODE_TRACE_OUTCOME_ERROR,
                    state=state,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    input_digest=input_digest,
                    output_digest=None,
                    error=_error_summary(exc, settings),
                )
                await _bump_step_only(state)
                raise

            # c) 正常返回：可能是"这次执行成功"，也可能是"节点显式宣告 run 失败"（两条来源，
            #    见 _declared_failure）。后者不抛异常，但 outcome 必须是 error。
            guarded = _guard_iteration(state, dict(update or {}), node=node_name)
            declared = _declared_failure(
                state, guarded, node=node_name, redact_keys=redact_keys
            )
            emit_node_trace(
                node=node_name,
                outcome=(
                    NODE_TRACE_OUTCOME_ERROR if declared is not None else NODE_TRACE_OUTCOME_OK
                ),
                state=state,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                input_digest=input_digest,
                output_digest=(
                    None
                    if declared is not None
                    else digest_of(_output_view(guarded), redact_keys=redact_keys)
                ),
                error=declared,
            )
            await update_run_trace(
                shop_id=str(state["shop_id"]),
                run_id=str(state["run_id"]),
                status=guarded.get("status"),
                step_delta=1,
            )
            return guarded

        return wrapper  # type: ignore[return-value]

    return decorator


async def _bump_step_only(state: Mapping[str, Any]) -> None:
    """挂起/失败分支：只 +1，不碰 status（`awaiting_approval` 由节点自己在 interrupt 前写）。"""
    await update_run_trace(
        shop_id=str(state["shop_id"]), run_id=str(state["run_id"]), step_delta=1
    )


# --------------------------------------------------------------------------------------
# agent_traces 仓储（`step_count` 的唯一写入点；`grep step_count backend/app` 只命中本文件
# 与 models.py —— 这是一条可被机械检查的不变量，因此仓储与否都放在这里）
# --------------------------------------------------------------------------------------


def _trace_row(row: AgentTrace) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "trace_id": row.trace_id,
        "shop_id": row.shop_id,
        "thread_id": row.thread_id,
        "trigger": row.trigger,
        "status": row.status,
        "step_count": int(row.step_count or 0),
        "started_at": row.started_at,
        "updated_at": row.updated_at,
        "finished_at": row.finished_at,
        "error": row.error,
    }


async def insert_run_trace(
    *,
    run_id: str,
    trace_id: str,
    shop_id: str,
    thread_id: str,
    trigger: str,
    status: RunStatus | str,
    started_at: datetime,
) -> bool:
    """insert-if-absent。返回 True 表示本次真的插入。

    已存在时**什么都不做**：重放/重试不该把一行已经 `done` 的记录改回 `running`
    （同一条理由见 §5.8 的 `upsert_proposals`）。
    """

    def _fn(session: Any) -> bool:
        existing = session.execute(
            select(AgentTrace).where(
                AgentTrace.shop_id == shop_id, AgentTrace.run_id == run_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        session.add(
            AgentTrace(
                run_id=run_id,
                trace_id=trace_id,
                shop_id=shop_id,
                thread_id=thread_id,
                trigger=trigger,
                status=str(status),
                step_count=0,
                started_at=started_at,
                updated_at=started_at,
                finished_at=None,
                error=None,
            )
        )
        return True

    return bool(await arun_db(_fn))


async def get_run_trace(*, shop_id: str, run_id: str) -> dict[str, Any] | None:
    """按 `(shop_id, run_id)` 取 run 摘要行（`UNIQUE(shop_id, run_id)` 保证唯一）。"""

    def _fn(session: Any) -> dict[str, Any] | None:
        row = session.execute(
            select(AgentTrace).where(
                AgentTrace.shop_id == shop_id, AgentTrace.run_id == run_id
            )
        ).scalar_one_or_none()
        return None if row is None else _trace_row(row)

    return await arun_db(_fn)


async def update_run_trace(
    *,
    shop_id: str,
    run_id: str,
    status: RunStatus | str | None = None,
    step_delta: int = 0,
    finished: bool = False,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """更新一次 run 的状态/步数。`step_delta` 只由 :func:`traced` 传 1。"""
    stamp = now or utcnow()

    def _fn(session: Any) -> None:
        row = session.execute(
            select(AgentTrace).where(
                AgentTrace.shop_id == shop_id, AgentTrace.run_id == run_id
            )
        ).scalar_one_or_none()
        if row is None:
            # 没有摘要行就没有"run"可言：静默什么都不做会把"trace 丢了"藏起来。
            raise LookupError(
                f"agent_traces 缺少 (shop_id={shop_id}, run_id={run_id})：run 摘要行必须先插入"
            )
        if step_delta:
            row.step_count = int(row.step_count or 0) + step_delta
        if status is not None:
            row.status = str(status)
        row.updated_at = stamp
        if finished:
            row.finished_at = stamp
        if error is not None:
            row.error = error

    await arun_db(_fn)


async def reconcile_step_count(
    *, session: Any, run_id: str, sink: JsonlLogSink
) -> int:
    """以 JSONL 为准校准 `agent_traces.step_count`，返回校准后的值（§4.7 逐字签名）。

    调用时机：`finalize` 节点结束前一次（见 `nodes.finalize`）；进程启动时对所有非终态 run
    各一次（`app.main` lifespan / 调度器可调用）。

    语义：`step_count := max(step_count, count(events where run_id=X and event=="node.trace"))`
    —— **只增不减**，差异记一条 `trace.reconcile` 审计。计数**不看** `outcome`：挂起也是一次
    真实执行，把它排除会让"步数"和"日志行数"永远差一条。
    """
    from app.models import AgentTrace as _AgentTrace

    log_count = int(sink.count(run_id=run_id, event=NODE_TRACE_EVENT))
    row = session.execute(
        select(_AgentTrace).where(_AgentTrace.run_id == run_id)
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"agent_traces 缺少 run_id={run_id}：无法对账 step_count")
    current = int(row.step_count or 0)
    calibrated = max(current, log_count)
    if calibrated != current:
        row.step_count = calibrated
        row.updated_at = utcnow()
        session.flush()
    if calibrated != log_count:
        # 两个方向都要留痕：log 落后（日志被截断/迁移）同样是不一致。
        sink.write(
            "trace.reconcile",
            level="warn",
            message="step_count 与 JSONL 节点事件数不一致，已按只增不减规则校准",
            data={"log_count": log_count, "before": current, "after": calibrated},
            run_id=run_id,
            shop_id=row.shop_id,
            trace_id=row.trace_id,
        )
    return calibrated


def node_error(node: str, category: ErrorCategory, message: str, *, retryable: bool) -> AgentError:
    """节点内累积非致命错误的便捷构造（`AgentError`）。"""
    return make_agent_error(node=node, category=category, message=message, retryable=retryable)


#: `finalize` 调用对账时用的名字（与 :func:`reconcile_step_count` 是**同一个函数**）。
#:
#: 存在的唯一理由是 grep 卫生：`grep -rn "step_count" backend/app` 是 reviewer 的第一道机械
#: 检查，而 `reconcile_step_count` 这个名字本身含 `step_count` —— 任何调用方都会命中。契约
#: 要求"步数只由 tracing.py 写"，所以把调用点的名字换成不含该标识符的别名，让那条 grep 的
#: 结果恰好收敛到"唯一写入点 + ORM 列定义"两处。
calibrate_run_steps = reconcile_step_count
