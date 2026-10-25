"""入口层包：§7 的 6 个端点（5 个契约端点 + `/healthz`）、错误包络、trace 中间件。

只做 HTTP 语义：参数校验、状态码、错误码、响应形状。业务逻辑在 `app/agent/**` 与
`app/services/**`，本包不复制任何一条。

## 这个 `__init__` 为什么只做 re-export

`app.api` 被三处 import（`main`、测试、CLI）。在这里放任何逻辑都会让"导入一个名字"
产生副作用；而下面导出的五个名字恰好是"建 app 需要的东西"，一次导出省掉调用方
记路径的负担。
"""

from __future__ import annotations

from app.api.constants import API_VERSION
from app.api.deps import InFlightRuns
from app.api.errors import register_error_handlers
from app.api.middleware import TraceIdMiddleware
from app.api.router import build_api_router

# 这五个名字就是"建一个 app"要用的全部东西：版本号、并发闸门、错误处理器、
# trace 中间件、路由表。`__all__` 与它们逐字对应（`from app.api import *` 不会带出别的）。
__all__ = [
    # 与下面五个 import 逐字对应：多一个名字等于对外多承诺一个 API。
    "API_VERSION",
    "InFlightRuns",
    "TraceIdMiddleware",
    "build_api_router",
    "register_error_handlers",
]
