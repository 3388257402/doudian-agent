"""API 层测试的公共夹具（CONTRACT §10.1 / §10.3 / §10.4）。

三条原则与 agent_core 的夹具一致，但这里刻意**不复用**那份 conftest：

1. **真走 HTTP**：`httpx.ASGITransport(app=...)` 直接打 ASGI 应用，不 monkeypatch 路由函数。
   这样被测的是"契约 §7 的端点形状"，不是"我调用了哪个服务层函数"。
2. **注入点用 §10.1 第 1 条**：`DoudianClient(transport=httpx.MockTransport(handler))` —— 一个
   真的 `DoudianClient`（真签名、真限流、真重试）+ 一个假上游。**不用 respx**（§10.1 禁止混用）。
   与 agent_core 的 `FakeDoudianClient` 的区别是刻意的：那边测图逻辑（只需 `.data`），
   这里测的是"API → 图 → client → 上游"整条链，client 本身必须是真的。
3. **零网络、零凭据、零共享状态**：每个用例一个 SQLite 文件、一个 checkpoint、一个 JSONL、
   一个 app 实例；时钟固定（`FIXED_NOW`），所以业务日期、指标、规则命中都是确定的。

`raise_app_exceptions=False`：Starlette 的 `ServerErrorMiddleware` 在**发出** 500 响应之后
会把异常原样 re-raise（交给服务器日志）。默认的 `True` 会让 httpx 抛异常、看不到 §7.1 包络；
这里要断言"本地 bug = 500 包络"，所以必须关掉它。
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 由 pytest 的 pythonpath 兜住
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agent import adapters  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.db import reset_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.observability.logging import get_logger  # noqa: E402
from app.services.doudian_client import DoudianClient, StaticTokenSource  # noqa: E402
from app.services.notifier import DryRunNotifier  # noqa: E402
from app.services.rules import load_rules  # noqa: E402

#: 固定时钟：UTC 2026-10-23 12:00 = 上海 20:00 → 业务日期 2026-10-23
FIXED_NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
TODAY = "2026-10-23"
SHOP_ID = "12345678"

#: 两个 SKU：`SKU_TOP_GMV` 的 GMV 最高（降价的目标），`SKU_TOP_REFUND` 的退款数最高（清库存的目标）
SKU_TOP_GMV = "3400000000000000002"
PRODUCT_TOP_GMV = "3400000000000000001"
SKU_TOP_REFUND = "3400000000000000012"
PRODUCT_TOP_REFUND = "3400000000000000011"


# --------------------------------------------------------------------------------------
# 假上游（httpx transport，按抖店 `method` 查询参数分派）
# --------------------------------------------------------------------------------------
def _orders() -> list[dict[str, Any]]:
    """近 7 天稳定 GMV + 今天暴跌 + 有退款：同时命中 gmv_drop_day 与 refund_spike。"""
    rows: list[dict[str, Any]] = []
    for offset in range(7):
        day = f"2026-10-{16 + offset:02d}"
        rows.append(
            {
                "order_id": f"h{offset}",
                "status": "paid",
                "pay_amount_cents": 1_000_000,
                "order_amount_cents": 1_000_000,
                "refund_amount_cents": 0,
                "product_id": PRODUCT_TOP_GMV,
                "sku_id": SKU_TOP_GMV,
                "create_time": f"{day} 12:00:00",
            }
        )
    rows.append(
        {
            "order_id": "today-ok",
            "status": "paid",
            "pay_amount_cents": 100_000,
            "order_amount_cents": 100_000,
            "refund_amount_cents": 0,
            "product_id": PRODUCT_TOP_GMV,
            "sku_id": SKU_TOP_GMV,
            "create_time": f"{TODAY} 09:12:00",
        }
    )
    rows.append(
        {
            "order_id": "today-refund",
            "status": "refunded",
            "pay_amount_cents": 9_900,
            "order_amount_cents": 9_900,
            "refund_amount_cents": 9_900,
            "refund_status": "refunded",
            "product_id": PRODUCT_TOP_REFUND,
            "sku_id": SKU_TOP_REFUND,
            "create_time": f"{TODAY} 13:05:00",
        }
    )
    return rows


def _products() -> list[dict[str, Any]]:
    return [
        {
            "product_id": PRODUCT_TOP_GMV,
            "sku_id": SKU_TOP_GMV,
            "title": "示例商品 A",
            "price": 19_900,
            "stock": 42,
            "gmv_cents": 900_000,
            "refund_order_count": 0,
            "is_on_sale": True,
        },
        {
            "product_id": PRODUCT_TOP_REFUND,
            "sku_id": SKU_TOP_REFUND,
            "title": "示例商品 B",
            "price": 25_900,
            "stock": 8,
            "gmv_cents": 200_000,
            "refund_order_count": 9,
            "is_on_sale": True,
        },
    ]


def _ok(request: httpx.Request, data: Any) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json={"err_no": 0, "message": "success", "data": data},
        request=request,
    )


class MockUpstream:
    """可编排的假抖店上游：记录每个请求，可按方法注入 HTTP 错误。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        #: method → HTTP 状态码（模拟上游 5xx；用来验证 502 `UPSTREAM_ERROR`）
        self.fail_status: dict[str, int] = {}
        #: method 集合：返回 HTTP 200 但 `err_no != 0`（business，不可重试 → run 降级为 failed）
        self.business_error: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.params.get("method", "")
        failure = self.fail_status.get(method)
        if failure is not None:
            return httpx.Response(
                status_code=failure,
                json={"err_no": -1, "message": f"upstream {failure} for {method}"},
                request=request,
            )
        if method in self.business_error:
            return httpx.Response(
                status_code=200,
                json={"err_no": 21000, "message": f"invalid param for {method}"},
                request=request,
            )
        if method == "shop.getShopInfo":
            return _ok(request, {"shop_id": SHOP_ID, "shop_name": "示例旗舰店"})
        if method == "order.searchList":
            return _ok(request, {"orders": _orders()})
        if method == "product.listV2":
            return _ok(request, {"products": _products()})
        if method in {"product.editPrice", "product.editStock"}:
            return _ok(request, {"updated": True})
        return httpx.Response(
            status_code=599,
            json={"err_no": -1, "message": f"no mock for {method}"},
            request=request,
        )

    def methods_called(self) -> list[str]:
        return [request.url.params.get("method", "") for request in self.requests]


# --------------------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------------------
@pytest.fixture()
def api_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """把库/检查点/日志指到 tmp，并把重试预算压到最短（测的是行为，不是等退避）。"""
    import app.config as config_module

    base = load_settings()
    resolved = dataclasses.replace(
        base,
        database=dataclasses.replace(
            base.database, url=f"sqlite+pysqlite:///{tmp_path}/doudian.db"
        ),
        checkpoint=dataclasses.replace(
            base.checkpoint, type="sqlite", path=str(tmp_path / "checkpoint.db")
        ),
        observability=dataclasses.replace(
            base.observability, log_file=str(tmp_path / "app.jsonl")
        ),
        doudian=dataclasses.replace(
            base.doudian,
            dry_run=True,  # §10.4：零凭据可跑
            rate_limit=dataclasses.replace(
                base.doudian.rate_limit,
                rate_per_sec=1_000.0,
                burst=1_000,
                max_attempts=2,
                backoff_base_s=0.0,
                backoff_max_s=0.0,
            ),
        ),
    )
    monkeypatch.setattr(config_module, "_settings", resolved, raising=False)
    yield resolved
    config_module.reset_settings_cache()


@pytest.fixture()
def rules_path(tmp_path: Path) -> Path:
    """两条确定性规则：GMV 暴跌 → 降价；退款率超阈值 → 清库存。

    用临时规则而不是仓里的 `config/rules.yaml`：那份会随运营调参变化，测试不该因为
    "多启了一条规则"而红。（仓里那份另有测试守着"能被加载"。）
    """
    path = tmp_path / "rules.yaml"
    path.write_text(
        """
version: "1"
rules:
  - id: gmv_drop_day
    enabled: true
    severity: warn
    metric: gmv_cents
    when: "gmv_cents < gmv_7d_avg_cents * 0.7"
    window: day
    description: "当日 GMV 低于近 7 日均值的 70%"
    actions:
      - type: price_update
        target_from: top_gmv_sku
        params: {price_drop_pct: 3}

  - id: refund_spike
    enabled: true
    severity: critical
    metric: refund_rate
    when: "refund_rate is not None and refund_rate > 0.08"
    window: day
    description: "退款率超过 8%"
    actions:
      - type: stock_update
        target_from: top_refund_sku
        params: {stock: 0}
""",
        encoding="utf-8",
    )
    return path


@dataclass
class ApiHarness:
    """一个已启动的应用 + 一个打进去的 HTTP 客户端 + 假上游。"""

    app: Any
    http: httpx.AsyncClient
    settings: Settings
    client: DoudianClient
    upstream: MockUpstream = field(default_factory=MockUpstream)

    def methods_called(self) -> list[str]:
        return self.upstream.methods_called()


@pytest.fixture()
async def api(api_settings: Settings, rules_path: Path) -> AsyncIterator[ApiHarness]:
    app = create_app(api_settings)
    upstream = MockUpstream()
    client = DoudianClient(
        app_key="test-app-key",
        app_secret="test-app-secret",
        base_url="https://dry-run.invalid",
        token_source=StaticTokenSource("dry-run-token"),
        transport=httpx.MockTransport(upstream.handler),
        timeout=api_settings.doudian.timeout.to_httpx(),
        limits=api_settings.doudian.limits.to_httpx(),
        rate_limit=api_settings.doudian.rate_limit,
        redact_keys=api_settings.observability.redact_keys,
    )
    try:
        async with app.router.lifespan_context(app):
            # lifespan 装的是按配置构建的运行时；测试用假上游 + 固定时钟接管它。
            adapters.set_runtime(
                adapters.AgentRuntime(
                    settings=api_settings,
                    client=client,
                    notifier=DryRunNotifier(
                        logger=get_logger("tests.api"), sink=adapters.require_log_sink()
                    ),
                    rules=load_rules(rules_path),
                    clock=lambda: FIXED_NOW,
                )
            )
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                yield ApiHarness(
                    app=app,
                    http=http,
                    settings=api_settings,
                    client=client,
                    upstream=upstream,
                )
            adapters.reset_runtime()
    finally:
        await client.aclose()
        reset_engine()


# --------------------------------------------------------------------------------------
# 断言用的小工具
# --------------------------------------------------------------------------------------
def assert_error_envelope(
    response: httpx.Response, *, code: str, status: int
) -> dict[str, Any]:
    """断言 §7.1 包络的形状（不只是状态码对）。返回 `error` 对象。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert error["code"] == code, error
    assert isinstance(error["message"], str) and error["message"]
    assert set(error) == {"code", "message", "details", "trace_id"}
    assert isinstance(error["trace_id"], str) and error["trace_id"]
    assert response.headers.get("x-trace-id") == error["trace_id"]
    return error


def decisions(action_ids: list[str], *, verdict: str = "approve") -> list[dict[str, Any]]:
    return [
        {
            "action_id": action_id,
            "verdict": verdict,
            "edited_params": None,
            "operator": "ops_zhang",
            "reason": None,
        }
        for action_id in action_ids
    ]


__all__ = [
    "ApiHarness",
    "FIXED_NOW",
    "MockUpstream",
    "PRODUCT_TOP_GMV",
    "PRODUCT_TOP_REFUND",
    "SHOP_ID",
    "SKU_TOP_GMV",
    "SKU_TOP_REFUND",
    "TODAY",
    "assert_error_envelope",
    "decisions",
]
