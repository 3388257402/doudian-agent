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

三条判断都在**进入 `run_agent` 之前**：图一跑起来就有副作用（采集/改价），
"先判断再跑"是唯一能避免副作用的顺序。
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

#: 触发端点的路由器（`/api` 前缀由 `app.main` 统一加，模块里不写死）。
#: 注意它**不带** `/agent` 前缀：路径是 `@router.post("/agent/run")` 的完整相对路径。
_ACTIVE = frozenset(ACTIVE_RUN_STATUSES)
# 与 `queries.ACTIVE_RUN_STATUSES` 同集合：这里只用于"是不是回放"的判断
# （非终态 = 不能回放、只能 409），与 `run_agent` 内部那份各自独立。


def _reject_unreachable_dry_run(*, settings: Settings, requested: bool | None) -> None:
    """请求里的 `dry_run` 与进程配置不一致 → 400（附上"怎么改"的说明）。

    不静默沿用：上游模式决定了"这一次到底有没有真的改商家的价格"，它必须能被调用方确认，
    而"确认"的前提是请求值与实际行为一致。

    保证：`requested` 为 `None`（省略）或与进程一致时返回（不抛）。
    不保证：不改变进程配置——它只**拒绝**不一致的请求。
    调用方坑：这条校验必须在打开 `inflight_runs` 闸门**之前**做：400 不该占用并发名额。
    """
    if requested is None or requested == settings.doudian.dry_run:
        # 省略 = "我接受进程当前模式"；相等 = "我确认过了"。两种都放行。
        return
    # 消息里必须写清"怎么改"（改 yaml + 配好三个环境变量 + 重启）：
    # 只说"不一致"会让人反复重试同一个请求。
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
    # 默认 202（已接受）：这一轮可能只是挂起，也可能已失败——202 不承诺业务成功。
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发一次采集→指标→异常→审批的 run",
    responses={
        # 200 是"回放"分支，与 202 的区别见模块 docstring 第 3 条。
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

    注意"缺省 `run_id`"的含义：每次请求都会生成**新的** run，所以"重试"必须复用同一个
    `run_id`——换 id 等于新开一轮采集，不是重试那个失败的。

    错误：
        400 VALIDATION_ERROR（字段形状/枚举外/未知键/`dry_run` 与进程不一致）
        409 RUN_IN_PROGRESS（同 `(shop_id, run_id)` 正在跑：本进程并发请求，或库里已有非终态行）
        502 UPSTREAM_ERROR（采集遇到**可重试**的上游错误且重试预算用尽；run 已被标记 failed）

    这三条错误的处置方向完全不同：400 改请求、409 等一会儿、502 看上游——所以码必须分开。

    调用方坑：
        - `202` 只代表"这一轮跑到了挂起/结束"，不代表业务成功：必须读 `status`（可能是 `failed`）；
        - 上游**不可重试**的业务错误（参数错/无权限）不会报 502，而是让本轮以 `202 + status=failed`
          结束——这类错误重试一百次也一样，别把它当"服务不可用"去重试；
        - 同一 `run_id` 在终态后再 POST 返回 `200`（回放已存结果，不重跑图、不再打上游）；想真的再跑一次
          必须换 `run_id`；
        - 客户端超时 ≠ run 失败：请求断了图可能还在跑，重发会撞 409；等 409 消失后去 `/api/alerts` 看结果；
        - `pending_approvals[].interrupt_id` 是审批的唯一凭据：本端点不会在 409 时补发它，丢了就去
          `/api/alerts` 重新取。

    保证：只要返回 202/200，`run_id`/`thread_id`/`trace_id` 三个身份都非空且互相对应。
    不保证：不保证 `dry_run` 与实际行为一致地"被遵守"——它只做校验，真正的模式在进程配置里。
    """
    settings = app_settings(request)
    # `response` 是 FastAPI 注入的：回放分支要靠它把装饰器上的默认 202 改成 200
    # （函数内改 status_code 是 FastAPI 支持的覆盖方式）。
    # 1) 先挡 dry_run 不一致：这条与并发无关，先判它给出的错误消息最有用（告诉人怎么改配置）。
    _reject_unreachable_dry_run(settings=settings, requested=body.dry_run)
    run_id = body.run_id or new_run_id()

    # 2) 进程内闸门：`hold` 是 async context manager，异常路径也会在 finally 里释放
    #    （否则一次 502 就把这个 run_id 永久锁死）。多 worker 部署时它挡不住跨进程并发。
    async with inflight_runs(request).hold(shop_id=body.shop_id, run_id=run_id):
        # 闸门内再查一次库：库里已有非终态行（例如上一个进程崩了）同样要 409。
        existing = await get_run_trace(shop_id=body.shop_id, run_id=run_id)
        # 只有"行存在且已终态"才算回放；非终态一律 409（下面那段）。
        replaying = existing is not None and str(existing["status"]) not in _ACTIVE
        if existing is not None and not replaying:
            raise RunInProgress(
                f"run 正在执行中：{body.shop_id}:{run_id}",
                details={
                    # details 里给结构化信息（前端可用），不是一句自然语言。
                    "shop_id": body.shop_id,
                    "run_id": run_id,
                    "status": str(existing["status"]),
                },
            )
        result = await run_agent(body.shop_id, run_id, trigger=body.trigger)
        # 闸门在 `async with` 退出时释放：无论这一轮成功、失败还是挂起，`run_id` 都不会被锁死。
    # 走完闸门再上调响应码：回放判断在闸门内做，但改状态码放在闸门之后，
    # 保证"失败路径不会先改码再抛异常"。
    if replaying:
        # 回放：这次什么都没启动，所以把装饰器上的默认 202 改成 200（§7.2 末段）。
        response.status_code = status.HTTP_200_OK
    return RunResponse(
        run_id=result.run_id,
        thread_id=result.thread_id,
        trace_id=result.trace_id,
        status=result.status,  # type: ignore[arg-type]
        # 两个转换都在入口层（`convert.*`）：领域 dataclass → §7 的 pydantic 模型，
        # 时间形状在这里统一成 ISO-Z，不散落到各路由。
        pending_approvals=alert_items(result.pending_approvals),
        effect_report=effect_report(result.effect_report),
    )
