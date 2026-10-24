"""入口层包：§7 的 6 个端点（5 个契约端点 + `/healthz`）、错误包络、trace 中间件。

只做 HTTP 语义：参数校验、状态码、错误码、响应形状。业务逻辑在 `app/agent/**` 与
`app/services/**`，本包不复制任何一条。
"""

from __future__ import annotations

from app.api.constants import API_VERSION
from app.api.deps import InFlightRuns
from app.api.errors import register_error_handlers
from app.api.middleware import TraceIdMiddleware
from app.api.router import build_api_router

__all__ = [
    "API_VERSION",
    "InFlightRuns",
    "TraceIdMiddleware",
    "build_api_router",
    "register_error_handlers",
]
