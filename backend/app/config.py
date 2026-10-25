"""配置加载（CONTRACT §8.1 / §8.2 / §8.4）。

优先级（高 → 低）：**环境变量 > `config/settings.yaml` > 代码默认值**。

三条硬约束：

1. 密钥**不进 yaml**。yaml 里只写变量名（`app_key_env` / `app_secret_env` / `access_token_env`
   / `feishu_webhook_env`），真正的值由 client / notifier 在启动时从环境变量取。
2. `${VAR}` / `${VAR:-default}` 占位：**未解析且无默认 → 启动期 `ConfigError`**，绝不静默留空。
3. 加载失败必须是**启动期**失败（进程退出码非 0），不能等到第一个请求才炸。

未知字段一律 `ConfigError`（`dry_run` 写错位置这种 typo，如果不报错就变成"配置没生效但没人知道"）。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.errors import ConfigError
from app.services.doudian_client import RateLimitConfig

__all__ = [
    "Settings",
    "AppConfig",
    "DatabaseConfig",
    "CheckpointConfig",
    "DoudianConfig",
    "TimeoutConfig",
    "LimitsConfig",
    "AgentConfig",
    "NotifierConfig",
    "EffectsConfig",
    "ObservabilityConfig",
    "build_settings",
    "load_settings",
    "get_settings",
    "reset_settings_cache",
    "load_rules",
    "startup_warnings",
    "assert_serving_safe",
    "default_settings_path",
    "BACKEND_ROOT",
]

#: backend/ 目录（app/config.py 的上两级）。
BACKEND_ROOT = Path(__file__).resolve().parents[1]

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_ALLOWED_WINDOWS = (0, 1, 3, 7)
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

logger = logging.getLogger("app.config")

#: `app.env=prod` 且 `doudian.dry_run=true` 时的横幅/错误文案（启动期只此一处生成）。
PROD_DRY_RUN_CONSEQUENCE = (
    "doudian.dry_run=true 时 DoudianClient 注入的是离线 cassette，不会真的调用抖店："
    "改价/改库存会返回 OK、action_log 会写 succeeded、看板也有数据，"
    "但上游什么都没发生。生产环境不许静默在这种模式下对外服务。"
)


# --------------------------------------------------------------------------------------
# 子配置
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AppConfig:
    """应用级配置（§8.2 `app` 段）：名字、环境、API 前缀、CORS、请求超时。

    保证：`frozen=True`（运行期不可改）——配置是启动期定下来的事实，改它需要重启。
    不保证：不做交叉校验（那在 `build_settings` 里）。
    调用方坑：`env` 只有 dev/prod 两值；`is_prod` / `prod_dry_run` 都由它派生，
        不要在别处再写一次 `env == "prod"`。
    """

    name: str = "doudian-agent"
    env: str = "dev"
    api_prefix: str = "/api"
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    # 节点级 TimeoutPolicy 也用它：一个节点的上游调用不该比整个 HTTP 请求还能耗。
    request_timeout_s: int = 30


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """业务库配置（§6.4）：URL、SQL echo、连接池大小。

    保证：只换 `url` 就能在 SQLite / Postgres 之间切换（方言差异只在 `app/db.py` 两处）。
    不保证：`pool_size` 对 SQLite 无效（它走 `check_same_thread=False` 的单池）。
    调用方坑：`url` 里的相对路径是**相对进程 cwd** 的，别假设它相对 `backend/`。
    """

    url: str = "sqlite+pysqlite:///./data/doudian.db"
    echo: bool = False
    pool_size: int = 5


@dataclass(frozen=True, slots=True)
class CheckpointConfig:
    """checkpoint 存储配置（§5.7）：`type` 决定用 SQLite 还是 Postgres。

    保证：`type=postgres` 时 `url` 必填（`build_settings` 会拦）。
    不保证：不保证 `path` 的父目录存在（`checkpoint_saver` 会 mkdir）。
    调用方坑：`path`/`url` 互斥使用——sqlite 借 `path`、postgres 借 `url`，
        同时填两个不会报错，但只有一个生效。
    """

    type: str = "sqlite"
    path: str = "data/checkpoint.db"
    url: str = ""


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    """httpx 的四段超时（connect/read/write/pool），由 `to_httpx()` 转成 httpx 对象。

    保证：四项都是正数（`build_settings` 校验 `>= 0.1`）——0 超时会让 httpx 立刻失败。
    不保证：不包含"总超时"（httpx 没有这个维度；节点级总预算在 `TimeoutPolicy` 里）。
    调用方坑：`read_s` 是最容易踩的一项：抖店的大列表接口给 3 秒不够，默认 10 秒是有理由的。
    """

    connect_s: float = 3.0
    read_s: float = 10.0
    write_s: float = 5.0
    pool_s: float = 2.0

    def to_httpx(self) -> httpx.Timeout:
        """转成 httpx 的 `Timeout` 对象（每次调用新建，`Timeout` 是不可变的共享值）。

        保证：四个维度一一对应。
        不保证：不改任何默认值（配置里的数就是最终生效的数）。
        调用方坑：别缓存这个对象到模块级——它跟着 settings 走，缓存会让"改了配置重启"失效。
        """
        return httpx.Timeout(
            connect=self.connect_s,
            read=self.read_s,
            write=self.write_s,
            pool=self.pool_s,
        )


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    """httpx 连接池上限（§8.2 `doudian.limits`）。

    保证：`keepalive <= max_connections` 由 httpx 自行处理（超出会抛，配置错误在启动期暴露）。
    不保证：不代表**并发请求数**上限——那是 `rate_limit.max_concurrency` 的事。
    调用方坑：把 `max_connections` 调到 1 会让所有上游调用串行（限流桶还在，但池只有一个连接）。
    """

    max_connections: int = 20
    max_keepalive_connections: int = 10
    keepalive_expiry_s: float = 30.0

    def to_httpx(self) -> httpx.Limits:
        """转成 httpx 的 `Limits` 对象。

        保证：三个字段一一对应。
        不保证：不改 httpx 的默认值（httpx 自己还有一套默认，这里显式覆盖）。
        调用方坑：`keepalive_expiry` 是秒（float），不是分钟。
        """
        return httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
            keepalive_expiry=self.keepalive_expiry_s,
        )


@dataclass(frozen=True, slots=True)
class DoudianConfig:
    """抖店开放平台配置（§8.2）：地址、签名、**密钥的变量名**、dry_run、超时/限流。

    保证：这里只存 `*_env`（环境变量**名**），真正的密钥由 client 在启动时读环境变量——
        yaml 里永远不出现密钥本身（§8.1 硬约束 1）。
    不保证：不保证那些环境变量真的存在（缺失会在 client 初始化时报错，不是在加载配置时）。
    调用方坑：`dry_run=true` 时 client 是离线 cassette，改价会"成功"但上游什么都没发生
        ——`assert_serving_safe` 就是为这个组合准备的闸门。
    """

    base_url: str = "https://openapi-fxg.jinritemai.com"
    version: int = 2
    sign_method: str = "hmac-sha256"
    # 只存变量名，不存值（§8.1）：值由 client 在启动时从环境变量读。
    app_key_env: str = "DOUDIAN_APP_KEY"
    app_secret_env: str = "DOUDIAN_APP_SECRET"
    access_token_env: str = "DOUDIAN_ACCESS_TOKEN"
    # 生产默认 True 是"安全侧默认"：没配密钥就绝不会真的改商家的价。
    dry_run: bool = True
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Agent 行为上限（§8.2 `agent` 段）：异常条数、动作数、审批超时、递归上限。

    保证：全部有下限（`>= 1`），0 会让"一个异常都不许报"这种配置悄悄生效。
    不保证：`auto_approve_risk` 只支持 `none`/`low`——`medium`/`high` 永远需要人工，
        这是有意的（见 §5.5 的风险分级）。
    调用方坑：`recursion_limit` 是**框架层**兜底，与 `tracing.MAX_ITERATIONS`（业务层）
        各自独立：改一个不会同步另一个。
    """

    max_anomalies: int = 20
    max_actions_per_run: int = 5
    approval_timeout_min: int = 1440
    # 只允许 none/low：medium/high 必须人工批（不是"暂时没实现自动批"）。
    auto_approve_risk: str = "none"
    # 图的递归上限（LangGraph 的 recursion_limit）；与 MAX_ITERATIONS=20 相互独立。
    recursion_limit: int = 50


@dataclass(frozen=True, slots=True)
class NotifierConfig:
    """通知配置（§5.6）：`dry_run`（不真发）或 `feishu`（webhook）。

    保证：`mode` 只允许两个值（`build_settings` 拦）。
    不保证：不保证 webhook 可达——通知失败**不阻断**审批（`approval_gate` 只记一条 error）。
    调用方坑：`feishu_webhook_env` 也是变量名，不是 URL 本身。
    """

    mode: str = "dry_run"
    feishu_webhook_env: str = "FEISHU_WEBHOOK_URL"


@dataclass(frozen=True, slots=True)
class EffectsConfig:
    """效果窗口与调度（§5.9 / ADR-003）：窗口天数、是否启用调度、轮询间隔。

    保证：`windows_days` 必须是 `(0,1,3,7)` 的非空**严格升序**子集（`)` 见 `build_settings`）。
    不保证：不保证调度器真的在跑（`scheduler_enabled=false` 时窗口只会停在 scheduled）。
    调用方坑：窗口天数与 `effect_snapshot.offset_days` 是同一组数；改它不会迁移已有行。
    """

    windows_days: tuple[int, ...] = (0, 1, 3, 7)
    scheduler_enabled: bool = True
    poll_interval_s: int = 60


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    """可观测性配置：日志级别、JSONL 文件、脱敏键清单。

    保证：`redact_keys` 的每一项都会在 `JsonlLogSink.write` 落盘前被替换成 `***`。
    不保证：不保证"脱敏"覆盖所有可能的密钥形态——它是**键名**匹配 + 值级 scrub，
        新加一种密钥（新键名）必须同时加进这份清单。
    调用方坑：把 `redact_keys` 清空会让 token/签名原样进日志（这是最容易犯的配置事故）。
    """

    log_level: str = "INFO"
    log_file: str = "data/logs/app.jsonl"
    # 键名匹配（大小写不敏感）；新增密钥类型必须同时加进来。
    redact_keys: tuple[str, ...] = (
        "app_secret",
        "access_token",
        "sign",
        "authorization",
        "cookie",
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """全部配置的根（八个段，逐字对应 §8.2）。

    保证：`frozen=True` + 每段都是 dataclass → 拿到 `Settings` 就等于拿到一份不可变快照，
        可以放心跨协程传递（不会有人中途改它）。
    不保证：不保证内部的 tuple/dict 深不可变（`redact_keys` 是 tuple，安全；`extras` 类字段本类没有）。
    调用方坑：只能用 `load_settings()` / `build_settings()` 构造——直接 `Settings()` 会绕过
        全部校验（那是给测试造最小对象用的，不是运行期入口）。
    """

    app: AppConfig = field(default_factory=AppConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)
    doudian: DoudianConfig = field(default_factory=DoudianConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    effects: EffectsConfig = field(default_factory=EffectsConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    @property
    def is_prod(self) -> bool:
        """`app.env == "prod"`（唯一判据；不要在别处再写一次字符串比较）。"""
        return self.app.env == "prod"

    @property
    def prod_dry_run(self) -> bool:
        """生产 + 离线夹具：Agent 会"假成功"。见 `PROD_DRY_RUN_CONSEQUENCE`。"""
        return self.is_prod and bool(self.doudian.dry_run)


# --------------------------------------------------------------------------------------
# 环境变量映射（§8.4 冻结名字；不做前缀推导，避免歧义）
# --------------------------------------------------------------------------------------
#: 允许的配置段（白名单）：出现别的段就是拼写错误，必须报错（§8.1 未知字段一律 ConfigError）。
_ENV_TO_PATH: dict[str, tuple[str, ...]] = {
    # 只映射这五个：§8.4 冻结了名字，不做"DOUDIAN_BASE_URL"这种前缀推导（歧义比方便更贵）。
    "DATABASE_URL": ("database", "url"),
    "CHECKPOINT_TYPE": ("checkpoint", "type"),
    "CHECKPOINT_PATH": ("checkpoint", "path"),
    "CHECKPOINT_URL": ("checkpoint", "url"),
    "LOG_LEVEL": ("observability", "log_level"),
}

_SECTION_KEYS: dict[str, frozenset[str]] = {
    # 每一段的键白名单：与对应 dataclass 的字段**同源**（加字段必须两处一起改）。
    "app": frozenset({"name", "env", "api_prefix", "cors_origins", "request_timeout_s"}),
    "database": frozenset({"url", "echo", "pool_size"}),
    "checkpoint": frozenset({"type", "path", "url"}),
    "doudian": frozenset(
        {
            "base_url", "version", "sign_method", "app_key_env", "app_secret_env",
            "access_token_env", "dry_run", "timeout", "limits", "rate_limit",
        }
    ),
    "agent": frozenset(
        {"max_anomalies", "max_actions_per_run", "approval_timeout_min",
         "auto_approve_risk", "recursion_limit"}
    ),
    "notifier": frozenset({"mode", "feishu_webhook_env"}),
    "effects": frozenset({"windows_days", "scheduler_enabled", "poll_interval_s"}),
    "observability": frozenset({"log_level", "log_file", "redact_keys"}),
}

_NESTED_KEYS: dict[tuple[str, str], frozenset[str]] = {
    # 三层嵌套段也要白名单：否则 `timeout.read`（少个 _s）会静默丢失，
    # 表现为"改了配置却没生效"——正是未知字段校验要防的那类事故。
    ("doudian", "timeout"): frozenset({"connect_s", "read_s", "write_s", "pool_s"}),
    ("doudian", "limits"): frozenset(
        {"max_connections", "max_keepalive_connections", "keepalive_expiry_s"}
    ),
    ("doudian", "rate_limit"): frozenset(
        {"rate_per_sec", "burst", "max_concurrency", "max_attempts",
         "backoff_base_s", "backoff_max_s"}
    ),
}


# --------------------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------------------
def default_settings_path() -> Path:
    """默认配置文件路径：`backend/config/settings.yaml`（相对模块位置，不依赖 cwd）。

    保证：路径由 `BACKEND_ROOT` 推出，因此"从哪个目录启动进程"不影响结果。
    不保证：不保证文件存在（`load_settings` 会显式报错，不做静默降级）。
    调用方坑：这是**默认值**入口；测试请显式传 `path`，不要改这个函数。
    """
    return BACKEND_ROOT / "config" / "settings.yaml"


def _resolve_placeholders(value: Any, env: Mapping[str, str], *, where: str) -> Any:
    """把 `${VAR}` / `${VAR:-default}` 解析成实际值（递归处理 dict/list/str）。"""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            # 空字符串按"未设置"处理：`FOO=` 是最常见的"忘了填"形态，
            # 拿空串当值会让 URL/路径类配置变成一个看起来合法但毫无意义的字符串。
            if name in env and env[name] != "":
                return env[name]
            if default is not None:
                # `${VAR:-default}` 的默认值分支：显式给了默认就不算错误。
                return default
            # 无默认且未设置 → 启动期 ConfigError（§8.1 硬约束 2）：绝不静默留空。
            raise ConfigError(f"配置项 {where} 引用的环境变量 {name} 未设置且无默认值")

        return _PLACEHOLDER.sub(_sub, value)
    if isinstance(value, Mapping):
        # 递归进 dict：`where` 沿途累积成 `settings.doudian.app_key_env` 这样的路径，
        # 报错时能直接指出是哪一项（否则只知道"某个占位符没解析"）。
        return {k: _resolve_placeholders(v, env, where=f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        # list 用下标标注位置：`settings.effects.windows_days[2]`。
        return [
            _resolve_placeholders(item, env, where=f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    # 数字/布尔原样返回：占位符只出现在字符串里，不做类型推断。
    return value


def _require(condition: bool, message: str) -> None:
    """断言式校验：不成立就抛 `ConfigError`（**启动期**失败，进程不该起来）。

    保证：失败时抛的是 `ConfigError` 而不是 `AssertionError`——后者在 `python -O` 下会被
        整条删掉，而"配置校验被优化掉"是最不该发生的静默失效。
    不保证：不返回任何值（它是语句，不是表达式）。
    调用方坑：`message` 要写清"哪个字段、期望什么、收到什么"——这是运维唯一能看到的线索。
    """
    if not condition:
        raise ConfigError(message)


def _check_keys(section: str, data: Mapping[str, Any]) -> None:
    """段级未知字段检查（§8.1：typo 不许静默忽略）。

    保证：未知键一次性全列出来（`sorted`），运维改一次就够。
    不保证：不检查**值**的类型（那是 `_as_*` 系列的事）。
    调用方坑：`data` 必须已经过 `_resolve_placeholders`（否则占位符里的键名会被误判）。
    """
    allowed = _SECTION_KEYS[section]
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"settings.yaml 的 {section} 段出现未知字段: {sorted(unknown)}")


def _nested(section: str, key: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """取一个嵌套段（如 `doudian.timeout`）并做同样的白名单检查。

    保证：返回新的 dict（不共享 yaml 里的对象）；缺省时返回 `{}`（让下游用默认值）。
    不保证：不校验值类型，只校验"是不是映射"和"键是否在白名单里"。
    调用方坑：`(section, key)` 必须已在 `_NESTED_KEYS` 里登记，否则 KeyError
        ——那是"加了新嵌套段却忘了登记"的早期信号。
    """
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"settings.yaml 的 {section}.{key} 必须是映射，收到 {type(raw).__name__}")
    allowed = _NESTED_KEYS[(section, key)]
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"settings.yaml 的 {section}.{key} 出现未知字段: {sorted(unknown)}")
    return dict(raw)


def _as_int(value: Any, where: str, *, minimum: int | None = None) -> int:
    """把配置值收成 int（拒绝 bool），可选下限检查。

    保证：`True`/`1.0`/`"5"` 都不会被当整数收下——bool 是 int 子类（必须显式排掉），
        字符串是 yaml 里最常见的引号误用。
    不保证：不做范围上限（例如端口号那类校验不在这里）。
    调用方坑：`where` 要带完整路径（`doudian.rate_limit.burst`），它直接进错误消息。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} 必须是整数，收到 {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where} 必须 >= {minimum}，收到 {value}")
    return value


def _as_float(value: Any, where: str, *, minimum: float | None = None) -> float:
    """把配置值收成 float（拒绝 bool），可选下限检查。

    保证：int 也接受（yaml 里 `3` 与 `3.0` 等价）；bool 被拒。
    不保证：不做 NaN/inf 检查（配置里手写 `.nan` 会通过，但那属于自伤）。
    调用方坑：返回值是 float，传给需要 int 的地方要自己转（本模块内部都有 `int(...)`）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必须是数字，收到 {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"{where} 必须 >= {minimum}，收到 {number}")
    return number


def _as_bool(value: Any, where: str) -> bool:
    """把配置值收成 bool：**只接受**真 bool（yaml 的 `true/false`）。

    保证：`"true"`/`1`/`"yes"` 全部报错——那是最危险的一类静默错误
        （`dry_run: "false"` 会被 truthy 判断当成 True，于是生产也跑夹具）。
    不保证：不做大小写/字符串兼容（兼容就等于允许上面那种歧义）。
    调用方坑：环境变量覆盖路径给的是字符串，所以那些键**不能**是 bool 型
        （`_ENV_TO_PATH` 里确实没有 bool 键）。
    """
    if not isinstance(value, bool):
        raise ConfigError(f"{where} 必须是布尔，收到 {value!r}")
    return value


def _as_str(value: Any, where: str) -> str:
    """把配置值收成非空字符串。

    保证：空串被拒（空 URL / 空路径几乎一定是配置写漏）。
    不保证：不做格式校验（URL/路径形状由各自的 `_require` 负责）。
    调用方坑：整数会被拒（`_as_str(5)` 报错）——要字符串就写字符串，不做隐式转换。
    """
    if not isinstance(value, str) or value == "":
        raise ConfigError(f"{where} 必须是非空字符串，收到 {value!r}")
    return value


# --------------------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------------------
def build_settings(
    data: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """纯函数：由 yaml 字典 + 环境变量组装 `Settings`（不做 I/O，测试直接用）。

    保证：所有校验都在这里发生——返回的 `Settings` 一定是"自洽且可用"的；任何问题抛
        `ConfigError` 并带"哪个字段、期望什么、收到什么"。
    不保证：不做 I/O（不读文件、不连库）；也不保证环境变量存在（`*_env` 只是名字）。
    调用方坑：`env=None` 时**读真实 `os.environ`**——测试一定要显式传 `env={}`，
        否则结果会随开发机环境漂移（这正是"本地过、CI 红"的常见成因）。
    """
    env = dict(os.environ if env is None else env)
    raw: dict[str, Any] = dict(data or {})
    unknown_sections = set(raw) - set(_SECTION_KEYS)
    if unknown_sections:
        # 顶层未知段：多半是把段名写错（`doudiann:`），不报错就等于整段配置静默失效。
        raise ConfigError(f"settings.yaml 出现未知配置段: {sorted(unknown_sections)}")

    # 占位符解析必须在**任何**校验之前：`${VAR}` 解析出来的可能是数字/布尔字符串，
    # 反过来 `_check_keys` 也不该看到占位符还没展开的原始文本。
    raw = _resolve_placeholders(raw, env, where="settings")
    for section in _SECTION_KEYS:
        value = raw.get(section)
        if value is None:
            # 整段缺省 = 全部用默认值（不是错误）：最小配置只需要写想改的那几段。
            raw[section] = {}
        elif not isinstance(value, Mapping):
            raise ConfigError(f"settings.yaml 的 {section} 必须是映射")
        else:
            _check_keys(section, value)
            raw[section] = dict(value)

    # 环境变量覆盖（§8.4）：只覆盖白名单里的路径，字符串值
    for env_name, path in _ENV_TO_PATH.items():
        value = env.get(env_name)
        if value is None or value == "":
            # 空字符串等于"没设"：容器编排里 `FOO=` 很常见，不该把配置覆盖成空。
            continue
        section, key = path
        raw[section][key] = value

    app_raw = raw["app"]
    database_raw = raw["database"]
    checkpoint_raw = raw["checkpoint"]
    doudian_raw = raw["doudian"]
    agent_raw = raw["agent"]
    notifier_raw = raw["notifier"]
    effects_raw = raw["effects"]
    observability_raw = raw["observability"]
    # 上面八行只是把段取出来并**保证它们都是 dict**（缺省的段已在循环里补成 `{}`）：
    # 后面每一段都从这里取值，不再出现 `raw["app"]` 这种重复索引。

    # --- app ---
    app_env = str(app_raw.get("env", "dev"))
    # 只允许 dev/prod：`staging`/`production` 这类写法会让 `is_prod` 静默为 False，
    # 于是"生产环境的风险闸门"整条失效。
    _require(app_env in {"dev", "prod"}, f"app.env 只能是 dev/prod，收到 {app_env!r}")
    # 前缀必须以 `/` 开头：`include_router(prefix=...)` 不接受没有斜杠的写法，
    # 而且是**启动期**才炸（路由注册时），所以在这里先拦。
    api_prefix = str(app_raw.get("api_prefix", "/api"))
    _require(api_prefix.startswith("/"), f"app.api_prefix 必须以 / 开头，收到 {api_prefix!r}")
    cors = app_raw.get("cors_origins", ["http://localhost:5173"])
    _require(
        isinstance(cors, Sequence) and not isinstance(cors, (str, bytes)),
        "app.cors_origins 必须是列表",
    )
    # 每一项都过 `_as_str`：CORS 里一个空串会把 `allow_origins` 变成含空值的列表，
    # 那种配置在 starlette 那边不会报错，但会静默放行/拒绝一批来源。
    app_cfg = AppConfig(
        name=_as_str(app_raw.get("name", "doudian-agent"), "app.name"),
        env=app_env,
        api_prefix=api_prefix,
        cors_origins=tuple(_as_str(item, "app.cors_origins[]") for item in cors),
        request_timeout_s=_as_int(
            app_raw.get("request_timeout_s", 30), "app.request_timeout_s", minimum=1
        ),
    )

    # --- database ---
    # 只要非空字符串：驱动/路径的合法性由 SQLAlchemy 在 `create_engine` 时报（那是启动期，
    # 同样属于"起不来"这一类，不需要在这里重写一遍驱动白名单）。
    db_url = _as_str(database_raw.get("url", "sqlite+pysqlite:///./data/doudian.db"), "database.url")
    database_cfg = DatabaseConfig(
        url=db_url,
        echo=_as_bool(database_raw.get("echo", False), "database.echo"),
        pool_size=_as_int(database_raw.get("pool_size", 5), "database.pool_size", minimum=1),
    )

    # --- checkpoint ---
    # `frozen` + `slots` 的 dataclass 全部在这里一次性组装：任何一段的校验失败都不会
    # 留下半份已经生效的配置（进程要么拿到完整 Settings，要么根本没起来）。
    checkpoint_type = str(checkpoint_raw.get("type", "sqlite"))
    _require(
        checkpoint_type in {"sqlite", "postgres"},
        f"checkpoint.type 只能是 sqlite/postgres，收到 {checkpoint_type!r}",
    )
    checkpoint_path = str(checkpoint_raw.get("path", "data/checkpoint.db"))
    checkpoint_url = str(checkpoint_raw.get("url", "") or "")
    if checkpoint_type == "postgres":
        # 两种驱动前缀都认：`postgresql+psycopg://` 是 SQLAlchemy 的显式驱动写法。
        _require(
            checkpoint_url.startswith(("postgresql://", "postgresql+psycopg://")),
            "checkpoint.type=postgres 时 checkpoint.url 必填且必须是 postgresql:// URL",
        )
    else:
        # sqlite 分支要 path 非空：空 path 会变成 `Path("")`（= 当前目录），建库时直接炸。
        _require(bool(checkpoint_path), "checkpoint.type=sqlite 时 checkpoint.path 不能为空")
    checkpoint_cfg = CheckpointConfig(
        type=checkpoint_type, path=checkpoint_path, url=checkpoint_url
    )

    # --- doudian ---
    base_url = _as_str(
        doudian_raw.get("base_url", "https://openapi-fxg.jinritemai.com"), "doudian.base_url"
    )
    # 必须是 http(s)：写成 `openapi-fxg.jinritemai.com`（漏协议）会让 httpx 在第一次请求时
    # 才报错——那时已经"启动成功"了，属于最晚的一次发现。
    _require(
        base_url.startswith(("http://", "https://")),
        f"doudian.base_url 必须是 http(s) URL，收到 {base_url!r}",
    )
    sign_method = str(doudian_raw.get("sign_method", "hmac-sha256"))
    # 只支持 hmac-sha256：签名算法只有一处实现（`services/signing.py`），
    # 允许配置成别的值等于允许"启动成功、每个请求都签名失败"。
    _require(
        sign_method == "hmac-sha256",
        f"doudian.sign_method 只支持 hmac-sha256，收到 {sign_method!r}",
    )
    timeout_raw = _nested("doudian", "timeout", doudian_raw)
    limits_raw = _nested("doudian", "limits", doudian_raw)
    rate_raw = _nested("doudian", "rate_limit", doudian_raw)
    # 四项下限都是 0.1 秒（不是 0）：0 会让 httpx 直接超时，"配置成 0 关掉超时"是做不到的。
    timeout_cfg = TimeoutConfig(
        connect_s=_as_float(timeout_raw.get("connect_s", 3.0), "doudian.timeout.connect_s", minimum=0.1),
        read_s=_as_float(timeout_raw.get("read_s", 10.0), "doudian.timeout.read_s", minimum=0.1),
        write_s=_as_float(timeout_raw.get("write_s", 5.0), "doudian.timeout.write_s", minimum=0.1),
        pool_s=_as_float(timeout_raw.get("pool_s", 2.0), "doudian.timeout.pool_s", minimum=0.1),
    )
    limits_cfg = LimitsConfig(
        max_connections=_as_int(
            limits_raw.get("max_connections", 20), "doudian.limits.max_connections", minimum=1
        ),
        # keepalive 允许 0：等于"不复用连接"，调试时有用（生产不建议）。
        max_keepalive_connections=_as_int(
            limits_raw.get("max_keepalive_connections", 10),
            "doudian.limits.max_keepalive_connections",
            minimum=0,
        ),
        keepalive_expiry_s=_as_float(
            limits_raw.get("keepalive_expiry_s", 30.0),
            "doudian.limits.keepalive_expiry_s",
            minimum=0.0,
        ),
    )
    burst = _as_int(rate_raw.get("burst", 16), "doudian.rate_limit.burst", minimum=1)
    # 限流桶的参数与 `DoudianClient` 共用同一份配置：`rate_per_sec`/`burst` 决定
    # 令牌桶，`max_concurrency` 决定同时在飞的请求数（两者是不同维度，不能互相替代）。
    rate_cfg = RateLimitConfig(
        rate_per_sec=_as_float(
            rate_raw.get("rate_per_sec", 8.0), "doudian.rate_limit.rate_per_sec", minimum=0.001
        ),
        burst=burst,
        max_concurrency=_as_int(
            rate_raw.get("max_concurrency", 4), "doudian.rate_limit.max_concurrency", minimum=1
        ),
        max_attempts=_as_int(
            rate_raw.get("max_attempts", 4), "doudian.rate_limit.max_attempts", minimum=1
        ),
        backoff_base_s=_as_float(
            rate_raw.get("backoff_base_s", 0.4), "doudian.rate_limit.backoff_base_s", minimum=0.0
        ),
        backoff_max_s=_as_float(
            rate_raw.get("backoff_max_s", 8.0), "doudian.rate_limit.backoff_max_s", minimum=0.0
        ),
    )
    _require(
        rate_cfg.backoff_max_s >= rate_cfg.backoff_base_s,
        "doudian.rate_limit.backoff_max_s 必须 >= backoff_base_s",
    )
    # `rstrip("/")` 去掉尾部斜杠：拼接路径时 `base_url + "/api"` 会变成双斜杠，
    # 某些网关会把双斜杠当成不同的路径（签名也就对不上）。
    doudian_cfg = DoudianConfig(
        base_url=base_url.rstrip("/"),
        version=_as_int(doudian_raw.get("version", 2), "doudian.version", minimum=1),
        sign_method=sign_method,
        # 三个 `*_env` 只校验"非空字符串"：它们是变量**名**，值在 client 启动时读。
        # 拼错的变量名会在第一次请求时表现为"缺少凭据"，而不是在这里。
        app_key_env=_as_str(
            doudian_raw.get("app_key_env", "DOUDIAN_APP_KEY"), "doudian.app_key_env"
        ),
        app_secret_env=_as_str(
            doudian_raw.get("app_secret_env", "DOUDIAN_APP_SECRET"), "doudian.app_secret_env"
        ),
        access_token_env=_as_str(
            doudian_raw.get("access_token_env", "DOUDIAN_ACCESS_TOKEN"),
            "doudian.access_token_env",
        ),
        dry_run=_as_bool(doudian_raw.get("dry_run", True), "doudian.dry_run"),
        timeout=timeout_cfg,
        limits=limits_cfg,
        rate_limit=rate_cfg,
    )

    # --- agent ---
    auto_approve_risk = str(agent_raw.get("auto_approve_risk", "none"))
    # 只允许 none/low：`medium`/`high` 的自动批准会让"改价改库存"这类不可逆动作无人确认。
    _require(
        auto_approve_risk in {"none", "low"},
        f"agent.auto_approve_risk 只能是 none/low，收到 {auto_approve_risk!r}",
    )
    agent_cfg = AgentConfig(
        # 每个上限都是 `>= 1`：0 会让"这块能力"被静默关掉（例如 max_actions_per_run=0
        # 就是"永不提议动作"），而那看起来像"今天没异常"。
        max_anomalies=_as_int(agent_raw.get("max_anomalies", 20), "agent.max_anomalies", minimum=1),
        max_actions_per_run=_as_int(
            agent_raw.get("max_actions_per_run", 5), "agent.max_actions_per_run", minimum=1
        ),
        approval_timeout_min=_as_int(
            agent_raw.get("approval_timeout_min", 1440), "agent.approval_timeout_min", minimum=1
        ),
        auto_approve_risk=auto_approve_risk,
        recursion_limit=_as_int(
            agent_raw.get("recursion_limit", 50), "agent.recursion_limit", minimum=1
        ),
    )

    # --- notifier ---
    notifier_mode = str(notifier_raw.get("mode", "dry_run"))
    # 默认 dry_run 是安全侧：没显式配 webhook 就绝不往外发消息。
    _require(
        notifier_mode in {"dry_run", "feishu"},
        f"notifier.mode 只能是 dry_run/feishu，收到 {notifier_mode!r}",
    )
    notifier_cfg = NotifierConfig(
        mode=notifier_mode,
        feishu_webhook_env=_as_str(
            notifier_raw.get("feishu_webhook_env", "FEISHU_WEBHOOK_URL"),
            "notifier.feishu_webhook_env",
        ),
    )

    # --- effects ---
    # 兼容 `effects.windows_days` 写成一个数字的写法在 `_as_int` 那层就失败了，
    # 所以这里只需处理"列表/元组"这一种：`Sequence` 判定同时排掉 str/bytes
    # （字符串也是 Sequence，直接判会让 `"0,1,3"` 被逐字符解析成数字）。
    windows_raw = effects_raw.get("windows_days", [0, 1, 3, 7])
    _require(
        isinstance(windows_raw, Sequence) and not isinstance(windows_raw, (str, bytes)),
        "effects.windows_days 必须是列表",
    )
    windows = tuple(
        _as_int(item, "effects.windows_days[]", minimum=0) for item in windows_raw
    )
    # 三条约束缺一不可：非空（否则不登记任何窗口）、是 (0,1,3,7) 的子集（契约冻结的天数）、
    # 严格升序不重复（`UNIQUE(action_log_id, offset_days)` 与报告的窗口顺序都依赖它）。
    _require(bool(windows), "effects.windows_days 不能为空")
    _require(
        set(windows) <= set(_ALLOWED_WINDOWS),
        f"effects.windows_days 必须是 {list(_ALLOWED_WINDOWS)} 的子集，收到 {list(windows)}",
    )
    _require(
        list(windows) == sorted(windows) and len(set(windows)) == len(windows),
        f"effects.windows_days 必须严格升序且不重复，收到 {list(windows)}",
    )
    # `windows` 到这里已经是"可用的窗口清单"，之后的 dataclass 只是搬运，不再校验。
    effects_cfg = EffectsConfig(
        windows_days=windows,
        scheduler_enabled=_as_bool(
            effects_raw.get("scheduler_enabled", True), "effects.scheduler_enabled"
        ),
        poll_interval_s=_as_int(
            effects_raw.get("poll_interval_s", 60), "effects.poll_interval_s", minimum=1
        ),
    )

    # --- observability ---
    log_level = str(observability_raw.get("log_level", "INFO")).upper()
    # `.upper()` 先归一化再校验：yaml 里写 `info` 很常见，不该因此启动失败。
    # 但拼错（`INFQ`）必须失败——那会让日志级别静默退回默认值。
    _require(
        log_level in _LOG_LEVELS,
        f"observability.log_level 只能是 {list(_LOG_LEVELS)}，收到 {log_level!r}",
    )
    redact_raw = observability_raw.get(
        "redact_keys", ["app_secret", "access_token", "sign", "authorization", "cookie"]
    )
    _require(
        isinstance(redact_raw, Sequence) and not isinstance(redact_raw, (str, bytes)),
        "observability.redact_keys 必须是列表",
    )
    observability_cfg = ObservabilityConfig(
        log_level=log_level,
        log_file=_as_str(
            observability_raw.get("log_file", "data/logs/app.jsonl"), "observability.log_file"
        ),
        # 逐项 `_as_str`：空的脱敏键（`""`）会让 redact 把每个空键都匹配一遍，纯属浪费。
        redact_keys=tuple(_as_str(item, "observability.redact_keys[]") for item in redact_raw),
    )

    return Settings(
        app=app_cfg,
        database=database_cfg,
        checkpoint=checkpoint_cfg,
        doudian=doudian_cfg,
        agent=agent_cfg,
        notifier=notifier_cfg,
        effects=effects_cfg,
        observability=observability_cfg,
    )


# --------------------------------------------------------------------------------------
# 启动期风险提示（§8.2：`prod` 下禁止 dry_run=true **静默**启动）
# --------------------------------------------------------------------------------------
# 两层：`startup_warnings` 只**返回文案**（纯函数，可断言），`assert_serving_safe` 决定
# 要不要据此拒绝启动。分开是为了让"哪些组合危险"可测，而"拒绝启动"这个副作用只有一处。
def startup_warnings(settings: Settings) -> tuple[str, ...]:
    """返回启动期必须喊出来的风险横幅（无风险 = 空元组）。

    纯函数：调用方（`app.main` lifespan / CLI）负责打日志与决定是否阻断。
    放在这里而不是散在 main 里，是为了让"哪些配置组合危险"只有一处定义，
    并且能在测试里断言——不然这条规则会随着启动流程重构悄悄消失。
    """
    warnings: list[str] = []
    if settings.prod_dry_run:
        # 唯一一条启动期风险：prod + dry_run。其余组合（dev + dry_run）是正常的开发形态。
        warnings.append(
            "PROD + DRY_RUN：app.env=prod 且 doudian.dry_run=true —— "
            + PROD_DRY_RUN_CONSEQUENCE
            + "（设置 DOUDIAN_APP_KEY/SECRET/ACCESS_TOKEN 并把 doudian.dry_run 置 false）"
        )
    return tuple(warnings)


def assert_serving_safe(settings: Settings) -> None:
    """对外服务前的最后一道闸（`app.main` lifespan 必须调用）。

    为什么是"拒绝启动"而不只是 warn：prod 下带 dry_run 对外服务属于**沉默型事故**——
    接口全 200、`action_log` 全 succeeded、看板在看，而上游抖店没被改过一个字节。
    这种错误没人会去翻日志，只能靠"起不来"逼配置改对。
    """
    if settings.prod_dry_run:
        # 这里是**抛**而不是 warn：prod 下带 dry_run 对外服务属于沉默型事故——
        # 接口全 200、action_log 全 succeeded、看板在看，而上游没被改过一个字节。
        # 这种错误没人会去翻日志，只能靠"起不来"逼配置改对。
        raise ConfigError(
            "拒绝在 prod 下以 doudian.dry_run=true 启动：" + PROD_DRY_RUN_CONSEQUENCE
        )


def load_settings(path: str | Path | None = None) -> Settings:
    """读 yaml + 环境变量组装 `Settings`。任何问题都在**启动期**抛 `ConfigError`。"""
    target = Path(path) if path is not None else default_settings_path()
    if not target.exists():
        # 缺文件**不**降级成"全默认值"：一个没写配置就起来的服务，看起来一切正常、
        # 实际 dry_run/密钥全都没配——比起不来危险得多。
        raise ConfigError(
            f"找不到配置文件: {target}"
            "（先写 config/settings.yaml，或显式传 path；不做静默默认值降级）"
        )
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        # 权限/编码/IO 错误也折成 ConfigError：启动期只抛一种异常，调用方的处理就一处。
        raise ConfigError(f"读取配置文件失败: {target}: {exc}") from exc
    try:
        # `safe_load` 而不是 `load`：后者能构造任意对象（yaml 反序列化 RCE 的经典面）。
        # `from exc` 保留原始栈：报错时能看出是第几行 yaml 语法错。
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML: {target}: {exc}") from exc
    if data is None:
        # 空文件 → 空配置（全部走默认值），这是合法的"最小配置"。
        data = {}
    if not isinstance(data, Mapping):
        # 顶层是 list/str 的 yaml（例如只写了一段列表）不可能是配置。
        raise ConfigError(f"配置文件顶层必须是映射: {target}")
    settings = build_settings(data)
    # 启动期就把风险喊出来（不是等第一个请求）：此时 logging 可能还没装配，
    # 记录会经 lastResort 落到 stderr——"没被配置成静默"本身就是要求。
    #
    # 顺序意义：先 `build_settings`（可能抛）再喊横幅——配置本身都错时，
    # 横幅里描述的组合还没确定，喊出来只会误导。
    _emit_startup_warnings(settings)
    return settings


def _emit_startup_warnings(settings: Settings) -> None:
    """把 `startup_warnings` 的每一条以 `config.startup_warning` 事件写进日志。"""
    warnings = startup_warnings(settings)
    if not warnings:
        return
    # 用 `error` 级而不是 `warning`：这两条横幅描述的都是"生产会静默出错"的组合，
    # 落在 warning 级会被默认的日志级别过滤掉。
    logger.error(
        "启动期配置风险（%d 条）：%s",
        len(warnings),
        " | ".join(warnings),
        extra={
            "event": "config.startup_warning",
            "data": {
                "prod_dry_run": settings.prod_dry_run,
                "app_env": settings.app.env,
                "warnings": list(warnings),
            },
        },
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """进程级缓存（同一进程只有一个 `Settings` 实例）。

    保证：首次调用后同进程内恒返回同一个对象（`is` 相等）；省钱也保证了
        "同一份配置只被解析一次"（占位符解析不是免费的）。
    不保证：不保证与**文件**同步——改完 `settings.yaml` 必须重启（或 `reset_settings_cache()`）。
    调用方坑：首次调用会读文件、可能抛 `ConfigError`；在请求处理路径里第一次调它意味着
        "配置错误变成 500"，所以入口层（`main.create_app`）必须在启动期先调一次。
    """
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_cache() -> None:
    """测试用：清掉缓存。

    保证：下次 `get_settings()` 会重新读文件/环境变量。
    不保证：不影响已经持有旧 `Settings` 的对象（它们是不可变快照，不会跟着变）。
    调用方坑：只在测试与"热重载配置"的工具里调用；生产路径调它会让同一进程出现两份配置。
    """
    global _settings
    _settings = None


def load_rules(path: str | Path | None = None) -> Any:
    """规则集加载（CONTRACT §8.1 列在本模块）。

    实现只有一处：`app/services/rules.py`（agent-core 维护）。这里只做转发，
    避免"两个 load_rules、两套校验"。延迟 import 是为了不让 config → rules 成为
    模块级依赖（rules 反过来可能需要配置）。
    """
    from app.services.rules import load_rules as _load_rules

    # 函数内 import：`rules` 反过来会读配置，模块级 import 会成环（config → rules → config）。
    return _load_rules(path)
