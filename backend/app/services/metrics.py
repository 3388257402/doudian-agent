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
) -> MetricsSnapshot:
    """由原始快照算出 `MetricsSnapshot`（公式见 §5.3 表，逐行对应）。

    `previous` 不在 `MetricsSnapshot` 里产出环比字段（§3.3 没有这些字段）：环比在节点层
    用 `history` 算出来放进规则命名空间（§8.3.3 的 `gmv_delta_ratio` 等）。
    这里对 `previous` 只做**顺序一致性校验**——把"历史倒序传入"这种错误在源头拦住，
    否则环比符号会整体翻转，而结果看起来完全正常。
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
        "computed_at": computed_at,
    }


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
