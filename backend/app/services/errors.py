"""抖店开放平台异常族（CONTRACT §5.1.2，逐字实现）。

两条硬规则：

1. 与 `app/errors.py`（应用层异常，直接映射 HTTP）**分开**。本模块的异常只在
   client / 服务层内部流转，API 边界由 `UpstreamError` 统一包一次（§5.0 映射规则 1）。
2. `retryable` **不在调用点手写**。契约 §5.1.2 末段明确要求"必须由类别查表得出"，
   唯一事实源是 `RETRYABLE_BY_CATEGORY`（内容照抄 §9.1 表）。
   构造时若传入的 `retryable` 与表不符 → `ValueError`（把"手写错了"变成立刻可见的 bug，
   而不是一个静默的重试行为差异）。
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
        self.message = message
        resolved = category if category is not None else type(self).category
        self.category = resolved
        expected = RETRYABLE_BY_CATEGORY[resolved]
        if retryable is not None and retryable != expected:
            raise ValueError(
                f"retryable={retryable} 与类别 {resolved!r} 的分类表不符"
                f"（唯一事实源 CONTRACT §9.1，见 RETRYABLE_BY_CATEGORY）"
            )
        self.retryable = expected
        self.http_status = http_status
        self.err_no = err_no
        self.request_id = request_id
        self.raw = raw
        super().__init__(message)

    def __str__(self) -> str:
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
    """签名错误：部署事故，不重试、不刷 token、必须告警。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.SIGNATURE


class DoudianAuthError(DoudianClientError):
    """token 失效/无权限：当次不可重试（client 内部允许强制刷 token 一次）。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.AUTH


class DoudianBusinessError(DoudianError):
    """业务错误（`err_no != 0` 且不属于上面任何一类）：该动作失败，run 继续。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.BUSINESS


class DoudianAmbiguousOutcomeError(DoudianError):
    """结果不确定（可变操作遇到 timeout/connection/5xx）：不重试，交对账（§9.3）。"""

    category: ClassVar[ErrorCategory] = ErrorCategory.AMBIGUOUS


#: 类别 -> 异常类。client 分类时只查这张表，不写 `except` 分支里挑类的逻辑。
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
    """按类别构造异常（`retryable` 由表决定，调用点不传）。"""
    return ERROR_CLASS_BY_CATEGORY[category](message, **kwargs)
