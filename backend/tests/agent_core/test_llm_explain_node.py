"""`explain` 节点（LLM 旁路）的契约：**模型只解释、不建议，失败一律降级**。

这一组用例守的是三件事，缺一条这条旁路就会变成"看起来在跑、其实没在跑"：

1. 成功路径确实把解释写进 state，并且**只**写 `explanations` / `suggestions`——
   不碰 `pending_actions` / `status` / `approval`（动作只由规则产出）；
2. 没配 key 是**开关**不是故障（`llm.skipped`），超时/坏 JSON 是**故障但可降级**
   （`llm.degraded`）——两者都不许让主链路失败；
3. 模型编出本次输入之外的 id 时，节点不仅要丢弃，还要留下 `schema_mismatch` 痕迹：
   静默丢弃会让模型质量慢慢退化而没人发现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent import adapters, nodes
from app.observability.logging import JsonlLogSink
from app.services import llm

RUN_ID = "run_explain_node"
SHOP_ID = "12345678"


def _sink_rows(sink: JsonlLogSink, event: str) -> list[dict[str, Any]]:
    """从 JSONL 文件里直接取本用例的事件行。

    为什么不用 `sink.read(...)`：那个读法会去查 `agent_traces` 拉 run 摘要，
    而这里只跑了一个节点、没有落库的 run —— 于是它抛 `LookupError`。
    节点测试关心的是"有没有写进汇"，不是"汇能不能顺带渲染摘要"，
    直接读文件更贴近被测对象，也不会因为 DB 状态让用例变脆。
    """
    lines = Path(sink.path).read_text(encoding="utf-8").splitlines()
    rows = [row for row in (json.loads(line) for line in lines if line.strip()) if isinstance(row, dict)]
    return [
        row
        for row in rows
        if row.get("event") == event and row.get("run_id") == RUN_ID and row.get("shop_id") == SHOP_ID
    ]


class _FakeLlm:
    """最小 `LlmClient` 替身：记录请求、返回预设结果。

    只实现协议里的两个方法（`explain` / `aclose`）。故意不继承任何基类：
    节点只该依赖协议，若它偷偷用了具体实现的方法，这里会立刻炸。
    """

    def __init__(self, result: llm.ExplainResult) -> None:
        self.result = result
        self.requests: list[llm.ExplainRequest] = []
        self.closed = False

    async def explain(self, request: llm.ExplainRequest) -> llm.ExplainResult:
        self.requests.append(request)
        return self.result

    async def aclose(self) -> None:
        self.closed = True


def _anomalies() -> list[dict[str, Any]]:
    """两条异常：字段取自 `_EXPLAIN_ANOMALY_KEYS` 白名单（多给的会被丢掉）。"""
    return [
        {
            "anomaly_id": "a1",
            "rule_id": "refund_spike",
            "metric": "refund_rate",
            "observed": 0.333333,
            "threshold": 0.08,
            "comparator": ">",
            "severity": "critical",
            "message": "退款率超过 8%",
        },
        {
            "anomaly_id": "a2",
            "rule_id": "gmv_drop_day",
            "metric": "gmv_cents",
            "observed": 100_000,
            "threshold": 186_600,
            "comparator": "<",
            "severity": "warn",
            "message": "GMV 环比 -31.2%",
        },
    ]


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "trace_id": "trace_explain_node",
        "shop_id": SHOP_ID,
        "thread_id": f"{SHOP_ID}:{RUN_ID}",
        "trigger": "manual",
        "started_at": "2026-10-23T12:00:00Z",
        "status": "running",
        "iteration": 0,
        "raw": None,
        "metrics": {"date": "2026-10-23", "gmv_cents": 100_000, "refund_rate": 0.333333},
        "history": [],
        "anomalies": _anomalies(),
        "pending_actions": [],
        "approved_actions": [],
        "rejected_actions": [],
        "approval": None,
        "execution_results": [],
        "effect_report": None,
        "errors": [],
        "audit": [],
        "explanations": {},
        "suggestions": {},
    }
    state.update(overrides)
    return state


def _config(runtime: Any) -> dict[str, Any]:
    return {"configurable": {"runtime": runtime}}


@pytest.fixture(autouse=True)
def _reset_llm_client() -> Any:
    """每个用例后把进程级客户端还原成 `None`。

    不还原的后果：成功路径那些用例会把自己的假客户端留给后面的用例，
    于是"没配 key 就跳过"这条最关键的用例会**静默变成另一个用例的副本**。
    """
    yield
    adapters.set_llm_client(None)


async def test_success_writes_explanations_and_audits(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """成功路径：解释/建议按 anomaly_id 落到 state，并写一条 info 级 `llm.explained`。"""
    fake = _FakeLlm(
        llm.ExplainResult.success(
            explanations={"a1": "退款率异常偏高，疑似刷单或质量问题", "a2": "昨日 GMV 明显下滑"},
            suggestions={"a1": "人工复核近 24 小时退款订单", "a2": "检查是否有活动结束"},
        )
    )
    adapters.set_llm_client(fake)
    state = _state()

    update = await nodes.explain_node.__wrapped__(  # type: ignore[attr-defined]
        state, _config(runtime)
    )

    assert update["explanations"]["a1"].startswith("退款率异常偏高")
    assert update["suggestions"]["a2"] == "检查是否有活动结束"
    # 旁路节点**不许**碰决策相关的字段
    for forbidden in ("pending_actions", "status", "approval", "proposed_actions"):
        assert forbidden not in update

    assert fake.requests[0].anomaly_ids == ("a1", "a2")
    assert update["audit"][0]["event"] == "llm.explained"
    rows = _sink_rows(sink, "llm.explained")
    assert rows and rows[-1]["level"] == "info"
    assert rows[-1]["data"] == {"count": 2, "suggestions": 2}


async def test_no_client_is_a_switch_not_a_failure(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """没装配客户端（没配 key）：`llm.skipped` + 空 dict，绝不抛异常。"""
    adapters.set_llm_client(None)
    state = _state()

    update = await nodes.explain_node.__wrapped__(  # type: ignore[attr-defined]
        state, _config(runtime)
    )

    assert update["explanations"] == {}
    assert update["suggestions"] == {}
    assert update["audit"][0]["event"] == "llm.skipped"
    rows = _sink_rows(sink, "llm.skipped")
    assert rows and rows[-1]["level"] == "warn"
    assert rows[-1]["data"]["reason"] == "no_client"
    assert rows[-1]["data"]["anomalies"] == 2


async def test_timeout_degrades_without_failing_the_run(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """超时 → `llm.degraded(reason=timeout)`；解释为空，但流程继续（不抛）。"""
    adapters.set_llm_client(_FakeLlm(llm.ExplainResult.degraded("timeout", "TimeoutError")))
    state = _state()

    update = await nodes.explain_node.__wrapped__(  # type: ignore[attr-defined]
        state, _config(runtime)
    )

    assert update["explanations"] == {}
    assert update["audit"][0]["event"] == "llm.degraded"
    rows = _sink_rows(sink, "llm.degraded")
    assert rows and rows[-1]["data"]["reason"] == "timeout"
    assert rows[-1]["data"]["unexplained"] == 2


async def test_dropped_ids_leave_a_schema_mismatch_trail(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """模型给了本次输入之外的 id：只保留合法条目，并把丢弃数记成 `schema_mismatch`。"""
    adapters.set_llm_client(
        _FakeLlm(
            llm.ExplainResult.success(
                explanations={"a1": "退款率偏高"},
                suggestions={"a1": "人工复核"},
                dropped=1,
            )
        )
    )
    state = _state()

    update = await nodes.explain_node.__wrapped__(  # type: ignore[attr-defined]
        state, _config(runtime)
    )

    assert set(update["explanations"]) == {"a1"}
    events = [entry["event"] for entry in update["audit"]]
    assert events == ["llm.explained", "llm.degraded"]
    rows = _sink_rows(sink, "llm.degraded")
    assert rows and rows[-1]["data"]["reason"] == "schema_mismatch"
    assert rows[-1]["data"]["dropped"] == 1
    # 2 条异常里只有 1 条拿到解释
    assert rows[-1]["data"]["unexplained"] == 1


async def test_no_anomalies_skips_the_model_call_entirely(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """没有异常 → 一次请求都不发（不白花钱），也不写审计（`anomaly.detected(count=0)` 已说清）。"""
    fake = _FakeLlm(llm.ExplainResult.success(explanations={}, suggestions={}))
    adapters.set_llm_client(fake)
    state = _state(anomalies=[])

    update = await nodes.explain_node.__wrapped__(  # type: ignore[attr-defined]
        state, _config(runtime)
    )

    assert fake.requests == []
    assert update == {"explanations": {}, "suggestions": {}}
    assert _sink_rows(sink, "llm.explained") == []


async def test_aborted_run_does_not_call_the_model(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """上游已判死（`_aborted`）→ 不产生任何外部副作用，也就不花一次模型调用费。"""
    fake = _FakeLlm(llm.ExplainResult.success(explanations={"a1": "x"}, suggestions={}))
    adapters.set_llm_client(fake)
    state = _state(status="failed")

    update = await nodes.explain_node.__wrapped__(  # type: ignore[attr-defined]
        state, _config(runtime)
    )

    assert update == {}
    assert fake.requests == []


def test_config_from_env_treats_empty_key_as_disabled() -> None:
    """`LLM_API_KEY` 为空 = 没启用（合法状态），不是配置错误。"""
    config = llm.config_from_env({})
    assert config.enabled is False
    assert llm.build_llm_client(config) is None

    enabled = llm.config_from_env({"LLM_API_KEY": "sk-test"})
    assert enabled.enabled is True
    client = llm.build_llm_client(enabled)
    assert client is not None
    assert client.endpoint.endswith("/chat/completions")


def test_bad_timeout_is_a_startup_error_not_a_silent_default() -> None:
    """`LLM_TIMEOUT_S` 写坏必须在**启动期**炸（与 §8.1 同一条规矩），不许悄悄取默认值。"""
    from app.errors import ConfigError

    with pytest.raises(ConfigError):
        llm.config_from_env({"LLM_API_KEY": "sk-test", "LLM_TIMEOUT_S": "abc"})
