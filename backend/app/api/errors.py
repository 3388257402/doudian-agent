"""统一错误包络与异常映射（CONTRACT §7.1）。

`§7.1` 的三条规则落在这里，且只落在这里：

1. **所有 4xx/5xx 同一个包络形状**（`{"error": {code, message, details, trace_id}}`）；
2. **9 个错误码与 HTTP 状态一一对应**（映射表在 `app/errors.py` 的异常类上，本文件只负责渲染）；
3. **502 只在"上游（抖店）"问题时用，本地 bug 一律 500**。所以这里把异常分成两族：
   `AppError`（应用层，自带 code/http_status）与 `DoudianError`（上游族，一律 502）。
   其它任何异常 → 500 `INTERNAL_ERROR`，`message` 固定"内部错误"，细节只进日志与 `trace_id`。

为什么 500 的 `message` 不写异常原文：`str(exc)` 可能带 SQL、路径、token 片段。把"排障信息"
塞进给用户的响应，等于用响应体做信息泄露；它只该出现在日志里，由 `trace_id` 关联。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ExceptionHandler

from app.enums import ApiErrorCode
from app.errors import AppError
from app.observability.logging import get_logger, get_trace_id, new_trace_id
from app.schemas import ErrorDetail, ErrorEnvelope
from app.services.errors import DoudianError

__all__ = ["register_error_handlers", "INTERNAL_ERROR_MESSAGE", "error_response"]

logger = get_logger("app.api.errors")

#: `INTERNAL_ERROR` 对外的固定文案（§7.1：细节只进日志与 trace_id）
INTERNAL_ERROR_MESSAGE = "内部错误"


def _current_trace_id() -> str:
    """当前请求的 trace id。

    正常情况下由 `TraceIdMiddleware` 写入 contextvar；极端路径（没经过中间件的直调，例如测试里
    直接 `handler(request, exc)`）下补一个，保证包络里的 `trace_id` 永远是**非空字符串**——
    前端拿空串去报障等于没有线索。
    """
    return get_trace_id() or new_trace_id()


def error_response(
    *,
    status_code: int,
    code: ApiErrorCode | str,
    message: str,
    details: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    """构造 §7.1 包络响应（响应头同步带 `X-Trace-Id`，与 `error.trace_id` 同一个值）。

    为什么只有这一个出口：包络的 5 条约束（形状固定、`code` ∈ 9 码、`message` 非空、
    `details` 可空但键必须出现、`trace_id` 与响应头**同值**）如果让每个 handler 各写一遍，
    迟早会有某一个 handler 忘了同步响应头——前端拿包络里的 id 去查日志，后端按响应头查，两边对不上。
    调用方坑：显式传入 `trace_id` 时**响应头与包络都用它**（不要只改其中一个）。
    """
    resolved_trace = trace_id or _current_trace_id()
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=str(code),  # type: ignore[arg-type]  # 取值域由调用方保证（ApiErrorCode）
            message=message,
            details=details,
            trace_id=resolved_trace,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"X-Trace-Id": resolved_trace},
    )


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """`AppError` 族：`code`/`http_status` 由异常类自己声明（§5.0 映射，不在这里二次判断）。"""
    assert isinstance(exc, AppError)
    if exc.http_status >= 500:
        # 5xx 也属于"本地 bug"：日志要留栈，响应体不泄露原文。
        logger.error(
            "应用层 5xx：%s",
            exc.message,
            extra={
                "event": "api.error",
                "data": {
                    "code": exc.code.value,
                    "http_status": exc.http_status,
                    "message": exc.message,
                    "details": exc.details,
                    "path": request.url.path,
                },
            },
        )
        return error_response(
            status_code=exc.http_status,
            code=exc.code,
            message=exc.message if exc.code is not ApiErrorCode.INTERNAL_ERROR else INTERNAL_ERROR_MESSAGE,
            details=exc.details,
        )
    logger.warning(
        "请求被拒绝：%s",
        exc.message,
        extra={
            "event": "api.error",
            "data": {
                "code": exc.code.value,
                "http_status": exc.http_status,
                "details": exc.details,
                "path": request.url.path,
            },
        },
    )
    return error_response(
        status_code=exc.http_status, code=exc.code, message=exc.message, details=exc.details
    )


async def _doudian_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """上游（抖店）异常 → 502 `UPSTREAM_ERROR`（§7.1：502 只给上游，本地 bug 不得走这条）。"""
    assert isinstance(exc, DoudianError)
    logger.error(
        "上游错误：%s",
        str(exc),
        extra={
            "event": "api.upstream_error",
            "data": {
                "category": str(exc.category),
                "retryable": bool(exc.retryable),
                "err_no": exc.err_no,
                "request_id": exc.request_id,
                "path": request.url.path,
            },
        },
    )
    return error_response(
        status_code=502,
        code=ApiErrorCode.UPSTREAM_ERROR,
        message=str(exc),
        details={
            "category": str(exc.category),
            "retryable": bool(exc.retryable),
            "request_id": exc.request_id,
        },
    )


async def _request_validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """请求体/查询参数校验失败 → 400 `VALIDATION_ERROR`（`details` 给前端标红用）。

    为什么 `details` 用 `{"errors": [{loc,msg,type}, ...]}` 这个形状：`loc` 点到具体字段
    （`decisions.0.edited_params`），前端据此标红**那一格**而不是只弹一句"参数不对"；
    只给 `message` 会让用户不知道该改哪个字段。
    为什么 `message` 只取第一条：9 个错误码里没有"多错"这个码，前端按 `details` 展示全部、
    按 `message` 展示摘要——两者职责分开，不把十行长文本塞进一个字段。
    """
    assert isinstance(exc, RequestValidationError)
    errors = [
        {
            "loc": ".".join(str(part) for part in item.get("loc", ())),
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        for item in exc.errors()
    ]
    logger.info(
        "请求校验失败（%d 处）",
        len(errors),
        extra={
            "event": "api.validation_error",
            "data": {"errors": errors, "path": request.url.path},
        },
    )
    first = errors[0] if errors else {"loc": "", "msg": "请求体不符合契约 §7"}
    summary = f"{first['loc'] or 'body'}: {first['msg']}" if errors else first["msg"]
    return error_response(
        status_code=400,
        code=ApiErrorCode.VALIDATION_ERROR,
        message=summary,
        details={"errors": errors},
    )


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：本地 bug → 500 `INTERNAL_ERROR`（带栈进日志，响应体只给 trace_id）。"""
    trace_id = _current_trace_id()
    logger.exception(
        "未捕获异常：%s %s",
        request.method,
        request.url.path,
        extra={"event": "api.unhandled", "trace_id": trace_id,
               "data": {"error_type": type(exc).__name__, "path": request.url.path}},
    )
    return error_response(
        status_code=500,
        code=ApiErrorCode.INTERNAL_ERROR,
        message=INTERNAL_ERROR_MESSAGE,
        details=None,
        trace_id=trace_id,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Starlette 层的 `HTTPException`（未知路由 404、方法不允许 405、415 等）也走同一包络。

    §7.1 只定义了 9 个错误码，而框架还会产生表外的状态码。这里不发明新码，取**最近的语义**：
    404 → `RESOURCE_NOT_FOUND`，其余 4xx → `VALIDATION_ERROR`，5xx → `INTERNAL_ERROR`。
    形状统一比"给每个边角状态码编一个新码"重要：前端只按 `error.code` 分支，
    一旦某条路径漏出 `{"detail": ...}`，前端只能归到 `MALFORMED_RESPONSE`。
    """
    assert isinstance(exc, StarletteHTTPException)
    status_code = int(exc.status_code)
    if status_code == 404:
        code = ApiErrorCode.RESOURCE_NOT_FOUND
    elif status_code >= 500:
        code = ApiErrorCode.INTERNAL_ERROR
    else:
        code = ApiErrorCode.VALIDATION_ERROR
    detail = exc.detail if isinstance(exc.detail, str) else ""
    logger.info(
        "HTTP %s：%s",
        status_code,
        detail,
        extra={
            "event": "api.http_exception",
            "data": {"status": status_code, "path": request.url.path},
        },
    )
    return error_response(
        status_code=status_code,
        code=code,
        message=detail or f"HTTP {status_code}",
        details={"path": request.url.path, "method": request.method},
    )


def register_error_handlers(app: FastAPI) -> None:
    """把 §7.1 的五类 handler 挂到应用上（顺序无关：FastAPI 按异常的具体类做 MRO 匹配）。

    为什么集中注册在入口层：错误包络是 **HTTP 语义**，只属于入口层。`AppError` 只声明"我是哪一类错"
    （`code`/`http_status`），`DoudianError` 只声明"上游怎么错了"（`category`/`retryable`）——
    把它们翻译成状态码与响应体，只在这里发生。否则同一张码表会在路由、中间件、CLI 各存一份，
    而且没人知道哪份是权威。
    为什么 `Exception` 也必须注册：不注册时未捕获异常会被 uvicorn 以纯文本/HTML 返回，
    前端拿不到包络、只能归到 `MALFORMED_RESPONSE`——"本地 bug"和"网关返回错误页"就分不开了。
    调用方坑：`StarletteHTTPException` 也要注册（未知路由/405），否则框架自己会漏出 `{"detail": ...}`，
    违反"所有 4xx/5xx 统一包络"。
    """
    handlers: list[tuple[type[Exception], ExceptionHandler]] = [
        (AppError, _app_error_handler),
        (DoudianError, _doudian_error_handler),
        (RequestValidationError, _request_validation_handler),
        (StarletteHTTPException, _http_exception_handler),
        (Exception, _unhandled_handler),
    ]
    for exc_class, handler in handlers:
        app.add_exception_handler(exc_class, handler)
