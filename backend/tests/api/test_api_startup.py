"""启动/恢复路径（CONTRACT §8.1 / §4.7）：这些行为只在"进程真的启动"时发生，端点测试覆盖不到。

两条断言：

1. **启动对账**：`agent_traces.step_count` 被人为改小（等价于"上次进程在写日志与写库之间被杀"）后，
   重启一次必须按 JSONL 里的 `node.trace` 行数把它校准回来，且**只增不减**（§4.7）。
   这是 lifespan 的责任，不是任何端点的责任——所以必须在这里单独测。
2. **prod + dry_run 拒绝启动**：`create_app()` 直接抛 `ConfigError`，进程非 0 退出（§8.2）。
   这条不能用"发个请求看看"验证：它必须发生在**第一个请求之前**。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.agent import adapters
from app.agent.tracing import get_run_trace
from app.config import Settings, load_settings
from app.db import run_db
from app.errors import ConfigError
from app.main import create_app
from app.models import AgentTrace
from app.observability.logging import get_logger
from app.services.doudian_client import DoudianClient, StaticTokenSource
from app.services.notifier import DryRunNotifier
from app.services.rules import load_rules

from .conftest import FIXED_NOW, SHOP_ID, MockUpstream


def _fake_client(settings: Settings, upstream: MockUpstream) -> DoudianClient:
    return DoudianClient(
        app_key="test-app-key",
        app_secret="test-app-secret",
        base_url="https://dry-run.invalid",
        token_source=StaticTokenSource("dry-run-token"),
        transport=httpx.MockTransport(upstream.handler),
        timeout=settings.doudian.timeout.to_httpx(),
        limits=settings.doudian.limits.to_httpx(),
        rate_limit=settings.doudian.rate_limit,
        redact_keys=settings.observability.redact_keys,
    )


def _install_runtime(settings: Settings, client: DoudianClient, rules_path: Any) -> None:
    adapters.set_runtime(
        adapters.AgentRuntime(
            settings=settings,
            client=client,
            notifier=DryRunNotifier(
                logger=get_logger("tests.api.startup"), sink=adapters.require_log_sink()
            ),
            rules=load_rules(rules_path),
            clock=lambda: FIXED_NOW,
        )
    )


def _force_step_count(run_id: str, value: int) -> None:
    """把 `step_count` 直接改成 `value`（模拟崩溃窗口造成的偏差；只有测试会这么干）。"""

    def _fn(session: Any) -> None:
        session.execute(
            sa.update(AgentTrace)
            .where(AgentTrace.run_id == run_id)
            .values(step_count=value)
        )

    run_db(_fn)


async def test_restart_reconciles_step_count_of_non_terminal_run(
    api_settings: Settings, rules_path: Any
) -> None:
    upstream = MockUpstream()
    rules = rules_path

    # --- 第一次启动：跑一次 run，停在 awaiting_approval（非终态）---
    first_app = create_app(api_settings)
    client = _fake_client(api_settings, upstream)
    try:
        async with first_app.router.lifespan_context(first_app):
            _install_runtime(api_settings, client, rules)
            transport = httpx.ASGITransport(app=first_app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                started = await http.post("/api/agent/run", json={"shop_id": SHOP_ID})
                assert started.status_code == 202, started.text
                run = started.json()
            assert run["status"] == "awaiting_approval"

            trace = await get_run_trace(shop_id=SHOP_ID, run_id=run["run_id"])
            assert trace is not None
            real_steps = trace["step_count"]
            # collect / compute_metrics / detect_anomalies / explain / propose_actions / approval_gate
            assert real_steps == 6
            assert trace["status"] == "awaiting_approval"

            # 人为把库里的步数改小（= 进程在"写日志"与"写库"之间被杀留下的偏差）
            _force_step_count(run["run_id"], 1)
            adapters.reset_runtime()
    finally:
        await client.aclose()

    # --- 第二次启动：lifespan 对账把它校准回 JSONL 的真实条数 ---
    second_app = create_app(api_settings)
    async with second_app.router.lifespan_context(second_app):
        healed = await get_run_trace(shop_id=SHOP_ID, run_id=run["run_id"])
        assert healed is not None
        assert healed["step_count"] == real_steps
        # 只增不减：状态不会被启动流程碰（仍是挂起等审批）
        assert healed["status"] == "awaiting_approval"


def test_prod_with_dry_run_refuses_to_start(api_settings: Settings) -> None:
    """§8.2：prod + dry_run=true 必须在**启动期**拒绝，而不是带病对外服务。"""
    unsafe = dataclasses.replace(
        api_settings,
        app=dataclasses.replace(api_settings.app, env="prod"),
        doudian=dataclasses.replace(api_settings.doudian, dry_run=True),
    )
    with pytest.raises(ConfigError):
        create_app(unsafe)


def test_missing_settings_file_is_startup_failure(tmp_path: Any) -> None:
    """§8.1：找不到配置文件 → 启动期 `ConfigError`（不静默降级到默认值）。"""
    from app.config import load_settings as _load

    with pytest.raises(ConfigError):
        _load(tmp_path / "nope.yaml")


def test_default_settings_are_loadable() -> None:
    """仓里的 `config/settings.yaml` 必须能被加载（README 的"clone 下来就能跑"依赖它）。"""
    settings = load_settings()
    assert settings.app.api_prefix.startswith("/")
    assert settings.doudian.dry_run is True
