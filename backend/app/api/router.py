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
    """把 6 个端点收成一个 router（`/api` 前缀由调用方加）。"""
    router = APIRouter()
    for sub in (
        health_router,
        alerts_router,
        approve_router,
        run_router,
        metrics_router,
        logs_router,
    ):
        router.include_router(sub)
    return router
