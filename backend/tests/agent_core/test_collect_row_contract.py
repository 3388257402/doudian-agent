"""`collect` 的行级契约（v1.5.1）：脏行**整行拒绝**、全被拒**硬失败**、拒绝率**可观测**。

这组用例补的是 F1b/F1 之间的四个缝（独立审查在 HEAD `1b81fc3` 上实测出）：

1. **P0-1 时间戳越界 500**：`create_time=9999999999999999` 让 `_business_date_of` 里的
   `datetime.fromtimestamp` 在 Windows 抛 `OSError`，而 `_aggregate_orders` 只接
   `(ValueError, TypeError)` → 逃出 `collect` → 图标 failed 后 re-raise → `POST /api/agent/run`
   **500**。平台相关（Linux 上可能是 ValueError 被接住）→ CI 绿、线上红。
2. **P0-2 解析不出的日期被算进"今天"**：`create_time="2026-13-45 99:99:99"` 回退
   `fallback=window_end` → 垃圾日期虚增当天 GMV。与"猜不出含义就整行拒绝"相反。
3. **P1 全行拒绝 → 静默 0 单 0 元**：两行 `pay_amount` 都是字符串时，旧实现是
   `status=None / partial=False / gmv=0 / orders=0 / errors=[]`——零告警的"生意归零"。
4. **P2 元素形状错被静默丢弃**：`{"shop_order_list": ["x", 1, None]}` 经 `_as_list` 过滤后
   变成空列表，只有一条 `collect.done`；`sku_id` 是 dict 时被 `str()` 成 `"{'a': 1}"` 当 SKU 名。

契约：**看到了行但一行都没解析出来 ≠ 今天没卖东西**。全被拒走 `_BatchContractError` 同一条
路径（`status=failed`，不 500）；部分被拒保持 warn + `source["partial"]=True`；拒绝率
（`rows_seen` / `rows_accepted`）进 `collect.done` 的审计。

v1.5.2 补的七个洞（都在同一个"行级契约"上，因此并进本文件而不是另开一套夹具）：

5. **falsy `create_time` 被算成今天**：`item.get("create_time") or item.get("pay_time")` 在 strict
   判定**之前**折掉 falsy 值，而 strict 对 `None` 仍然回退当日 → `False` / `0` / `""` 都变成
   "今天成交 + GMV 全额计入"（上一轮声称修掉的那个现象，换了个形状回来）。
6. **日期只看第一个字段**：`create_time="0000-00-00 00:00:00"` 配合法 `pay_time` 时整行被丢
   （GMV 少算）——"逐字段尝试"是同一个上游的另一个出口。
7. **行级"一个已知字段都没有"零告警**：`[{}, {}, {}]` → `order_count=3 / gmv=0 /
   rows_rejected=0 / partial=False`。批层有该判据，行层没有 → `no_known_fields`。
8. **代码 bug 被降级成数据脏**：把 `except ValueError` 变异成 `except Exception` 时全部用例照旧
   通过——`TypeError` 那条硬边界此前没有任何测试守护。
9. **`rows_seen - rows_accepted` 对不上账**：它 = 被拒行 **+ 窗口外行**，而
   `rows_out_of_window` 不在任何事件的 `data` 里 → "100-93=7"里有一个查不到的桶。
10. **falsy 日期回落的成功路径零留痕**：`create_time=False` + 合法 `pay_time` → 行被接受、
   用 pay_time 的日期，既不进 rejected 也不进任何审计 → "上游把 `create_time` 全写成 False"
   永远查不出来。
11. **`_best_effort_units` 的"唯一一处 TypeError"声明不成立**：`_business_date_of` 里还有三处
   `except (..., TypeError, ...)`；本版收窄为数据侧真正会抛的三种异常，并各加一条用例。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from app.agent import nodes
from app.agent.nodes import collect
from app.enums import RunStatus
from app.observability.logging import JsonlLogSink

from .conftest import TODAY, FakeDoudianClient, FakeResponse

RUN_ID = "run_collect_row_contract"


def _state(**overrides: Any) -> dict[str, Any]:
    """`collect` 真正读的最小 state（其余键留空，避免顺手跑别的逻辑）。"""
    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "trace_id": "trace_collect_row_contract",
        "shop_id": "12345678",
        "status": RunStatus.RUNNING.value,
        "raw": None,
        "errors": [],
        "audit": [],
    }
    state.update(overrides)
    return state


def _config(runtime: Any) -> dict[str, Any]:
    from .conftest import FIXED_NOW

    return {"configurable": {"now": FIXED_NOW, "runtime": runtime}}


def _good_row(order_id: str = "o_good", *, pay_amount: int = 9_900) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "sku_id": "sku_a",
        "pay_amount": pay_amount,
        "product_count": 1,
        "create_time": f"{TODAY} 10:00:00",
    }


def _rejected(update: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in update["audit"] if entry["event"] == "collect.row_rejected"]


def _done(update: dict[str, Any]) -> dict[str, Any]:
    entries = [entry for entry in update["audit"] if entry["event"] == "collect.done"]
    assert len(entries) == 1, [entry["event"] for entry in update["audit"]]
    return entries[0]["data"]


def _assert_row_accounting(data: dict[str, Any], *, rejected_field: str = "rows_rejected") -> None:
    """v1.5.2 的行级账目恒等式：看到 = 接受 + 被拒 + 窗口外。

    四个数必须同时出现；少任何一个，`rows_seen - rows_accepted` 就会包含一个查不到的桶。
    `collect.row_rejected` 的"被拒"项就是它自己的 `count`（该事件本身只汇总被拒行），
    所以调用方传 `rejected_field="count"`——不在同一行里重复一个等值的 `rows_rejected` 键。
    """
    assert data["rows_seen"] == (
        data["rows_accepted"] + data[rejected_field] + data["rows_out_of_window"]
    ), data


# ======================================================================================
# P0-1 / P0-2：行级日期解析（strict）
# ======================================================================================


async def test_out_of_range_timestamp_rejects_the_row_instead_of_http_500(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`create_time=9999999999999999` → `bad_create_time` 整行拒绝，**不**从节点逃出去。

    值本身越界（毫秒/秒换算后是公元 30 万年），任何平台都表示不出来。旧实现里 Windows 上
    `datetime.fromtimestamp` 抛 `OSError`（`[Errno 22] Invalid argument`），只被
    `graph.ainvoke` 的 `except Exception` 兜住 → 标 failed 再 re-raise → API 500；Linux 上
    可能抛 `ValueError` 被行级 `except` 接住 → 同一个入口两种行为（CI 绿、线上红）。
    """
    fake_client.orders = [
        {
            "order_id": "o_epoch_overflow",
            "sku_id": "sku_a",
            "pay_amount": 100,
            "create_time": 9_999_999_999_999_999,  # 越界时间戳
        },
        _good_row(),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    # 节点没有 failed（旧实现这里会是 status=failed + 异常逃出 → 500）
    assert update.get("status") != RunStatus.FAILED.value
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 9_900, "越界时间戳那一行的 100 分不许进 GMV"
    assert summary["order_count"] == 1

    rejected = _rejected(update)
    assert len(rejected) == 1
    data = rejected[0]["data"]
    assert data["count"] == 1
    assert data["reasons"] == {"bad_create_time": 1}
    sample = data["samples"][0]
    assert sample["order_id"] == "o_epoch_overflow"
    assert sample["reason"] == "bad_create_time"
    assert "时间戳" in sample["error"]
    # 有行被拒 = 这份采集不完整：raw 快照必须自证（P3）
    assert update["raw"]["source"]["partial"] is True


async def test_unparseable_create_time_is_rejected_instead_of_counted_as_today(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`create_time="2026-13-45 99:99:99"` → 整行拒绝，**绝不**回退成今天。

    旧实现回退 `fallback=window_end`（今天），垃圾日期被当成一笔当日成交（审查实测
    `gmv=555 / orders=1`）。这与"猜不出含义就整行拒绝"正好相反，而且是**虚增**。
    """
    fake_client.orders = [
        {
            "order_id": "o_bad_date",
            "sku_id": "sku_a",
            "pay_amount": 555,
            "create_time": "2026-13-45 99:99:99",
        },
        _good_row(),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 9_900, "无法解析的日期不许被算进今天（旧实现是 555）"
    assert summary["order_count"] == 1
    assert summary["paid_order_count"] == 1

    data = _rejected(update)[0]["data"]
    assert data["reasons"] == {"bad_create_time": 1}
    assert data["samples"][0]["order_id"] == "o_bad_date"
    assert data["samples"][0]["reason"] == "bad_create_time"
    assert update["raw"]["source"]["partial"] is True


# ======================================================================================
# P1：三个区间的拒绝率（全拒 → failed；部分被拒 → warn + partial；零行才是真的零）
# ======================================================================================


async def test_all_rows_rejected_fails_like_a_broken_batch_instead_of_zero_metrics(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """两行 `pay_amount` 都是字符串 → **failed**（不 500），不是静默的 0 单 0 元。

    审查实测的旧状态：`status=None / partial=False / gmv=0 / orders=0 / anomalies=[] /
    errors=[]`，run 正常 done、零告警。这正是 F1b 要避免的状态，只是从"整批契约坏"降级成
    "逐行契约坏"。判据是 `rows_seen > 0 且 rows_accepted == 0`，走同一条
    `_BatchContractError` 路径（记明确错误 + `status=failed`）。
    """
    fake_client.orders = [
        {"order_id": "o1", "sku_id": "sku_a", "pay_amount": "199.00",
         "create_time": f"{TODAY} 09:00:00"},
        {"order_id": "o2", "sku_id": "sku_a", "pay_amount": "299.00",
         "create_time": f"{TODAY} 11:00:00"},
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") == RunStatus.FAILED.value
    assert "raw" not in update, "一行都没解析出来时不许交出一份全 0 的 raw"
    messages = [str(item["message"]) for item in update["errors"]]
    assert any("订单行全部被拒绝" in text for text in messages), messages
    # 文案要说清"一行都没解析出来 ≠ 今天没卖东西"
    assert any("一行都没解析出来 ≠ 今天没卖东西" in text for text in messages), messages

    failed = [entry for entry in update["audit"] if entry["event"] == "collect.failed"]
    assert len(failed) == 1
    data = failed[0]["data"]
    assert data["rows_seen"] == 2
    assert data["rows_accepted"] == 0
    assert data["rows_rejected"] == 2
    assert data["reject_reasons"] == {"bad_pay_amount": 2}
    assert data["source"]["partial"] is True


async def test_partially_rejected_rows_stay_a_warning_with_partial_source(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """中间区间：一行坏、一行好 → 正常算指标 + warn + `source.partial=True`。

    旧实现里行级拒绝后 `partial` 仍是 False（审查实测）：`raw.source` 与 `collect.row_rejected`
    两个出口对"这次采集完不完整"的口径互相矛盾。
    """
    fake_client.orders = [
        {"order_id": "o_bad", "sku_id": "sku_a", "pay_amount": "199.00",
         "create_time": f"{TODAY} 09:00:00"},
        _good_row(pay_amount=19_900),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 19_900
    assert summary["order_count"] == 1
    assert update["raw"]["source"]["partial"] is True, "有行被拒时 raw.source 必须自证不完整"

    data = _rejected(update)[0]["data"]
    assert data["count"] == 1
    assert data["reasons"] == {"bad_pay_amount": 1}
    # 分母必须跟着分子：只看 `count=1` 分不清"1/2"还是"1/1000"（v1.5.2）。
    assert data["rows_seen"] == 2
    assert data["rows_accepted"] == 1
    assert data["excluded_from_metrics"] is True

    done = _done(update)
    assert done["rows_seen"] == 2
    assert done["rows_accepted"] == 1
    assert done["rows_rejected"] == 1
    assert done["partial"] is True


async def test_zero_rows_is_a_legitimate_empty_day_not_a_failure(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`orders=[]` → 合法空结果（真的没卖东西），不能因为 `rows_accepted == 0` 就误判 failed。

    阈值是"看到了行但一行都没接受"，`rows_seen == 0` 不在其中：空列表与"全部被拒"是两件事。
    """
    fake_client.orders = []

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 0
    assert summary["order_count"] == 0
    assert _rejected(update) == []
    assert update["raw"]["source"]["partial"] is False
    done = _done(update)
    assert (done["rows_seen"], done["rows_accepted"], done["rows_rejected"]) == (0, 0, 0)


async def test_all_rows_accepted_reports_full_acceptance_rate(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """全部被接受 → `rows_seen == rows_accepted`、`rows_rejected == 0`、`partial=False`。

    第三个区间的正向对照：拒绝率不是"有数就算"，分子分母都要对。
    """
    fake_client.orders = [_good_row("o1"), _good_row("o2", pay_amount=19_900)]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    assert update["raw"]["source"]["partial"] is False
    done = _done(update)
    assert done["rows_seen"] == 2
    assert done["rows_accepted"] == 2
    assert done["rows_rejected"] == 0


# ======================================================================================
# P2：元素形状错 / sku_id 形状错
# ======================================================================================


async def test_non_object_order_elements_are_counted_as_rejected_rows(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """`["x", 1, None]` 不是"没给数据"，是"给了但没解析出来" → `not_an_object` 计入拒绝率。

    旧实现经 `_as_list` 静默过滤成空列表：`gmv=0 / orders=0`，只有一条 `collect.done`。
    """

    async def _raw_orders(**_kwargs: Any) -> FakeResponse:
        # 夹具的 search_orders 会 `dict(item)` 复制每一行，非对象元素过不去；这里直接给原始 payload。
        return FakeResponse(data={"shop_order_list": ["x", 1, None]})

    monkeypatch.setattr(fake_client, "search_orders", _raw_orders)

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") == RunStatus.FAILED.value
    messages = [str(item["message"]) for item in update["errors"]]
    assert any("订单行全部被拒绝" in text for text in messages), messages

    failed = next(entry for entry in update["audit"] if entry["event"] == "collect.failed")
    data = failed["data"]
    assert data["rows_seen"] == 3
    assert data["rows_accepted"] == 0
    assert data["reject_reasons"] == {"not_an_object": 3}


async def test_one_non_object_element_among_valid_rows_is_a_partial_rejection(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """混合：一行好、一行是字符串 → 好行照常算，坏元素记 `not_an_object`（不是静默丢弃）。"""

    async def _raw_orders(**_kwargs: Any) -> FakeResponse:
        return FakeResponse(data={"shop_order_list": [_good_row(), "oops"]})

    monkeypatch.setattr(fake_client, "search_orders", _raw_orders)

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    assert update["raw"]["order_summary"]["gmv_cents"] == 9_900
    data = _rejected(update)[0]["data"]
    assert data["reasons"] == {"not_an_object": 1}
    assert data["samples"][0]["reason"] == "not_an_object"
    assert data["samples"][0]["where"] == "order[1]"
    done = _done(update)
    assert (done["rows_seen"], done["rows_accepted"]) == (2, 1)


async def test_dict_sku_id_is_rejected_instead_of_becoming_a_fake_sku_name(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`sku_id={"a": 1}` → `bad_sku_id` 整行拒绝，不许被 `str()` 成 "{'a': 1}" 进 `by_sku`。

    那个假 SKU 名永远匹配不上成本表，只会把 `cost_coverage_ratio` 拉低而让人不明所以。
    """
    fake_client.orders = [
        {
            "order_id": "o_bad_sku",
            "sku_id": {"a": 1},
            "pay_amount": 299_000,
            "product_count": 1,
            "create_time": f"{TODAY} 09:00:00",
        },
        _good_row(pay_amount=19_900),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 19_900, "假 SKU 行的金额不许进 GMV"
    assert summary["by_sku"] == {"sku_a": {"gmv_cents": 19_900, "units": 1}}

    data = _rejected(update)[0]["data"]
    assert data["reasons"] == {"bad_sku_id": 1}
    sample = data["samples"][0]
    assert sample["order_id"] == "o_bad_sku"
    assert sample["reason"] == "bad_sku_id"
    assert "sku_id" in sample["error"]


# ======================================================================================
# P3：可观测性（样本截断 + staged 降级不被合并）
# ======================================================================================


async def test_row_rejection_samples_are_bounded_and_the_audit_says_so(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """7 条坏行 → `count=7` 但 `samples` 只有前 5 条，且审计显式给出 `sample_limit` /
    `samples_truncated`，文案写"仅前 5 条样本"。

    旧实现只给 `count` + `items[:5]`，文案说"样本见 data.samples"——读者很容易把
    "贴了 5 条"读成"一共 5 条"。
    """
    fake_client.orders = [
        {"order_id": f"o_bad_{index}", "sku_id": "sku_a", "pay_amount": "199.00",
         "create_time": f"{TODAY} 09:00:00"}
        for index in range(7)
    ] + [_good_row()]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    data = _rejected(update)[0]["data"]
    assert data["count"] == 7
    assert data["sample_limit"] == 5
    assert data["samples_truncated"] is True
    assert len(data["samples"]) == 5
    assert [sample["order_id"] for sample in data["samples"]] == [
        f"o_bad_{index}" for index in range(5)
    ]

    rows = sink.read(run_id=RUN_ID, event_prefix="collect.row_rejected")
    assert len(rows) == 1
    assert "仅前 5 条样本" in rows[0]["message"]
    assert "其余已截断" in rows[0]["message"]


async def test_staged_degradation_is_discarded_when_the_row_is_later_rejected(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """行内先出现"件数缺失"、随后 `refund_amount` 坏 → 整行拒绝，**件数降级不许被合并**。

    旧实现把降级现场先并进 `units_degraded`、再校验：一个后面又被拒的行会在审计里留下
    "缺失件数"的痕迹，可它根本没进任何指标——审计会自己骗自己。本用例钉住"接受后才合并"。
    """
    fake_client.orders = [
        {
            "order_id": "o_degraded_then_rejected",
            "sku_id": "sku_a",
            "pay_amount": 199_000,
            # 没有 product_count：解析到这里会往 staged_degraded 写一条 missing
            "refund_amount": "1.00",  # 随后金额解析失败 → 整行拒绝
            "create_time": f"{TODAY} 09:00:00",
        },
        _good_row(pay_amount=19_900),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    # 被拒行不许留下"件数降级"的痕迹：没有 units_degraded 审计
    assert [entry for entry in update["audit"] if entry["event"] == "collect.units_degraded"] == []
    assert sink.read(run_id=RUN_ID, event_prefix="collect.units_degraded") == []

    data = _rejected(update)[0]["data"]
    assert data["reasons"] == {"bad_refund_amount": 1}
    assert data["samples"][0]["order_id"] == "o_degraded_then_rejected"
    # 好行本身没有降级：只有一条 warn，且它不是件数降级
    assert update["raw"]["order_summary"]["by_sku"] == {
        "sku_a": {"gmv_cents": 19_900, "units": 1}
    }


@pytest.mark.parametrize("bad_units", [0, -1])
async def test_accepted_rows_with_units_degradation_mark_the_source_partial(
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
    bad_units: int,
) -> None:
    """被**接受**的行发生件数降级（0/负数 → 按 1 件）时，`source.partial` 也要是 True。

    `partial` 的含义是"这份采集不完整/有降级"，不是"有行被拒"。降级发生在被接受的行上，
    但 raw 快照同样不该声称这是一份干净数据。
    """
    fake_client.orders = [
        {
            "order_id": "o_unit_degraded",
            "sku_id": "sku_a",
            "pay_amount": 199_000,
            "product_count": bad_units,
            "create_time": f"{TODAY} 09:00:00",
        }
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    assert update["raw"]["source"]["partial"] is True
    degraded = [entry for entry in update["audit"] if entry["event"] == "collect.units_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["data"]["count"] == 1
    done = _done(update)
    assert done["rows_seen"] == done["rows_accepted"] == 1
    assert done["partial"] is True


# ======================================================================================
# v1.5.2：falsy 日期 / 逐字段尝试 / 行级"一个已知字段都没有" / 代码 bug 不许降级成数据脏
# ======================================================================================


@pytest.mark.parametrize("falsy_value", [False, 0, ""], ids=["false", "zero", "empty-string"])
async def test_falsy_create_time_is_rejected_instead_of_counted_as_today(
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
    falsy_value: Any,
) -> None:
    """`create_time` 是 falsy（`False` / `0` / `""`）→ `bad_create_time`，**绝不**回退今天。

    旧实现 `item.get("create_time") or item.get("pay_time")` 在 strict 判定**之前**就把 falsy
    折成了 `None`，而 strict 对 `None` 仍然回退 `window_end` → 这一行变成"今天的成交"、金额
    全额进 GMV（独立审查实测 `gmv=555 / orders=1`）。falsy 与"缺键 / 显式 `None`"是两件事：
    前者是"给了一个不可能是日期的值"（脏数据，整行拒绝），后者才是"没给日期"（缺数据，保持回退）。

    这里用一行坏 + 一行好：坏行被拒（金额 555 分不进 GMV），好行照常算，run 不 failed。
    """
    fake_client.orders = [
        {
            "order_id": "o_falsy_time",
            "sku_id": "sku_a",
            "pay_amount": 555,
            "product_count": 1,
            "create_time": falsy_value,
        },
        _good_row(),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 9_900, "falsy 日期那行的 555 分不许被算进今天"
    assert summary["order_count"] == 1, "它也不许被算成今天的一笔订单"

    data = _rejected(update)[0]["data"]
    assert data["reasons"] == {"bad_create_time": 1}
    sample = data["samples"][0]
    assert sample["order_id"] == "o_falsy_time"
    assert sample["reason"] == "bad_create_time"
    # 文案要自证"这是 falsy，不是缺日期"：否则读日志的人会去追上游为什么"没给时间"
    assert "falsy" in sample["error"]
    assert update["raw"]["source"]["partial"] is True


async def test_unusable_create_time_falls_back_to_pay_time_instead_of_dropping_the_row(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`create_time="0000-00-00 00:00:00"` + 合法 `pay_time` → 用 **pay_time 的日期**，不整行拒绝。

    旧实现只拿第一个非空字段（`create_time or pay_time` 取到的正是那个垃圾串），于是在 strict
    里整行被丢：这一行其实有可用日期，GMV 少算。日期在这两个字段上同义（都表示这单哪天成交），
    一个不可用就该试下一个，两个都不可用才拒绝。

    用**另一个业务日**（10-20，不是今天）才能证明取到的是 `pay_time` 的日期而不是回退的今天。

    v1.5.2 补上留痕：行被接受**不等于**这件事没发生。`create_time` 系统性坏掉、靠 `pay_time`
    兜住时，指标一切正常、业务日期口径却已经悄悄换了；所以必须有一条 `collect.date_fallback`
    （warn）且 `source.partial=True`，而它**不算** `row_rejected`（行是被接受的）。
    """
    fake_client.orders = [
        {
            "order_id": "o_pay_time_only",
            "sku_id": "sku_a",
            "pay_amount": 555,
            "product_count": 1,
            "create_time": "0000-00-00 00:00:00",
            "pay_time": "2026-10-20 11:00:00",
        },
        _good_row(),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    assert _rejected(update) == [], "有可用 pay_time 时不许整行拒绝"
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 9_900, "这一行的 555 分应落在 10-20 的桶里，不是今天"
    bucket = summary["by_date"]["2026-10-20"]
    assert bucket["gmv_cents"] == 555
    assert bucket["order_count"] == 1
    # 日期降级也是降级：raw 快照必须自证"这份不是原样的 create_time 口径"。
    assert update["raw"]["source"]["partial"] is True

    fallbacks = [e for e in update["audit"] if e["event"] == "collect.date_fallback"]
    assert len(fallbacks) == 1, [entry["event"] for entry in update["audit"]]
    data = fallbacks[0]["data"]
    assert data["kind"] == "date_fallback"
    assert data["count"] == 1
    assert data["reasons"] == {"unparseable": 1}
    sample = data["samples"][0]
    assert sample["order_id"] == "o_pay_time_only"
    assert sample["where"] == "order[0]"
    assert sample["used_key"] == "pay_time"
    assert sample["skipped_keys"] == ["create_time"]
    assert sample["reason"] == "unparseable"

    # 同一条事实也必须进 JSONL（`/api/logs` 是它唯一的人类出口）。
    rows = sink.read(run_id=RUN_ID, event_prefix="collect.date_fallback")
    assert len(rows) == 1
    assert rows[0]["level"] == "warn"
    assert rows[0]["data"]["count"] == 1

    done = _done(update)
    _assert_row_accounting(done)
    assert (done["rows_seen"], done["rows_accepted"], done["rows_rejected"]) == (2, 2, 0)


@pytest.mark.parametrize("falsy_value", [False, 0, ""], ids=["false", "zero", "empty-string"])
async def test_falsy_create_time_with_a_usable_pay_time_is_accepted_but_traced(
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
    falsy_value: Any,
) -> None:
    """`create_time` falsy + 合法 `pay_time` → 行被接受，但**必须有** `collect.date_fallback`。

    这是第 3 条要修的现象：旧实现里这一行既不进 rejected、也不进任何审计（`attempts` 是局部
    变量，成功即丢弃）。后果是"上游把 `create_time` 全写成 `False`"这件事**永远查不出来**，
    而同一文件里 `_order_units` 的规矩是"降级必须可见"。

    判据要能和"整行拒绝"分开：行进了指标（GMV 计入），`row_rejected` 一条都不许有。
    """
    fake_client.orders = [
        {
            "order_id": "o_falsy_but_paid",
            "sku_id": "sku_a",
            "pay_amount": 555,
            "product_count": 1,
            "create_time": falsy_value,
            "pay_time": "2026-10-20 11:00:00",
        },
        _good_row(),
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    assert _rejected(update) == [], "行被接受了，不许同时记成 row_rejected"
    assert update["raw"]["order_summary"]["gmv_cents"] == 9_900
    assert update["raw"]["order_summary"]["by_date"]["2026-10-20"]["gmv_cents"] == 555
    assert update["raw"]["source"]["partial"] is True

    data = next(e for e in update["audit"] if e["event"] == "collect.date_fallback")["data"]
    assert data["reasons"] == {"falsy_value": 1}
    sample = data["samples"][0]
    assert sample["order_id"] == "o_falsy_but_paid"
    assert sample["used_key"] == "pay_time"
    assert sample["skipped_keys"] == ["create_time"]
    assert sample["reason"] == "falsy_value"
    assert sink.read(run_id=RUN_ID, event_prefix="collect.date_fallback")[0]["level"] == "warn"

    done = _done(update)
    _assert_row_accounting(done)
    assert (done["rows_seen"], done["rows_accepted"], done["rows_rejected"]) == (2, 2, 0)


async def test_pay_time_only_row_is_not_a_date_degradation(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """只给 `pay_time`（**键不存在**，不是"给了不可用的值"）是上游正常形态，不告警。

    判据必须是"字段存在但给不出日期"，否则每次正常采集都会 warn（抖店不同接口本来就可能只给
    其中一个日期字段），真正的系统性坏字段会被淹掉。
    """
    fake_client.orders = [
        {
            "order_id": "o_pay_only",
            "sku_id": "sku_a",
            "pay_amount": 555,
            "product_count": 1,
            "pay_time": "2026-10-20 11:00:00",
        }
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    assert update["raw"]["source"]["partial"] is False
    assert [e for e in update["audit"] if e["event"] == "collect.date_fallback"] == []
    assert sink.read(run_id=RUN_ID, event_prefix="collect.date_fallback") == []
    assert update["raw"]["order_summary"]["by_date"]["2026-10-20"]["gmv_cents"] == 555


async def test_row_accounting_identity_includes_out_of_window_rows(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`rows_seen - rows_accepted` 必须能被 `rows_rejected + rows_out_of_window` 完整解释。

    旧实现只在函数内部数 `rows_out_of_window`、不写进任何事件的 `data`，于是
    `rows_seen - rows_accepted` 会被读成"被拒行数"——实际它是"被拒 + 窗口外"。
    这里三行分别落进三个桶：1 好行 + 1 脏行 + 1 窗口外。
    """
    fake_client.orders = [
        _good_row(),
        {
            "order_id": "o_bad",
            "sku_id": "sku_a",
            "pay_amount": "199.00",
            "create_time": f"{TODAY} 09:00:00",
        },
        {
            "order_id": "o_old",
            "sku_id": "sku_a",
            "pay_amount": 100,
            "create_time": "2026-10-01 10:00:00",  # 窗口是 10-16..10-23
        },
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    done = _done(update)
    _assert_row_accounting(done)
    assert (
        done["rows_seen"],
        done["rows_accepted"],
        done["rows_rejected"],
        done["rows_out_of_window"],
    ) == (3, 1, 1, 1)

    rejected = _rejected(update)[0]["data"]
    _assert_row_accounting(rejected, rejected_field="count")
    assert (rejected["rows_seen"], rejected["rows_accepted"]) == (3, 1)
    assert rejected["rows_out_of_window"] == 1

    rows = sink.read(run_id=RUN_ID, event_prefix="collect.row_rejected")
    assert len(rows) == 1
    _assert_row_accounting(rows[0]["data"], rejected_field="count")


async def test_all_rows_out_of_window_fails_with_the_out_of_window_bucket(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """全部落在窗口外 → 视同整批失败，但失败审计要能看出"不是脏、是窗口外"。

    没有 `rows_out_of_window` 时，`all_rows_rejected` + `reject_reasons={}` 会被误读成
    "上游给了 N 行脏数据"；四桶里真正的答案是 `rows_out_of_window == rows_seen`。
    """
    fake_client.orders = [
        {
            "order_id": f"o_old_{index}",
            "sku_id": "sku_a",
            "pay_amount": 100,
            "create_time": "2026-10-01 10:00:00",
        }
        for index in range(2)
    ]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") == RunStatus.FAILED.value
    failed = next(entry for entry in update["audit"] if entry["event"] == "collect.failed")
    data = failed["data"]
    _assert_row_accounting(data)
    assert data["reason"] == "all_rows_rejected"
    assert (
        data["rows_seen"],
        data["rows_accepted"],
        data["rows_rejected"],
        data["rows_out_of_window"],
    ) == (2, 0, 0, 2)

    rows = sink.read(run_id=RUN_ID, event_prefix="collect.failed")
    assert len(rows) == 1
    _assert_row_accounting(rows[0]["data"])


def test_business_date_of_propagates_type_error_from_our_own_code() -> None:
    """`_business_date_of` **不接** `TypeError`（v1.5.2）：代码 bug 不许变成"日期脏"。

    四个参数分支都已按类型收窄，数据侧只能触发 `OSError` / `OverflowError` / `ValueError`；
    `TypeError` 只可能来自我们自己的代码（改了 `_BUSINESS_TZ`、或传了写坏的 `datetime` 子类）。
    若被接住，`strict=True` 就会把它转成 `_BadBusinessDate` → 该行被拒 → 指标静默少算，
    与 `_best_effort_units` 那条**旁路**（只喂审计样本、保留 TypeError）口径不同但各自自洽。
    """

    class _BoomDatetime(datetime):
        def astimezone(self, tz: Any = None) -> Any:
            raise TypeError("内部实现坏了（不是上游数据形状）")

    value = _BoomDatetime(2026, 10, 23, 12, 0, tzinfo=nodes._BUSINESS_TZ)
    with pytest.raises(TypeError):
        nodes._business_date_of(value, fallback=TODAY)


def test_best_effort_units_still_absorbs_type_error_by_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_best_effort_units` 是**唯一**故意保留 `TypeError` 的地方：它只喂审计样本。

    这条与上一条一起锁死"两处口径不同是有意的"：谁若为了统一而把它也收窄，旁路样本的
    一个 `TypeError` 就会打死整轮采集（当天所有指标一起没有），本用例会红。
    """

    def _boom(*_args: Any, **_kwargs: Any) -> int:
        raise TypeError("内部实现坏了")

    monkeypatch.setattr(nodes, "_order_units", _boom)
    assert nodes._best_effort_units({"order_id": "o"}, where="order[0]") is None


# ======================================================================================
# v1.5.2：falsy 日期 / 逐字段尝试 / 行级"一个已知字段都没有" / 代码 bug 不许降级成数据脏
# ======================================================================================


async def test_rows_without_any_known_order_field_go_the_all_rows_rejected_path(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`{"shop_order_list": [{}, {}, {}]}` → 全行 `no_known_fields` → **failed**，不是"3 单 0 元"。

    旧行为（独立审查实测）：`order_count=3 / gmv=0 / rows_accepted=3 / rows_rejected=0 /
    partial=False` 且**零告警**。原因是空对象没有 `create_time`/`pay_time`，日期回退今天、
    金额缺失 → `paid=False`，于是一行"什么都没有"被当成一笔 0 元当日订单。批层有"一个已知
    **列表**字段都没有"的判据（`_require_orders_container`），行层原本没有对应的一条。
    """
    fake_client.orders = [{}, {}, {}]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") == RunStatus.FAILED.value
    assert "raw" not in update, "一行都没有已知订单字段时不许交出一份全 0 的 raw"
    messages = [str(item["message"]) for item in update["errors"]]
    assert any("订单行全部被拒绝" in text for text in messages), messages

    failed = next(entry for entry in update["audit"] if entry["event"] == "collect.failed")
    data = failed["data"]
    assert data["reason"] == "all_rows_rejected"
    assert (data["rows_seen"], data["rows_accepted"], data["rows_rejected"]) == (3, 0, 3)
    assert data["reject_reasons"] == {"no_known_fields": 3}
    assert data["source"]["partial"] is True


async def test_one_fieldless_row_among_good_rows_is_rejected_not_counted(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """`[{}, good]`：空对象记 `no_known_fields`（不是"一笔 0 元订单"），好行照常算。

    这一条钉住"部分被拒"的形态：`order_count` 只能是 1（不是 2），且拒绝事件必须带分母
    （`rows_seen=2 / rows_accepted=1`），否则日志页上"1 行被拒"分不清是 1/2 还是 1/1000。
    """
    fake_client.orders = [{}, _good_row()]

    update = await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    assert update.get("status") != RunStatus.FAILED.value
    summary = update["raw"]["order_summary"]
    assert summary["gmv_cents"] == 9_900
    assert summary["order_count"] == 1, "空对象不许被当成第二笔订单"

    data = _rejected(update)[0]["data"]
    assert data["reasons"] == {"no_known_fields": 1}
    sample = data["samples"][0]
    assert sample["where"] == "order[0]"
    assert sample["reason"] == "no_known_fields"
    assert data["rows_seen"] == 2
    assert data["rows_accepted"] == 1
    assert update["raw"]["source"]["partial"] is True


async def test_internal_type_error_propagates_instead_of_becoming_a_dirty_row(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
) -> None:
    """我们自己代码抛的 `TypeError` 必须**照常传播**，不许被记成 `row_rejected` 后继续"0 单成功"。

    ## 为什么偏偏用 `TypeError`

    本文件数据侧（`_money` / `_order_units` / `_is_refunded`）对**上游数据形状**只会抛
    `ValueError`；`TypeError` 只可能来自我们自己的代码（少传/错传参数、把 `None` 当 Mapping、
    改签名时漏改调用点）。它一旦被 `except Exception` 兜住，就是"代码 bug 被降级成数据脏"：
    指标静默少算，日志上只多一条"这一行脏"，没有人会去修代码——与"上游改了字段类型"完全
    无法区分。

    ## 这条用例守护的是哪个变异

    独立审查做过变异：把 `_aggregate_orders` 里的 `except ValueError` 改成 `except Exception`
    → 旧用例集 321 passed、**零红**。这条边界此前没有测试。变异后本用例会红（`TypeError` 变成
    `reason="unclassified"` 的行级拒绝，不再传播）。

    ## 为什么只对 `order[...]` 抛

    商品归一化（`_normalize_product`）也走 `_money`，但它在本函数之前执行。若在商品路径就炸，
    这个用例测到的不是"行级契约"，而是"商品列表炸了"——那样即便 `_aggregate_orders` 被放宽成
    `except Exception`，用例依然绿，守护就失效了。所以把注入点精确卡在订单行。
    """
    real_money = nodes._money

    def _boom(value: Any, *, where: str) -> int:
        if where.startswith("order["):
            raise TypeError(f"{where}: 内部调用写错了（不是上游数据形状）")
        return real_money(value, where=where)

    monkeypatch.setattr(nodes, "_money", _boom)
    fake_client.orders = [_good_row()]

    with pytest.raises(TypeError):
        await collect.__wrapped__(_state(), _config(runtime))  # type: ignore[attr-defined]

    # 关键：既没有"这一行被拒"的审计，也没有一条"采集完成（0 单 0 元）"——异常必须照常逃出去。
    assert sink.read(run_id=RUN_ID, event_prefix="collect.") == []
