"""配置加载（CONTRACT §8.1 / §8.2 / §8.4）：优先级、占位符、启动期失败。

这组用例守的是"配置错了就起不来"：所有校验都发生在**加载期**，
而不是等第一个请求才炸。优先级固定为 环境变量 > yaml > 默认值。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    BACKEND_ROOT,
    assert_serving_safe,
    build_settings,
    get_settings,
    load_settings,
    reset_settings_cache,
    startup_warnings,
)
from app.errors import ConfigError

REPO_SETTINGS = BACKEND_ROOT / "config" / "settings.yaml"


# --------------------------------------------------------------------------------------
# 优先级
# --------------------------------------------------------------------------------------
def test_defaults_match_the_contract_when_yaml_is_empty() -> None:
    settings = build_settings({}, env={})
    assert settings.app.name == "doudian-agent"
    assert settings.app.env == "dev"
    assert settings.app.api_prefix == "/api"
    assert settings.app.cors_origins == ("http://localhost:5173",)
    assert settings.app.request_timeout_s == 30
    assert settings.database.url == "sqlite+pysqlite:///./data/doudian.db"
    assert settings.checkpoint.type == "sqlite"
    assert settings.checkpoint.path == "data/checkpoint.db"
    assert settings.doudian.base_url == "https://openapi-fxg.jinritemai.com"
    assert settings.doudian.version == 2
    assert settings.doudian.sign_method == "hmac-sha256"
    assert settings.doudian.dry_run is True
    assert settings.doudian.timeout.connect_s == 3.0
    assert settings.doudian.timeout.read_s == 10.0
    assert settings.doudian.limits.max_connections == 20
    assert settings.doudian.rate_limit.max_attempts == 4
    assert settings.agent.max_anomalies == 20
    assert settings.agent.max_actions_per_run == 5
    assert settings.agent.auto_approve_risk == "none"
    assert settings.notifier.mode == "dry_run"
    assert settings.effects.windows_days == (0, 1, 3, 7)
    assert settings.observability.log_level == "INFO"
    assert settings.observability.redact_keys == (
        "app_secret", "access_token", "sign", "authorization", "cookie",
    )


def test_env_beats_yaml_beats_default() -> None:
    yaml_data = {"database": {"url": "sqlite+pysqlite:///./data/from-yaml.db"}}
    assert build_settings(yaml_data, env={}).database.url.endswith("from-yaml.db")
    assert (
        build_settings(yaml_data, env={"DATABASE_URL": "postgresql+psycopg://u@h/db"}).database.url
        == "postgresql+psycopg://u@h/db"
    ), "环境变量优先级最高"

    assert (
        build_settings({"observability": {"log_level": "debug"}}, env={}).observability.log_level
        == "DEBUG"
    )
    assert (
        build_settings({"observability": {"log_level": "debug"}}, env={"LOG_LEVEL": "warning"})
        .observability.log_level
        == "WARNING"
    )


def test_empty_env_value_does_not_override() -> None:
    """`DATABASE_URL=`（空串）视为未设置，否则"清空变量"会变成"清空配置"。"""
    settings = build_settings({"database": {"url": "sqlite+pysqlite:///./data/x.db"}}, env={"DATABASE_URL": ""})
    assert settings.database.url.endswith("x.db")


def test_checkpoint_env_overrides() -> None:
    settings = build_settings(
        {},
        env={
            "CHECKPOINT_TYPE": "postgres",
            "CHECKPOINT_URL": "postgresql://u:p@h:5432/ckpt",
        },
    )
    assert settings.checkpoint.type == "postgres"
    assert settings.checkpoint.url == "postgresql://u:p@h:5432/ckpt"


# --------------------------------------------------------------------------------------
# 占位符
# --------------------------------------------------------------------------------------
def test_placeholder_without_default_fails_at_load_time() -> None:
    data = {"database": {"url": "${MISSING_DATABASE_URL}"}}
    with pytest.raises(ConfigError) as excinfo:
        build_settings(data, env={})
    assert "MISSING_DATABASE_URL" in str(excinfo.value)


def test_placeholder_with_default_uses_default_or_env() -> None:
    data = {"observability": {"log_file": "${LOG_PATH:-data/logs/fallback.jsonl}"}}
    assert build_settings(data, env={}).observability.log_file == "data/logs/fallback.jsonl"
    assert (
        build_settings(data, env={"LOG_PATH": "data/logs/custom.jsonl"}).observability.log_file
        == "data/logs/custom.jsonl"
    )


def test_placeholder_inside_list_and_nested_mapping() -> None:
    data = {
        "app": {"cors_origins": ["${ORIGIN:-http://localhost:5173}"]},
        "doudian": {"base_url": "${BASE_URL:-https://openapi-fxg.jinritemai.com}"},
    }
    settings = build_settings(data, env={})
    assert settings.app.cors_origins == ("http://localhost:5173",)
    assert settings.doudian.base_url == "https://openapi-fxg.jinritemai.com"


def test_empty_env_var_treated_as_unset_for_placeholder() -> None:
    data = {"observability": {"log_file": "${LOG_PATH:-data/logs/fallback.jsonl}"}}
    assert (
        build_settings(data, env={"LOG_PATH": ""}).observability.log_file
        == "data/logs/fallback.jsonl"
    )


# --------------------------------------------------------------------------------------
# 未知字段 / 类型 / 语义校验
# --------------------------------------------------------------------------------------
def test_unknown_section_or_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        build_settings({"doudiann": {}}, env={})
    assert "未知配置段" in str(excinfo.value)

    with pytest.raises(ConfigError) as excinfo:
        build_settings({"doudian": {"dryrun": True}}, env={})
    assert "dryrun" in str(excinfo.value)


def test_secrets_are_not_accepted_in_yaml() -> None:
    """密钥只从环境变量取：写进 yaml 直接启动失败（而不是"能用但泄露"）。"""
    for section, field in (
        ("doudian", "app_secret"),
        ("doudian", "access_token"),
        ("notifier", "webhook_url"),
    ):
        with pytest.raises(ConfigError):
            build_settings({section: {field: "leaked"}}, env={})


def test_type_validation_names_the_offending_field() -> None:
    with pytest.raises(ConfigError) as excinfo:
        build_settings({"doudian": {"version": "two"}}, env={})
    assert "doudian.version" in str(excinfo.value)

    with pytest.raises(ConfigError):
        build_settings({"observability": {"log_level": 10}}, env={})

    with pytest.raises(ConfigError):
        build_settings({"database": {"pool_size": 0}}, env={})

    with pytest.raises(ConfigError):
        build_settings({"doudian": {"timeout": {"read_s": "10s"}}}, env={})


def test_semantic_validation() -> None:
    cases = [
        {"app": {"env": "staging"}},
        {"app": {"api_prefix": "api"}},
        {"doudian": {"sign_method": "md5"}},
        {"doudian": {"base_url": "openapi-fxg.jinritemai.com"}},
        {"doudian": {"rate_limit": {"backoff_base_s": 4.0, "backoff_max_s": 1.0}}},
        {"agent": {"auto_approve_risk": "high"}},
        {"notifier": {"mode": "sms"}},
        {"effects": {"windows_days": [0, 3, 1]}},
        {"effects": {"windows_days": [0, 2]}},
        {"effects": {"windows_days": [0, 0, 1]}},
        {"effects": {"windows_days": []}},
        {"checkpoint": {"type": "postgres"}},
        {"checkpoint": {"type": "mysql"}},
    ]
    for data in cases:
        with pytest.raises(ConfigError):
            build_settings(data, env={})


def test_windows_days_subset_allowed() -> None:
    settings = build_settings({"effects": {"windows_days": [0, 1, 7]}}, env={})
    assert settings.effects.windows_days == (0, 1, 7)


def test_log_level_is_normalised_to_upper() -> None:
    assert (
        build_settings({"observability": {"log_level": "warning"}}, env={}).observability.log_level
        == "WARNING"
    )
    with pytest.raises(ConfigError):
        build_settings({"observability": {"log_level": "warn"}}, env={})
    with pytest.raises(ConfigError):
        build_settings({"observability": {"log_level": "VERBOSE"}}, env={})


# --------------------------------------------------------------------------------------
# 文件加载与进程缓存
# --------------------------------------------------------------------------------------
def test_load_settings_reads_the_shipped_yaml() -> None:
    settings = load_settings()
    assert settings.app.name == "doudian-agent"
    assert settings.doudian.dry_run is True, "dev 默认必须零凭据可跑"
    assert settings.effects.windows_days == (0, 1, 3, 7)
    assert settings.notifier.mode in {"dry_run", "feishu"}


def test_missing_file_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings(tmp_path / "nope.yaml")
    assert "找不到配置文件" in str(excinfo.value)


def test_invalid_yaml_is_a_startup_error(tmp_path: Path) -> None:
    broken = tmp_path / "settings.yaml"
    broken.write_text("app: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(broken)


def test_non_mapping_yaml_is_a_startup_error(tmp_path: Path) -> None:
    broken = tmp_path / "settings.yaml"
    broken.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(broken)


def test_get_settings_is_process_cached(monkeypatch) -> None:
    reset_settings_cache()
    first = get_settings()
    second = get_settings()
    assert first is second
    reset_settings_cache()
    assert get_settings() is not first


def test_shipped_yaml_has_no_plaintext_secrets() -> None:
    text = REPO_SETTINGS.read_text(encoding="utf-8")
    for token in ("app_secret:", "access_token:", "webhook_url:"):
        assert f"\n  {token}" not in text, f"settings.yaml 里不许出现真实凭据字段: {token}"


# --------------------------------------------------------------------------------------
# F1：prod + dry_run 必须在启动期喊出来（不许静默"假成功"）
# --------------------------------------------------------------------------------------
def test_prod_plus_dry_run_is_not_silent(capsys, tmp_path: Path) -> None:
    """§8.2：`prod` 下禁止 dry_run=true **静默**启动。

    这是沉默型事故：接口全 200、action_log 全 succeeded、看板有数，而上游没被改过。
    所以断言的不是"有个函数存在"，而是**启动加载路径真的发出了 error 级事件**。
    """
    import logging

    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "app:\n  env: prod\ndoudian:\n  dry_run: true\n", encoding="utf-8"
    )

    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("app.config")
    logger.handlers.clear()
    logger.addHandler(_Handler())
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    settings = load_settings(config_path)
    assert settings.prod_dry_run is True

    warnings = [r for r in records if getattr(r, "event", None) == "config.startup_warning"]
    assert len(warnings) == 1, "启动期必须且只能发一次，不许逐次请求 warn"
    record = warnings[0]
    assert record.levelno == logging.ERROR, "静默事故要用 error，不是 info/warn"
    payload = getattr(record, "data", {})
    assert payload["prod_dry_run"] is True
    assert payload["app_env"] == "prod"
    assert "action_log" in record.getMessage(), "横幅必须说清后果，不是只说『配置有问题』"


def test_dev_or_real_mode_has_no_banner() -> None:
    """dev+dry_run 是正常的离线模式；prod+真实凭据也正常。两者都不许刷告警。"""
    assert startup_warnings(build_settings({"app": {"env": "dev"}}, env={})) == ()
    assert (
        startup_warnings(
            build_settings(
                {"app": {"env": "prod"}, "doudian": {"dry_run": False}}, env={}
            )
        )
        == ()
    )


def test_assert_serving_safe_refuses_prod_dry_run() -> None:
    """对外服务前必须能一句话拒绝：prod + dry_run 直接起不来（不是只 warn）。"""
    dangerous = build_settings({"app": {"env": "prod"}, "doudian": {"dry_run": True}}, env={})
    with pytest.raises(ConfigError) as excinfo:
        assert_serving_safe(dangerous)
    assert "dry_run" in str(excinfo.value)

    safe_prod = build_settings({"app": {"env": "prod"}, "doudian": {"dry_run": False}}, env={})
    assert_serving_safe(safe_prod) is None

    dev = build_settings({}, env={})  # 默认 dev + dry_run = 本地离线开发模式
    assert_serving_safe(dev) is None
