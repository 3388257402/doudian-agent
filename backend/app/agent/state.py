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
    start: str  # 业务日期 YYYY-MM-DD (Asia/Shanghai)
    end: str


class RawSnapshot(TypedDict):
    collected_at: str  # UTC ISO
    window: Window
    shop: dict[str, Any]  # 店铺基础信息（原样字段 + 归一化字段）
    products: list[dict[str, Any]]  # 每个商品/规格一条，金额已归一化为 *_cents
    order_summary: dict[str, Any]  # 订单聚合（金额 *_cents）
    source: dict[str, Any]  # {"methods": [...], "request_ids": [...], "partial": bool}


class MetricsSnapshot(TypedDict):
    date: str  # 业务日期 YYYY-MM-DD
    gmv_cents: int
    order_count: int  # 下单数
    paid_order_count: int  # 支付数
    refund_order_count: int
    refund_amount_cents: int
    aov_cents: int  # gmv_cents / paid_order_count，分母 0 => 0
    refund_rate: float | None  # refund_order_count / paid_order_count，分母 0 => None
    uv: int | None  # 访客数，无数据 => None
    cvr: float | None  # paid_order_count / uv，任一缺失 => None
    gmv_7d_avg_cents: int | None  # 近 7 日（不含今天）均值，历史不足 => None
    computed_at: str


class Anomaly(TypedDict):
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
    action_id: str
    verdict: Verdict
    edited_params: dict[str, Any] | None
    operator: str | None
    reason: str | None
    decided_at: str


class ApprovalState(TypedDict):
    interrupt_id: str | None
    requested_at: str | None
    decided_at: str | None
    decided_by: str | None
    decisions: list[Decision]


class ExecutionResult(TypedDict):
    action_id: str
    idempotency_key: str
    status: str  # ActionLogStatus
    upstream_request_id: str | None
    error: str | None
    error_category: str | None  # ErrorCategory
    attempt: int
    executed_at: str


class EffectWindow(TypedDict):
    offset_days: int  # 0 / 1 / 3 / 7
    due_at: str
    snapshot_id: str
    status: str  # "scheduled" | "evaluated" | "missed"


class EffectReport(TypedDict):
    run_id: str
    generated_at: str
    attribution: Attribution
    windows: list[EffectWindow]
    conclusion: str  # 必须符合 §5.9.3 文案规则
    caveats: list[str]


class AgentError(TypedDict):
    node: str
    category: str  # ErrorCategory
    message: str
    retryable: bool
    at: str


class AuditEntry(TypedDict):
    event_id: str  # sha256(json(event))[:16]，去重键
    at: str
    node: str
    event: str  # "node.start" / "anomaly.detected" / "action.proposed" ...
    data: dict[str, Any]


class AgentState(TypedDict):
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
    history: list[MetricsSnapshot]
    anomalies: list[Anomaly]
    pending_actions: list[ActionProposal]
    approved_actions: list[ActionProposal]
    rejected_actions: list[ActionProposal]
    approval: ApprovalState | None
    execution_results: list[ExecutionResult]
    effect_report: EffectReport | None
    errors: list[AgentError]
    audit: list[AuditEntry]


# --------------------------------------------------------------------------------------
# 对外返回值数据类（§4.1 逐字）
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingApproval:
    action_id: str  # 批准对象的唯一身份 = approval_request.action_id（§5.8，不另设 approval_id）
    run_id: str
    thread_id: str
    shop_id: str
    trace_id: str
    interrupt_id: str
    action_type: str
    target: str
    params: dict[str, Any]
    reason: str
    evidence: dict[str, Any]
    risk: str
    status: str  # ApprovalStatus
    created_at: str


@dataclass(frozen=True)
class AppliedDecision:
    action_id: str
    verdict: str
    status: str  # ActionLogStatus
    action_log_id: str | None
    error: str | None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    thread_id: str
    trace_id: str
    status: str  # RunStatus
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    effect_report: EffectReport | None = None
    state: AgentState | None = None


@dataclass(frozen=True)
class ResumeResult:
    run_id: str
    thread_id: str
    trace_id: str
    status: str
    applied: list[AppliedDecision] = field(default_factory=list)
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    effect_report: EffectReport | None = None
    state: AgentState | None = None


# --------------------------------------------------------------------------------------
# 时间 / 序列化工具（§0：UTC ISO-8601 带 Z，毫秒可选）
# --------------------------------------------------------------------------------------

_ISO_Z_RE = re.compile(r"^(?P<body>.+?)(?P<tz>Z|[+-]\d{2}:?\d{2})$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def utcnow() -> datetime:
    """当前时间，**总是**带 tzinfo 的 UTC（禁止 naive）。"""
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    """datetime -> `2026-10-23T14:25:30Z`（毫秒只在非零时保留）。"""
    dt = value or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(value: str) -> datetime:
    """解析 §0 约定的时间字符串。非法输入抛 ValueError（调用方决定包成哪个应用异常）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"not an ISO-8601 timestamp: {value!r}")
    text = value.strip()
    match = _ISO_Z_RE.match(text)
    if match:
        body, tz = match.group("body"), match.group("tz")
        if tz == "Z":
            text = body + "+00:00"
        elif ":" not in tz:
            text = f"{body}{tz[:3]}:{tz[3:]}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def canonical_json(value: Any) -> str:
    """§4.5：`json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str) -> str:
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
        event_id=make_event_id(node, event, payload),
        at=at or utc_iso(),
        node=node,
        event=event,
        data=payload,
    )


def append_audit(state: AgentState, entry: AuditEntry) -> list[AuditEntry]:
    """按 `event_id` 去重追加，返回**完整**新列表（不要用 reducer）。"""
    existing = list(state.get("audit") or [])
    event_id = entry["event_id"]
    if any(item.get("event_id") == event_id for item in existing):
        return existing
    return [*existing, entry]


def append_audits(state: AgentState, entries: list[AuditEntry]) -> list[AuditEntry]:
    """:func:`append_audit` 的批量版本（顺序保留，同样按 `event_id` 去重）。"""
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
        category=str(category),
        message=message,
        retryable=bool(retryable),
        at=at or utc_iso(),
    )


def append_errors(state: AgentState, errors: list[AgentError]) -> list[AgentError]:
    """追加错误，按 `(node, category, message)` 去重 —— 重放不会让同一条错误翻倍。"""
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
    """`f"{shop_id}:{run_id}"`。两侧字符集都不含冒号，因此 thread_id 可安全反解。"""
    if not _RUN_ID_RE.match(shop_id or ""):
        raise ValueError(f"invalid shop_id: {shop_id!r}")
    if not _RUN_ID_RE.match(run_id or ""):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return f"{shop_id}:{run_id}"


def new_run_id(now: datetime | None = None) -> str:
    """§7.2：`run_{YYYYMMDDTHHMMSSZ}_{8hex}`。"""
    stamp = (now or utcnow()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{secrets.token_hex(4)}"


def business_date(now: datetime | None = None) -> str:
    """§0：业务日期是 `Asia/Shanghai` 的 `YYYY-MM-DD`（不受进程本地时区影响）。"""
    from zoneinfo import ZoneInfo

    return (now or utcnow()).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def pending_action_ids(state: AgentState) -> list[str]:
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
