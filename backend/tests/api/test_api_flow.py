"""API 全链路（CONTRACT §7.2–§7.7）：run → interrupt → alerts → approve → 恢复 → logs。

这一组用例只断言**契约里写死的对外形状与状态迁移**，不重复 agent-core 的图内断言：

- 谁产生了挂起（`status=awaiting_approval` + `pending_approvals`）以及 `interrupt_id` 是否
  已经回填到持久化审批行（否则前端手里的 id 是空串，审批按钮永远 404）；
- `/api/alerts` 是否真的从 `approval_request` 读（而不是内存）；
- `/api/approve` 是否真的恢复**同一个 thread**（`applied[]` 与 `effect_report` 是证据）；
- `/api/logs` 是否能看到那次挂起（`outcome="interrupted"`、`level="info"`、`error=null`）；
- `/api/metrics` 是否能把 run 的指标读回来（金额=整数分、缺的日期不补零）。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.tracing import get_run_trace

from .conftest import (
    PRODUCT_TOP_GMV,
    SHOP_ID,
    SKU_TOP_GMV,
    SKU_TOP_REFUND,
    TODAY,
    ApiHarness,
)


async def _start_run(api: ApiHarness, **overrides: Any) -> dict[str, Any]:
    """POST /api/agent/run 并返回响应体（默认不带 run_id，让服务端生成）。"""
    payload: dict[str, Any] = {"shop_id": SHOP_ID}
    payload.update(overrides)
    response = await api.http.post("/api/agent/run", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def _ids_by_type(pending: list[dict[str, Any]]) -> dict[str, str]:
    return {item["action_type"]: item["action_id"] for item in pending}


async def test_run_interrupts_and_returns_pending_approvals(api: ApiHarness) -> None:
    """§7.2：202 + `awaiting_approval` + 待审批提议（含已回填的 `interrupt_id`）。"""
    response = await api.http.post("/api/agent/run", json={"shop_id": SHOP_ID})
    assert response.status_code == 202
    assert response.headers.get("x-trace-id")
    body = response.json()

    assert body["run_id"].startswith("run_")
    assert body["thread_id"] == f"{SHOP_ID}:{body['run_id']}"
    assert len(body["trace_id"]) == 32
    assert body["status"] == "awaiting_approval"
    # 图内只登记窗口、不给因果结论；没执行过动作时 effect_report 必须是 null（§7.2）
    assert body["effect_report"] is None

    pending = body["pending_approvals"]
    assert {item["action_type"] for item in pending} == {"price_update", "stock_update"}
    assert len({item["action_id"] for item in pending}) == len(pending)
    for item in pending:
        assert item["thread_id"] == body["thread_id"]
        assert item["shop_id"] == SHOP_ID
        assert item["status"] == "pending"
        # 回填过 interrupt_id：否则审批台手里的 id 是空串（run_agent 的 backfill 责任）
        assert item["interrupt_id"]
        assert item["params"] and isinstance(item["params"], dict)
        assert item["evidence"]["rule_id"]
        assert item["created_at"].endswith("Z")

    ids = _ids_by_type(pending)
    assert ids["price_update"].endswith(f":price_update:{SKU_TOP_GMV}")
    assert ids["stock_update"].endswith(f":stock_update:{SKU_TOP_REFUND}")

    # 幂等：同一 (shop_id, run_id) 在终态之前再跑 → 409 RUN_IN_PROGRESS
    # （这里 run 处于 awaiting_approval，属于 §4.1 的"非终态"）
    again = await api.http.post(
        "/api/agent/run", json={"shop_id": SHOP_ID, "run_id": body["run_id"]}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "RUN_IN_PROGRESS"


async def test_alerts_reads_persisted_rows_and_pages_by_id(api: ApiHarness) -> None:
    """§7.3：从 `approval_request` 读；`next_cursor` 是最后一条 `id`，keyset 翻页不重不漏。"""
    run = await _start_run(api)
    expected = {item["action_id"] for item in run["pending_approvals"]}

    first = await api.http.get("/api/alerts", params={"shop_id": SHOP_ID, "limit": 1})
    assert first.status_code == 200
    assert first.headers.get("x-trace-id")
    page1 = first.json()
    assert len(page1["items"]) == 1
    assert page1["next_cursor"] is not None
    assert page1["next_cursor"].isdigit()

    second = await api.http.get(
        "/api/alerts",
        params={"shop_id": SHOP_ID, "limit": 1, "before_id": int(page1["next_cursor"])},
    )
    page2 = second.json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None  # 第二页之后没有更多
    seen = {page1["items"][0]["action_id"], page2["items"][0]["action_id"]}
    assert seen == expected

    # 过滤：别的店铺 / 别的状态都查不到
    other = await api.http.get("/api/alerts", params={"shop_id": "99999999"})
    assert other.status_code == 200 and other.json() == {"items": [], "next_cursor": None}
    approved = await api.http.get("/api/alerts", params={"status": "approved"})
    assert approved.status_code == 200 and approved.json()["items"] == []


async def test_approve_resumes_same_thread_and_writes_action_log(api: ApiHarness) -> None:
    """§7.4：edit + reject 各一条 → 200 `done`、`applied[]` 如实、效果窗口只给成功的那条。"""
    run = await _start_run(api)
    pending = run["pending_approvals"]
    ids = _ids_by_type(pending)
    interrupt_id = pending[0]["interrupt_id"]

    body = {
        "thread_id": run["thread_id"],
        "interrupt_id": interrupt_id,
        "operator": "ops_zhang",
        "decisions": [
            {
                "action_id": ids["price_update"],
                "verdict": "edit",
                "edited_params": {"price_cents": 19_000},
                "operator": None,
                "reason": "降幅收敛到 4.5%",
            },
            {
                "action_id": ids["stock_update"],
                "verdict": "reject",
                "edited_params": None,
                "operator": "ops_li",
                "reason": "该 SKU 已停售",
            },
        ],
    }
    response = await api.http.post("/api/approve", json=body)
    assert response.status_code == 200, response.text
    assert response.headers.get("x-trace-id")
    result = response.json()

    assert result["thread_id"] == run["thread_id"]
    assert result["run_id"] == run["run_id"]
    assert result["trace_id"] == run["trace_id"]
    assert result["status"] == "done"
    assert result["pending_approvals"] == []

    applied = {item["action_id"]: item for item in result["applied"]}
    assert applied[ids["price_update"]]["status"] == "succeeded"
    assert applied[ids["price_update"]]["verdict"] == "edit"
    assert applied[ids["price_update"]]["action_log_id"]  # = idempotency_key（64 hex）
    assert len(applied[ids["price_update"]]["action_log_id"]) == 64
    # reject 不写 action_log（§7.4）
    assert applied[ids["stock_update"]] == {
        "action_id": ids["stock_update"],
        "verdict": "reject",
        "status": "rejected",
        "action_log_id": None,
        "error": None,
    }

    # 真的把**审批后**的价格发给了上游：19000 分，不是提议里的 19303 分
    edits = [
        json.loads(request.content.decode("utf-8"))
        for request in api.upstream.requests
        if request.url.params.get("method") == "product.editPrice"
    ]
    assert edits == [
        {"product_id": PRODUCT_TOP_GMV, "sku_id": SKU_TOP_GMV, "price": 19_000}
    ]
    assert "product.editStock" not in api.methods_called()  # reject 的动作不执行

    # 效果报告：只有成功的那一条，4 个窗口（0/1/3/7），全部 scheduled（图内不等待）
    report = result["effect_report"]
    assert report is not None
    assert report["run_id"] == run["run_id"]
    assert [window["offset_days"] for window in report["windows"]] == [0, 1, 3, 7]
    assert {window["status"] for window in report["windows"]} == {"scheduled"}
    assert report["attribution"] == "single_arm_before_after"
    assert len(report["caveats"]) >= 2

    # 审批行的状态已经落库（不是只在图里）
    approved_rows = await api.http.get("/api/alerts", params={"status": "approved"})
    rejected_rows = await api.http.get("/api/alerts", params={"status": "rejected"})
    assert [item["action_id"] for item in approved_rows.json()["items"]] == [
        ids["price_update"]
    ]
    assert [item["action_id"] for item in rejected_rows.json()["items"]] == [
        ids["stock_update"]
    ]

    # run 摘要行是 done（终态）
    trace = await get_run_trace(shop_id=SHOP_ID, run_id=run["run_id"])
    assert trace is not None and trace["status"] == "done" and trace["step_count"] == 10

    # 已终态的同一 run 再 POST：200（回放，不是 202）——§7.2 末段
    replay = await api.http.post(
        "/api/agent/run", json={"shop_id": SHOP_ID, "run_id": run["run_id"]}
    )
    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed["status"] == "done"
    assert replayed["thread_id"] == run["thread_id"]
    assert replayed["pending_approvals"] == []
    # 回放没有重新跑图：步数不变、上游没有被再打一遍（改价只发生过一次）
    after = await get_run_trace(shop_id=SHOP_ID, run_id=run["run_id"])
    assert after is not None and after["step_count"] == trace["step_count"]
    assert api.methods_called().count("product.editPrice") == 1


async def test_approve_twice_is_409_duplicate_decision(api: ApiHarness) -> None:
    """§7.4 / §7.1：同一条动作不能裁决两次 → 409 `DUPLICATE_DECISION`（不是 500，也不是 200）。"""
    run = await _start_run(api)
    body = {
        "thread_id": run["thread_id"],
        "interrupt_id": run["pending_approvals"][0]["interrupt_id"],
        "operator": "ops_zhang",
        "decisions": [
            {
                "action_id": item["action_id"],
                "verdict": "approve",
                "edited_params": None,
            }
            for item in run["pending_approvals"]
        ],
    }
    first = await api.http.post("/api/approve", json=body)
    assert first.status_code == 200, first.text

    second = await api.http.post("/api/approve", json=body)
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "DUPLICATE_DECISION"
    assert error["details"]["thread_id"] == run["thread_id"]
    assert second.headers.get("x-trace-id") == error["trace_id"]


async def test_logs_show_interrupted_node_trace_and_desc_paging(api: ApiHarness) -> None:
    """§7.6 / §7.6.1：挂起是 `info` 不是 `error`；`seq DESC`；`before_seq` 是 keyset。"""
    run = await _start_run(api)
    await api.http.post(
        "/api/approve",
        json={
            "thread_id": run["thread_id"],
            "interrupt_id": run["pending_approvals"][0]["interrupt_id"],
            "operator": "ops_zhang",
            "decisions": [
                {
                    "action_id": item["action_id"],
                    "verdict": "approve",
                    "edited_params": None,
                }
                for item in run["pending_approvals"]
            ],
        },
    )

    response = await api.http.get(
        "/api/logs", params={"run_id": run["run_id"], "event": "node.trace", "limit": 50}
    )
    assert response.status_code == 200
    assert response.headers.get("x-trace-id")
    items = response.json()["items"]
    # 9 个节点 + approval_gate 重放一次 = 10 条（§4.7 不变量；v1.6.0 节点数 8 → 9，
    # 多出来的是旁路节点 `explain`，它同样走 traced，因此**必然**多一条 trace）。
    assert len(items) == 10
    seqs = [item["seq"] for item in items]
    assert seqs == sorted(seqs, reverse=True)
    assert all(item["event"] == "node.trace" for item in items)
    assert all(item["run_id"] == run["run_id"] for item in items)
    assert all(item["data"]["outcome"] in {"ok", "interrupted", "error"} for item in items)

    interrupted = [item for item in items if item["data"]["outcome"] == "interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["node"] == "approval_gate"
    assert interrupted[0]["level"] == "info"
    assert interrupted[0]["data"]["error"] is None
    assert interrupted[0]["message"] == "node interrupted (awaiting human decision)"
    assert interrupted[0]["data"]["output_digest"] is None

    # keyset：拿第一页最后一条 seq 作为 before_seq，下一页必须严格更小
    page1 = await api.http.get(
        "/api/logs", params={"run_id": run["run_id"], "event": "node.trace", "limit": 3}
    )
    first_page = page1.json()
    assert len(first_page["items"]) == 3
    assert first_page["next_cursor"] is not None
    page2 = await api.http.get(
        "/api/logs",
        params={
            "run_id": run["run_id"],
            "event": "node.trace",
            "limit": 50,
            "before_seq": int(first_page["next_cursor"]),
        },
    )
    second_page = page2.json()
    assert second_page["next_cursor"] is None
    # 不写死数字：第二页的条数由"总数 − 第一页取走的条数"决定。写死 7 的话，
    # 将来节点数再变（或多/少一条 trace）时，这条断言会跟着漂移而失去回归意义——
    # 而"分页是否丢行"正是它真正要守的东西。
    assert len(second_page["items"]) == len(items) - len(first_page["items"])
    assert all(item["seq"] < int(first_page["next_cursor"]) for item in second_page["items"])

    # level 过滤：本 run 有**一条** critical 异常（`refund_spike`），它必须落成 `level=error`。
    #
    # v1.5.1 之前这里断言的是 `items == []`——那条断言本身编码了缺陷：审计条目当时只进
    # `state.audit`（checkpoint），从来不写 JSONL，所以一条 critical 告警在日志页上根本不存在。
    # 现在两条通道并存：`node.trace` 的键集合不变，异常走独立的 `anomaly.detected` 事件。
    errors = await api.http.get("/api/logs", params={"run_id": run["run_id"], "level": "error"})
    assert errors.status_code == 200
    error_items = errors.json()["items"]
    assert [item["event"] for item in error_items] == ["anomaly.detected"]
    assert error_items[0]["data"]["rule_id"] == "refund_spike"
    assert error_items[0]["data"]["severity"] == "critical"
    # 但**节点级**没有红灯：整轮没有一条 node.trace 的 outcome=error（挂起是 info，§7.6.1）
    assert [item for item in error_items if item["event"] == "node.trace"] == []
    warning_level = await api.http.get(
        "/api/logs", params={"run_id": run["run_id"], "level": "warn"}
    )
    assert warning_level.status_code == 200
    warn_items = warning_level.json()["items"]
    # warn 档：异常明细（gmv_drop_day 是 warn）+ 采集层降级（上游没给件数，成本会偏乐观）。
    # 按 `data.kind` 取明细（v1.5.1）：同一个 event 名下有两种 schema，不能靠"有没有 rule_id"猜。
    assert "gmv_drop_day" in [
        item["data"]["rule_id"] for item in warn_items if item["data"].get("kind") == "detail"
    ]
    assert any(item["event"] == "collect.units_degraded" for item in warn_items)
    assert all(item["event"] != "node.trace" for item in warn_items)
    # `event` 前缀匹配：`anomaly.` 命中本次 run 的所有异常事件（一条汇总 + 每条异常一条明细）。
    # 这是"审批台看不到的告警至少能在日志页查到"的接口级保证（loss_making 只告警不给动作）。
    anomalies = await api.http.get(
        "/api/logs", params={"run_id": run["run_id"], "event": "anomaly."}
    )
    assert anomalies.status_code == 200
    anomaly_items = anomalies.json()["items"]
    assert all(item["event"] == "anomaly.detected" for item in anomaly_items)
    detail_items = [item for item in anomaly_items if item["data"].get("kind") == "detail"]
    summary_items = [item for item in anomaly_items if item["data"].get("kind") == "summary"]
    assert len(summary_items) == 1 and summary_items[0]["data"]["count"] == 2
    assert "rule_id" not in summary_items[0]["data"]
    assert {"gmv_drop_day", "refund_spike"} <= {
        item["data"]["rule_id"] for item in detail_items
    }

    unknown = await api.http.get("/api/logs", params={"run_id": "run_does_not_exist"})
    assert unknown.status_code == 200 and unknown.json() == {"items": [], "next_cursor": None}


async def test_metrics_reads_run_state_points(api: ApiHarness) -> None:
    """§7.5：`points` 按日期升序、金额=整数分、比率为 `0..1`、缺的日期不补零。"""
    run = await _start_run(api)
    await api.http.post(
        "/api/approve",
        json={
            "thread_id": run["thread_id"],
            "interrupt_id": run["pending_approvals"][0]["interrupt_id"],
            "operator": "ops_zhang",
            "decisions": [
                {
                    "action_id": item["action_id"],
                    "verdict": "approve",
                    "edited_params": None,
                }
                for item in run["pending_approvals"]
            ],
        },
    )

    response = await api.http.get(
        "/api/metrics",
        params={"shop_id": SHOP_ID, "date_from": "2026-10-16", "date_to": TODAY},
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("x-trace-id")
    body = response.json()

    assert body["shop_id"] == SHOP_ID
    assert body["granularity"] == "day"
    assert body["unit"] == {"money": "cent", "date": "Asia/Shanghai", "ratio": "0..1"}

    points = body["points"]
    dates = [point["date"] for point in points]
    assert dates == sorted(dates)
    assert dates[0] == "2026-10-16" and dates[-1] == TODAY
    # 7 天历史 + 今天：图里 compute_metrics 会为每个业务日各算一条
    assert len(points) == 8

    today = points[-1]
    assert today["gmv_cents"] == 109_900  # 100000 + 9900 退款单
    assert today["paid_order_count"] == 2
    assert today["refund_order_count"] == 1
    assert today["aov_cents"] == 54_950
    assert today["refund_rate"] == 0.5
    # 订单聚合里没有 uv（`collect` 只从订单明细推导，上游 summary.uv 不参与）→ null，不是 0
    assert today["uv"] is None
    assert today["cvr"] is None
    assert today["gmv_7d_avg_cents"] == 1_000_000
    assert today["computed_at"].endswith("Z")
    # 历史不足 3 天的头两天：均值是 null（"算不出来"），不是 0
    assert points[0]["gmv_7d_avg_cents"] is None

    # 只取今天一天：不补零、不多给
    one_day = await api.http.get(
        "/api/metrics", params={"shop_id": SHOP_ID, "date_from": TODAY, "date_to": TODAY}
    )
    assert one_day.status_code == 200
    assert [point["date"] for point in one_day.json()["points"]] == [TODAY]
