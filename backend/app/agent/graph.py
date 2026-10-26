"""LangGraph 状态机的组装与四个公开入口（CONTRACT §4.1 / §4.2 / §4.4 / §4.6）。

## 图拓扑（§4.2 冻结）

```
START → collect ──(ok)──> compute_metrics → detect_anomalies → explain → propose_actions
           │                                                        │
           │                                          (无提议)      ├──> finalize → END
           │                                                        └──> approval_gate ← interrupt()
           └──(fail)──> finalize → END                                     │
                                                            (恢复后) → execute → record_effect → finalize
```

`explain` 是**旁路**节点（v1.6.0）：它读 `anomalies` + `metrics`，写 `explanations` /
`suggestions`（都只是给人看的文字），然后把控制权原样交给 `propose_actions`。它不产生
`ActionProposal`、不改 `status`、也不进审批链——动作**永远**只由规则产出，模型的话最多
变成一句解释（见 `nodes.explain_node` 的模块说明）。

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
        # `max_attempts` 含首次：配置 4 就是"最多 4 次调用"，不是"重试 4 次"（差一次很要命）。
        max_attempts=int(rate.max_attempts),
        initial_interval=float(rate.backoff_base_s),
        # 指数退避固定 2 倍：这个倍数与 `backoff_base/max` 一起构成 §9.2 的预算，
        # 改它等于改契约，不是调参。
        backoff_factor=2.0,
        max_interval=float(rate.backoff_max_s),
        # 抖动是必须的：多个 worker 同时被 429 打回，不抖动会同步重试再次把上游打满。
        jitter=True,
        # 判据只有一处（`is_retryable_doudian_error`）：HITL 挂起、本地校验错、
        # business/4xx/auth/signature 都不在这里重试，重试它们只是把同一个错重复四遍。
        retry_on=nodes.is_retryable_doudian_error,
    )


def _execute_retry_policy(settings: Settings) -> RetryPolicy:
    """执行节点的重试预算：同上，但**只有**可重试错误才有第二次机会。

    `ambiguous`（mutating 调用遇到超时/连接/5xx）不在可重试集合里（ADR-011）：
    抖店没有服务端幂等键，重发等于赌"上一次没生效"。它走 `status="unknown"` + 对账（§9.3）。
    """
    return _collect_retry_policy(settings)


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    """构建并编译状态机。`checkpointer=None` 时不启用持久化（仅限内部工具/单测）。

    保证：产出的图与 §4.2 的拓扑逐边一致——9 个节点、2 条条件边、8 条普通边
        （含 `START → collect` 与 `finalize → END`）。
    不保证：不保证能跑。这里只**装配**：没有表、没有运行时、没有日志汇时图照样能编译成功，
        第一个节点执行才会炸。
    调用方坑：`checkpointer=None` 只给单测/画图用；`app/` 下必须走
        `resolve_checkpointer()`（§5.7 禁止 `MemorySaver`），否则审批挂起点在进程重启后消失。

    ## 9 个节点各自的边（凡是"为什么是这条边"都在这里说清）

    - `collect`：`START` 进来；出边**条件化**（`route_after_collect`）——`raw` 缺失或
      `status=failed` 时直奔 `finalize`，否则去 `compute_metrics`。这是全图唯一的"入口分流"，
      也是 §4.2 那条 `collect --(fail)--> finalize`。
    - `compute_metrics` / `detect_anomalies` / `explain`：普通边串联。它们不决定去向，"缺数据"
      一律通过 `status=failed` + `_aborted` 短路表达，出口只有 `finalize` 一个。`explain` 卡在
      `detect_anomalies` 与 `propose_actions` 之间的理由是"它需要 anomalies 已经落地"——放到
      `detect_anomalies` 之前就得自己去求值规则，那是第二份规则真相。
    - `propose_actions`：出边**条件化**（`route_after_propose_actions`）——`pending_actions`
      为空就直通 `finalize`（没有提议就没有可审批的东西），否则进 `approval_gate`。
    - `approval_gate`：单入单出。它"是不是要等人"不由自己判断，而由上游条件边决定；
      节点体内只有一次 `interrupt()`，恢复后重放到末尾就沿普通边去 `execute`。
    - `execute` → `record_effect` → `finalize`：普通边。`record_effect` **不等** T+N
      （ADR-003），只登记窗口，采样交给调度器。
    - `finalize`：`END`。它是**所有**分支的汇合点（失败也走这里收口）。

    ## `route_after_*` 两个判据为什么长这样

    两个函数都先看 `_aborted(state)`（`status == failed`）再看业务条件：失败是**终局**，
    它必须能压过"看起来还有活要干"的判断——否则一个失败的 run 会带着残缺数据继续往下跑，
    最后在更深的节点里以另一种面貌再炸一次。

    `explain` 没有引入第三條条件边，这是刻意的：它没有任何"要不要走另一条路"的判断。
    它降级（无 key / 超时 / JSON 坏）时返回空解释并让流程继续，判死与否由上游的
    `status=failed` 决定（`_aborted` 在节点开头短路）——把降级画成一条分支边等于让
    "旁路坏了"和"run 失败了"在图上长得一样。

    ## 为什么挂起**不走** failure

    `approval_gate` 的 `interrupt()` 抛的是 `GraphInterrupt`（`GraphBubbleUp` 子类），它在
    `traced()` 里被分流成 `outcome="interrupted"` + `level="info"`，且**原样 re-raise**。
    从图的角度看，挂起是"这一轮正常停在这里，等一个恢复命令"，不是"出错了"：把挂起算成
    failure 会让每轮正常审批都在日志/告警页上留一条红灯，而红灯天天亮就等于没有告警。
    """
    settings = get_settings()
    retry = _collect_retry_policy(settings)
    # 节点级超时取自 `app.request_timeout_s`：一个节点的上游调用不该比整个 HTTP 请求还能耗。
    timeout = TimeoutPolicy(run_timeout=timedelta(seconds=int(settings.app.request_timeout_s)))

    # `StateGraph(AgentState)` 用 TypedDict 的形状做"字段全集"校验：节点返回未声明的键
    # 在 LangGraph 里是被丢弃的（不会报错），所以 §3.2 的字段表必须与这里的类型逐字对齐。
    builder: StateGraph = StateGraph(AgentState)
    # 只有 `collect` / `execute` 挂重试策略：它们是**唯一**会打外部上游的两个节点。
    # 其余节点是纯计算或纯落库，重试它们只会把同一个逻辑错误重复几遍。
    builder.add_node(
        "collect", nodes.collect, retry_policy=retry, timeout=timeout
    )
    builder.add_node("compute_metrics", nodes.compute_metrics_node)
    builder.add_node("detect_anomalies", nodes.detect_anomalies_node)
    # `explain` **不挂** RetryPolicy / TimeoutPolicy，这是设计而不是遗漏：
    #   ① 旁路的设计规则是"失败一律降级、不重试"——LangGraph 的 RetryPolicy 会对可重试错误
    #      重发，那正是这里禁止的行为；它自己的降级与墙钟预算在 `services/llm.py` 里。
    #   ② 节点的 TimeoutPolicy 取自 `app.request_timeout_s`（30s），比 LLM 的 5s 预算长得多：
    #      套上它只会产生第二个"谁先超时"的真相，而内层永远先到。
    builder.add_node("explain", nodes.explain_node)
    builder.add_node("propose_actions", nodes.propose_actions_node)
    builder.add_node("approval_gate", nodes.approval_gate)
    builder.add_node(
        "execute", nodes.execute, retry_policy=_execute_retry_policy(settings), timeout=timeout
    )
    builder.add_node("record_effect", nodes.record_effect)
    builder.add_node("finalize", nodes.finalize)

    builder.add_edge(START, "collect")
    # 条件边的映射表把"节点返回的字符串"翻译成"下一个节点名"。两个键必须与
    # `route_after_collect` 的返回值逐字一致，否则运行期会抛 "unknown branch"。
    builder.add_conditional_edges(
        "collect",
        nodes.route_after_collect,
        {"compute_metrics": "compute_metrics", "finalize": "finalize"},
    )
    builder.add_edge("compute_metrics", "detect_anomalies")
    # `detect_anomalies --(旧直连)--> propose_actions` 在 v1.6.0 被拆成两段：中间插入 `explain`。
    # 两条都保持**普通边**（不是条件边）：`explain` 降级时也要继续往下走，而"这一轮死了没有"
    # 由节点自己的 `_aborted` 短路表达，不需要在边上再判一次。
    builder.add_edge("detect_anomalies", "explain")
    builder.add_edge("explain", "propose_actions")
    builder.add_conditional_edges(
        "propose_actions",
        nodes.route_after_propose_actions,
        {"approval_gate": "approval_gate", "finalize": "finalize"},
    )
    # 普通边而不是条件边：要不要等人已经在 `propose_actions` 之后判过了，
    # 这里再判一次就会有两处"要不要审批"的真相。
    builder.add_edge("approval_gate", "execute")
    builder.add_edge("execute", "record_effect")
    builder.add_edge("record_effect", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


def _config(thread_id: str, settings: Settings, **configurable: Any) -> dict[str, Any]:
    """统一的运行配置：`thread_id` 必须给（checkpointer 靠它认线程）+ 递归上限。

    保证：返回的 dict 可以直接作为 `ainvoke/aget_state` 的 `config=`；`thread_id` 一定在
        `configurable` 里（这正是 checkpoint 认线程、以及 `resume_agent` 找到挂起点的依据）。
    不保证：不保证线程已存在。`thread_id` 只是个键，不存在时 `aget_state` 返回空快照。
    调用方坑：`**configurable` 里的键（`runtime`/`client`/`now`）是**测试注入通道**，
        会覆盖进程级单例；生产路径不要传，否则一次调用会用到另一份运行时。
    """
    payload: dict[str, Any] = {"thread_id": thread_id}
    payload.update(configurable)
    return {
        "configurable": payload,
        # 递归上限是 `induction`/自循环的最后一道兜底：`traced()` 的 iteration 守卫是
        # 业务层的，这里是框架层的，两者不互相替代（一个能写出审计，一个能强制停机）。
        "recursion_limit": int(settings.agent.recursion_limit),
    }


def _status_of(snapshot: Any) -> str:
    """以快照为准判定 run 状态：挂起中优先（§4.2 的 `awaiting_approval`）。

    保证：只要有挂起的中断就返回 `awaiting_approval`，**不管** `values["status"]` 是什么。
    不保证：不保证挂起之后又跑了别的节点（那种情况该看 `interrupts` 是否为空）。
    调用方坑：`AgentState.status` 在这里会"落后一步"——`interrupt()` 抛异常，节点那个
        `status=awaiting_approval` 的返回值被丢弃了，所以必须读 `snapshot.interrupts`。
    """
    if getattr(snapshot, "interrupts", None):
        # 挂起优先：`values` 里的 status 还是挂起**之前**那个（running），
        # 拿它判断会让 `/api/agent/run` 把"等人审批"回成"还在跑"。
        return RunStatus.AWAITING_APPROVAL.value
    values = getattr(snapshot, "values", None) or {}
    return str(values.get("status") or RunStatus.RUNNING.value)


def _initial_state(*, shop_id: str, run_id: str, thread_id: str, trace_id: str,
                   trigger: str, started_at: Any) -> AgentState:
    """§3.2 全字段初始值：一个字段都不省（缺字段的 state 会在节点里变成 KeyError）。

    保证：21 个 §3.2 字段全部就位，且列表类/字典类字段是**空容器**而不是 `None`（节点普遍写
        `state.get("errors") or []`，但 `_initial_state` 是唯一的"第一份 state"，
        这里给 None 会让第一轮就分流到不同的代码路径）。
    不保证：不保证业务上"没有数据"。`raw`/`metrics`/`approval`/`effect_report` 全是 `None`，
        表示"这次还没算出来"，不是"算出来是空"。
    调用方坑：`status` 直接写 `running`——`run_agent` 必须先把 `agent_traces` 行插好，
        否则第一个节点（`traced` 包装器）写状态时会撞 `LookupError`。
    """
    return AgentState(
        run_id=run_id,
        trace_id=trace_id,
        shop_id=shop_id,
        thread_id=thread_id,
        trigger=trigger,
        started_at=started_at,
        status=RunStatus.RUNNING.value,
        # iteration 从 0 起：`traced()` 在每次正常返回时 +1，所以第一次执行后是 1。
        iteration=0,
        raw=None,
        metrics=None,
        # history 是"近 8 天"的累积序列（含今天），由 compute_metrics 逐日 append。
        history=[],
        anomalies=[],
        # 旁路节点 explain 的产物（§3.2 的 NotRequired 两字段）：这里也给空 dict，
        # 让"读 state 的人"不必区分"没有这个键"与"它是空的"（老 checkpoint 才会缺键）。
        explanations={},
        suggestions={},
        pending_actions=[],
        approved_actions=[],
        rejected_actions=[],
        approval=None,
        execution_results=[],
        effect_report=None,
        errors=[],
        # audit 与 errors 都靠 append_* 系列"返回完整新列表"来累积（§3.1 禁 reducer）。
        audit=[],
    )


async def _pending_for_run(run_id: str) -> list[PendingApproval]:
    """某 run 当前仍处于 `pending` 的审批提议（`/api/alerts` 与两个入口共用的读路径）。

    保证：只返回 `status=pending` 的行；不存在的 run 给空列表（不是 404）。
    不保证：不保证这些 action 在 checkpointer 里还有对应的挂起点——thread 被清理后
        这里仍可能列出"点不动的待办"（列表能工作优先于列表精确，见 routes_alerts 的说明）。
    调用方坑：这是**同步 Session + 异步仓储**的组合（见 `_session()`），必须在事件循环里
        `await`，且不能把它放进已经打开的事务里嵌套调用。
    """
    from app.services import approvals

    with _session() as session:
        return await approvals.list_pending_for_run(session=session, run_id=run_id)


def _session() -> Any:
    """节点/入口共用的事务上下文（同步 Session + async 仓储函数，见 nodes 模块 docstring）。

    保证：`with` 退出时提交、异常时回滚、无论如何关闭（`db.session_scope` 的语义）。
    不保证：不保证线程安全——同步 `Session` 不是一个可共享对象，所以每次调用都新开一个。
    调用方坑：`with _session() as session:` 里 `await` 一个 async 仓储函数是**有意**的搭配：
        仓储内部用 `arun_db`（`to_thread`）执行同步 SQL，Session 只是被传进去的句柄。
    """
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

    保证：只要返回，`agent_traces` 里那一行的 `trace_id`/`thread_id` 与返回值一致；
        已终态时**不重跑图**，返回的是已存结果的回放。
    不保证：不保证"没抛异常就是成功"——`status` 可能是 `failed`（节点把 run 判死）。
        也不保证并发安全：两个请求同时进来时本函数有竞态窗口（先查后插不是原子的），
        真正的互斥在 `app/api/deps.py::inflight_runs`。
    调用方坑：不要为了"重试一下"换 `run_id`——`run_id` 就是幂等键，换了等于新开一轮
        采集+改价；失败后重跑同一个 `run_id` 也只会回放失败结果。
    """
    thread_id = thread_id_for(shop_id, run_id)
    settings = get_settings()

    # 幂等检查在**插入之前**：已存在的行决定"是回放还是重跑"，而 insert_run_trace 是
    # insert-if-absent，先插再查会把"回放"变成"新建"，等于把终态 run 重置成 running。
    existing = await get_run_trace(shop_id=shop_id, run_id=run_id)
    if existing is not None:
        status = str(existing["status"])
        if status in _ACTIVE_STATUSES:
            # 非终态 = 有一个执行者正在跑（或崩溃遗留）。这里直接 409 而不是"帮它收尾"：
            # 收尾需要知道图停在哪，而这份知识只在那个执行者的进程里。
            raise RunInProgress(
                f"run 正在执行中：{thread_id}",
                details={"thread_id": thread_id, "status": status},
            )
        # 终态 → 回放（不重新跑图，也不再写一行 agent_traces）
        # 仍然要打开 checkpointer：`effect_report` 与 `state` 只存在于 checkpoint 里，
        # `agent_traces` 只存摘要（§6.1 那行没有节点明细列）。
        async with resolve_checkpointer(checkpointer, settings=settings) as saver:
            graph = build_graph(saver)
            config = _config(thread_id, settings)
            snapshot = await graph.aget_state(config)
            return RunResult(
                run_id=run_id,
                thread_id=thread_id,
                # trace_id 取库里那份而不是新生成：回放必须给客户端同一个 id，
                # 否则它会以为自己拿到了另一次执行。
                trace_id=str(existing["trace_id"]),
                status=status,
                # 回放时 pending_approvals 恒为空：终态意味着已经没有待办了。
                pending_approvals=[],
                effect_report=_effect_report_of(snapshot),
                state=_values_of(snapshot),
            )

    # 先插摘要行再跑图：`traced()` 的第一次 +1 会 `update` 这一行，
    # 没有它就直接 `LookupError`（宁可启动失败，也不要把 trace 丢了）。
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
    # 日志汇是节点明细的事实源：没人装配就被动装配一个，否则这一轮的 node.trace 全丢。
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
            # started_at 用 ISO 字符串进 state、用 datetime 进表：state 要走 checkpoint
            # 序列化（datetime 会被 serde 变成带类型标记的对象，读回来还要再还原）。
            started_at=utc_iso(started_at),
        )
        try:
            await graph.ainvoke(initial, config=config)
        except Exception as exc:  # noqa: BLE001 - 兜住后**记失败再抛**，绝不吞
            # 异常从图里逃出来时，`finalize` 没跑过，`agent_traces` 还停在 running；
            # 不在这里补写 failed，这一行会永远"看起来在跑"（后续请求全都 409）。
            await _mark_failed(shop_id=shop_id, run_id=run_id, exc=exc)
            raise
        snapshot = await graph.aget_state(config)

    status = _status_of(snapshot)
    if status == RunStatus.AWAITING_APPROVAL.value:
        # 挂起后必须回填 interrupt_id：审批台的所有入口（列表、POST /api/approve）
        # 都靠它，空串等于"这个待办点不动"。
        await _backfill_interrupt_id(snapshot)
    return RunResult(
        run_id=run_id,
        thread_id=thread_id,
        trace_id=trace_id,
        status=status,
        pending_approvals=(
            # 只有挂起时才去查待办：终态 run 查出来必然是空的，白跑一次库。
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
        # 没挂起就没有"当前中断 id"可回填：正常路径（跑到终点）走这里。
        return
    values = _values_of(snapshot) or {}
    action_ids = [str(item["action_id"]) for item in values.get("pending_actions") or []]
    if not action_ids:
        # 挂起了但没有待办 = 图状态与库不一致，此时回填只会写脏数据，直接跳过。
        return
    interrupt_id = str(interrupts[0].id)
    with _session() as session:
        await approvals.backfill_interrupt_id(
            session=session, action_ids=action_ids, interrupt_id=interrupt_id
        )


def _ensure_log_sink(settings: Settings) -> None:
    """没人装配 JSONL 汇时按 settings 装配一个：节点级明细的事实源不能是空的。

    保证：本函数返回后 `get_log_sink()` 一定非 `None`（要么原来就有，要么这里建了）。
    不保证：不保证那个汇指向"你期望的文件"——已经装配过的汇不会被换掉（测试注入的 tmp
        汇因此不会被生产路径覆盖）。
    调用方坑：只在**写路径**（入口）调用；读路径（`/api/logs`）必须用 `get_log_sink()`，
        否则一次读请求会顺手建文件、且新实例的 `_seq` 从 0 重新数，破坏 seq 单调性。
    """
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
        # `finished=True` 必须一起给：否则这一行会停在 failed 但 finished_at 为空，
        # 启动对账会把它当成"非终态 run"再捞一遍。
        finished=True,
        # 只留 "类型: 原文"：完整栈在 JSONL 的 `api.unhandled` / `node.trace(error)` 里，
        # 往这一列塞几百行栈会让 `agent_traces` 变成日志表。
        error=f"{type(exc).__name__}: {exc}",
    )


def _values_of(snapshot: Any) -> AgentState | None:
    """快照里的 state 值；空快照 → `None`（而不是 `{}`）。

    保证：区分"线程不存在"（`None`）与"线程存在但 state 为空"（理论上不会发生）。
    不保证：不保证 state 结构完整——老 checkpoint 可能缺后来新增的键，读取方要按 `.get()` 取。
    调用方坑：`None` 必须被当成 404 的依据（`resume_agent` 就是这么做），不要 `or {}` 抹平。
    """
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
        # 用 warn 而不是 info：这条事件是"某次恢复没走闸门"的唯一证据，
        # 按 `level=warn` 过滤是运维发现它的唯一入口。
        level="warn",
        message="resume_agent 未传 expected_interrupt_id：本次恢复跳过了 interrupt_id 校验",
        # 带上当前 interrupt id（而不是只带 thread）：事后才能判断"当时挂起点是哪个"。
        data={"thread_id": thread_id, "current_interrupt_id": interrupt_id},
    )


def _effect_report_of(snapshot: Any) -> Any:
    """快照里的 `effect_report`；没有/形状不对 → `None`。

    保证：只有 `dict` 才返回，`None` 与"非 dict 的脏值"统一折成 `None`。
    不保证：不保证报告是完好的 §3.3 形状（外层键齐不齐交给 `api/convert.effect_report`
        的 pydantic 校验，那里失败会 500——宁可炸，不要给前端半份报告）。
    调用方坑：`None` 的含义是"本轮没有执行过动作，没有窗口可登记"，**不是**"执行了没效果"；
        后者是 `windows[].status` 的事。
    """
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

    保证：恢复成功后裁决已落库、`agent_traces` 已从 `awaiting_approval` 迁到 `executing`，
        返回的 `applied[]` 与提交的 `decisions` 一一对应。
    不保证：不保证线程从此终态——上游如果还有下一轮提议，`status` 可能又是
        `awaiting_approval`；也不保证执行成功，`applied[].status` 可能是 `failed`/`unknown`。
    调用方坑：提交是**全量**的（`validate_resume_payload` 要求 action_id 集合完全相等）；
        重复提交同一份 body 会先撞 `RunStateConflict`，API 层再翻译成 `DUPLICATE_DECISION`。
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
        # `interrupts` 是**当前**挂起点；空 = 这个线程现在没在等人。
        interrupts = tuple(getattr(snapshot, "interrupts", ()) or ())
        if not interrupts:
            raise RunStateConflict(
                f"thread 存在但没有挂起的中断：{thread_id}",
                details={"thread_id": thread_id, "status": _status_of(snapshot)},
            )
        # 只取第一个：本图任意时刻最多一个挂起点（approval_gate 是唯一的 interrupt 点）。
        interrupt_id = str(interrupts[0].id)
        if expected_interrupt_id is None:
            # 不抛（契约允许这种调用形状），但它等于放弃防重放闸门，所以必须留痕。
            _warn_interrupt_not_checked(thread_id=thread_id, interrupt_id=interrupt_id)
        elif expected_interrupt_id != interrupt_id:
            # 旧页面点第二次 / 别人的挂起点：必须挡住，不能把旧裁决喂给新挂起点。
            raise ApprovalNotFound(
                "提交的 interrupt_id 不是当前挂起的中断（可能已处理或已过期）",
                details={"thread_id": thread_id, "current_interrupt_id": interrupt_id},
            )
        pending = list(values.get("pending_actions") or [])
        # 待审批的 action_id 集合是**后续三处校验的共同基准**：集合相等（这里）、
        # 库里这些行仍是 pending（`assert_all_pending`）、以及回填 interrupt_id 的目标。
        pending_ids = [str(item["action_id"]) for item in pending]

        # 第二道闸：API 层已校验过一次，这里再校验一次（防"绕过 API 直接调 resume_agent"）。
        payload = nodes.validate_resume_payload(
            {"type": "approval_decisions", "decisions": list(decisions)}, expected=pending
        )

        shop_id = str(values.get("shop_id"))
        run_id = str(values.get("run_id"))
        with _session() as session:
            # 库里的 pending 与 checkpoint 里的 pending 可能不同步（另一个进程刚批过）：
            # `assert_all_pending` 把"已裁决的行"抛成 DuplicateDecision，API 层据此回 409
            # 而不是让同一批动作被执行第二次。
            await approvals.assert_all_pending(
                session=session, thread_id=thread_id, action_ids=pending_ids
            )
            # 回填是幂等的（`WHERE interrupt_id=''`）：恢复路径也补一次，兜住
            # "挂起后进程被杀、入口没来得及回填"的时间窗。
            await approvals.backfill_interrupt_id(
                session=session, action_ids=pending_ids, interrupt_id=interrupt_id
            )

        # §4.2：校验通过后 run 进入 executing
        from app.agent.tracing import update_run_trace

        await update_run_trace(
            shop_id=shop_id, run_id=run_id, status=RunStatus.EXECUTING.value
        )

        try:
            # resume 的 key 必须是 interrupt id、value 必须是校验后的 payload（§4.4 / §11）：
            # 用 list 或裸 payload 都会让 LangGraph 找不到要恢复的中断（"点了没反应"）。
            await graph.ainvoke(Command(resume={interrupt_id: payload}), config=config)
        except Exception as exc:  # noqa: BLE001 - 记失败再抛
            # 异常逃出图 = finalize 没跑，库里还停在 executing；不补写 failed 会永久 409。
            await _mark_failed(shop_id=shop_id, run_id=run_id, exc=exc)
            raise
        snapshot = await graph.aget_state(config)

    # 恢复后快照为空（极端窗口里 thread 被清理）时退回恢复**之前**的 values：
    # 至少还能把 run_id/trace_id 返回给客户端，而不是抛 AttributeError 变 500。
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
    """`applied[]`：拒绝的动作不写 `action_log`，其 status 固定 `"rejected"`（§7.4）。

    保证：返回的条数与 `decisions` 相同，顺序按 `decisions`（dict 保序）；每条都带
        `action_id` / `verdict` / `status`。
    不保证：不保证 `status` 一定是终态——`pending`（本轮没产生执行回执）也会出现，
        那表示"批了但还没执行"，不是"执行成功"。
    调用方坑：`action_log_id` 只等于 `idempotency_key`（§6.1 没有第二套 ID），而
        `skipped_duplicate` / `rejected` 两种状态下它必须是 `null`——前端按它为 `null`
        判断"这次没有真的向上游发出请求"。
    """
    results = {
        str(item["action_id"]): item for item in values.get("execution_results") or []
    }
    verdicts = {str(item["action_id"]): str(item["verdict"]) for item in decisions}
    applied: list[AppliedDecision] = []
    for action_id, verdict in verdicts.items():
        if verdict == "reject":
            # 拒绝的动作**没有**执行，所以既没有 action_log 行也不该有 idempotency_key。
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
            # 批了但本轮没执行：可能是 abort（run 在前面就失败了），也可能是恢复了但没有
            # 排到它。给 `pending` + 一句说明，比静默丢一条更能让人发现"这批没跑完"。
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
    """所有 `pending` 的审批提议（审批台首屏；`/api/alerts` 复用同一仓储）。

    保证：只返回 `status=pending` 的行；`shop_id` 为 `None` 时不过滤店铺（全局待办。
    不保证：不保证每一行都能被审批——thread 被清理后这些行仍在库里（列表与审批可点性
        是两件事，见 `routes_alerts` 的模块说明）。
    调用方坑：`before_id` 是 keyset 游标（`id <` 而不是 offset），而排序第一键是
        `created_at`；只在"created_at 随 id 单调"的前提下翻页才不重不漏。
    """
    from app.services import approvals

    with _session() as session:
        return await approvals.list_by_status(
            session=session,
            # 写死 pending：这个函数的名字就是承诺，不提供"列出全部"的口子。
            status=ApprovalStatus.PENDING,
            shop_id=shop_id,
            limit=limit,
            before_id=before_id,
        )
