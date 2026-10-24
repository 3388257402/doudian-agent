"""应用层异常 + HTTP 映射 —— 逐字对应 docs/CONTRACT.md §5.0（唯一事实源）。

两套异常族**分开，不许合并**：

- ``app/services/errors.py`` —— **抖店上游**异常族（`DoudianError` 及其子类，带
  `category`/`retryable`），只在 client 与服务层内部使用。
- 本文件 —— **应用层**异常，直接对应 HTTP 错误码（契约 §7.1）。API 层只抛/映射这一族。

为什么必须分开：上游异常携带的是"上游怎么错了"（err_no、request_id、是否可重试），
应用异常携带的是"这次 HTTP 请求应该怎么回"（code + http_status）。合成一族的结果是
API 层被迫去判断 `err_no`，于是"业务错误码表"在三个地方各存一份。
"""

from __future__ import annotations

from typing import Any

from app.enums import ApiErrorCode


class AppError(Exception):
    """应用层异常基类：`code` 决定响应包络里的 `error.code`，`http_status` 决定状态码。"""

    code: ApiErrorCode
    http_status: int
    message: str
    details: dict[str, Any] | None = None

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(AppError):
    code = ApiErrorCode.VALIDATION_ERROR
    http_status = 400


class ThreadNotFound(AppError):
    code = ApiErrorCode.THREAD_NOT_FOUND
    http_status = 404


class ApprovalNotFound(AppError):
    code = ApiErrorCode.APPROVAL_NOT_FOUND
    http_status = 404


class ResourceNotFound(AppError):
    code = ApiErrorCode.RESOURCE_NOT_FOUND
    http_status = 404


class DuplicateDecision(AppError):
    code = ApiErrorCode.DUPLICATE_DECISION
    http_status = 409


class RunInProgress(AppError):
    code = ApiErrorCode.RUN_IN_PROGRESS
    http_status = 409


class RunStateConflict(AppError):
    code = ApiErrorCode.RUN_STATE_CONFLICT
    http_status = 409


class UpstreamError(AppError):
    code = ApiErrorCode.UPSTREAM_ERROR
    http_status = 502


class InternalError(AppError):
    code = ApiErrorCode.INTERNAL_ERROR
    http_status = 500


# 语义子类（都是 ValidationError 的子类，API 层统一按 400 处理）
class ApprovalValidationError(ValidationError):
    """恢复值不合规（§4.4 校验规则 1–5 任一失败）。"""


class ParamValidationError(ValidationError):
    """动作参数 schema 不符（§5.5 `validate_params`）。"""


class NoPendingInterrupt(ThreadNotFound):
    """`get_state` 无 interrupts —— 线程存在但没挂起审批（§4.4 末段：按 404 处理）。"""


class ConfigError(Exception):
    """启动期配置错误。不是 API 错误：进程必须退出（非 0），由 lifespan/入口捕获。

    刻意**不**继承 `AppError`：它不该被 HTTP exception handler 转成一个 4xx/5xx 响应，
    否则"配置坏了"会伪装成"这个请求有问题"，而进程还在带病服务后续请求。
    """


__all__ = [
    "AppError",
    "ApprovalNotFound",
    "ApprovalValidationError",
    "ConfigError",
    "DuplicateDecision",
    "InternalError",
    "NoPendingInterrupt",
    "ParamValidationError",
    "ResourceNotFound",
    "RunInProgress",
    "RunStateConflict",
    "ThreadNotFound",
    "UpstreamError",
    "ValidationError",
]
