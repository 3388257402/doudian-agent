"""抖店开放平台客户端（CONTRACT §5.1 / §9 / §10）。

一个进程一个 `DoudianClient`：**唯一**的 `httpx.AsyncClient`（连接池）+ 挂在同一实例上的
令牌桶与并发信号量（ADR-004）。为什么必须同实例：连接池与限流是同一个"容量"概念，
分成两处就会出现"桶放行 8 QPS、连接池只给 20 条各排队 2s"的互相踩；而且单例是
注入 `transport` 的前提，直接决定"零凭据可测"（ADR-013）能否成立。

错误处理按两级分类（§9.1）：HTTP 层（timeout/connection/5xx/429/4xx）+ 业务层（`err_no`）。
抖店的失败大多是 **HTTP 200 + body err_no != 0**，只看状态码必然漏判。

`mutating=True` 是调用方在写代码那一刻就必须回答的问题："我这个操作重复执行安全吗？"
可变操作只对 `rate_limited` 重试；`timeout`/`connection`/`5xx` 一律转
`DoudianAmbiguousOutcomeError`（结果不确定 → 落 `unknown` → 交对账，ADR-011）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from app.enums import ErrorCategory
from app.observability.logging import DEFAULT_REDACT_KEYS, redact
from app.services.errors import (
    RETRYABLE_BY_CATEGORY,
    DoudianAmbiguousOutcomeError,
    DoudianAuthError,
    DoudianClientError,
    DoudianError,
    DoudianRateLimitedError,
    DoudianSignatureError,
    DoudianTransportError,
    DoudianUpstreamError,
    error_for_category,
)
from app.services.signing import (
    SIGN_METHOD_HMAC_SHA256,
    format_timestamp,
    normalize_param_json,
    sign,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.config import Settings

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CASSETTE_DIR",
    "DEFAULT_LIMITS",
    "DEFAULT_TIMEOUT",
    "SIGNATURE_ERR_NOS",
    "AUTH_ERR_NOS",
    "RATE_LIMIT_ERR_NOS",
    "TokenSource",
    "StaticTokenSource",
    "RateLimitConfig",
    "DoudianResponse",
    "DoudianClient",
    "Cassette",
    "CassetteTransport",
    "load_cassettes",
    "create_doudian_client",
    "redact_query",
]

DEFAULT_BASE_URL = "https://openapi-fxg.jinritemai.com"
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)
DEFAULT_LIMITS = httpx.Limits(
    max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0
)

#: 抖店协议时间戳用北京时间（官方 SDK 用"本机本地时间"）。我们固定时区而不是跟随
#: 机器 TZ：容器默认 UTC 时，跟着机器走会让签名时间戳差 8 小时被服务端判过期。
DOUDIAN_TZ = ZoneInfo("Asia/Shanghai")

#: 签名类业务码（契约 §9.1 点名 10000/10008 类）。
SIGNATURE_ERR_NOS: frozenset[int] = frozenset({10000, 10008})

#: 鉴权类业务码：**故意为空**。官方文档未核实 body 级 token 失效码（DECISIONS 附 B），
#: 猜一个码的代价是"把业务错误当鉴权错误去刷 token"或反之，两者都比"保守落 business"
#: 更糟。鉴权错误目前只按 HTTP 401/403 判定；核实后往这两个集合里加码即可，
#: 分类逻辑不需要改（`classify_err_no` 是纯查表）。
AUTH_ERR_NOS: frozenset[int] = frozenset()

#: 限流类业务码：同上（HTTP 429 已覆盖主路径）。
RATE_LIMIT_ERR_NOS: frozenset[int] = frozenset()

#: 这些类别在 `mutating=True` 时必须转成"结果不确定"，绝不重试（§9.1 / ADR-011）。
_AMBIGUOUS_ON_MUTATING = frozenset(
    {ErrorCategory.TIMEOUT, ErrorCategory.CONNECTION, ErrorCategory.SERVER_5XX}
)

#: 夹具目录（`backend/tests/fixtures/doudian/cassettes`）。
DEFAULT_CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "doudian" / "cassettes"

#: 方法名形状：`namespace.action`，两段都必须是标识符（§5.1-2 点号形式）。
#: 客户端本地校验的意义：拼错 method 不该消耗一次网络往返、更不该被算进限流额度。
_METHOD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")


class TokenSource(Protocol):
    """access_token 来源。`force_refresh=True` 表示"上次调用被判鉴权失败，强制换新"。"""

    async def get_access_token(self, *, force_refresh: bool = False) -> str: ...


class StaticTokenSource:
    """固定 token（dry-run / 单测）。`force_refresh` 时重新读一次环境变量。"""

    def __init__(self, token: str, *, env_name: str | None = None) -> None:
        self._token = token
        self._env_name = env_name

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh and self._env_name:
            self._token = os.environ.get(self._env_name, self._token)
        return self._token


@dataclass(frozen=True)
class RateLimitConfig:
    """限流 + 重试参数（默认值 = 契约 §5.1）。"""

    rate_per_sec: float = 8.0
    burst: int = 16
    max_concurrency: int = 4
    max_attempts: int = 4
    backoff_base_s: float = 0.4
    backoff_max_s: float = 8.0

    def __post_init__(self) -> None:
        if self.rate_per_sec <= 0:
            raise ValueError("rate_per_sec 必须 > 0")
        if self.burst < 1:
            raise ValueError("burst 必须 >= 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")


@dataclass(frozen=True)
class DoudianResponse:
    ok: bool
    err_no: int
    message: str
    data: Any
    http_status: int
    request_id: str | None
    raw: dict[str, Any]
    attempts: int


def classify_err_no(err_no: int) -> ErrorCategory:
    """业务码 → 类别（纯查表，§9.1）。未收录的码一律 `business`（保守：不重试）。"""
    if err_no in SIGNATURE_ERR_NOS:
        return ErrorCategory.SIGNATURE
    if err_no in AUTH_ERR_NOS:
        return ErrorCategory.AUTH
    if err_no in RATE_LIMIT_ERR_NOS:
        return ErrorCategory.RATE_LIMITED
    return ErrorCategory.BUSINESS


def redact_query(
    query: Mapping[str, Any],
    redact_keys: Sequence[str] = DEFAULT_REDACT_KEYS,
) -> dict[str, Any]:
    """日志用：把 query 里的 `sign`/`access_token` 等替换成 `***`（脱敏在落盘前）。"""
    return redact(dict(query), redact_keys)  # type: ignore[return-value]


class _TokenBucket:
    """进程内令牌桶（`clock`/`sleep` 可注入 → 限流测试零等待）。"""

    def __init__(
        self,
        *,
        rate_per_sec: float,
        burst: int,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._rate = rate_per_sec
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    @property
    def tokens(self) -> float:
        return self._tokens

    async def acquire(self, tokens: float = 1.0) -> float:
        """取 token；不够就按 `deficit / rate` 睡眠。返回累计等待秒数。"""
        waited = 0.0
        while True:
            now = self._clock()
            elapsed = now - self._last
            if elapsed > 0:
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return waited
            deficit = tokens - self._tokens
            delay = deficit / self._rate
            await self._sleep(delay)
            waited += delay


# --------------------------------------------------------------------------------------
# 离线夹具 transport（§10.2）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Cassette:
    """一个录制好的上游响应。"""

    name: str
    method: str
    body_contains: dict[str, Any]
    status: int
    headers: dict[str, str]
    json_body: Any
    delay_ms: int
    source: Path


def load_cassettes(directory: str | Path = DEFAULT_CASSETTE_DIR) -> list[Cassette]:
    """读目录下所有 `*.json` cassette，按**文件名字典序**返回（匹配顺序确定，§10.2）。"""
    import json

    base = Path(directory)
    if not base.exists():
        raise FileNotFoundError(f"cassette 目录不存在: {base}")
    cassettes: list[Cassette] = []
    for path in sorted(base.glob("*.json"), key=lambda item: item.name):
        payload = json.loads(path.read_text(encoding="utf-8"))
        match = payload.get("match") or {}
        response = payload.get("response") or {}
        cassettes.append(
            Cassette(
                name=str(payload.get("name") or path.stem),
                method=str(match.get("method") or ""),
                body_contains=dict(match.get("body_contains") or {}),
                status=int(response.get("status", 200)),
                headers=dict(response.get("headers") or {}),
                json_body=response.get("json"),
                delay_ms=int(payload.get("delay_ms") or 0),
                source=path,
            )
        )
    return cassettes


def _is_subset(expected: Any, actual: Any) -> bool:
    """递归子集匹配：`null` 表示"键必须存在且为 null"（§10.2）。"""
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        for key, want in expected.items():
            if key not in actual:
                return False
            if not _is_subset(want, actual[key]):
                return False
        return True
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(_is_subset(want, got) for want, got in zip(expected, actual, strict=True))
    return expected == actual


class CassetteTransport(httpx.AsyncBaseTransport):
    """按 cassette 回放的 `httpx.AsyncBaseTransport`。

    无命中返回 **599** + `err_no=-1`（让"夹具没录"显式失败，不静默 200）。
    handler **忽略但记录** `sign`（测试可断言它是 64 位小写 hex）。
    """

    def __init__(self, cassettes: Sequence[Cassette]) -> None:
        self._cassettes = list(cassettes)
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import json

        params = dict(request.url.params)
        method = params.get("method", "")
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        self.requests.append(
            {
                "method": method,
                "params": params,
                "body": body,
                "sign": params.get("sign"),
                "url": str(request.url.copy_with(query=None)),
            }
        )
        for cassette in self._cassettes:
            if cassette.method != method:
                continue
            if not _is_subset(cassette.body_contains, body):
                continue
            if cassette.delay_ms:
                await asyncio.sleep(cassette.delay_ms / 1000.0)
            return httpx.Response(
                status_code=cassette.status,
                headers=cassette.headers,
                json=cassette.json_body,
                request=request,
            )
        return httpx.Response(
            status_code=599,
            json={"err_no": -1, "message": f"no cassette matched: {method}"},
            request=request,
        )


# --------------------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------------------
class DoudianClient:
    """抖店开放平台客户端。线程/协程安全：状态都在实例内且只在事件循环里变更。"""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        token_source: TokenSource | None = None,
        version: int = 2,
        sign_method: str = SIGN_METHOD_HMAC_SHA256,
        timeout: httpx.Timeout | None = None,
        limits: httpx.Limits | None = None,
        rate_limit: RateLimitConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
        redact_keys: Sequence[str] = DEFAULT_REDACT_KEYS,
    ) -> None:
        if not app_key:
            raise DoudianSignatureError("app_key 缺失，无法签名（检查 DOUDIAN_APP_KEY）")
        if sign_method != SIGN_METHOD_HMAC_SHA256:
            raise DoudianSignatureError(
                f"unsupported sign_method: {sign_method!r}（只实现 {SIGN_METHOD_HMAC_SHA256}）"
            )
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url 必须是 http(s) URL，收到 {base_url!r}")
        self._app_key = app_key
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._token_source = token_source
        self._version = version
        self._sign_method = sign_method
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._limits = limits or DEFAULT_LIMITS
        self._rate_limit = rate_limit or RateLimitConfig()
        self._logger = logger or logging.getLogger("app.services.doudian_client")
        self._redact_keys = tuple(redact_keys)
        self._sleep = sleep
        self._clock = clock
        # 唯一实例：连接池（ADR-004）。禁止每次请求 new 一个。
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=self._timeout,
            limits=self._limits,
        )
        self._bucket = _TokenBucket(
            rate_per_sec=self._rate_limit.rate_per_sec,
            burst=self._rate_limit.burst,
            clock=clock,
            sleep=sleep,
        )
        self._semaphore = asyncio.Semaphore(self._rate_limit.max_concurrency)

    # --- 生命周期 ---
    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> DoudianClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- 公共调用 ---
    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        mutating: bool = False,
        timeout: httpx.Timeout | None = None,
    ) -> DoudianResponse:
        """调用一个抖店方法（点号形式，如 `product.editPrice`）。

        成功返回 `DoudianResponse`；失败**一律抛** `DoudianError` 子类（不返回 ok=False
        让调用方自己判断——那必然有人忘判）。
        """
        if not method or not _METHOD_PATTERN.match(method):
            raise DoudianClientError(
                f"method 必须是点号两段形式（如 product.editPrice），收到 {method!r}"
            )

        attempts = 0
        auth_retried = False
        while True:
            attempts += 1
            try:
                return await self._send(
                    method, params, mutating=mutating, timeout=timeout, attempts=attempts
                )
            except DoudianAuthError as exc:
                # 鉴权失败：强制刷 token **一次**（§9.1 处理动作列）。再失败就抛。
                if auth_retried or self._token_source is None:
                    raise
                auth_retried = True
                self._log(
                    "doudian.auth_refresh",
                    level="warn",
                    message=f"鉴权失败，强制刷新 token 后重试: {method}",
                    data={"method": method, "attempt": attempts, "err_no": exc.err_no},
                )
                await self._token_source.get_access_token(force_refresh=True)
            except DoudianError as exc:
                if mutating and exc.category in _AMBIGUOUS_ON_MUTATING:
                    # 可变操作 + 结果不确定：不重试，交对账（§9.3）。
                    self._log(
                        "doudian.ambiguous",
                        level="warn",
                        message=f"可变操作结果不确定，转 unknown 对账: {method}",
                        data={
                            "method": method,
                            "category": str(exc.category),
                            "attempt": attempts,
                        },
                    )
                    raise DoudianAmbiguousOutcomeError(
                        f"{method} 结果不确定（{exc.category}）：不重试，交对账",
                        err_no=exc.err_no,
                        http_status=exc.http_status,
                        request_id=exc.request_id,
                        raw=exc.raw,
                    ) from exc
                if not exc.retryable or attempts >= self._rate_limit.max_attempts:
                    raise
                delay = self._retry_delay(exc, attempts)
                self._log(
                    "doudian.retry",
                    level="warn",
                    message=f"可重试错误，{delay:.3f}s 后重试: {method}",
                    data={
                        "method": method,
                        "category": str(exc.category),
                        "attempt": attempts,
                        "delay_s": round(delay, 6),
                        "err_no": exc.err_no,
                    },
                )
                await self._sleep(delay)

    # --- 业务封装 ---
    async def get_shop_info(self) -> DoudianResponse:
        return await self.call("shop.getShopInfo", {})

    async def search_orders(
        self,
        *,
        start_time: str,
        end_time: str,
        page: int = 0,
        size: int = 50,
    ) -> DoudianResponse:
        return await self.call(
            "order.searchList",
            {"start_time": start_time, "end_time": end_time, "page": page, "size": size},
        )

    async def list_products(self, *, page: int = 0, size: int = 50) -> DoudianResponse:
        return await self.call("product.listV2", {"page": page, "size": size})

    async def edit_price(
        self, *, product_id: str, sku_id: str, price_cents: int
    ) -> DoudianResponse:
        return await self.call(
            "product.editPrice",
            {"product_id": product_id, "sku_id": sku_id, "price": price_cents},
            mutating=True,
        )

    async def update_stock(self, *, product_id: str, sku_id: str, stock: int) -> DoudianResponse:
        return await self.call(
            "product.editStock",
            {"product_id": product_id, "sku_id": sku_id, "stock": stock},
            mutating=True,
        )

    # --- 内部：一次尝试 ---
    async def _send(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        mutating: bool,
        timeout: httpx.Timeout | None,
        attempts: int,
    ) -> DoudianResponse:
        started = time.perf_counter()
        param_json = normalize_param_json(params)
        # ADR-001：timestamp 只生成一次，签名与 URL 用**同一个字符串**；重试时重新生成。
        timestamp = format_timestamp(datetime.now(DOUDIAN_TZ))
        signature = sign(
            self._app_secret,
            app_key=self._app_key,
            method=method,
            param_json=param_json,
            timestamp=timestamp,
            version=self._version,
            sign_method=self._sign_method,
        )
        access_token = await self._get_access_token()
        query: dict[str, str] = {
            "app_key": self._app_key,
            "method": method,
            "access_token": access_token,
            "timestamp": timestamp,
            "v": str(self._version),
            "sign_method": self._sign_method,
            "sign": signature,
        }
        path = "/" + method.replace(".", "/")
        self._logger.debug(
            "doudian.request",
            extra={
                "event": "doudian.request",
                "data": {"method": method, "path": path, "query": redact_query(query, self._redact_keys)},
            },
        )

        await self._bucket.acquire()
        async with self._semaphore:
            try:
                response = await self._http.post(
                    path,
                    params=query,
                    content=param_json.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    timeout=timeout if timeout is not None else self._timeout,
                )
            except httpx.TimeoutException as exc:
                self._log_attempt_result(
                    method,
                    http_status=None,
                    err_no=None,
                    request_id=None,
                    attempts=attempts,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    ok=False,
                    category=ErrorCategory.TIMEOUT,
                )
                raise DoudianTransportError(
                    f"请求超时: {method}",
                    category=ErrorCategory.TIMEOUT,
                    raw={"mutating": mutating},
                ) from exc
            except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TransportError) as exc:
                self._log_attempt_result(
                    method,
                    http_status=None,
                    err_no=None,
                    request_id=None,
                    attempts=attempts,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    ok=False,
                    category=ErrorCategory.CONNECTION,
                )
                raise DoudianTransportError(
                    f"连接错误: {method}: {type(exc).__name__}",
                    category=ErrorCategory.CONNECTION,
                    raw={"mutating": mutating},
                ) from exc

        duration_ms = (time.perf_counter() - started) * 1000.0
        try:
            parsed = self._parse_response(method, response, attempts=attempts)
        except DoudianError as exc:
            # §5.1-7：每次 call 结束写一条 `doudian.call`。失败也留一条，否则排障只剩异常栈。
            self._log_attempt_result(
                method,
                http_status=exc.http_status,
                err_no=exc.err_no,
                request_id=exc.request_id,
                attempts=attempts,
                duration_ms=duration_ms,
                ok=False,
                category=exc.category,
                message=exc.message,
            )
            raise
        self._log_attempt_result(
            method,
            http_status=parsed.http_status,
            err_no=parsed.err_no,
            request_id=parsed.request_id,
            attempts=parsed.attempts,
            duration_ms=duration_ms,
            ok=True,
        )
        return parsed

    def _log_attempt_result(
        self,
        method: str,
        *,
        http_status: int | None,
        err_no: int | None,
        request_id: str | None,
        attempts: int,
        duration_ms: float,
        ok: bool,
        category: ErrorCategory | None = None,
        message: str | None = None,
    ) -> None:
        """写一条 `doudian.call`（成功一次、失败也一次）。

        level 的取舍：可重试类别（还会再试）记 `warn`；不可重试（这次调用已经死了）记
        `error`；成功记 `info`。这样 `/api/logs?level=error` 直接等于"要人看的东西"。
        """
        data: dict[str, Any] = {
            "method": method,
            "http_status": http_status,
            "err_no": err_no,
            "attempts": attempts,
            "duration_ms": round(duration_ms, 3),
            "request_id": request_id,
            "ok": ok,
        }
        if category is not None:
            data["category"] = str(category)
        if ok:
            self._log("doudian.call", message=f"{method} ok", data=data)
            return
        retryable = category is not None and RETRYABLE_BY_CATEGORY.get(category, False)
        self._log(
            "doudian.call",
            level="warn" if retryable else "error",
            message=f"{method} failed: {message or 'unknown'}",
            data=data,
        )

    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        if self._token_source is None:
            return ""
        return await self._token_source.get_access_token(force_refresh=force_refresh)

    def _retry_delay(self, exc: DoudianError, attempts: int) -> float:
        """指数退避 + 抖动；429 带 `Retry-After` 时以它为准（上限 `backoff_max_s`）。"""
        cfg = self._rate_limit
        if isinstance(exc, DoudianRateLimitedError) and exc.retry_after is not None:
            return min(float(exc.retry_after), cfg.backoff_max_s)
        base = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (attempts - 1)))
        return base + random.uniform(0.0, 0.1 * base)

    # --- 内部：响应解析与分类 ---
    def _parse_response(
        self, method: str, response: httpx.Response, *, attempts: int
    ) -> DoudianResponse:
        status = response.status_code
        request_id = response.headers.get("x-tt-logid") or response.headers.get("x-request-id")
        body = self._json_body(response)
        if request_id is None and isinstance(body, dict):
            log_id = body.get("log_id")
            request_id = str(log_id) if log_id else None
        # §9.1：4xx 要"把 message 上报"；5xx/429 带上游 message 也排除了一半排障成本。
        upstream_message = ""
        if isinstance(body, dict):
            upstream_message = str(body.get("message") or body.get("msg") or "")
        message_suffix = f" message={upstream_message}" if upstream_message else ""

        if status == 429:
            raise DoudianRateLimitedError(
                f"限流: {method}{message_suffix}",
                retry_after=self._retry_after_seconds(response),
                http_status=status,
                request_id=request_id,
                raw=body if isinstance(body, dict) else None,
            )
        if status >= 500:
            raise DoudianUpstreamError(
                f"上游 5xx: {method} status={status}{message_suffix}",
                http_status=status,
                request_id=request_id,
                raw=body if isinstance(body, dict) else None,
            )
        if status >= 400:
            category = self._category_for_error_body(status, body)
            raise error_for_category(
                category,
                f"上游 {status}: {method}{message_suffix}"
                + (f" err_no={body.get('err_no')}" if isinstance(body, dict) else ""),
                http_status=status,
                err_no=body.get("err_no") if isinstance(body, dict) else None,
                request_id=request_id,
                raw=body if isinstance(body, dict) else None,
            )
        if not isinstance(body, dict):
            # 2xx 却不是 JSON：协议被破坏。非 mutating 可安全重试（幂等读）。
            raise DoudianUpstreamError(
                f"响应不是合法 JSON 对象: {method}",
                http_status=status,
                request_id=request_id,
            )
        if "err_no" not in body:
            # 200 但缺 err_no：不能当成功（网关错误页会被读成"数据为空"）。
            # 报成可重试的协议错误，让"偶发网关抽风"能自愈、持续异常能被看见。
            raise DoudianUpstreamError(
                f"响应缺少 err_no 字段: {method}",
                http_status=status,
                request_id=request_id,
                raw=body,
            )
        err_no_raw = body["err_no"]
        try:
            err_no = int(err_no_raw)
        except (TypeError, ValueError) as exc:
            raise DoudianUpstreamError(
                f"响应 err_no 非整数: {method} err_no={err_no_raw!r}",
                http_status=status,
                request_id=request_id,
                raw=body,
            ) from exc
        message = str(body.get("message") or body.get("msg") or "")
        if err_no != 0:
            category = classify_err_no(err_no)
            raise error_for_category(
                category,
                f"业务错误: {method} err_no={err_no} {message}".strip(),
                http_status=status,
                err_no=err_no,
                request_id=request_id,
                raw=body,
            )
        return DoudianResponse(
            ok=True,
            err_no=0,
            message=message,
            data=body.get("data"),
            http_status=status,
            request_id=request_id,
            raw=body,
            attempts=attempts,
        )

    @staticmethod
    def _category_for_error_body(status: int, body: Any) -> ErrorCategory:
        """HTTP 4xx 的分类（§9.1）：先看鉴权，再看 body 里有没有"真业务码"。

        关键区分：4xx 且 `err_no == 0`（或没有 err_no）是**协议/网关层**的失败 → `client_4xx`；
        只有 `err_no != 0` 才是抖店的业务错误（`business`），或点名过的签名/鉴权/限流码。
        把 4xx+err_no=0 记成 business，会让排障时把"路径写错/网关拒绝"当成"业务规则不通过"。
        """
        if status in (401, 403):
            return ErrorCategory.AUTH
        if isinstance(body, dict):
            err_no = body.get("err_no")
            if isinstance(err_no, int) and not isinstance(err_no, bool) and err_no != 0:
                return classify_err_no(err_no)
        return ErrorCategory.CLIENT_4XX

    @staticmethod
    def _json_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _log(
        self,
        event: str,
        *,
        message: str,
        data: Mapping[str, Any],
        level: str = "info",
    ) -> None:
        """结构化日志：`data` 里不含 sign/app_secret/access_token（脱敏是构造性的）。"""
        log_level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warn": logging.WARNING,
            "error": logging.ERROR,
        }[level]
        self._logger.log(
            log_level,
            message,
            extra={"event": event, "data": redact(dict(data), self._redact_keys)},
        )


def create_doudian_client(settings: Settings) -> DoudianClient:
    """按配置建 client（应用 lifespan 里建一次、`aclose()` 关一次）。

    `settings.doudian.dry_run=True` → 注入 cassette transport，**不读**任何凭据（§8.2），
    保证零凭据可跑；否则从环境变量取凭据，缺一个就 `ConfigError`（启动期失败）。
    """
    cfg = settings.doudian
    common: dict[str, Any] = {
        "base_url": cfg.base_url,
        "version": cfg.version,
        "sign_method": cfg.sign_method,
        "timeout": cfg.timeout.to_httpx(),
        "limits": cfg.limits.to_httpx(),
        "rate_limit": cfg.rate_limit,
        "redact_keys": settings.observability.redact_keys,
    }
    if cfg.dry_run:
        return DoudianClient(
            app_key="dry-run-app-key",
            app_secret="dry-run-app-secret",
            token_source=StaticTokenSource("dry-run-token"),
            transport=CassetteTransport(load_cassettes(DEFAULT_CASSETTE_DIR)),
            **common,
        )

    from app.errors import ConfigError  # 延迟 import：避免 services → app 层的模块级依赖

    app_key = os.environ.get(cfg.app_key_env, "")
    app_secret = os.environ.get(cfg.app_secret_env, "")
    access_token = os.environ.get(cfg.access_token_env, "")
    if not app_key:
        raise ConfigError(f"缺少环境变量 {cfg.app_key_env}（doudian.dry_run=false 时必须提供）")
    if not app_secret:
        raise ConfigError(f"缺少环境变量 {cfg.app_secret_env}（doudian.dry_run=false 时必须提供）")
    if not access_token:
        raise ConfigError(f"缺少环境变量 {cfg.access_token_env}（doudian.dry_run=false 时必须提供）")
    return DoudianClient(
        app_key=app_key,
        app_secret=app_secret,
        token_source=StaticTokenSource(access_token, env_name=cfg.access_token_env),
        **common,
    )
