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
    # 只在类型检查时 import：`Engine`/`Settings` 在运行期会造成环（config 反过来可能要 db）。
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
# 两个模块级单例：`init_db()` 一次设定，`reset_engine()` 一次清空。
# "一个进程一个 engine"是刻意的（两个 engine = 两套连接池 + 两套事务边界）。


def create_engine_from_settings(settings: Settings) -> Engine:
    """按 `settings.database` 建 engine（dev sqlite / prod postgres 同一段代码）。

    保证：SQLite/Postgres 的差异只出现在两个 `if` 分支里（连接参数与池配置）；
        其余代码对两种方言一视同仁。
    不保证：不建表、不开 WAL、不登记——那些是 `init_db()` 的事。
    调用方坑：SQLite 分支的 `timeout=30.0` 是**连接级**锁等待（秒），与 `doudian.timeout`
        是两回事；`StaticPool` 只给内存库用（每个用例一个 `:memory:` 时必需）。
    """
    url = str(settings.database.url)
    # `future=True` 是 SQLAlchemy 2.x 的风格开关（本仓全部按 2.x 写，别混 1.x 语义）。
    kwargs: dict[str, object] = {
        "echo": bool(settings.database.echo),
        "future": True,
    }
    if url.startswith("sqlite"):
        # 唯一允许的方言分支之一（§6.3 规则 8/9）：`arun_db` 用 to_thread 执行，
        # 连接会在不同线程间流转，因此必须关掉 pysqlite 的同线程检查。
        # `timeout=30.0` 是**连接级**的锁等待秒数（SQLite 写锁被占时的等待上限），
        # 与网络超时无关——缺了它，一个长事务会让别的写直接拿 "database is locked"。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
        if ":memory:" in url:
            # 内存库必须固定单连接，否则每个连接一个独立库（create_all 看不到表）。
            kwargs["poolclass"] = sa.StaticPool
    else:
        # Postgres：`pool_pre_ping` 防"连接被服务端踢掉后第一次查询必失败"，
        # `pool_recycle` 防长期空闲连接被中间设备静默断开。
        kwargs["pool_size"] = int(settings.database.pool_size)
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800
    return sa.create_engine(url, **kwargs)


def configure_engine(engine: Engine) -> None:
    """登记进程级 engine（`session_scope()` 用）。

    保证：登记后 `session_scope()`/`get_session()`/`run_db`/`arun_db` 都有 engine 可用。
    不保证：不建表、不开 WAL（那是 `init_db` 的事，它先调本函数再补那两步）。
    调用方坑：`expire_on_commit=False` 是有意的：提交后对象仍可读（否则读属性会触发
        一个已经关掉的 Session 里的惰性刷新，报 DetachedInstanceError）。
    """
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_engine() -> Engine:
    """取进程级 engine；未初始化就抛（不返回 None，也不顺手建一个默认库）。

    保证：返回值非 None。
    不保证：不保证连接可用（engine 存在不代表 DB 可连，那要真发一条查询）。
    调用方坑：`init_db` 之前调用会抛 `RuntimeError`——测试夹具必须先建库。
    """
    if _engine is None:
        raise RuntimeError("engine 未初始化：先调用 init_db(engine)（lifespan 或测试夹具）")
    return _engine


def _require_factory() -> sessionmaker[Session]:
    """取 session 工厂；未初始化就抛（错误消息点明该调 `init_db`）。"""
    if _session_factory is None:
        raise RuntimeError("session factory 未初始化：先调用 init_db(engine)")
    return _session_factory


def enable_sqlite_wal(engine: Engine) -> bool:
    """SQLite 开 WAL（§6.3 规则 8：**唯一**允许的 PRAGMA）。返回是否真的切到 `wal`。

    内存库/只读库会回报 `memory`/`delete`，这里如实返回 False 而不是假装成功；
    非 sqlite 方言直接返回 False（生产 Postgres 上不存在这条路径）。

    保证：非 sqlite 时返回 False 且不执行任何 SQL（PG 不支持 PRAGMA）。
    不保证：不保证切换成功——返回值就是那句话（False 时别假设并发写变快了）。
    调用方坑：只在 `init_db` 里调用一次；重复调用是幂等的，但没必要。
    """
    if engine.dialect.name != "sqlite":
        # 生产 Postgres 上不存在这条路径：PRAGMA 是 SQLite 专有语法。
        return False
    with engine.connect() as conn:
        # `journal_mode=WAL` 是**唯一**允许的 PRAGMA（§6.3 规则 8）：
        # 其他 PRAGMA（synchronous/foreign_keys…）在两种方言下的语义不一致，一律不碰。
        mode = conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()
        conn.commit()
    # 如实返回：内存库/只读库会回报 `memory`/`delete`，那时 WAL 没生效——
    # 假装成功会让"并发写变慢"变成一个查不出原因的现象。
    return str(mode).lower() == "wal"


def init_db(engine: Engine) -> None:
    """建表（dev 用 create_all）+ SQLite 开 WAL。生产建表由 alembic/init SQL 负责。

    保证：三步都做完（登记 → WAL → create_all），`session_scope()` 之后立刻可用。
    不保证：不迁移已有表（`create_all` 只建缺的表，加列/改类型它不管）。
    调用方坑：`create_all` 是幂等的（`IF NOT EXISTS`），可以重复调用；
        但它依赖 `models.Base.metadata` 已被 import——所以本模块顶部必须 import 模型。
    """
    configure_engine(engine)
    enable_sqlite_wal(engine)
    Base.metadata.create_all(engine)


def reset_engine() -> None:
    """丢弃进程级 engine（测试用：每个用例一个库）。

    保证：`dispose()` 关掉连接池，`get_engine()` 之后会抛（而不是返回一个死 engine）。
    不保证：不删除库文件——它只释放连接。
    调用方坑：`dispose()` 之后任何持有旧 Session 的代码都会失败，所以只能在关停/夹具里调。
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个事务：正常提交，异常回滚，无论如何关闭。

    保证：`with` 块内的多次写属于**同一个事务**；块内抛异常时整体回滚。
        捕获 `BaseException`（不只 `Exception`）：`CancelledError` 也要回滚，否则
        连接会带着一个未完成的事务回到池里。
    不保证：不保证并发安全——一个 Session 不是一个可共享对象，每次调用新开一个。
    调用方坑：`finally: close()` 在回滚之后执行，所以块内**不能**在 yield 之后再用 session；
        用 `ctx.session` 之外的引用（比如捕获到外面的 session 变量）会在块外失效。
    """
    session = _require_factory()()
    try:
        yield session
        # 提交放在 `yield` 之后：块内任何异常都跳到这里之前，于是回滚分支先命中。
        session.commit()
    except BaseException:
        # 捕 `BaseException`（不只 Exception）：`CancelledError` 也要回滚，
        # 否则连接会带着未完成事务回到池里，下一个用户拿到脏事务。
        session.rollback()
        raise
    finally:
        # `close()` 一定执行（在回滚之后）：连接归还池，句柄不泄漏。
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI `Depends` 用（一个 HTTP 请求 = 一个 session）。

    保证：与 `session_scope()` 同一事务语义（请求成功提交、异常回滚）。
    不保证：当前没有任何路由在用它（本仓路由大多直接走仓储函数）——留着是给未来的依赖注入用。
    调用方坑：它是生成器（`yield`），只能被 FastAPI 的依赖系统消费；直接 `next()` 会漏掉提交。
    """
    with session_scope() as session:
        yield session


def run_db(fn: Callable[[Session], T]) -> T:
    """在一个事务里执行 `fn` 并返回其结果。

    保证：`fn` 抛异常时整个事务回滚；正常返回时提交。
    不保证：不重试、不吞异常——数据库错误原样抛给调用方（由它决定 500 还是降级）。
    调用方坑：`fn` 里不要 `await`（这是同步上下文）；异步场景用 `arun_db`。
    """
    with session_scope() as session:
        return fn(session)


async def arun_db(fn: Callable[[Session], T]) -> T:
    """异步场景的入口：把同步事务丢到线程池，避免阻塞事件循环。

    保证：语义与 `run_db` 完全一致（同一事务、同样回滚），只是不占事件循环。
    不保证：不保证 `fn` 在**同一个线程**里执行两次（线程池会换线程，这也是
        `create_engine_from_settings` 要给 SQLite 设 `check_same_thread=False` 的原因）。
    调用方坑：`fn` 必须是纯同步函数；在里面再 `await` 会直接 TypeError。
    """
    return await asyncio.to_thread(run_db, fn)


def to_utc(dt: datetime | None) -> datetime | None:
    """读时间必须过这里：SQLite 返回 naive datetime，按 UTC 补 tzinfo；其余原样返回。

    保证：返回的 aware datetime 一定是 UTC；`None` 原样返回。
    不保证：不保证那个 naive 值**真的**是 UTC——它只是"本仓写进去的都是 UTC"这个约定。
    调用方坑：把库里读出来的时间直接 `isoformat()` 会得到一台本地时区的假时间（差 8 小时），
        这是最容易在"日期边界"上出错的地方。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _main() -> int:
    """`python -m app.db`：一行命令建库。

    保证：成功时打印库地址与表名，返回 0；失败时异常向上抛（非 0 退出）。
    不保证：不迁移、不灌数据（只 `create_all` + WAL）。
    调用方坑：这是手工命令（`if __name__ == "__main__"` 后面），不要被导入路径调用。
    """
    from app.config import load_settings

    settings = load_settings()
    # 注意：这里**不用** `get_settings()`——命令行的意义就是"读当前文件+环境变量"，
    # 进程缓存会让它在同一个 python 进程里重复调用时读到旧配置。
    engine = create_engine_from_settings(settings)
    init_db(engine)
    print(f"database ready: {settings.database.url}")
    print(f"tables: {', '.join(sorted(Base.metadata.tables))}")
    # 显式 dispose：脚本退出后连接池的句柄要还回去，否则 Windows 上文件可能被占住。
    engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工命令
    raise SystemExit(_main())
