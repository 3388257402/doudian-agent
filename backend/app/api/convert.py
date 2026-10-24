"""领域对象 → §7 响应模型的转换（只在入口层做，不改服务层/agent 的返回值形状）。

为什么需要转换而不是 `response_model` 自动吃：agent 与服务层返回的是**dataclass / TypedDict**
（`PendingApproval`、`EffectReport`），它们的字段名与 §7 一致，但 `created_at` 等时间字符串
来自不同模块，形状可能差一位（微秒 vs 毫秒）。把"对外的时间形状"收敛到入口层一处，
是让 §0 的时间规则（UTC、毫秒、`Z` 结尾）只被解释一次。

转换只做**形状**，不做业务：没有默认值填充、没有状态推断、没有兜底值。
`None` 就是 `None`（`Response` 里一律出现键，值为 `null`——§0 空值规则）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.agent.state import PendingApproval, parse_utc_iso
from app.schemas import AlertItem, EffectReport

__all__ = ["alert_item", "alert_items", "effect_report", "iso_z"]


def iso_z(value: str) -> str:
    """把任意 §0 形状的时间字符串规范成 `2026-10-23T14:25:30.000Z`（毫秒、`Z`）。

    为什么要"规范"而不是原样透传：`approval_request.created_at` 由 ORM 写入、经 `to_utc()`
    读出后是 Python `isoformat()` 形状（带 6 位微秒、`+00:00`），而 §7.3 的示例是毫秒 + `Z`。
    两种形状混在同一份响应里，前端按字符串比较/排序会得到假差异。
    调用方坑：非 ISO-8601 输入会抛 `ValueError`（→ 500）。这里是**形状闸门**，不是解析兜底：
    坏数据必须暴露，不能悄悄替换成"现在"。
    """
    if not value:
        return ""
    parsed = parse_utc_iso(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def alert_item(pending: PendingApproval) -> AlertItem:
    """`PendingApproval`（§4.1）→ §7.3/§7.2 的 `AlertItem`。

    为什么还要这一层转换（字段名明明一样）：领域对象是 dataclass、对外是 pydantic 模型，
    两者**各自演进**——`PendingApproval` 加字段不该自动出现在 HTTP 响应里（§7"未知字段禁止透传"），
    反过来模型加校验也不该拖累图内部。转换只做形状（含 `created_at` 规范化），不做默认值填充。
    调用方坑：`action_type`/`risk`/`status` 的取值域由领域层保证，pydantic 会**校验**它们；
    领域层一旦产生域外值，这里抛 `ValidationError` → 500（宁可炸，不要让前端收到未知枚举）。
    """
    return AlertItem(
        action_id=pending.action_id,
        run_id=pending.run_id,
        thread_id=pending.thread_id,
        shop_id=pending.shop_id,
        trace_id=pending.trace_id,
        interrupt_id=pending.interrupt_id,
        action_type=pending.action_type,  # type: ignore[arg-type]  # 领域层保证取值域
        target=pending.target,
        params=dict(pending.params),
        reason=pending.reason,
        evidence=dict(pending.evidence),
        risk=pending.risk,  # type: ignore[arg-type]
        status=pending.status,  # type: ignore[arg-type]
        created_at=iso_z(pending.created_at),
    )


def alert_items(pending: Sequence[PendingApproval]) -> list[AlertItem]:
    return [alert_item(item) for item in pending]


def effect_report(report: Mapping[str, Any] | None) -> EffectReport | None:
    """`state["effect_report"]`（§3.3 `EffectReport`）→ §7.4 的 `effect_report`。

    `None` 原样返回 `None`：没有执行过动作就没有效果报告，不能编一个空报告出来。
    """
    if not report:
        return None
    return EffectReport.model_validate(dict(report))
