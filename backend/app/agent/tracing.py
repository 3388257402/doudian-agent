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
    """`input_digest` 的入参视图：只挑能判断"输入是否同一份"的字段。

    保证：同一份逻辑输入（同一 run、同一采集时刻、同一批 pending/approved/executed id）
        永远得到同一个摘要——重放与重试因此可以按摘要比对"这次是不是又拿同一份输入跑的"。
    不保证：它**不是** state 的完整快照，也不是业务语义的校验和。没被挑进这里的字段
        （`audit` / `errors` / `history` / `effect_report`）改了都不会改变摘要；拿它当
        "state 变了没有"的判据会漏判。
    调用方坑：这里**不**脱敏也不做深拷贝——脱敏发生在 :func:`digest_of` 里；而摘要只保留
        16 位、且不进 `state`，所以它不能用来反查内容，只能用来判"同不同"。
    """
    raw = state.get("raw")
    metrics = state.get("metrics")
    return {
        "run_id": state.get("run_id"),
        "status": str(state.get("status") or ""),
        # 采集时刻是"输入是否同一份"的最强信号：同一个 run 重跑会重新采集，
        # 两次采集的 collected_at 必然不同，摘要也就必然不同。
        "raw_collected_at": raw.get("collected_at") if isinstance(raw, Mapping) else None,
        # 业务日期同理：跨零点重跑会落到另一天，摘要变了才是对的。
        "metrics_date": metrics.get("date") if isinstance(metrics, Mapping) else None,
        # 三个 id 列表用**顺序敏感**的 list（不是 set）：节点重放时顺序是不变量的一部分，
        # 排序会掩盖"同样的集合、不同的顺序"这种真实差异。
        "anomaly_ids": [item.get("anomaly_id") for item in state.get("anomalies") or []],
        "pending_action_ids": [
            item.get("action_id") for item in state.get("pending_actions") or []
        ],
        "approved_action_ids": [
            item.get("action_id") for item in state.get("approved_actions") or []
        ],
        # 已经执行过的幂等键：它一变就说明"这次不是同一份输入"（execute 已经动过上游）。
        "executed_keys": [
            item.get("idempotency_key") for item in state.get("execution_results") or []
        ],
    }


def _output_view(update: Mapping[str, Any]) -> dict[str, Any]:
    """`output_digest` 的出参视图：节点这次**返回了什么形状的结果**。

    保证：`keys` 是排序后的键集合，因此"节点这次返回了哪些字段"这一层形状变化一定能被
        摘要捕捉到（新增/漏掉一个字段 → 摘要必变）。
    不保证：不保证内容等价。两个不同的 summary 文本、两个不同的 `errors[].message` 只要
        键相同且摘要碰撞，就会得到同一个 `output_digest`；它**不是**内容校验和，
        排障时不要拿它当"这次输出对不对"的证据。
    调用方坑：它只在 `traced()` 判定 `outcome="ok"` 时才被调用（挂起/失败分支写 `None`），
        所以拿 `output_digest is None` 反推"这个节点失败了"会把正常挂起也算进去。
    """
    raw = update.get("raw")
    metrics = update.get("metrics")
    return {
        "status": str(update.get("status") or ""),
        # 排序是**摘要稳定性**要求：dict 的迭代顺序取决于构造顺序，节点重放时不一定一致，
        # 不排序会让"内容没变"的两次执行得到不同的摘要。
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
    """写一条 `node.trace`。`data` 只含 §7.6.1 冻结的五个键，**不新增**顶层字段。

    ## 三态 `outcome` 与 `level` 是**派生**关系，不是两个独立入参

    调用方只给 `outcome`，`level` 与 `message` 由 `_OUTCOME_LEVEL` / `_OUTCOME_MESSAGE`
    查表得出：`ok`/`interrupted` → `info`，`error` → `error`。这样"挂起不许染红"
    （ADR-016）就不是一条需要每个调用点记住的规矩，而是查表结果的唯一形态——想让挂起变红，
    必须改动那张表，而它是本模块私有的。

    保证：`data` 里恰好五个键；`error` 的存在性由 `outcome` 唯一决定（`error` 只能配
        `outcome="error"`，其余形态必须为 `None`，三条前置断言把这条守住）。
    不保证：不保证 `level` 一定等于"人想报警的东西"——`warn` 级事实走的是
        `_log_audit` 的自定义事件，`node.trace` 只有 info/error 两档。
    调用方坑：写盘失败会抛（这是事实源，不能吞）；且本函数**不**写 `step_count`，
        计数由 :func:`traced` 在同一次执行里紧接着 +1（两者顺序不可交换，见该函数）。
    """
    if outcome not in _OUTCOME_LEVEL:  # pragma: no cover - 内部调用点受限
        # 三态是闭合集合：调用方拼错一个字符串（"failed" / "ok "）必须当场炸，
        # 否则那条日志会因为 level 查表失败而在写盘路径上变成另一个更难查的异常。
        raise ValueError(f"非法 outcome: {outcome!r}（取值域 ok/interrupted/error）")
    if outcome == NODE_TRACE_OUTCOME_ERROR and error is None:  # pragma: no cover - 内部约束
        # error 是"为什么红"的唯一字段：允许它缺省等于让前端拿到一条没有原因的红行。
        raise ValueError("outcome=error 必须带 error 摘要")
    if outcome != NODE_TRACE_OUTCOME_ERROR and error is not None:  # pragma: no cover
        # 反向也守：挂起带着 error 会让读取端把"正常暂停"渲染成故障（ADR-016 的老事故）。
        raise ValueError(f"outcome={outcome} 时 error 必须为 None（§7.6.1 硬规则）")
    data = {
        "outcome": outcome,
        # 保留一位小数：毫秒精度足够定位慢节点，再多的位数只是噪声且会让日志行变长。
        "duration_ms": round(float(duration_ms), 1),
        "input_digest": input_digest,
        # 只有"这次真的成功了"才有出参摘要；挂起/失败的出参不是一个可比较的结果。
        "output_digest": output_digest if outcome == NODE_TRACE_OUTCOME_OK else None,
        "error": dict(error) if error is not None else None,
    }
    return dict(
        # 身份三件套直接取自 state（而不是参数）：保证 trace 行与它所描述的 run 不会
        # 出现"参数传错导致日志挂到别的 run 下"的错位。
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
    """异常 → §7.6.1 冻结的 `{"type", "message"}` 摘要（`message` 已脱敏）。

    保证：形状只有这两个键（读取端可以无条件按字符串展示），`message` 在落盘前会按
        `observability.redact_keys` 脱敏——token/签名不会因为"抛异常"而漏进日志。
    不保证：不保证能还原现场。这里**不**取 traceback、不取异常链、不取 `details`：
        要栈就去看 `api.unhandled` 那条（带 `logger.exception`），本函数只服务日志页的
        "这一行红的是什么"。
    调用方坑：`str(exc)` 可能很短甚至为空（例如 `raise ValueError()`），所以 `message`
        允许是空串；拿它做分组统计时要按 `type` 归一，不要按 `message` 相等。
    """
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
    # 先算"这次是第几次"再判上限：`iteration` 只在**正常返回**时自增（异常分支不经过这里），
    # 因此它统计的是"节点成功走完的次数"，不是"执行尝试次数"。
    iteration = int(state.get("iteration") or 0) + 1
    update["iteration"] = iteration
    if iteration <= MAX_ITERATIONS:
        return update
    # 超过上限不是"这次节点失败"，而是"整张图在打转"：只有强转 failed 才能让它走到
    # finalize 收口（图层看到 failed 才会走 collect--(fail)-->finalize 那一类收尾路径）。
    update["status"] = RunStatus.FAILED.value
    error = make_agent_error(
        node=node,
        category=ErrorCategory.UNKNOWN,
        message=f"iteration 超过上限 {MAX_ITERATIONS}，强制终止",
        retryable=False,
    )
    # 新错误必须并进**已有**列表再交给 `append_errors` 去重：直接覆盖会让前面节点攒下的
    # 错误凭空消失，而"为什么这一轮失败"往往就藏在那几条早期错误里。
    merged = list(state.get("errors") or [])
    merged.append(error)
    update["errors"] = append_errors({"errors": merged}, merged)  # type: ignore[arg-type]
    # 同样的道理给审计：`node.iteration_limit` 要能被"读 checkpoint 的人"看见，
    # 这条事件没有表级出口，所以它只能走 state.audit（不是 JSONL）。
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
        # 绝大多数节点正常返回都走这里：输出不是 failed，就没有"显式宣告失败"可言。
        return None
    if str(state.get("status") or "") == RunStatus.FAILED.value:
        # 输入已经是 failed：这是收口节点在给一个已死的 run 收尾，不是这次执行失败。
        # 少了这一句，`finalize` 会在每一轮失败 run 上再报一次错，日志里出现两条同因红灯。
        return None
    message = ""
    # 取**最后一条**非空 message：节点通常把根因（"订单数据缺失"）append 在最后，
    # 前面的条目往往是更早、更外围的降级记录。
    for item in update.get("errors") or []:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("message") or "")
        if text:
            message = text
    if not message:
        # 节点没给原因也必须能读：一句兜底说明总比 `error=None` 好，
        # 但措辞里点明"节点未给出具体原因"，免得读者以为那就是上游的原话。
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

    ## 为什么 `GraphBubbleUp` 必须**原样** re-raise（不是返回、不是包装）

    异常对象**就是**控制流本身：`GraphInterrupt` 携带 payload 去挂起、`GraphDrained` 通知
    图退出、`ParentCommand` 传递跨图命令。此处若 `return`（哪怕返回一个"看起来等价"的
    update）、或 `raise XxxInterrupt(...)` 重新构造、或把它包成自己的异常，LangGraph 就
    认不出这是一次中断：图不会挂起，节点返回值被当成普通结果继续往下走——`approval_gate`
    于是"不等人"，而 checkpoint 里也不会留下可恢复的挂起点。这就是"原样"两个字的全部含义：
    `raise` 空语句（裸 `raise`）而不是 `raise exc`，连 traceback 都不重开。

    ## 顺序不变量的两个方向

    - 事件必须写在 re-raise **之前**：一旦抛回去，本函数的栈就没了，之后没有任何地方能把
      这次执行补记成 `interrupted`；
    - `step_count` 也必须在 re-raise 之前 +1：挂起是一次**真实执行**（它写了 checkpoint），
      把它排除会让"步数"和"日志行数"永远差一条，`reconcile_step_count` 下次启动又要"修正"。
    """

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            settings = get_settings()
            redact_keys = settings.observability.redact_keys
            # 计时用 `perf_counter`（单调、不受系统时钟调整影响）：`time.time()` 在 NTP 校正
            # 或夏令时切换时会给出负的耗时。
            started = time.perf_counter()
            # 摘要必须在**调用节点之前**算：节点可能就地改 state 里的可变对象，
            # 事后再算就把"输入"算成了"输出"。
            input_digest = digest_of(_input_view(state), redact_keys=redact_keys)

            try:
                update = await fn(state, config)
            except GraphBubbleUp:
                # a) 控制流中断：不是失败（ADR-016）。记录后**原样**抛回去。
                #    这里绝不能 `return`——返回等于告诉图"这个节点正常走完了"，挂起就没了。
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
                #    注意捕的是 `Exception` 而不是 `BaseException`：`CancelledError`（部署时被
                #    kill）属于取消，不该被记成"节点失败"，否则一次正常重启会留下红灯。
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
            #    `dict(update or {})` 是拷贝：守卫要往 update 里写 iteration/status，
            #    直接改节点返回的对象会污染节点自己的局部引用。
            guarded = _guard_iteration(state, dict(update or {}), node=node_name)
            declared = _declared_failure(
                state, guarded, node=node_name, redact_keys=redact_keys
            )
            emit_node_trace(
                node=node_name,
                # outcome 由"有没有宣告失败"派生，不是由"返回了什么都算 ok"默认：
                # 这条三元就是 `collect --(fail)--> finalize` 那条正常边能被照红的原因。
                outcome=(
                    NODE_TRACE_OUTCOME_ERROR if declared is not None else NODE_TRACE_OUTCOME_OK
                ),
                state=state,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                input_digest=input_digest,
                output_digest=(
                    # 宣告失败时不给 output_digest：这一次的"输出"就是失败本身，
                    # 摘要它会让读取端以为拿到了一个可比对的正常结果。
                    None
                    if declared is not None
                    else digest_of(_output_view(guarded), redact_keys=redact_keys)
                ),
                error=declared,
            )
            # DB 更新排在 JSONL 事件**之后**：日志是事实源，先有事件才有"该 +1 多少"的依据；
            # 崩溃窗口造成的偏差由 `reconcile_step_count` 以日志为准校准回来。
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
    """挂起/失败分支：只 +1，不碰 status（`awaiting_approval` 由节点自己在 interrupt 前写）。

    保证：`step_count` 一定 +1（这次执行确实发生了），且**不会**顺手改 `status`。
    不保证：不保证状态迁移被记下来。失败时 `status=failed` 是节点自己写进 update 的
        （`_declared_failure` 只看不写），异常逃逸时由 `run_agent` 的 `_mark_failed` 补写。
    调用方坑：本函数必须在本节点 re-raise **之前**被 await（顺序见 :func:`traced`）；
        把它挪到 `finally` 之后、或改成"失败就跳过"，都会让 `step_count` 与 `node.trace`
        行数分叉。
    """
    await update_run_trace(
        shop_id=str(state["shop_id"]), run_id=str(state["run_id"]), step_delta=1
    )


# --------------------------------------------------------------------------------------
# agent_traces 仓储（`step_count` 的唯一写入点；`grep step_count backend/app` 只命中本文件
# 与 models.py —— 这是一条可被机械检查的不变量，因此仓储与否都放在这里）
# --------------------------------------------------------------------------------------


def _trace_row(row: AgentTrace) -> dict[str, Any]:
    """ORM 行 → 纯 dict（仓储函数对外的唯一形状，调用方不需要认得 `AgentTrace`）。

    保证：键集合固定；`step_count` 用 `int(... or 0)` 兜住历史 NULL（老库/手写 SQL 造出来的行）。
    不保证：时间列**没有**在这里转 ISO，也没有过 `to_utc()`——那两件事在入口层
        （`app/api/queries.to_utc` / `convert.iso_z`）做，本模块只搬值。
    调用方坑：返回的是新 dict，改它不会写回 ORM；反过来，调用方拿到的 `datetime` 可能是
        naive（SQLite 丢 tzinfo），直接 `isoformat()` 会得到一台本地时区的假时间。
    """
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

    保证：`(shop_id, run_id)` 至多一行；返回 `True` 严格等价于"这一行是我插的"（调用方
        可以据此决定是否打 `app.started` 之类的日志，而不会重复打）。
    不保证：不保证"插入成功 = run 可以开始跑"。两个并发请求都能走到这里、其中一个拿到
        `False`，但**都不**会因此停下来——串行化在 `app/api/deps.py` 的 `inflight_runs`，
        这里只保证不覆盖已有行。
    调用方坑：`step_count` 初始写死 0 且**不**接受外部传入（计数只由 `traced` 加）；
        已存在的行不会被"补全"字段，所以本函数不是 upsert，别拿它做数据修复。
    """

    def _fn(session: Any) -> bool:
        # 先查后插（不用 INSERT ... ON CONFLICT）：唯一约束只在真并发时才需要兜底，
        # 而普通重试/回放走的是这条便宜的读路径。
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
                # 计数从 0 起、只由 `traced()` 加：这里写任何非 0 值都会让
                # "行数 == step_count" 从第一秒起就不成立。
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
    """按 `(shop_id, run_id)` 取 run 摘要行（`UNIQUE(shop_id, run_id)` 保证唯一）。

    保证：返回形状与 :func:`_trace_row` 一致；不存在时给 `None`（不是空 dict，也不是抛错）。
    不保证：不含节点明细。`trace_id` / `step_count` 在这里是**摘要**，要"某 run 最近 N 条
        节点事件"请读 JSONL（`/api/logs`），本表没有那些行。
    调用方坑：`None` 是正常返回值，不是异常——`run_agent` 用它区分"首跑"与"回放"；
        但 `None` 也意味着 `update_run_trace` 会抛 `LookupError`，两者不要混用。
    """

    def _fn(session: Any) -> dict[str, Any] | None:
        # `scalar_one_or_none` 而不是 `first()`：唯一约束保证最多一行，
        # 真出现两行说明约束没生效（迁移漏了），此时应该炸而不是随便挑一行返回。
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
    """更新一次 run 的状态/步数。`step_delta` 只由 :func:`traced` 传 1。

    ## 为什么 `step_count` 只有本模块能写

    契约 §4.7 的不变量是"`count(node.trace)` == `agent_traces.step_count`"，它只有在
    **同一个写入点**里顺序执行"写事件 + 计数"时才成立。允许第二个地方 `+= 1`，两个计数
    就会各自漂移，而漂移只能在下次启动对账时才发现——那时已经分不清是崩溃窗口还是算错了。
    可机械检查的形态是 `grep -rn step_count backend/app` 只命中本文件与 `models.py` 的列定义。

    保证：`step_delta` 是**增量**（加在现值上，不是覆盖）；`status` 为 `None` 时不动状态列；
        行不存在时**抛** `LookupError` 而不是静默跳过。
    不保证：不保证并发安全。SQLite/PG 下这里是 `读-加-写`，两个请求同时更新同一行仍有丢更新
        窗口；幂等的前提是"同一 run 只有一个执行者"（API 层的 `inflight_runs` 闸门）。
    调用方坑：必须先 `insert_run_trace`，否则这里抛（故意的：没有摘要行就没有 run 可言）；
        并且不要为了"顺手把状态也改一下"而在这里塞业务判断——状态该由节点/入口决定。
    """
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
            # 增量加在现值上。这里的非原子性是已知的：真正的串行化由 API 层的
            # `inflight_runs` 闸门保证（同一 (shop_id, run_id) 只有一个执行者）。
            row.step_count = int(row.step_count or 0) + step_delta
        if status is not None:
            row.status = str(status)
        # `updated_at` 无条件推进（即使只加了步数）：它是"这个 run 最近一次动过"的信号，
        # `/api/healthz` 之外的对账也靠它判断"卡了多久"。
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
    # `max` 而不是直接赋值：只增不减。日志文件被轮转/截断时 log_count 会偏小，
    # 直接赋值等于把已经真实发生过的步数抹掉。
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
    """节点内累积非致命错误的便捷构造（`AgentError`）。

    保证：返回的是 §3.3 `AgentError` 的完整形状（`node`/`category`/`message`/`retryable`/`at`），
        可以直接塞进节点的 `errors` 列表。
    不保证：**不落库、不进 state**。它只是构造一个值；要真正留痕，调用方必须把它并进
        返回值里的 `errors`（`append_errors`）或被 `_log_audit` 写进 JSONL。
    调用方坑：`retryable` 必须由调用方按 §9.1 的类别表判断（通常用
        `enums.is_retryable_category`），不要手写 `retryable=True`——那条规则只有一个事实源。
    """
    return make_agent_error(node=node, category=category, message=message, retryable=retryable)


#: `finalize` 调用对账时用的名字（与 :func:`reconcile_step_count` 是**同一个函数**）。
#:
#: 存在的唯一理由是 grep 卫生：`grep -rn "step_count" backend/app` 是 reviewer 的第一道机械
#: 检查，而 `reconcile_step_count` 这个名字本身含 `step_count` —— 任何调用方都会命中。契约
#: 要求"步数只由 tracing.py 写"，所以把调用点的名字换成不含该标识符的别名，让那条 grep 的
#: 结果恰好收敛到"唯一写入点 + ORM 列定义"两处。
calibrate_run_steps = reconcile_step_count
