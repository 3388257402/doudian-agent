"""`GET /api/metrics` —— 店铺级指标序列（CONTRACT §7.5）。

## 数据源（契约留下了口子，这里必须把它说清楚）

§7.5 自己写明：**四张表里没有指标表**（§6.1），"历史指标从哪来"此前没有定义，所以不允许
凭空造一张表或补零。当前**唯一**已存在的指标事实源是 LangGraph 的 checkpoint：
`compute_metrics` 把当日 `metrics` 与近 8 天 `history`（每一条都带自己的 7 日均值）写进
state（§3.3），`/api/metrics` 就把这些状态读出来，按 `date` 合并去重。

因此：

- **没有数据的店铺 / 区间 → 404 `RESOURCE_NOT_FOUND`**（§7.5），不是 200 + 空数组；
- **缺的日期不补零**（§7.5 末段）：读不到就是读不到，前端自己决定怎么画；
- 数据只覆盖"跑过 run 的那些业务日"。要真正的历史趋势，需要先做"指标持久化"决策
  （新表或 JSONL 汇 + 契约变更 + 一条 ADR），不在本端点里偷偷发明。

## 为什么读 checkpoint 是有界的

只读该店铺最近 `_MAX_RUNS` 个 run 的状态（按 `started_at` 升序，后写的覆盖先写的），
不扫全表、不做 N+1 展开成"每个 SKU 一次调用"。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Query

from app.agent.checkpoint import resolve_checkpointer
from app.agent.graph import build_graph
from app.agent.state import business_date
from app.api import queries
from app.config import get_settings
from app.errors import ResourceNotFound, ValidationError
from app.schemas import SHOP_ID_PATTERN, ErrorEnvelope, MetricPoint, MetricsResponse

__all__ = ["router"]

router = APIRouter(tags=["metrics"])

#: 参与合并的 run 上限（dev 规模足够；生产该换成指标持久化，见模块 docstring）
_MAX_RUNS = 200

#: 默认窗口长度：今天往前 6 天 + 今天 = 7 个业务日（§7.5）
_DEFAULT_WINDOW_DAYS = 6


def _parse_day(value: str, *, field: str) -> str:
    """`YYYY-MM-DD` 校验（§7.5 的 `date_from` / `date_to`）。"""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError(
            f"{field} 必须是 YYYY-MM-DD（Asia/Shanghai 的业务日期）",
            details={field: value},
        ) from exc


def _shift(day: str, offset_days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=offset_days)).isoformat()


async def _points_by_date(*, shop_id: str) -> dict[str, dict[str, Any]]:
    """该店铺所有 run 里出现过的指标快照（`date -> MetricsSnapshot`，后写的覆盖先写的）。"""
    rows = await queries.list_run_rows(shop_id=shop_id, limit=_MAX_RUNS)
    if not rows:
        return {}

    settings = get_settings()
    collected: dict[str, dict[str, Any]] = {}
    async with resolve_checkpointer(settings=settings) as saver:
        graph = build_graph(saver)
        for row in rows:  # 升序：新 run 的同日指标覆盖旧的
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": row["thread_id"]}}
            )
            values = getattr(snapshot, "values", None) or {}
            candidates: list[Any] = list(values.get("history") or [])
            if isinstance(values.get("metrics"), Mapping):
                candidates.append(values["metrics"])
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                day = str(candidate.get("date") or "")
                if not day:
                    continue
                collected[day] = dict(candidate)
    return collected


@router.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="店铺级日粒度指标（金额单位：分）",
    responses={400: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}},
)
async def get_metrics(
    shop_id: str = Query(pattern=SHOP_ID_PATTERN),
    date_from: str | None = Query(default=None, description="YYYY-MM-DD，默认今天-6"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD，默认今天（含端点）"),
    granularity: Literal["day"] = Query(default="day", description="仅支持 day"),
) -> MetricsResponse:
    """店铺级日粒度指标序列（金额一律整数分）。

    输入边界：
        - `shop_id` 必填 `^[A-Za-z0-9_-]{1,64}$`；
        - `date_from`/`date_to` 可选 `YYYY-MM-DD`（Asia/Shanghai 业务日），默认"今天-6 .. 今天"，含端点；
          `date_from > date_to` 400；`granularity` 只支持 `day`（周/月聚合不在契约里）；
        - `points` 只包含**跑过 run 的日期**：缺的日期不补零（补零会把"没数据"伪装成"GMV=0"）。

    错误：
        400 VALIDATION_ERROR（日期格式/区间倒置/granularity 越界/shop_id 字符集）
        404 RESOURCE_NOT_FOUND（该店铺在该区间内没有任何指标快照——空态，不是 `200 + points=[]`）

    调用方坑：
        - 数据源是各 run 的 checkpoint state（§7.5 明确没有指标表）：**同一天以最新一次 run 为准**
          （旧 run 的同日值被覆盖，旧 run 独有的日期仍保留）。所以这不是财务口径的历史账，别拿它对账；
        - `refund_rate`/`cvr`/`uv`/`gmv_7d_avg_cents` 为 `null` = "算不出来"（分母缺失/历史不足/上游没给），
          与 `0` 是**两个**业务事实；前端禁止 `?? 0` 兜底（"转化率 0%" 会让人做出错误决策）；
        - `uv` 是否有值取决于 `collect` 能否确认上游 `summary` 就是当天（§5.3 作用域规则）：
          拿不到就是 `null`，不会摊派、不会编默认值；
        - 默认区间按**服务端时钟**算：跨零点前后请求同一区间会得到不同结果，要稳定就显式传两端日期。
    """
    today = business_date()
    resolved_to = _parse_day(date_to or today, field="date_to")
    resolved_from = _parse_day(
        date_from or _shift(today, -_DEFAULT_WINDOW_DAYS), field="date_from"
    )
    if resolved_from > resolved_to:
        raise ValidationError(
            "date_from 不能晚于 date_to",
            details={"date_from": resolved_from, "date_to": resolved_to},
        )

    collected = await _points_by_date(shop_id=shop_id)
    points = [
        MetricPoint.model_validate(collected[day])
        for day in sorted(collected)
        if resolved_from <= day <= resolved_to
    ]
    if not points:
        raise ResourceNotFound(
            f"店铺 {shop_id} 在 {resolved_from}..{resolved_to} 没有指标数据",
            details={
                "shop_id": shop_id,
                "date_from": resolved_from,
                "date_to": resolved_to,
            },
        )
    return MetricsResponse(shop_id=shop_id, granularity="day", points=points)
