"""ORM 模型（CONTRACT §6.1，四张表冻结）。

可移植性规则（§6.3）逐条落在这里，违反则"只换 DATABASE_URL"就是假的：

1. JSON 只用 `sa.JSON`（不用 JSONB / 方言 JSON）。
2. 所有 `String` 带长度，长文用 `Text`。
3. 主键 `BigInteger().with_variant(Integer, "sqlite")`——SQLite 的 AUTOINCREMENT 只认
   `INTEGER PRIMARY KEY`，直接用 BigInteger 会导致自增失效。
4. 时间列 `DateTime(timezone=True)`，值一律 Python 侧 `_utcnow()`；**禁止** `server_default`。
   读出来的值必须过 `app.db.to_utc()`（SQLite 丢 tzinfo）。
5. 唯一约束是真约束（SQLite/PG 都生效），冲突处理走"先查后插 + 捕 IntegrityError 后重查"。
6. 不用方言专有 SQL、不用部分索引/表达式索引。

`_utcnow` 定义在本模块（列默认值需要它），`app.db` 只 re-export——时间默认值只能有一处。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "AgentTrace",
    "ApprovalRequest",
    "ActionLog",
    "EffectSnapshot",
    "utcnow",
    "_utcnow",
    "PK_TYPE",
]

#: 主键类型：PG 用 BIGINT，SQLite 退化成 INTEGER（否则 autoincrement 不生效）。
PK_TYPE = sa.BigInteger().with_variant(sa.Integer, "sqlite")

#: 时间列的 Python 侧默认值（§6.3 规则 4）。
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_utcnow = utcnow


class Base(DeclarativeBase):
    pass


class AgentTrace(Base):
    """一次 run 一行：运行摘要（status / step_count / updated_at / error）。

    节点级 trace **不进这张表**，走 JSONL `event="node.trace"`（§4.7、ADR-015）。
    `step_count` 的唯一写入点是 `app/agent/tracing.py::traced`。
    """

    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    trigger: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    step_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("shop_id", "run_id", name="uq_agent_traces_shop_run"),
        sa.UniqueConstraint("thread_id", name="uq_agent_traces_thread"),
        sa.Index("ix_agent_traces_trace_id", "trace_id"),
        sa.Index("ix_agent_traces_status_updated", "status", "updated_at"),
    )

    def __repr__(self) -> str:
        return f"<AgentTrace run_id={self.run_id} status={self.status} steps={self.step_count}>"


class ApprovalRequest(Base):
    """一条审批提议一行。身份就是 `action_id`（UNIQUE），没有 `approval_id` 列。"""

    __tablename__ = "approval_request"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    #: 首次 INSERT 写 ""，恢复后由 backfill_interrupt_id 以 WHERE interrupt_id='' 回填（§5.8）。
    interrupt_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    action_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    risk: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    verdict: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        sa.UniqueConstraint("action_id", name="uq_approval_request_action_id"),
        sa.UniqueConstraint("thread_id", "action_id", name="uq_approval_request_thread_action"),
        sa.Index("ix_approval_request_status_created", "status", "created_at"),
        sa.Index("ix_approval_request_run_id", "run_id"),
        sa.Index("ix_approval_request_shop_status", "shop_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<ApprovalRequest action_id={self.action_id} status={self.status}>"


class ActionLog(Base):
    """一次执行一行。执行幂等的唯一来源是 `idempotency_key`（UNIQUE）。

    `action_log_id` 就等于 `idempotency_key`（64 hex），不再引入第二套 ID。
    """

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    action_log_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_id: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    request_params: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    upstream_request_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    error_category: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_action_log_idempotency_key"),
        sa.Index("ix_action_log_run_id", "run_id"),
        sa.Index("ix_action_log_shop_created", "shop_id", "created_at"),
        sa.Index("ix_action_log_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<ActionLog {self.action_log_id} status={self.status} attempt={self.attempt}>"


class EffectSnapshot(Base):
    """一个效果窗口一行（T+0/1/3/7）。`UNIQUE(action_log_id, offset_days)` 保证登记幂等。"""

    __tablename__ = "effect_snapshot"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    action_log_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    offset_days: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="scheduled")
    due_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    evaluated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    baseline: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    followup: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    delta: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    attribution: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    caveats: Mapped[list[str] | None] = mapped_column(sa.JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        sa.UniqueConstraint("action_log_id", "offset_days", name="uq_effect_snapshot_log_offset"),
        sa.Index("ix_effect_snapshot_status_due", "status", "due_at"),
        sa.Index("ix_effect_snapshot_run_id", "run_id"),
    )

    def __repr__(self) -> str:
        return f"<EffectSnapshot {self.snapshot_id} offset=+{self.offset_days} {self.status}>"
