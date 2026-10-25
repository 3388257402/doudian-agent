"""`X-Trace-Id` 中间件（§7：每个响应头带 `X-Trace-Id`；日志里也有）。

## 为什么是纯 ASGI 中间件，而不是 `@app.middleware("http")`

1. `BaseHTTPMiddleware` 会把下游放进**子任务**执行，异常/取消的语义要过一层 anyio；
   纯 ASGI 里"设置 contextvar → 调下游 → 在 finally 里还原"是一条直线。
2. 错误响应也必须带 `X-Trace-Id`：包络里的 `error.trace_id` 与响应头必须是**同一个值**
   （前端把两者任一展示给用户）。纯 ASGI 的 `send` 包装在"异常处理器已经生成响应"之后仍然生效。

## 传入的 trace id 会被复用

上游（前端/网关）已经带 `X-Trace-Id` 时沿用它：跨服务对账靠这个 id，重新生成等于丢线索。
只接受 `^[A-Za-z0-9_.-]{1,64}$`（防止把换行/超长内容写进日志与响应头）。
"""

from __future__ import annotations

import re

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.logging import new_trace_id, reset_trace_id, set_trace_id

__all__ = ["TRACE_HEADER", "TraceIdMiddleware"]

#: 响应/请求共用的头名（§7）
TRACE_HEADER = "X-Trace-Id"

_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _incoming_trace_id(scope: Scope) -> str | None:
    """读请求头里的 trace id；格式不符或没有 → `None`（由调用方生成新的）。

    保证：返回值若不为 `None`，一定匹配 `^[A-Za-z0-9_.-]{1,64}$`。
    不保证：不保证它是"我们发出去的"那个——外部可以是任意网关，沿用是**信任**上游的选择
        （跨服务对账需要它）。
    调用方坑：不合规的值被**丢弃**而不是报错：头注入/超长内容不该让请求失败，
        但更不能被写进日志（那是日志伪造）。
    """
    raw = Headers(scope=scope).get(TRACE_HEADER)
    if raw is None:
        return None
    candidate = raw.strip()
    # 只做"形状"过滤：不做大小写/前缀校验（trace id 的语义由上游定）。
    return candidate if _TRACE_ID_RE.match(candidate) else None


class TraceIdMiddleware:
    """给每个 HTTP 请求分配（或沿用）trace id，并写回响应头。

    为什么是**纯 ASGI** 而不是 `@app.middleware("http")`（即 `BaseHTTPMiddleware`）：
        1. `BaseHTTPMiddleware` 把下游放进**子任务**跑：contextvar 的传播要额外补一遍，补漏了就是
           "日志里 trace_id 是空、响应头里却有值"；
        2. 它多包一层 anyio，异常/取消会先变成任务结果再变回异常；而我们要保证的恰恰是
           "**异常处理器生成的 500 响应**也带头"——纯 ASGI 的 `send` 包装在那条路径上照样生效；
        3. 一条直线：设 contextvar → 调下游 → `finally` 还原。没有跨任务的状态需要解释。

    为什么 trace id 要"沿用来访值"：跨服务对账靠它，重新生成等于把上游的线索切掉。只接受
    `^[A-Za-z0-9_.-]{1,64}$`：否则换行/超长内容会被写进日志与响应头（头注入 + 日志伪造）。

    调用方坑：下游若自己 `set_trace_id()`，退出时我们只还原**自己的** token，不会把下游的值带出去；
    所以不要在中间件之外假设"响应头里的 id == 我刚设的 id"。
    """

    def __init__(self, app: ASGIApp) -> None:
        # 纯 ASGI 中间件只包一层 `app`：不构造请求对象、不解析 body（那才是
        # `BaseHTTPMiddleware` 的代价）。
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - 本应用没有 websocket/lifespan 走这里
            # 非 HTTP 流量原样放行：给 websocket/lifespan 加 trace 头没有意义。
            await self.app(scope, receive, send)
            return

        trace_id = _incoming_trace_id(scope) or new_trace_id()
        # 路由与异常处理器都通过 `get_trace_id()` 读它（同一 context 内传播）
        # `setdefault` 兼容 Starlette 不同版本对 `scope["state"]` 的初始化行为。
        scope.setdefault("state", {})
        scope["state"]["trace_id"] = trace_id
        token = set_trace_id(trace_id)

        async def send_with_trace(message: Message) -> None:
            # 只改 `http.response.start`（响应头只能在那一条消息里设置）。
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[TRACE_HEADER] = trace_id
            # 其它消息（`http.response.body`）原样透传：中间件不碰响应体。
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            # 必须还原：uvicorn 的 worker 复用 context，泄漏会让下一个请求继承上一个的 id。
            # `reset_trace_id(token)` 用的是"自己设的那个 token"，所以下游自己设过的值
            # 不会被这里误清（它只还原到本中间件进入之前的状态）。
            reset_trace_id(token)
