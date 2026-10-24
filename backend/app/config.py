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
    name: str = "doudian-agent"
    env: str = "dev"
    api_prefix: str = "/api"
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    request_timeout_s: int = 30


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    url: str = "sqlite+pysqlite:///./data/doudian.db"
    echo: bool = False
    pool_size: int = 5


@dataclass(frozen=True, slots=True)
class CheckpointConfig:
    type: str = "sqlite"
    path: str = "data/checkpoint.db"
    url: str = ""


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    connect_s: float = 3.0
    read_s: float = 10.0
    write_s: float = 5.0
    pool_s: float = 2.0

    def to_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_s,
            read=self.read_s,
            write=self.write_s,
            pool=self.pool_s,
        )


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    max_connections: int = 20
    max_keepalive_connections: int = 10
    keepalive_expiry_s: float = 30.0

    def to_httpx(self) -> httpx.Limits:
        return httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
            keepalive_expiry=self.keepalive_expiry_s,
        )


@dataclass(frozen=True, slots=True)
class DoudianConfig:
    base_url: str = "https://openapi-fxg.jinritemai.com"
    version: int = 2
    sign_method: str = "hmac-sha256"
    app_key_env: str = "DOUDIAN_APP_KEY"
    app_secret_env: str = "DOUDIAN_APP_SECRET"
    access_token_env: str = "DOUDIAN_ACCESS_TOKEN"
    dry_run: bool = True
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    max_anomalies: int = 20
    max_actions_per_run: int = 5
    approval_timeout_min: int = 1440
    auto_approve_risk: str = "none"
    recursion_limit: int = 50


@dataclass(frozen=True, slots=True)
class NotifierConfig:
    mode: str = "dry_run"
    feishu_webhook_env: str = "FEISHU_WEBHOOK_URL"


@dataclass(frozen=True, slots=True)
class EffectsConfig:
    windows_days: tuple[int, ...] = (0, 1, 3, 7)
    scheduler_enabled: bool = True
    poll_interval_s: int = 60


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    log_level: str = "INFO"
    log_file: str = "data/logs/app.jsonl"
    redact_keys: tuple[str, ...] = (
        "app_secret",
        "access_token",
        "sign",
        "authorization",
        "cookie",
    )


@dataclass(frozen=True, slots=True)
class Settings:
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
        return self.app.env == "prod"

    @property
    def prod_dry_run(self) -> bool:
        """生产 + 离线夹具：Agent 会"假成功"。见 `PROD_DRY_RUN_CONSEQUENCE`。"""
        return self.is_prod and bool(self.doudian.dry_run)


# --------------------------------------------------------------------------------------
# 环境变量映射（§8.4 冻结名字；不做前缀推导，避免歧义）
# --------------------------------------------------------------------------------------
_ENV_TO_PATH: dict[str, tuple[str, ...]] = {
    "DATABASE_URL": ("database", "url"),
    "CHECKPOINT_TYPE": ("checkpoint", "type"),
    "CHECKPOINT_PATH": ("checkpoint", "path"),
    "CHECKPOINT_URL": ("checkpoint", "url"),
    "LOG_LEVEL": ("observability", "log_level"),
}

_SECTION_KEYS: dict[str, frozenset[str]] = {
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
    return BACKEND_ROOT / "config" / "settings.yaml"


def _resolve_placeholders(value: Any, env: Mapping[str, str], *, where: str) -> Any:
    """把 `${VAR}` / `${VAR:-default}` 解析成实际值（递归处理 dict/list/str）。"""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in env and env[name] != "":
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"配置项 {where} 引用的环境变量 {name} 未设置且无默认值")

        return _PLACEHOLDER.sub(_sub, value)
    if isinstance(value, Mapping):
        return {k: _resolve_placeholders(v, env, where=f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [
            _resolve_placeholders(item, env, where=f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _check_keys(section: str, data: Mapping[str, Any]) -> None:
    allowed = _SECTION_KEYS[section]
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"settings.yaml 的 {section} 段出现未知字段: {sorted(unknown)}")


def _nested(section: str, key: str, data: Mapping[str, Any]) -> dict[str, Any]:
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
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} 必须是整数，收到 {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where} 必须 >= {minimum}，收到 {value}")
    return value


def _as_float(value: Any, where: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必须是数字，收到 {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"{where} 必须 >= {minimum}，收到 {number}")
    return number


def _as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} 必须是布尔，收到 {value!r}")
    return value


def _as_str(value: Any, where: str) -> str:
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
    """纯函数：由 yaml 字典 + 环境变量组装 `Settings`（不做 I/O，测试直接用）。"""
    env = dict(os.environ if env is None else env)
    raw: dict[str, Any] = dict(data or {})
    unknown_sections = set(raw) - set(_SECTION_KEYS)
    if unknown_sections:
        raise ConfigError(f"settings.yaml 出现未知配置段: {sorted(unknown_sections)}")

    raw = _resolve_placeholders(raw, env, where="settings")
    for section in _SECTION_KEYS:
        value = raw.get(section)
        if value is None:
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

    # --- app ---
    app_env = str(app_raw.get("env", "dev"))
    _require(app_env in {"dev", "prod"}, f"app.env 只能是 dev/prod，收到 {app_env!r}")
    api_prefix = str(app_raw.get("api_prefix", "/api"))
    _require(api_prefix.startswith("/"), f"app.api_prefix 必须以 / 开头，收到 {api_prefix!r}")
    cors = app_raw.get("cors_origins", ["http://localhost:5173"])
    _require(
        isinstance(cors, Sequence) and not isinstance(cors, (str, bytes)),
        "app.cors_origins 必须是列表",
    )
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
    db_url = _as_str(database_raw.get("url", "sqlite+pysqlite:///./data/doudian.db"), "database.url")
    database_cfg = DatabaseConfig(
        url=db_url,
        echo=_as_bool(database_raw.get("echo", False), "database.echo"),
        pool_size=_as_int(database_raw.get("pool_size", 5), "database.pool_size", minimum=1),
    )

    # --- checkpoint ---
    checkpoint_type = str(checkpoint_raw.get("type", "sqlite"))
    _require(
        checkpoint_type in {"sqlite", "postgres"},
        f"checkpoint.type 只能是 sqlite/postgres，收到 {checkpoint_type!r}",
    )
    checkpoint_path = str(checkpoint_raw.get("path", "data/checkpoint.db"))
    checkpoint_url = str(checkpoint_raw.get("url", "") or "")
    if checkpoint_type == "postgres":
        _require(
            checkpoint_url.startswith(("postgresql://", "postgresql+psycopg://")),
            "checkpoint.type=postgres 时 checkpoint.url 必填且必须是 postgresql:// URL",
        )
    else:
        _require(bool(checkpoint_path), "checkpoint.type=sqlite 时 checkpoint.path 不能为空")
    checkpoint_cfg = CheckpointConfig(
        type=checkpoint_type, path=checkpoint_path, url=checkpoint_url
    )

    # --- doudian ---
    base_url = _as_str(
        doudian_raw.get("base_url", "https://openapi-fxg.jinritemai.com"), "doudian.base_url"
    )
    _require(
        base_url.startswith(("http://", "https://")),
        f"doudian.base_url 必须是 http(s) URL，收到 {base_url!r}",
    )
    sign_method = str(doudian_raw.get("sign_method", "hmac-sha256"))
    _require(
        sign_method == "hmac-sha256",
        f"doudian.sign_method 只支持 hmac-sha256，收到 {sign_method!r}",
    )
    timeout_raw = _nested("doudian", "timeout", doudian_raw)
    limits_raw = _nested("doudian", "limits", doudian_raw)
    rate_raw = _nested("doudian", "rate_limit", doudian_raw)
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
    doudian_cfg = DoudianConfig(
        base_url=base_url.rstrip("/"),
        version=_as_int(doudian_raw.get("version", 2), "doudian.version", minimum=1),
        sign_method=sign_method,
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
    _require(
        auto_approve_risk in {"none", "low"},
        f"agent.auto_approve_risk 只能是 none/low，收到 {auto_approve_risk!r}",
    )
    agent_cfg = AgentConfig(
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
    windows_raw = effects_raw.get("windows_days", [0, 1, 3, 7])
    _require(
        isinstance(windows_raw, Sequence) and not isinstance(windows_raw, (str, bytes)),
        "effects.windows_days 必须是列表",
    )
    windows = tuple(
        _as_int(item, "effects.windows_days[]", minimum=0) for item in windows_raw
    )
    _require(bool(windows), "effects.windows_days 不能为空")
    _require(
        set(windows) <= set(_ALLOWED_WINDOWS),
        f"effects.windows_days 必须是 {list(_ALLOWED_WINDOWS)} 的子集，收到 {list(windows)}",
    )
    _require(
        list(windows) == sorted(windows) and len(set(windows)) == len(windows),
        f"effects.windows_days 必须严格升序且不重复，收到 {list(windows)}",
    )
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
def startup_warnings(settings: Settings) -> tuple[str, ...]:
    """返回启动期必须喊出来的风险横幅（无风险 = 空元组）。

    纯函数：调用方（`app.main` lifespan / CLI）负责打日志与决定是否阻断。
    放在这里而不是散在 main 里，是为了让"哪些配置组合危险"只有一处定义，
    并且能在测试里断言——不然这条规则会随着启动流程重构悄悄消失。
    """
    warnings: list[str] = []
    if settings.prod_dry_run:
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
        raise ConfigError(
            "拒绝在 prod 下以 doudian.dry_run=true 启动：" + PROD_DRY_RUN_CONSEQUENCE
        )


def load_settings(path: str | Path | None = None) -> Settings:
    """读 yaml + 环境变量组装 `Settings`。任何问题都在**启动期**抛 `ConfigError`。"""
    target = Path(path) if path is not None else default_settings_path()
    if not target.exists():
        raise ConfigError(
            f"找不到配置文件: {target}"
            "（先写 config/settings.yaml，或显式传 path；不做静默默认值降级）"
        )
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取配置文件失败: {target}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML: {target}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置文件顶层必须是映射: {target}")
    settings = build_settings(data)
    # 启动期就把风险喊出来（不是等第一个请求）：此时 logging 可能还没装配，
    # 记录会经 lastResort 落到 stderr——"没被配置成静默"本身就是要求。
    _emit_startup_warnings(settings)
    return settings


def _emit_startup_warnings(settings: Settings) -> None:
    """把 `startup_warnings` 的每一条以 `config.startup_warning` 事件写进日志。"""
    warnings = startup_warnings(settings)
    if not warnings:
        return
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
    """进程级缓存（同一进程只有一个 `Settings` 实例）。"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_cache() -> None:
    """测试用：清掉缓存。"""
    global _settings
    _settings = None


def load_rules(path: str | Path | None = None) -> Any:
    """规则集加载（CONTRACT §8.1 列在本模块）。

    实现只有一处：`app/services/rules.py`（agent-core 维护）。这里只做转发，
    避免"两个 load_rules、两套校验"。延迟 import 是为了不让 config → rules 成为
    模块级依赖（rules 反过来可能需要配置）。
    """
    from app.services.rules import load_rules as _load_rules

    return _load_rules(path)
