"""`approval_request` 仓储 —— 对应 docs/CONTRACT.md §5.8。

这张表是**索引**，不是权威状态（ADR-002：权威状态在 checkpoint；两者不一致时以 checkpoint
为准）。它存在的唯一理由是"审批台要能在不读 checkpoint 的情况下列出待办"。

两条语义决定了整个 HITL 的正确性，改动前先读 ADR-010：

1. **`upsert_proposals` 是 insert-if-absent，已存在时什么都不做**。`approval_gate` 在 resume
   时会被重放（LangGraph 从节点开头重跑），若 upsert 覆盖 `status=approved` → `pending`，
   审批台上会冒出一条"审批完成却又在等审批"的幽灵待办。
2. **`interrupt_id` 只能回填**（`WHERE interrupt_id=''`）。`interrupt()` 是阻塞的，它的 id
   在恢复后才拿得到，而落库发生在 `interrupt()` 之前（§5.8 的时序）。

这两条之外还有一条**不写在契约里、但被代码依赖**的性质：本仓储不做任何业务校验（不改参数、
不判风险、不看 run 状态）。它只做"表的读写 + 三条 409/404 前置断言"。所以：

- 想在审批台上拦一个"高危动作必须两个人批"，落点在 `assert_all_pending` 的调用方（节点），
  不在这里加 —— 在仓储里加会变成"有的调用方走、有的不走"。
- 想改"批准后执行什么"，落点在 `effective_params` 的消费方（`execute` 节点），不在这里改
  `params` 的语义。

所有函数按契约声明为 `async def`，但内部用的是**同步** `Session`（§6.4：ORM 是同步
SQLAlchemy 2.x）。调用方在 async 节点里用 `session_scope()` 拿 session 后直接 await 这些函数，
不要跨 await 长时间持有 session。

三个"身份"别混（混了就会出现"审批了 A 却执行了 B"或"执行了两次"）：

| 名字 | 生成时机 | 由谁生成 | 作用 |
| --- | --- | --- | --- |
| `action_id` | **审批之前**（提议阶段） | `actions.action_id_for` | 审批行主键；`run:type:target` |
| `payload_digest` | 审批行插入时 + `verdict=edit` 时**重算** | `actions.payload_digest` | 参数指纹，回答"人改了参数没有" |
| `idempotency_key` | **执行阶段**（审批之后） | `actions.idempotency_key` | 执行身份，回答"这动作执行过没有" |

幂等键在**审批之后**才生成，是这个模块最容易读错的一点：审批行本身只能用 `action_id`
做身份（那时还没有"最终参数"，`verdict=edit` 可能改掉参数）。所以"同一 action_id 重复裁决
→ 409"这条保证，靠的是本模块对 `status` 的 CAS 式检查，不是靠幂等键。
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


# --------------------------------------------------------------------------------------
# 状态机（`ApprovalStatus` 的全部取值，别在别处发明第 5 个）
# --------------------------------------------------------------------------------------
#   pending --approve/edit--> approved    mark_decided 落 verdict + decided_at
#   pending --reject------->  rejected    同上
#   pending --超时--------->  expired     ⚠ 只有枚举值、配置项（`agent.approval_timeout_min`）
#                                         和接口过滤位存在，**本仓库当前没有任何代码把行写成
#                                         expired**。所以"待审批过了 24 小时会自己过期"这句
#                                         话不成立，不要据此写告警文案或运维手册。
#
# 两条推论（都是真实会踩的）：
# 1. 只有 `pending` 能被裁决；`approved`/`rejected`/`expired` 都是终态 → 本模块一律 `409`。
# 2. `expired` 不是本模块写的，所以**读到什么状态都要先看 `status` 再读裁决四件套**：
#    一行 expired 的 `verdict`/`decided_by`/`decided_at`/`decision_reason` 全是 NULL。
#
# 异常类型与 HTTP 的对应关系（由 API 层翻译，本模块只负责抛对类型）：
#   `ApprovalNotFound`  → 404（行不存在 / 不属于这个 thread）
#   `DuplicateDecision` → 409（已裁决过）
#   `ValidationError`   → 400（参数坏了，例如 `verdict=edit` 却没带 `edited_params`）
#
# 事务边界（本模块所有函数都**不** commit，只 `flush`）：
#   `db.session_scope()` 在退出时 commit、异常时 rollback，而调用方（节点/接口）在同一个
#   `with session_scope()` 里可能还要写别的行。所以这里的 `flush()` 只做一件事：把行推进到
#   "本事务内可见"（后面的 `list_pending_for_run` 能读到），提交时机交给上层。
#   **不要**在这里 `session.commit()`：那会把调用方的其它未提交改动一起提交，也会让
#   "这一步失败 → 整轮回滚"这条保证失效。
# --------------------------------------------------------------------------------------


# 本模块的三条自查清单（每次改完这里的函数，按顺序过一遍；每条都对应一次真实事故）：
#
#   ① 有没有可能在"已裁决"的行上再写一次状态？
#      → 所有写路径都必须以 `status == pending` 为前提（`already` 检查 / `if row.interrupt_id`
#        的同类写法）。没有这个前提，"双击批准"就会执行两遍。
#   ② 有没有可能把 `interrupt_id` 清掉或覆盖？
#      → 只允许 `backfill_interrupt_id` 那一处写它，且只在原值为空时写。
#   ③ 返回值/异常有没有把"行不存在"和"状态冲突"混成一个？
#      → 前者 404（ApprovalNotFound）、后者 409（DuplicateDecision）。混成一个会让调用方
#        无法决定"要不要刷新列表"。
# --------------------------------------------------------------------------------------
def _as_pending_approval(row: ApprovalRequest) -> PendingApproval:
    """ORM 行 → 纯 dict 形状（`PendingApproval`），**出库即脱钩**。

    保证：返回的结构里没有 ORM 对象引用，`params`/`evidence` 是**拷贝**——上层（节点、
    接口响应）改了它们不会在 `session.flush()` 时被写回库里。这是"审批台读到的参数"
    与"库里存的参数"不会互相污染的前提。
    不保证：字段完整性由数据库 NOT NULL 保证，本函数不做二次校验。
    坑：`interrupt_id` 用 `or ""` 兜底——库里的列是 NOT NULL，但"空串"在语义上是
    "还没回填"（§5.8 的时序），转成 None 会让上层多一种空值形态要处理。
    """
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
    """DB 里的 datetime → 契约时间串（UTC、`Z` 结尾）。

    保证：`None` 返回**空串**（不是 "None"、不是 1970）——`PendingApproval.created_at` 是字符串
    字段，空串在接口层能被一致地渲染成 null（§0 时间规则）。
    不保证：毫秒精度。这里用 `isoformat()`（微秒），与 `metrics._iso_z`（毫秒）**不同**：
    库里的时间只做展示/排序，不做跨事件的严格对账，所以没有统一到毫秒的必要。
    坑：`from app.db import to_utc` 是**函数内**导入（不是模块顶部）：`app.db` 在 import 期会
    反过来依赖 models/services，放在顶部就成环。改到这里时不要"顺手整理"上去。
    """
    from app.db import to_utc

    if value is None:
        return ""
    resolved = to_utc(value)
    return resolved.isoformat().replace("+00:00", "Z") if resolved else ""


def _trace_id_for_run(session: Any, *, shop_id: str, run_id: str) -> str:
    """`approval_request.trace_id` 是 NOT NULL，但 §5.8 的入参里没有 trace_id。

    它就在 `agent_traces` 那一行上（`run_agent` 在入图之前已经写入），所以这里查一次，
    而不是让调用方多传一个可能传错的参数。

    保证：返回的 trace_id 与这次 run 的 `agent_traces` 行**同源**，所以"审批行 → trace →
    JSONL"能一路串起来。
    不保证：这一行一定存在——不存在就抛 `ValidationError`（→ 400）。**不要**在这里
    生成一个随机 trace_id 兜底：那会造出一条永远对不上的审计线索，比失败更糟。
    坑：查询条件是 `(shop_id, run_id)` 两个键，不是只用 run_id——同一 run_id 在不同店铺下
    可能重名，单键查询会 `scalar_one_or_none` 报多行错误。
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
    """insert-if-absent（§5.8）。已存在的 `action_id` **一个字节都不改**。

    保证：① 同一 `action_id` 重复调用不会把 `status=approved` 覆盖回 `pending`（见模块
    docstring 第 1 条，ADR-010）；② 返回时行已经 `flush` 到会话（同一事务里可见），调用方
    可以立刻 `list_pending_for_run` 读回来。
    不保证：幂等键层面的去重——这里只按 `action_id`（`run:type:target`）判重，同一次 run
    里"同类型同目标"的提议本来就会被 `propose_actions_with_report` 先丢掉。
    坑：① 空列表直接返回、**不查 trace**：没有提议时不该因为缺 `agent_traces` 行而报错
    （空跑的 run 是合法的）；② 并发插入撞 uniq 时走 `IntegrityError` 分支，那里 `rollback()`
    会**丢掉本会话里其它未提交的改动**——所以调用方必须把本函数放在"这一步只写审批行"的
    位置（本仓的 `approval_gate` 就是这么排的）。
    """
    if not proposals:
        return
    trace_id = _trace_id_for_run(session, shop_id=shop_id, run_id=run_id)
    now = utcnow()
    action_ids = [proposal["action_id"] for proposal in proposals]
    # 一次性把"已存在的"查出来（一条 IN 查询），而不是每行 `SELECT`：提议数量受
    # `max_actions_per_run`（默认 5）约束，但重复调用发生在 resume 重放路径上，逐行查会
    # 让"重放一次审批门"变成 N 次往返。
    existing = {
        row.action_id
        for row in session.execute(
            select(ApprovalRequest).where(ApprovalRequest.action_id.in_(action_ids))
        ).scalars()
    }
    for proposal in proposals:
        if proposal["action_id"] in existing:
            # 只说"跳过"，不更新任何列——包括 `updated_at`：改了它，审批台会显示
            # "这条待办刚刚被动过"，而实际上什么都没发生。
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
                # 摘要在这里算（而不是执行时算）：审批台上的"参数指纹"是给人看的
                # "这条提议的内容没变"，与 `notifier.payload_digest` 用同一公式，所以
                # 通知卡片上的 digest 和库里的这一列可以直接对齐。
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
                # 裁决四件套显式写 None（而不是留给列默认值）：`PendingApproval` 的形状要求
                # 这些字段"存在且为空"，让"未裁决"在库里是显式的。
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
        # 不用 `ON CONFLICT DO NOTHING` 是因为要同时支持 sqlite/postgres 且要保持
        # "不知道冲突的是哪一行"时的可观测性——重查之后才知道到底缺不缺。
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
    """回填 `interrupt_id`，条件带 `AND interrupt_id = ''`（幂等且不覆盖）。

    调用点只有两处（都在 `agent/graph.py` 的 resume 路径上）：拿到 `interrupt()` 返回值之后，
    按"这次恢复涉及的 action_id 集合"回填。它不在 `mark_decided` 里做——两件事的时机相差一个
    `interrupt()`，混在一起就只能等审批完才写 id，而审批台需要 id 来判断"这条待办还能不能续"。

    保证：① 只填**空**的（`if row.interrupt_id: continue`），已有值一个字节不动——重放
    `approval_gate` 时 `interrupt()` 会给出一个新的 id，若覆盖，"按 interrupt_id 找待办"
    就会在两个 id 之间漂；② 空 `action_ids` / 空 `interrupt_id` 直接返回（no-op）。
    不保证：`action_ids` 里每个 id 都存在。缺的那些被静默忽略——这是有意的：resume 路径上
    "部分提议被丢弃"是正常情形，为一个已经不存在的行抛错会让整次 resume 失败。
    坑：必须**在 `interrupt()` 之后**调用（§5.8 的时序）。在它之前调没有 id 可填；调用方
    忘了调，审批台上的行会一直是 `interrupt_id=''`，表现为"批准了但没有继续跑"。
    """
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
        # 只动 `updated_at` 不够——真正被改的是 interrupt_id，但它不参与任何业务判断，
        # 所以不需要额外的事件记录（审批台只看 status）。
        row.updated_at = now
    session.flush()


# 列表接口的分页契约（前端 `before_id` 游标，别改成 offset）：
#   第一页：`before_id` 不传 → `id DESC LIMIT n`
#   下一页：传本页最后一行的 `id` → `id < before_id` 继续
# 为什么不用 OFFSET：审批台是可写的列表，翻页期间有人新增/裁决时，OFFSET 会让同一行出现两次
# 或整行跳过；keyset 的游标是主键，不受插入影响。
async def list_by_status(
    *,
    session: Any,
    status: ApprovalStatus | None = None,
    shop_id: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> list[PendingApproval]:
    """keyset 分页（`id < before_id`），按 `id DESC`。

    保证：分页游标是**自增主键**，所以"翻页过程中有人新增/裁决"不会让某一行出现两次或跳过
    （`OFFSET` 会）。`limit` 至少为 1（`max(1, ...)`），不会返回"空页却还有更多"。
    不保证：总数。keyset 分页拿不到 total，前端要显示"共 N 条"得单独 count——这是刻意的，
    不做 count 查询是为了让审批台的首屏不被大表拖死。
    坑：游标方向是 `id < before_id`（**向更早**翻）。第一页传 `None`；取下一页要传本页最后
    一行的 `id`，传成第一行的 id 会立刻空页。`limit` 传字符串会被 `int()` 转换，非法值抛
    ValueError（不静默用默认值）。
    """
    stmt = select(ApprovalRequest)
    if status is not None:
        # `str(status)` 而不是 `status.value`：`ApprovalStatus` 是 str 枚举，两者对 sqlite 等价，
        # 但 `str()` 对"传进来的是裸字符串"也成立，少一条会踩的路径。
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

    保证：只返回 `status=pending` 的（已裁决的不出现在入口返回值里），按 `id ASC` —— 顺序与
    提议产生顺序一致，审批台/接口都能稳定渲染同一批待办。
    不保证：跨 run 的去重。同一 `action_id` 在库里只有一行，所以理论上不会重复；但如果
    调用方把不同 run 的待办拼在一起展示，需要自己按 `action_id` 去重。
    坑：入口在 interrupt 之后、resume 之前调用它，此时行已存在但 `interrupt_id` 可能还是空
    串（回填发生在 resume 之后），所以**不要**拿 `interrupt_id` 是否为空判断"是否有效"。
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
    """任一 `action_id` 已裁决 → `DuplicateDecision`(409)；行不存在 → `ApprovalNotFound`(404)。

    保证：这是一个**只读**的前置断言：不改变任何行，所以可以在真正落裁决之前随便调。
    检查粒度是"整批"——只要有一条不满足就整批拒绝（不做部分成功），因为一次审批提交是一个
    原子操作。
    不保证：并发安全。两个人同时提交同一批裁决时，两边都会通过这个断言，然后由
    `mark_decided` 的 `already` 检查 + 行状态再拦一次（双保险，见 `mark_decided`）。
    坑：两个检查的**顺序**是刻意的——先 404（行不存在）再 409（已裁决）。颠倒的话，
    "传了一个别的 thread 的 action_id"会被报成"已裁决"（因为查不到行 → `decided` 列表为空
    → 也不会报 409，最终是**静默通过**），那就会把别人的审批行当成待办去改。
    `thread_id` 必须参与查询条件：`action_id` 不含 thread，单靠 id 会跨 thread 命中。
    """
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


# `mark_decided` 的两条调用路径（顺序不同，但都要求"先断言再落库"）：
#
#   ① REST：`routes_approve` → `assert_all_pending`（404/409 在这里先给）→ `mark_decided`
#   ② resume：`graph` 拿到用户裁决 → `assert_all_pending` → `mark_decided` → 回填 interrupt_id
#
# 为什么 `mark_decided` 还要自己再查一遍 `already`：① 是只读断言，与真正的写之间有 TOCTOU
# 窗口；② ②路径上 `assert_all_pending` 与 `mark_decided` 之间还夹着取消挂起等步骤。
# 两条都靠"读到 pending 才改"这个条件，而不是靠调用方自觉。
async def mark_decided(
    *, session: Any, thread_id: str, decisions: Sequence[Decision]
) -> list[str]:
    """落裁决。返回被更新的 `action_id` 列表。

    `verdict=edit` 时用**审批后的最终参数**重算 `payload_digest`（§4.5）—— 这一列存在的意义
    就是"参数在审批时被改过"，不重算它等于把审计线索擦掉。

    保证：① `edit` 的参数要过 `validate_params`（人改的参数与机器生成的参数过**同一道闸**）；
    ② `approved`/`rejected` 分别映射到 `ApprovalStatus`，`verdict` 原样落库；③ 幂等性靠
    "行状态必须是 pending"这一点：已裁决的行会先被 `already` 拦住（409）。
    不保证：执行结果。本函数只落裁决，动作真正执行是 `execute` 节点的事；也不保证同批
    裁决的原子性（不是一次 UPDATE，而是逐行改属性 + 一次 flush——同一事务内，失败一起回滚）。
    坑：`decided_at` 缺省用 `utcnow()`（不是留空）：审批台要按裁决时间排序，空值会让"刚批的"
    沉到底部。传进来的时间串必须是能被 `parse_utc_iso` 解析的 ISO-8601，否则**不会**报错而是
    退回 `utcnow()`（见下面那行三元），所以前端传错时间会"静默变成现在"。
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
        # 与 `assert_all_pending` 的 404 同义：行不属于这个 thread 或根本不存在。
        raise ApprovalNotFound(
            "审批行不存在", details={"thread_id": thread_id, "missing_action_ids": missing}
        )
    already = [
        {"action_id": item, "status": by_id[item].status}
        for item in action_ids
        if by_id[item].status != ApprovalStatus.PENDING.value
    ]
    if already:
        # 这一层是**必须**的，不能只靠调用点的 `assert_all_pending`：两个请求可能都通过了
        # 那个只读断言（TOCTOU），随后一起走到这里。真正的闸门是"读到 pending 才改"。
        raise DuplicateDecision(
            "动作已被裁决过", details={"thread_id": thread_id, "decided": already}
        )

    updated: list[str] = []
    for decision in decisions:
        row = by_id[decision["action_id"]]
        # 枚举校验放在循环内：一个非法的 verdict 会让整批回滚，报错里带 action_id，
        # 能直接指出是"哪个动作的裁决写错了"。
        verdict = Verdict(str(decision["verdict"]))
        if verdict is Verdict.EDIT:
            edited = decision.get("edited_params")
            if not isinstance(edited, dict) or not edited:
                # 空 dict 不能当"没改"放过：`edit` 的语义是"用这份参数执行"，
                # 空参数执行下去就是"改成一个空值"，必须在落库前拦住。
                raise ValidationError(
                    "verdict=edit 必须带非空 edited_params",
                    details={"action_id": row.action_id},
                )
            # 人的输入也过 schema（§4.4 校验规则 4）：否则审批台能提交一个"负数价格"绕过
            # 提议阶段的所有校验——那是整条链路里唯一由人直接输入的地方。
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
            # 注意：`approve` **不改** `params`/`payload_digest` —— 这两个值必须仍然是
            # 提议时的原值，否则"批准的就是屏幕上看到的东西"这条保证就没了。
            row.status = ApprovalStatus.APPROVED.value
        else:
            # 只有 approve / edit / reject 三个值；reject 落到这里。
            row.status = ApprovalStatus.REJECTED.value
        row.verdict = verdict.value
        # `decided_by` 允许为空（系统自动裁决/老数据），不设 NOT NULL 的意义是"别为了满足
        # 列约束编一个 operator 出来"。
        row.decided_by = decision.get("operator")
        decided_at = decision.get("decided_at")
        row.decided_at = parse_utc_iso(decided_at) if isinstance(decided_at, str) else utcnow()
        row.decision_reason = decision.get("reason")
        row.updated_at = utcnow()
        updated.append(row.action_id)
    session.flush()
    return updated


def effective_params(row: PendingApproval | ApprovalRequest, decision: Decision) -> dict[str, Any]:
    """§4.5：`params_effective = verdict==edit ? edited_params : proposal.params`，并过归一化。

    保证：返回的是**副本**（`validate_params`/`normalize_params` 都构造新 dict），改它不会
    影响审批行；且 edit 分支过的是**完整校验**、非 edit 分支只做归一化。
    不保证：非 edit 分支的合法性——提议参数在审批行落库时已经校验过，这里只补归一化
    （剔除 None、字符串数字转 int），重复做完整校验只会把"库里存的旧格式"变成硬错误。
    坑：执行前必须用本函数的返回值、不要用 `row.params`：两者在 `edit` 之后虽然一致，
    但 `normalize_params` 会剔除 None，直接下发 `None` 会被上游当成"显式置空"。
    """
    if Verdict(str(decision["verdict"])) is Verdict.EDIT:
        edited = decision.get("edited_params") or {}
        return validate_params(row.action_type, edited)
    if isinstance(row, PendingApproval):
        return normalize_params(row.action_type, row.params)
    return normalize_params(row.action_type, dict(row.params or {}))


def canonical(params: dict[str, Any]) -> str:  # pragma: no cover - 便于调试/审计时打印
    """把参数序列化成规范 JSON（与 `payload_digest`/`idempotency_key` 用的是同一份口径）。

    只用于调试/审计打印，不参与哈希计算本身——所以它 `pragma: no cover`。
    """
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
