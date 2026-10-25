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

## 四张表各自的唯一职责（不要跨表兼职）

| 表 | 唯一职责 | 它**不**负责 |
|---|---|---|
| `agent_traces` | "一次 run 一行"的**运行摘要**：状态、步数、起止时间、最后一条错误 | 节点级明细（进 JSONL，ADR-015）、审批内容、动作参数 |
| `approval_request` | 一条**待审批提议**的持久化索引：审批台列表、`interrupt_id` 回填、裁决落库 | 执行结果（那是 `action_log`）、run 摘要 |
| `action_log` | 一次**执行尝试**：幂等键是唯一身份，执行结果/上游 request_id 都在这里 | 审批状态、"这个动作值不值得做" |
| `effect_snapshot` | 一个**效果窗口**（T+0/1/3/7）的登记与采样结果 | 因果结论（结论只在报告文案里）、动作参数 |

职责唯一是为了让"同一件事只有一个写入方"成立：`step_count` 只由 `tracing.py` 写、
`action_log` 只由 `nodes.py` 写、`approval_request` 只由 `services/approvals.py` 写、
`effect_snapshot` 只由 `services/effects.py` 写。

## 为什么**没有**指标表

§6.1 只冻结了这四张表，`/api/metrics` 的指标历史因此**只能**从 checkpoint 的 state 里读
（`compute_metrics` 把当日指标与近 8 天历史都写进 state）。这不是遗漏，而是刻意的：
指标是**派生量**，把它落表就意味着"表里的数与 raw 快照可能不一致"，而本仓的第一原则是
"两张真相必须由同一个写入点产生"。真要持久化指标，需要一次契约变更 + 一条新的 ADR
（`/api/metrics` 的 docstring 里也写了这条边界），不在本文件里偷偷加一张表。
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
#: 这是 §6.3 规则 3 的落点：`BigInteger` 在 SQLite 上不会自增（只有 `INTEGER PRIMARY KEY` 会），
#: 所以用 `with_variant` 让同一个声明在两种方言下都正确。
PK_TYPE = sa.BigInteger().with_variant(sa.Integer, "sqlite")

#: 时间列的 Python 侧默认值（§6.3 规则 4）。
def utcnow() -> datetime:
    """当前时间（UTC aware）。列默认值与业务代码共用同一个函数。

    保证：一定带 tzinfo（`timezone.utc`），写进库的是 aware datetime。
    不保证：**不保证读出来还是 aware** —— SQLite 会丢掉 tzinfo，读路径必须过 `app.db.to_utc()`。
    调用方坑：不要在列上写 `server_default`（§6.3 规则 4）：那会让时间由 DB 时钟决定，
        与"业务日期按 Asia/Shanghai 算"的口径分叉。
    """
    return datetime.now(timezone.utc)


_utcnow = utcnow


class Base(DeclarativeBase):
    """SQLAlchemy 2.x 的声明式基类（`Base.metadata.create_all` 的入口）。

    保证：所有表都注册在 `Base.metadata` 上，`init_db()` 一次 `create_all` 就建全。
    不保证：不做迁移——表结构变更需要 alembic/手工 SQL（生产建表不由这里负责）。
    调用方坑：`create_all` 只建"不存在的表"，**不会**改已存在表的列（列类型变了它不管）。
    """

    pass


class AgentTrace(Base):
    """一次 run 一行：运行摘要（status / step_count / updated_at / error）。

    节点级 trace **不进这张表**，走 JSONL `event="node.trace"`（§4.7、ADR-015）。
    `step_count` 的唯一写入点是 `app/agent/tracing.py::traced`。

    保证：`UNIQUE(shop_id, run_id)` —— `(shop_id, run_id)` 就是幂等键，最多一行；
        `UNIQUE(thread_id)` —— 一个 thread 只对应一个 run（thread_id 由 shop_id:run_id 生成）。
    不保证：不保证 `status` 与 checkpoint 里的真相一致——这张表是**索引**，崩溃窗口里会落后，
        靠 `reconcile_step_count` / `_mark_failed` 校准。
    调用方坑：`error` 只存最后一条错误的 message（不是栈，也不是完整错误列表）；
        完整错误在 state 与 JSONL 里。
    """

    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    # 三个身份键：run_id 是幂等键（与 shop_id 组合唯一），thread_id 是 checkpoint 的线程名
    # （`f"{shop_id}:{run_id}"`），trace_id 是日志关联 id（跨日志/响应/包络三处对账）。
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    # trigger 只进审计（manual/scheduled/retry），不改变执行路径。
    trigger: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="manual")
    # status 存的是 `RunStatus` 的**值**（字符串），与 checkpoint 里的 state 同源。
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    # 只由 `traced()` +1（唯一的写入点），且只增不减：`reconcile_step_count` 以 JSONL 为准校准。
    step_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # `onupdate=_utcnow` 让每次 UPDATE 都推进：它是"这个 run 最近动过"的唯一信号
    # （`/api/healthz` 之外的对账/排障都看它）。
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    # `finished_at` 为空 = 还没到终态；启动对账按 `status` 判断，不按这一列（历史行可能缺它）。
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # 只存最后一条错误的 message（不是栈）：完整错误在 state.errors 与 JSONL 里。
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        # 幂等键：`(shop_id, run_id)` 唯一 → insert-if-absent 才有意义。
        sa.UniqueConstraint("shop_id", "run_id", name="uq_agent_traces_shop_run"),
        # 一个 thread 只对应一个 run：`resume_agent` 靠 thread_id 找挂起点，重复会让它找错。
        sa.UniqueConstraint("thread_id", name="uq_agent_traces_thread"),
        sa.Index("ix_agent_traces_trace_id", "trace_id"),
        # 启动对账要按 (status, updated_at) 找"卡住的非终态 run"。
        sa.Index("ix_agent_traces_status_updated", "status", "updated_at"),
    )

    def __repr__(self) -> str:
        return f"<AgentTrace run_id={self.run_id} status={self.status} steps={self.step_count}>"


class ApprovalRequest(Base):
    """一条审批提议一行。身份就是 `action_id`（UNIQUE），没有 `approval_id` 列。

    保证：`UNIQUE(action_id)` + `UNIQUE(thread_id, action_id)` —— upsert 因此可以做成
        insert-if-absent（重放不会把已裁决的行改回 pending）。
    不保证：不保证行的 `interrupt_id` 非空——首次 INSERT 写 `""`，由回填补上；
        空串期间该行"点不动"（`POST /api/approve` 会 404）。
    调用方坑：`payload_digest` 是提议内容的摘要（去重/审计用），**不是**幂等键——
        执行的幂等键在 `action_log.idempotency_key`。
    """

    __tablename__ = "approval_request"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    # run/shop/trace 三件套是"这条待办属于哪一轮"的定位信息（日志与响应都用它们关联）。
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    #: 首次 INSERT 写 ""，恢复后由 backfill_interrupt_id 以 WHERE interrupt_id='' 回填（§5.8）。
    interrupt_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    action_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # `params`/`evidence` 用 `sa.JSON`（不是 JSONB）：可移植性规则 1，SQLite/PG 都支持。
    params: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    # 提议内容的摘要：去重与审计用（**不是**执行的幂等键，那个在 `action_log`）。
    payload_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    risk: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    # 五列构成裁决结果：`status` 由 approvals 仓储维护，其余四列在裁决时一次写全。
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
        # 提议身份唯一：`action_id` 一句就是 §6.1 "没有 approval_id" 的实现。
        sa.UniqueConstraint("action_id", name="uq_approval_request_action_id"),
        # 再加线程维度：同一 action_id 在两个 thread 下是两件事（理论上不会发生，但约束在）。
        sa.UniqueConstraint("thread_id", "action_id", name="uq_approval_request_thread_action"),
        # `/api/alerts` 的主查询：`WHERE status=? ORDER BY created_at DESC, id DESC`。
        sa.Index("ix_approval_request_status_created", "status", "created_at"),
        sa.Index("ix_approval_request_run_id", "run_id"),
        # 按店铺过滤（`?shop_id=`）走这条。
        sa.Index("ix_approval_request_shop_status", "shop_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<ApprovalRequest action_id={self.action_id} status={self.status}>"


class ActionLog(Base):
    """一次执行一行。执行幂等的唯一来源是 `idempotency_key`（UNIQUE）。

    `action_log_id` 就等于 `idempotency_key`（64 hex），不再引入第二套 ID。

    保证：`UNIQUE(idempotency_key)` —— 同一份"店铺+动作+目标+参数"最多一行；
        `status` 落在 pending/succeeded/failed/unknown/skipped_duplicate 五态之一。
    不保证：不保证 `action_id` 非空（老行/对账补录可能没有提议来源）；也不保证 `response`
        是完整上游响应——成功时只存 `{ok, request_id, data_digest}` 这份最小摘要。
    调用方坑：`attempt` 是"第几次尝试"，重试会 +1 但**不会**新建行；判断"能不能再执行"
        要看 `status`（`succeeded`/`skipped_duplicate` 一律不再执行）。
    """

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    # 两个"同一个值"的列是刻意的：`action_log_id` 是给读代码的人的稳定名，
    # `idempotency_key` 是约束与查询用的名字。值相同，不做第二套 ID 算法。
    action_log_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # `action_id` 可为空：对账补录/历史行可能没有提议来源。
    action_id: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # 参与幂等键计算的最终参数（人改过就是改后的那份）。
    request_params: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    # 只存最小摘要 `{ok, request_id, data_digest}`：整块上游 data 会让一次执行写进几百 KB。
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
        # 执行的幂等性全靠这一条约束：同一个 key 至多一行，重试走同一行（attempt +1）。
        sa.UniqueConstraint("idempotency_key", name="uq_action_log_idempotency_key"),
        sa.Index("ix_action_log_run_id", "run_id"),
        sa.Index("ix_action_log_shop_created", "shop_id", "created_at"),
        # 对账任务：按 status 找 pending/unknown 的旧行。
        sa.Index("ix_action_log_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<ActionLog {self.action_log_id} status={self.status} attempt={self.attempt}>"


class EffectSnapshot(Base):
    """一个效果窗口一行（T+0/1/3/7）。`UNIQUE(action_log_id, offset_days)` 保证登记幂等。

    保证：同一 (动作, 窗口) 只有一行；`status` ∈ scheduled/evaluated/missed。
    不保证：不保证 `baseline`/`followup`/`delta` 有值——只有 `evaluated` 的窗口才有。
    调用方坑：`due_at` 是**绝对时刻**（不是"过几天"），调度器按 `due_at <= now` 捞；
        `missed` 表示"到点了但没采成"，与 `scheduled`（还没到）是不同事实。
    """

    __tablename__ = "effect_snapshot"

    id: Mapped[int] = mapped_column(PK_TYPE, primary_key=True, autoincrement=True)
    # `snapshot_id` 是给外部（报告/前端）用的短 id；真正的唯一性在 (action_log_id, offset_days)。
    snapshot_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    action_log_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    shop_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # 三态：scheduled（还没到）/ evaluated（采过）/ missed（到点了没采成）。
    offset_days: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="scheduled")
    # 绝对时刻：调度器按 `due_at <= now` 捞，不靠"过几天"这种相对表达。
    due_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    evaluated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # baseline/followup/delta 三段：只有 evaluated 的窗口才齐全。
    baseline: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    followup: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    delta: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    # 归因强度（single_arm_before_after / ab_test / none）：报告文案据此决定能不能说因果。
    attribution: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    # 注意事项（无对照组时必须挂两条）：`sa.JSON` 存 list[str]。
    caveats: Mapped[list[str] | None] = mapped_column(sa.JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        # 登记幂等：同一个动作的同一个窗口只有一行（重复登记被约束挡住，不会产生两张"T+7"）。
        sa.UniqueConstraint("action_log_id", "offset_days", name="uq_effect_snapshot_log_offset"),
        # 调度器的唯一查询：`WHERE status='scheduled' AND due_at <= now`。
        sa.Index("ix_effect_snapshot_status_due", "status", "due_at"),
        sa.Index("ix_effect_snapshot_run_id", "run_id"),
    )

    def __repr__(self) -> str:
        return f"<EffectSnapshot {self.snapshot_id} offset=+{self.offset_days} {self.status}>"
