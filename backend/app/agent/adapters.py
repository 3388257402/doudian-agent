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
        # 未显式给脱敏键时按 settings 取：脱敏**不能**由调用方"忘了传"而退化成不脱敏。
        keys = get_settings().observability.redact_keys
    return sha256_hex(canonical_json(redact(value, keys)))[:16]


# --------------------------------------------------------------------------------------
# JSONL 日志汇（§7.6；节点明细的唯一落点）
# --------------------------------------------------------------------------------------

_sink: JsonlLogSink | None = None
# 换汇与写汇在并发下会互相撕裂引用：`write` 路径读 `_sink` 时可能正被替换。
# 加锁只保护"取/换引用"这一瞬（写盘本身不需要锁：sink 自己负责串行化）。
_sink_lock = threading.Lock()


def set_log_sink(sink: JsonlLogSink | None) -> None:
    """装配进程级 JSONL 汇（`app.main` lifespan 注入；测试注入指向 tmp 的实例）。

    保证：加锁写入（`_sink_lock`），所以"换汇"与"正在写"不会互相撕裂引用。
    不保证：不保证旧汇被关闭——所有权在调用方（lifespan 的 `shutdown` 负责 `close`）。
    调用方坑：传 `None` 是注销（关停路径用）；注销后 `require_log_sink()` 会**重新建一个**。
    """
    global _sink
    with _sink_lock:
        _sink = sink


def get_log_sink() -> JsonlLogSink | None:
    """已装配的汇；未装配返回 `None`（不顺手建文件，避免意外污染工作目录）。

    保证：只读，无副作用（这才是**读路径**该用的那个）。
    不保证：`None` 不代表"日志功能坏了"——`require_log_sink()` 会在写路径上兜底建一个。
    调用方坑：`/api/logs` 必须用它而不是 `require_log_sink()`：后者会新建 sink，
        新实例的 `_seq` 从文件末尾重新续，读接口因此产生写副作用。
    """
    return _sink


def refresh_log_sink(settings: Settings | None = None) -> JsonlLogSink:
    """按 settings 建/重建进程级汇（lifespan 与"没人装配"的兜底路径共用）。"""
    resolved = settings or get_settings()
    sink = JsonlLogSink(
        resolved.observability.log_file,
        # 脱敏键在**建汇时**固定：之后每次 write 都用这份清单，调用方不需要记得传。
        redact_keys=resolved.observability.redact_keys,
    )
    set_log_sink(sink)
    return sink


def require_log_sink() -> JsonlLogSink:
    """取汇，未装配则按 settings 装配一个。

    兜底路径存在的理由：节点级明细的事实源是 JSONL；某个入口忘了装配汇就静默丢账，
    比"多建一个日志文件"严重得多。

    保证：返回非 `None`；未装配时会真的建一个并**登记为进程级**（后续调用拿到同一个）。
    不保证：不保证它就是"你期望那个文件"——已经装配过的汇不会被替换。
    调用方坑：这是**写路径**专用的（会建文件、会从文件末尾续 `_seq`）。读接口
        （`/api/logs`）必须用 `get_log_sink()`，否则一次读请求会造出第二个 sink 实例，
        两个实例各持一份 `_seq`，直接破坏"seq 跨重启单调递增"（§7.6）。
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
    # `client` 必须是进程单例：限流桶与并发信号量都挂在同一个 `httpx.AsyncClient` 上
    # （ADR-004）。换一个 client 就是换一套限流，两个进程内实例会互相把配额打穿。
    client: Any
    notifier: Any
    rules: Any
    # 可注入的钟：生产就是 `utcnow`，测试换成固定钟就能让整张图（含业务日期）确定下来。
    clock: Callable[[], datetime] = utcnow
    # 扩展位（当前未用）：给未来的依赖留口子，而不是再加一个 dataclass 字段。
    extras: Mapping[str, Any] = field(default_factory=dict)


_runtime: AgentRuntime | None = None
# 进程级运行时单例：`set_runtime()` 注入、`reset_runtime()` 清空。
# 它是**可变模块状态**（不是配置），所以只在装配层出现，节点永远通过 `resolve_*` 取。


def build_runtime(
    settings: Settings | None = None,
    *,
    client: Any = None,
    notifier: Any = None,
    rules: Any = None,
    clock: Callable[[], datetime] | None = None,
) -> AgentRuntime:
    """按 settings 构建一份运行时（client/notifier/rules 可显式注入，测试与 lifespan 共用）。

    保证：三个依赖都非 None（没注入的按配置新建）；`clock` 总有值。
    不保证：**不登记**——想让它成为进程级运行时得再调 `set_runtime()`。
    调用方坑：每次调用都会新建（没注入时连 client 都新建一个）。所以"构建一次、多处使用"，
        不要在每个节点里 build；否则连接池会一份变 N 份（ADR-004 明令禁止）。
    """
    resolved = settings or get_settings()
    if client is None:
        from app.services.doudian_client import create_doudian_client

        # 懒 import：`doudian_client` 会反过来读配置，模块级 import 会成环。
        # 没显式注入就按配置造一个——**全进程唯一的那个**（ADR-004）。
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
        # 只传了 `settings` 而没传 `clock` 时用 `utcnow`：钟必须**总有**一个，
        # 否则 `resolve_now` 会在生产路径上拿到 None。
        clock=clock or utcnow,
    )


def _notifier_http_client() -> Any:
    """通知用的 HTTP 客户端（feishu 模式需要；dry-run 不会真的发请求）。

    懒创建且进程内复用，理由同 ADR-004：不为每次通知新建连接池。
    """
    import httpx

    # 读超时比抖店客户端更短（通知是旁路，不能拖住审批主路径）；
    # 连接超时 3s 是"发不出去就算了"的量级——通知失败本身不阻断审批（见 approval_gate）。
    return httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=2.0))


def get_runtime() -> AgentRuntime:
    """取进程级运行时（首次访问时按 settings 构建）。

    保证：进程内同一个实例（client 单例，ADR-004）；首次调用后一定非 `None`。
    不保证：不是线程安全的懒加载——两个协程同时首次调用可能各自建一份，
        但只会有一份被 `_runtime` 记住（另一份的 client 会泄漏，属已知边界）。
    调用方坑：首次构建会读配置 + 建连接；**不要**在关停路径调它（关停要的是"当前有没有"，
        有 `get_log_sink` 那种只读语义的用法 `set_runtime(None)` 才对）。
    """
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def set_runtime(runtime: AgentRuntime | None) -> None:
    """注入进程级运行时（`app.main` lifespan 用它接管单例 client 的生命周期）。

    保证：设置后 `get_runtime()` 立刻返回它，且**不会**再被懒加载覆盖。
    不保证：不保证旧运行时被关闭（所有权在调用方：lifespan 的 `shutdown` 负责 `aclose`）。
    调用方坑：传 `None` 是"清空"（关停路径），之后的 `get_runtime()` 会重新建一份。
    """
    global _runtime
    _runtime = runtime


def reset_runtime() -> None:
    """清掉进程级运行时（测试用：避免上一个用例的假 client 泄漏到下一个）。

    保证：`get_runtime()` 之后会重新按 settings 构建。
    不保证：不关闭被清掉的那个 client（`reset_runtime` 只是解引用；lifespan 才负责关）。
    调用方坑：测试里替换过运行时的话，退出夹具时调这个清干净；否则下一个用例会复用假 client。
    """
    set_runtime(None)


def resolve_runtime(config: Mapping[str, Any] | None = None) -> AgentRuntime:
    """节点取运行时：`config["configurable"]["runtime"]` 优先（测试注入），否则进程单例。

    保证：永远返回一个 `AgentRuntime`（注入的必须是真实例，鸭子类型不算）。
    不保证：不保证注入的那份与进程单例一致（测试可以给完全不同的 rules/clock）。
    调用方坑：`isinstance(injected, AgentRuntime)` 是硬判据——传个 Mock 进来会被忽略，
        于是节点会用进程单例，测试里表现为"我明明注入了却没生效"。
    """
    configurable = (config or {}).get("configurable")
    if isinstance(configurable, Mapping):
        injected = configurable.get("runtime")
        if isinstance(injected, AgentRuntime):
            return injected
    return get_runtime()


def resolve_client(config: Mapping[str, Any] | None, runtime: AgentRuntime | None = None) -> Any:
    """节点取 client：`config["configurable"]["client"]` 优先（测试注入假 client）。

    保证：永远返回一个非 `None` 的 client（注入优先，否则运行时的那个）。
    不保证：不保证它是 `DoudianClient`——测试常注入假对象，只要方法签名对得上就行。
    调用方坑：注入判据是 `is not None`（不查类型），传 `None` 等于"没注入"；
        `runtime` 参数是为了让同一次执行里的 `runtime`/`client` 来自同一份快照。
    """
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
            # 固定值优先：测试要的是"这一刻"，而不是"每次调用都返回同一刻的函数"。
            return fixed
        clock = configurable.get("clock")
        if callable(clock):
            return clock()
    return resolve_runtime(config).clock()
