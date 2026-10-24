"""agent 与 platform 侧基础设施之间的**装配层**：进程级运行时 + 依赖解析 + 日志汇 + 摘要。

## 这个文件是什么、不是什么

**是**：节点需要的进程级依赖从哪来。契约 §4.3 把节点签名冻结成 `(state, config)` —— 节点
不能多收一个 `client` 或 `session`。于是"节点怎么拿到 `DoudianClient` / `Notifier` / `Rules`
/ `now`"必须有一个统一答案：`config["configurable"]` 显式注入优先，其次进程级单例。
散在 8 个节点里各自 `create_doudian_client()` 会出现 8 份连接池（ADR-004 禁止：限流桶与
并发信号量必须挂在同一个 `httpx.AsyncClient` 上）。

**不是**：任何表的仓储。**本文件禁止出现 `insert_*` / `update_*` / `select(...)` 之类的
数据访问代码**（reviewer 的机械检查项）。理由：同一张表出现两个仓储部件，就会有两处各自
`+= 1` 的计数逻辑——那正是 CONTRACT §4.7「两张真相必须由同一个写入点产生」要防的东西。
边界如下，越界即 bug：

| 表 | 唯一仓储位置 |
|---|---|
| `agent_traces` | `app/agent/tracing.py`（计数只由 `traced()` 写） |
| `action_log` | `app/agent/nodes.py`（执行幂等的唯一写入方） |
| `approval_request` | `app/services/approvals.py` |
| `effect_snapshot` | `app/services/effects.py` |
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent.state import canonical_json, sha256_hex, utcnow
from app.config import Settings, get_settings
from app.observability.logging import JsonlLogSink, redact

__all__ = [
    "AgentRuntime",
    "build_runtime",
    "digest_of",
    "get_log_sink",
    "get_runtime",
    "refresh_log_sink",
    "require_log_sink",
    "reset_runtime",
    "resolve_client",
    "resolve_now",
    "resolve_runtime",
    "set_log_sink",
    "set_runtime",
]


# --------------------------------------------------------------------------------------
# 摘要（脱敏后哈希，只存摘要不存原文）
# --------------------------------------------------------------------------------------


def digest_of(value: Any, *, redact_keys: tuple[str, ...] | frozenset[str] | None = None) -> str:
    """摘要 = 脱敏后 canonical JSON 的 sha256 前 16 位（§7.6.1）。

    放摘要而不是原文，是为了同时满足两件事：出问题时能判断"两次重放的输入是否相同"，
    以及 access_token 之类的值永远没有机会出现在日志里。
    """
    keys = redact_keys
    if keys is None:
        keys = get_settings().observability.redact_keys
    return sha256_hex(canonical_json(redact(value, keys)))[:16]


# --------------------------------------------------------------------------------------
# JSONL 日志汇（§7.6；节点明细的唯一落点）
# --------------------------------------------------------------------------------------

_sink: JsonlLogSink | None = None
_sink_lock = threading.Lock()


def set_log_sink(sink: JsonlLogSink | None) -> None:
    """装配进程级 JSONL 汇（`app.main` lifespan 注入；测试注入指向 tmp 的实例）。"""
    global _sink
    with _sink_lock:
        _sink = sink


def get_log_sink() -> JsonlLogSink | None:
    """已装配的汇；未装配返回 `None`（不顺手建文件，避免意外污染工作目录）。"""
    return _sink


def refresh_log_sink(settings: Settings | None = None) -> JsonlLogSink:
    """按 settings 建/重建进程级汇（lifespan 与"没人装配"的兜底路径共用）。"""
    resolved = settings or get_settings()
    sink = JsonlLogSink(
        resolved.observability.log_file,
        redact_keys=resolved.observability.redact_keys,
    )
    set_log_sink(sink)
    return sink


def require_log_sink() -> JsonlLogSink:
    """取汇，未装配则按 settings 装配一个。

    兜底路径存在的理由：节点级明细的事实源是 JSONL；某个入口忘了装配汇就静默丢账，
    比"多建一个日志文件"严重得多。
    """
    sink = _sink
    if sink is not None:
        return sink
    return refresh_log_sink()


# --------------------------------------------------------------------------------------
# 运行时依赖
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRuntime:
    """节点需要的进程级依赖。`client` 必须是**单例**（ADR-004）。"""

    settings: Settings
    client: Any
    notifier: Any
    rules: Any
    clock: Callable[[], datetime] = utcnow
    extras: Mapping[str, Any] = field(default_factory=dict)


_runtime: AgentRuntime | None = None


def build_runtime(
    settings: Settings | None = None,
    *,
    client: Any = None,
    notifier: Any = None,
    rules: Any = None,
    clock: Callable[[], datetime] | None = None,
) -> AgentRuntime:
    """按 settings 构建一份运行时（client/notifier/rules 可显式注入，测试与 lifespan 共用）。"""
    resolved = settings or get_settings()
    if client is None:
        from app.services.doudian_client import create_doudian_client

        client = create_doudian_client(resolved)
    if rules is None:
        from app.services.rules import load_rules

        rules = load_rules()
    if notifier is None:
        from app.services.notifier import build_notifier

        notifier = build_notifier(
            resolved, http_client=_notifier_http_client(), sink=require_log_sink()
        )
    return AgentRuntime(
        settings=resolved,
        client=client,
        notifier=notifier,
        rules=rules,
        clock=clock or utcnow,
    )


def _notifier_http_client() -> Any:
    """通知用的 HTTP 客户端（feishu 模式需要；dry-run 不会真的发请求）。

    懒创建且进程内复用，理由同 ADR-004：不为每次通知新建连接池。
    """
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=2.0))


def get_runtime() -> AgentRuntime:
    """取进程级运行时（首次访问时按 settings 构建）。"""
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def set_runtime(runtime: AgentRuntime | None) -> None:
    """注入进程级运行时（`app.main` lifespan 用它接管单例 client 的生命周期）。"""
    global _runtime
    _runtime = runtime


def reset_runtime() -> None:
    """清掉进程级运行时（测试用：避免上一个用例的假 client 泄漏到下一个）。"""
    set_runtime(None)


def resolve_runtime(config: Mapping[str, Any] | None = None) -> AgentRuntime:
    """节点取运行时：`config["configurable"]["runtime"]` 优先（测试注入），否则进程单例。"""
    configurable = (config or {}).get("configurable")
    if isinstance(configurable, Mapping):
        injected = configurable.get("runtime")
        if isinstance(injected, AgentRuntime):
            return injected
    return get_runtime()


def resolve_client(config: Mapping[str, Any] | None, runtime: AgentRuntime | None = None) -> Any:
    """节点取 client：`config["configurable"]["client"]` 优先（测试注入假 client）。"""
    configurable = (config or {}).get("configurable")
    if isinstance(configurable, Mapping):
        injected = configurable.get("client")
        if injected is not None:
            return injected
    return (runtime or get_runtime()).client


def resolve_now(config: Mapping[str, Any] | None = None) -> datetime:
    """可注入的"现在"。

    优先级：`config["configurable"]["now"]`（固定值）→ `["clock"]`（可调用）→ 运行时自带的
    `clock` → `utcnow()`。让**运行时**也持有钟是有意的：生产里它就是 `utcnow`，测试里换一个
    固定钟就能让整张图（含业务日期）确定下来，而不是靠"把机器的系统时间改掉"。
    """
    configurable = (config or {}).get("configurable")
    if isinstance(configurable, Mapping):
        fixed = configurable.get("now")
        if isinstance(fixed, datetime):
            return fixed
        clock = configurable.get("clock")
        if callable(clock):
            return clock()
    return resolve_runtime(config).clock()
