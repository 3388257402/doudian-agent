"""店铺级成本口径：`gross_margin` / `cost_coverage_ratio` + `loss_making` 规则（v1.5.0）。

这组用例守的不是"算得对"，而是**`None` 与 `0` 的边界**（README 已知限制 4 / §8.5）：

- 一个成本都没配 → `gross_margin is None`，`loss_making` **不触发**。把"没配成本"当 0 会把
  正常商品判成亏本，然后 Agent 去改价/下架——本仓里最贵的一类误报；
- 覆盖率太低 → 毛利率只代表少数成交，**不许**拿它下结论（规则里的 `>= 0.6` 是硬条件）；
- 分母为 0 → `None`（不是 `ZeroDivisionError`，也不是 0）；
- 成本表本身在**加载期**校验：负数/非 int/重复/空值/非字符串键全是 `ConfigError`。

夹具数字都是**手算过的**，算式写在断言旁的注释里——改公式会立刻打红，而不是"看起来还行"。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from app.agent.nodes import _aggregate_orders, _order_units, _unmatched_cost_skus
from app.errors import ConfigError
from app.services.anomalies import evaluate_rules
from app.services.costs import (
    load_sku_cost_config,
    load_sku_costs,
    parse_sku_costs,
)
from app.services.metrics import compute_metrics
from app.services.rules import ALLOWED_NAMES, RuleSet, load_rules

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEMO_DAY = "2026-10-23"


# --------------------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------------------


def _raw(
    *,
    gmv_cents: int = 100_000,
    by_sku: Mapping[str, Mapping[str, int]] | None = None,
    paid_order_count: int = 10,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "gmv_cents": gmv_cents,
        "order_count": 12,
        "paid_order_count": paid_order_count,
        "refund_order_count": 0,
        "refund_amount_cents": 0,
        "uv": 1_000,
    }
    if by_sku is not None:
        summary["by_sku"] = {sku: dict(entry) for sku, entry in by_sku.items()}
    return {
        "collected_at": "2026-10-23T10:00:00.000Z",
        "window": {"start": DEMO_DAY, "end": DEMO_DAY},
        "shop": {"shop_id": "12345678"},
        "products": [],
        "order_summary": summary,
        "source": {"methods": ["shop.getShopInfo"], "request_ids": [], "partial": False},
    }


def _hits(metrics: Mapping[str, Any], rules: RuleSet) -> list[str]:
    """用**仓里真实**的 `config/rules.yaml` 求值，返回命中的 rule_id 列表。

    `max_anomalies` 显式传值：否则会去 `get_settings()` 读进程级配置，
    让一个纯函数用例依赖环境（本仓反复踩过的"测试顺序一变就红"）。
    """
    evaluation = evaluate_rules(
        metrics=metrics,  # type: ignore[arg-type]
        history=[],
        rules=rules,
        run_id="run_gross_margin",
        max_anomalies=20,
    )
    assert evaluation.skipped == [], f"规则求值错误：{evaluation.skipped}"
    return [anomaly["rule_id"] for anomaly in evaluation.anomalies]


@pytest.fixture(scope="module")
def repo_rules() -> RuleSet:
    """仓里真实的规则集（`loss_making` 就在里面，别在测试里抄一份条件）。"""
    return load_rules()


# --------------------------------------------------------------------------------------
# 1) 全部 SKU 有成本 → 加权毛利率等于手算值
# --------------------------------------------------------------------------------------


def test_all_skus_costed_margin_matches_hand_calculation() -> None:
    """覆盖 100% 时，加权毛利率就是全店的真实毛利率。

    手算：
        sku_a 成交 60000，卖 30 件，成本 1000/件 → 成本额 30000
        sku_b 成交 40000，卖 10 件，成本 3000/件 → 成本额 30000
        毛利率 = (60000 + 40000 - 30000 - 30000) / (60000 + 40000)
               = 40000 / 100000 = 0.4
        覆盖率 = (60000 + 40000) / 100000 = 1.0
    """
    metrics = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 60_000, "units": 30},
                     "sku_b": {"gmv_cents": 40_000, "units": 10}}),
        cost_table={"sku_a": 1_000, "sku_b": 3_000},
    )
    assert metrics["gross_margin"] == 0.4
    assert metrics["cost_coverage_ratio"] == 1.0
    # 成本不变，成交额变一半 → 毛利率仍由"单价-成本"决定，不是"金额决定"。
    again = compute_metrics(
        _raw(gmv_cents=50_000,
             by_sku={"sku_a": {"gmv_cents": 30_000, "units": 15},
                     "sku_b": {"gmv_cents": 20_000, "units": 5}}),
        cost_table={"sku_a": 1_000, "sku_b": 3_000},
    )
    assert again["gross_margin"] == 0.4
    assert again["cost_coverage_ratio"] == 1.0


# --------------------------------------------------------------------------------------
# 2) 部分 SKU 有成本 → 只基于有成本部分，且覆盖率 < 1
# --------------------------------------------------------------------------------------


def test_partial_cost_only_uses_costed_skus_and_exposes_coverage() -> None:
    """只有 sku_a 配了成本：毛利率只代表 sku_a，覆盖率把它暴露出来。

    手算：
        sku_a 成交 60000，卖 30 件，成本 1000/件 → 成本额 30000
        毛利率 = (60000 - 30000) / 60000 = 0.5          ← 只算 sku_a
        覆盖率 = 60000 / 100000 = 0.6                    ← sku_b 的 40000 未覆盖
    """
    metrics = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 60_000, "units": 30},
                     "sku_b": {"gmv_cents": 40_000, "units": 10}}),
        cost_table={"sku_a": 1_000},
    )
    assert metrics["gross_margin"] == 0.5
    assert metrics["cost_coverage_ratio"] == 0.6
    assert metrics["cost_coverage_ratio"] < 1.0, "部分覆盖必须可见，不能被读成全店结论"

    # 成本表里有、订单里没有的 SKU 不影响计算（未卖出 = 没有成本额发生）
    with_dead_sku = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 60_000, "units": 30},
                     "sku_b": {"gmv_cents": 40_000, "units": 10}}),
        cost_table={"sku_a": 1_000, "sku_dead": 999_999},
    )
    assert with_dead_sku["gross_margin"] == 0.5
    assert with_dead_sku["cost_coverage_ratio"] == 0.6


# --------------------------------------------------------------------------------------
# 3) 全部没成本 → None，且 loss_making 不触发（最关键的一条）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cost_table", [None, {}])
def test_no_cost_at_all_is_none_and_never_triggers_loss_making(
    cost_table: Mapping[str, int] | None, repo_rules: RuleSet
) -> None:
    """没有成本 → `gross_margin is None`（**不是 0、也不是 1.0**），规则不触发。

    为什么这条最重要：`gross_margin` 若被写成 0，`loss_making` 的条件
    `gross_margin < 0.0` 的**反方向**写法（或任何"缺失当 0 参与比较"的实现）会把这家
    根本没配成本的店判成"全店亏本"，Agent 就会去改价/下架。宁可漏报，不可误报。
    """
    metrics = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 100_000, "units": 40}}),
        cost_table=cost_table,
    )
    assert metrics["gross_margin"] is None
    assert metrics["cost_coverage_ratio"] == 0.0, "一个子儿都没覆盖：这是真实的 0，不是 None"
    assert "loss_making" not in _hits(metrics, repo_rules)


def test_loss_making_does_not_fire_when_cost_table_is_empty_even_at_zero_gmv(
    repo_rules: RuleSet,
) -> None:
    """零成交 + 没成本：两个字段都不会因为"分母是 0"或"缺成本"被算成 0。"""
    metrics = compute_metrics(_raw(gmv_cents=0, by_sku={}, paid_order_count=0), cost_table={})
    assert metrics["gross_margin"] is None
    assert metrics["cost_coverage_ratio"] is None
    assert "loss_making" not in _hits(metrics, repo_rules)


# --------------------------------------------------------------------------------------
# 4) 覆盖率低于下限 → 即使毛利率很低也不触发
# --------------------------------------------------------------------------------------


def test_low_coverage_blocks_rule_even_with_very_negative_margin(repo_rules: RuleSet) -> None:
    """毛利率 -50% 但只覆盖 10% 成交额 → 不触发；覆盖率 60% 才触发。

    手算（低覆盖）：
        sku_a 成交 10000，卖 5 件，成本 3000/件 → 成本额 15000
        毛利率 = (10000 - 15000) / 10000 = -0.5
        覆盖率 = 10000 / 100000 = 0.1  < 0.6 → **不触发**
    手算（覆盖达标）：
        sku_a 成交 60000，卖 30 件，成本 3000/件 → 成本额 90000
        毛利率 = (60000 - 90000) / 60000 = -0.5
        覆盖率 = 60000 / 100000 = 0.6  >= 0.6 → 触发（正好压在下限上）
    """
    low_coverage = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 10_000, "units": 5},
                     "sku_b": {"gmv_cents": 90_000, "units": 20}}),
        cost_table={"sku_a": 3_000},
    )
    assert low_coverage["gross_margin"] == -0.5
    assert low_coverage["cost_coverage_ratio"] == 0.1
    assert "loss_making" not in _hits(low_coverage, repo_rules), (
        "覆盖率 10% 时的毛利率只代表少数成交，拿它给全店改价就是误伤"
    )

    at_floor = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 60_000, "units": 30},
                     "sku_b": {"gmv_cents": 40_000, "units": 10}}),
        cost_table={"sku_a": 3_000},
    )
    assert at_floor["gross_margin"] == -0.5
    assert at_floor["cost_coverage_ratio"] == 0.6
    assert "loss_making" in _hits(at_floor, repo_rules)


def test_positive_margin_does_not_trigger(repo_rules: RuleSet) -> None:
    """覆盖率 100% 但毛利率为正 → 不触发（卖得贵不是问题）。"""
    metrics = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 100_000, "units": 10}}),
        cost_table={"sku_a": 1_000},  # 成本 10000，毛利率 0.9
    )
    assert metrics["gross_margin"] == 0.9
    assert "loss_making" not in _hits(metrics, repo_rules)


# --------------------------------------------------------------------------------------
# 5) 分母为 0 → None（不是 ZeroDivisionError，也不是 0）
# --------------------------------------------------------------------------------------


def test_zero_denominators_return_none() -> None:
    """两个分母各自为 0 时的语义：都不许抛异常、不许返回 0。"""
    # (a) 有成本的 SKU 今天一件没卖：毛利率分母 0 → None；覆盖率是真 0.0
    no_sales = compute_metrics(
        _raw(by_sku={"sku_a": {"gmv_cents": 0, "units": 0}}),
        cost_table={"sku_a": 1_000},
    )
    assert no_sales["gross_margin"] is None
    assert no_sales["cost_coverage_ratio"] == 0.0

    # (b) 全店零成交：覆盖率分母 0 → None
    zero_gmv = compute_metrics(_raw(gmv_cents=0, by_sku={}), cost_table={"sku_a": 1_000})
    assert zero_gmv["gross_margin"] is None
    assert zero_gmv["cost_coverage_ratio"] is None

    # (c) 没有按 SKU 归集（老快照/历史桶）：无法归属 → 两个都 None，而不是"毛利 0"
    legacy = compute_metrics(_raw(by_sku=None), cost_table={"sku_a": 1_000})
    assert legacy["gross_margin"] is None
    assert legacy["cost_coverage_ratio"] is None


def test_revenue_without_units_is_rejected_not_silently_discounted() -> None:
    """有成交额却没有件数 → 成本摊不出去，边界上炸（静默按 0 件算会把成本算成 0）。"""
    with pytest.raises(ValueError) as excinfo:
        compute_metrics(
            _raw(by_sku={"sku_a": {"gmv_cents": 10_000, "units": 0}}),
            cost_table={"sku_a": 1_000},
        )
    assert "units" in str(excinfo.value)


def test_coverage_is_not_clamped_so_data_inconsistency_is_visible() -> None:
    """覆盖率 > 1 说明上游汇总与订单明细对不上：如实暴露，不用 min() 抹平。"""
    metrics = compute_metrics(
        _raw(gmv_cents=1_000, by_sku={"sku_a": {"gmv_cents": 2_000, "units": 1}}),
        cost_table={"sku_a": 100},
    )
    assert metrics["cost_coverage_ratio"] == 2.0
    assert metrics["gross_margin"] == 0.95  # (2000 - 100) / 2000


def test_bad_cost_table_values_are_rejected_at_the_metric_boundary() -> None:
    """`compute_metrics` 是公开纯函数：调用方直接传字典时也要在边界炸。"""
    raw = _raw(by_sku={"sku_a": {"gmv_cents": 10_000, "units": 1}})
    for bad in (10.5, "100", True, 0, -1):
        with pytest.raises(ValueError):
            compute_metrics(raw, cost_table={"sku_a": bad})


def test_malformed_by_sku_is_rejected() -> None:
    raw = _raw()
    raw["order_summary"]["by_sku"] = {"sku_a": 123}
    with pytest.raises(ValueError) as excinfo:
        compute_metrics(raw, cost_table={"sku_a": 100})
    assert "映射" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# 6) `collect` 侧的按 SKU 归集（成本口径的数据源）
# --------------------------------------------------------------------------------------


def _orders_payload(orders: list[dict[str, Any]]) -> dict[str, Any]:
    return {"shop_order_list": orders}


def test_broken_order_batch_contract_raises_instead_of_zeroing_metrics() -> None:
    """**整批**形状不对 → 抛错（硬失败）；空列表/`null` 是合法空结果。

    这是 F1 判据的另一半（`_aggregate_orders` docstring 的分界线）：一行的数据脏 → 逐行拒绝；
    整批的契约坏了 → 硬失败。若这里退化成 `[]`，`collect` 会把 `{"oops": 1}` 聚合成
    "今天 0 单 0 元"——一次上游改结构被伪装成生意归零，所有基于 gmv 的规则静默失效。
    """
    for payload, pattern in (
        (None, "必须是映射或列表"),
        ("oops", "必须是映射或列表"),
        ({"oops": 1}, "缺少订单列表字段"),
        ({"shop_order_list": "oops"}, "必须是 list"),
    ):
        with pytest.raises(ValueError, match=pattern):
            _aggregate_orders(payload, window_start=DEMO_DAY, window_end=DEMO_DAY)

    empty = _aggregate_orders(
        {"shop_order_list": []}, window_start=DEMO_DAY, window_end=DEMO_DAY
    )
    assert empty["gmv_cents"] == 0 and empty["order_count"] == 0
    assert empty["by_date"] == {} or empty["order_count"] == 0  # 窗口内的空桶不算"有订单"
    # 上游对空结果返回 null 是常见写法：按空列表处理，不能因此把整轮判死（那是误伤）。
    null_result = _aggregate_orders(
        {"shop_order_list": None}, window_start=DEMO_DAY, window_end=DEMO_DAY
    )
    assert null_result["order_count"] == 0
    # 裸 list 也是合法容器（`_as_list` 的既有契约）
    bare = _aggregate_orders([], window_start=DEMO_DAY, window_end=DEMO_DAY)
    assert bare["order_count"] == 0


def test_aggregate_orders_groups_revenue_and_units_by_sku() -> None:
    """只统计**已支付**的订单行；没有 sku_id 的行进全店 GMV 但不进 by_sku（= 未覆盖）。"""
    aggregate = _aggregate_orders(
        _orders_payload(
            [
                {"sku_id": "sku_a", "pay_amount": 19_900, "pay_time": f"{DEMO_DAY} 09:00:00",
                 "create_time": f"{DEMO_DAY} 09:00:00"},
                {"sku_id": "sku_a", "pay_amount": 19_900, "product_count": 2,
                 "create_time": f"{DEMO_DAY} 10:00:00"},
                # 未支付（没有 pay_amount / pay_time）：不进 GMV，也不进 by_sku
                {"sku_id": "sku_a", "create_time": f"{DEMO_DAY} 11:00:00"},
                # 没有 sku_id 的已支付订单：全店 GMV 计入，但无法归属到某个 SKU
                {"pay_amount": 5_000, "create_time": f"{DEMO_DAY} 12:00:00"},
            ]
        ),
        window_start=DEMO_DAY,
        window_end=DEMO_DAY,
    )
    assert aggregate["gmv_cents"] == 19_900 + 19_900 + 5_000
    assert aggregate["by_sku"] == {"sku_a": {"gmv_cents": 39_800, "units": 3}}
    # 历史桶里也带同一份 by_sku（当天桶就是同一个对象），历史指标因此口径一致
    assert aggregate["by_date"][DEMO_DAY]["by_sku"] == aggregate["by_sku"]


def test_order_units_defaults_to_one_and_records_every_degradation() -> None:
    """件数缺失/非正按 1 件，而且**降级现场必须可追溯**；类型错不许按 1 件猜。

    方向（v1.5.1 更正了写反的注释）：件数偏小 → 成本额偏小 → 毛利率偏高 → 更不容易报亏本，
    所以这是**偏乐观**的假设，后果是**漏报**（旧注释写成了"偏保守"）。

    `_aggregate_orders` 把现场记进 `degraded`，`collect` 据此写 warn 审计——没有这条留痕的话，
    "为什么这次没报亏本"在日志里完全无法解释。

    v1.5.1 的样本 schema（缺件数与件数非正**统一**，且都带 `order_id`）：
    `{order_id, where, keys, key, value, reason}`。只给页内下标 `order[3]` 的话，换个 run /
    重试之后就无法回到具体哪一单（下标会变），审计也就没法"事后定位"。
    """
    degraded: list[dict[str, Any]] = []
    assert _order_units({}, where="order[0]", order_id="o_1", degraded=degraded) == 1
    assert degraded == [
        {
            "order_id": "o_1",
            "where": "order[0]",
            "keys": ["product_count", "goods_count", "quantity"],
            "key": None,
            "value": None,
            "reason": "missing",
        }
    ]

    degraded.clear()
    assert (
        _order_units(
            {"product_count": 0}, where="order[1]", order_id="o_2", degraded=degraded
        )
        == 1
    )
    assert degraded == [
        {
            "order_id": "o_2",
            "where": "order[1]",
            "keys": ["product_count", "goods_count", "quantity"],
            "key": "product_count",
            "value": 0,
            "reason": "non_positive",
        }
    ]
    # 两种 reason 的键集合必须**完全一致**：消费方按同一套键读，不该为"有没有 key/value"分叉。
    non_positive_sample = degraded[0]
    degraded.clear()
    assert _order_units({}, where="order[6]", order_id="o_6", degraded=degraded) == 1
    assert set(degraded[0]) == set(non_positive_sample)
    assert degraded[0]["reason"] == "missing"
    assert degraded[0]["order_id"] == "o_6"

    degraded.clear()
    assert _order_units({"quantity": -3}, where="order[2]", degraded=degraded) == 1
    assert degraded[0]["reason"] == "non_positive"

    # 正常值不受影响、也不留痕
    degraded.clear()
    assert _order_units({"product_count": 3}, where="order[3]", degraded=degraded) == 3
    assert _order_units({"quantity": 2}, where="order[4]", degraded=degraded) == 2
    assert degraded == []

    # 不传 degraded 也不能抛（调用方漏传不该变成第二个 bug）
    assert _order_units({"product_count": 0}, where="order[5]") == 1

    # 类型错**不降级成 1 件**：那是"上游改了字段类型"，静默取 1 会把"每单只买一件"永久藏起来。
    # （调用方 `_read_order_row` 会把它变成"整行拒绝 + 审计"，见 test_node_audit_sink 的用例。）
    for bad in (1.5, "3", True):
        with pytest.raises(ValueError):
            _order_units({"product_count": bad}, where="order[0]")


def test_missing_unit_count_biases_margin_optimistic_not_conservative() -> None:
    """注释里那个算例必须真的成立（"注释写反"这类缺陷只能靠可执行的算例钉住）。

    demo 夹具的真丝衬衫成本 28000 分/件；一行 `pay_amount=199000`（10 件 × 199 元）但**没有**
    件数字段 → 按 1 件算：

        按 1 件（错）：(199000 - 28000×1)  / 199000 =  0.859296 → **+0.8593**
        按 10 件（对）：(199000 - 28000×10) / 199000 = -0.407035 → **-0.4070**
        偏差 = 0.859296 - (-0.407035) = 1.266331 → **+126.6 个百分点**

    方向是"亏本被看成暴利"，也就是**漏报**——这正是"偏保守"那句注释写反的地方。
    """
    aggregate = _aggregate_orders(
        _orders_payload(
            [
                {
                    "sku_id": "sku_a",
                    "pay_amount": 199_000,
                    "create_time": f"{DEMO_DAY} 09:00:00",
                }
            ]
        ),
        window_start=DEMO_DAY,
        window_end=DEMO_DAY,
    )
    assert aggregate["by_sku"]["sku_a"]["units"] == 1, "缺件数 → 降级成 1 件"
    assert aggregate["units_degraded"][0]["reason"] == "missing"

    metrics = compute_metrics(
        _raw(gmv_cents=199_000, by_sku=aggregate["by_sku"]),
        cost_table={"sku_a": 28_000},
    )
    biased = metrics["gross_margin"]
    correct = round((199_000 - 28_000 * 10) / 199_000, 6)
    assert biased == round((199_000 - 28_000) / 199_000, 6) == 0.859296
    assert correct == -0.407035
    assert round((biased - correct) * 100, 1) == 126.6
    assert biased > correct, "偏乐观：算出来的毛利率偏高（亏本被看成暴利）"


def test_zero_revenue_paid_row_is_kept_out_of_the_cost_margin() -> None:
    """有 `pay_time` 但成交额为 0 的行**不进成本口径**，否则会虚增成本、误报亏本。

    算例（与 `nodes._aggregate_orders` docstring 一致）：
        sku_a：成交 120000、2 件、成本 30000/件 → 成本额 60000
        sku_b：一行"0 元单"（`pay_time` 有、`pay_amount` 没有）、1 件、成本 80000/件
        不计 sku_b → 毛利率 = (120000 - 60000) / 120000 = **+0.5**
        计入 sku_b → 毛利率 = (120000 - 140000) / 120000 = **-0.1667**（足以误触发 loss_making）

    GMV 口径不变：这一行仍然是"已支付"（`paid_order_count` +1、`gmv_cents` +0）——那是另一件事，
    不能为了成本口径好看而把它从订单数里删掉。

    v1.5.1：样本补 `order_id` / `units` / `unit_cost_cents`（`cost_table` 传了才有）。只报"有 N 行
    没进成本口径"是没法排序的——`units × unit_cost_cents` 才是"这一行漏了多少成本"。
    """
    aggregate = _aggregate_orders(
        _orders_payload(
            [
                {
                    "order_id": "o_paid",
                    "sku_id": "sku_a",
                    "pay_amount": 120_000,
                    "product_count": 2,
                    "create_time": f"{DEMO_DAY} 09:00:00",
                },
                {
                    "order_id": "o_zero",
                    "sku_id": "sku_b",
                    "pay_time": f"{DEMO_DAY} 10:00:00",
                    "product_count": 1,
                    "create_time": f"{DEMO_DAY} 10:00:00",
                },
            ]
        ),
        window_start=DEMO_DAY,
        window_end=DEMO_DAY,
        cost_table={"sku_a": 30_000, "sku_b": 80_000},
    )
    assert aggregate["gmv_cents"] == 120_000
    assert aggregate["paid_order_count"] == 2
    assert aggregate["by_sku"] == {"sku_a": {"gmv_cents": 120_000, "units": 2}}
    assert aggregate["zero_revenue_paid"] == [
        {
            "order_id": "o_zero",
            "where": "order[1]",
            "sku_id": "sku_b",
            "units": 1,
            "unit_cost_cents": 80_000,
        }
    ]
    # 样本自证"漏了多少成本"：1 件 × 80000 分 = 800 元成本没有进毛利率。
    leaked = aggregate["zero_revenue_paid"][0]
    assert leaked["units"] * leaked["unit_cost_cents"] == 80_000

    metrics = compute_metrics(
        _raw(gmv_cents=aggregate["gmv_cents"], by_sku=aggregate["by_sku"]),
        cost_table={"sku_a": 30_000, "sku_b": 80_000},
    )
    assert metrics["gross_margin"] == 0.5, "0 元单不许把 +0.5 拖成 -0.1667"


def test_by_sku_flows_from_collect_aggregation_into_metrics() -> None:
    """端到端（纯函数层）：订单 → by_sku → 毛利率，覆盖与手算一致。

    手算：sku_a 成交 39800，卖 3 件，成本 10000/件 → 成本 30000
          sku_other 成交 5000（无成本）
          GMV = 44800
          毛利率 = (39800 - 30000) / 39800 = 0.246231（6 位小数）
          覆盖率 = 39800 / 44800 = 0.888393
    """
    aggregate = _aggregate_orders(
        _orders_payload(
            [
                {"sku_id": "sku_a", "pay_amount": 19_900, "create_time": f"{DEMO_DAY} 09:00:00"},
                {"sku_id": "sku_a", "pay_amount": 19_900, "product_count": 2,
                 "create_time": f"{DEMO_DAY} 10:00:00"},
                {"pay_amount": 5_000, "create_time": f"{DEMO_DAY} 12:00:00"},
            ]
        ),
        window_start=DEMO_DAY,
        window_end=DEMO_DAY,
    )
    metrics = compute_metrics(
        _raw(gmv_cents=aggregate["gmv_cents"], by_sku=aggregate["by_sku"]),
        cost_table={"sku_a": 10_000},
    )
    assert metrics["gross_margin"] == round((39_800 - 30_000) / 39_800, 6)
    assert metrics["cost_coverage_ratio"] == round(39_800 / 44_800, 6)


def test_unmatched_cost_entries_are_reported_but_do_not_block() -> None:
    """§8.5 要求"对不上的成本条目不能静默忽略"：能判断时给出名单，判不了时不制造假警报。

    启动期没有 `raw.products`，所以这条只能运行期查；商品列表为空（采集降级）时
    所有条目都会"看起来对不上"，那时候报出来只会淹掉真问题。
    """
    products = [{"sku_id": "sku_a"}, {"sku_id": "sku_b"}]
    assert _unmatched_cost_skus(products, {"sku_a": 100, "sku_zombie": 200}) == ["sku_zombie"]
    assert _unmatched_cost_skus(products, {"sku_a": 100}) == []
    assert _unmatched_cost_skus([], {"sku_a": 100}) == [], "没有商品列表 ≠ 全部对不上"


# --------------------------------------------------------------------------------------
# 7) 成本表加载器：加载期校验（§8.5）
# --------------------------------------------------------------------------------------


def _write_costs(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sku_costs.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_cost_file_loads_costs_and_effective_from(tmp_path: Path) -> None:
    path = _write_costs(
        tmp_path,
        """
version: "1"
effective_from: "2026-10-23"
costs:
  "3400000000000000002": 8200
  "3400000000000000003": 4500
""",
    )
    config = load_sku_cost_config(path)
    assert config.as_table() == {"3400000000000000002": 8_200, "3400000000000000003": 4_500}
    assert config.effective_from == "2026-10-23"
    assert load_sku_costs(path) == config.as_table()  # 便捷入口返回同一份映射（拷贝）


def test_missing_cost_file_is_not_an_error_but_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """文件缺失 = "这家店没配成本"，属于正常状态：返回空表 + 一条带 event 的 warn。"""
    missing = tmp_path / "nope" / "sku_costs.yaml"
    with caplog.at_level("WARNING", logger="app.services.costs"):
        table = load_sku_costs(missing)
    assert table == {}
    events = [
        str(getattr(record, "event", ""))
        for record in caplog.records
        if record.name == "app.services.costs"
    ]
    assert "config.sku_costs_missing" in events, "缺失必须可见（日志页能查到），不能静默"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        # 负数：成本为负等于"卖一件赚两件的钱"，一定是写错了
        ('costs:\n  "sku_a": -100\n', "> 0"),
        # 0：会把毛利率算成 100%，而"没配成本"应当直接不写这一条
        ('costs:\n  "sku_a": 0\n', "> 0"),
        # float：分与元的歧义（§0 金额一律整数分）
        ('costs:\n  "sku_a": 10.5\n', "整数分"),
        # 字符串：同上
        ('costs:\n  "sku_a": "100"\n', "整数分"),
        # bool：`True` 在 Python 里是 int，必须显式拒收
        ('costs:\n  "sku_a": true\n', "整数分"),
        # 空值：绝不能读成 0 成本
        ('costs:\n  "sku_a":\n', "整数分"),
        # 重复键：PyYAML 默认静默取后者，成本会被无声覆盖
        ('costs:\n  "sku_a": 100\n  "sku_a": 200\n', "重复键"),
        # 键不是字符串（YAML 里没加引号的纯数字 → int）：与上游 str sku_id 永远对不上
        ("costs:\n  3400000000000000002: 100\n", "sku_id 字符串"),
        # 段名拼错：静默忽略会让成本表"看起来没生效"
        ("cost:\n  sku_a: 100\n", "未知字段"),
        # 没有 costs 段
        ('version: "1"\n', "缺少 costs"),
        # effective_from 不是日期
        ('effective_from: "去年"\ncosts:\n  "sku_a": 100\n', "effective_from"),
        # 空文件
        ("# 只有注释\n", "成本表是空的"),
    ],
)
def test_bad_cost_file_raises_config_error(tmp_path: Path, body: str, reason: str) -> None:
    path = _write_costs(tmp_path, body)
    with pytest.raises(ConfigError) as excinfo:
        load_sku_costs(path)
    assert reason in str(excinfo.value), str(excinfo.value)


def test_non_utf8_cost_file_is_a_config_error(tmp_path: Path) -> None:
    """非 UTF-8（财务另存成 GBK 之类）→ ConfigError：不"尽力而为"地读半份成本。"""
    path = tmp_path / "sku_costs.yaml"
    path.write_bytes(b'costs:\n  "sku_a": 100\n\xff\xfe\n')
    with pytest.raises(ConfigError) as excinfo:
        load_sku_costs(path)
    assert "UTF-8" in str(excinfo.value)


def test_yaml_merge_key_only_works_when_it_adds_new_skus(tmp_path: Path) -> None:
    """`<<` 合并键的**真实**行为（v1.5.1 实测校正，旧注释把它写反过）。

    实测两件事：

    1. 合并块**只提供新增键** → 正常生效（`<<` 被 `flatten_mapping` 展开成真正的键值对）；
    2. 合并块与**显式键同名** → `ConfigError`，**不是** YAML 语义里的"显式键胜出"：
       PyYAML 6 的 `flatten_mapping` 把合并来的键**前置**进 `node.value` 且不按显式键去重，
       同一个键于是在展开后的列表里出现两次，重复键检测拦住它。

    第 2 条不去"修好"成了不起眼的覆盖语义：这张表是扁平的 `sku_id -> 整数分`，`<<` 在这里
    唯一能做的就是让某条成本**有两处来源**，而"成本被无声覆盖"正是加载器要防的事故。
    错误信息里要说明是"合并键造成的"，否则人会对着"重复键"去找自己明明只写了一次的 sku。
    """
    merged = _write_costs(tmp_path, 'costs:\n  <<: {"sku_a": 100}\n  "sku_b": 200\n')
    assert load_sku_costs(merged) == {"sku_a": 100, "sku_b": 200}

    conflict = tmp_path / "conflict.yaml"
    conflict.write_text('costs:\n  <<: {"sku_a": 999}\n  "sku_a": 100\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_sku_costs(conflict)
    message = str(excinfo.value)
    assert "重复键" in message
    assert "合并键" in message, "错误信息必须点明是 `<<` 合并造成的，否则人找不到自己写的那一处"


def test_parse_sku_costs_accepts_plain_dict_for_callers() -> None:
    """`parse_sku_costs` 是纯校验入口（测试/装配可以直接喂字典，不必落一个文件）。

    重复 sku_id 只能在 YAML 加载层拦——Python 的 dict 在构造时就已经把重复键合并掉了，
    那时候"有没有重复"这个信息已经不存在。所以重复检测的用例走文件路径（见上一条）。
    """
    parsed = parse_sku_costs({"costs": {"sku_a": 100}}, source="<memory>")
    assert parsed.as_table() == {"sku_a": 100}
    assert parsed.effective_from is None
    with pytest.raises(ConfigError):
        parse_sku_costs({"costs": {"sku_a": 100}, "costs_extra": {}}, source="<memory>")


# --------------------------------------------------------------------------------------
# 8) 规则本身：命名空间、severity、alert-only
# --------------------------------------------------------------------------------------


def test_loss_making_rule_shape_is_registered(repo_rules: RuleSet) -> None:
    """`loss_making` 必须在仓里的规则集中，且满足设计约束。

    - `metric` 在主指标命名空间内（否则 `load_rules` 早就炸了，这里顺带钉住）；
    - severity 是现有枚举里最高的那一档（`critical`，与 refund_spike 同级）；
    - **没有 actions**：店铺级指标指不出是哪件货在亏，任何自动动作都会猜错目标
      （`config/rules.yaml` 里写了完整理由）。这条断言防止有人后来"顺手"加个降价动作
      把亏损品降得更亏。
    """
    rule = repo_rules.get("loss_making")
    assert rule.metric == "gross_margin"
    assert rule.metric in ALLOWED_NAMES
    assert {"gross_margin", "cost_coverage_ratio"} <= ALLOWED_NAMES
    assert str(rule.severity) == "critical"
    assert rule.actions == ()
    assert "cost_coverage_ratio" in rule.when.names, "覆盖率下限必须是规则的一部分，不能只写在注释里"
    assert "gross_margin" in rule.when.names


def test_repo_rules_yaml_still_loads(repo_rules: RuleSet) -> None:
    """改过 `rules.yaml` 之后，仓里那份仍然能被加载（否则线上启动就没规则可用）。"""
    assert repo_rules.version == "1"
    assert len(repo_rules.rules) >= 5
    assert "gmv_drop_day" in {rule.id for rule in repo_rules.rules}
