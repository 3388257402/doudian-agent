"""`POST /api/agent/run` —— 触发一次 run（CONTRACT §7.2）。

三个必须由入口层负责的判断：

1. **`dry_run` 是进程级配置，不是单请求开关**。`AgentRuntime.client` 是进程单例（ADR-004：
   限流桶与并发信号量都挂在同一个 `httpx.AsyncClient` 上），请求体只能"确认"当前模式，
   不能"切换"它。请求值与进程配置不一致时直接 400 并说清怎么改——静默沿用进程配置会让
   "以为这次打的是真实上游、其实全是夹具"（反过来的情况更危险）。
2. **同一 `(shop_id, run_id)` 并发请求 → 409 `RUN_IN_PROGRESS`**（`app/api/deps.py` 的闸门）。
   `run_agent` 自己的幂等检查有竞态窗口，闸门必须挂在更外层。
3. **已终态的同一 run 回放 → 200（不是 202）**（§7.2 末段）：这次没有启动任何东西，
   只是把已存结果读回来，用 202 会骗前端"又有新的一轮在跑了"。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.agent import run_agent
from app.agent.state import new_run_id
from app.agent.tracing import get_run_trace
from app.api.convert import alert_items, effect_report
from app.api.deps import app_settings, inflight_runs
from app.api.queries import ACTIVE_RUN_STATUSES
from app.config import Settings
from app.errors import RunInProgress, ValidationError
from app.schemas import ErrorEnvelope, RunRequest, RunResponse

__all__ = ["router"]

router = APIRouter(tags=["agent"])

_ACTIVE = frozenset(ACTIVE_RUN_STATUSES)


def _reject_unreachable_dry_run(*, settings: Settings, requested: bool | None) -> None:
    """请求里的 `dry_run` 与进程配置不一致 → 400（附上"怎么改"的说明）。

    不静默沿用：上游模式决定了"这一次到底有没有真的改商家的价格"，它必须能被调用方确认，
    而"确认"的前提是请求值与实际行为一致。
    """
    if requested is None or requested == settings.doudian.dry_run:
        return
    raise ValidationError(
        "请求体的 dry_run 与进程配置不一致：上游客户端是进程单例（ADR-004），"
        "单次请求不能切换上游模式。要改变请修改 config/settings.yaml 的 doudian.dry_run"
        "（并配置/移除 DOUDIAN_APP_KEY/SECRET/ACCESS_TOKEN）后重启进程。",
        details={
            "requested_dry_run": requested,
            "process_dry_run": settings.doudian.dry_run,
        },
    )


@router.post(
    "/agent/run",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发一次采集→指标→异常→审批的 run",
    responses={
        200: {"model": RunResponse, "description": "同 (shop_id, run_id) 已终态：回放已存结果"},
        400: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        502: {"model": ErrorEnvelope},
    },
)
async def start_run(
    body: RunRequest, request: Request, response: Response
) -> RunResponse:
    """触发一次 run：采集 → 指标 → 异常 → 提议（需要人审批时在这里挂起）。

    输入边界：
        - `shop_id` 必填 `^[A-Za-z0-9_-]{1,64}$`；`run_id` 可选同字符集，缺省由服务端生成
          `run_{YYYYMMDDTHHMMSSZ}_{8hex}`（同一个 id 就是幂等键，别靠换 id 来"重试"）；
        - `trigger` ∈ `manual/scheduled/retry`：只进审计与日志，不改变执行路径；
        - `dry_run` 只允许"确认"进程配置（省略即取 `settings.doudian.dry_run`）：与进程值不一致直接 400。
          上游客户端是进程单例（ADR-004），单次请求切换会污染同进程里的其他请求。

    错误：
        400 VALIDATION_ERROR（字段形状/枚举外/未知键/`dry_run` 与进程不一致）
        409 RUN_IN_PROGRESS（同 `(shop_id, run_id)` 正在跑：本进程并发请求，或库里已有非终态行）
        502 UPSTREAM_ERROR（采集遇到**可重试**的上游错误且重试预算用尽；run 已被标记 failed）

    调用方坑：
        - `202` 只代表"这一轮跑到了挂起/结束"，不代表业务成功：必须读 `status`（可能是 `failed`）；
        - 上游**不可重试**的业务错误（参数错/无权限）不会报 502，而是让本轮以 `202 + status=failed`
          结束——这类错误重试一百次也一样，别把它当"服务不可用"去重试；
        - 同一 `run_id` 在终态后再 POST 返回 `200`（回放已存结果，不重跑图、不再打上游）；想真的再跑一次
          必须换 `run_id`；
        - 客户端超时 ≠ run 失败：请求断了图可能还在跑，重发会撞 409；等 409 消失后去 `/api/alerts` 看结果；
        - `pending_approvals[].interrupt_id` 是审批的唯一凭据：本端点不会在 409 时补发它，丢了就去
          `/api/alerts` 重新取。
    """
    settings = app_settings(request)
    _reject_unreachable_dry_run(settings=settings, requested=body.dry_run)
    run_id = body.run_id or new_run_id()

    async with inflight_runs(request).hold(shop_id=body.shop_id, run_id=run_id):
        existing = await get_run_trace(shop_id=body.shop_id, run_id=run_id)
        replaying = existing is not None and str(existing["status"]) not in _ACTIVE
        if existing is not None and not replaying:
            raise RunInProgress(
                f"run 正在执行中：{body.shop_id}:{run_id}",
                details={
                    "shop_id": body.shop_id,
                    "run_id": run_id,
                    "status": str(existing["status"]),
                },
            )
        result = await run_agent(body.shop_id, run_id, trigger=body.trigger)

    if replaying:
        response.status_code = status.HTTP_200_OK
    return RunResponse(
        run_id=result.run_id,
        thread_id=result.thread_id,
        trace_id=result.trace_id,
        status=result.status,  # type: ignore[arg-type]
        pending_approvals=alert_items(result.pending_approvals),
        effect_report=effect_report(result.effect_report),
    )
