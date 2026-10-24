"""HTTP 边界上的行级契约（v1.5.2）：**脏行不是 500**。

为什么这一组必须走真 HTTP（`tests/api/conftest.py` 的 `MockUpstream` + `ASGITransport`）：
现有脏行用例全部直调 `collect.__wrapped__`，钉住的是**节点内部**的分级；而"脏行 → 202 +
`status=failed`（或正常成功）"这条**对外契约**跨了四层——节点 → 图 → `run_agent` 的错误映射
→ §7.1 包络。任何一层把异常改回 re-raise，节点级用例都照样绿，前端却会收到 500/502。

两条边界（判据见 `app/agent/nodes.py` 的 `_aggregate_orders` docstring）：

1. **脏行混在好行里** → 整行拒绝 + warn + `partial=True`，run 正常走完（本例落到
   `awaiting_approval`：规则命中了降价/清库存）。**不是** 500，也**不是** 502；
2. **整批行都脏** → `all_rows_rejected` → `status=failed`。请求本身仍然是 **202**
   （这不是"服务不可用"，是"这一轮 run 失败了"），日志页上必须能查到
   `collect.failed` 且 `data.reason == "all_rows_rejected"`。
"""

from __future__ import annotations

from typing import Any

from .conftest import SHOP_ID, SKU_TOP_GMV, TODAY, ApiHarness, _orders


def _dirty_units_row() -> dict[str, Any]:
    """`product_count: "3"`：件数是字符串 → `bad_units` 整行拒绝（猜不出含义，不许按 1 件猜）。"""
    return {
        "order_id": "today-dirty-units",
        "status": "paid",
        "pay_amount_cents": 199_000,
        "order_amount_cents": 199_000,
        "refund_amount_cents": 0,
        "sku_id": SKU_TOP_GMV,
        "product_count": "3",
        "create_time": f"{TODAY} 10:30:00",
    }


def _dirty_amount_row(index: int) -> dict[str, Any]:
    """`pay_amount: "199.00"`：金额是字符串 → `bad_pay_amount` 整行拒绝。"""
    return {
        "order_id": f"today-dirty-amount-{index}",
        "status": "paid",
        "pay_amount": "199.00",
        "sku_id": SKU_TOP_GMV,
        "product_count": 1,
        "create_time": f"{TODAY} 10:3{index}:00",
    }


async def _logs(api: ApiHarness, *, run_id: str, event_prefix: str) -> list[dict[str, Any]]:
    response = await api.http.get(
        "/api/logs", params={"run_id": run_id, "event": event_prefix, "limit": 200}
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def test_dirty_row_mixed_with_good_rows_is_202_with_a_warning_not_500(
    api: ApiHarness,
) -> None:
    """一行 `product_count:"3"` + 默认的好行 → **202**、run 正常成功、warn + 拒绝率可查。

    HTTP 契约（§7.2 / §7.1）：脏数据是 `collect` 的**行级降级**，不是服务端故障。所以：

    - 状态码 202（不是 500 `INTERNAL_ERROR`，也不是 502 `UPSTREAM_ERROR`）；
    - `body.status` 是正常的 `awaiting_approval`（本轮的规则照常命中并挂起等待审批）——
      一行脏数据不该打死当天所有指标，也不该改变这一轮的业务结论；
    - 日志里 `collect.row_rejected` 的 `data` 带**分子与分母**（`count=1` 而
      `rows_seen = 好行数 + 1`）：只看"1 行被拒"没法判断这是 1/10 还是 1/10 万。
    """
    baseline_rows = len(_orders())
    api.upstream.extra_orders.append(_dirty_units_row())

    response = await api.http.post("/api/agent/run", json={"shop_id": SHOP_ID})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "awaiting_approval", body
    assert body["pending_approvals"], "脏行不影响本轮规则命中"

    rejected = await _logs(api, run_id=body["run_id"], event_prefix="collect.row_rejected")
    assert len(rejected) == 1, [item["event"] for item in rejected]
    data = rejected[0]["data"]
    assert data["count"] == 1
    assert data["reasons"] == {"bad_units": 1}
    assert data["excluded_from_metrics"] is True
    # 分母：上游给了 baseline_rows + 1 行，只有那 1 行被拒
    assert data["rows_seen"] == baseline_rows + 1
    assert data["rows_accepted"] == baseline_rows

    done = await _logs(api, run_id=body["run_id"], event_prefix="collect.done")
    assert len(done) == 1
    assert done[0]["data"]["partial"] is True, "有行被拒 → 这份采集不完整，raw 必须自证"
    assert done[0]["data"]["rows_rejected"] == 1


async def test_all_rows_dirty_is_202_with_failed_status_and_a_queryable_reason(
    api: ApiHarness,
) -> None:
    """整批行都是 `pay_amount:"199.00"` → **202** + `status=failed` + 可查的 `all_rows_rejected`。

    这是"逐行拒绝"容易漏掉的第三区间：一行都没被接受时，降级成"0 单 0 元"就是零告警的
    生意归零。节点内走 `_AllRowsRejected`（`_BatchContractError` 子类）→ 记错 + `failed`
    → `finalize`，因此 **HTTP 层是 202 而不是 500**（本地 bug 才 500），也**不是 502**
    （上游确实回了 200 + 一批行，没坏）。

    日志页是这条失败唯一的人类出口：`collect.failed` 的 `data.reason` 必须把
    "全行被拒" 与 "上游接口挂了"（`orders_unavailable`）分开——两者的处置方向完全相反。
    """
    api.upstream.orders_override = [_dirty_amount_row(index) for index in range(3)]

    response = await api.http.post("/api/agent/run", json={"shop_id": SHOP_ID})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "failed", body
    assert body["pending_approvals"] == []

    collected = await _logs(api, run_id=body["run_id"], event_prefix="collect.")
    failed = [item for item in collected if item["event"] == "collect.failed"]
    assert len(failed) == 1, [item["event"] for item in collected]
    data = failed[0]["data"]
    assert data["reason"] == "all_rows_rejected"
    assert (data["rows_seen"], data["rows_accepted"], data["rows_rejected"]) == (3, 0, 3)
    assert data["reject_reasons"] == {"bad_pay_amount": 3}
    # 失败侧不许混进一条"采集完成"：那会让日志页同时给出两个互相矛盾的状态
    assert [item["event"] for item in collected if item["event"] == "collect.done"] == []
