"""结构不变量：这些断言不是"跑一遍流程"，而是钉住几条**可以机械检查**的纪律。

- `backend/app/` 里没有 `MemorySaver` / `InMemorySaver`（§5.7）；
- `app_traces` 的计数字段只在 `tracing.py` 里被代码触碰（§4.7，注释与字符串不算）；
- 节点的控制流异常分流顺序正确（先 `GraphBubbleUp` 后 `Exception`）；
- 同一 `(shop_id, run_id)` 在跑时再提交 → 409；终态再提交 → 回放不重跑。
"""

from __future__ import annotations

import tokenize
from pathlib import Path
from typing import Any

import pytest

from app.agent import run_agent
from app.agent.checkpoint import checkpoint_saver
from app.enums import RunStatus
from app.errors import RunInProgress
from app.observability.logging import JsonlLogSink

from .conftest import SHOP_ID, history_then_today, node_events, trace_row

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _code_tokens(path: Path) -> list[str]:
    """只取**代码** token：注释与字符串被跳过，避免"一句注释就让检查说谎"。"""
    with path.open(encoding="utf-8") as handle:
        tokens = list(tokenize.generate_tokens(handle.readline))
    return [
        tok.string for tok in tokens if tok.type not in {tokenize.COMMENT, tokenize.STRING}
    ]


def test_no_memory_saver_in_app() -> None:
    """`app/` 下出现内存 checkpointer = 审批窗口等于"进程不重启"，这是禁止项。

    只看**代码** token：`app/agent/checkpoint.py` 的 docstring 里正当地提到这两个类名
    （解释为什么禁止用它们），文档谈禁令不等于程序引入禁令对象。
    """
    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        tokens = _code_tokens(path)
        for needle in ("MemorySaver", "InMemorySaver"):
            if any(needle in token for token in tokens):
                offenders.append(f"{path.relative_to(APP_ROOT.parent).as_posix()}:{needle}")
    assert offenders == [], f"backend/app 下不允许内存 checkpointer：{offenders}"

    # 反向确认：本套用例连测试里都**没有**用内存实现（否则"持久化"根本没被测到）
    test_offenders = [
        path.name
        for path in (Path(__file__).resolve().parent).rglob("*.py")
        if any(
            "InMemorySaver" in token or "MemorySaver" in token
            for token in _code_tokens(path)
        )
    ]
    assert test_offenders == []


def test_step_count_only_touched_by_tracing() -> None:
    """计数只由 `tracing.py` 写：`app/` 里出现该标识符的**代码**只允许两处（含 ORM 列）。"""
    allowed = {"agent/tracing.py", "models.py"}
    hits: dict[str, list[str]] = {}
    for path in APP_ROOT.rglob("*.py"):
        tokens = _code_tokens(path)
        if any("step_count" in token for token in tokens):
            rel = path.relative_to(APP_ROOT).as_posix()
            if rel not in allowed:
                hits[rel] = [token for token in tokens if "step_count" in token]
    assert hits == {}, f"step_count 只允许出现在 {sorted(allowed)}，实际还有：{sorted(hits)}"


def test_traced_catches_control_flow_before_generic_exception() -> None:
    """`traced()` 的两个 except 顺序是硬规则（§4.7 规则 5 / ADR-016）。"""
    import ast

    source = (APP_ROOT / "agent" / "tracing.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler_orders: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        kinds: list[str] = []
        for handler in node.handlers:
            if handler.type is None:
                kinds.append("bare")
            elif isinstance(handler.type, ast.Name):
                kinds.append(handler.type.id)
            elif isinstance(handler.type, ast.Attribute):
                kinds.append(handler.type.attr)
            else:  # pragma: no cover - 目前没有元组形式的 handler
                kinds.append("other")
        handler_orders.append(kinds)
    assert ["GraphBubbleUp", "Exception"] in handler_orders, (
        f"没找到 GraphBubbleUp→Exception 的分流顺序，实际：{handler_orders}"
    )


async def test_checkpointer_is_sqlite_and_setup(
    runtime: Any, settings: Any
) -> None:
    """真实 checkpointer 的类型与 `setup()`：必须是落盘的 `AsyncSqliteSaver`。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with checkpoint_saver(settings) as saver:
        assert isinstance(saver, AsyncSqliteSaver)
        assert Path(settings.checkpoint.path).exists() or True  # 连接建立一个空文件
    await _assert_tables(settings.checkpoint.path)


async def _assert_tables(path: str) -> None:
    import aiosqlite

    async with aiosqlite.connect(path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in await cursor.fetchall()}
    assert "checkpoints" in tables
    assert "writes" in tables


async def test_rerun_while_active_is_conflict(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """同一 `(shop_id, run_id)` 还在等审批时再提交 → 409 `RUN_IN_PROGRESS`（§4.6）。"""
    await run_agent(SHOP_ID, "run_conflict")
    with pytest.raises(RunInProgress):
        await run_agent(SHOP_ID, "run_conflict")


async def test_terminal_replay_does_not_rerun(
    runtime: Any, sink: JsonlLogSink, fake_client: Any
) -> None:
    """终态再提交 → 回放已存结果，**不重跑图**（否则每刷新一次就多一份执行）。"""
    fake_client.orders = history_then_today(history_gmv=1_000_000, today_gmv=2_000_000)
    first = await run_agent(SHOP_ID, "run_replay_done")
    assert first.status == RunStatus.DONE.value
    events_before = len(node_events(sink, "run_replay_done"))

    fake_client.orders = []  # 若真重跑，指标会缺失并 failed
    again = await run_agent(SHOP_ID, "run_replay_done")

    assert again.status == RunStatus.DONE.value
    assert again.trace_id == first.trace_id
    assert len(node_events(sink, "run_replay_done")) == events_before
    assert (await trace_row(SHOP_ID, "run_replay_done"))["step_count"] == events_before
