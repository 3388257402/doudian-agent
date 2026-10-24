"""数据库接入层（CONTRACT §6.4）。

设计取舍：

- ORM 是**同步** SQLAlchemy 2.x。异步场景（agent 节点 / async 路由）统一
  `await arun_db(lambda s: ...)` → `asyncio.to_thread(run_db, fn)`。
  为什么不上 async ORM：事务边界要能在一个同步 `Session` 里表达（节点内多次写 = 一个
  `session_scope()`），同步 Session + `to_thread` 比 async session 的并发/取消语义更好推理。
- `session_scope()` 是**模块级单例工厂**：engine 由 `init_db()` 设定一次（lifespan 或测试），
  避免"两个 engine、两套连接池"（这正是 captain 把 config/db/models 收归一处的原因）。
- 开发 SQLite / 生产 Postgres 只换 `database.url`；方言差异只出现在两处：连接参数
  （SQLite 需要 `check_same_thread=False` 才能在 `to_thread` 里复用连接）与 WAL PRAGMA。

一行命令建库（dev）：

    .venv\\Scripts\\python.exe -m app.db            # 读 config/settings.yaml + 环境变量
    DATABASE_URL=postgresql+psycopg://... .venv\\Scripts\\python.exe -m app.db
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypeVar

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, _utcnow, utcnow

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from sqlalchemy import Engine

    from app.config import Settings

__all__ = [
    "create_engine_from_settings",
    "configure_engine",
    "get_engine",
    "enable_sqlite_wal",
    "init_db",
    "reset_engine",
    "session_scope",
    "get_session",
    "run_db",
    "arun_db",
    "to_utc",
    "utcnow",
    "_utcnow",
]

T = TypeVar("T")

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_engine_from_settings(settings: Settings) -> Engine:
    """按 `settings.database` 建 engine（dev sqlite / prod postgres 同一段代码）。"""
    url = str(settings.database.url)
    kwargs: dict[str, object] = {
        "echo": bool(settings.database.echo),
        "future": True,
    }
    if url.startswith("sqlite"):
        # 唯一允许的方言分支之一（§6.3 规则 8/9）：`arun_db` 用 to_thread 执行，
        # 连接会在不同线程间流转，因此必须关掉 pysqlite 的同线程检查。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
        if ":memory:" in url:
            # 内存库必须固定单连接，否则每个连接一个独立库（create_all 看不到表）。
            kwargs["poolclass"] = sa.StaticPool
    else:
        kwargs["pool_size"] = int(settings.database.pool_size)
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800
    return sa.create_engine(url, **kwargs)


def configure_engine(engine: Engine) -> None:
    """登记进程级 engine（`session_scope()` 用）。"""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("engine 未初始化：先调用 init_db(engine)（lifespan 或测试夹具）")
    return _engine


def _require_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("session factory 未初始化：先调用 init_db(engine)")
    return _session_factory


def enable_sqlite_wal(engine: Engine) -> bool:
    """SQLite 开 WAL（§6.3 规则 8：**唯一**允许的 PRAGMA）。返回是否真的切到 `wal`。

    内存库/只读库会回报 `memory`/`delete`，这里如实返回 False 而不是假装成功；
    非 sqlite 方言直接返回 False（生产 Postgres 上不存在这条路径）。
    """
    if engine.dialect.name != "sqlite":
        return False
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()
        conn.commit()
    return str(mode).lower() == "wal"


def init_db(engine: Engine) -> None:
    """建表（dev 用 create_all）+ SQLite 开 WAL。生产建表由 alembic/init SQL 负责。"""
    configure_engine(engine)
    enable_sqlite_wal(engine)
    Base.metadata.create_all(engine)


def reset_engine() -> None:
    """丢弃进程级 engine（测试用：每个用例一个库）。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个事务：正常提交，异常回滚，无论如何关闭。"""
    session = _require_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI `Depends` 用（一个 HTTP 请求 = 一个 session）。"""
    with session_scope() as session:
        yield session


def run_db(fn: Callable[[Session], T]) -> T:
    """在一个事务里执行 `fn` 并返回其结果。"""
    with session_scope() as session:
        return fn(session)


async def arun_db(fn: Callable[[Session], T]) -> T:
    """异步场景的入口：把同步事务丢到线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(run_db, fn)


def to_utc(dt: datetime | None) -> datetime | None:
    """读时间必须过这里：SQLite 返回 naive datetime，按 UTC 补 tzinfo；其余原样返回。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _main() -> int:
    """`python -m app.db`：一行命令建库。"""
    from app.config import load_settings

    settings = load_settings()
    engine = create_engine_from_settings(settings)
    init_db(engine)
    print(f"database ready: {settings.database.url}")
    print(f"tables: {', '.join(sorted(Base.metadata.tables))}")
    engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工命令
    raise SystemExit(_main())
