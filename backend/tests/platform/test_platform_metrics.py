"""指标（CONTRACT §5.3）：纯函数、公式逐行、`None` 与 `0` 严格区分。

这组用例的核心不是"算得对"，而是**语义边界**：
分母为 0 的比率是 `None`（"没有意义"）而不是 `0`（"等于零"）；
缺少必需字段要炸出字段名，而不是静默算成 0；
金额出现"元"或 float 必须在边界被拒。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from app.services.metrics import MIN_HISTORY_DAYS, compute_metrics


def _raw(**overrides) -> dict:
    summary = {
        "gmv_cents": 100_000,
        "order_count": 12,
        "paid_order_count": 10,
        "refund_order_count": 1,
        "refund_amount_cents": 9_900,
        "uv": 1_000,
    }
    summary.update(overrides.pop("summary", {}))
    raw = {
        "collected_at": "2026-10-23T10:00:00.000Z",
        "window": {"start": "2026-10-23", "end": "2026-10-23"},
        "shop": {"shop_id": "12345678"},
        "products": [],
        "order_summary": summary,
        "source": {"methods": ["shop.getShopInfo"], "request_ids": [], "partial": False},
    }
    raw.update(overrides)
    return raw


def test_formulas_follow_the_contract_table() -> None:
    metrics = compute_metrics(_raw())
    assert metrics["date"] == "2026-10-23"
    assert metrics["gmv_cents"] == 100_000
    assert metrics["order_count"] == 12
    assert metrics["paid_order_count"] == 10
    assert metrics["refund_order_count"] == 1
    assert metrics["refund_amount_cents"] == 9_900
    assert metrics["aov_cents"] == 10_000  # 100000 / 10，四舍五入到整数分
    assert metrics["refund_rate"] == 0.1
    assert metrics["uv"] == 1_000
    assert metrics["cvr"] == 0.01
    assert metrics["gmv_7d_avg_cents"] is None  # 没有历史
    assert metrics["computed_at"].endswith("Z")


def test_aov_rounds_half_to_even_boundary_and_keeps_int_type() -> None:
    metrics = compute_metrics(_raw(summary={"gmv_cents": 100_001, "paid_order_count": 3}))
    assert metrics["aov_cents"] == 33_334  # 33333.67 -> 33334
    assert isinstance(metrics["aov_cents"], int)


def test_zero_denominator_semantics_none_versus_zero() -> None:
    """§5.3 表：aov 分母 0 → 0（业务上有意义的 0）；refund_rate 分母 0 → None。

    cvr 的分母是 `uv` 不是 `paid_order_count`：有访客、没成交 → `cvr == 0.0`
    （"转化率确实为 0"是信息）；没有访客数据 → `cvr is None`（"算不出来"）。
    这两种情况在看板上必须长得不一样。
    """
    metrics = compute_metrics(
        _raw(
            summary={
                "paid_order_count": 0,
                "order_count": 5,  # 下单了但没支付
                "refund_order_count": 0,
                "uv": 800,
            }
        )
    )
    assert metrics["aov_cents"] == 0
    assert metrics["refund_rate"] is None
    assert metrics["cvr"] == 0.0, "有 800 个访客却 0 成交：转化率是真实的 0，不是『算不出来』"
    assert metrics["uv"] == 800

    no_traffic = compute_metrics(_raw(summary={"paid_order_count": 0, "uv": None}))
    assert no_traffic["cvr"] is None, "没有访客数据才是 None"


def test_uv_zero_is_zero_not_none_but_cvr_is_none() -> None:
    """`uv=0` 是"有数据且访客为 0"，与"没有 uv 数据"必须区分。"""
    metrics = compute_metrics(_raw(summary={"uv": 0}))
    assert metrics["uv"] == 0
    assert metrics["cvr"] is None

    no_uv = compute_metrics(_raw(summary={"uv": None}))
    assert no_uv["uv"] is None
    assert no_uv["cvr"] is None

    missing_uv = _raw()
    del missing_uv["order_summary"]["uv"]
    assert compute_metrics(missing_uv)["uv"] is None


def test_missing_required_field_names_the_field() -> None:
    for field in (
        "gmv_cents",
        "order_count",
        "paid_order_count",
        "refund_order_count",
        "refund_amount_cents",
    ):
        broken = _raw()
        del broken["order_summary"][field]
        with pytest.raises(KeyError) as excinfo:
            compute_metrics(broken)
        assert field in str(excinfo.value)


def test_missing_order_summary_or_window_is_an_error() -> None:
    broken = _raw()
    del broken["order_summary"]
    with pytest.raises(KeyError):
        compute_metrics(broken)

    broken = _raw()
    del broken["window"]
    with pytest.raises(KeyError):
        compute_metrics(broken)


def test_float_or_bool_money_is_rejected_at_the_boundary() -> None:
    """9900 分 vs 99.00 元的歧义必须在边界炸掉，不许静默转换。"""
    with pytest.raises(ValueError) as excinfo:
        compute_metrics(_raw(summary={"gmv_cents": 990.0}))
    assert "整数分" in str(excinfo.value)

    with pytest.raises(ValueError):
        compute_metrics(_raw(summary={"gmv_cents": True}))

    with pytest.raises(ValueError):
        compute_metrics(_raw(summary={"paid_order_count": 3.5}))


def test_yuan_fields_are_rejected_anywhere_in_the_snapshot() -> None:
    broken = _raw()
    broken["products"] = [{"sku_id": "s1", "price_yuan": 99.0}]
    with pytest.raises(ValueError) as excinfo:
        compute_metrics(broken)
    assert "元" in str(excinfo.value)


def test_7d_average_uses_only_days_before_today_and_caps_at_seven() -> None:
    history = [
        {"date": "2026-10-22", "gmv_cents": 10_000},
        {"date": "2026-10-21", "gmv_cents": 20_000},
        {"date": "2026-10-20", "gmv_cents": 30_000},
        {"date": "2026-10-23", "gmv_cents": 999_999},  # 今天：必须被排除
    ]
    metrics = compute_metrics(_raw(), history=history)
    assert metrics["gmv_7d_avg_cents"] == 20_000  # (10000+20000+30000)/3

    # 超过 7 条：只取最近的 7 条（不含今天）
    many = [
        {"date": f"2026-10-{day:02d}", "gmv_cents": 1_000 * day}
        for day in range(1, 20)
    ]
    metrics_many = compute_metrics(_raw(), history=many)
    recent_seven = [1_000 * day for day in range(19, 12, -1)]  # 19..13
    assert metrics_many["gmv_7d_avg_cents"] == int(round(sum(recent_seven) / 7))


def test_7d_average_is_none_when_history_is_too_short() -> None:
    """不足 3 天 → None：宁可"没有基线"也不给"用 1 天算均值"的假精确。"""
    history = [
        {"date": "2026-10-22", "gmv_cents": 10_000},
        {"date": "2026-10-21", "gmv_cents": 20_000},
    ]
    assert len(history) < MIN_HISTORY_DAYS
    assert compute_metrics(_raw(), history=history)["gmv_7d_avg_cents"] is None


def test_history_with_non_int_gmv_is_rejected_not_averaged() -> None:
    history = [
        {"date": "2026-10-22", "gmv_cents": 10_000},
        {"date": "2026-10-21", "gmv_cents": 20_000},
        {"date": "2026-10-20", "gmv_cents": "30000"},
    ]
    with pytest.raises(ValueError):
        compute_metrics(_raw(), history=history)


def test_previous_must_be_strictly_earlier() -> None:
    """历史倒序传入会让环比符号整体翻转，而结果看起来完全正常 → 源头拦住。"""
    same_day = {"date": "2026-10-23", "gmv_cents": 1}
    with pytest.raises(ValueError):
        compute_metrics(_raw(), previous=same_day)

    later = {"date": "2026-10-24", "gmv_cents": 1}
    with pytest.raises(ValueError):
        compute_metrics(_raw(), previous=later)

    earlier = {"date": "2026-10-22", "gmv_cents": 1}
    assert compute_metrics(_raw(), previous=earlier)["date"] == "2026-10-23"


def test_computed_at_is_injected_now_utc_z() -> None:
    now = datetime(2026, 10, 23, 14, 25, 30, 113_000, tzinfo=timezone.utc)
    metrics = compute_metrics(_raw(), now=now)
    assert metrics["computed_at"] == "2026-10-23T14:25:30.113Z"

    naive = datetime(2026, 10, 23, 14, 25, 30)
    assert compute_metrics(_raw(), now=naive)["computed_at"].endswith("Z")


def test_pure_function_same_input_same_output() -> None:
    now = datetime(2026, 10, 23, 0, 0, 0, tzinfo=timezone.utc)
    history = [
        {"date": "2026-10-22", "gmv_cents": 10_000},
        {"date": "2026-10-21", "gmv_cents": 20_000},
        {"date": "2026-10-20", "gmv_cents": 30_000},
    ]
    first = compute_metrics(_raw(), history=history, now=now)
    second = compute_metrics(_raw(), history=history, now=now)
    assert first == second
    # 不修改入参（纯函数不许有副作用）
    assert _raw()["order_summary"]["gmv_cents"] == 100_000


def test_ratios_are_rounded_to_six_decimals() -> None:
    metrics = compute_metrics(
        _raw(summary={"paid_order_count": 7, "order_count": 9, "refund_order_count": 1, "uv": 3})
    )
    assert metrics["refund_rate"] == round(1 / 7, 6)
    assert metrics["cvr"] == round(7 / 3, 6)


def test_computed_at_uses_millisecond_precision_like_every_other_timestamp() -> None:
    """F5：全仓时间戳同一形状（毫秒 + Z），不许这一处是微秒。

    精度不一致 → 前端字符串排序/去重、以及"两个事件谁先谁后"的对账都会出现假差异。
    """
    from app.observability.logging import utcnow_iso
    from app.services.metrics import _iso_z

    moment = datetime(2026, 10, 23, 14, 25, 30, 113_456, tzinfo=timezone.utc)
    computed_at = compute_metrics(_raw(), now=moment)["computed_at"]

    assert computed_at == "2026-10-23T14:25:30.113Z"
    assert computed_at == _iso_z(moment) == utcnow_iso(moment), "指标与日志必须用同一个时间形状"
    # 形状检查：毫秒固定 3 位，没有微秒尾巴
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", computed_at)

    # naive 输入按 UTC 解释，不是按本机时区
    naive = datetime(2026, 10, 23, 14, 25, 30, 113_456)
    assert compute_metrics(_raw(), now=naive)["computed_at"] == "2026-10-23T14:25:30.113Z"
