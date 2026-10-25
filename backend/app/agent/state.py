"""AgentState 与子结构 —— 逐字对应 docs/CONTRACT.md §3（唯一事实源）。

reducer 规则（§3.1，硬性，违反即 bug）：

- 所有字段都是 **replace** 语义。**禁止** `Annotated[..., operator.add]` 或任何自定义
  append reducer。理由：`interrupt()` 恢复会**从节点开头重放**该节点（DECISIONS ADR-010），
  append reducer + 重放 = 重复行，且重复行会被下游当成"真的发生了两次"。
- 节点返回"该字段的完整目标值"。
- 需要审计事件时调用 :func:`append_audit`，它内部按 `event_id` 去重并返回**完整**新列表。

本文件同时定义 §4.1 要求的四个返回值数据类（`PendingApproval` / `RunResult` /
`ResumeResult` / `AppliedDecision`）。它们放在这里而不是 graph.py，是因为
`app/services/approvals.py` 也要 import `PendingApproval`（契约 §1 的导入表如此约定）。

## 必需键 vs 可选键（`TypedDict` 的 `total` / `NotRequired` 语义）

- `AgentState` 是 **`total=True`（全部必需）**，这不是没来得及标可选：图节点的第一件事就是
  `state["run_id"]` / `state["shop_id"]` 这类直取，缺键会**当场 KeyError**，而不是静默拿到
  `None` 之后在几十行后变成一个更难查的错误。所以"必需"在这里是一种**早失败**设计：
  `graph._initial_state` 必须把 19 个字段一次给全。
- 全字段必需还有一个后果：`TypedDict` 在运行期只是 `dict`，**不做任何校验**。`status` 标注
  成 `RunStatus`、实际写进去的是 `RunStatus.XXX.value`（字符串）——这是有意的（`StrEnum`
  就是 `str`，"值"与"枚举"在比较时等价），但 `isinstance` 检查会失败，不要拿类型注记当
  运行期保证。
- 唯一的 `NotRequired` 是 `ActionProposal.idempotency_key`，理由很具体：**提议阶段还没有
  最终参数**（人可能改价），幂等键必须在审批之后由"最终参数"算出（§4.5）。审批前它就
  该"缺省/None"，强制必需会逼出一串假键（例如用旧参数算出来的那份），而假键会让两次
  不同的改动共享同一个 `action_log` 行 —— 那正是幂等要防的事。
- 子结构（`Decision` / `ApprovalState` / `ExecutionResult` …）同样是全必需：它们的构造点
  都在本仓内部，缺字段属于 bug，不该用可选字段把它藏起来。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict

from app.enums import (
    ActionType,
    ApprovalStatus,
    Attribution,
    ErrorCategory,
    Risk,
    RunStatus,
    Severity,
    Verdict,
)

# --------------------------------------------------------------------------------------
# 子结构（§3.3 逐字）
# --------------------------------------------------------------------------------------


class Window(TypedDict):
    """采集窗口（业务日期，`Asia/Shanghai` 的 `YYYY-MM-DD`，两端都含）。

    保证：`start <= end`，且都是**业务日期**而不是 UTC 时间戳。
    不保证：不保证窗口长度是 8 天——长度由 `collect` 的 `HISTORY_WINDOW_DAYS` 决定，
        本结构只是载体。
    调用方坑：跨时区把它当时间戳比较会得到错误结论；要转 UTC 请显式走
        `_business_date_of` / `business_date`，不要就地加减。
    """

    start: str  # 业务日期 YYYY-MM-DD (Asia/Shanghai)
    end: str


class RawSnapshot(TypedDict):
    """一次采集的原始快照（§3.3）：指标的唯一输入。

    保证：`source.partial=True` 时这份快照**不是完整的一份**（有降级/拒绝/上游失败），
        消费方必须能看出来。
    不保证：不保证 `products`/`order_summary` 非空——上游没给就是空/零，不补假数据。
    调用方坑：`partial` 只在 `collect` 里置位；下游不要自己改它（它描述的是"采集那一刻"）。
    """

    collected_at: str  # UTC ISO
    window: Window
    shop: dict[str, Any]  # 店铺基础信息（原样字段 + 归一化字段）
    # 每个商品/规格一条：金额已归一化成 `*_cents`（整数分），形状见 §5.5 `PRODUCT_FIELDS`。
    # 采集降级时可能是空列表——空列表 ≠ "这家店没有商品"。
    products: list[dict[str, Any]]  # 每个商品/规格一条，金额已归一化为 *_cents
    # 订单聚合（含 `by_date`）：指标的唯一来源；`collect` 失败时整个 raw 都不存在。
    order_summary: dict[str, Any]  # 订单聚合（金额 *_cents）
    # `{"methods": [...], "request_ids": [...], "partial": bool}`：`partial` 是"这份数据
    # 是否完整"的唯一标志（行级拒绝/件数降级/日期降级/上游失败都会置 True）。
    source: dict[str, Any]  # {"methods": [...], "request_ids": [...], "partial": bool}


class MetricsSnapshot(TypedDict):
    """一天的店铺级指标（§5.3）：`/api/metrics` 的 `MetricPoint` 与 checkpoint 里的历史。

    保证：`*_cents` 全是整数分；"算不出来"的比率一律 `None`（**绝不填 0**）。
    不保证：不保证每个键都有意义——`gross_margin=None` 可能是"没配成本"或"没有可归集
        成交额"，两者在本结构里不可区分（区分要看 `cost_coverage_ratio`）。
    调用方坑：`0` 与 `None` 是两个业务事实（0 = 等于零，None = 不知道）；前端禁止 `?? 0`。
    """

    date: str  # 业务日期 YYYY-MM-DD
    gmv_cents: int
    order_count: int  # 下单数
    paid_order_count: int  # 支付数
    refund_order_count: int
    refund_amount_cents: int
    # 客单价：分母 0 时给 0（不是 None）——"没有支付订单"时客单价就是 0，
    # 与"不知道"不同（后者用 None）。
    aov_cents: int  # gmv_cents / paid_order_count，分母 0 => 0
    # 退款率：分母 0 → None（填 0 会读成"退款率 0%，很健康"，而事实是无从谈起）。
    refund_rate: float | None  # refund_order_count / paid_order_count，分母 0 => None
    uv: int | None  # 访客数，无数据 => None
    cvr: float | None  # paid_order_count / uv，任一缺失 => None
    #: 近 7 日（不含今天）均值：历史不足 3 天 → None（用 1 天算"7 日均值"是假精确）。
    gmv_7d_avg_cents: int | None  # 近 7 日（不含今天）均值，历史不足 => None
    # 成本口径（v1.5.0，README 已知限制 4 计划中的"店铺级加权毛利率"）：
    # 只对**配了成本**的 SKU 计算，成本未知的 SKU 既不进分子也不进分母。
    # 没有成本 / 没有按 SKU 归集 / 分母为 0 => None（**绝不填 0**：0 会把"没配成本"判成亏本）。
    gross_margin: float | None  # (有成本成交额 - 这些 SKU 的成本额) / 有成本成交额
    cost_coverage_ratio: float | None  # 有成本 SKU 的成交额占全店 GMV 的比例（暴露覆盖不全）
    computed_at: str


class Anomaly(TypedDict):
    """一条异常（§5.4）：规则求值的输出，也是"为什么提议这个动作"的证据。

    保证：`anomaly_id` 由 `sha256(run_id|rule_id|metric)[:16]` 生成，同一 run 里唯一且
        可复现（重跑同样的数据得到同样的 id，去重才有意义）。
    不保证：不保证 `observed`/`threshold` 同类型——规则可能比整数也可能比浮点，
        `None` 表示"这个指标算不出来"（不是 0）。
    调用方坑：`severity` 决定日志 `level`（critical→error），别在别处再写一份映射。
    """

    anomaly_id: str  # sha256(f"{run_id}|{rule_id}|{metric}")[:16]
    rule_id: str
    metric: str
    observed: float | int | None
    threshold: float | int | None
    comparator: str  # "<" "<=" ">" ">=" "==" "!=" 或 "and"/"or" 组合的可读摘要
    severity: Severity
    message: str
    window: str  # "day" / "7d"
    detected_at: str


class ActionProposal(TypedDict):
    """一条动作提议（§5.5）：审批台的展示对象，也是执行阶段的输入。

    保证：`action_id` 在提议阶段就定下来（`run_id:action_type:target`），审批与执行都靠它
        对齐；`params` 已按动作类型归一化（金额一律 `*_cents`）。
    不保证：`idempotency_key` 一定在——它是 `NotRequired`，**审批前必须缺省/None**（§4.5：
        人可能改参数，键只能在"参数定稿"之后算）。
    调用方坑：不要用提议时的参数自己算一个幂等键塞进来——那样改价 9900→9600 会命中同一行
        `action_log`，两次不同的改动被当成一次。
    """

    action_id: str  # f"{run_id}:{action_type}:{target}"（提议身份，审批前生成）
    action_type: ActionType
    target: str  # sku_id 或 product_id
    params: dict[str, Any]  # 归一化后的生效参数（金额用 *_cents）
    idempotency_key: NotRequired[str | None]  # 审批前必须缺省/None；执行阶段由 §4.5 计算
    reason: str
    evidence: dict[str, Any]  # {"anomaly_id":..., "rule_id":..., "metrics": {...}}
    risk: Risk
    created_at: str


class Decision(TypedDict):
    """一条人工裁决（§4.4）：`verdict` 决定执行路径，`edited_params` 只在 edit 时有值。

    保证：`decided_at` 一定非空（服务端在缺省时补 `utc_iso()`）。
    不保证：不保证 `operator`/`reason` 有值——两者都是可选描述，不参与任何分支判断。
    调用方坑：`verdict` 在运行期是字符串（`Verdict` 是 StrEnum），用 `is Verdict.EDIT`
        比较时要确认拿到的确实是枚举实例（`_validate_decisions_shape` 会转成枚举）。
    """

    action_id: str
    verdict: Verdict
    edited_params: dict[str, Any] | None
    operator: str | None
    reason: str | None
    decided_at: str


class ApprovalState(TypedDict):
    """审批的聚合状态（§3.3）：既记录"什么时候挂的"，也记录"谁批的"。

    保证：`decisions` 与当次挂起点的 `pending_actions` 一一对应（集合相等，§4.4 规则 2）。
    不保证：`interrupt_id` 可能一直是 `None`——节点拿不到自己的 interrupt id（它由 LangGraph
        生成），只能由 `resume_agent` / `run_agent` 事后回填到 `approval_request` 表里。
    调用方坑：`decided_by` 只记**第一条**裁决的操作人；多人分别操作时完整名单在 `decisions`，
        不要把它当成"这次审批的全部参与者"。
    """

    interrupt_id: str | None
    requested_at: str | None
    decided_at: str | None
    decided_by: str | None
    decisions: list[Decision]


class ExecutionResult(TypedDict):
    """一条执行回执（§4.5）：`action_log` 那一次尝试在 state 里的投影。

    保证：`idempotency_key` 非空（每次执行都由最终参数算出）；`status` 是 `ActionLogStatus`。
    不保证：不保证 `upstream_request_id` 有值——skip/unknown 两条路径拿不到它。
    调用方坑：`status="succeeded"` 不代表"对生意有正面效果"，只是"上游接受了这次调用"；
        效果是 `effect_report` 的事，两者不可互相替代。
    """

    action_id: str
    idempotency_key: str
    status: str  # ActionLogStatus
    upstream_request_id: str | None
    error: str | None
    error_category: str | None  # ErrorCategory
    attempt: int
    executed_at: str


class EffectWindow(TypedDict):
    """一个效果窗口（T+0/1/3/7）。`status` 三态：`scheduled`（还没到）/`evaluated`（采过）/
    `missed`（到点了但没采成）。

    保证：`offset_days` 是**整数天**（离散枚举），`due_at` 是**绝对时刻**——两者都落盘，
        因为图内不等待（ADR-003），窗口是靠 `due_at <= now` 被调度器捞起来的。
    不保证：不保证被采样到——`missed` 就是"没看成"，它和 `scheduled` 是两个事实。
    调用方坑：不要把 `status` 压成 bool 展示（会把"漏采"画成"待采"）。
    """

    offset_days: int  # 0 / 1 / 3 / 7
    due_at: str
    snapshot_id: str
    status: str  # "scheduled" | "evaluated" | "missed"


class EffectReport(TypedDict):
    """效果报告（§5.9）：只描述"登记了哪些窗口 + 归因强度"，**不给因果结论**。

    保证：`conclusion` 符合 §5.9.3 的文案规则（无对照组时不许出现因果断言词）；
        无对照组时 `caveats` 同时挂"季节性"与"归因强度"两条。
    不保证：不保证 `windows` 里已经有采样结果——图刚跑完时它们都是 `scheduled`。
    调用方坑：`attribution="single_arm_before_after"` 意味着"前后对比"，不是 A/B；
        拿它当因果证据会得出错误结论。
    """

    run_id: str
    # 报告生成时刻（UTC ISO）：不是"最后一次采样"的时刻——窗口的采样由调度器写。
    generated_at: str
    # 归因强度：`single_arm_before_after`（前后对比）/`ab_test`/`none`。
    attribution: Attribution
    windows: list[EffectWindow]
    conclusion: str  # 必须符合 §5.9.3 文案规则
    # 无对照组时必须同时挂"季节性"与"归因强度"两条：一条字符串会让调用方自己拆分隔符。
    caveats: list[str]


class AgentError(TypedDict):
    """一条**非致命**错误（§3.3）：进 `state.errors`，不改变流程走向。

    保证：`retryable` 由 §9.1 的类别表得出（不手写），因此"能不能重试"只有一个事实源。
    不保证：不保证这条错误会让 run 失败——判死是 `status=failed` 的事。
    调用方坑：去重键是 `(node, category, message)`，改措辞等于换一条错误（重放会变两条）。
    """

    node: str
    category: str  # ErrorCategory
    message: str
    retryable: bool
    at: str


class AuditEntry(TypedDict):
    """一条审计事件（§3.3）：`state.audit` 的元素。

    保证：`event_id` = `sha256(json(node, event, data))[:16]`，**刻意不含 `at`**——节点重放时
        同一逻辑事件必须得到同一个 id，否则去重失效（见 `make_event_id`）。
    不保证：不保证有 JSONL 出口——只写 `state.audit` 的事件只有"读 checkpoint 的人"看得见。
    调用方坑：`data` 参与哈希，所以往里加字段等于换 id（重放后去重会失效）；要加键就一次性加。
    """

    event_id: str  # sha256(json(event))[:16]，去重键
    at: str
    node: str
    event: str  # "node.start" / "anomaly.detected" / "action.proposed" ...
    data: dict[str, Any]


class AgentState(TypedDict):
    """整个 run 的状态（§3.2）：LangGraph 的唯一数据载体，19 个字段全部**必需**。

    ## 为什么全必需（`total=True`）

    节点第一句就直取 `state["run_id"]` / `state["shop_id"]`；把键标成可选会让"漏建 state"
    从 KeyError（立即、指到出错的节点）变成 `None` 顺着往下传（几十行后炸在一个无关的地方）。
    `graph._initial_state` 因此必须把 19 个字段一次给全 —— 那是本类型的**唯一**首次构造点。

    保证：`status` 是 `RunStatus` 的**值**（字符串）；三个列表（`errors`/`audit`/`history`）
        一定存在且是列表（空列表 ≠ 缺失）。
    不保证：不保证"有时序"——除了 `history`，其余字段都是"当前值"，历史要看 checkpoint。
    调用方坑：**replace 语义**（§3.1）：往 `state["errors"]` 里 append 不会生效，必须返回
        完整新列表；`append_audits` / `append_errors` 就是为此存在的。
    """

    run_id: str
    trace_id: str
    shop_id: str
    thread_id: str
    trigger: str
    started_at: str
    status: RunStatus
    iteration: int
    raw: RawSnapshot | None
    metrics: MetricsSnapshot | None
    # 近 8 个业务日的指标序列（含今天）：`compute_metrics` 逐日 append，最多留 9 条。
    # 是唯一"带时序"的字段，其余字段都只表达"当前值"。
    history: list[MetricsSnapshot]
    anomalies: list[Anomaly]
    # 提议 → 审批 → 执行的三段：`pending_actions` 是提议快照，审批后**不清空**；
    # `approved_actions`（含 edit 后的最终参数）才是 `execute` 的输入；拒绝的只进
    # `rejected_actions`（不产 `action_log`，所以永远没有幂等键）。
    pending_actions: list[ActionProposal]
    approved_actions: list[ActionProposal]
    rejected_actions: list[ActionProposal]
    # 审批聚合状态：`interrupt_id` 在节点内恒为 None，由入口回填进 `approval_request`。
    approval: ApprovalState | None
    # 每个动作一条回执（含 skip/unknown）：`execute` 的唯一输出，也是 record_effect 的输入。
    execution_results: list[ExecutionResult]
    # 效果报告：只有**执行过动作**才有（None = 没执行，不是"执行了没效果"）。
    effect_report: EffectReport | None
    # 非致命错误（不改变流程走向）：判死靠 `status`，不靠这个列表非空。
    errors: list[AgentError]
    # 审计留痕（checkpoint 里可回放）：`append_audits` 按 event_id 去重后返回完整列表。
    audit: list[AuditEntry]


# --------------------------------------------------------------------------------------
# 对外返回值数据类（§4.1 逐字）
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingApproval:
    """一条待审批项（§4.1）：`resume_agent` / `/api/alerts` 的对外形状。

    保证：`frozen=True`（拿到就不能改，避免"审批台展示的内容"与"落库的内容"分叉）。
    不保证：不保证 `interrupt_id` 非空——它是事后回填的，空串表示"还没关联到挂起点"。
    调用方坑：`action_id` 是唯一身份（§6.1 没有 `approval_id`）；拿 `run_id` 当身份会在
        "一轮多个提议"时直接把两条待办混成一条。
    """

    action_id: str  # 批准对象的唯一身份 = approval_request.action_id（§5.8，不另设 approval_id）
    run_id: str
    thread_id: str
    shop_id: str
    trace_id: str
    # 空串 = 还没回填（行的落库发生在 interrupt() 之前）；空串时点审批必然 404。
    interrupt_id: str
    action_type: str
    target: str
    # `params`/`evidence` 是动作类型相关的自由字段（改价看 price_cents、改库存看 stock）：
    # §7 只冻结了外层键名，强类型会逼领域层认得每种动作。
    params: dict[str, Any]
    reason: str
    evidence: dict[str, Any]
    risk: str
    status: str  # ApprovalStatus
    created_at: str


@dataclass(frozen=True)
class AppliedDecision:
    """一条裁决的执行结果（§7.4 `applied[]`）：回应"我批的这条最后怎么样了"。

    保证：`reject` 的 `status` 固定 `"rejected"` 且 `action_log_id=None`（没执行就没有日志行）。
    不保证：不保证 `action_log_id` 一定有值——`pending` / `skipped_duplicate` 都可能是 `None`。
    调用方坑：`action_log_id` **等于** `idempotency_key`（§6.1 只有一套 ID），不要再去查第二套。
    """

    action_id: str
    verdict: str
    status: str  # ActionLogStatus
    action_log_id: str | None
    error: str | None


@dataclass(frozen=True)
class RunResult:
    """`run_agent` 的返回值（§4.1 / §7.2 `RunResponse` 的领域形状）。

    保证：`status` 是恢复后的真实 run 状态（可能是 `awaiting_approval`，不是"一定跑完"）。
    不保证：`state` 可能为 `None`（checkpoint 不存在时的回放）。
    调用方坑：`pending_approvals` 默认空列表而不是 `None`：空列表 = "这次没有待办"，
        不要在入口层把它转成 `null`（两者语义不同）。
    """

    run_id: str
    thread_id: str
    trace_id: str
    status: str  # RunStatus
    # 默认空列表（不是 None）：空列表 = "这次没有待办"，None 会逼前端多一个状态分支。
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    effect_report: EffectReport | None = None
    # 完整的 state（回放时可能是 None）：只给调试/深度集成用，HTTP 响应里不出现。
    state: AgentState | None = None


@dataclass(frozen=True)
class ResumeResult:
    """`resume_agent` 的返回值（§4.1 / §7.4 `ApproveResponse` 的领域形状）。

    保证：`applied[]` 与提交的 `decisions` 一一对应（含被拒的那些）。
    不保证：不保证 run 就此终态——上游可能还有下一轮挂起（那样 `pending_approvals` 非空）。
    调用方坑：`effect_report=None` 与 `windows=[]` 是两件事（前者没执行过动作，后者登记了 0 个窗口）。
    """

    run_id: str
    thread_id: str
    trace_id: str
    status: str
    # 与请求里的 `decisions` 一一对应（不是"只有成功的那些"）：被拒的也在里面。
    applied: list[AppliedDecision] = field(default_factory=list)
    # 恢复后**新一轮**的待办（可能为空）：清空 ≠ 这一轮结束了，要看 `status`。
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    effect_report: EffectReport | None = None
    state: AgentState | None = None


# --------------------------------------------------------------------------------------
# 时间 / 序列化工具（§0：UTC ISO-8601 带 Z，毫秒可选）
# --------------------------------------------------------------------------------------

# ISO-8601 的时区后缀：`Z` 或 `+HH:MM` / `+HHMM`（`datetime.fromisoformat` 认前两种写法）。
_ISO_Z_RE = re.compile(r"^(?P<body>.+?)(?P<tz>Z|[+-]\d{2}:?\d{2})$")
# `shop_id` / `run_id` 的字符集（§7.2）：不含冒号，`thread_id` 才能安全反解。
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def utcnow() -> datetime:
    """当前时间，**总是**带 tzinfo 的 UTC（禁止 naive）。

    保证：返回值 `tzinfo` 非空，可直接与其它 aware datetime 比较/相减。
    不保证：不保证单调——系统时钟被校正（NTP）时会跳；计时请用 `time.perf_counter`。
    调用方坑：写库前不用它（ORM 的 `_utcnow` 自己取）；这里主要给"显式传时间"的路径
        （测试注入、`update_run_trace(now=...)`）用，保证两边取到同一份时间基准。
    """
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    """datetime -> `2026-10-23T14:25:30Z`（毫秒只在非零时保留）。"""
    dt = value or utcnow()
    if dt.tzinfo is None:
        # naive 输入按 UTC 解释（本仓所有时间都是 UTC）；不这样兜会抛 ValueError，
        # 而调用方往往只是传了一个从库里读出来忘了过 `to_utc()` 的 datetime。
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    if dt.microsecond:
        # 毫秒只在非零时保留：`...:30Z` 与 `...:30.000Z` 的字符串比较不相等，
        # 但业务上是同一时刻，所以统一"整秒不带毫秒"。
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(value: str) -> datetime:
    """解析 §0 约定的时间字符串。非法输入抛 ValueError（调用方决定包成哪个应用异常）。

    保证：返回 UTC aware datetime；`Z` / `+08:00` / `+0800` 三种时区写法都认。
    不保证：不保证输入是"业务时间"——它可以是任意时区，这里只做折算。
    调用方坑：本函数抛的是裸 `ValueError`（不是 AppError）：入口层要自己包成 400/500
        （`api/convert.py` 用的是"炸成 500"：坏时间不该被静默替换成"现在"）。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"not an ISO-8601 timestamp: {value!r}")
    text = value.strip()
    match = _ISO_Z_RE.match(text)
    if match:
        # 先把 `Z` / `+0800` 规范成 `fromisoformat` 认的形式（老版本 CPython 不认 `Z`，
        # 也不认不带冒号的偏移），否则"合法的 ISO-8601"会在这里被误判成非法。
        body, tz = match.group("body"), match.group("tz")
        if tz == "Z":
            text = body + "+00:00"
        elif ":" not in tz:
            text = f"{body}{tz[:3]}:{tz[3:]}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # 没带时区的时间串按 UTC：§0 的约定是不出现 naive，但历史上确实写进来过。
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def canonical_json(value: Any) -> str:
    """§4.5：`json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`。

    保证：同一份逻辑内容**永远**得到同一个字符串（键排序、无多余空格、中文不转义）。
        这是所有摘要/幂等键可复现的前提。
    不保证：不保证能序列化任意对象（datetime/Enum 会抛）：调用方先转成 JSON 原生类型。
    调用方坑：不要在这里加 `default=str` 之类的兜底——那会让"两种不同对象"静默变成
        同一个字符串（幂等键就失效了）。
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    """`text` 的 sha256 十六进制摘要（全 64 位；要 16 位前缀请在外面切）。

    保证：UTF-8 编码后求摘要，中文/emoji 都稳定。
    不保证：不做任何脱敏——传进来的必须是已经脱敏过的内容（见 `digest_of` 的调用约定）。
    调用方坑：入参必须是 `str`；传 dict 会抛 `TypeError`（先过 `canonical_json`）。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# 审计与错误累积（不用 reducer，返回完整列表）
# --------------------------------------------------------------------------------------


def make_event_id(node: str, event: str, data: dict[str, Any]) -> str:
    """`event_id` = sha256(json(event))[:16]。

    **刻意不含 `at`**：节点会被重放（ADR-010），若时间参与哈希，同一次逻辑事件在重放后
    会得到新的 `event_id`，去重就失效了，`audit` 里会出现两条"看起来发生了两次"的记录。
    哈希输入固定为 `(node, event, data)` —— 同一逻辑事件永远同一 id。
    """
    # 只取前 16 位：碰撞概率在"单次 run 的审计条数"量级上可忽略，而短 id 便于日志阅读/比对。
    return sha256_hex(canonical_json({"node": node, "event": event, "data": data}))[:16]


def make_audit_entry(
    *,
    node: str,
    event: str,
    data: dict[str, Any] | None = None,
    at: str | None = None,
) -> AuditEntry:
    payload = dict(data or {})
    return AuditEntry(
        # `at` 不进 `event_id` 的哈希（见 `make_event_id`）：重放时时间会变，id 不能变。
        event_id=make_event_id(node, event, payload),
        at=at or utc_iso(),
        node=node,
        event=event,
        # 存一份拷贝：调用方之后改自己的 dict 不该影响已经进审计的那条。
        data=payload,
    )


def append_audit(state: AgentState, entry: AuditEntry) -> list[AuditEntry]:
    """按 `event_id` 去重追加，返回**完整**新列表（不要用 reducer）。

    保证：幂等——同一个 `event_id` 追加两次，第二次原样返回旧列表。
    不保证：不保证顺序是"发生顺序"（重放时事件会被重新 append，顺序按到达先后）。
    调用方坑：返回值必须赋给 `state["audit"]`；直接 `state["audit"].append(...)` 会被
        LangGraph 的 replace 语义丢弃（而且改的是 checkpoint 里的对象）。
    """
    existing = list(state.get("audit") or [])
    event_id = entry["event_id"]
    if any(item.get("event_id") == event_id for item in existing):
        return existing
    return [*existing, entry]


def append_audits(state: AgentState, entries: list[AuditEntry]) -> list[AuditEntry]:
    """:func:`append_audit` 的批量版本（顺序保留，同样按 `event_id` 去重）。

    保证：返回完整新列表；批内与批间都按 `event_id` 去重。
    不保证：不保证 `entries` 内部无重复——这里会顺手去重（`seen` 集合跨两批共用）。
    调用方坑：`state` 只需要有 `audit` 键（传 `{"audit": audits}` 即可循环使用）；
        传真正的 `AgentState` 也行，但同样返回新列表、不改入参。
    """
    result = list(state.get("audit") or [])
    seen = {item.get("event_id") for item in result}
    for entry in entries:
        if entry["event_id"] in seen:
            continue
        seen.add(entry["event_id"])
        result.append(entry)
    return result


def make_agent_error(
    *,
    node: str,
    category: ErrorCategory | str,
    message: str,
    retryable: bool,
    at: str | None = None,
) -> AgentError:
    return AgentError(
        node=node,
        # `category` 存字符串（不是枚举实例）：这一份要进 checkpoint 序列化，
        # 存 StrEnum 也能过，但读回来的类型会依赖 serde 的 allowlist（见 checkpoint.py）。
        category=str(category),
        message=message,
        # 强制 bool：上游可能传 0/1，`retryable=1` 在 JSON 里会变成数字，前端判真值时容易踩。
        retryable=bool(retryable),
        at=at or utc_iso(),
    )


def append_errors(state: AgentState, errors: list[AgentError]) -> list[AgentError]:
    """追加错误，按 `(node, category, message)` 去重 —— 重放不会让同一条错误翻倍。

    保证：返回完整新列表；同一 `(node, category, message)` 只保留一条（后者被丢弃）。
    不保证：不保证 `at` 是最早那条的时间（保留先到的那条，时间自然是早的）。
    调用方坑：去重键**不含** `at`（否则重放必翻倍）；也不含 `retryable`——同一个错误
        不该既标可重试又标不可重试，真出现说明上游有 bug，保留第一条即可。
    """
    result = list(state.get("errors") or [])
    seen = {(e["node"], e["category"], e["message"]) for e in result}
    for error in errors:
        key = (error["node"], error["category"], error["message"])
        if key in seen:
            continue
        seen.add(key)
        result.append(error)
    return result


# --------------------------------------------------------------------------------------
# 标识符工具（§0 / §7.2）
# --------------------------------------------------------------------------------------


def thread_id_for(shop_id: str, run_id: str) -> str:
    """`f"{shop_id}:{run_id}"`。两侧字符集都不含冒号，因此 thread_id 可安全反解。

    保证：非法 `shop_id`/`run_id`（含冒号/超长/空串）抛 `ValueError`。
    不保证：不保证 thread 在 checkpointer 里真的存在（它只是个名字）。
    调用方坑：这是**唯一**生成 thread_id 的地方；自己拼 `f"{shop}:{run}"` 会绕过校验，
        带冒号的 id 会让反解出错（checkpoint 的线程身份就乱了）。
    """
    if not _RUN_ID_RE.match(shop_id or ""):
        # 校验字符集不是为了好看：`thread_id` 靠冒号分隔，两侧一旦允许冒号就无法反解，
        # 而 checkpoint 的线程身份、日志过滤都依赖这个可反解性。
        raise ValueError(f"invalid shop_id: {shop_id!r}")
    if not _RUN_ID_RE.match(run_id or ""):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return f"{shop_id}:{run_id}"


def new_run_id(now: datetime | None = None) -> str:
    """§7.2：`run_{YYYYMMDDTHHMMSSZ}_{8hex}`。

    保证：格式固定、可按字典序排时间；8 hex 随机后缀让"同一秒内两次调用"不撞。
    不保证：**不保证唯一**——它只是"足够不可能撞"，真正的唯一性约束在
        `agent_traces.UNIQUE(shop_id, run_id)`（撞了会 insert-if-absent 跳过）。
    调用方坑：不要把 `run_id` 当时间戳解析（前缀是 UTC，但业务日期是上海）；要重跑请显式
        传同一个 `run_id`（它就是幂等键），别指望新生成的 id 会"重新跑一次"。
    """
    stamp = (now or utcnow()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{secrets.token_hex(4)}"


def business_date(now: datetime | None = None) -> str:
    """§0：业务日期是 `Asia/Shanghai` 的 `YYYY-MM-DD`（不受进程本地时区影响）。"""
    from zoneinfo import ZoneInfo

    # 显式 `Asia/Shanghai` 而不是 `now().date()`：进程可能部署在 UTC 机器上，
    # 用本地日期会让"今天"在跨零点后错一天（业务日口径必须与部署环境无关）。
    return (now or utcnow()).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def pending_action_ids(state: AgentState) -> list[str]:
    """`state.pending_actions` 的 `action_id` 列表（顺序保持提议顺序）。

    保证：只读；返回新列表，改它不影响 state。
    不保证：不保证这些 id 在库里仍是 pending（要查库请用 `approvals.assert_all_pending`）。
    调用方坑：这是**提议**的 id 列表，不是"待处理"的保证——审批之后 `pending_actions`
        不会被清空（它是那一轮提议的快照）。
    """
    return [proposal["action_id"] for proposal in state.get("pending_actions") or []]


__all__ = [
    "ActionProposal",
    "AgentError",
    "AgentState",
    "Anomaly",
    "AppliedDecision",
    "ApprovalState",
    "Attribution",
    "AuditEntry",
    "Decision",
    "EffectReport",
    "EffectWindow",
    "ExecutionResult",
    "MetricsSnapshot",
    "PendingApproval",
    "RawSnapshot",
    "ResumeResult",
    "RunResult",
    "Window",
    "append_audit",
    "append_audits",
    "append_errors",
    "business_date",
    "canonical_json",
    "make_agent_error",
    "make_audit_entry",
    "make_event_id",
    "new_run_id",
    "parse_utc_iso",
    "pending_action_ids",
    "sha256_hex",
    "thread_id_for",
    "utc_iso",
    "utcnow",
]
