"""入口层的**只读**辅助查询 —— 契约冻结的仓储签名没有覆盖的那几条读路径。

## 为什么这里会有 SQL

契约 §5.8 把 `approval_request` 的仓储方法冻结成返回 `PendingApproval[]`（**不含行 `id`**），
而 §7.3 要求 `next_cursor` 是"最后一条 `id` 的字符串形式"、排序 `created_at DESC, id DESC`；
§4.7 把 `agent_traces` 的仓储冻结成按 `(shop_id, run_id)` 取单行，而 `/api/metrics` 需要
"这家店铺有哪些 run"。这两件事都**无法**用现有签名表达。

## 边界（越界即 bug）

| 允许 | 禁止 |
|---|---|
| `SELECT`（分页、枚举、取字段） | 任何 `INSERT/UPDATE/DELETE` |
| 时间列过 `to_utc()` 后转 ISO | 派生/校准 `step_count`（唯一写入点是 `tracing.py`） |
| 原样返回库里的值 | 业务判断（状态机、幂等键、告警分组） |

写路径仍然各自唯一：`approval_request` → `app/services/approvals.py`、`agent_traces` →
`app/agent/tracing.py`。本模块只补"读"，不制造第二个 `+= 1`。

## "禁写清单"的依据（不是风格偏好，是三条不变量）

1. **`step_count` 只能由 `tracing.py` 写**：`count(node.trace) == step_count` 这条等式只有在
   同一个写入点里顺序执行"写事件 + 计数"时才成立。在这个只读模块里补一次校准，就是第二个
   `+= 1`，两个计数会各自漂移，而且漂移只能在下次启动对账时才发现。
2. **`approval_request.status` 只能由 `approvals.py` 改**：状态迁移带着"谁、什么时候、依据
   什么"三条审计信息（`verdict`/`decided_by`/`decided_at`），本模块没有那些上下文。
3. **不做业务判断**：这里不该出现"如果 status 是这个就当成那个"——那会把状态机的真相搬到
   查询层，于是同一个判断在服务层与查询层各有一份。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db import arun_db, to_utc
from app.enums import RunStatus
from app.models import AgentTrace, ApprovalRequest

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "list_active_runs",
    "list_approval_page",
    "list_run_rows",
]

#: 非终态（§4.2 迁移表）：启动对账要覆盖的就是这三个
#: 与 `graph._ACTIVE_STATUSES` 同集合，但**不是**同一个常量：这里服务"启动对账"，
#: 那里服务"409 判定"，两处语义不同，合并会让其中一个被另一个的改动带偏。
ACTIVE_RUN_STATUSES: tuple[str, ...] = (
    RunStatus.RUNNING.value,
    RunStatus.AWAITING_APPROVAL.value,
    RunStatus.EXECUTING.value,
)


def _iso_z(value: datetime | None) -> str:
    """库里的时间 → `2026-10-23T14:25:30.000Z`（§0：UTC、毫秒、`Z` 结尾）。

    读时间必须过 `to_utc()`：SQLite 丢 tzinfo，不补会得到一台本地时区的假时间。

    保证：输出形状固定（毫秒三位 + `Z`）；`None` → 空串。
    不保证：不做业务日期的时区折算（那是 `business_date` 的事，`Z` 就是 UTC）。
    调用方坑：空串与 `null` 不是一回事：本模块约定"没有值就是空串"（§7.3 的
        `interrupt_id` 同理），下游不要把它当"缺失"再转成 null。
    """
    resolved = to_utc(value)
    if resolved is None:
        # `None` → 空串而不是抛：DB 列允许 NULL（老行），读出空串让前端显示"-"，
        # 比让整个列表 500 好。
        return ""
    # 固定毫秒三位 + `Z`：§0 的对外形状，与 `convert.iso_z` 一致（两处必须同形，
    # 否则同一份响应里会出现两种时间写法）。
    # `to_utc` 必须先过：SQLite 读出来是 naive，直接 strftime 会得到本地时区的假时间。
    return resolved.strftime("%Y-%m-%dT%H:%M:%S.") + f"{resolved.microsecond // 1000:03d}Z"


def _approval_row(row: ApprovalRequest) -> dict[str, Any]:
    """`approval_request` 行 → 纯 dict（`/api/alerts` 的原始形状，未过 pydantic）。

    保证：键名与 §7.3 的 `AlertItem` 一致，时间已规范化成 ISO-Z；`params`/`evidence` 是 dict。
    不保证：不做业务过滤——`status` 是什么就返回什么（过滤在 SQL 的 `WHERE` 里）。
    调用方坑：`id` 也在返回里（分页游标要用它），但 `AlertItem` **没有** `id` 字段，
        路由层必须把它剔掉再 `model_validate`（否则 `extra="forbid"` 会 400）。
    """
    return {
        # `id` 是分页游标的来源（§7.3 `next_cursor`），必须返回；`AlertItem` 里没有它，
        # 所以路由层要剔掉。
        "id": int(row.id),
        "run_id": row.run_id,
        "thread_id": row.thread_id,
        "shop_id": row.shop_id,
        "trace_id": row.trace_id,
        # 空串 → 空串（不折成 None）：§7.3 的 `interrupt_id` 是 `str` 且允许空串，
        # "还没回填"就是空串这个事实本身。
        "interrupt_id": row.interrupt_id or "",
        "action_id": row.action_id,
        "action_type": row.action_type,
        "target": row.target,
        "params": dict(row.params or {}),
        "reason": row.reason,
        "evidence": dict(row.evidence or {}),
        "risk": row.risk,
        "status": row.status,
        # 时间统一过 `_iso_z`（`to_utc` + 毫秒 + Z）：§0 的对外形状只在这里解释一次。
        "created_at": _iso_z(row.created_at),
    }


async def list_approval_page(
    *,
    status: str | None,
    shop_id: str | None,
    limit: int,
    before_id: int | None,
) -> list[dict[str, Any]]:
    """`/api/alerts` 的一页（§7.3）：`ORDER BY created_at DESC, id DESC` + `id < before_id`。

    `limit` 由调用方多取一条（`limit + 1`）来判断"还有没有下一页"——不额外发一条 `COUNT`。

    保证：返回的顺序是**倒序**（最新在前），且只含 `WHERE` 命中的行；每行都是纯 dict。
    不保证：不保证返回条数等于 `limit`——末尾页会少；"还有没有下一页"由调用方用
        `limit + 1` 的多取一条来判断，本函数不做那个判断。
    调用方坑：`limit` 会被 `max(1, ...)` 夹到至少 1（传 0 不会返回空列表，会返回 1 条）；
        同理 `list_run_rows` / `list_active_runs` 也是这个行为——它们不是校验层。

    为什么排序键是 `created_at DESC, id DESC` 两个而不是 `id DESC`：同一批提议的 `created_at`
    由同一个 `now` 写入（`upsert_proposals` 一次事务里全一样），只按 id 排会在"同事务多行"时
    退化成插入顺序；契约把两个键都写死，这里照抄。

    调用方坑：`before_id` 是 `id` 的 keyset，而排序第一键是 `created_at` —— 只有 `created_at`
    随 id 单调（本服务就是这样：先插 id 小、时间早）时翻页才不重不漏；**不要**给这个函数传
    "上一次响应的 created_at" 当游标。
    """
    stmt = select(ApprovalRequest)
    # 三个过滤都是"给了才加"：`status=None` 表示不过滤（`all` 由路由层翻译成 None），
    # 不是"过滤 status 为 NULL 的行"。
    # SQL 只做过滤/排序/截断，业务判断一概不做（见模块 docstring 的禁写清单）。
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    if shop_id is not None:
        stmt = stmt.where(ApprovalRequest.shop_id == shop_id)
    if before_id is not None:
        # keyset 分页：`id <` 而不是 offset。OFFSET 在"审批不断新增/裁决"的表上会跳行/重复行。
        stmt = stmt.where(ApprovalRequest.id < int(before_id))
    stmt = stmt.order_by(
        # 两个排序键都写死（§7.3）：只按 id 排会在"同事务多行"时退化成插入顺序。
        ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc()
    ).limit(max(1, int(limit)))

    def _fn(session: Any) -> list[dict[str, Any]]:
        # 同步 SQL 执行，由 `arun_db` 丢到线程池：不要在事件循环里直接查库。
        # `scalars()` 取第一列实体（`select(ApprovalRequest)` 的整行）。
        return [_approval_row(row) for row in session.execute(stmt).scalars()]

    return await arun_db(_fn)


async def list_run_rows(*, shop_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """某店铺最近的若干 run（**按开始时间升序**返回，便于调用方"后写的覆盖先写的"）。

    只取 `/api/metrics` 需要的列：`run_id` / `thread_id` / `status` / `started_at`。
    先按 `started_at DESC, id DESC` 取最近 `limit` 行，再反转成升序。

    为什么限制 `limit`：`/api/metrics` 要把每个 run 的 state 读一遍（每读一次都是一次 checkpoint
    往返），不设上限会让一次请求变成"几百次读库"。截断即"更早的 run 不参与指标合并"，属已知边界。
    调用方坑：返回值**不含 `shop_id`**（调用方已经知道自己查的是哪个店）；`status` 是字符串，
    不要拿它和 `RunStatus` 枚举做 `is` 比较。
    """
    stmt = (
        select(
            # 只取需要的四列：`agent_traces.error` 之类的大文本不参与指标合并，
            # 拉出来只是白占带宽。
            AgentTrace.run_id,
            AgentTrace.thread_id,
            AgentTrace.status,
            AgentTrace.started_at,
        )
        .where(AgentTrace.shop_id == shop_id)
        .order_by(AgentTrace.started_at.desc(), AgentTrace.id.desc())
        .limit(max(1, int(limit)))
    )

    def _fn(session: Any) -> list[dict[str, Any]]:
        rows = [
            {
                "run_id": row.run_id,
                "thread_id": row.thread_id,
                "status": row.status,
                # 时间过 `_iso_z`（`to_utc` + 毫秒 + Z）：SQLite 丢 tzinfo，不补会得到本地时区假时间。
                "started_at": _iso_z(row.started_at),
            }
            for row in session.execute(stmt)
        ]
        # 先在 SQL 里按 DESC 取"最近的 N 条"，再反转成升序：调用方要的是
        # "后写的覆盖先写的"（`_points_by_date` 的合并顺序），而不是"最新的在前"。
        # 反转在 Python 里做（而不是再发一条 ASC 查询）：省一半的库往返。
        rows.reverse()
        return rows

    return await arun_db(_fn)


async def list_active_runs(*, limit: int = 500) -> list[dict[str, Any]]:
    """所有非终态 run（`app.main` 启动时用它们逐条对账 `step_count`，§4.7）。

    保证：只返回 `ACTIVE_RUN_STATUSES` 里的三种状态；按 `id` 升序（老 run 先对账，
        日志里因此是"按创建顺序"）。
    不保证：不保证这些 run 的进程还活着——崩溃遗留的非终态行正是要在这里校准的。
    调用方坑：`limit` 默认 500，超过就静默截断（启动对账只覆盖前 500 个，属已知边界）；
        本函数只读 `agent_traces`，不碰 checkpoint。
    """
    stmt = (
        select(AgentTrace.run_id, AgentTrace.shop_id, AgentTrace.thread_id, AgentTrace.status)
        # `in_(list(...))`：`ACTIVE_RUN_STATUSES` 是 tuple，SQLAlchemy 需要 list。
        .where(AgentTrace.status.in_(list(ACTIVE_RUN_STATUSES)))
        .order_by(AgentTrace.id.asc())
        .limit(max(1, int(limit)))
    )

    def _fn(session: Any) -> list[dict[str, Any]]:
        return [
            {
                "run_id": row.run_id,
                "shop_id": row.shop_id,
                "thread_id": row.thread_id,
                # 状态原样返回（字符串）：本模块不做状态机判断，翻译交给调用方。
                # `shop_id` 在这条里**有**（调用方要按店铺分组对账），与 `list_run_rows` 相反。
                "status": row.status,
            }
            for row in session.execute(stmt)
        ]

    return await arun_db(_fn)
