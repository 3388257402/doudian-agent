"""统一错误包络与异常映射（CONTRACT §7.1）。

`§7.1` 的三条规则落在这里，且只落在这里：

1. **所有 4xx/5xx 同一个包络形状**（`{"error": {code, message, details, trace_id}}`）；
2. **9 个错误码与 HTTP 状态一一对应**（映射表在 `app/errors.py` 的异常类上，本文件只负责渲染）；
3. **502 只在"上游（抖店）"问题时用，本地 bug 一律 500**。所以这里把异常分成两族：
   `AppError`（应用层，自带 code/http_status）与 `DoudianError`（上游族，一律 502）。
   其它任何异常 → 500 `INTERNAL_ERROR`，`message` 固定"内部错误"，细节只进日志与 `trace_id`。

为什么 500 的 `message` 不写异常原文：`str(exc)` 可能带 SQL、路径、token 片段。把"排障信息"
塞进给用户的响应，等于用响应体做信息泄露；它只该出现在日志里，由 `trace_id` 关联。

## 9 个错误码 → HTTP 的映射理由（映射值在 `app/errors.py` 的异常类上，这里只解释"为什么"）

| code | HTTP | 为什么是这个码 |
|---|---|---|
| `VALIDATION_ERROR` | 400 | 请求体/查询参数的形状、类型、枚举、必填字段不合契约。**请求方**能改，所以是 4xx 里最"近"的一个 |
| `THREAD_NOT_FOUND` | 404 | checkpointer 里没有这个 thread：从没跑过，或 checkpoint 被清理。资源不存在 |
| `APPROVAL_NOT_FOUND` | 404 | `interrupt_id` 与当前挂起点不一致（旧页面、已被别人批过）。**也是**资源不存在：那个挂起点确实没了 |
| `RESOURCE_NOT_FOUND` | 404 | 只读查询查不到数据（`/api/metrics` 该店铺该区间没有快照）。**空态不是 200** |
| `DUPLICATE_DECISION` | 409 | 同一批 action 已裁决过。与 `RUN_STATE_CONFLICT` 的区别是**更具体**：前端据此刷新列表即可 |
| `RUN_IN_PROGRESS` | 409 | 同一 `(shop_id, run_id)` 正在跑。冲突来自"并发"，客户端稍后重试是正解 |
| `RUN_STATE_CONFLICT` | 409 | thread 存在但当前不挂起、动作也没被裁决过。状态机允许不了这次操作 |
| `UPSTREAM_ERROR` | 502 | 上游（抖店）不可用/返回错误。**只有上游问题走 502**，本地 bug 一律 500 |
| `INTERNAL_ERROR` | 500 | 兜底：本地 bug、未捕获异常、形状自检失败。500 的 `message` 固定"内部错误" |

三条 4xx 的细分（`THREAD_NOT_FOUND` / `APPROVAL_NOT_FOUND` / `RESOURCE_NOT_FOUND`）都是 404
而不是 400：请求本身是合法的，"问的那个东西现在没有"。反过来 `DUPLICATE_DECISION` /
`RUN_STATE_CONFLICT` / `RUN_IN_PROGRESS` 都是 409：请求合法、资源也在，只是**现在这个状态
不允许这次操作**。前端按 `error.code` 分支，不按 HTTP 码猜。

**"502 只给上游、本地 bug 一律 500"这条的判据是异常族，不是消息内容**：`DoudianError` 一族
→ 502；`AppError` 一族 → 它自己声明的 `http_status`；其它一切 → 500。所以"上游返回了奇怪的
JSON，我们解析时崩了"是 500（那是我们的 bug），而"上游 503" 是 502。
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

#: 异常 → HTTP 包络的翻译只发生在本模块：路由层只抛 `AppError`/`DoudianError`，
#: 不自己拼 `JSONResponse`（否则响应头 `X-Trace-Id` 迟早漏掉某一处）。
#:
#: `INTERNAL_ERROR` 对外的固定文案（§7.1：细节只进日志与 trace_id）
#: 注意：500 的 `message` **只能**是这个常量，任何 handler 都不许把 `str(exc)` 塞进去。
INTERNAL_ERROR_MESSAGE = "内部错误"


def _current_trace_id() -> str:
    """当前请求的 trace id。

    正常情况下由 `TraceIdMiddleware` 写入 contextvar；极端路径（没经过中间件的直调，例如测试里
    直接 `handler(request, exc)`）下补一个，保证包络里的 `trace_id` 永远是**非空字符串**——
    前端拿空串去报障等于没有线索。

    保证：返回值非空（`get_trace_id()` 为空时当场生成一个新的）。
    不保证：补出来的那个 id 与日志里的 id 对得上——它只在这一条响应里存在（没经过中间件，
        日志里本来就没有这次请求的 trace）。
    调用方坑：不要缓存它的返回值；每次调用都重新读 contextvar（中间件会在请求边界还原）。
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

    保证：`code` 原样进包络（`str(code)`，`ApiErrorCode` 是 StrEnum）；`status_code` 由调用方定。
    不保证：不校验 `code` 的取值域——传个不认识的字符串也会被写出去（`ErrorDetail` 是 Literal，
        `model_dump` 不做运行期校验；真写错会在前端展示出未知码，而不是在这里炸）。
    调用方坑：这是唯一的包络出口，**不要**在路由里手拼 `JSONResponse`——响应头与包络的
        `trace_id` 必须同值，两处各写一次迟早会对不上。
    """
    resolved_trace = trace_id or _current_trace_id()
    # 只构造一次 `ErrorEnvelope`（而不是手拼 dict）：§7.1 的五个键由模型保证，
    # 形状错了在这里就炸，不会漏到线上。
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=str(code),  # type: ignore[arg-type]  # 取值域由调用方保证（ApiErrorCode）
            # `message` 非空由调用方保证：§7.1 要求它一定能让用户读懂"发生了什么"。
            message=message,
            # `details` 允许 None，但**键必须出现**（pydantic 的 `model_dump` 会输出 null）：
            # 前端按"有没有这个键"写逻辑时不该看到两种形状。
            details=details,
            trace_id=resolved_trace,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        # 响应头与包络**同值**：前端把两者任一展示给用户，不同值等于两条线索互相矛盾。
        headers={"X-Trace-Id": resolved_trace},
    )


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """`AppError` 族：`code`/`http_status` 由异常类自己声明（§5.0 映射，不在这里二次判断）。

    保证：状态码与 `code` **只从异常类读**（这里没有任何 `if status == ...` 的分支），
        因此"码表在三个地方各存一份"这件事在结构上不可能发生。
    不保证：不保证 `message` 对 500 是可读原文——`INTERNAL_ERROR` 一律替换成固定文案。
    调用方坑：`assert isinstance(exc, AppError)` 只是给类型检查器的；handler 是按异常类
        注册的，FastAPI 不会把别的族送到这里。
    """
    assert isinstance(exc, AppError)
    if exc.http_status >= 500:
        # 5xx 也属于"本地 bug"：日志要留栈，响应体不泄露原文。
        # 分界用 `>= 500` 而不是列举具体码：将来加一个 503 的子类也不必回来改这里。
        logger.error(
            "应用层 5xx：%s",
            exc.message,
            extra={
                "event": "api.error",
                "data": {
                    "code": exc.code.value,
                    "http_status": exc.http_status,
                    "message": exc.message,
                    # 日志里可以带 details：它是给运维看的，不进用户响应。
                    "details": exc.details,
                    "path": request.url.path,
                },
            },
        )
        return error_response(
            status_code=exc.http_status,
            code=exc.code,
            # 唯一的条件替换：`INTERNAL_ERROR` 的 message 必须是固定文案（防泄露）。
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
        # 4xx 原样透出 `message`：它由我们自己的代码写中文，本来就是给人读的
        # （不像 500 那样可能带 SQL/路径）。
        status_code=exc.http_status, code=exc.code, message=exc.message, details=exc.details
    )


async def _doudian_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """上游（抖店）异常 → 502 `UPSTREAM_ERROR`（§7.1：502 只给上游，本地 bug 不得走这条）。

    保证：这一族**全部**映射到 502，不看子类、不看 `category`（`category` 只进 `details`
        供排障）。所以"502 出现"在语义上等价于"上游有问题"。
    不保证：不保证 `message` 脱敏过——上游原文可能带 request_id/err_no（那是排障必需，
        且不是本服务的密钥）；真正的密钥在 `str(exc)` 里不会出现（`DoudianError` 不拼 token）。
    调用方坑：本 handler 只接 `DoudianError`；本地解析上游响应时抛的 `ValueError` 属于我们的
        bug，会落到 `_unhandled_handler` 变 500——那是对的，不要"为了统一"把它也变成 502。
    """
    assert isinstance(exc, DoudianError)
    logger.error(
        "上游错误：%s",
        str(exc),
        extra={
            "event": "api.upstream_error",
            "data": {
                # 日志里给全（category/retryable/err_no/request_id）：这是排障的唯一入口，
                # 响应体里只给前三个（`err_no` 属上游内部编号，对客户端没意义）。
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
        # 上游原文给人读（含 err_no/request_id 这类排障线索），但**不含**我们的密钥。
        message=str(exc),
        details={
            # `category` 与 `retryable` 进 details：客户端据此决定"要不要稍后重试"，
            # 这是 502 唯一可操作的信息。
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
            # `loc` 用点号连接：前端拿到 `decisions.0.edited_params` 就能定位到具体的格子，
            # 而不是只弹一句"参数不对"。
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
            # info 级：参数写错是**用户**的操作，不是服务故障（告警只看 error 级）。
            "event": "api.validation_error",
            "data": {"errors": errors, "path": request.url.path},
        },
    )
    first = errors[0] if errors else {"loc": "", "msg": "请求体不符合契约 §7"}
    # 兜底文案分支（`errors` 为空）只可能出现在"pydantic 抛了但没给任何条目"的极端情况，
    # 但它必须存在：否则这里会 IndexError，把一个 400 变成 500。
    # `summary` 只带第一条：9 个错误码里没有"多错"这个码，完整清单在 `details.errors`，
    # 摘要与清单的职责分开（否则 `message` 会变成十行长文本）。
    summary = f"{first['loc'] or 'body'}: {first['msg']}" if errors else first["msg"]
    return error_response(
        status_code=400,
        code=ApiErrorCode.VALIDATION_ERROR,
        # 400 一律用 `VALIDATION_ERROR`，不细分成"字段错/枚举错"：前端按 `details.errors`
        # 逐条标红，码只用来决定"要不要弹错误页"。
        message=summary,
        details={"errors": errors},
    )


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：本地 bug → 500 `INTERNAL_ERROR`（带栈进日志，响应体只给 trace_id）。

    保证：响应体形状与其它错误**完全一致**（同一包络、同一个 `X-Trace-Id`）；栈只进日志。
    不保证：不保证能区分"哪个模块崩的"——那是日志里 `api.unhandled` 事件的 `error_type`。
    调用方坑：`logger.exception` 必须在 handler 里调用（它靠 `sys.exc_info()` 拿栈，
        在别的上下文里调用会写 `NoneType: None`）。
    """
    trace_id = _current_trace_id()
    logger.exception(
        "未捕获异常：%s %s",
        request.method,
        request.url.path,
        # 显式给 trace_id：`logger.exception` 之外的字段要自己带上，才能和前端手里的 id 对上。
        extra={"event": "api.unhandled", "trace_id": trace_id,
               "data": {"error_type": type(exc).__name__, "path": request.url.path}},
    )
    return error_response(
        status_code=500,
        code=ApiErrorCode.INTERNAL_ERROR,
        # 固定文案，绝不 `str(exc)`：后者可能带 SQL、路径、token 片段（信息泄露）。
        message=INTERNAL_ERROR_MESSAGE,
        details=None,
        # 复用同一个 trace_id：日志里的那条与响应里的这条是同一次请求。
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
    # 映射到"最近的语义"而不是发明新码：前端只按 `error.code` 分支，
    # 多一个码就等于多一条前端不认识的分支（会被归到 MALFORMED_RESPONSE）。
    if status_code == 404:
        # 未知路由 / 资源不存在：语义最近的码就是 RESOURCE_NOT_FOUND。
        code = ApiErrorCode.RESOURCE_NOT_FOUND
    elif status_code >= 500:
        # 框架自己抛的 5xx（中间件/挂载的子应用）：按本地内部错误处理。
        code = ApiErrorCode.INTERNAL_ERROR
    else:
        # 405 / 415 / 其它 4xx：不发明新码，统一归 VALIDATION_ERROR（"这个请求不合规矩"）。
        code = ApiErrorCode.VALIDATION_ERROR
    detail = exc.detail if isinstance(exc.detail, str) else ""
    logger.info(
        "HTTP %s：%s",
        status_code,
        detail,
        extra={
            # 框架层的 4xx（未知路由/405）用 info：那是客户端请求了不存在的路径，不是故障。
            "event": "api.http_exception",
            "data": {"status": status_code, "path": request.url.path},
        },
    )
    return error_response(
        status_code=status_code,
        code=code,
        # `detail` 可能不是字符串（Starlette 允许 dict），那时退化成一行的 "HTTP 404"：
        # 不把任意对象塞进 `message` 字段。
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

    保证：五类 handler 覆盖"我们自己的异常 / 上游异常 / 参数校验 / 框架 HTTP 异常 / 一切其它"，
        没有漏网的出口；每个 handler 都走 :func:`error_response`（包络形状只有一处实现）。
    不保证：不保证 `code` 的语义足够细——9 个码是冻结的，边角状态码只能归到"最近的那个"。
    调用方坑：必须在 `app.include_router` **之前**调用（handler 是 app 级配置，与路由顺序无关，
        但要确保没有别的入口先注册了同名 handler）。
    """
    handlers: list[tuple[type[Exception], ExceptionHandler]] = [
        # 注册顺序不影响匹配：FastAPI/Starlette 按异常类的 MRO 找最近的 handler，
        # 所以 `ApprovalValidationError`（ValidationError 子类）也会落到 `_app_error_handler`。
        (AppError, _app_error_handler),
        (DoudianError, _doudian_error_handler),
        (RequestValidationError, _request_validation_handler),
        # `StarletteHTTPException` 必须注册：不注册时未知路由/405 会漏出 `{"detail": ...}`，
        # 违反"所有 4xx/5xx 统一包络"。
        (StarletteHTTPException, _http_exception_handler),
        # `Exception` 放最后（兜底）：未注册时 uvicorn 会返回纯文本/HTML，前端拿不到包络，
        # "本地 bug"与"网关返回错误页"就分不开了。
        (Exception, _unhandled_handler),
    ]
    for exc_class, handler in handlers:
        app.add_exception_handler(exc_class, handler)
