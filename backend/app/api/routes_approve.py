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

## 这个端点不做的事

不校验"参数是否符合作动作 schema"（那是 `nodes.validate_resume_payload` 的 §4.4 规则 4）、
不执行动作（`execute` 节点）、不判断 run 是否终态（`resume_agent`）。入口层只做 HTTP 语义：
请求体形状、错误码翻译、响应形状。少了这条分工，同一套校验会在三处各写一遍。
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

#: 审批路由器（`/api` 前缀由 `app.main` 加）。注意它只有 `/approve` 一个端点：
#: 审批列表在 `routes_alerts`（那是**读**入口，与这个**写**入口分开）。


async def _already_decided(*, thread_id: str, action_ids: Sequence[str]) -> bool:
    """这些 `action_id` 是否已裁决（复用 §5.8 的仓储谓词，不另写一套状态判断）。
    保证：只有真的"已裁决"才返回 True（`DuplicateDecision` 是那个谓词给出的答案）。
    不保证：不做任何写操作；也不判断 run 状态（那是 `resume_agent` 的事）。
    调用方坑：`except AppError: return False` 是**故意**的：其它应用层错误（行不存在等）
        应该让原始异常（`RUN_STATE_CONFLICT`）继续上抛，而不是被这里翻译成"已裁决"。
    """
    try:
        with session_scope() as session:
            # 谓词的答案只有两种：全部仍 pending（不抛）或至少一条已被裁决（抛 DuplicateDecision）。
            await approvals.assert_all_pending(
                session=session, thread_id=thread_id, action_ids=action_ids
            )
    except DuplicateDecision:
        # 这就是我们要的"是"：这些行确实已经被裁决过。
        return True
    except AppError:
        # 行不存在 / 别的应用层错误：不是"被裁决过"，交回原异常（RUN_STATE_CONFLICT）
        return False
    # 没抛 = 全部仍 pending，所以"没被裁决过"。
    return False


@router.post(
    "/approve",
    response_model=ApproveResponse,
    summary="提交审批裁决并恢复同一 thread",
    # 四种失败面都在这里声明：OpenAPI 是前端的接口文档，缺一个码前端就会漏一条分支。
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

    提交是**全量**的：这个接口没有"只改一条"的形状。请求层只做形状校验；
    "集合是否等于挂起的那批"要读 state，在 `resume_agent` 里判。

    错误：
        400 VALIDATION_ERROR（形状/枚举/重复 action_id/集合不一致/edit 参数不符 schema）
        404 THREAD_NOT_FOUND（checkpointer 里没有这个 thread：从没跑过，或 checkpoint 被清理）
        404 APPROVAL_NOT_FOUND（`interrupt_id` 与当前挂起中断不一致：已过期、或被别人先批了）
        409 DUPLICATE_DECISION（这些 action_id 已裁决——含"同一页面点第二次"）
        409 RUN_STATE_CONFLICT（thread 存在但当前不挂起，且动作也没被裁决过）
        502 UPSTREAM_ERROR（恢复后执行动作时上游抖店不可用；**裁决此时已经落库**，不要重复提交，
            按 §9.3 的结果对账处理）

    注意 502 这条：裁决已经落库了，所以"重试这次审批"会撞 409 DUPLICATE_DECISION，
    而不是重新执行动作——正确动作是去对账，不是重发。

    调用方坑：
        - 必须把 `interrupt_id` 原样传下来（本端点再传给 `resume_agent(expected_interrupt_id=...)`）：
          漏掉它等于放弃"这次审批对应哪个挂起点"的唯一闸门，并发场景下会把旧裁决批到新决策上，
          而且**不会报错**；
        - 提交是**全量**的：只想改一条也要把同一挂起点的所有 `action_id` 一起带上（少一条就 400）；
        - 409 之后不要用同一份 body 重试（只会继续 409）：去刷新 `/api/alerts` 拿最新状态；
        - 成功返回的 `status` 可能是 `done` 也可能是 `awaiting_approval`（上游还有下一轮挂起时），
          按返回值决定下一步，别硬编码 `done`。

    注意"全量提交"这条与"409 之后重试"的组合：重试**同一份 body** 必然还是 409
    （那些 action 已被裁决），正确做法是刷新 `/api/alerts` 看最新待办。

    保证：只要 200，裁决就已经落库（`approval_request` 的状态已迁移）；`applied[]` 与请求一一对应。
    不保证：不保证动作都执行成功——每个 `applied[].status` 各自独立（`failed`/`unknown` 都可能）。
    """
    decided_at = utc_iso()
    # `decided_at` 由服务端盖（不吃客户端时间）：时钟不一是"同一次审批两个时间"的根源。
    # 这里只做"请求模型 → 领域 TypedDict"的搬运，不做任何业务判断（业务判断在
    # `resume_agent` / `nodes.validate_resume_payload`）。
    payload: list[DecisionPayload] = [
        DecisionPayload(
            action_id=item.action_id,
            # `verdict` 已经是 Literal 校验过的字符串（"approve"/"reject"/"edit"），
            # 领域 TypedDict 在运行期就是 dict，不需要再转一次枚举。
            verdict=item.verdict,  # type: ignore[typeddict-item]  # TypedDict 运行期就是 str
            # 只对 `edit` 有值；其余 verdict 必须是 None（形状由 `Decision` 的校验器保证）。
            edited_params=dict(item.edited_params) if item.edited_params is not None else None,
            # 缺省用顶层 operator（§7.4 表格：decisions[].operator 可缺省）
            # —— 单条不写就沿用请求级的那个人，避免前端把同一个名字重复填 N 遍。
            operator=item.operator or body.operator,
            reason=item.reason,
            # 服务端时间覆盖客户端的 `decided_at`（请求模型里根本没有这个字段）。
            decided_at=decided_at,
        )
        for item in body.decisions
    ]
    try:
        result = await resume_agent(
            # `expected_interrupt_id` 必须传：漏掉它等于放弃"这次审批对应哪个挂起点"的闸门。
            body.thread_id, payload, expected_interrupt_id=body.interrupt_id
        )
    except RunStateConflict as conflict:
        # 两层含义都可能是"没有挂起点"：① 已经批过了 ② 状态确实冲突。
        # 这里再问一次仓储，把"已裁决"翻译成更具体的 409（DUPLICATE_DECISION）。
        # 顺序不能反：先拿到"已裁决"的答案，再决定抛哪个码——反过来会让
        # RUN_STATE_CONFLICT 总是先出，前端永远拿不到更具体的那个。
        if await _already_decided(
            thread_id=body.thread_id, action_ids=[item.action_id for item in body.decisions]
        ):
            raise DuplicateDecision(
                "这些动作已被裁决过（同一条动作不能裁决两次）",
                details={
                    # 带上 thread 与完整 action_ids：前端据此刷新列表并定位冲突的那几条。
                    "thread_id": body.thread_id,
                    "action_ids": [item.action_id for item in body.decisions],
                },
            ) from conflict
        # 不是"已裁决" → 原样上抛 RUN_STATE_CONFLICT（状态机不允许这次操作）。
        # `from conflict` 保留原始异常链：日志里能看到"最内层是哪个判断失败"。
        raise

    return ApproveResponse(
        thread_id=result.thread_id,
        run_id=result.run_id,
        trace_id=result.trace_id,
        status=result.status,  # type: ignore[arg-type]
        # 三个转换都在入口层：领域 dataclass → §7 的 pydantic 模型（时间形状在这里统一）。
        # `asdict` 之后交给 pydantic 校验：域外值（未知 status）会在这里炸成 500，
        # 宁可炸也不要把未知枚举透给前端。
        applied=[AppliedDecision.model_validate(asdict(item)) for item in result.applied],
        pending_approvals=alert_items(result.pending_approvals),
        effect_report=effect_report(result.effect_report),
    )
