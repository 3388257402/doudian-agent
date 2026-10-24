"""`GET /api/healthz` —— 探活（CONTRACT §7.7）。

这个端点不是"装饰品"：它是 §10.4 全链路跑法的第一步（起没起来看它），也是部署时的
就绪判据。所以它必须真的探一次依赖，而不是恒返回 `{"status":"ok"}`：

- `db`：真发一条 `SELECT 1`（engine 已 dispose / 路径不可写 / 迁移没跑，都会在这里暴露）；
- `checkpointer`：lifespan 是否真的装配了持久化 saver（退回"每次请求临时开一个"意味着
  审批窗口不再是持久的，那是 HITL 的致命退化）。

任一不可用 → `503` + `{"status":"degraded","db":...,"detail":...}`（§7.7 的形状）。
`detail` 里带异常类型与原文：这是运维接口，藏起来只会让排障变成猜。
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent.checkpoint import get_checkpointer
from app.api.constants import API_VERSION
from app.db import arun_db
from app.observability.logging import utcnow_iso
from app.schemas import HealthzDegraded, HealthzResponse

__all__ = ["router"]

router = APIRouter(tags=["health"])


async def _db_probe() -> tuple[bool, str]:
    """真发一条查询。返回 `(ok, detail)`；失败时 detail 是"类型: 原文"。"""
    try:
        await arun_db(lambda session: session.execute(sa.text("SELECT 1")).scalar())
    except Exception as exc:  # noqa: BLE001 - 探活必须把任何失败变成 degraded，而不是 500
        return False, f"{type(exc).__name__}: {exc}"
    return True, "ok"


def _checkpointer_probe() -> tuple[bool, str]:
    saver = get_checkpointer()
    if saver is None:
        return False, "checkpointer 未装配：lifespan 没有登记持久化 saver"
    return True, "ok"


@router.get(
    "/healthz",
    response_model=HealthzResponse,
    summary="探活：db / checkpointer",
    responses={503: {"model": HealthzDegraded}},
)
async def healthz(request: Request) -> HealthzResponse | JSONResponse:
    """探活：真探 db 与 checkpointer，任一不可用返回 503 degraded。

    输入边界：无参数、无鉴权（部署的就绪探针要能直接打）；没有"部分健康"——依赖坏一个就是 degraded。

    错误：`503 {"status":"degraded","db":"error"|"ok","detail":"<异常类型: 原文>"}`（§7.7 逐字）。
          本端点**不会**返回 400/404/409/502，也不会返回 500：依赖坏了是"降级"，不是"这个请求有 bug"。

    调用方坑：
        - `200` 只说明"这一瞬间 db 与 checkpointer 都在"，不代表 run 能成功（抖店上游是外部的，不在探活范围）；
        - 探活会真发一条 `SELECT 1`：别塞进每请求一次的热路径（= 每请求一次库往返），轮询间隔按部署需要定；
        - `200` 与 `503` 的 body 形状**不同**（§7.7 就是这么写的）：用一个解析器强解两种会被 503 打成畸形；
        - `detail` 故意带异常原文（运维接口），不要把这段响应体直接回显给终端用户。
    """
    db_ok, db_detail = await _db_probe()
    cp_ok, cp_detail = _checkpointer_probe()
    if not db_ok or not cp_ok:
        degraded = HealthzDegraded(
            status="degraded",
            db="ok" if db_ok else "error",
            detail=db_detail if not db_ok else cp_detail,
        )
        # 503 的响应体形状与 200 不同（§7.7 逐字），所以直接返回 Response：
        # 声明 `responses={503: ...}` 让 OpenAPI 仍然是完整的。
        return JSONResponse(status_code=503, content=degraded.model_dump(mode="json"))
    return HealthzResponse(
        status="ok",
        version=API_VERSION,
        db="ok",
        checkpointer="ok",
        time=utcnow_iso(),
    )
