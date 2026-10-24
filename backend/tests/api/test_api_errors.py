"""错误路径（CONTRACT §7.1 的 9 个错误码）与 §7 各端点的边界。

这一组用例的取向：**每条失败路径都要断言"哪个码 + 哪个 HTTP 状态 + 包络形状 + trace_id"**，
而不是只断言"没 200"。理由：前端只按 `error.code` 分支，码错了 UI 就会给出错误的处置建议
（比如把"上游抖动可重试"显示成"参数错误"）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.state import thread_id_for
from app.agent.tracing import insert_run_trace
from app.api import queries
from app.enums import RunStatus
from app.models import utcnow

from .conftest import SHOP_ID, ApiHarness, assert_error_envelope, decisions


def _approve_body(run: dict[str, Any], *, verdict: str = "approve") -> dict[str, Any]:
    return {
        "thread_id": run["thread_id"],
        "interrupt_id": run["pending_approvals"][0]["interrupt_id"],
        "operator": "ops_zhang",
        "decisions": decisions(
            [item["action_id"] for item in run["pending_approvals"]], verdict=verdict
        ),
    }


async def _start_run(api: ApiHarness) -> dict[str, Any]:
    response = await api.http.post("/api/agent/run", json={"shop_id": SHOP_ID})
    assert response.status_code == 202, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# 400 VALIDATION_ERROR
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"shop_id": "bad shop id"},  # 字符集违规
        {"shop_id": ""},  # 空
        {},  # 缺必填
        {"shop_id": SHOP_ID, "trigger": "cron"},  # 枚举外
        {"shop_id": SHOP_ID, "unknown_field": 1},  # 未知键（extra="forbid"）
        {"shop_id": SHOP_ID, "dry_run": False},  # 进程 dry_run=true 时不能单次切到真实上游
    ],
    ids=["bad-shop-id", "empty-shop-id", "missing-shop-id", "bad-trigger", "unknown-field", "dry-run-mismatch"],
)
async def test_run_request_validation(api: ApiHarness, payload: dict[str, Any]) -> None:
    response = await api.http.post("/api/agent/run", json=payload)
    error = assert_error_envelope(response, code="VALIDATION_ERROR", status=400)
    assert error["details"] is not None
    assert error["trace_id"] == response.headers["x-trace-id"]


async def test_approve_request_validation(api: ApiHarness) -> None:
    run = await _start_run(api)
    pending = run["pending_approvals"]
    base = {
        "thread_id": run["thread_id"],
        "interrupt_id": pending[0]["interrupt_id"],
        "operator": "ops_zhang",
    }
    price_id, stock_id = pending[0]["action_id"], pending[1]["action_id"]

    cases = {
        "no-decisions": {**base, "decisions": []},
        "missing-edited-params-key": {
            **base,
            "decisions": [{"action_id": price_id, "verdict": "approve"}],
        },
        "edit-without-params": {
            **base,
            "decisions": [
                {"action_id": price_id, "verdict": "edit", "edited_params": None}
            ],
        },
        "params-on-approve": {
            **base,
            "decisions": [
                {
                    "action_id": price_id,
                    "verdict": "approve",
                    "edited_params": {"price_cents": 1},
                }
            ],
        },
        "duplicate-action-id": {
            **base,
            "decisions": decisions([price_id, price_id]),
        },
        "bad-operator": {**base, "operator": "ops zhang!", "decisions": decisions([price_id])},
        "unknown-verdict": {
            **base,
            "decisions": [
                {"action_id": price_id, "verdict": "maybe", "edited_params": None}
            ],
        },
    }
    for name, payload in cases.items():
        response = await api.http.post("/api/approve", json=payload)
        assert response.status_code == 400, f"{name}: {response.text}"
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR", f"{name}: {error}"
        assert error["details"], f"{name}: details 不能为空（前端要靠它标红）"

    # decisions 比待审批集合**少一条** → §4.4 校验规则 2（既不许多也不许少），400
    fewer = {
        **base,
        "decisions": decisions([price_id]),
    }
    response = await api.http.post("/api/approve", json=fewer)
    error = assert_error_envelope(response, code="VALIDATION_ERROR", status=400)
    assert error["details"]["missing"] == [stock_id]


async def test_alerts_query_validation(api: ApiHarness) -> None:
    for params in (
        {"limit": 0},
        {"limit": 201},
        {"status": "deleted"},
        {"shop_id": "bad shop"},
        {"before_id": 0},
    ):
        response = await api.http.get("/api/alerts", params=params)
        assert_error_envelope(response, code="VALIDATION_ERROR", status=400)


async def test_metrics_query_validation(api: ApiHarness) -> None:
    bad_format = await api.http.get("/api/metrics", params={"shop_id": SHOP_ID, "date_from": "2026/10/16"})
    assert_error_envelope(bad_format, code="VALIDATION_ERROR", status=400)

    inverted = await api.http.get(
        "/api/metrics",
        params={"shop_id": SHOP_ID, "date_from": "2026-10-23", "date_to": "2026-10-01"},
    )
    error = assert_error_envelope(inverted, code="VALIDATION_ERROR", status=400)
    assert error["details"]["date_from"] == "2026-10-23"

    bad_granularity = await api.http.get(
        "/api/metrics", params={"shop_id": SHOP_ID, "granularity": "week"}
    )
    assert_error_envelope(bad_granularity, code="VALIDATION_ERROR", status=400)


async def test_logs_query_validation(api: ApiHarness) -> None:
    for params in ({"limit": 0}, {"limit": 501}, {"level": "fatal"}, {"before_seq": 0}):
        response = await api.http.get("/api/logs", params=params)
        assert_error_envelope(response, code="VALIDATION_ERROR", status=400)


# --------------------------------------------------------------------------------------
# 404：THREAD_NOT_FOUND / APPROVAL_NOT_FOUND / RESOURCE_NOT_FOUND
# --------------------------------------------------------------------------------------
async def test_approve_unknown_thread_is_404_thread_not_found(api: ApiHarness) -> None:
    response = await api.http.post(
        "/api/approve",
        json={
            "thread_id": thread_id_for(SHOP_ID, "run_missing"),
            "interrupt_id": "deadbeef",
            "operator": "ops_zhang",
            "decisions": decisions(["run_missing:price_update:1"]),
        },
    )
    error = assert_error_envelope(response, code="THREAD_NOT_FOUND", status=404)
    assert error["details"]["thread_id"] == thread_id_for(SHOP_ID, "run_missing")


async def test_approve_wrong_interrupt_id_is_404_approval_not_found(api: ApiHarness) -> None:
    run = await _start_run(api)
    body = _approve_body(run)
    body["interrupt_id"] = "0123456789abcdef"  # 旧页面/别人已经处理过的那种
    response = await api.http.post("/api/approve", json=body)
    assert_error_envelope(response, code="APPROVAL_NOT_FOUND", status=404)


async def test_metrics_without_data_is_404_resource_not_found(api: ApiHarness) -> None:
    response = await api.http.get("/api/metrics", params={"shop_id": SHOP_ID})
    error = assert_error_envelope(response, code="RESOURCE_NOT_FOUND", status=404)
    assert error["details"]["shop_id"] == SHOP_ID


async def test_unknown_path_uses_same_envelope(api: ApiHarness) -> None:
    """§7.1 是"所有 4xx/5xx 统一"：框架产生的 404 也不能漏出 `{"detail": ...}`。"""
    response = await api.http.get("/api/nope")
    error = assert_error_envelope(response, code="RESOURCE_NOT_FOUND", status=404)
    assert error["details"]["path"] == "/api/nope"


# --------------------------------------------------------------------------------------
# 409：RUN_IN_PROGRESS / RUN_STATE_CONFLICT / DUPLICATE_DECISION
# --------------------------------------------------------------------------------------
async def test_run_with_active_run_id_is_409_run_in_progress(api: ApiHarness) -> None:
    run_id = "run_manual_active"
    await insert_run_trace(
        run_id=run_id,
        trace_id="a" * 32,
        shop_id=SHOP_ID,
        thread_id=thread_id_for(SHOP_ID, run_id),
        trigger="manual",
        status=RunStatus.RUNNING.value,
        started_at=utcnow(),
    )
    response = await api.http.post(
        "/api/agent/run", json={"shop_id": SHOP_ID, "run_id": run_id}
    )
    error = assert_error_envelope(response, code="RUN_IN_PROGRESS", status=409)
    assert error["details"]["run_id"] == run_id
    assert error["details"]["status"] == "running"


async def test_approve_on_done_thread_is_409(api: ApiHarness) -> None:
    """thread 存在但已不挂起 → 409（`RUN_STATE_CONFLICT`；重复裁决的具体路径另有用例）。"""
    run = await _start_run(api)
    first = await api.http.post("/api/approve", json=_approve_body(run))
    assert first.status_code == 200, first.text

    # 换一条**没被裁决过**的 action_id：既不是重复裁决，也没有挂起中断
    stale = {
        "thread_id": run["thread_id"],
        "interrupt_id": run["pending_approvals"][0]["interrupt_id"],
        "operator": "ops_zhang",
        "decisions": [
            {
                "action_id": f"{run['run_id']}:price_update:9999999999999999999",
                "verdict": "approve",
                "edited_params": None,
            }
        ],
    }
    response = await api.http.post("/api/approve", json=stale)
    assert_error_envelope(response, code="RUN_STATE_CONFLICT", status=409)


# --------------------------------------------------------------------------------------
# 502 UPSTREAM_ERROR（只在"上游抖店"问题时用）
# --------------------------------------------------------------------------------------
async def test_run_with_upstream_5xx_is_502_upstream_error(api: ApiHarness) -> None:
    api.upstream.fail_status["order.searchList"] = 503
    response = await api.http.post("/api/agent/run", json={"shop_id": SHOP_ID})
    error = assert_error_envelope(response, code="UPSTREAM_ERROR", status=502)
    assert error["details"]["category"] == "server_5xx"
    assert error["details"]["retryable"] is True


async def test_run_with_business_error_ends_failed_not_502(api: ApiHarness) -> None:
    """上游**不可重试**的业务错误：图自己降级为 `status=failed`（§4.2），请求本身是 202。

    这条边界很容易被写错：业务错误不是"服务不可用"，把它变成 502 会让前端一直提供"重试"
    （它会一直失败）；正确的处置是"这一轮 run 失败了"。
    """
    api.upstream.business_error.add("order.searchList")
    response = await api.http.post("/api/agent/run", json={"shop_id": SHOP_ID})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["pending_approvals"] == []


# --------------------------------------------------------------------------------------
# 500 INTERNAL_ERROR（本地 bug 一律 500，不冒充 502）
# --------------------------------------------------------------------------------------
async def test_local_bug_is_500_without_leaking_details(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(**_: Any) -> Any:
        raise RuntimeError("postgresql://user:SECRET@db.internal:5432/doudian")

    monkeypatch.setattr(queries, "list_approval_page", boom)
    response = await api.http.get("/api/alerts")

    error = assert_error_envelope(response, code="INTERNAL_ERROR", status=500)
    assert error["message"] == "内部错误"
    assert error["details"] is None
    assert "SECRET" not in response.text


# --------------------------------------------------------------------------------------
# X-Trace-Id：沿用来访，不重新生成
# --------------------------------------------------------------------------------------
async def test_trace_id_is_reused_when_supplied(api: ApiHarness) -> None:
    supplied = "trace-from-gateway-0001"
    ok = await api.http.get("/api/healthz", headers={"X-Trace-Id": supplied})
    assert ok.status_code == 200
    assert ok.headers["x-trace-id"] == supplied

    bad = await api.http.post("/api/agent/run", json={}, headers={"X-Trace-Id": supplied})
    error = assert_error_envelope(bad, code="VALIDATION_ERROR", status=400)
    assert error["trace_id"] == supplied
