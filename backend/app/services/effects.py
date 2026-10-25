"""效果快照与调度 —— 对应 docs/CONTRACT.md §5.9。

核心设计（ADR-003）：**图内不等待 T+1/T+3/T+7**。`record_effect` 节点只为每个成功执行的
动作写 `effect_snapshot` 行（`due_at = executed_at + offset_days`，`status="scheduled"`）然后
立刻返回；真正的采样由独立调度器按 `due_at <= now` 拉取。这样一次图执行是秒级的，
"等三天"变成表里的一行数据（可重放、可审计、可独立扩缩）。

归因文案（§5.9.3）是硬性的：没有对照组时**禁止**出现"提升/带来/导致/因为"这类因果断言。
`conclusion` 由 :func:`build_effect_report` 统一生成，测试里对非 A/B 情形做词表断言。

窗口为什么是 T+0/T+1/T+3/T+7（`settings.effects.windows_days`，必须是 `[0,1,3,7]` 的子集）：

| 窗口 | 回答的问题 | 为什么不能省 |
| --- | --- | --- |
| T+0 | 动作落地了吗（价格/库存现在是多少） | 它是**唯一**能区分"执行失败"与"执行成功但没效果"的窗口 |
| T+1 | 次日指标（日粒度 GMV/退款率第一次可读） | 改价当天的影响混着"当天剩余时长"，T+1 才是第一个完整日 |
| T+3 | 短期趋势是否延续 | 一天的波动不足以支撑"改回去"的决定 |
| T+7 | 一个完整自然周 | 抹掉周末/工作日的结构性差异；也是本仓允许的最长观察期 |

`windows_days` 由配置给，本文件不校验"必须是子集"（那是 `app.config` 的加载期职责），但
`offsets = sorted({...})` 会去重并升序——同一批快照不允许出现两个 T+1 行，否则
`UNIQUE(action_log_id, offset_days)` 会在 `flush` 时才炸。

`results` 的形状（调用方传进来的列表）来自 `nodes.execute_actions`：每项至少有
`{action_id, idempotency_key, status, executed_at}`。本文件只用三样：`status`（筛 succeeded）、
`idempotency_key`（当 `action_log` 主键查行）、`executed_at`（算 due_at）。**不看**
`action_id`：执行身份是幂等键，`action_id` 只是提议身份——参数被 `edit` 过之后两者会分叉。

"无 A/B 只说明相关"这条在代码里的落点有两处，缺一不可：① `attribution` 恒为
`single_arm_before_after`（**不是** A/B，别在别处"猜"一个出来）；② `BASE_CAVEATS` 随每个
快照与每份报告一起下发，让"没有对照组"这件事跟着数据走、而不是只写在文档里。
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
#:
#: 这两条**跟着数据走**（存在每一行 `effect_snapshot.caveats` 里），不是只出现在报告里：
#: 报告是可以被单独复制出去的，而快照行才是"这条结论当时是在什么前提下得出的"的存档。
BASE_CAVEATS: tuple[str, ...] = (
    "无对照组，指标变化可能来自季节性/活动/竞品等外部因素",
    "归因强度：弱（单臂前后对比）",
)

#: §5.9.3：非 A/B 时**禁止**出现的因果断言词（否定式"不能归因于"除外）
#:
#: 这份词表是给**测试**用的断言清单（`conclusion` 不得包含它们），代码里不拿它过滤文案——
#: 过滤会让"写错了"变成"被悄悄删掉"，而断言会让它立刻红。改文案时先看这条注释：
#: 想表达因果就得先有对照组（`Attribution.AB_TEST`），否则只能写"相关/同期变化"。
FORBIDDEN_CAUSAL_WORDS: tuple[str, ...] = ("提升", "带来", "导致", "因为", "归因于本次")

# `effect_snapshot.status` 的三个字面量（**没有**枚举，就这么三个字符串，全仓 grep 得到）：
#   "scheduled" → 已登记等评估（`due_at <= now` 时被调度器捞走）
#   "evaluated" → 已采样（有 baseline/followup/delta）
#   "missed"    → 终态：到点 7 天后仍读不到目标，或 action_log 行丢了
#
# 为什么是裸字符串而不是枚举：这三态还会出现在 SQL 的 `WHERE status = 'scheduled'` 和前端
# 文案里，做成枚举只会多一层转换、不会少一处字面量。代价是**改字面量必须全仓 grep**：
# 调度器查询、`build_effect_report` 的 pending 判断、前端过滤三处都硬编码了它们。
#
# ⚠ 接线现状（读代码时别假设它已生效）：`run_effect_evaluator_once` 在整个 `backend/` 里
# **只有**定义和 `tests/fixtures/demo/run_demo.py` 的调用；`effects.scheduler_enabled` 目前
# 只被配置校验读过。也就是说生产路径上没有任何循环会去评估到期的快照——文档里说的"独立
# 调度器"是设计目标，不是已接线的事实。写文案/看板时不要声称"T+1 会自动算出来"。


# 四个 id 别混（排障时最常见的误解就是"都是 id，应该能互相查"）：
#
#   run_id         一次图执行的主键（`run_*`）；报告/审计按它聚合
#   action_id      提议身份 = `run:type:target`（`actions.action_id_for`）；审批行主键
#   action_log_id  **执行身份** = `idempotency_key`（不含 run_id）；`effect_snapshot` 挂在它上面
#   snapshot_id    = sha256(f"{action_log_id}|{offset_days}")[:32]；只标识"某个观察窗"
#
# 所以"同一动作被重放"时：run_id 变、action_id 可能不变、idempotency_key 不变（参数没被 edit），
# 于是新的 run 会**复用**旧的 effect_snapshot 行（这是想要的：效果只评估一次）。
def snapshot_id_for(*, action_log_id: str, offset_days: int) -> str:
    """§5.9.2：`sha256(f"{action_log_id}|{offset_days}")[:32]`。

    保证：同一个 (执行身份, 窗口) 永远得到同一个 id —— 这是"重复登记不产生第二行"的基础
    （配合库里的 `UNIQUE(action_log_id, offset_days)`）。
    不保证：全局唯一。截断到 32 hex 之后碰撞概率仍然极低，但没有做全量唯一性证明——真正的
    唯一约束在库上，不在这个函数里。
    坑：`offset_days` 参与哈希，所以**同一执行身份下 T+1 与 T+3 是两个不同的 snapshot_id**；
    不要拿 snapshot_id 当"这条动作的 id"用（那是 `action_log_id`）。
    """
    return hashlib.sha256(f"{action_log_id}|{offset_days}".encode("utf-8")).hexdigest()[:32]


def _as_window(row: EffectSnapshot) -> EffectWindow:
    """ORM 行 → `EffectWindow`（只带 4 个字段，其余留给报告层）。

    坑：`int(row.offset_days)` 显式转换——sqlite 的 SmallInteger 可能被读成字符串/Decimal，
    不转换会让 `sorted()` 与前端比较出现意外。
    """
    return EffectWindow(
        offset_days=int(row.offset_days),
        due_at=_iso(row.due_at),
        snapshot_id=row.snapshot_id,
        status=row.status,
    )


def _iso(value: datetime | None) -> str:
    """DB datetime → 契约时间串（UTC + `Z`）；`None` → 空串。

    坑：`from app.db import to_utc` 在函数内（避免 services → db 的模块级环依赖）；
    空串（不是 None）是 `EffectWindow.due_at` 的既定形态。
    """
    from app.db import to_utc

    if value is None:
        return ""
    resolved = to_utc(value)
    return resolved.isoformat().replace("+00:00", "Z") if resolved else ""


# 调用顺序（谁在什么时候调这两件事，顺序错了就是"登记不上"或"永远评估不了"）：
#
#   execute 节点：先 `insert_action_log(status=pending)` → 调上游 → `update_action_log`
#                 ⇒ 只有 `succeeded` 的行才会被下一步看到
#   record_effect 节点：`register_effect_snapshots(results=…)`  ← 本文件的第一个函数
#   （调度器 tick）：`run_effect_evaluator_once()`  ← 本文件的第二个函数
#
# 这两步之间**没有**任何图内等待（ADR-003）：登记完立刻返回，"等三天"是表里的一行数据。
async def register_effect_snapshots(
    *,
    session: Any,
    run_id: str,
    shop_id: str,
    results: Sequence[ExecutionResult],
    windows_days: Sequence[int],
    now: datetime | None = None,
) -> list[EffectWindow]:
    """为每个**成功执行**的动作登记 `offset_days ∈ windows_days` 的快照行（幂等 upsert）。

    保证：① 只为 `status == succeeded` 的执行结果登记（失败/未执行的动作不会有观察窗）；
    ② 同一 (action_log_id, offset_days) 重复调用只更新/复用已有行，不新增第二行；③ `due_at`
    以**执行时刻**为基准（`executed_at + offset_days`），不是以"现在"为基准——否则重跑一次
    就会把观察窗整体往后推。
    不保证：`results` 的顺序、也不保证同一个 action 在 `results` 里只出现一次（幂等靠查库，
    不靠入参去重）。
    坑：① `windows_days` 出现负数直接 `ValueError`（不是跳过）——负 offset 会造出"due_at 在
    执行之前"的行，调度器一上来就把它判成过期；
    ② 执行成功但 `action_log` 行不存在 → `ValueError`：这说明调用顺序错了（必须先落
    `action_log` 再登记效果）。这里**不兜底**自动插一条 log，因为 log 的 `request_params`
    是 baseline 的唯一来源，编不出来的东西不应该假装有；
    ③ `IntegrityError` 分支里 `rollback()` 后**重新抛出**（与本仓 approval_request 的 upsert
    不同）——效果快照是纯派生数据，撞车时"这次 tick 失败、下个 tick 再来"比"吞掉"更安全。
    """
    stamp = now or utcnow()
    # 去重 + 升序：`sorted({...})` 让同一批不会出现两个 T+1（否则 UNIQUE 在 flush 才炸），
    # 且登记顺序与窗口大小一致（排障时日志读起来是 T+0/T+1/T+3/T+7）。
    offsets = sorted({int(offset) for offset in windows_days})
    if any(offset < 0 for offset in offsets):
        raise ValueError(f"windows_days 不能为负：{offsets}")

    windows: list[EffectWindow] = []
    for result in results:
        if str(result["status"]) != ActionLogStatus.SUCCEEDED.value:
            # 非 succeeded（失败/unknown/未执行）不登记：给一个"没生效的动作"建观察窗，
            # 之后评估出来的 delta 会被当成"动作无效"，那是彻头彻尾的错误结论。
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
                    # action_type/target 从 action_log 行上抄，而不是从 results 里取：
                    # results 是"这次执行的结果视图"，而这两个字段是**执行身份**的一部分，
                    # 单一事实源在库里那一行上。
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
            # 已有的行**不更新**（包括 due_at）：重放登记时把 due_at 往后推，会让一个本该
            # 今天评估的观察窗被推迟——那是"数据看起来都对、结论永远来不了"。
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
        # 这里直接重新抛出而不是像 approvals 那样重查：快照是派生数据，下个 tick 重试即可，
        # 而"重查后静默成功"会掩盖"insert 一直失败"的真实故障。
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

    保证：① 一次 tick 最多取 200 行（`limit(200)`）——积压时每个 tick 的耗时有上界，配合
    `poll_interval_s` 不会让一次采样把上游打爆；② `products` 列表**每个 tick 只拉一次**
    （`if products is None` 惰性拉取），N 条快照不会变成 N 次 `list_products`；
    ③ 返回的是"真正评估成功的条数"，`missed` 不计入——用返回值做监控指标时不会把"没看成"
    混成"看成了"。
    不保证：上游 `list_products` 的分页完整性（只拉 page=0/size=100）。目标在第 101 个之后
    时会被判成"读不到"，超过 7 天就变 `missed`——这是已知局限，宁可少看也不假装看过。
    坑：① `baseline` 用 `log_row.request_params`（执行时真正下发的参数），**不是**当时的
    商品快照——所以 `delta` 是"参数 vs 当前状态"，不是"指标前后对比"；
    ② `_to_utc(row.due_at)` 的 fallback 是 `utcnow()`：`due_at` 为空的行会被立刻判成
    "超过 7 天"→ `missed`，这一行看起来像 bug，其实是"没有 due_at 的脏行"该有的归宿。
    """
    stamp = now or utcnow()
    # 排序 + limit 是"公平性"的唯一来源：按 due_at 升序，先到期的先评估，积压时不会饿死
    # 最早的观察窗。
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
            # 没有 action_log 就没有 baseline，评估出来也没有意义 → 直接 missed（终态）。
            # 不保持 scheduled：缺 log 行不是"暂时读不到"，重试一万次也还是缺。
            row.status = "missed"
            row.evaluated_at = stamp
            continue
        if products is None:
            response = await client.list_products(page=0, size=100)
            products = _extract_products(response)
        current = _find_product(products, row.target)
        if current is None:
            # 7 天是"上游商品可能还在但接口没返回"的容忍上限；超过就认账（missed）。
            # 用 `>` 而不是 `>=`：正好第 7 天还愿意再试一次。
            if (stamp - _to_utc(row.due_at)) > timedelta(days=7):
                row.status = "missed"
                row.evaluated_at = stamp
            continue
        baseline = dict(log_row.request_params or {})
        # followup 只取三个**动作可影响**的字段：价格/库存/上下架。取全量会让 delta 里混进
        # 标题、销量这类"动作没动过、但上游会变"的字段，读起来像"动作带来了变化"。
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
    # 只 flush 不 commit：事务边界归调用方（`session_scope`）。在一个 tick 里对 200 行做一次
    # flush 已经足够（不是逐行 flush），别改成"每行一次"——那会让 sqlite 的写锁持有时间
    # 从毫秒级涨到秒级。
    session.flush()
    return evaluated


def _to_utc(value: datetime | None) -> datetime:
    """`to_utc` 的"永不返回 None"包装（缺值按 `utcnow()`）。

    坑：这个 fallback 只用于算"距今多久"，不要拿它当业务时间——`due_at` 为空本身就是脏数据。
    """
    from app.db import to_utc

    resolved = to_utc(value)
    return resolved if resolved is not None else utcnow()


def _extract_products(response: Any) -> list[Mapping[str, Any]]:
    """从上游响应里抠出商品列表（`data.products` / `data.items`，两处都试）。

    保证：返回值里每一项都是 `Mapping`（过滤掉 None/字符串），下游的 `.get()` 不会炸。
    不保证：`response` 的结构——拿不到就返回空列表，于是本次 tick 的所有快照都会走
    "读不到目标"的分支（保持 scheduled 或超期 missed），**不会**假装评估过。
    坑：`getattr(response, "data", None)` 而不是 `response["data"]`：这里接受过
    `DoudianResponse`（属性）与 dict-like 两种形态，改成一个会让 dry-run/单测的另一半崩。
    """
    data = getattr(response, "data", None)
    if isinstance(data, Mapping):
        items = data.get("products") or data.get("items") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, Mapping)]
    return []


def _find_product(products: Sequence[Mapping[str, Any]], target: str) -> Mapping[str, Any] | None:
    """按 `target`（可能是 sku_id 也可能是 product_id）在商品列表里找那一条。

    保证：先比 `sku_id`、再比 `product_id`——与 `actions._target_id` 的优先序一致，所以
    "当初按 sku 建的动作"一定能找回来。
    不保证：唯一性。两个 SKU 的 `product_id` 相同（同商品多规格）时，只按 `product_id` 命中
    会返回**第一个**匹配项，可能不是当初那个规格。
    坑：`str(...) == str(target)` 双侧转换：上游给的 id 可能是 int，`target` 一定是字符串，
    不转换会让"看起来一样的 id"永远匹配不上（这类 bug 只在真实响应上出现）。
    """
    for product in products:
        for key in ("sku_id", "product_id"):
            if str(product.get(key)) == str(target):
                return product
    return None


def _delta(baseline: Mapping[str, Any], followup: Mapping[str, Any]) -> dict[str, Any]:
    """逐字段的数值差（`{"before": x, "after": y, "diff": y - x}`）。

    保证：只处理**两侧都是数值**的字段（bool 也算数值，这里没排除——`is_on_sale` 的
    `True -> False` 会得到 diff=-1，是想要的）。
    不保证：字符串/None 的字段出现在结果里。它们被**静默跳过**（不是记 null），所以
    "delta 里没有这个字段"≠"这个字段没变"。
    坑：① `baseline` 是 `action_log.request_params`（执行时真正下发的动作参数，例如
    `{"price_cents": 9900}` / `{"stock": 0}`），不是"当时的指标快照"——所以 delta 比的是
    "下发的参数 vs 现在的商品状态"，同名才有意义（`price_cents`/`stock` 同名，能比）；
    `followup` 里的 `is_on_sale` 在 baseline 里没有对应参数，所以**永远不会有**它的 delta 项；
    ② 字符串/None 字段被静默跳过，"delta 里没有这个字段"≠"这个字段没变"。
    """
    delta: dict[str, Any] = {}
    for key, before in baseline.items():
        after = followup.get(key)
        # `isinstance(..., (int, float))` 让 bool 也进来（`is_on_sale` 的 True→False 得 -1），
        # 但 None/字符串/缺失都会落到 else 之外——不写 null，因为"没比"和"差值是 null"
        # 在审计里是两件事。
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            delta[key] = {"before": before, "after": after, "diff": after - before}
    return delta


# 报告文案的四条禁令（§5.9.3；违反会被 `FORBIDDEN_CAUSAL_WORDS` 的词表断言抓住）：
#   ① 不写"提升/带来/导致/因为" —— 没有对照组，这些词都是因果断言；
#   ② 不写"效果为 X" —— 图内没有测量结果，只有"窗口已登记"；
#   ③ 不写 delta 数字 —— `windows` 里没有 delta 字段，编一个就是伪造测量；
#   ④ 不把"没有窗口"说成"没有效果" —— 那是把缺失当结论。
# 换句话说：本函数只回答"接下来会在什么时候评估"，不回答"有没有用"。
def build_effect_report(
    *,
    run_id: str,
    windows: Sequence[EffectWindow],
    attribution: Attribution | str,
    now: datetime | None = None,
) -> EffectReport:
    """生成占位报告。图内**没有**测量结果，所以文案必须说"还没到评估时间"（ADR-003）。

    保证：非 A/B 情形下的 `conclusion` 一定包含"未做对照实验，不能归因于本次动作"这类限定，
    且 `caveats` 恒为 `BASE_CAVEATS`（不管有没有窗口）——"没有对照组"这件事不随数据是否
    齐全而消失。
    不保证：`conclusion` 里的数字是"效果"。它只报**窗口**（几个、哪几天），不报 delta：
    图内那一刻根本还没有 delta，编一个出来就是伪造测量。
    坑：① `attribution` 传进来的是**调用方的声明**，不是检测结果——传 `ab_test` 却没有任何
    对照组数据时，文案会变成"待调度器采样后按组间差异评估"，读起来像"我们做了 A/B"。
    所以这个参数只允许由真正建立了对照的调用方传 `AB_TEST`；
    ② 三个分支的顺序有意义：先算 `pending`，再判断"有窗口但都已评估"，最后 `ab_test`
    **覆盖**前两者。A/B 的文案必须赢，否则 A/B 场景会落到"尚未到评估时间"的通用文案上。
    """
    resolved = Attribution(str(attribution))
    pending = [window for window in windows if window["status"] == "scheduled"]
    if windows and not pending:
        offsets = sorted({int(window["offset_days"]) for window in windows})
        conclusion = (
            f"已登记 {len(windows)} 个观察窗（T+{', T+'.join(str(o) for o in offsets)}），"
            "效果由调度器按 due_at 评估；未做对照实验，不能归因于本次动作。"
        )
    else:
        # 这个 else 同时覆盖"没有窗口"和"还有窗口没到期"两种情形：两种情况都只能说
        # "尚未到达评估时间"——把"没有窗口"说成"没效果"是另一种伪造结论。
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
