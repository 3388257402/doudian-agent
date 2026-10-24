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
ACTIVE_RUN_STATUSES: tuple[str, ...] = (
    RunStatus.RUNNING.value,
    RunStatus.AWAITING_APPROVAL.value,
    RunStatus.EXECUTING.value,
)


def _iso_z(value: datetime | None) -> str:
    """库里的时间 → `2026-10-23T14:25:30.000Z`（§0：UTC、毫秒、`Z` 结尾）。

    读时间必须过 `to_utc()`：SQLite 丢 tzinfo，不补会得到一台本地时区的假时间。
    """
    resolved = to_utc(value)
    if resolved is None:
        return ""
    return resolved.strftime("%Y-%m-%dT%H:%M:%S.") + f"{resolved.microsecond // 1000:03d}Z"


def _approval_row(row: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": row.run_id,
        "thread_id": row.thread_id,
        "shop_id": row.shop_id,
        "trace_id": row.trace_id,
        "interrupt_id": row.interrupt_id or "",
        "action_id": row.action_id,
        "action_type": row.action_type,
        "target": row.target,
        "params": dict(row.params or {}),
        "reason": row.reason,
        "evidence": dict(row.evidence or {}),
        "risk": row.risk,
        "status": row.status,
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

    为什么排序键是 `created_at DESC, id DESC` 两个而不是 `id DESC`：同一批提议的 `created_at`
    由同一个 `now` 写入（`upsert_proposals` 一次事务里全一样），只按 id 排会在"同事务多行"时
    退化成插入顺序；契约把两个键都写死，这里照抄。
    调用方坑：`before_id` 是 `id` 的 keyset，而排序第一键是 `created_at` —— 只有 `created_at`
    随 id 单调（本服务就是这样：先插 id 小、时间早）时翻页才不重不漏；**不要**给这个函数传
    "上一次响应的 created_at" 当游标。
    """
    stmt = select(ApprovalRequest)
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    if shop_id is not None:
        stmt = stmt.where(ApprovalRequest.shop_id == shop_id)
    if before_id is not None:
        stmt = stmt.where(ApprovalRequest.id < int(before_id))
    stmt = stmt.order_by(
        ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc()
    ).limit(max(1, int(limit)))

    def _fn(session: Any) -> list[dict[str, Any]]:
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
                "started_at": _iso_z(row.started_at),
            }
            for row in session.execute(stmt)
        ]
        rows.reverse()
        return rows

    return await arun_db(_fn)


async def list_active_runs(*, limit: int = 500) -> list[dict[str, Any]]:
    """所有非终态 run（`app.main` 启动时用它们逐条对账 `step_count`，§4.7）。"""
    stmt = (
        select(AgentTrace.run_id, AgentTrace.shop_id, AgentTrace.thread_id, AgentTrace.status)
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
                "status": row.status,
            }
            for row in session.execute(stmt)
        ]

    return await arun_db(_fn)
