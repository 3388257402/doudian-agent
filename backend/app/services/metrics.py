"""指标计算（CONTRACT §5.3，纯函数）。

设计要点（每一条都会被追问）：

1. **纯函数**：无 I/O、无全局状态、`now` 可注入 → 同一份 `raw` 在任何时刻算出同一结果，
   指标可复现、可对账。
2. **`None` 与 `0` 严格区分**：分母为 0 的比率返回 `None`（"没有意义"），不是 `0`
   （"等于零"）。把两者混起来，看板会把"没有访客数据"画成"转化率 0%"，
   运营会据此得出错误结论。
3. 金额**必须已是整数分**：出现 `*_yuan` 或 `*_cents` 是 float → 立刻 `ValueError`。
   在边界炸，而不是静默转换——静默转换会让 9900 分 vs 99.00 元的歧义一路传到下单。
4. 缺字段语义：`order_summary` 的五个金额/计数是**必需字段**（缺 → `KeyError`，
   因为缺了就算不出契约要求的字段）；`uv` 是可缺字段（缺 → `uv=None`, `cvr=None`）。
5. 成本口径（`gross_margin` / `cost_coverage_ratio`）：成本是**参数**（`cost_table`）不是文件
   读取（§8.5：读 `config/sku_costs.yaml` 是装配层的责任）。没有成本 → 两个字段都是 `None`，
   **不是 0**：把"没配成本"当 0 会把正常的商品判成亏本，然后 Agent 去改价/下架——本仓里
   最贵的一类误报。`cost_coverage_ratio` 存在的唯一理由，是让消费者看到"这个毛利率只覆盖了
   一部分成交额"，而不是把它当成全店的结论。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型（避免 metrics → agent 的模块级依赖）
    from app.agent.state import MetricsSnapshot, RawSnapshot

__all__ = ["compute_metrics", "REQUIRED_ORDER_SUMMARY_KEYS", "MIN_HISTORY_DAYS"]

#: `order_summary` 里必需存在的字段（缺一个就算不出契约字段，直接 KeyError 报名字）。
REQUIRED_ORDER_SUMMARY_KEYS: tuple[str, ...] = (
    "gmv_cents",
    "order_count",
    "paid_order_count",
    "refund_order_count",
    "refund_amount_cents",
)

#: 近 7 日均值的最少历史天数（不足 → None，不给"用 1 天算均值"的假精确）。
MIN_HISTORY_DAYS = 3

#: 近 7 日均值最多取多少条。
MAX_HISTORY_DAYS = 7


def compute_metrics(
    raw: RawSnapshot,
    *,
    previous: MetricsSnapshot | None = None,
    history: Sequence[MetricsSnapshot] | None = None,
    now: datetime | None = None,
    cost_table: Mapping[str, int] | None = None,
) -> MetricsSnapshot:
    """由原始快照算出 `MetricsSnapshot`（公式见 §5.3 表，逐行对应）。

    `previous` 不在 `MetricsSnapshot` 里产出环比字段（§3.3 没有这些字段）：环比在节点层
    用 `history` 算出来放进规则命名空间（§8.3.3 的 `gmv_delta_ratio` 等）。
    这里对 `previous` 只做**顺序一致性校验**——把"历史倒序传入"这种错误在源头拦住，
    否则环比符号会整体翻转，而结果看起来完全正常。

    `cost_table` 是 keyword-only 的**可选**参数（`sku_id -> 成本整数分`，§8.5）：不传等价于
    "这家店没配成本"，两条成本口径字段都是 `None`。传了也不会凭空造出按 SKU 的成交额——那
    必须由 `collect` 归集进 `order_summary["by_sku"]`，否则我们只知道"成本是多少"，不知道
    "卖出去了多少件、按什么价卖的"，算不出任何有意义的毛利率。
    """
    _reject_non_cent_amounts(raw)

    try:
        window = raw["window"]
        date = str(window["end"])
    except (KeyError, TypeError) as exc:
        raise KeyError(f"raw 缺少 window.end（业务日期）: {exc}") from exc

    summary = raw.get("order_summary")
    if not isinstance(summary, Mapping):
        raise KeyError("raw.order_summary 缺失或不是映射")
    gmv_cents = _require_int(summary, "gmv_cents", where="raw.order_summary")
    order_count = _require_int(summary, "order_count", where="raw.order_summary")
    paid_order_count = _require_int(summary, "paid_order_count", where="raw.order_summary")
    refund_order_count = _require_int(summary, "refund_order_count", where="raw.order_summary")
    refund_amount_cents = _require_int(summary, "refund_amount_cents", where="raw.order_summary")

    uv_raw = summary.get("uv")
    uv = None if uv_raw is None else _require_int(summary, "uv", where="raw.order_summary")

    # aov：分母 0 → 0（契约 §5.3 表；"客单价 0"是业务上有意义的 0，不是 None）
    aov_cents = 0 if paid_order_count == 0 else int(round(gmv_cents / paid_order_count))
    # 退款率 / 转化率：分母 0 → None（"没有支付订单"算不出退款率）
    refund_rate = (
        None if paid_order_count == 0 else round(refund_order_count / paid_order_count, 6)
    )
    cvr = None if not uv else round(paid_order_count / uv, 6)

    if previous is not None:
        prev_date = previous.get("date")
        if not prev_date:
            raise ValueError("previous 缺少 date")
        if str(prev_date) >= date:
            raise ValueError(
                f"previous.date({prev_date}) 必须早于本次 date({date})：历史被倒序传入"
            )

    gmv_7d_avg_cents = _gmv_7d_avg(history or (), today=date)

    gross_margin, cost_coverage_ratio = _margin_and_coverage(
        summary, gmv_cents=gmv_cents, cost_table=cost_table
    )

    computed_at = _iso_z(now)

    return {
        "date": date,
        "gmv_cents": gmv_cents,
        "order_count": order_count,
        "paid_order_count": paid_order_count,
        "refund_order_count": refund_order_count,
        "refund_amount_cents": refund_amount_cents,
        "aov_cents": aov_cents,
        "refund_rate": refund_rate,
        "uv": uv,
        "cvr": cvr,
        "gmv_7d_avg_cents": gmv_7d_avg_cents,
        "gross_margin": gross_margin,
        "cost_coverage_ratio": cost_coverage_ratio,
        "computed_at": computed_at,
    }


def _margin_and_coverage(
    summary: Mapping[str, Any],
    *,
    gmv_cents: int,
    cost_table: Mapping[str, int] | None,
) -> tuple[float | None, float | None]:
    """店铺级加权毛利率 + 有成本 SKU 的成交额占比（两个都可能为 `None`）。

    公式（只对**有成本**的那部分 SKU 算，成本未知的 SKU 既不进分子也不进分母）：

        covered_revenue = Σ by_sku[sku].gmv_cents        （sku ∈ cost_table）
        covered_cost    = Σ cost_table[sku] × by_sku[sku].units
        gross_margin        = (covered_revenue - covered_cost) / covered_revenue
        cost_coverage_ratio = covered_revenue / gmv_cents

    为什么分母是"有成本的成交额"而不是全店 GMV：把没有成本的 SKU 塞进分母，等于假设
    "这些 SKU 的成本等于售价"（毛利 0）。那是**编出来的数**，而且方向不可控——真亏的会被
    摊薄成"还行"，真赚的会被摊薄成"亏本"。宁可只报一个覆盖了一部分的毛利率，也不报一个
    假装覆盖了全店的。

    为什么需要 `cost_coverage_ratio`：上面那个"只报一部分"的性质必须**可见**，否则
    `gross_margin = -0.5` 会被读成"全店在亏"，其实它可能只代表 5% 的成交额。规则层因此
    要求覆盖率下限；本函数只负责如实给出这个数。

    `None` 的四种来源（都**不能**用 0 代替）：
    - `by_sku` 缺失：`collect` 没做按 SKU 归集（老快照/历史桶）→ 无法归属，两个都是 `None`；
    - `covered_revenue == 0`：一个 SKU 都没配上成本（或配了但今天没卖）→ 毛利率 `None`；
    - `gmv_cents == 0`：全店没成交 → 覆盖率的分母是 0 → `None`（§5.3 的除法边界）；
    - 覆盖率不裁剪：`covered_revenue > gmv_cents` 说明上游汇总与订单明细对不上，如实给出
      > 1 的数，让数据不一致暴露出来，而不是用 `min(…, 1.0)` 把它抹平。
    """
    by_sku = summary.get("by_sku")
    if by_sku is None:
        return None, None
    if not isinstance(by_sku, Mapping):
        # 用 ValueError 而不是 TypeError：与同文件的 `_require_int` / `_reject_non_cent_amounts`
        # 保持一致——本模块把"上游给的数据形状不对"统一表达成 ValueError（调用方只需处理一种）。
        raise ValueError(  # noqa: TRY004
            f"order_summary.by_sku 必须是映射（sku_id -> {{gmv_cents, units}}），"
            f"收到 {type(by_sku).__name__}"
        )

    table = cost_table or {}
    covered_revenue = 0
    covered_cost = 0
    for raw_sku, entry in by_sku.items():
        sku_id = str(raw_sku)
        where = f"order_summary.by_sku[{sku_id!r}]"
        if not isinstance(entry, Mapping):
            raise ValueError(  # noqa: TRY004 - 同上：数据形状错误统一用 ValueError
                f"{where} 必须是映射，收到 {type(entry).__name__}"
            )
        entry_revenue = _require_int(entry, "gmv_cents", where=where)
        entry_units = _require_int(entry, "units", where=where)
        if entry_revenue < 0:
            raise ValueError(f"{where}.gmv_cents 必须 >= 0，收到 {entry_revenue}")
        if entry_units < 0:
            raise ValueError(f"{where}.units 必须 >= 0，收到 {entry_units}")

        cost = table.get(sku_id)
        if cost is None:
            continue  # 成本未知：不计入毛利，但它的成交额也不会被塞进分母（见 docstring）
        # 成本表本身在加载期已校验（§8.5），这里再查一次是因为 `compute_metrics` 是公开
        # 纯函数：调用方可以直接传字典进来，边界上炸比让 float 成本静默参与运算更好。
        if isinstance(cost, bool) or not isinstance(cost, int):
            raise ValueError(  # noqa: TRY004 - 同上：金额类型错误统一用 ValueError
                f"cost_table[{sku_id!r}] 必须是整数分 int，收到 {cost!r}"
            )
        if cost <= 0:
            raise ValueError(f"cost_table[{sku_id!r}] 必须 > 0（整数分），收到 {cost}")
        if entry_revenue > 0 and entry_units == 0:
            # 有成交额却没有件数：成本摊不出去（金额/件数任一为 0 都会让毛利率失真）。
            #
            # 为什么采集层容错、这里却拒绝（v1.5.1，两层**态度不同是刻意的**）：
            # - 采集层（`nodes._order_units`）面对的是**上游真实脏数据**：`product_count: 0`
            #   如果直接抛，`collect` 节点会把整轮 run 打成 failed → `POST /api/agent/run` 500
            #   → 一条脏数据打死当天所有指标。所以那一层对"件数 <= 0 / 缺件数"按 1 件降级，
            #   并记一条 warn 级审计（JSONL 可见）；
            # - 本层是**纯函数边界**，面对的是"已经归集好的 by_sku"。到这里 `units` 已经被
            #   下游当成真值参与 `成本额 = 单价成本 × 件数`，再猜一次就等于在真值上盖一层猜测，
            #   而且会让"有人绕开 collect 手工拼 summary"这类错误永远不被发现。所以这里拒绝，
            #   不降级、不兜底。
            #
            # 换句话说：容错属于**数据入口**，拒绝属于**计算契约**；两者都在做自己的本职。
            raise ValueError(
                f"{where}: 成交额 {entry_revenue} > 0 但 units = 0，成本无法折算"
                "（按 SKU 归集时件数必须 >= 1）"
            )

        covered_revenue += entry_revenue
        covered_cost += cost * entry_units

    gross_margin = (
        None
        if covered_revenue <= 0
        else round((covered_revenue - covered_cost) / covered_revenue, 6)
    )
    cost_coverage_ratio = None if gmv_cents <= 0 else round(covered_revenue / gmv_cents, 6)
    return gross_margin, cost_coverage_ratio


def _gmv_7d_avg(history: Sequence[Mapping[str, Any]], *, today: str) -> int | None:
    """近 7 日（**不含今天**）GMV 均值；可用天数 < 3 → None。"""
    entries = [
        item for item in history
        if isinstance(item, Mapping) and str(item.get("date") or "") < today
    ]
    entries.sort(key=lambda item: str(item["date"]), reverse=True)
    entries = entries[:MAX_HISTORY_DAYS]
    if len(entries) < MIN_HISTORY_DAYS:
        return None
    values = [_require_int(item, "gmv_cents", where="history[]") for item in entries]
    return int(round(sum(values) / len(values)))


def _require_int(container: Mapping[str, Any], key: str, *, where: str) -> int:
    """取整数字段：缺失 → KeyError（带位置），`bool` 不算 `int`，float 一律拒绝。"""
    if key not in container:
        raise KeyError(f"{where} 缺少必需字段 {key!r}")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}.{key} 必须是 int（整数分/计数），收到 {value!r}")
    return value


def _reject_non_cent_amounts(raw: Any, *, path: str = "raw") -> None:
    """金额字段必须是整数分：出现 `*_yuan` 或非 int 的 `*_cents` → ValueError。"""
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            key_str = str(key)
            here = f"{path}.{key_str}"
            if key_str.endswith("_yuan"):
                raise ValueError(f"{here}: 禁止元为单位的金额字段（一律整数分 *_cents）")
            if key_str.endswith("_cents") and not isinstance(value, bool):
                if not isinstance(value, int):
                    raise ValueError(f"{here}: 金额必须是整数分 int，收到 {value!r}")
            _reject_non_cent_amounts(value, path=here)
    elif isinstance(raw, (list, tuple)):
        for index, item in enumerate(raw):
            _reject_non_cent_amounts(item, path=f"{path}[{index}]")
    return


def _iso_z(dt: datetime | None) -> str:
    """UTC ISO-8601、**毫秒**精度、`Z` 结尾（§0 时间规则 + §7.5 示例）。

    统一毫秒而不是 `isoformat()` 的微秒：日志/`X-Trace-Id`/API 响应里的时间戳
    必须是同一个形状，否则前端按字符串比较/排序、以及"两个事件谁先谁后"的对账
    都会因为精度不同而出现假差异（reviewer F5）。毫秒截断不是四舍五入——时间戳
    只做展示与排序，截断不会让它偏离真实时刻超过 1ms。
    """
    value = dt if dt is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
