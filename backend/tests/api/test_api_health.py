"""`GET /api/healthz`（CONTRACT §7.7）：真的探依赖，不是恒返回 ok。

仓库的验收命令就是"起服务 → healthz 200"，所以这条用例同时是**契约断言**与**起得来**的证明。
降级路径用 `reset_engine()` 造（engine 被丢弃 = 库不可用），而不是 monkeypatch 一个假函数：
这样连"`session_scope` 在 engine 缺失时报什么"这条真实失败路径也一起被走过。
"""

from __future__ import annotations

import re

from app.db import reset_engine

from .conftest import ApiHarness, assert_error_envelope


async def test_healthz_reports_ok_with_dependencies(api: ApiHarness) -> None:
    response = await api.http.get("/api/healthz")
    assert response.status_code == 200
    assert response.headers.get("x-trace-id")
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["checkpointer"] == "ok"
    assert body["version"] == "0.1.0"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", body["time"])


async def test_healthz_degrades_when_db_is_gone(api: ApiHarness) -> None:
    reset_engine()  # 等价于"库不可达/引擎被丢弃"：探活必须如实报 degraded
    response = await api.http.get("/api/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"
    assert body["detail"]
    # 503 也走 §7.1 的 trace 约定（响应头必须有，便于和日志对上）
    assert response.headers.get("x-trace-id")

    # 顺带：degraded 时端点自身也不该假装成功
    alerts = await api.http.get("/api/alerts")
    error = assert_error_envelope(alerts, code="INTERNAL_ERROR", status=500)
    assert error["trace_id"] == alerts.headers["x-trace-id"]
