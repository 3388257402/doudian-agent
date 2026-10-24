"""恢复路径的两条硬要求：**跨进程**（a）与**恢复值严格校验**（b）。

(a) 进程重启后未完成的 interrupt 仍能 resume —— 用真子进程证明，不是"再 new 一个 saver"
    这种同进程内的等价物（那种写法即使 checkpointer 是内存实现也照样通过）。
(b) 恢复值缺项/多项/重复/形状不对全部 400，且**被拒之后状态不能被弄脏**（还能正常恢复）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.agent import list_pending_approvals, resume_agent, run_agent
from app.enums import ApprovalStatus, RunStatus
from app.errors import ApprovalNotFound, ApprovalValidationError
from app.observability.logging import JsonlLogSink

from .conftest import (
    SHOP_ID,
    FakeDoudianClient,
    action_log_rows,
    approval_rows,
    decisions_for,
    node_events,
)

_CHILD = Path(__file__).resolve().parent / "_restart_child.py"

# ======================================================================================
# (a) 真·进程重启
# ======================================================================================


def test_resume_survives_process_restart(
    runtime: Any,
    settings: Any,
    sink: JsonlLogSink,
    fake_client: FakeDoudianClient,
    rules_path: Path,
) -> None:
    """父进程跑到挂起 → 关掉一切 → **新进程** 只靠 checkpoint 文件完成恢复。

    用普通 `def`（不是 async）：要把 `run_agent` 与子进程串在同一个用例里，async 版本里再
    `subprocess.run` 会阻塞事件循环，跑得通但语义混乱。
    """
    import asyncio

    run_id = "run_restart"
    result = asyncio.run(run_agent(SHOP_ID, run_id))
    assert result.status == RunStatus.AWAITING_APPROVAL.value
    pending = result.pending_approvals[0]
    sink.close()  # 父进程的日志汇关掉，模拟进程退出

    payload = {
        "database_url": settings.database.url,
        "checkpoint_path": settings.checkpoint.path,
        "log_file": settings.observability.log_file,
        "rules_path": str(rules_path.resolve()),
        "thread_id": result.thread_id,
        "interrupt_id": pending.interrupt_id,
        "decisions": decisions_for([pending.action_id]),
    }
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_CHILD.parents[2])
    completed = subprocess.run(  # noqa: S603 - 固定解释器 + 固定脚本，无外部输入拼接
        [sys.executable, str(_CHILD), json.dumps(payload)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_CHILD.parents[2]),
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    child = json.loads(completed.stdout.strip().splitlines()[-1])

    assert child["status"] == RunStatus.DONE.value
    assert [item["status"] for item in child["applied"]] == ["succeeded"]
    assert child["effect_windows"] == 4  # T+0/1/3/7
    assert child["price_updates"] == [
        {"product_id": "9001", "sku_id": "3400000000000000002", "price_cents": 9603}
    ]

    # 子进程写的是同一份业务库：证据在父进程这一侧可见
    logs = action_log_rows(run_id)
    assert len(logs) == 1
    assert logs[0]["status"] == "succeeded"
    rows = approval_rows(result.thread_id)
    assert rows[0]["status"] == "approved"

    # 新进程继续写同一条 JSONL：node.trace 事件跨进程累加到 9（父 5 + 子 4）
    from app.observability.logging import JsonlLogSink as Sink

    reopened = Sink(settings.observability.log_file)
    try:
        events = [
            row
            for row in reopened.read(run_id=run_id, event_prefix="node.trace", limit=500)
        ]
    finally:
        reopened.close()
    assert len(events) == 9
    assert sorted(row["node"] for row in events).count("approval_gate") == 2

# ======================================================================================
# (b) 恢复值严格校验
# ======================================================================================


async def _await_approval(run_id: str) -> tuple[Any, str]:
    result = await run_agent(SHOP_ID, run_id)
    assert result.status == RunStatus.AWAITING_APPROVAL.value
    pending = result.pending_approvals[0]
    return result, pending.action_id


async def test_resume_rejects_missing_decision(runtime: Any, sink: JsonlLogSink) -> None:
    result, _ = await _await_approval("run_missing")
    with pytest.raises(ApprovalValidationError) as excinfo:
        await resume_agent(result.thread_id, [])
    assert "missing" in excinfo.value.details  # type: ignore[union-attr]


async def test_resume_rejects_extra_decision(runtime: Any, sink: JsonlLogSink) -> None:
    result, action_id = await _await_approval("run_extra")
    decisions = decisions_for([action_id, "run_extra:price_update:不存在的目标"])
    with pytest.raises(ApprovalValidationError) as excinfo:
        await resume_agent(result.thread_id, decisions)  # type: ignore[arg-type]
    assert excinfo.value.details["extra"] == ["run_extra:price_update:不存在的目标"]  # type: ignore[index]


async def test_resume_rejects_duplicate_decision(runtime: Any, sink: JsonlLogSink) -> None:
    result, action_id = await _await_approval("run_dup")
    decisions = decisions_for([action_id, action_id])
    with pytest.raises(ApprovalValidationError) as excinfo:
        await resume_agent(result.thread_id, decisions)  # type: ignore[arg-type]
    assert excinfo.value.details["duplicates"] == [action_id]  # type: ignore[index]


@pytest.mark.parametrize(
    "verdict,edited",
    [
        ("edit", None),  # edit 必须带非空 edited_params
        ("edit", {}),  # 空 dict 也不行
        ("approve", {"price_cents": 9600}),  # 非 edit 必须为 null（不做宽容处理）
        ("reject", {"price_cents": 9600}),
    ],
)
async def test_resume_rejects_illegal_verdict_payload(
    runtime: Any, sink: JsonlLogSink, verdict: str, edited: dict[str, Any] | None
) -> None:
    result, action_id = await _await_approval(f"run_shape_{verdict}")
    decisions = [
        {
            "action_id": action_id,
            "verdict": verdict,
            "edited_params": edited,
            "operator": "ops_zhang",
            "reason": None,
            "decided_at": "2026-10-23T12:05:00Z",
        }
    ]
    with pytest.raises(ApprovalValidationError):
        await resume_agent(result.thread_id, decisions)  # type: ignore[arg-type]


async def test_resume_rejects_edit_params_outside_schema(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """`edit` 的参数必须能过 §5.5 的 schema：多一个键也是错的。"""
    result, action_id = await _await_approval("run_schema")
    decisions = [
        {
            "action_id": action_id,
            "verdict": "edit",
            "edited_params": {"price_cents": 9600, "未知键": 1},
            "operator": "ops_zhang",
            "reason": "顺便塞点别的",
            "decided_at": "2026-10-23T12:05:00Z",
        }
    ]
    with pytest.raises(ApprovalValidationError):
        await resume_agent(result.thread_id, decisions)  # type: ignore[arg-type]


async def test_rejected_resume_does_not_dirty_state(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """被拒的恢复请求必须**什么都没做**：审批行仍 pending、没有执行、还能正常恢复。"""
    result, action_id = await _await_approval("run_dirty")

    with pytest.raises(ApprovalValidationError):
        await resume_agent(result.thread_id, decisions_for([action_id])[:0])  # type: ignore[arg-type]

    rows = approval_rows(result.thread_id)
    assert rows[0]["status"] == ApprovalStatus.PENDING.value
    assert fake_client.price_updates == []
    assert action_log_rows("run_dirty") == []
    assert [row["node"] for row in node_events(sink, "run_dirty")].count("execute") == 0

    resumed = await resume_agent(
        result.thread_id, decisions_for([action_id]), expected_interrupt_id=None  # type: ignore[arg-type]
    )
    assert resumed.status == RunStatus.DONE.value
    assert fake_client.price_updates[0]["price_cents"] == 9603


async def test_resume_with_wrong_interrupt_id_is_rejected(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """拿旧页面的 interrupt_id 再点一次：必须 404，不许把裁决喂给当前挂起点。"""
    result, action_id = await _await_approval("run_wrong_iid")
    with pytest.raises(ApprovalNotFound):
        await resume_agent(
            result.thread_id,
            decisions_for([action_id]),  # type: ignore[arg-type]
            expected_interrupt_id="deadbeefdeadbeefdeadbeefdeadbeef",
        )
    assert approval_rows(result.thread_id)[0]["status"] == ApprovalStatus.PENDING.value


async def test_second_resume_is_duplicate_decision(
    runtime: Any, sink: JsonlLogSink, fake_client: FakeDoudianClient
) -> None:
    """同一批裁决提交两次：第二次必须是 409 语义（而不是再改一次价）。"""
    from app.errors import DuplicateDecision, RunStateConflict

    result, action_id = await _await_approval("run_twice")
    first = await resume_agent(result.thread_id, decisions_for([action_id]))  # type: ignore[arg-type]
    assert first.status == RunStatus.DONE.value

    with pytest.raises((DuplicateDecision, RunStateConflict)):
        await resume_agent(result.thread_id, decisions_for([action_id]))  # type: ignore[arg-type]
    assert len(fake_client.price_updates) == 1


async def test_list_pending_approvals_exposes_interrupt_id(
    runtime: Any, sink: JsonlLogSink
) -> None:
    """审批台只能从库里拿 `interrupt_id`：它必须在挂起之后就已经可读（§7.4 的前提）。"""
    result, action_id = await _await_approval("run_pending_list")
    pending = await list_pending_approvals()
    assert [item.action_id for item in pending] == [action_id]
    assert pending[0].interrupt_id == result.pending_approvals[0].interrupt_id
    assert len(pending[0].interrupt_id) == 32

