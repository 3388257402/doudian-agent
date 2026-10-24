"""效果快照与调度 —— 对应 docs/CONTRACT.md §5.9。

核心设计（ADR-003）：**图内不等待 T+1/T+3/T+7**。`record_effect` 节点只为每个成功执行的
动作写 `effect_snapshot` 行（`due_at = executed_at + offset_days`，`status="scheduled"`）然后
立刻返回；真正的采样由独立调度器按 `due_at <= now` 拉取。这样一次图执行是秒级的，
"等三天"变成表里的一行数据（可重放、可审计、可独立扩缩）。

归因文案（§5.9.3）是硬性的：没有对照组时**禁止**出现"提升/带来/导致/因为"这类因果断言。
`conclusion` 由 :func:`build_effect_report` 统一生成，测试里对非 A/B 情形做词表断言。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.state import (
    EffectReport,
    EffectWindow,
    ExecutionResult,
    parse_utc_iso,
    utc_iso,
    utcnow,
)
from app.enums import ActionLogStatus, Attribution
from app.models import ActionLog, EffectSnapshot

#: §5.9.3 要求的固定 caveats（至少这两条）
BASE_CAVEATS: tuple[str, ...] = (
    "无对照组，指标变化可能来自季节性/活动/竞品等外部因素",
    "归因强度：弱（单臂前后对比）",
)

#: §5.9.3：非 A/B 时**禁止**出现的因果断言词（否定式"不能归因于"除外）
FORBIDDEN_CAUSAL_WORDS: tuple[str, ...] = ("提升", "带来", "导致", "因为", "归因于本次")


def snapshot_id_for(*, action_log_id: str, offset_days: int) -> str:
    """§5.9.2：`sha256(f"{action_log_id}|{offset_days}")[:32]`。"""
    return hashlib.sha256(f"{action_log_id}|{offset_days}".encode("utf-8")).hexdigest()[:32]


def _as_window(row: EffectSnapshot) -> EffectWindow:
    return EffectWindow(
        offset_days=int(row.offset_days),
        due_at=_iso(row.due_at),
        snapshot_id=row.snapshot_id,
        status=row.status,
    )


def _iso(value: datetime | None) -> str:
    from app.db import to_utc

    if value is None:
        return ""
    resolved = to_utc(value)
    return resolved.isoformat().replace("+00:00", "Z") if resolved else ""


async def register_effect_snapshots(
    *,
    session: Any,
    run_id: str,
    shop_id: str,
    results: Sequence[ExecutionResult],
    windows_days: Sequence[int],
    now: datetime | None = None,
) -> list[EffectWindow]:
    """为每个**成功执行**的动作登记 `offset_days ∈ windows_days` 的快照行（幂等 upsert）。"""
    stamp = now or utcnow()
    offsets = sorted({int(offset) for offset in windows_days})
    if any(offset < 0 for offset in offsets):
        raise ValueError(f"windows_days 不能为负：{offsets}")

    windows: list[EffectWindow] = []
    for result in results:
        if str(result["status"]) != ActionLogStatus.SUCCEEDED.value:
            continue
        action_log_id = str(result["idempotency_key"])
        executed_at = parse_utc_iso(result["executed_at"]) if result.get("executed_at") else stamp
        log_row = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == action_log_id)
        ).scalar_one_or_none()
        if log_row is None:
            # 执行成功却没有 action_log 行 = 调用顺序错了（必须先落 action_log 再登记效果）。
            raise ValueError(
                f"action_log 缺少 idempotency_key={action_log_id}，无法登记效果快照"
            )
        existing = {
            int(row.offset_days): row
            for row in session.execute(
                select(EffectSnapshot).where(EffectSnapshot.action_log_id == action_log_id)
            ).scalars()
        }
        for offset in offsets:
            due_at = executed_at + timedelta(days=offset)
            row = existing.get(offset)
            if row is None:
                row = EffectSnapshot(
                    snapshot_id=snapshot_id_for(action_log_id=action_log_id, offset_days=offset),
                    action_log_id=action_log_id,
                    run_id=run_id,
                    shop_id=shop_id,
                    action_type=log_row.action_type,
                    target=log_row.target,
                    offset_days=offset,
                    status="scheduled",
                    due_at=due_at,
                    evaluated_at=None,
                    baseline=None,
                    followup=None,
                    delta=None,
                    attribution=Attribution.SINGLE_ARM_BEFORE_AFTER.value,
                    caveats=list(BASE_CAVEATS),
                    created_at=stamp,
                )
                session.add(row)
                existing[offset] = row
            windows.append(
                EffectWindow(
                    offset_days=offset,
                    due_at=_iso(due_at),
                    snapshot_id=snapshot_id_for(action_log_id=action_log_id, offset_days=offset),
                    status=row.status,
                )
            )
    try:
        session.flush()
    except IntegrityError:
        # 并发登记同一 (action_log_id, offset_days)：回滚后重查（§6.3 禁止 ON CONFLICT）。
        session.rollback()
        raise
    return windows


async def run_effect_evaluator_once(
    *, session: Any, client: Any, now: datetime | None = None
) -> int:
    """调度器的一个 tick：把 `due_at <= now` 的 `scheduled` 快照评估掉。返回评估条数。

    评估方式（无对照组时的诚实做法）：读上游**当前**状态作为 `followup`，把当初下发/审批后的
    参数作为 `baseline`，`delta` 只记录两者的**数值差**，不写任何因果结论。
    读不到目标（商品已下架/删除）时保持 `scheduled` 让下个 tick 再试；超过 7 天仍未读到 →
    `missed`（"我们确实没看成"，比假装评估过更可信）。
    """
    stamp = now or utcnow()
    rows = list(
        session.execute(
            select(EffectSnapshot)
            .where(EffectSnapshot.status == "scheduled", EffectSnapshot.due_at <= stamp)
            .order_by(EffectSnapshot.due_at.asc())
            .limit(200)
        ).scalars()
    )
    if not rows:
        return 0

    products: list[Mapping[str, Any]] | None = None
    evaluated = 0
    for row in rows:
        log_row = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == row.action_log_id)
        ).scalar_one_or_none()
        if log_row is None:
            row.status = "missed"
            row.evaluated_at = stamp
            continue
        if products is None:
            response = await client.list_products(page=0, size=100)
            products = _extract_products(response)
        current = _find_product(products, row.target)
        if current is None:
            if (stamp - _to_utc(row.due_at)) > timedelta(days=7):
                row.status = "missed"
                row.evaluated_at = stamp
            continue
        baseline = dict(log_row.request_params or {})
        followup = {
            key: current.get(key)
            for key in ("price_cents", "stock", "is_on_sale")
            if key in current
        }
        row.baseline = baseline
        row.followup = followup
        row.delta = _delta(baseline, followup)
        row.status = "evaluated"
        row.evaluated_at = stamp
        evaluated += 1
    session.flush()
    return evaluated


def _to_utc(value: datetime | None) -> datetime:
    from app.db import to_utc

    resolved = to_utc(value)
    return resolved if resolved is not None else utcnow()


def _extract_products(response: Any) -> list[Mapping[str, Any]]:
    data = getattr(response, "data", None)
    if isinstance(data, Mapping):
        items = data.get("products") or data.get("items") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, Mapping)]
    return []


def _find_product(products: Sequence[Mapping[str, Any]], target: str) -> Mapping[str, Any] | None:
    for product in products:
        for key in ("sku_id", "product_id"):
            if str(product.get(key)) == str(target):
                return product
    return None


def _delta(baseline: Mapping[str, Any], followup: Mapping[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key, before in baseline.items():
        after = followup.get(key)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            delta[key] = {"before": before, "after": after, "diff": after - before}
    return delta


def build_effect_report(
    *,
    run_id: str,
    windows: Sequence[EffectWindow],
    attribution: Attribution | str,
    now: datetime | None = None,
) -> EffectReport:
    """生成占位报告。图内**没有**测量结果，所以文案必须说"还没到评估时间"（ADR-003）。"""
    resolved = Attribution(str(attribution))
    pending = [window for window in windows if window["status"] == "scheduled"]
    if windows and not pending:
        offsets = sorted({int(window["offset_days"]) for window in windows})
        conclusion = (
            f"已登记 {len(windows)} 个观察窗（T+{', T+'.join(str(o) for o in offsets)}），"
            "效果由调度器按 due_at 评估；未做对照实验，不能归因于本次动作。"
        )
    else:
        conclusion = "效果窗口已登记，尚未到达评估时间（最早 T+1）。"
    if resolved is Attribution.AB_TEST:
        conclusion = "已登记 A/B 对照观察窗，待调度器采样后按组间差异评估。"
    return EffectReport(
        run_id=run_id,
        generated_at=utc_iso(now),
        attribution=resolved,
        windows=list(windows),
        conclusion=conclusion,
        caveats=list(BASE_CAVEATS),
    )


__all__ = [
    "BASE_CAVEATS",
    "FORBIDDEN_CAUSAL_WORDS",
    "build_effect_report",
    "register_effect_snapshots",
    "run_effect_evaluator_once",
    "snapshot_id_for",
]
