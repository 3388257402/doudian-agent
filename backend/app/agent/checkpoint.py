"""检查点工厂 —— 对应 docs/CONTRACT.md §5.7 / ADR-002。

HITL 的全部价值在于"人可以在几小时甚至几天后做决定"。把 checkpoint 放在进程内存里
（`MemorySaver`/`InMemorySaver`），等于把审批窗口限制在"进程不重启"这个事实上不可控的
区间：部署一次、OOM 一次、`--reload` 一次，审批台上的待办就全变成点不动的僵尸。

所以 `app/` 下**禁止**出现 `MemorySaver` / `InMemorySaver`（只在 `tests/` 允许，用来跑纯图
逻辑）。dev 用 SQLite 文件、prod 用 Postgres，切换只体现在 `settings.checkpoint.type`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.config import Settings, get_settings
from app.errors import ConfigError


def _build_serde() -> JsonPlusSerializer:
    """构造带 allowlist 的 checkpoint 序列化器（把 `app.enums.*` 显式登记）。

    为什么必须显式登记：LangGraph 对未知类型的反序列化目前只打 warning
    （`Deserializing unregistered type app.enums.Verdict ... will be blocked in a
    future version`），下个版本会直接拒绝。我们的 state 里合法地存着 `Verdict` /
    `Attribution` 这类枚举，不登记的结果是**升级 LangGraph 那天 HITL 恢复全线炸**，
    而且炸在最不该炸的地方（人已经批了，图恢复不起来）。

    为什么动态枚举而不是硬编码两个类名：枚举清单会随 state 演进增长，硬编码的
    allowlist 迟早漏一个，漏的那个仍然要等到恢复时才暴露。这里按"模块里所有 Enum"
    生成，新增枚举自动覆盖。

    **登记的是类型对象本身，不是 `(模块名, 名字)` 配对**：配对形式的名字取自命名空间，
    于是 `from enum import StrEnum` 这种"导入进来的类"也会被收进白名单（reviewer 实测
    误收 `('app.enums', 'StrEnum')`），而且将来一句 `from x import SomeEnum`（转发枚举是
    常见重构）就会悄悄扩表——没人会记得那是一条安全边界。直接传类型由 langgraph 自己取
    `__module__/__qualname__`，配合 `__module__ == "app.enums"` 过滤，只登记本模块定义的枚举。
    """
    import app.enums as enums_module

    allowed = tuple(
        value
        for value in vars(enums_module).values()
        if isinstance(value, type)
        and issubclass(value, Enum)
        and value.__module__ == "app.enums"
    )
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)

_active_saver: BaseCheckpointSaver[str] | None = None


@asynccontextmanager
async def checkpoint_saver(settings: Settings | None = None) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """按 `settings.checkpoint` 打开一个持久化 saver（退出时关闭连接）。

    `await saver.setup()` 是幂等的（LangGraph 用 `CREATE TABLE IF NOT EXISTS`），重复调用安全，
    因此不需要"只初始化一次"的额外状态。
    """
    resolved = settings or get_settings()
    checkpoint = resolved.checkpoint
    if checkpoint.type == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = Path(checkpoint.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 用 `aiosqlite.connect` + 显式 `serde`，而不是 `from_conn_string(...)`：
        # 后者只能传 conn_string，构造 `AsyncSqliteSaver(conn)` 时用的是默认 serde，
        # 于是每次反序列化都会为 `app.enums.*` 打一条
        # "Deserializing unregistered type ... will be blocked in a future version" 警告。
        # 我们的 checkpoint 里合法地存着 `Verdict` / `Attribution` 等枚举（state 结构的一部分），
        # 所以要把它们显式登记进 allowlist——不登记不是"现在没事"，是"下个版本直接坏"。
        async with aiosqlite.connect(str(path)) as conn:
            saver = AsyncSqliteSaver(conn, serde=_build_serde())
            await saver.setup()
            yield saver
        return
    if checkpoint.type == "postgres":
        if not checkpoint.url:
            # §8.2：type=postgres 时 url 必填，缺失是启动期配置错误（不是运行期 500）。
            raise ConfigError("checkpoint.type=postgres 但 checkpoint.url 为空")
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # 同一个 serde 也要传给 Postgres 分支：只修 sqlite 等于"开发环境不告警、
        # 生产环境照旧告警"——那是最难被发现的一类不彻底（`from_conn_string` 接受 serde）。
        async with AsyncPostgresSaver.from_conn_string(
            checkpoint.url, serde=_build_serde()
        ) as saver:
            await saver.setup()
            yield saver
        return
    raise ConfigError(f"未知 checkpoint.type={checkpoint.type!r}（允许 sqlite / postgres）")


def set_checkpointer(saver: BaseCheckpointSaver[str] | None) -> None:
    """登记进程级 saver（`app.main` lifespan 里用它复用同一个连接池）。"""
    global _active_saver
    _active_saver = saver


def get_checkpointer() -> BaseCheckpointSaver[str] | None:
    """返回 lifespan 登记的 saver；未登记返回 `None`（调用方退化为"每次调用开一个"）。"""
    return _active_saver


@asynccontextmanager
async def resolve_checkpointer(
    checkpointer: BaseCheckpointSaver[str] | None = None,
    *,
    settings: Settings | None = None,
) -> AsyncIterator[BaseCheckpointSaver[str] | None]:
    """入口统一取 saver：显式传入 > lifespan 登记 > 临时打开一个（仍落盘，仍是持久的）。"""
    if checkpointer is not None:
        yield checkpointer
        return
    active = get_checkpointer()
    if active is not None:
        yield active
        return
    async with checkpoint_saver(settings) as saver:
        yield saver


def checkpoint_info(settings: Settings | None = None) -> dict[str, Any]:
    """给 `/api/healthz` 用的一行状态（不连库，只报配置意图）。"""
    resolved = settings or get_settings()
    return {
        "type": resolved.checkpoint.type,
        "path": resolved.checkpoint.path if resolved.checkpoint.type == "sqlite" else None,
        "shared": _active_saver is not None,
    }


__all__ = [
    "checkpoint_info",
    "checkpoint_saver",
    "get_checkpointer",
    "resolve_checkpointer",
    "set_checkpointer",
]
