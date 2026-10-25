"""抖店开放平台异常族（CONTRACT §5.1.2，逐字实现）。

两条硬规则：

1. 与 `app/errors.py`（应用层异常，直接映射 HTTP）**分开**。本模块的异常只在
   client / 服务层内部流转，API 边界由 `UpstreamError` 统一包一次（§5.0 映射规则 1）。
2. `retryable` **不在调用点手写**。契约 §5.1.2 末段明确要求"必须由类别查表得出"，
   唯一事实源是 `RETRYABLE_BY_CATEGORY`（内容照抄 §9.1 表）。
   构造时若传入的 `retryable` 与表不符 → `ValueError`（把"手写错了"变成立刻可见的 bug，
   而不是一个静默的重试行为差异）。

"类别 → 谁能处置"（§9.1 的处理动作列，写在这里是因为读代码时最常问的就是这个）：

| 类别 | 谁动手 | 动作 |
| --- | --- | --- |
| `timeout` / `connection` / `server_5xx` | `doudian_client.call` | 退避重试（只读）；可变操作转 `ambiguous` 交对账 |
| `rate_limited` | `doudian_client.call` | 按 `Retry-After`（夹在 [0, backoff_max]）重试，可变操作也重试 |
| `client_4xx` / `business` | `execute` 节点 | 不重试，该动作记失败，run 继续 |
| `signature` | **人** | 部署事故（密钥/算法），不重试不刷 token，告警 |
| `auth` | `doudian_client.call` | 强制刷 token **一次**后重试，仍失败就抛 |
| `ambiguous` | 对账任务（§9.3） | 不自动重发，人工/对账确认上游是否已执行 |

这张表与 `RETRYABLE_BY_CATEGORY` 是同一份事实的两种投影：改一个必须对另一个。
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.enums import ErrorCategory

__all__ = [
    "RETRYABLE_BY_CATEGORY",
    "ERROR_CLASS_BY_CATEGORY",
    "DoudianError",
    "DoudianTransportError",
    "DoudianUpstreamError",
    "DoudianRateLimitedError",
    "DoudianClientError",
    "DoudianSignatureError",
    "DoudianAuthError",
    "DoudianBusinessError",
    "DoudianAmbiguousOutcomeError",
    "error_for_category",
]

#: §9.1 表的机器可读形式：类别 -> 是否可重试。
#: `auth` 是"❌ 当次"：client 允许强制刷新 token 后重试一次，但那是 client 的策略，
#: 不是 `retryable=True`——否则调用方会对一个 token 已失效的请求无限重试。
#:
#: 三处必须一致，改一处就要三处一起看：① 这张表；② `doudian_client._AMBIGUOUS_ON_MUTATING`
#: （可变操作下哪些"可重试"类别反而**不能**重试）；③ `doudian_client.call` 里的判断顺序
#: （先判 ambiguous、再判 retryable）。举例：把 `TIMEOUT` 改成 False，可变操作的行为不变
#: （它在那之前就被转成 ambiguous 了），但**只读**请求的重试会静默消失——这类改动不会让
#: 任何测试变红，只会让偶发超时的失败率上升。
RETRYABLE_BY_CATEGORY: dict[ErrorCategory, bool] = {
    ErrorCategory.TIMEOUT: True,
    ErrorCategory.CONNECTION: True,
    ErrorCategory.SERVER_5XX: True,
    ErrorCategory.RATE_LIMITED: True,
    ErrorCategory.CLIENT_4XX: False,
    ErrorCategory.SIGNATURE: False,
    ErrorCategory.AUTH: False,
    ErrorCategory.BUSINESS: False,
    ErrorCategory.AMBIGUOUS: False,
    ErrorCategory.CONFIG: False,
    ErrorCategory.UNKNOWN: False,
}


# 为什么上下文字段在**类上**给默认值（`http_status = None` 等），而不是只在 `__init__` 里赋值：
# ① 任何实例（包括未来某个忘了调 `__init__` 的子类路径）访问 `exc.retryable` 都不会 AttributeError；
# ② `__str__` 用 `is not None` 判断，靠的就是"没设过时是类默认值"这个事实；
# ③ 这些默认值是**契约形状**的一部分（`RetryPolicy` 只看 `retryable`，不信别的）。
class DoudianError(Exception):
    """抖店侧异常基类。

    `category` 与 `retryable` 是契约要求的实例属性（agent-core 的 RetryPolicy 按
    `retryable` 判定，不硬编码类别）。子类用 ClassVar 固定 `category`，构造时只需传消息。
    """

    category: ClassVar[ErrorCategory] = ErrorCategory.UNKNOWN
    http_status: int | None = None
    err_no: int | None = None
    request_id: str | None = None
    raw: dict[str, Any] | None = None
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        err_no: int | None = None,
        request_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """构造异常；`retryable` 只允许"与分类表一致"的显式值。

        保证：`self.retryable` **恒等于** `RETRYABLE_BY_CATEGORY[category]` —— 调用点就算传了
        `retryable=`，也只能传和表一致的那个值，传反了立刻 `ValueError`（把"手写错了"变成
        编译/测试期可见的 bug，而不是一个静默的重试行为差异）。
        不保证：`message` 的格式。它只被 `__str__` 拼进可读串，不做结构化解析。
        坑：**不要**在子类里覆盖 `category` 之外的 ClassVar（尤其 `retryable`）——子类的
        `category` 一变，`retryable` 会自动跟着表走，手动改它就是绕过这张表。
        """
        self.message = message
        # 子类用 ClassVar 固定 category；显式传参优先（`error_for_category` 就是这么用的）。
        resolved = category if category is not None else type(self).category
        self.category = resolved
        expected = RETRYABLE_BY_CATEGORY[resolved]
        if retryable is not None and retryable != expected:
            raise ValueError(
                f"retryable={retryable} 与类别 {resolved!r} 的分类表不符"
                f"（唯一事实源 CONTRACT §9.1，见 RETRYABLE_BY_CATEGORY）"
            )
        self.retryable = expected
        # 四个"上下文"字段全部可选：能拿到就带上（排障时它们是唯一能和上游对上的信息），
        # 拿不到就是 None——不要为了填满而编一个（例如 `request_id="unknown"` 会让排障
        # 误以为"上游没给 id"和"上游给了一个叫 unknown 的 id"是同一件事）。
        self.http_status = http_status
        self.err_no = err_no
        self.request_id = request_id
        self.raw = raw
        super().__init__(message)

    def __str__(self) -> str:
        """可读串：`<message> category=... [err_no=...] [http_status=...] [request_id=...]`。

        保证：缺的上下文字段**不出现**（不是打成 `None`），所以日志里不会有 "err_no=None"
        这种噪音；`category` 一定出现（它是排障第一眼要看的）。
        不保证：能被结构化解析（它不是 JSON，字段顺序固定但不保证被谁 grep）。
        """
        parts = [self.message, f"category={self.category}"]
        if self.err_no is not None:
            parts.append(f"err_no={self.err_no}")
        if self.http_status is not None:
            parts.append(f"http_status={self.http_status}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self}>"


# 六个子类的分工（每个只声明 `category`，别的什么都不加 —— 这是刻意的）：
#
#   子类                            category        谁抛
#   DoudianTransportError           timeout / connection   `_send` 的 httpx 异常分支
#   DoudianUpstreamError            server_5xx             5xx / 非 JSON / 缺 err_no / err_no 非整数
#   DoudianRateLimitedError         rate_limited           429（带 `retry_after`）
#   DoudianClientError              client_4xx             4xx 且没有真业务码
#   DoudianSignatureError           signature              `classify_err_no` 命中签名码 / 构造期缺 app_key
#   DoudianAuthError                auth                   401/403（4xx 分类时优先判）
#   DoudianBusinessError            business               err_no != 0 且未收录
#   DoudianAmbiguousOutcomeError    ambiguous              `call` 把可变操作的不确定结果包一层
#
# `DoudianTransportError` 的 category 默认是 TIMEOUT，CONNECTION 由构造参数显式传 —— 因为两者
# 共用同一个类（HTTP 层都是"没拿到响应"），而重试语义与日志 category 需要区分。
class DoudianTransportError(DoudianError):
    """超时 / 连接错误（`timeout` | `connection`），可重试。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.TIMEOUT


class DoudianUpstreamError(DoudianError):
    """上游 5xx，可重试。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.SERVER_5XX


class DoudianRateLimitedError(DoudianError):
    """限流（HTTP 429 或限流业务码），可重试；`retry_after` 来自响应头。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.RATE_LIMITED

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class DoudianClientError(DoudianError):
    """客户端 4xx（非签名/鉴权原因），不可重试。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.CLIENT_4XX


class DoudianSignatureError(DoudianClientError):
    """签名错误：部署事故，不重试、不刷 token、必须告警。

    继承 `DoudianClientError` 而不是直接继承 `DoudianError`：签名错误在 HTTP 上是 4xx 语义
    （"这次请求不被接受"），所以任何 `except DoudianClientError` 的调用点都应该一起兜住它。
    代价是**不能**用 `except DoudianClientError` 区分"参数错"和"密钥错"——要区分必须精确捕获
    本类（`doudian_client` 里就是用 `except (httpx...)` / `except DoudianAuthError` 逐个处理的，
    没有依赖这个父类）。
    """

    category: ClassVar[ErrorCategory] = ErrorCategory.SIGNATURE


class DoudianAuthError(DoudianClientError):
    """token 失效/无权限：当次不可重试（client 内部允许强制刷 token 一次）。

    坑：`retryable` 是 **False**（见表）。"刷新 token 后重试一次"是 `doudian_client.call` 的
    策略，不是这个类属性；把这张表改成 True 会让调用方对"token 已失效"的请求无限重试。
    """

    category: ClassVar[ErrorCategory] = ErrorCategory.AUTH


class DoudianBusinessError(DoudianError):
    """业务错误（`err_no != 0` 且不属于上面任何一类）：该动作失败，run 继续。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.BUSINESS


class DoudianAmbiguousOutcomeError(DoudianError):
    """结果不确定（可变操作遇到 timeout/connection/5xx）：不重试，交对账（§9.3）。

    为什么不直接抛出原始的 `DoudianTransportError`/`DoudianUpstreamError`：那两个类型在调用
    方眼里是"可以重试的网络抖动"，而这一类是"**上游可能已经执行了**"。用一个独立类型把
    "不能重试"写进类型系统，`execute` 节点才能一眼区分"该重试"和"该交对账"（ADR-011）。

    坑：本类的 `retryable` 是 False，但它**不是**终态失败——它表示"结果待确认"。上层可以
    （也应当）把这个 action_log 落成 `unknown` 而不是 `failed`，两者的运维动作完全不同。
    """

    category: ClassVar[ErrorCategory] = ErrorCategory.AMBIGUOUS


#: 类别 -> 异常类。client 分类时只查这张表，不写 `except` 分支里挑类的逻辑。
#:
#: `CONFIG` / `UNKNOWN` 映射到基类 `DoudianError` 是**有意的兜底**：这两类不该由上游响应产生
#: （CONFIG 是装配期问题、UNKNOWN 是"没分类出来"），落到基类意味着"调用方只能按 category
#: 判断，不能靠异常类型分支"——所以 `except DoudianSignatureError` 这类精确捕获永远只匹配
#: 真正精确的那一类。
#:
#: 顺序/完整性要求：**每一个** `ErrorCategory` 都必须在这张表里。少一个会让
#: `error_for_category` 在运行期抛 `KeyError`（对端是一个真实的错误响应，却变成我们自己的
#: 500）。加新类别时这张表和 `RETRYABLE_BY_CATEGORY` 必须同时加。
ERROR_CLASS_BY_CATEGORY: dict[ErrorCategory, type[DoudianError]] = {
    ErrorCategory.TIMEOUT: DoudianTransportError,
    ErrorCategory.CONNECTION: DoudianTransportError,
    ErrorCategory.SERVER_5XX: DoudianUpstreamError,
    ErrorCategory.RATE_LIMITED: DoudianRateLimitedError,
    ErrorCategory.CLIENT_4XX: DoudianClientError,
    ErrorCategory.SIGNATURE: DoudianSignatureError,
    ErrorCategory.AUTH: DoudianAuthError,
    ErrorCategory.BUSINESS: DoudianBusinessError,
    ErrorCategory.AMBIGUOUS: DoudianAmbiguousOutcomeError,
    ErrorCategory.CONFIG: DoudianError,
    ErrorCategory.UNKNOWN: DoudianError,
}


def error_for_category(category: ErrorCategory, message: str, **kwargs: Any) -> DoudianError:
    """按类别构造异常（`retryable` 由表决定，调用点不传）。

    保证：返回的实例 `category` 就是入参、`retryable` 由 `RETRYABLE_BY_CATEGORY` 决定；
    调用点不需要（也不应该）再挑异常类。
    不保证：类别与 kwargs 的匹配（例如给 TIMEOUT 传 `retry_after`，它会被基类忽略吗？不会，
    `DoudianTransportError` 不接受该 kwarg，会 TypeError）。所以调用点必须知道自己在给哪一类
    加参数——`doudian_client._parse_response` 只在 429 分支传 `retry_after`。
    坑：这个函数**返回**异常而不抛（调用点写 `raise error_for_category(...)`）。统一成"返回"
    让调用点能自己决定 `from exc` 链不链因果。
    """
    return ERROR_CLASS_BY_CATEGORY[category](message, **kwargs)
