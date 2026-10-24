"""platform 侧共享测试设施（fake clock / 可计数 transport / 捕获日志 / 签名复算）。

为什么要 fake clock：契约 §5.1 要求令牌桶与退避都走注入的 `clock`/`sleep`，
否则重试用例要真睡 0.4+0.8+1.6 秒，一套测试几十秒——时间浪费在等待上而不是验证上。
这里 `FakeClock.sleep` 只推进虚拟时间，测试零等待，且"退避是否真的发生"变成可断言的
`clock.sleeps` 列表。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from app.services.doudian_client import DoudianClient, RateLimitConfig
from app.services.signing import canonical_string

APP_KEY = "7000000000000000001"
APP_SECRET = "test_secret_do_not_use"
BASE_URL = "https://openapi.test.local"
VERSION = 2


class FakeClock:
    """可注入时钟：`sleep` 只推进虚拟时间，并记录每次退避的秒数。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


class RecordingTransport(httpx.AsyncBaseTransport):
    """记录每次请求（URL 查询串 + body + 收到时的虚拟时间）的 transport。"""

    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        clock: FakeClock | None = None,
    ) -> None:
        self._handler = handler
        self._clock = clock
        self.requests: list[dict[str, Any]] = []
        self.received_at: list[float] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(
            {
                "method": params.get("method"),
                "params": params,
                "body": request.content.decode("utf-8") if request.content else "",
                "url": str(request.url.copy_with(query=None)),
            }
        )
        if self._clock is not None:
            self.received_at.append(self._clock.now)
        return self._handler(request)

    @property
    def call_count(self) -> int:
        return len(self.requests)


def json_response(
    status: int = 200,
    *,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    hdrs = dict(headers or {})
    return httpx.Response(status_code=status, headers=hdrs, json=body)


def ok_body(
    data: Any = None,
    *,
    request_id: str = "logid-ok",
    message: str = "success",
) -> dict[str, Any]:
    return {
        "err_no": 0,
        "message": message,
        "log_id": request_id,
        "data": data if data is not None else {"ok": True},
    }


def make_client(
    transport: httpx.AsyncBaseTransport,
    *,
    clock: FakeClock | None = None,
    rate_limit: RateLimitConfig | None = None,
    token_source: Any = None,
    logger: logging.Logger | None = None,
    app_key: str = APP_KEY,
    app_secret: str = APP_SECRET,
) -> tuple[DoudianClient, FakeClock]:
    """建一个注入了 transport / 假时钟的 client（零网络、零真实等待）。"""
    clock = clock or FakeClock()
    client = DoudianClient(
        app_key=app_key,
        app_secret=app_secret,
        base_url=BASE_URL,
        token_source=token_source,
        version=VERSION,
        transport=transport,
        rate_limit=rate_limit
        or RateLimitConfig(
            rate_per_sec=1_000_000.0,
            burst=1_000,
            max_concurrency=8,
            max_attempts=4,
            backoff_base_s=0.4,
            backoff_max_s=8.0,
        ),
        clock=clock,
        sleep=clock.sleep,
        logger=logger,
    )
    return client, clock


class TokenRecorder:
    """记录 `get_access_token` 调用（含 `force_refresh`）的 token 来源。"""

    def __init__(self, token: str = "token-v1") -> None:
        self.token = token
        self.force_refresh_calls = 0
        self.calls = 0

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.calls += 1
        if force_refresh:
            self.force_refresh_calls += 1
            self.token = f"token-v{self.force_refresh_calls + 1}"
        return self.token


class LogCapture:
    """挂一个 collector handler，拿到 `extra=` 里的结构（脱敏断言用）。"""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    def __call__(self, record: logging.LogRecord) -> None:  # pragma: no cover - 直接 addHandler
        self.records.append(record)

    def data(self) -> list[dict[str, Any]]:
        return [
            dict(getattr(record, "data", {}) or {})
            for record in self.records
        ]

    def events(self) -> list[str]:
        return [str(getattr(record, "event", "")) for record in self.records]


class _CollectorHandler(logging.Handler):
    def __init__(self, collector: LogCapture) -> None:
        super().__init__()
        self._collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        self._collector.records.append(record)


def expected_sign(
    request: Mapping[str, Any],
    *,
    app_key: str = APP_KEY,
    app_secret: str = APP_SECRET,
    version: int = VERSION,
) -> str:
    """按契约 §5.2 独立复算"这次请求自己那组参数"的签名。

    用它断言"签名与 payload 用的是同一个 timestamp"：不是比较实现与实现，
    而是拿请求上真实出现的 timestamp/body 重新算一遍。
    """
    params = request["params"]
    canonical = canonical_string(
        app_key,
        str(params["method"]),
        request["body"],
        str(params["timestamp"]),
        version,
    )
    message = app_secret + canonical + app_secret
    return hmac.new(
        app_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def load_signing_vectors() -> dict[str, Any]:
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fixtures" / "doudian" / "signing_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))


def redacted_values(records: Sequence[logging.LogRecord]) -> str:
    """把所有日志记录拼成一段文本（含 data/msg），供"密钥不出现"的断言使用。"""
    chunks: list[str] = []
    for record in records:
        chunks.append(record.getMessage())
        chunks.append(json.dumps(getattr(record, "data", {}) or {}, ensure_ascii=False, default=str))
    return "\n".join(chunks)
