"""DoudianClient（CONTRACT §5.1 / §9 / §10）：错误分类、重试预算、脱敏、可注入 transport。

这组用例的判据不是"跑通"，而是**每一条失败路径都走到边界上**：
哪一类不重试、哪一类重试到第几次放弃、可变操作为什么必须转 ambiguous、
签名是不是这次请求自己那个 timestamp 算出来的。全部走注入的
`MockTransport` + 假时钟：零网络、零真实等待（§10.1 主路径）。
"""

from __future__ import annotations

import asyncio
import json as _json
import re

import httpx
import pytest

from app.enums import ErrorCategory
from app.services.doudian_client import (
    DoudianClient,
    DoudianResponse,
    RateLimitConfig,
    create_doudian_client,
)
from app.services.errors import (
    DoudianAmbiguousOutcomeError,
    DoudianAuthError,
    DoudianBusinessError,
    DoudianClientError,
    DoudianRateLimitedError,
    DoudianSignatureError,
    DoudianTransportError,
    DoudianUpstreamError,
)
from app.config import build_settings
from platform_kit import (
    APP_KEY,
    APP_SECRET,
    TokenRecorder,
    RecordingTransport,
    expected_sign,
    json_response,
    make_client,
    ok_body,
    redacted_values,
)


# --------------------------------------------------------------------------------------
# 正常路径与响应解析
# --------------------------------------------------------------------------------------
async def test_success_returns_response_and_uses_single_timestamp_for_sign_and_query() -> None:
    transport = RecordingTransport(lambda request: json_response(body=ok_body({"shop_id": "1"})))
    client, _ = make_client(transport)

    response = await client.call("shop.getShopInfo", {})

    assert isinstance(response, DoudianResponse)
    assert response.ok is True and response.err_no == 0
    assert response.data == {"shop_id": "1"}
    assert response.attempts == 1
    assert response.request_id == "logid-ok"  # body.log_id 兜底
    assert transport.call_count == 1

    request = transport.requests[0]
    # 签名用的是**这个请求自己**的 timestamp（不是另生成一个）：用请求上真实出现的
    # timestamp + body 独立复算一遍，而不是比对实现自己的输出
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", request["params"]["timestamp"])
    assert request["params"]["sign"] == expected_sign(request)
    # 请求形态：POST {base_url}/{method 转路径}?全部参数在 query，body=param_json
    assert request["url"] == "https://openapi.test.local/shop/getShopInfo"
    assert set(request["params"]) == {
        "app_key", "method", "access_token", "timestamp", "v", "sign_method", "sign",
    }
    assert request["params"]["app_key"] == APP_KEY
    assert request["params"]["v"] == "2"
    assert request["params"]["sign_method"] == "hmac-sha256"
    assert len(request["params"]["sign"]) == 64


async def test_request_id_falls_back_to_header_then_none() -> None:
    transport = RecordingTransport(
        lambda request: json_response(
            body={"err_no": 0, "message": "ok", "data": {}},
            headers={"x-tt-logid": "from-header"},
        )
    )
    client, _ = make_client(transport)
    assert (await client.call("shop.getShopInfo")).request_id == "from-header"

    transport2 = RecordingTransport(
        lambda request: json_response(body={"err_no": 0, "message": "ok", "data": {}})
    )
    client2, _ = make_client(transport2)
    # 三个来源都没有 → None，不编造
    assert (await client2.call("shop.getShopInfo")).request_id is None


async def test_missing_err_no_is_a_protocol_error_not_a_success() -> None:
    """200 但没有 err_no：不能当成功（否则网关错误页会被读成"数据为空"）。"""
    transport = RecordingTransport(lambda request: json_response(body={"data": {"x": 1}}))
    client, _ = make_client(transport)
    with pytest.raises(DoudianUpstreamError) as excinfo:
        await client.call("shop.getShopInfo")
    assert "err_no" in str(excinfo.value)
    assert excinfo.value.http_status == 200


async def test_non_json_2xx_response_is_retried_as_upstream_error() -> None:
    transport = RecordingTransport(
        lambda request: httpx.Response(status_code=200, content=b"<html>gateway</html>")
    )
    client, clock = make_client(transport)
    with pytest.raises(DoudianUpstreamError):
        await client.call("shop.getShopInfo")
    assert transport.call_count == 4  # 4 次尝试后放弃
    assert len(clock.sleeps) == 3


# --------------------------------------------------------------------------------------
# 4xx：不重试，直接暴露
# --------------------------------------------------------------------------------------
async def test_client_4xx_is_not_retried_and_exposes_message() -> None:
    transport = RecordingTransport(
        lambda request: json_response(404, body={"err_no": 0, "message": "shop not found"})
    )
    client, clock = make_client(transport)

    with pytest.raises(DoudianClientError) as excinfo:
        await client.call("shop.getShopInfo")

    error = excinfo.value
    assert transport.call_count == 1, "4xx 不许重试"
    assert clock.sleeps == []
    assert error.category is ErrorCategory.CLIENT_4XX
    assert error.retryable is False
    assert error.http_status == 404
    assert "shop not found" in str(error)


async def test_business_err_no_under_2xx_is_raised_not_returned() -> None:
    transport = RecordingTransport(
        lambda request: json_response(body={"err_no": 40001, "message": "库存不足"})
    )
    client, clock = make_client(transport)

    with pytest.raises(DoudianBusinessError) as excinfo:
        await client.call("product.editStock", {"stock": 0}, mutating=True)

    assert transport.call_count == 1, "业务错误不重试"
    assert clock.sleeps == []
    assert excinfo.value.err_no == 40001
    assert excinfo.value.category is ErrorCategory.BUSINESS
    assert "库存不足" in str(excinfo.value)


async def test_signature_err_no_is_signature_category_and_not_retried() -> None:
    transport = RecordingTransport(
        lambda request: json_response(body={"err_no": 10000, "message": "sign error"})
    )
    client, _ = make_client(transport)
    with pytest.raises(DoudianSignatureError) as excinfo:
        await client.call("shop.getShopInfo")
    assert excinfo.value.category is ErrorCategory.SIGNATURE
    assert excinfo.value.retryable is False
    assert transport.call_count == 1


async def test_http_400_with_unknown_err_no_is_business_error() -> None:
    transport = RecordingTransport(
        lambda request: json_response(400, body={"err_no": 49999, "message": "bad"})
    )
    client, _ = make_client(transport)
    with pytest.raises(DoudianBusinessError):
        await client.call("order.searchList", {"page": 0})


# --------------------------------------------------------------------------------------
# 5xx / 超时：非可变操作重试到上限
# --------------------------------------------------------------------------------------
async def test_5xx_retries_until_max_attempts_then_raises() -> None:
    transport = RecordingTransport(
        lambda request: json_response(503, body={"err_no": -1, "message": "busy"})
    )
    client, clock = make_client(transport)

    with pytest.raises(DoudianUpstreamError) as excinfo:
        await client.call("shop.getShopInfo")

    assert transport.call_count == 4, "max_attempts=4（含首次）"
    assert len(clock.sleeps) == 3
    # 指数退避：0.4 / 0.8 / 1.6（各带 <=10% 抖动）
    for index, delay in enumerate(clock.sleeps):
        base = 0.4 * (2**index)
        assert base <= delay <= base * 1.1 + 1e-9
    assert excinfo.value.category is ErrorCategory.SERVER_5XX
    assert excinfo.value.retryable is True


async def test_5xx_then_success_reports_attempts_count() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] < 3:
            return json_response(500, body={"err_no": -1})
        return json_response(body=ok_body())

    transport = RecordingTransport(handler)
    client, clock = make_client(transport)
    response = await client.call("shop.getShopInfo")
    assert response.attempts == 3
    assert len(clock.sleeps) == 2
    # 每次尝试重新签名：三个请求的 timestamp/sign 组合都必须自洽
    for request in transport.requests:
        assert request["params"]["sign"] == expected_sign(request)


async def test_timeout_on_read_is_retried() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ReadTimeout("slow upstream", request=request)
        return json_response(body=ok_body())

    transport = RecordingTransport(handler)
    client, clock = make_client(transport)
    response = await client.call("order.searchList", {"page": 0})
    assert response.attempts == 2
    assert len(clock.sleeps) == 1


async def test_connection_error_is_transport_category_and_retries() -> None:
    transport = RecordingTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request))
    )
    client, _ = make_client(transport)
    with pytest.raises(DoudianTransportError) as excinfo:
        await client.call("shop.getShopInfo")
    assert excinfo.value.category is ErrorCategory.CONNECTION
    assert transport.call_count == 4


# --------------------------------------------------------------------------------------
# 429：按 Retry-After 等待
# --------------------------------------------------------------------------------------
async def test_429_honours_retry_after_header() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return json_response(429, body={"err_no": -1}, headers={"retry-after": "1.5"})
        return json_response(body=ok_body())

    transport = RecordingTransport(handler)
    client, clock = make_client(transport)
    response = await client.call("shop.getShopInfo")
    assert response.attempts == 2
    assert clock.sleeps == [1.5], "Retry-After 优先于指数退避"


async def test_429_retry_after_is_capped_by_backoff_max() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return json_response(429, body={"err_no": -1}, headers={"retry-after": "600"})
        return json_response(body=ok_body())

    transport = RecordingTransport(handler)
    client, clock = make_client(transport)
    await client.call("shop.getShopInfo")
    assert clock.sleeps == [8.0], "Retry-After 上限 = backoff_max_s"


async def test_429_without_retry_after_falls_back_to_exponential_backoff() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= 2:
            return json_response(429, body={"err_no": -1})
        return json_response(body=ok_body())

    transport = RecordingTransport(handler)
    client, clock = make_client(transport)
    await client.call("shop.getShopInfo")
    assert len(clock.sleeps) == 2
    assert 0.4 <= clock.sleeps[0] <= 0.44
    assert 0.8 <= clock.sleeps[1] <= 0.88


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_forces_one_token_refresh_then_retries(status: int) -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return json_response(status, body={"err_no": 0, "message": "token expired"})
        return json_response(body=ok_body())

    tokens = TokenRecorder()
    transport = RecordingTransport(handler)
    client, _ = make_client(transport, token_source=tokens)
    response = await client.call("shop.getShopInfo")

    assert response.attempts == 2
    assert tokens.force_refresh_calls == 1
    # 第二次请求带的是刷新后的 token
    assert transport.requests[1]["params"]["access_token"] == "token-v2"


async def test_auth_failure_twice_stops_after_single_refresh() -> None:
    transport = RecordingTransport(
        lambda request: json_response(401, body={"err_no": 0, "message": "no permission"})
    )
    tokens = TokenRecorder()
    client, _ = make_client(transport, token_source=tokens)
    with pytest.raises(DoudianAuthError):
        await client.call("shop.getShopInfo")
    assert transport.call_count == 2, "只允许强制刷 token 一次"
    assert tokens.force_refresh_calls == 1


# --------------------------------------------------------------------------------------
# mutating：只对限流重试，其余转 ambiguous（§9.1 / §9.3 / ADR-011）
# --------------------------------------------------------------------------------------
async def test_mutating_timeout_becomes_ambiguous_without_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("write timeout", request=request)

    transport = RecordingTransport(handler)
    client, clock = make_client(transport)

    with pytest.raises(DoudianAmbiguousOutcomeError) as excinfo:
        await client.call("product.editPrice", {"price": 9900}, mutating=True)

    assert transport.call_count == 1, "可变操作遇到超时不许重试"
    assert clock.sleeps == []
    assert excinfo.value.category is ErrorCategory.AMBIGUOUS
    assert excinfo.value.retryable is False


async def test_mutating_5xx_becomes_ambiguous_without_retry() -> None:
    transport = RecordingTransport(
        lambda request: json_response(503, body={"err_no": -1, "message": "busy"})
    )
    client, _ = make_client(transport)
    with pytest.raises(DoudianAmbiguousOutcomeError):
        await client.call("product.editStock", {"stock": 5}, mutating=True)
    assert transport.call_count == 1


async def test_mutating_429_is_retried() -> None:
    """限流是"请求没被受理"，重试安全 → 可变操作也重试（§9.1 限流行）。"""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return json_response(429, body={"err_no": -1}, headers={"retry-after": "0.25"})
        return json_response(body=ok_body())

    transport = RecordingTransport(handler)
    client, clock = make_client(transport)
    response = await client.call("product.editPrice", {"price": 9900}, mutating=True)
    assert response.attempts == 2
    assert clock.sleeps == [0.25]


async def test_read_5xx_is_not_converted_to_ambiguous() -> None:
    transport = RecordingTransport(lambda request: json_response(500, body={"err_no": -1}))
    client, _ = make_client(transport)
    with pytest.raises(DoudianUpstreamError):
        await client.call("product.listV2", {"page": 0})  # mutating=False
    assert transport.call_count == 4


# --------------------------------------------------------------------------------------
# 业务封装
# --------------------------------------------------------------------------------------
async def test_business_wrappers_call_the_frozen_method_names() -> None:
    transport = RecordingTransport(lambda request: json_response(body=ok_body()))
    client, _ = make_client(transport)

    await client.get_shop_info()
    await client.search_orders(start_time="2026-10-23 00:00:00", end_time="2026-10-23 23:59:59")
    await client.list_products(page=1, size=20)
    await client.edit_price(product_id="p1", sku_id="s1", price_cents=9900)
    await client.update_stock(product_id="p1", sku_id="s1", stock=7)

    methods = [request["params"]["method"] for request in transport.requests]
    assert methods == [
        "shop.getShopInfo",
        "order.searchList",
        "product.listV2",
        "product.editPrice",
        "product.editStock",
    ]
    edit_price_body = _json.loads(transport.requests[3]["body"])
    edit_stock_body = _json.loads(transport.requests[4]["body"])
    assert edit_price_body == {"product_id": "p1", "sku_id": "s1", "price": 9900}
    assert edit_stock_body == {"product_id": "p1", "sku_id": "s1", "stock": 7}
    assert _json.loads(transport.requests[1]["body"])["size"] == 50
    assert _json.loads(transport.requests[2]["body"]) == {"page": 1, "size": 20}


async def test_invalid_method_shape_is_rejected_locally() -> None:
    transport = RecordingTransport(lambda request: json_response(body=ok_body()))
    client, _ = make_client(transport)
    with pytest.raises(DoudianClientError) as excinfo:
        await client.call("shop.getShopInfo.extra")
    assert transport.call_count == 0
    assert excinfo.value.retryable is False


# --------------------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------------------
async def test_logs_never_contain_secret_token_or_signature(capture_logger) -> None:
    logger, collector = capture_logger
    transport = RecordingTransport(
        lambda request: json_response(503, body={"err_no": -1, "message": "busy"})
    )
    client, _ = make_client(transport, token_source=TokenRecorder("super-secret-token"), logger=logger)

    with pytest.raises(DoudianUpstreamError):
        await client.call("shop.getShopInfo")

    text = redacted_values(collector.records)
    assert collector.records, "必须留下结构化日志"
    assert "super-secret-token" not in text
    assert APP_SECRET not in text
    # 但 sign 这个键仍然在（值被打码）：能看出"这里签过名"
    assert "***" in text
    events = collector.events()
    assert "doudian.retry" in events


async def test_every_successful_call_writes_one_structured_event(capture_logger) -> None:
    logger, collector = capture_logger
    transport = RecordingTransport(lambda request: json_response(body=ok_body()))
    client, _ = make_client(transport, logger=logger)
    await client.call("shop.getShopInfo")

    calls = [data for event, data in zip(collector.events(), collector.data()) if event == "doudian.call"]
    assert len(calls) == 1
    payload = calls[0]
    assert payload["method"] == "shop.getShopInfo"
    assert payload["http_status"] == 200
    assert payload["err_no"] == 0
    assert payload["attempts"] == 1
    assert payload["request_id"] == "logid-ok"
    assert isinstance(payload["duration_ms"], (int, float))


async def test_failed_call_also_writes_a_structured_event(capture_logger) -> None:
    """§5.1-7：每次 call 结束写一条 `doudian.call`。失败也必须留一条，否则排障只剩异常栈。"""
    logger, collector = capture_logger
    transport = RecordingTransport(
        lambda request: json_response(403, body={"err_no": 0, "message": "forbidden"})
    )
    client, _ = make_client(transport, logger=logger)
    with pytest.raises(DoudianAuthError):
        await client.call("shop.getShopInfo")

    events = collector.events()
    assert "doudian.call" in events
    payload = [
        data for event, data in zip(collector.events(), collector.data()) if event == "doudian.call"
    ][0]
    assert payload["http_status"] == 403
    assert payload["category"] == "auth"
    assert payload["attempts"] >= 1


# --------------------------------------------------------------------------------------
# 生命周期与工厂
# --------------------------------------------------------------------------------------
async def test_client_is_async_context_manager_and_closes_transport() -> None:
    transport = RecordingTransport(lambda request: json_response(body=ok_body()))
    client, _ = make_client(transport)
    async with client as entered:
        assert entered is client
        await client.call("shop.getShopInfo")
    assert client._http.is_closed  # 唯一 AsyncClient 被关掉，不留悬空连接池


async def test_constructor_rejects_bad_configuration() -> None:
    with pytest.raises(DoudianSignatureError):
        DoudianClient(app_key="", app_secret="s")
    with pytest.raises(DoudianSignatureError):
        DoudianClient(app_key="k", app_secret="s", sign_method="md5")
    with pytest.raises(ValueError):
        DoudianClient(app_key="k", app_secret="s", base_url="openapi.jinritemai.com")
    with pytest.raises(ValueError):
        RateLimitConfig(rate_per_sec=0)
    with pytest.raises(ValueError):
        RateLimitConfig(burst=0)
    with pytest.raises(ValueError):
        RateLimitConfig(max_attempts=0)
    with pytest.raises(ValueError):
        RateLimitConfig(max_concurrency=0)


async def test_dry_run_factory_uses_cassettes_and_needs_no_credentials(monkeypatch) -> None:
    """§8.2：dry_run=true 不读凭据，走 fixtures transport —— 零凭据可跑。"""
    monkeypatch.delenv("DOUDIAN_APP_KEY", raising=False)
    monkeypatch.delenv("DOUDIAN_APP_SECRET", raising=False)
    monkeypatch.delenv("DOUDIAN_ACCESS_TOKEN", raising=False)
    settings = build_settings({"doudian": {"dry_run": True}})

    client = create_doudian_client(settings)
    try:
        shop = await client.get_shop_info()
        products = await client.list_products()
        orders = await client.search_orders(start_time="a", end_time="b")
        price = await client.edit_price(product_id="p", sku_id="s", price_cents=9900)
        stock = await client.update_stock(product_id="p", sku_id="s", stock=1)
    finally:
        await client.aclose()

    assert shop.data["shop_name"] == "示例旗舰店"
    assert products.data["total"] == 3
    assert orders.data["total"] == 3
    assert price.ok and stock.ok


async def test_dry_run_missing_cassette_fails_loudly() -> None:
    """没录到的 method 必须显式失败（599 + err_no=-1），不许静默 200。

    用假时钟直接驱动 CassetteTransport，避免为一条断言真睡 2.8s 的退避。
    """
    from platform_kit import BASE_URL, FakeClock

    from app.services.doudian_client import CassetteTransport, load_cassettes

    clock = FakeClock()
    client = DoudianClient(
        app_key="k",
        app_secret="s",
        base_url=BASE_URL,
        transport=CassetteTransport(load_cassettes()),
        clock=clock,
        sleep=clock.sleep,
        rate_limit=RateLimitConfig(
            rate_per_sec=1_000_000.0, burst=100, max_concurrency=2, max_attempts=2
        ),
    )
    try:
        with pytest.raises(DoudianUpstreamError) as excinfo:
            await client.call("coupon.createActivity", {})
    finally:
        await client.aclose()
    assert excinfo.value.http_status == 599
    assert "no cassette matched" in str(excinfo.value)


async def test_dry_run_factory_injects_cassette_transport(monkeypatch) -> None:
    from app.services.doudian_client import CassetteTransport

    monkeypatch.delenv("DOUDIAN_APP_KEY", raising=False)
    client = create_doudian_client(build_settings({"doudian": {"dry_run": True}}))
    try:
        assert isinstance(client._http._transport, CassetteTransport)
    finally:
        await client.aclose()


async def test_real_mode_requires_credentials_from_env(monkeypatch) -> None:
    from app.errors import ConfigError

    for name in ("DOUDIAN_APP_KEY", "DOUDIAN_APP_SECRET", "DOUDIAN_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    settings = build_settings({"doudian": {"dry_run": False}})
    with pytest.raises(ConfigError):
        create_doudian_client(settings)

    monkeypatch.setenv("DOUDIAN_APP_KEY", "k")
    monkeypatch.setenv("DOUDIAN_APP_SECRET", "s")
    with pytest.raises(ConfigError):
        create_doudian_client(settings)

    monkeypatch.setenv("DOUDIAN_ACCESS_TOKEN", "t")
    client = create_doudian_client(settings)
    await client.aclose()


async def test_concurrent_calls_share_one_http_client() -> None:
    """ADR-004：唯一 AsyncClient。并发调用不允许各自建连接池。"""
    transport = RecordingTransport(lambda request: json_response(body=ok_body()))
    client, _ = make_client(transport)
    responses = await asyncio.gather(
        *(client.call("shop.getShopInfo") for _ in range(5))
    )
    assert len(responses) == 5
    assert transport.call_count == 5
    await client.aclose()
