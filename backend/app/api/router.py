"""§7 的路由装配：6 个端点 → 一个 `APIRouter`（前缀由 `app.main` 按 `app.api_prefix` 加）。

为什么不在各路由模块里写死 `/api`：`settings.app.api_prefix` 是契约字段（§8.2），
挂前缀的地方只有一处（`create_app`），改前缀不需要动 6 个文件。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes_alerts import router as alerts_router
from app.api.routes_approve import router as approve_router
from app.api.routes_health import router as health_router
from app.api.routes_logs import router as logs_router
from app.api.routes_metrics import router as metrics_router
from app.api.routes_run import router as run_router

__all__ = ["build_api_router"]


def build_api_router() -> APIRouter:
    """把 6 个端点收成一个 router（`/api` 前缀由调用方加）。

    保证：返回的 router 含 6 个端点（healthz / alerts / approve / run / metrics / logs）；
        `/api` 前缀**不在**这里加。
    不保证：不注册错误处理器、不加中间件——那些是 `create_app` 的事。
    调用方坑：每次调用都新建一个 `APIRouter`（不复用模块级实例），所以同一个进程里
        建多个 app 不会共享路由表（测试并行时这一点很重要）。
    """
    router = APIRouter()
    # 顺序只影响 OpenAPI 文档里的分组顺序，不影响路由匹配（path 是唯一的）。
    # 每个子 router 自带 tag（health/alerts/approval/agent/metrics/logs），前端按 tag 分组。
    for sub in (
        # healthz 放第一个：OpenAPI 里它排在最前，运维/前端找起来最方便。
        health_router,
        alerts_router,
        approve_router,
        # run 与 approve 分属两个 tag，但都在这一个 router 上（前缀统一）。
        run_router,
        metrics_router,
        logs_router,
    ):
        router.include_router(sub)
    return router
