"""ORM 与数据库接入（CONTRACT §6.1 / §6.3 / §6.4）。

这组用例守的是"只换 DATABASE_URL 就能切 Postgres"这句话是真的：
- 建表成功只是起点；要断言**约束真的存在**（唯一约束是真约束，冲突能被捕获并重查）；
- 主键在 SQLite 上必须编译成 `INTEGER`（否则 AUTOINCREMENT 不生效，插入拿到 NULL）；
- 时间列必须能在 SQLite（丢 tzinfo）与 Postgres（带 tzinfo）之间往返；
- 不许出现 `server_default`、方言 JSON、方言 SQL。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from app.db import (
    arun_db,
    create_engine_from_settings,
    enable_sqlite_wal,
    init_db,
    reset_engine,
    run_db,
    session_scope,
    to_utc,
)
from app.config import build_settings
from app.models import ActionLog, AgentTrace, ApprovalRequest, Base, EffectSnapshot

EXPECTED_TABLES = {"agent_traces", "approval_request", "action_log", "effect_snapshot"}


@pytest.fixture()
def engine(tmp_path: Path):
    eng = sa.create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    init_db(eng)
    yield eng
    reset_engine()
    eng.dispose()


# --------------------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------------------
def test_four_tables_are_created_with_the_frozen_column_sets(engine) -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES

    inspector = sa.inspect(engine)
    created = set(inspector.get_table_names())
    assert EXPECTED_TABLES <= created

    expected_columns = {
        "agent_traces": {
            "id", "run_id", "trace_id", "shop_id", "thread_id", "trigger", "status",
            "step_count", "started_at", "updated_at", "finished_at", "error",
        },
        "approval_request": {
            "id", "run_id", "thread_id", "shop_id", "trace_id", "interrupt_id", "action_id",
            "action_type", "target", "params", "payload_digest", "reason", "evidence", "risk",
            "status", "verdict", "decided_by", "decided_at", "decision_reason",
            "created_at", "updated_at",
        },
        "action_log": {
            "id", "action_log_id", "idempotency_key", "action_id", "run_id", "shop_id",
            "action_type", "target", "request_params", "response", "status", "attempt",
            "upstream_request_id", "error_category", "error_message",
            "created_at", "updated_at", "finished_at",
        },
        "effect_snapshot": {
            "id", "snapshot_id", "action_log_id", "run_id", "shop_id", "action_type",
            "target", "offset_days", "status", "due_at", "evaluated_at", "baseline",
            "followup", "delta", "attribution", "caveats", "created_at",
        },
    }
    for table, columns in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        assert actual == columns, f"{table} 列集合与 §6.1 不一致"


def test_unique_constraints_and_indexes_exist(engine) -> None:
    inspector = sa.inspect(engine)

    def unique_sets(table: str) -> set[frozenset[str]]:
        return {
            frozenset(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table)
        }

    assert frozenset({"shop_id", "run_id"}) in unique_sets("agent_traces")
    assert frozenset({"thread_id"}) in unique_sets("agent_traces")
    assert frozenset({"action_id"}) in unique_sets("approval_request")
    assert frozenset({"idempotency_key"}) in unique_sets("action_log")
    assert frozenset({"action_log_id", "offset_days"}) in unique_sets("effect_snapshot")

    def indexed_columns(table: str) -> set[tuple[str, ...]]:
        result: set[tuple[str, ...]] = set()
        for index in inspector.get_indexes(table):
            result.add(tuple(index["column_names"]))
        return result

    assert ("trace_id",) in indexed_columns("agent_traces")
    assert ("status", "updated_at") in indexed_columns("agent_traces")
    assert ("status", "created_at") in indexed_columns("approval_request")
    assert ("run_id",) in indexed_columns("approval_request")
    assert ("shop_id", "status") in indexed_columns("approval_request")
    assert ("status", "due_at") in indexed_columns("effect_snapshot")


def test_portability_rules_hold_for_every_column() -> None:
    """§6.3 九条里的可静态检查部分：全部逐列断言。"""
    sqlite_dialect = sqlite.dialect()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            type_ = column.type
            # 1) JSON 只能是 sa.JSON（不是 JSONB / 方言 JSON）
            if isinstance(type_, sa.JSON):
                assert type(type_) is sa.JSON, f"{table.name}.{column.name} 用了方言 JSON 变体"
            # 2) String 必须带长度；Text 用于长文
            if isinstance(type_, sa.String) and not isinstance(type_, sa.Text):
                assert type_.length is not None and type_.length > 0, (
                    f"{table.name}.{column.name} 的 String 没有长度"
                )
            # 4) 时间列必须 timezone=True，且不许 server_default
            if isinstance(type_, sa.DateTime):
                assert type_.timezone is True, f"{table.name}.{column.name} 少了 timezone=True"
            assert column.server_default is None, (
                f"{table.name}.{column.name} 不许用 server_default（§6.3-4）"
            )
            # 主键类型：SQLite 必须是 INTEGER，PG 是 BIGINT
            if column.primary_key:
                assert type_.compile(dialect=sqlite_dialect) == "INTEGER"
                assert type_.compile(dialect=postgresql.dialect()) == "BIGINT"


def test_ddl_compiles_for_postgres_too() -> None:
    """不连库，只做方言编译：契约要求的"切库不改代码"至少要能生成 PG DDL。"""
    for table in Base.metadata.tables.values():
        ddl = str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect()))
        assert table.name in ddl
        assert "CREATE TABLE" in ddl


# --------------------------------------------------------------------------------------
# 主键与默认值
# --------------------------------------------------------------------------------------
def test_sqlite_autoincrement_works_for_primary_keys(engine) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add_all(
            [
                AgentTrace(
                    run_id="run_1", trace_id="a" * 32, shop_id="12345678",
                    thread_id="t1", status="running", started_at=now, updated_at=now,
                ),
                AgentTrace(
                    run_id="run_2", trace_id="b" * 32, shop_id="12345678",
                    thread_id="t2", status="running", started_at=now, updated_at=now,
                ),
            ]
        )
    with session_scope() as session:
        ids = [row.id for row in session.execute(sa.select(AgentTrace)).scalars()]
    assert ids == [1, 2], "SQLite 上 BigInteger 主键会让自增失效（with_variant 的意义）"


def test_python_side_defaults_are_applied(engine) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        trace = AgentTrace(
            run_id="run_d", trace_id="c" * 32, shop_id="1", thread_id="thread_d", status="pending",
        )
        session.add(trace)
    with session_scope() as session:
        row = session.execute(sa.select(AgentTrace)).scalar_one()
        assert row.trigger == "manual"
        assert row.step_count == 0
        assert to_utc(row.started_at) is not None
        assert to_utc(row.updated_at) is not None
        assert row.finished_at is None
        assert row.error is None
        assert now.tzinfo is not None  # 只是说明：值一律 Python 侧生成


def test_approval_request_defaults(engine) -> None:
    with session_scope() as session:
        session.add(
            ApprovalRequest(
                run_id="run_a", thread_id="t", shop_id="1", trace_id="d" * 32,
                action_id="act_1", action_type="price_update", target="sku:1",
                params={"price_cents": 9900}, payload_digest="e" * 64, reason="r",
                evidence={"rule_id": "gmv_drop_day"}, risk="medium",
            )
        )
    with session_scope() as session:
        row = session.execute(sa.select(ApprovalRequest)).scalar_one()
        assert row.interrupt_id == ""      # 首插 ""，恢复后回填（§6.1）
        assert row.status == "pending"
        assert row.verdict is None
        assert row.decided_by is None
        assert row.params == {"price_cents": 9900}
        assert row.evidence == {"rule_id": "gmv_drop_day"}


# --------------------------------------------------------------------------------------
# 唯一约束：冲突必须能被捕获并重查（§6.3-6 的冲突处理模式）
# --------------------------------------------------------------------------------------
def test_idempotency_key_conflict_is_catchable_and_requeryable(engine) -> None:
    key = "f" * 64

    def insert() -> None:
        with session_scope() as session:
            session.add(
                ActionLog(
                    action_log_id=key, idempotency_key=key, run_id="run_1", shop_id="1",
                    action_type="price_update", target="sku:1",
                    request_params={"price_cents": 9900}, status="succeeded",
                )
            )

    insert()
    with pytest.raises(IntegrityError):
        insert()  # 第二次同 key：数据库层面拒绝

    # 冲突后重查模式（生产代码的写法）：先查后插，捕 IntegrityError 后再查
    with session_scope() as session:
        rows = session.execute(
            sa.select(ActionLog).where(ActionLog.idempotency_key == key)
        ).scalars().all()
    assert len(rows) == 1, "幂等键冲突不许产生第二行"
    assert rows[0].status == "succeeded"
    assert rows[0].action_log_id == key


def test_run_uniqueness_is_per_shop(engine) -> None:
    now = datetime.now(timezone.utc)

    def add(shop_id: str, run_id: str, thread_id: str) -> None:
        with session_scope() as session:
            session.add(
                AgentTrace(
                    run_id=run_id, trace_id="a" * 32, shop_id=shop_id,
                    thread_id=thread_id, status="running", started_at=now, updated_at=now,
                )
            )

    add("1", "run_x", "thread_1")
    add("2", "run_x", "thread_2")  # 不同店铺用同一 run_id 合法
    with pytest.raises(IntegrityError):
        add("1", "run_x", "thread_3")  # 同店同 run_id 非法
    with pytest.raises(IntegrityError):
        add("3", "run_y", "thread_1")  # thread_id 全局唯一


def test_effect_snapshot_window_uniqueness(engine) -> None:
    due = datetime.now(timezone.utc) + timedelta(days=1)

    def add(offset_days: int) -> None:
        with session_scope() as session:
            session.add(
                EffectSnapshot(
                    snapshot_id=f"snap_{offset_days}", action_log_id="log_1", run_id="run_1",
                    shop_id="1", action_type="price_update", target="sku:1",
                    offset_days=offset_days, due_at=due, attribution="manual_observation",
                )
            )

    add(0)
    add(1)
    with pytest.raises(IntegrityError):
        add(0)  # 同一效果窗口只登记一次


# --------------------------------------------------------------------------------------
# 时间往返
# --------------------------------------------------------------------------------------
def test_to_utc_treats_naive_as_utc_and_passes_aware_through() -> None:
    naive = datetime(2026, 10, 23, 14, 25, 30, 113000)
    converted = to_utc(naive)
    assert converted is not None
    assert converted.tzinfo is timezone.utc
    assert converted.replace(tzinfo=None) == naive

    aware = datetime(2026, 10, 23, 14, 25, 30, tzinfo=timezone.utc)
    assert to_utc(aware) is aware
    assert to_utc(None) is None


def test_datetime_roundtrip_through_sqlite_preserves_the_instant(engine) -> None:
    moment = datetime(2026, 10, 23, 14, 25, 30, 113000, tzinfo=timezone.utc)
    with session_scope() as session:
        session.add(
            AgentTrace(
                run_id="run_t", trace_id="0" * 32, shop_id="1", thread_id="thread_t",
                status="running", started_at=moment, updated_at=moment,
            )
        )
    with session_scope() as session:
        row = session.execute(sa.select(AgentTrace)).scalar_one()
    stored = to_utc(row.started_at)
    assert stored is not None
    assert stored == moment, "读时间必须过 to_utc，否则 naive/aware 混算会偏 8 小时"
    # 未过 to_utc 时 SQLite 返回 naive（这正是规则 5 存在的原因）
    assert row.started_at.tzinfo is None


# --------------------------------------------------------------------------------------
# engine / session 生命周期
# --------------------------------------------------------------------------------------
def test_create_engine_from_settings_uses_the_configured_url(tmp_path: Path) -> None:
    settings = build_settings(
        {
            "database": {"url": f"sqlite+pysqlite:///{tmp_path / 'from-settings.db'}", "echo": False}
        },
        env={},
    )
    eng = create_engine_from_settings(settings)
    try:
        assert eng.dialect.name == "sqlite"
        init_db(eng)
        assert set(Base.metadata.tables) <= set(sa.inspect(eng).get_table_names())
    finally:
        reset_engine()
        eng.dispose()


def test_wal_is_enabled_for_sqlite_files_only(engine) -> None:
    """§6.3-8：唯一允许的 PRAGMA，且只在 sqlite 上执行。"""
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert str(mode).lower() == "wal"

    # 显式调用也幂等（init_db 已开过），并且如实回报"确实切到了 wal"
    assert enable_sqlite_wal(engine) is True


def test_enable_sqlite_wal_is_a_noop_for_postgres(tmp_path: Path) -> None:
    """切库规则（§6.3-9）：非 sqlite 方言不得执行任何 PRAGMA。

    这里只建 engine、不连库：如果真的执行了 `PRAGMA`（PG 不认这个语法），
    连接阶段就会炸；`False` 返回值同时证明它走了早退分支。
    """
    pg_engine = sa.create_engine("postgresql+psycopg://user:pass@127.0.0.1:1/none")
    try:
        assert enable_sqlite_wal(pg_engine) is False
    finally:
        pg_engine.dispose()


def test_in_memory_database_reports_wal_not_supported() -> None:
    """内存库切不了 WAL：函数必须如实返回 False，不许假装成功。"""
    mem_engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.StaticPool,
    )
    try:
        assert enable_sqlite_wal(mem_engine) is False
        init_db(mem_engine)
        assert set(Base.metadata.tables) <= set(sa.inspect(mem_engine).get_table_names())
    finally:
        reset_engine()
        mem_engine.dispose()


async def test_arun_db_runs_a_synchronous_transaction_off_the_event_loop(engine) -> None:
    from app.db import _utcnow

    def write(session: sa.orm.Session) -> int:
        session.add(
            AgentTrace(
                run_id="run_async", trace_id="1" * 32, shop_id="1", thread_id="thread_async",
                status="running", started_at=_utcnow(), updated_at=_utcnow(),
            )
        )
        return session.query(AgentTrace).count()

    count = await arun_db(write)
    assert count == 1
    assert run_db(lambda session: session.query(AgentTrace).count()) == 1


def test_session_scope_rolls_back_on_error(engine) -> None:
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            session.add(
                AgentTrace(
                    run_id="run_rb", trace_id="2" * 32, shop_id="1", thread_id="thread_rb",
                    status="running", started_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            raise RuntimeError("boom")
    with session_scope() as session:
        assert session.query(AgentTrace).count() == 0


def test_uninitialised_engine_fails_loudly() -> None:
    reset_engine()
    with pytest.raises(RuntimeError):
        with session_scope():
            pass
