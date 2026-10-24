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
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.config import Settings, get_settings
from app.errors import ConfigError

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
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            await saver.setup()
            yield saver
        return
    if checkpoint.type == "postgres":
        if not checkpoint.url:
            # §8.2：type=postgres 时 url 必填，缺失是启动期配置错误（不是运行期 500）。
            raise ConfigError("checkpoint.type=postgres 但 checkpoint.url 为空")
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(checkpoint.url) as saver:
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
