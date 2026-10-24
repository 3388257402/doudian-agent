"""限流（CONTRACT §5.1-3）：令牌桶 + 并发信号量，全部走注入的 `clock`/`sleep`。

为什么这组用例必须存在：限流是"看起来测过了其实没测"的重灾区——
真睡 20 × 1/8 秒既慢又不稳定。这里用假时钟把**虚拟时间**当断言对象：
`(20 - burst) / rate_per_sec` 是契约给的下界，少一秒就是限流漏了。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.doudian_client import RateLimitConfig
from platform_kit import (
    FakeClock,
    RecordingTransport,
    json_response,
    make_client,
    ok_body,
)


def _fast_concurrency_config(**overrides) -> RateLimitConfig:
    values = dict(rate_per_sec=1_000_000.0, burst=1_000, max_concurrency=4, max_attempts=2)
    values.update(overrides)
    return RateLimitConfig(**values)


async def test_twenty_calls_take_at_least_the_theoretical_token_time() -> None:
    """burst=16, rate=8/s → 20 次调用至少需要 (20-16)/8 = 0.5s 虚拟时间。"""
    clock = FakeClock()
    transport = RecordingTransport(lambda request: json_response(body=ok_body()), clock=clock)
    client, _ = make_client(
        transport,
        clock=clock,
        rate_limit=RateLimitConfig(
            rate_per_sec=8.0, burst=16, max_concurrency=4, max_attempts=1
        ),
    )

    for _ in range(20):
        await client.call("shop.getShopInfo")

    assert transport.call_count == 20
    assert clock.total_slept >= (20 - 16) / 8.0 - 1e-9


async def test_burst_is_free_and_never_exceeds_capacity() -> None:
    clock = FakeClock()
    transport = RecordingTransport(lambda request: json_response(body=ok_body()), clock=clock)
    client, _ = make_client(
        transport,
        clock=clock,
        rate_limit=RateLimitConfig(rate_per_sec=2.0, burst=4, max_concurrency=4, max_attempts=1),
    )

    for _ in range(4):
        await client.call("shop.getShopInfo")
    assert clock.total_slept == 0.0, "burst 以内的调用不该等待"

    # 空闲很久后桶不会溢出（上限 = burst，不攒无限额度）
    await client.call("shop.getShopInfo")  # 第 5 次：等 (1/2)s
    assert clock.total_slept == pytest.approx(0.5, abs=1e-9)
    clock.now += 100.0
    for _ in range(4):
        await client.call("shop.getShopInfo")
    assert clock.total_slept == pytest.approx(0.5, abs=1e-9), "补满到 burst 就停"


async def test_concurrency_never_exceeds_max_concurrency() -> None:
    """信号量必须在**真实**并发下生效：这里用真实 asyncio.sleep，观察在飞数量。"""
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            return httpx.Response(status_code=200, json=ok_body(), request=request)

    client, _ = make_client(
        SlowTransport(),
        rate_limit=_fast_concurrency_config(max_concurrency=2),
    )
    await asyncio.gather(*(client.call("shop.getShopInfo") for _ in range(6)))
    assert peak == 2, f"max_concurrency=2 时峰值并发必须正好 2，实测 {peak}"


def test_rate_limit_config_rejects_impossible_values() -> None:
    with pytest.raises(ValueError):
        RateLimitConfig(rate_per_sec=0.0)
    with pytest.raises(ValueError):
        RateLimitConfig(rate_per_sec=-1.0)
    with pytest.raises(ValueError):
        RateLimitConfig(burst=0)
    with pytest.raises(ValueError):
        RateLimitConfig(max_concurrency=0)
    with pytest.raises(ValueError):
        RateLimitConfig(max_attempts=0)
    assert RateLimitConfig().max_attempts == 4, "契约默认含首次 4 次"


async def test_retry_sleeps_are_visible_to_the_injected_clock() -> None:
    """退避走注入的 sleep：测试零等待，且退避序列可断言。"""
    transport = RecordingTransport(
        lambda request: json_response(502, body={"err_no": -1, "message": "bad gateway"})
    )
    client, clock = make_client(transport, rate_limit=_fast_concurrency_config(max_attempts=4))
    with pytest.raises(Exception):  # noqa: B017 - 只看退避行为，异常类型另有用例
        await client.call("shop.getShopInfo")

    assert len(clock.sleeps) == 3
    assert clock.total_slept < 3.5, "3 次退避总和被 backoff_max_s 约束在个位数秒"
