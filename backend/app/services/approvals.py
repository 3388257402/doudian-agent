"""`approval_request` 仓储 —— 对应 docs/CONTRACT.md §5.8。

这张表是**索引**，不是权威状态（ADR-002：权威状态在 checkpoint；两者不一致时以 checkpoint
为准）。它存在的唯一理由是"审批台要能在不读 checkpoint 的情况下列出待办"。

两条语义决定了整个 HITL 的正确性，改动前先读 ADR-010：

1. **`upsert_proposals` 是 insert-if-absent，已存在时什么都不做**。`approval_gate` 在 resume
   时会被重放（LangGraph 从节点开头重跑），若 upsert 覆盖 `status=approved` → `pending`，
   审批台上会冒出一条"审批完成却又在等审批"的幽灵待办。
2. **`interrupt_id` 只能回填**（`WHERE interrupt_id=''`）。`interrupt()` 是阻塞的，它的 id
   在恢复后才拿得到，而落库发生在 `interrupt()` 之前（§5.8 的时序）。

所有函数按契约声明为 `async def`，但内部用的是**同步** `Session`（§6.4：ORM 是同步
SQLAlchemy 2.x）。调用方在 async 节点里用 `session_scope()` 拿 session 后直接 await 这些函数，
不要跨 await 长时间持有 session。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.state import (
    ActionProposal,
    Decision,
    PendingApproval,
    canonical_json,
    parse_utc_iso,
    utcnow,
)
from app.enums import ApprovalStatus, Verdict
from app.errors import ApprovalNotFound, DuplicateDecision, ValidationError
from app.models import AgentTrace, ApprovalRequest
from app.services.actions import normalize_params, payload_digest, validate_params


def _as_pending_approval(row: ApprovalRequest) -> PendingApproval:
    return PendingApproval(
        action_id=row.action_id,
        run_id=row.run_id,
        thread_id=row.thread_id,
        shop_id=row.shop_id,
        trace_id=row.trace_id,
        interrupt_id=row.interrupt_id or "",
        action_type=row.action_type,
        target=row.target,
        params=dict(row.params or {}),
        reason=row.reason,
        evidence=dict(row.evidence or {}),
        risk=row.risk,
        status=row.status,
        created_at=_iso(row.created_at),
    )


def _iso(value: datetime | None) -> str:
    from app.db import to_utc

    if value is None:
        return ""
    resolved = to_utc(value)
    return resolved.isoformat().replace("+00:00", "Z") if resolved else ""


def _trace_id_for_run(session: Any, *, shop_id: str, run_id: str) -> str:
    """`approval_request.trace_id` 是 NOT NULL，但 §5.8 的入参里没有 trace_id。

    它就在 `agent_traces` 那一行上（`run_agent` 在入图之前已经写入），所以这里查一次，
    而不是让调用方多传一个可能传错的参数。
    """
    row = session.execute(
        select(AgentTrace).where(
            AgentTrace.shop_id == shop_id, AgentTrace.run_id == run_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValidationError(
            f"agent_traces 里没有 ({shop_id}, {run_id}) 这一行，无法确定 trace_id",
            details={"shop_id": shop_id, "run_id": run_id},
        )
    return row.trace_id


async def upsert_proposals(
    *,
    session: Any,
    run_id: str,
    shop_id: str,
    thread_id: str,
    proposals: Sequence[ActionProposal],
) -> None:
    """insert-if-absent（§5.8）。已存在的 `action_id` **一个字节都不改**。"""
    if not proposals:
        return
    trace_id = _trace_id_for_run(session, shop_id=shop_id, run_id=run_id)
    now = utcnow()
    action_ids = [proposal["action_id"] for proposal in proposals]
    existing = {
        row.action_id
        for row in session.execute(
            select(ApprovalRequest).where(ApprovalRequest.action_id.in_(action_ids))
        ).scalars()
    }
    for proposal in proposals:
        if proposal["action_id"] in existing:
            continue
        session.add(
            ApprovalRequest(
                run_id=run_id,
                thread_id=thread_id,
                shop_id=shop_id,
                trace_id=trace_id,
                interrupt_id="",  # §5.8：恢复后由 backfill_interrupt_id 回填
                action_id=proposal["action_id"],
                action_type=str(proposal["action_type"]),
                target=proposal["target"],
                params=dict(proposal["params"]),
                payload_digest=payload_digest(
                    shop_id=shop_id,
                    action_type=proposal["action_type"],
                    target=proposal["target"],
                    params=proposal["params"],
                ),
                reason=proposal["reason"],
                evidence=dict(proposal["evidence"]),
                risk=str(proposal["risk"]),
                status=ApprovalStatus.PENDING.value,
                verdict=None,
                decided_by=None,
                decided_at=None,
                decision_reason=None,
                created_at=now,
                updated_at=now,
            )
        )
    try:
        session.flush()
    except IntegrityError:
        # 并发下别人先插了同一条：回滚后重查，已存在的跳过（§6.3 第 6 条禁止 ON CONFLICT）。
        session.rollback()
        existing_after = {
            row.action_id
            for row in session.execute(
                select(ApprovalRequest).where(ApprovalRequest.action_id.in_(action_ids))
            ).scalars()
        }
        missing = [item for item in action_ids if item not in existing_after]
        if missing:  # 重查后仍缺 = 不是并发冲突，而是真的写失败，必须炸
            raise ValidationError(
                "approval_request 写入失败且重查后仍缺失",
                details={"missing_action_ids": missing},
            )


async def backfill_interrupt_id(
    *, session: Any, action_ids: Sequence[str], interrupt_id: str
) -> None:
    """回填 `interrupt_id`，条件带 `AND interrupt_id = ''`（幂等且不覆盖）。"""
    if not action_ids or not interrupt_id:
        return
    now = utcnow()
    rows = session.execute(
        select(ApprovalRequest).where(ApprovalRequest.action_id.in_(list(action_ids)))
    ).scalars()
    for row in rows:
        if row.interrupt_id:
            continue
        row.interrupt_id = interrupt_id
        row.updated_at = now
    session.flush()


async def list_by_status(
    *,
    session: Any,
    status: ApprovalStatus | None = None,
    shop_id: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> list[PendingApproval]:
    """keyset 分页（`id < before_id`），按 `id DESC`。"""
    stmt = select(ApprovalRequest)
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == str(status))
    if shop_id is not None:
        stmt = stmt.where(ApprovalRequest.shop_id == shop_id)
    if before_id is not None:
        stmt = stmt.where(ApprovalRequest.id < int(before_id))
    stmt = stmt.order_by(ApprovalRequest.id.desc()).limit(max(1, int(limit)))
    return [_as_pending_approval(row) for row in session.execute(stmt).scalars()]


async def list_pending_for_run(*, session: Any, run_id: str) -> list[PendingApproval]:
    """按 run 取待审批（`run_agent` 返回 `RunResult.pending_approvals` 用）。

    `list_by_status` 的签名（§5.8）里没有 `run_id` 过滤，而"这一次 run 的待办"是入口返回值的
    必需内容，所以在同一仓储里补这一个查询，而不是让调用方拉 200 条再自己过滤。
    """
    rows = session.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.run_id == run_id, ApprovalRequest.status == ApprovalStatus.PENDING.value)
        .order_by(ApprovalRequest.id.asc())
    ).scalars()
    return [_as_pending_approval(row) for row in rows]


async def assert_all_pending(
    *, session: Any, thread_id: str, action_ids: Sequence[str]
) -> None:
    """任一 `action_id` 已裁决 → `DuplicateDecision`(409)；行不存在 → `ApprovalNotFound`(404)。"""
    if not action_ids:
        return
    rows = list(
        session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.thread_id == thread_id,
                ApprovalRequest.action_id.in_(list(action_ids)),
            )
        ).scalars()
    )
    by_id = {row.action_id: row for row in rows}
    missing = [item for item in action_ids if item not in by_id]
    if missing:
        raise ApprovalNotFound(
            "审批行不存在（可能不是这个 thread 的待办）",
            details={"thread_id": thread_id, "missing_action_ids": missing},
        )
    decided = [
        {"action_id": row.action_id, "status": row.status, "verdict": row.verdict}
        for row in rows
        if row.status != ApprovalStatus.PENDING.value
    ]
    if decided:
        raise DuplicateDecision(
            "存在已裁决的动作，拒绝重复裁决",
            details={"thread_id": thread_id, "decided": decided},
        )


async def mark_decided(
    *, session: Any, thread_id: str, decisions: Sequence[Decision]
) -> list[str]:
    """落裁决。返回被更新的 `action_id` 列表。

    `verdict=edit` 时用**审批后的最终参数**重算 `payload_digest`（§4.5）—— 这一列存在的意义
    就是"参数在审批时被改过"，不重算它等于把审计线索擦掉。
    """
    if not decisions:
        return []
    action_ids = [decision["action_id"] for decision in decisions]
    rows = list(
        session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.thread_id == thread_id,
                ApprovalRequest.action_id.in_(action_ids),
            )
        ).scalars()
    )
    by_id = {row.action_id: row for row in rows}
    missing = [item for item in action_ids if item not in by_id]
    if missing:
        raise ApprovalNotFound(
            "审批行不存在", details={"thread_id": thread_id, "missing_action_ids": missing}
        )
    already = [
        {"action_id": item, "status": by_id[item].status}
        for item in action_ids
        if by_id[item].status != ApprovalStatus.PENDING.value
    ]
    if already:
        raise DuplicateDecision(
            "动作已被裁决过", details={"thread_id": thread_id, "decided": already}
        )

    updated: list[str] = []
    for decision in decisions:
        row = by_id[decision["action_id"]]
        verdict = Verdict(str(decision["verdict"]))
        if verdict is Verdict.EDIT:
            edited = decision.get("edited_params")
            if not isinstance(edited, dict) or not edited:
                raise ValidationError(
                    "verdict=edit 必须带非空 edited_params",
                    details={"action_id": row.action_id},
                )
            final_params = validate_params(row.action_type, edited)
            row.params = final_params
            row.payload_digest = payload_digest(
                shop_id=row.shop_id,
                action_type=row.action_type,
                target=row.target,
                params=final_params,
            )
            row.status = ApprovalStatus.APPROVED.value
        elif verdict is Verdict.APPROVE:
            row.status = ApprovalStatus.APPROVED.value
        else:
            row.status = ApprovalStatus.REJECTED.value
        row.verdict = verdict.value
        row.decided_by = decision.get("operator")
        decided_at = decision.get("decided_at")
        row.decided_at = parse_utc_iso(decided_at) if isinstance(decided_at, str) else utcnow()
        row.decision_reason = decision.get("reason")
        row.updated_at = utcnow()
        updated.append(row.action_id)
    session.flush()
    return updated


def effective_params(row: PendingApproval | ApprovalRequest, decision: Decision) -> dict[str, Any]:
    """§4.5：`params_effective = verdict==edit ? edited_params : proposal.params`，并过归一化。"""
    if Verdict(str(decision["verdict"])) is Verdict.EDIT:
        edited = decision.get("edited_params") or {}
        return validate_params(row.action_type, edited)
    if isinstance(row, PendingApproval):
        return normalize_params(row.action_type, row.params)
    return normalize_params(row.action_type, dict(row.params or {}))


def canonical(params: dict[str, Any]) -> str:  # pragma: no cover - 便于调试/审计时打印
    return canonical_json(params)


__all__ = [
    "assert_all_pending",
    "backfill_interrupt_id",
    "effective_params",
    "list_by_status",
    "list_pending_for_run",
    "mark_decided",
    "upsert_proposals",
]
