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
    """应用层异常基类：`code` 决定响应包络里的 `error.code`，`http_status` 决定状态码。

    保证：每个子类都**声明**了 `code` 与 `http_status`（类属性，不是实例上算出来的），
        所以 `api/errors.py` 的 handler 里没有任何状态码判断分支。
    不保证：不保证 `message` 适合直接给终端用户——`InternalError` 的 message 会被替换成
        固定文案"内部错误"（防止把 SQL/路径泄进响应体）。
    调用方坑：`details` 是**可选**的 dict，但它会原样进响应体：只放"给前端做判断用"的
        结构化信息（字段名、id），不要放栈或原始上游响应。
    """

    code: ApiErrorCode
    http_status: int
    message: str
    details: dict[str, Any] | None = None

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        """`message` 位置参数（便于 `raise XxxError("...")`），`details` 必须关键字传。

        保证：`self.message` 与 `str(exc)` 同值（`super().__init__(message)`）。
        不保证：不做任何脱敏/转义——调用方负责别把密钥拼进 message。
        调用方坑：`details` 是关键字专用（`*`），位置传第二个参数会 TypeError。
        """
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(AppError):
    """请求体/查询参数不合契约 → **400**。

    为什么是 400：形状/枚举/必填字段由请求方决定，改一下请求就能过——这是 4xx 里最"近"的码。
    """

    code = ApiErrorCode.VALIDATION_ERROR
    http_status = 400


#: 从码到状态码的映射只在这些类属性上：`api/errors.py` 的 handler **不**做二次判断，
#: 所以"改一个状态码"永远是一次单点修改（这也是 9 个码能保持一一对应的机制保证）。


class ThreadNotFound(AppError):
    """checkpointer 里没有这个 thread → **404**。

    为什么是 404：请求本身合法，但"问的那个 thread"不存在（从没跑过，或 checkpoint 被清理）。
    注意它与 `RunStateConflict` 的区别：这里是"没有"，那里是"有但不允许这次操作"。
    """

    code = ApiErrorCode.THREAD_NOT_FOUND
    http_status = 404


class ApprovalNotFound(AppError):
    """`interrupt_id` 与当前挂起点不一致 → **404**。

    为什么也是 404：那个挂起点**确实不存在**了（已被处理/已过期/是别的挂起点）。
    这道码挡住的是"拿旧审批页面点第二次"——它是防重放闸门的对外表现。
    """

    code = ApiErrorCode.APPROVAL_NOT_FOUND
    http_status = 404


class ResourceNotFound(AppError):
    """只读查询查不到数据 → **404**。

    为什么不是 200+空数组：`/api/metrics` 的"没有数据"必须与"有数据但都是 0"区分开
    （§7.5 明确要求 404）。空态是**事实**，不该伪装成"查到了 0 条"。
    """

    code = ApiErrorCode.RESOURCE_NOT_FOUND
    http_status = 404


class DuplicateDecision(AppError):
    """同一批 action 已被裁决过 → **409**。

    为什么是 409 而不是 400：请求合法、资源也在，只是**状态**不允许再来一次。
    它比 `RunStateConflict` 更具体（API 层据此让前端"刷新列表"而不是"看状态"）。
    """

    code = ApiErrorCode.DUPLICATE_DECISION
    http_status = 409


class RunInProgress(AppError):
    """同一 `(shop_id, run_id)` 正在跑 → **409**。

    为什么是 409：并发冲突，客户端稍后重试是正解（不是"请求写错了"）。
    它也用于"库里已有非终态行"（崩溃遗留），此时运维需要看 status 才能决定怎么收尾。
    """

    code = ApiErrorCode.RUN_IN_PROGRESS
    http_status = 409


class RunStateConflict(AppError):
    """thread 存在但当前不挂起、动作也没被裁决过 → **409**。

    为什么是 409：状态机不允许这次操作（比如"没在等人审批，却来提交裁决"）。
    """

    code = ApiErrorCode.RUN_STATE_CONFLICT
    http_status = 409


class UpstreamError(AppError):
    """上游（抖店）不可用/返回错误 → **502**。

    为什么是 502 而不是 500：故障在**上游**，不是我们算错了。这条区分决定运维该看哪边的日志。
    注意：实际的抖店异常走 `api/errors.py` 的 `_doudian_error_handler`（直接映射 502）；
    本类用于"应用层想把某个失败表达成上游问题"的少数场景。
    """

    code = ApiErrorCode.UPSTREAM_ERROR
    http_status = 502


class InternalError(AppError):
    """本地 bug / 未捕获异常 → **500**。

    为什么 message 会被替换：500 的 `str(exc)` 可能带 SQL、路径、token 片段。响应体里只留
    "内部错误" + `trace_id`，细节进日志——这是 §7.1 的信息泄露边界。
    """

    code = ApiErrorCode.INTERNAL_ERROR
    http_status = 500


#: `ConfigError` 是这条分界线的唯一例外：它**不是** HTTP 错误，而是"进程不该起来"。
#: 把它混进上面那张表，就等于让"配置坏了"变成一个可以被 API 层吞掉的响应。


# 语义子类（都是 ValidationError 的子类，API 层统一按 400 处理）
# 它们**不**新增状态码：只用来让 `except` 与日志能区分"哪一类校验失败"。
# 例：`resume_agent` 抛 `ApprovalValidationError`，而 `validate_params` 抛
# `ParamValidationError`——两者对外都是 400，但日志里的可读原因不同。
class ApprovalValidationError(ValidationError):
    """恢复值不合规（§4.4 校验规则 1–5 任一失败）。"""


class ParamValidationError(ValidationError):
    """动作参数 schema 不符（§5.5 `validate_params`）。"""


class NoPendingInterrupt(ThreadNotFound):
    """`get_state` 无 interrupts —— 线程存在但没挂起审批（§4.4 末段：按 404 处理）。

    注意它继承的是 `ThreadNotFound`（404）而不是 `RunStateConflict`（409）：契约 §4.4
    末段就是这么定的——"没有可恢复的挂起点"按"资源不存在"处理。
    """


class ConfigError(Exception):
    """启动期配置错误。不是 API 错误：进程必须退出（非 0），由 lifespan/入口捕获。

    刻意**不**继承 `AppError`：它不该被 HTTP exception handler 转成一个 4xx/5xx 响应，
    否则"配置坏了"会伪装成"这个请求有问题"，而进程还在带病服务后续请求。

    保证：只在启动期被抛（`load_settings` / `build_settings` / lifespan / `checkpoint_saver`）。
    不保证：不保证能在一个已经跑起来的进程里被安全处理——它表示"这份配置不能用"。
    调用方坑：不要给它加 `code`/`http_status`，也不要注册它的 handler；
        它唯一的正确处理方式是"让进程起不来"。
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
