"""`POST /api/approve` —— 人工裁决恢复挂起的 run（CONTRACT §7.4）。

三个要点，每个都对应一类真实事故：

1. **恢复是 `Command(resume={interrupt_id: payload})`**（§4.4 / §11 自检项）：用 list 或裸 payload
   都会让 LangGraph 找不到要恢复的中断，表现为"点了审批没反应"。
2. **`interrupt_id` 必须等于当前挂起中断的 id**：否则 404 `APPROVAL_NOT_FOUND`。
   这道闸挡住的是"拿旧审批页面点第二次"——不让旧裁决被喂给一个新的挂起点。
3. **重复裁决是 409 `DUPLICATE_DECISION`，不是 500、也不是 200**。第一次审批成功后 thread 不再
   挂起，`resume_agent` 会先撞上 `RUN_STATE_CONFLICT`；本层再用同一个仓储谓词
   （`approvals.assert_all_pending`）确认"这些 action_id 确实已被裁决"，从而回**更具体**的那个码。
   前端拿 `DUPLICATE_DECISION` 会刷新列表；拿 `RUN_STATE_CONFLICT` 会提示状态冲突——
   两者都是"刷新"，但前者的文案才是事实。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict

from fastapi import APIRouter

from app.agent import resume_agent
from app.agent.state import Decision as DecisionPayload
from app.agent.state import utc_iso
from app.api.convert import alert_items, effect_report
from app.db import session_scope
from app.errors import AppError, DuplicateDecision, RunStateConflict
from app.schemas import (
    AppliedDecision,
    ApproveRequest,
    ApproveResponse,
    ErrorEnvelope,
)
from app.services import approvals

__all__ = ["router"]

router = APIRouter(tags=["approval"])


async def _already_decided(*, thread_id: str, action_ids: Sequence[str]) -> bool:
    """这些 `action_id` 是否已裁决（复用 §5.8 的仓储谓词，不另写一套状态判断）。"""
    try:
        with session_scope() as session:
            await approvals.assert_all_pending(
                session=session, thread_id=thread_id, action_ids=action_ids
            )
    except DuplicateDecision:
        return True
    except AppError:
        # 行不存在 / 别的应用层错误：不是"被裁决过"，交回原异常（RUN_STATE_CONFLICT）
        return False
    return False


@router.post(
    "/approve",
    response_model=ApproveResponse,
    summary="提交审批裁决并恢复同一 thread",
    responses={
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        502: {"model": ErrorEnvelope},
    },
)
async def approve(body: ApproveRequest) -> ApproveResponse:
    """提交人工裁决并恢复**同一个**挂起 thread。

    输入边界：
        - `thread_id` 必填：必须对应一个当前处于 `awaiting_approval` 的 run（不是"任意历史 thread"）；
        - `interrupt_id` 必填：必须等于**当前挂起中断**的 id（旧审批页面手里的 id 会被拒）；
        - `operator` 必填 `^[A-Za-z0-9_.-]{1,64}$`；`decisions` 至少 1 条、`action_id` 不许重复；
        - `decisions` 覆盖的集合必须与挂起时的 `pending_actions` **完全相等**：多一个少一个都 400，
          不按位置配对、不静默忽略（§4.4 校验规则 2）；`edit` 必须带非空 `edited_params`，其它 verdict
          必须为 `null`（不做"多给就忽略"的宽容）；`decided_at` 由服务端盖，不吃客户端时间。

    错误：
        400 VALIDATION_ERROR（形状/枚举/重复 action_id/集合不一致/edit 参数不符 schema）
        404 THREAD_NOT_FOUND（checkpointer 里没有这个 thread：从没跑过，或 checkpoint 被清理）
        404 APPROVAL_NOT_FOUND（`interrupt_id` 与当前挂起中断不一致：已过期、或被别人先批了）
        409 DUPLICATE_DECISION（这些 action_id 已裁决——含"同一页面点第二次"）
        409 RUN_STATE_CONFLICT（thread 存在但当前不挂起，且动作也没被裁决过）
        502 UPSTREAM_ERROR（恢复后执行动作时上游抖店不可用；**裁决此时已经落库**，不要重复提交，
            按 §9.3 的结果对账处理）

    调用方坑：
        - 必须把 `interrupt_id` 原样传下来（本端点再传给 `resume_agent(expected_interrupt_id=...)`）：
          漏掉它等于放弃"这次审批对应哪个挂起点"的唯一闸门，并发场景下会把旧裁决批到新决策上，
          而且**不会报错**；
        - 提交是**全量**的：只想改一条也要把同一挂起点的所有 `action_id` 一起带上（少一条就 400）；
        - 409 之后不要用同一份 body 重试（只会继续 409）：去刷新 `/api/alerts` 拿最新状态；
        - 成功返回的 `status` 可能是 `done` 也可能是 `awaiting_approval`（上游还有下一轮挂起时），
          按返回值决定下一步，别硬编码 `done`。
    """
    decided_at = utc_iso()
    payload: list[DecisionPayload] = [
        DecisionPayload(
            action_id=item.action_id,
            verdict=item.verdict,  # type: ignore[typeddict-item]  # TypedDict 运行期就是 str
            edited_params=dict(item.edited_params) if item.edited_params is not None else None,
            # 缺省用顶层 operator（§7.4 表格：decisions[].operator 可缺省）
            operator=item.operator or body.operator,
            reason=item.reason,
            decided_at=decided_at,
        )
        for item in body.decisions
    ]
    try:
        result = await resume_agent(
            body.thread_id, payload, expected_interrupt_id=body.interrupt_id
        )
    except RunStateConflict as conflict:
        if await _already_decided(
            thread_id=body.thread_id, action_ids=[item.action_id for item in body.decisions]
        ):
            raise DuplicateDecision(
                "这些动作已被裁决过（同一条动作不能裁决两次）",
                details={
                    "thread_id": body.thread_id,
                    "action_ids": [item.action_id for item in body.decisions],
                },
            ) from conflict
        raise

    return ApproveResponse(
        thread_id=result.thread_id,
        run_id=result.run_id,
        trace_id=result.trace_id,
        status=result.status,  # type: ignore[arg-type]
        applied=[AppliedDecision.model_validate(asdict(item)) for item in result.applied],
        pending_approvals=alert_items(result.pending_approvals),
        effect_report=effect_report(result.effect_report),
    )
