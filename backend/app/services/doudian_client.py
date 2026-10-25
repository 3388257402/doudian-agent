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

另有两件事**不**归本文件管，读代码时最容易误挂到这里：

- **token 的获取与缓存**：`TokenSource` 只被"叫一次"——正常路径一次、鉴权失败时
  `force_refresh=True` 再一次。刷新失败怎么退避、要不要落库，都是实现方的事。
- **单次 `call` 的总时间预算**：契约 §9.2 要求 `total_budget_s`，但 v1.5.0 的状态是
  "已入册、代码未实现"（§13(m)）。所以最坏耗时只有 `max_attempts` × 退避这个上界，
  量级是几十秒 —— 具体推导见 `RateLimitConfig` 上方那段。任何"请求 30s 内必返回"的说法
  在当前代码里都不成立，不要把它写进文档或看板。
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

# 导出面的用法（新增 `__all__` 条目时先确认它确实要给外部用）：
#   - 装配层只用两个：`create_doudian_client`（lifespan 里建一次）与 `RateLimitConfig`
#     （config 里构造，注入超时/限流参数）。
#   - 测试用：`CassetteTransport` / `load_cassettes` / `Cassette`（离线回放）、
#     `StaticTokenSource`（假 token）、`DoudianResponse`（断言形状）。
#   - `redact_query` 出现在这里是因为 API 层/日志层要能把 query 脱敏后再展示，而 `redact`
#     本身在 observability 里——这里提供一个"知道 query 形状"的薄入口。
#   - `SIGNATURE_ERR_NOS` / `AUTH_ERR_NOS` / `RATE_LIMIT_ERR_NOS` 三个集合导出是为了让运维脚本
#     能列出"当前被判定为签名/鉴权/限流"的业务码（AUTH/RATE_LIMIT 现在是空的，见上方注释）。

DEFAULT_BASE_URL = "https://openapi-fxg.jinritemai.com"

# 四个超时是分层给的，每一层回答一个不同的问题（ADR-004 定的值，不是"随手 10s"）：
#   connect=3s  —— TCP+TLS 握手。ADR-004 实测公网握手 100~300ms，3s 已经是它的 10 倍：
#                  走到这里超时，说明是"网络坏了"而不是"抖店慢"，重试有意义、等下去没意义。
#   read=10s    —— 抖店改价/改库存接口本身是几十毫秒量级（ADR-004），10s 之后再等，得到的
#                  大概率是同一个超时；把它压得更小反而会把正常的长尾订单查询一起打死。
#   write=5s    —— 请求体只有几 KB（param_json），5s 传不完只可能是链路已经不可用。
#   pool=2s     —— **排队**超时，不是网络超时。它的作用是把"并发已经打满"变成 2s 内可见的
#                  失败，而不是让请求在池子里排 30s 再超时——后者会让人误判成上游慢。
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)

# 连接池：必须与令牌桶同实例（ADR-004 的整个论点是"限流和连接池是同一个容量概念"）。
# max_connections=20 留了 burst=16 之上的一点点余量；keepalive 10 条 + 30s 过期，
# 比"每次新建"省掉每次 100~300ms 的握手（ADR-004 问题 1）。
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
#:
#: 为什么**只有**这三个、而不把 `business` / `client_4xx` / `rate_limited` 也收进来：
#: 判据是"上游到底有没有可能已经执行了"。`timeout`/`connection`/`5xx` 三种情况下请求
#: 已经发出去了，只是回执没拿到 —— 上游可能已经改了价。`429` 是**明确没受理**（可安全重试）；
#: `business`（err_no != 0）和 `4xx` 是**明确受理并拒绝**，两者都不会产生"执行了但不知道"。
#: 把不该收的类别收进来，代价是正常的 429 重试也被毙掉；漏收一个，代价是重复改价/重复发券。
_AMBIGUOUS_ON_MUTATING = frozenset(
    {ErrorCategory.TIMEOUT, ErrorCategory.CONNECTION, ErrorCategory.SERVER_5XX}
)

#: 夹具目录（`backend/tests/fixtures/doudian/cassettes`）。
#:
#: `parents[2]` 从 `.../backend/app/services/doudian_client.py` 往上数三级 = `backend/`：
#: services → app → backend。少一级会指到 `app/`、多一级会指到仓库根——离线夹具找不到时
#: 的表现是"每个请求都 599"，而不是路径错误，所以这个偏移量值得单独写一句。
DEFAULT_CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "doudian" / "cassettes"

#: 方法名形状：`namespace.action`，两段都必须是标识符（§5.1-2 点号形式）。
#: 客户端本地校验的意义：拼错 method 不该消耗一次网络往返、更不该被算进限流额度。
#:
#: 为什么不用更宽的 `[^/]+`：`path = "/" + method.replace(".", "/")` 直接把 method 拼进 URL
#: 路径，所以 method 里任何 `/`、`?`、`#`、`..` 都会改变请求目标（路径穿越/参数注入）。
#: 这个正则同时也是那次拼接的**唯一**合法性保证——别在别处再拼一次 URL 而绕开它。
_METHOD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")


class TokenSource(Protocol):
    """access_token 来源。`force_refresh=True` 表示"上次调用被判鉴权失败，强制换新"。

    保证：`get_access_token` 返回一个能被服务端接受的 token 字符串（空串也能返回——
    `create_doudian_client` 在有凭据的路径上不允许出现空 token，那是装配层的责任）。
    不保证：刷新一定成功。实现方拿不到新 token **不应该**抛 `DoudianAuthError` 之外的
    东西，否则 `call()` 里那条"只刷一次"的边界会被异常绕过去。
    坑：同一次 `call` 里可能被调两次（第二次 `force_refresh=True`），所以实现必须可重入；
    也别在这里做无限重试——重试策略归 client，两层各自重试会把 `max_attempts` 变成乘法。
    """

    async def get_access_token(self, *, force_refresh: bool = False) -> str: ...


class StaticTokenSource:
    """固定 token（dry-run / 单测）。`force_refresh` 时重新读一次环境变量。

    保证：`force_refresh=True` 时只做"重读环境变量"这一件事，读不到就保持旧值。
    不保证：真能换到一个新 token —— 所以它**不能**冒充生产实现。
    坑：环境变量名在构造时就固定（`env_name`）；传 `None` 时 `force_refresh` 是纯 no-op，
    测试用它正好可以断言"鉴权失败后只多打一次请求、不多打第二次"。
    """

    def __init__(self, token: str, *, env_name: str | None = None) -> None:
        self._token = token
        self._env_name = env_name

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh and self._env_name:
            # 只用 `os.environ.get(..., 旧值)`：环境变量被删掉时保留旧 token，而不是变成空串——
            # 空串会让下一次签名请求必然 401，把"配置被清掉了"伪装成"token 失效"。
            self._token = os.environ.get(self._env_name, self._token)
        return self._token


# --------------------------------------------------------------------------------------
# 限流 + 重试参数：为什么是这些数（不是"经验值"）
# --------------------------------------------------------------------------------------
# 六个默认值都是契约 §5.1 的冻结取值，改它们要走契约变更。能自己推出来的是**上界**——
# 而这个上界决定了"一次 call 最坏能拖多久"，排障时要用它来判断"是不是卡在重试里"：
#
#   退避序列 = min(8, 0.4 * 2**(n-1))，n = 1,2,3（第 4 次尝试失败后直接抛，不再睡）
#            → 0.4 + 0.8 + 1.6 = 2.8s，抖动最多 +10% → 累计 ≤ 3.08s
#   每次尝试本身受 httpx.Timeout 约束（connect 3s + read 10s）→ 单次 ≤ 13s，4 次 ≤ 52s
#   ⇒ 一次 call 的最坏耗时 ≈ 55s（还没算令牌桶/信号量的排队时间）
#
# 所以"没有 total_budget_s"不是个小缺口：§13(m) 记的就是这件事。任何按"请求 30s 内返回"
# 写的超时/告警阈值，在连续 5xx 的窗口里都是错的。
#
# 桶与信号量为什么必须在**同一个实例**上（ADR-004）：分成两处就会出现"桶放行 8 QPS、连接池
# 只给 20 条各排队 2s"的互相踩；更要命的是"每次请求 new 一个 client"会让每个新桶都从满格
# 开始，限流等于不存在——那是最糟的一类不一致（文档承诺了、实现没有）。
@dataclass(frozen=True)
class RateLimitConfig:
    """限流 + 重试参数（默认值 = 契约 §5.1）。

    保证：四个参数在构造期就校验成"能算出有限等待"的取值范围；非法值立刻 `ValueError`。
    不保证：这些值配得上上游的真实配额。8 QPS 是契约按上游配额给的，本文件只消费它。
    坑：`max_attempts` 含**首次**尝试，所以它是"最多 4 次请求"而不是"首次 + 4 次重试"；
    按后者理解会把退避序列算多一倍。
    """

    rate_per_sec: float = 8.0
    burst: int = 16
    max_concurrency: int = 4
    max_attempts: int = 4
    backoff_base_s: float = 0.4
    backoff_max_s: float = 8.0

    def __post_init__(self) -> None:
        # 这四条校验的**唯一**目的：不让"进程活着但永远不干活"发生。
        # `burst=0` 时 `_TokenBucket.acquire` 在 `while True` 里永远拿不到 token（睡多少都
        # 攒不出额度），现场表现是"服务在线、所有请求挂起、没有任何报错" —— 比启动期
        # ValueError 难查一个数量级。`max_attempts=0` 同理会变成"一次都不试就抛"。
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
    """一次成功调用的结果（`ok` 恒为 True：失败一律走异常，没有 ok=False 的返回值）。

    `attempts` 是"打到第几次才成功"，`raw` 是**上游原始 body**：排障时要能把它整段贴给
    抖店工单，所以这里不做任何裁剪。`request_id`（`x-tt-logid`）是唯一能和上游对上的键。
    """

    ok: bool
    err_no: int
    message: str
    data: Any
    http_status: int
    request_id: str | None
    raw: dict[str, Any]
    attempts: int


def classify_err_no(err_no: int) -> ErrorCategory:
    """业务码 → 类别（纯查表，§9.1）。未收录的码一律 `business`（保守：不重试）。

    保证：同一个 `err_no` 永远映射到同一个类别（三个集合都是冻结的 frozenset，本函数没有
    分支依赖外部状态），所以分类结果可以复现、可以在日志里直接对账。
    不保证：未收录的码"其实不可重试"——它是按"不知道就别重试"落 `business` 的，不是查证过。
    坑：`AUTH_ERR_NOS` / `RATE_LIMIT_ERR_NOS` 现在是**空集**（见上面两段注释），所以
    "鉴权类业务码会命中的分支"目前走不到；补码时只加集合，别改这个函数的分支顺序——
    签名码必须排在鉴权码前面（10000/10008 同时可能被读成"鉴权问题"，先判签名才能给出
    "这是部署事故，去查 app_secret"这句正确的处置）。
    """
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
    """日志用：把 query 里的 `sign`/`access_token` 等替换成 `***`（脱敏在落盘前）。

    保证：返回的是**新 dict**（`dict(query)` 拷贝后再脱敏），调用方手里那份 query 不会被改。
    不保证：拼好的 URL 是安全的 —— 这个函数只处理 dict，`_send` 里拼出来的完整 query string
    不在它的职责范围内，所以**不要**在别处把 query 手拼成字符串再打日志。
    坑：这是抖店客户端这一层的脱敏，absent 的键不会出现在结果里；真正的兜底在
    `JsonlLogSink`（它会把 `DEFAULT_REDACT_KEYS` 与本层的键取并集再 scrub 一遍）。
    两层的意义不同：本层保证"写进日志的字段名从一开始就不带密文"，sink 层保证"漏掉一个
    键名也不会明文落盘" —— 所以**不能**拿"反正 sink 会脱敏"当理由省掉本层。
    """
    return redact(dict(query), redact_keys)  # type: ignore[return-value]


class _TokenBucket:
    """进程内令牌桶（`clock`/`sleep` 可注入 → 限流测试零等待）。

    保证：任意 1 秒窗口内放行的量不超过 `rate` 加上一个 `burst`（桶容量），因为补充永远被
    `min(burst, ...)` 截断——空闲一小时也不会攒出"一秒钟打 3600 个请求"的额度。
    不保证：跨进程/跨实例的限流。每个 `DoudianClient` 各有一个桶，两个进程 = 两份额度；
    多副本部署时要限制在单个实例内使用（或者接受上游 429 作为真正的闸门）。
    坑：`while True` 不是防御性写法 —— 注入的 `sleep` 只推进**假时钟**时，`await` 之后
    `elapsed` 可能仍是 0（真机上不会，但测试里 `sleep` 与 `clock` 是两个独立注入点），
    所以必须重新取时钟、重新算额度，而不是"睡一次就够了"。改成 `if` 会在时钟不前进的
    注入组合下死循环。
    """

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
        # 初始满桶：启动瞬间的 burst 个请求不需要等（冷启动要能立刻探活）。
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    @property
    def tokens(self) -> float:
        return self._tokens

    async def acquire(self, tokens: float = 1.0) -> float:
        """取 token；不够就按 `deficit / rate` 睡眠。返回累计等待秒数。

        保证：返回时额度已经扣掉（调用方拿到的是"已授权"）；等待时长**恰好**是补足差额
        所需的时间（`deficit / rate`），不多睡。
        不保证：公平性/顺序。先到的协程先醒是 `sleep` 实现的属性，这里没有队列。
        坑：返回值是给测试断言用的（"20 次调用虚拟耗时 ≥ (20-burst)/rate"），生产代码不要
        拿它做业务逻辑 —— 那是限流的内部等待，不是业务耗时。
        """
        waited = 0.0
        while True:
            now = self._clock()
            elapsed = now - self._last
            # `elapsed > 0` 才推进 `_last`：时钟被注入成"倒退"（或测试里停住）时，若把 `_last`
            # 跟着拨回去，下一次正常推进看到的 `elapsed` 会被算成一个巨大的正数 →
            # `min(burst, ...)` 立刻把桶灌满 → 限流在最不该失效的时刻失效。
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
    """一个录制好的上游响应。

    匹配靠 `method` 精确相等 + `body_contains` 递归子集（见 :func:`_is_subset`）；
    `status` 可以是 429/5xx —— 故障路径也要能离线复现，否则重试逻辑只能靠真网络验。
    """

    name: str
    method: str
    body_contains: dict[str, Any]
    status: int
    headers: dict[str, str]
    json_body: Any
    delay_ms: int
    source: Path


def load_cassettes(directory: str | Path = DEFAULT_CASSETTE_DIR) -> list[Cassette]:
    """读目录下所有 `*.json` cassette，按**文件名字典序**返回（匹配顺序确定，§10.2）。

    保证：同一个目录永远得到同一个列表顺序 —— 回放是"第一条命中的赢"，顺序不稳等于夹具
    行为随机（测试会变成偶发红）。
    不保证：`match.method` 一定非空。缺字段时塞空串，于是这条 cassette 永远匹配不上
    （`method` 一定有值，空串对不上任何真实方法名）—— 静默失效，不是报错。
    坑：目录不存在直接 `FileNotFoundError`（不是返回空列表）：空列表会让 dry-run 变成
    "每个请求都 599"，排障成本远高于启动期少一个夹具目录。
    """
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

    保证：每次请求都先在 `self.requests` 里留一条（method/params/body/sign/去 query 的 url），
    所以断言"发了什么"不需要改 transport；`delay_ms` 用真 `asyncio.sleep`，用于测超时路径。
    不保证：`sign` 一定正确 —— 它**故意**不校验签名，签名正确性归 `signing.py` 的单测；
    在这里校验会让每个测夹具用例都附带一份签名算法的复制品。
    坑：599 落在 `>= 500` 分支，是**可重试**类别，所以缺夹具的真实表现是"重试 4 次后抛
    `DoudianUpstreamError`"，不是"一次就失败"。断言异常类型没问题，断言"只请求一次"会红。
    """

    def __init__(self, cassettes: Sequence[Cassette]) -> None:
        self._cassettes = list(cassettes)
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """按顺序找第一条命中的 cassette 并回放；**没命中**返回 599 + `err_no=-1`。

        保证：① 每次请求都先记进 `self.requests`（即使没命中），所以"到底发了什么"可断言；
        ② 命中就回放 `status`/`headers`/`json` 全部三样（429/5xx 的夹具因此能测重试）；
        ③ `delay_ms` 用真 `asyncio.sleep`（不是假时钟）——它模拟的是**上游慢**，
        与限流的假时钟是两件事。
        不保证：`sign` 正确性（故意不校验）。
        坑：body 解析失败时按空 dict 匹配，于是"只匹配 body 子集"的 cassette 会在脏 body 上
        误命中——夹具里请用 `method` + 明确的 `body_contains`，别只靠 method。
        """
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
# 三个可注入的"环境"参数，各有明确的测试用途（生产路径都用默认值）：
#
#   `transport`  httpx transport 注入点。`CassetteTransport` 走它 → 零凭据、零网络（ADR-013）；
#                生产为 None（真网络）。**不要**用 respx/monkeypatch 在别处替换 client，
#                那样"dry-run 能跑"就不再覆盖真实的请求构造路径。
#   `clock`      单调秒表（默认 `time.monotonic`），只给令牌桶用；测试注入假时钟 → 限流零等待。
#   `sleep`      异步睡眠（默认 `asyncio.sleep`），令牌桶与退避都用它；测试注入"只推进假时钟
#                的 sleep" → 退避序列可以逐值断言。
#
# 注入的边界到此为止：`_send` 里的耗时统计用 `time.perf_counter()`（真实墙钟），不走 `clock`
# ——把两者混用会让"限流测试零等待"变成"耗时断言全为 0"。
class DoudianClient:
    """抖店开放平台客户端。线程/协程安全：状态都在实例内且只在事件循环里变更。

    保证：所有请求共用**一个** `httpx.AsyncClient`（连接池）+ 一个令牌桶 + 一个信号量，
    因此"限流额度"与"连接容量"是同一个概念，不会互相踩（ADR-004）。
    不保证：幂等。`mutating=True` 只是把"结果不确定"显式转成异常，真正的去重靠
    `actions.idempotency_key` + 对账（ADR-006 / ADR-011），不在本类。
    坑：① 必须在装配处 `aclose()`（lifespan 关闭时），否则连接池与 keep-alive 连接泄漏，
    `asyncio.run` 退出时还会刷 "Unclosed client" 告警；② 不要在同一个进程里跨事件循环
    复用实例 —— 信号量的等待者挂在创建它的那个 loop 上；③ `dry_run` 用的也是这个类，
    所以"dry-run 能跑"和"真凭据能跑"走的是同一条代码路径，不存在只在生产才生效的分支。
    """

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
        # 三条构造期校验：都选了"启动就炸"而不是"运行时才发现"。
        # app_key 缺失时签名一定是错的，等第一个请求回来才 401 等于把配置事故伪装成鉴权事故。
        if not app_key:
            raise DoudianSignatureError("app_key 缺失，无法签名（检查 DOUDIAN_APP_KEY）")
        # sign_method 只认一个值：多实现一分支就多一条"客户用别的算法、我们算出来的签名
        # 服务端不认"的排障支路，而官方现网只有 hmac-sha256（ADR-007）。
        if sign_method != SIGN_METHOD_HMAC_SHA256:
            raise DoudianSignatureError(
                f"unsupported sign_method: {sign_method!r}（只实现 {SIGN_METHOD_HMAC_SHA256}）"
            )
        # base_url 必须是绝对 URL：`AsyncClient(base_url=...)` 只接受带 scheme 的值，
        # 但它的报错发生在**第一次请求**时，那时已经离装配点很远了。
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
        # `transport=None` 时 httpx 用真网络；注入它（CassetteTransport）是"零凭据可测"的
        # 唯一入口（ADR-013），所以它必须是构造参数而不是可以被内部覆盖的属性。
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=self._timeout,
            limits=self._limits,
        )
        # 桶与信号量跟连接池**同一个实例**、同一份 clock/sleep：否则"桶放行 8 QPS、
        # 连接池只给 20 条各排队 2s"会互相踩（ADR-004），且限流测试没法用假时钟断言。
        self._bucket = _TokenBucket(
            rate_per_sec=self._rate_limit.rate_per_sec,
            burst=self._rate_limit.burst,
            clock=clock,
            sleep=sleep,
        )
        self._semaphore = asyncio.Semaphore(self._rate_limit.max_concurrency)

    # --- 生命周期 ---
    async def aclose(self) -> None:
        """关闭唯一那个 `httpx.AsyncClient`（释放连接池）。

        保证：幂等（httpx 的 `aclose` 重复调用不抛），关闭后再 `call` 会立刻抛 httpx 的
        "client has been closed"，而不是静默卡住。
        不保证：这次的关闭会中断正在飞的请求 —— 那是事件循环取消的责任。
        坑：这是**必须**由装配方调用的副作用（`app.main` lifespan 关闭时）；忘了它不会报错，
        只会漏连接和一条 UnclosedClient 告警，在 CI 里很容易被 --strict 之外的配置吞掉。
        """
        await self._http.aclose()

    async def __aenter__(self) -> DoudianClient:
        """`async with` 入口：返回自己，**不**建任何新资源（资源已在 `__init__` 建好）。"""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """`async with` 出口：只关连接池，**返回 None → 不吞异常**（异常继续往外抛）。"""
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

        保证：① 失败一定是异常，`DoudianResponse` 只有在真成功时才出现；
        ② 鉴权失败只会为整次 `call` 强制刷一次 token（不会在每次尝试里各刷一次）；
        ③ `mutating=True` 的 timeout/connection/5xx 一定抛
        `DoudianAmbiguousOutcomeError`，绝不会被重试。
        不保证：幂等、也不保证"最多多久返回"。没有 `total_budget_s`（§13(m)），最坏耗时是
        `max_attempts` × 退避 + 4 次超时的量级（约 55s，推导见 `RateLimitConfig` 上方）。
        坑：`mutating` 判断错了没人能救——`mutating=False` 会把一个可能已经执行的改价当成
        "可安全重试的读"重打一遍；调用方在写 `edit_price` 这类封装时必须传 True。
        重试是**本函数**的职责，不要在调用方再包一层重试（那是 `max_attempts ** 2`）。
        """
        # 方法名先校验、再动网络：拼错的方法名不该消耗令牌桶额度，更不该变成一次真实往返
        # 之后才 404。这里的 ValueError 是**调用方写错了代码**，不是上游的错。
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
                # 这个 `except` 必须排在 `except DoudianError` **前面**：`DoudianAuthError`
                # 是它的子类，顺序颠倒后 401 会掉进通用分支，被当成"不可重试"直接抛出去，
                # 强制刷新那条路径就永远走不到（测试里表现为"token 过期一次就整轮失败"）。
                # 用 `auth_retried` 而不是看 attempts：令牌失效跟"第几次尝试"无关，
                # 拿 attempts 做闸门会让"第 3 次遇到 401"再刷一次，把 4 次尝试预算烧在刷新上。
                if auth_retried or self._token_source is None:
                    raise
                auth_retried = True
                self._log(
                    "doudian.auth_refresh",
                    level="warn",
                    message=f"鉴权失败，强制刷新 token 后重试: {method}",
                    data={"method": method, "attempt": attempts, "err_no": exc.err_no},
                )
                # 刷新结果**不看**：`get_access_token` 返回值被忽略是有意的——token 由
                # `_send` 重新取（它每次尝试都取一次），在这里判断"刷到了没有"只会多一份
                # 与 token_source 重复的逻辑。刷不到就是下一轮再 401，然后 `raise`。
                await self._token_source.get_access_token(force_refresh=True)
            except DoudianError as exc:
                # **顺序要紧**：先判"可变操作 + 结果不确定"，再判 `retryable`。
                # 反过来的话，`mutating=True` 遇到 5xx 会先看到 `retryable=True` 而重试——
                # 把"可能已经改成功了一次"变成"确定改了两次"，这正是 ADR-011 要拦的事。
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
                # `attempts >= max_attempts` 用 `>=`（不是 `>`）：max_attempts 含首次尝试，
                # `attempts` 是"已经打出去的次数"，到 4 就该抛 —— 用 `>` 会多打一次。
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
                # `self._sleep` 而不是 `asyncio.sleep`：注入的 sleep 让限流/退避测试用假时钟，
                # 零等待跑完。"睡多久"由 `_retry_delay` 单独决定，便于单测直接断言数值。
                await self._sleep(delay)

    # --- 业务封装 ---
    # 这五个方法都是薄封装，唯一的信息量在**是否 mutating**。写它们的理由是"方法名字面量
    # 只出现一次"：调用点拼字符串迟早会拼错，而拼错的方法名在真机上要一次往返才知道。
    async def get_shop_info(self) -> DoudianResponse:
        """读店铺信息（`shop.getShopInfo`）。

        保证：只读，失败走 `call` 的重试/分类语义（可安全重试）。
        不保证：任何字段形状 —— 上游 body 原样在 `data`/`raw` 里，解析归调用方。
        坑：dry-run 下它同样要命中 cassette，没录就是 599（→ 重试 4 次后 `DoudianUpstreamError`）。
        """
        return await self.call("shop.getShopInfo", {})

    async def search_orders(
        self,
        *,
        start_time: str,
        end_time: str,
        page: int = 0,
        size: int = 50,
    ) -> DoudianResponse:
        """按时间窗搜订单（`order.searchList`，只读）。

        保证：参数按契约字段名下发（`start_time`/`end_time`/`page`/`size`），不做时区换算。
        不保证：分页边界可用性 —— `page`/`size` 由调用方保证（默认 50 是契约给的单页量）。
        坑：时间窗必须由调用方按业务日期算好再传；在这里兜底拼日期会让"空跑一天"和
        "真的没有订单"不可区分。
        """
        return await self.call(
            "order.searchList",
            {"start_time": start_time, "end_time": end_time, "page": page, "size": size},
        )

    async def list_products(self, *, page: int = 0, size: int = 50) -> DoudianResponse:
        """列商品/规格（`product.listV2`，只读）。

        保证：只读可重试；`data` 原样返回（本仓的 `effects` 调度器会自己从里面取 products）。
        不保证：返回条数 —— 上游可能少于 `size`，翻页逻辑在调用方。
        坑：`effects.run_effect_evaluator_once` 依赖它"按 page=0,size=100 拿到目标 SKU"，
        改默认值会直接影响效果评估能不能找到目标。
        """
        return await self.call("product.listV2", {"page": page, "size": size})

    async def edit_price(
        self, *, product_id: str, sku_id: str, price_cents: int
    ) -> DoudianResponse:
        """改价（`product.editPrice`，**可变操作**）。

        保证：`mutating=True` → 遇 timeout/connection/5xx 抛
        `DoudianAmbiguousOutcomeError`，绝不重试（重试就是重复改价）。
        不保证：改价成功后的实际生效状态；也不校验价格区间（校验在 `validate_params`）。
        坑：`price_cents` 是**整数分**，本方法不做元→分换算（换算在适配层，混着做就是资损）。
        """
        return await self.call(
            "product.editPrice",
            {"product_id": product_id, "sku_id": sku_id, "price": price_cents},
            mutating=True,
        )

    async def update_stock(self, *, product_id: str, sku_id: str, stock: int) -> DoudianResponse:
        """改库存（`product.editStock`，**可变操作**）。

        保证：同上 —— 结果不确定时不重试，交对账。
        不保证：`stock` 是"增量"还是"绝对值"由上游语义决定，本方法按**绝对值**下发。
        坑：`stock=0` 是合法值（清库存动作就是这么用的），不要在这一层把 0 当 falsy 拦掉。
        """
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
        """一次尝试：签名 → 限流 → 发请求 → 解析分类（**不**重试、**不**吞异常）。

        保证：① 签名、URL query、body 里的 `param_json`/`timestamp` 是同一份值（ADR-001）；
        ② 无论成功失败，每次尝试都写且只写一条 `doudian.call`（§5.1-7），失败排障不会只剩
        一段异常栈；③ 传输层异常一律转成 `DoudianTransportError` 并带上 category。
        不保证：重试决策 —— 那是 `call` 的事；本函数只做"这一次"。
        坑：`mutating` 在这里只被塞进 `raw={"mutating": ...}` 供日志/审计用，**不做任何**
        重试判断；别以为改了这里就能影响重试行为（那会让 `call` 与本函数各有一份策略）。
        """

        started = time.perf_counter()
        # 计时用 `time.perf_counter()` 而**不是**注入的 `self._clock`：`clock` 是给令牌桶用的
        # （测试里注入假时钟让它零等待），如果耗时也走假时钟，`duration_ms` 在测试里会恒为
        # "虚拟时间"，而真实吞吐/超时分析要的是墙钟。两个时钟各有用途，别合并。
        # `perf_counter`（单调、不受系统时间调整影响）才是"算耗时"的正确来源。
        # `param_json` 只归一化**一次**，签名与 body 用同一个字符串（ADR-001 的同一类错误）：
        # 分别 dumps 两次、只要有一处改了分隔符，服务端算出来的摘要就和我们不一致。
        param_json = normalize_param_json(params)
        # ADR-001：timestamp 只生成一次，签名与 URL 用**同一个字符串**；重试时重新生成。
        # "重试时重新生成"是必要的：`_send` 每次尝试都会走到这里，旧 timestamp 可能已过期
        # （§9.2）。反过来，如果在 `call` 里生成一次传下来，第 4 次尝试就会带着一个几分钟前
        # 的时间戳去签名，服务端直接判签名过期——那会表现为"重试越多越必然失败"。
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
        # token 每次尝试都取（而不是构造时取一次）：token 会过期，长跑进程里"缓存到死"是最
        # 常见的 401 来源。`TokenSource` 实现负责它自己的缓存，本层只负责"该问就问"。
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
        # 这条 debug 日志必须过 `redact_query`：query 里有 access_token 与 sign，原样打就是
        # 把凭据写进 JSONL。`self._redact_keys` 来自 settings，与本层的默认集合是并集关系。
        self._logger.debug(
            "doudian.request",
            extra={
                "event": "doudian.request",
                "data": {"method": method, "path": path, "query": redact_query(query, self._redact_keys)},
            },
        )

        # 顺序：**先令牌、后信号量**。反过来（先占并发槽再等令牌）会让持有信号量的协程在
        # 桶上睡，有效并发被压到 1 附近，吞吐被退避拖成串行——而且看起来像"上游变慢了"。
        # 日志在上面、acquire 在这里也是刻意的：卡在限流里也要留痕，否则"没有 request 日志"
        # 会被误判成"根本没调用"。
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
            # `TimeoutException` 必须**排在** `TransportError` 之前：它是 TransportError 的
            # 子类，顺序颠倒后所有超时都被归成 CONNECTION，`_AMBIGUOUS_ON_MUTATING` 里两个
            # 类别虽然都在集合里、但日志与告警的 category 会整个错位。
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
            # 这里的 `raise` 不带 from：`_parse_response` 里抛的就是最终类别，包装反而会让
            # `except DoudianRateLimitedError` 之类的调用点判断失效（异常类型必须是精确的）。
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

        保证：一条事件 = 一条 JSONL 行；`data` 里的键**固定**（缺的写 null），前端按行计数
        不需要判空。
        不保证：`level=error` 不等于"这次 call 最终失败了"——判据是**类别**而不是"还会不会
        再试"，所以最后一次尝试遇到 5xx（可重试类别）仍然是 `warn`。要抓"最终失败"，看
        `doudian.ambiguous` / 上层的 run 状态，或者按 `ok=false && attempts==max_attempts` 过滤。
        坑：别在这里把 `duration_ms` 四舍五入到整秒——退避的抖动上限只有 0.1×delay，
        粗化后数值就看不出来了。
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
        """取 token；**没有** `TokenSource` 时返回空串（不是抛错）。

        保证：`token_source=None` 是一条被支持的路径（dry-run 的假凭据、只读夹具回放），
        所以这里返回空串让请求照发，而不是在客户端核心逻辑里插一个"必须有凭据"的分支。
        不保证：空 token 会被服务端接受 —— 真凭据路径上 `create_doudian_client` 已经拦住了
        "缺 access_token"的情形，所以空串只出现在不需要它的场景。
        坑：不要在别处（比如 `call`）也判空；两处判空会让 dry-run 突然跑不起来。
        """
        if self._token_source is None:
            return ""
        return await self._token_source.get_access_token(force_refresh=force_refresh)

    def _retry_delay(self, exc: DoudianError, attempts: int) -> float:
        """指数退避 + 抖动；429 带 `Retry-After` 时以它为准（上限 `backoff_max_s`）。

        保证：返回值恒为 `>= 0` 的有限数（**不会**把负数/NaN 传进 `sleep`）。
        不保证：总耗时上界 —— 这里只算"下一次睡多久"，4 次尝试的累计上界 ≈ 3.08s（见
        `RateLimitConfig` 上方），但每次请求本身的超时是另一笔账。
        坑：`Retry-After` 的**下限只到 0**，不是 `backoff_base_s`。上游给 `Retry-After: 0`
        时我们确实立刻重试（这是它的语义），代价是"0 值 + 4 次尝试"能在几十毫秒内打完，
        上游很可能再给一次 429；要更保守就在调用点兜，不要在这里偷偷加料。
        """
        cfg = self._rate_limit
        if isinstance(exc, DoudianRateLimitedError) and exc.retry_after is not None:
            # `Retry-After` 必须**两头夹**：上限挡"上游给 3600 我们真等一小时"，
            # 下限挡"上游给 -1 或非法值"。后者是 review 实测出的真 bug：
            # 不做下限时 `sleep(-1)` 会让注入时钟倒退，退避等于不存在，
            # 重试全速打完、限流被彻底绕过。负数/NaN 一律按 0 处理（立刻重试，至少不倒退）。
            # 解析失败走**另一条**路（`backoff_base_s`）而不是 0：头在、但不是数字，说明
            # 我们对上游的协议理解有偏差，此时按常规退避（0.4s 起）比按"立刻重试"更安全。
            try:
                retry_after = float(exc.retry_after)
            except (TypeError, ValueError):
                retry_after = cfg.backoff_base_s
            if retry_after != retry_after or retry_after < 0:  # NaN 或负数
                # 自己和自己不相等是 NaN 的唯一可靠判据（`math.isnan` 也行，但这里不想
                # 为了一个判断再引一个 import；NaN 过不了下面的 min 比较，行为会变得不确定）。
                return 0.0
            return min(retry_after, cfg.backoff_max_s)
        # 第 n 次尝试的退避 = min(cap, base * 2**(n-1)) → 0.4 / 0.8 / 1.6（第 4 次不再睡）。
        # 抖动只加不减（`uniform(0, 0.1*base)`）：只有一个方向的抖动能把"所有协程同一时刻
        # 醒来"的羊群效应打散，同时保证下界仍是 base（不会因为抖动把退避缩成 0）。
        base = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (attempts - 1)))
        return base + random.uniform(0.0, 0.1 * base)

    # --- 内部：响应解析与分类 ---
    # 响应分类树（下方分支顺序就是这张树的先序；顺序不可重排，见方法 docstring）：
    #
    #   status == 429            → rate_limited（可重试，读 Retry-After）
    #   status >= 500            → server_5xx（可重试）
    #   status >= 400            → 再看 body.err_no：
    #                                err_no 非 0 → 按 `classify_err_no` 归类（签名/鉴权/业务）
    #                                err_no 为 0 或无 → client_4xx（协议/网关层，不重试）
    #   body 不是 JSON 对象      → server_5xx（协议被破坏，按可重试处理）
    #   body 缺 err_no           → server_5xx（网关错误页不能被当成"数据为空"）
    #   err_no 非整数            → server_5xx（不猜成 0）
    #   err_no != 0              → business/签名/鉴权/限流
    #   err_no == 0              → 成功（`DoudianResponse(ok=True)`）
    def _parse_response(
        self, method: str, response: httpx.Response, *, attempts: int
    ) -> DoudianResponse:
        """把一次 `httpx.Response` 分类成"成功"或一个精确的 `DoudianError` 子类。

        保证：分类顺序是**固定**的 429 → 5xx → 4xx → 非 JSON → 缺 err_no → err_no 非整数 →
        err_no != 0；每一类都有对应的 `category`，且这个 category 是 `error_for_category`
        唯一的事实源。
        不保证：`raw` 一定是 dict（非 JSON 的 body 会被丢成 None）。
        坑：**顺序不能重排**。429 是 4xx，如果让 `status >= 400` 的分支先跑，
        `_category_for_error_body` 会把它判成 `CLIENT_4XX`（不可重试）→ 限流永远不重试；
        同理 5xx 若排在 4xx 之后也吃不到，因为 `>= 400` 包含 `>= 500`。
        """
        status = response.status_code
        # request_id 有三处来源，按可靠性排序：`x-tt-logid`（抖店网关）→ `x-request-id`
        # （通用/代理注入）→ body.log_id（个别接口把它放在体里）。三处都要试：排障时这
        # 是唯一能和上游工单对上的键，宁可多写两个 fallback 也不要报"no request id"。
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

        # ① 429 必须最先判：它同时满足"4xx"和"可重试"，只有放在最前面才能保住 retryable。
        if status == 429:
            raise DoudianRateLimitedError(
                f"限流: {method}{message_suffix}",
                retry_after=self._retry_after_seconds(response),
                http_status=status,
                request_id=request_id,
                raw=body if isinstance(body, dict) else None,
            )
        # ② 5xx 在 4xx 之前：`>= 400` 也吃 5xx，顺序颠倒会让 5xx 被当成 client error（不可重试）。
        if status >= 500:
            raise DoudianUpstreamError(
                f"上游 5xx: {method} status={status}{message_suffix}",
                http_status=status,
                request_id=request_id,
                raw=body if isinstance(body, dict) else None,
            )
        if status >= 400:
            # 4xx 的类别不在这里拍板，交给 `_category_for_error_body` 看 body——4xx+err_no=0
            # 是协议/网关问题，4xx+err_no!=0 才是业务问题，混成一个类别会让排障方向全错。
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
            # 上游理论上给 int，但见过字符串 "0"。能转就当它是对齐的；转不了就是协议问题，
            # 不能猜成 0（那会把一个错误响应当成功返回）。
            raise DoudianUpstreamError(
                f"响应 err_no 非整数: {method} err_no={err_no_raw!r}",
                http_status=status,
                request_id=request_id,
                raw=body,
            ) from exc
        message = str(body.get("message") or body.get("msg") or "")
        if err_no != 0:
            # 抖店的主要失败形态：HTTP 200 + err_no != 0。只看状态码在这里必然漏判。
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
        """尽量把 body 解析成 JSON；失败返回 `None`（**不抛**）。

        保证：任何 body 都能安全调用它——调用方据此判断"是不是 JSON 对象"，不需要先 try。
        不保证：非 UTF-8 的 2xx body 会被当成 None → 上层报"响应不是合法 JSON 对象"，
        错误信息里**没有**原文（原文可能含凭据，宁可不打）。
        坑：45 行以后 `isinstance(body, dict)` 的分支都依赖这个 None 语义；改成返回 `{}`
        会让"网关错误页"变成"缺 err_no"从而改变 category。
        """
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """读 `Retry-After`（秒）。缺失/非数字 → `None`（交给指数退避兜底）。

        保证：**不**在这里做上下限夹取 —— 夹取在 `_retry_delay` 里，两处都夹会让
        "429 的等待上限"出现两个不同的定义。
        不保证：支持 HTTP-date 形式的 `Retry-After`（抖店现网给的是秒数；给日期会走到
        `ValueError` → None → 退回指数退避，行为仍然安全，只是退避可能比上游期望的短）。
        坑：`float(raw)` 会接受 `"nan"`/`"inf"` 这类串，所以负值/NaN 必须由 `_retry_delay`
        兜住，不要把"这里已经校验过"当事实。
        """
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
        """结构化日志：`data` 里不含 sign/app_secret/access_token（脱敏是构造性的）。

        保证：`redact(dict(data), ...)` **先拷贝**再脱敏，所以调用方传进来的 dict 不会被改，
        重复使用同一个 dict 也不会有"第一次被脱敏、第二次还是 ***"的副作用。
        不保证：`data` 里所有键名都在脱敏集合里 —— 校验靠 `redact` 的键名层 + sink 的
        文本层，本函数只负责"送去脱敏"这一步。
        坑：`level` 走的是 `dict[level]` 查表，传一个不在 `debug/info/warn/error` 里的字符串
        会直接 KeyError（不是静默降级成 info）。这是有意的：等级的拼写错误会让"该告警的
        事件记成 info"，静默降级比崩更难发现。
        """
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

    保证：返回的实例已经配好 base_url/timeout/limits/限流参数，调用方不需要（也不应该）
    再改任何一项——它们是同一个"容量"概念的四个面（ADR-004）。
    不保证：凭据有效。这里只检查"有没有设置"，不检查"服务端认不认"；后者第一次调用才知道。
    坑：① dry_run 分支用的是**假凭据 + 夹具 transport**，所以它和真实路径的差异只有
    transport 与凭据两处，任何"只有生产才走的分支"都会在 dry-run 里被漏测；
    ② 返回的 client 必须由调用方关（lifespan 关闭时 `aclose()`），本函数不登记任何清理钩子；
    ③ 缺凭据是 `ConfigError`（启动期）**而不是**第一次请求时 401 —— 这是有意的：把配置
    事故挡在启动前，而不是让它伪装成运行期的鉴权问题。
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
