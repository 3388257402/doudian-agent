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
#: 这个数是**查询成本上限**（每读一个 run = 一次 checkpoint 往返），不是业务常量。
_MAX_RUNS = 200

#: 默认窗口长度：今天往前 6 天 + 今天 = 7 个业务日（§7.5）
_DEFAULT_WINDOW_DAYS = 6


def _parse_day(value: str, *, field: str) -> str:
    """`YYYY-MM-DD` 校验（§7.5 的 `date_from` / `date_to`）。

    保证：合法时返回规范化的 `isoformat()`（与入参同形）；非法时抛 400。
    不保证：不校验业务含义（是否在今天之前、是否在上海时区）——只是格式校验。
    调用方坑：`date.fromisoformat` 在 3.11+ 也接受 `2026-10-23T00:00:00` 这类形状，
        返回值会被截成日期部分；这是有意的宽容，不是漏校验。
    """
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        # 统一成 `ValidationError`（→ 400）而不是让 `ValueError` 逃出去变 500：
        # 日期格式是**调用方**的问题，不是本地 bug。
        raise ValidationError(
            f"{field} 必须是 YYYY-MM-DD（Asia/Shanghai 的业务日期）",
            details={field: value},
        ) from exc


def _shift(day: str, offset_days: int) -> str:
    """业务日期平移（`YYYY-MM-DD`），用于默认窗口 `今天-6`。

    保证：纯日历运算，跨月/跨年正确；输出形状与入参一致（`isoformat()`）。
    不保证：不处理时区——入参必须已经是业务日期（`business_date()` 的产物）。
    调用方坑：`day` 非法会抛 `ValueError`（调用前已由 `_parse_day` 校验过）。
    """
    return (date.fromisoformat(day) + timedelta(days=offset_days)).isoformat()


async def _points_by_date(*, shop_id: str) -> dict[str, dict[str, Any]]:
    """该店铺所有 run 里出现过的指标快照（`date -> MetricsSnapshot`，后写的覆盖先写的）。

    保证：同一天以**最新一次** run 为准（`rows` 已按开始时间升序，后面的覆盖前面的）。
    不保证：不保证覆盖"全部历史"——只读最近 `_MAX_RUNS` 个 run（更早的不参与合并）。
    调用方坑：这不是财务口径的历史账。早先 run 独有的日期仍会保留，但同一天的数会被后来的
        run 覆盖——真正的对账需要指标持久化（见模块 docstring）。
    """
    rows = await queries.list_run_rows(shop_id=shop_id, limit=_MAX_RUNS)
    if not rows:
        # 该店铺一个 run 都没有：直接返回空（端点会在下面给出 404，不在这里抛）。
        return {}

    settings = get_settings()
    collected: dict[str, dict[str, Any]] = {}
    async with resolve_checkpointer(settings=settings) as saver:
        graph = build_graph(saver)
        for row in rows:  # 升序：新 run 的同日指标覆盖旧的
            # 每个 run 读一次 state：这是本端点的主要成本（N 次 checkpoint 往返），
            # 所以 `_MAX_RUNS` 是有必要的上限，不是随手写的数。
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": row["thread_id"]}}
            )
            values = getattr(snapshot, "values", None) or {}
            # `history` 是近 8 天序列，`metrics` 是当日：两者都要，否则只有当天有图。
            candidates: list[Any] = list(values.get("history") or [])
            if isinstance(values.get("metrics"), Mapping):
                candidates.append(values["metrics"])
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    # 老 checkpoint 里可能有非 mapping 的脏值：跳过而不是 500。
                    continue
                day = str(candidate.get("date") or "")
                if not day:
                    # 没有 `date` 的快照无法归日：跳过（它不该出现在合法的 state 里）。
                    continue
                collected[day] = dict(candidate)
    return collected


@router.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="店铺级日粒度指标（金额单位：分）",
    # 400（参数）与 404（该店铺该区间没有数据）：没有 409/502——本端点不打上游。
    responses={400: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}},
)
async def get_metrics(
    shop_id: str = Query(pattern=SHOP_ID_PATTERN),
    # 两个日期都默认 `None`（而不是在签名里算默认值）：默认值依赖"今天的业务日期"，
    # 只能在函数体里算（签名在导入期求值，那时算出来的"今天"是进程启动那天）。
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

    两个日期参数的缺省值都在服务端算：客户端不传就等于"我要最近 7 天"，
    而不是"我要全部"——后者会让一次请求变成全表扫描。

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

    保证：`points` 按 `date` 升序；每个点都过了 `MetricPoint` 校验（金额是 int 分）。
    不保证：不保证 `points` 连续——缺的日期不补零，前端要自己处理空洞。
    """
    today = business_date()
    # 默认窗口"今天-6 .. 今天"由**服务端时钟**算：跨零点前后请求同一区间会得到不同结果，
    # 要稳定就显式传两端日期（这一点写在 docstring 的调用方坑里）。
    resolved_to = _parse_day(date_to or today, field="date_to")
    resolved_from = _parse_day(
        date_from or _shift(today, -_DEFAULT_WINDOW_DAYS), field="date_from"
    )
    if resolved_from > resolved_to:
        # 区间倒置：不夹逼、不交换（那会让调用方以为参数生效了），直接 400。
        raise ValidationError(
            "date_from 不能晚于 date_to",
            details={"date_from": resolved_from, "date_to": resolved_to},
        )

    collected = await _points_by_date(shop_id=shop_id)
    # 按日期排序后逐点校验：`MetricPoint` 会把缺的键补默认值（`None`），
    # 所以老 checkpoint（没有成本口径字段）能直接兼容，不需要在这里做版本判断。
    points = [
        MetricPoint.model_validate(collected[day])
        for day in sorted(collected)
        # 区间过滤在**内存**里做（collected 已经按日期分好）：省一次 DB 往返，
        # 而且"缺的日期不补零"在这里是天然的（没有的 key 就不产生 point）。
        if resolved_from <= day <= resolved_to
    ]
    if not points:
        # 空态 → 404（§7.5）：`200 + points=[]` 会让前端把"没数据"画成一条 0 线。
        raise ResourceNotFound(
            f"店铺 {shop_id} 在 {resolved_from}..{resolved_to} 没有指标数据",
            details={
                "shop_id": shop_id,
                "date_from": resolved_from,
                "date_to": resolved_to,
            },
        )
    # `granularity="day"` 写死：`MetricsResponse.granularity` 是单值 Literal，
    # 传别的值 pydantic 会拒（这样"周聚合"的请求在类型层就被挡住，而不是返回假数据）。
    return MetricsResponse(shop_id=shop_id, granularity="day", points=points)