"""8 个图节点（CONTRACT §4.3 逐字签名）+ 节点运行期依赖。

## 节点怎么拿到 client / session / notifier

契约把节点签名冻结成 `(state, config)` —— 节点不能多收一个 `client` 或 `session` 参数。
所以依赖走两处，且只有这两处：

1. `config["configurable"]["runtime"] / ["client"] / ["now"]`：测试与上层显式注入；
2. 进程级单例 `AgentRuntime`（`app.main` lifespan 里 `set_runtime()`）。

散在 8 个节点里各自 `create_doudian_client()` 是不行的：ADR-004 要求一个进程一个
`httpx.AsyncClient`，限流桶与并发信号量都挂在那个实例上。

## 重放与幂等（ADR-010）

`interrupt()` 恢复会**从节点开头重放整个 `approval_gate`**。因此：

- `approval_request` 的 upsert 是 insert-if-absent（绝不回写 `status`）；
- 通知失败不阻断（重放会再通知一次，这是被接受的代价）；
- `action_log` 的唯一键是 `idempotency_key`（UNIQUE），重放/重试第二次是
  `skipped_duplicate` 而不是真的再改一次价。

## 副作用与状态

节点返回的是"该字段的完整目标值"（replace 语义，§3.1），不返回增量。
不做任何 `except Exception: pass`：异常要么被记成 `errors` 并让流程降级到 `finalize`，
要么原样抛给 LangGraph 的重试策略 / `run_agent`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from sqlalchemy import select

from app.agent.adapters import resolve_client, resolve_now, resolve_runtime
from app.agent.state import (
    ActionProposal,
    AgentError,
    AgentState,
    Anomaly,
    AuditEntry,
    Decision,
    ExecutionResult,
    MetricsSnapshot,
    RawSnapshot,
    append_audits,
    append_errors,
    business_date,
    make_agent_error,
    make_audit_entry,
    utc_iso,
    utcnow,
)
from app.agent.tracing import calibrate_run_steps, digest_of, traced, update_run_trace
from app.db import arun_db, session_scope
from app.enums import ActionLogStatus, ActionType, Attribution, ErrorCategory, RunStatus, Verdict
from app.errors import ApprovalValidationError, ParamValidationError
from app.models import ActionLog
from app.services.errors import (
    DoudianAmbiguousOutcomeError,
    DoudianAuthError,
    DoudianBusinessError,
    DoudianError,
)

__all__ = [
    "HISTORY_WINDOW_DAYS",
    "approval_gate",
    "collect",
    "compute_metrics_node",
    "detect_anomalies_node",
    "execute",
    "finalize",
    "insert_action_log",
    "is_retryable_doudian_error",
    "load_action_log",
    "propose_actions_node",
    "record_effect",
    "route_after_collect",
    "route_after_propose_actions",
    "update_action_log",
    "validate_resume_payload",
]

#: 近 7 日均值要 7 天历史，再加今天 = 8 个业务日（§5.3 的 `gmv_7d_avg_cents`）
HISTORY_WINDOW_DAYS = 7

#: 业务日期所属时区（§0：`date` 字段是 `Asia/Shanghai` 的 `YYYY-MM-DD`）
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

#: `action_log` 的终态里"已经成功过、不该再执行"的那两个
_NO_REEXECUTE_STATUSES = frozenset(
    {ActionLogStatus.SUCCEEDED.value, ActionLogStatus.SKIPPED_DUPLICATE.value}
)

#: 售后/退款状态词（归一化用；上游是字符串枚举，这里只做"是否涉及退款"的判断）
_REFUND_STATUSES = frozenset(
    {
        "REFUND",
        "REFUNDED",
        "REFUNDING",
        "AFTER_SALE",
        "APPLY_REFUND",
        "已退款",
        "退款中",
        "售后中",
    }
)


# ======================================================================================
# 通用小工具
# ======================================================================================


def _aborted(state: Mapping[str, Any]) -> bool:
    """前面的节点已经把 run 判死：后续节点不再产生任何副作用。"""
    return str(state.get("status") or "") == RunStatus.FAILED.value


def _audit(state: Mapping[str, Any], *, node: str, event: str, data: dict[str, Any] | None = None,
           at: str | None = None) -> list[AuditEntry]:
    return append_audits(
        {"audit": list(state.get("audit") or [])},  # type: ignore[arg-type]
        [make_audit_entry(node=node, event=event, data=data or {}, at=at)],
    )


def _errors(state: Mapping[str, Any], errors: Sequence[AgentError]) -> list[AgentError]:
    return append_errors({"errors": list(state.get("errors") or [])}, list(errors))  # type: ignore[arg-type]


def _shift_business_date(day: str, offset_days: int) -> str:
    base = date.fromisoformat(day)
    return (base + timedelta(days=offset_days)).isoformat()


def _business_date_of(value: Any, *, fallback: str) -> str:
    """把上游的时间（带时区 ISO / 无时区北京时间 / 秒/毫秒时间戳）折成业务日期。

    无时区的时间戳按 **Asia/Shanghai** 解释：抖店开放平台的 `create_time` 字段就是北京时间，
    把它当 UTC 会让"昨晚 23:30 的单"落到错误的一天，指标与规则一起错。
    """
    if isinstance(value, datetime):
        resolved = value if value.tzinfo else value.replace(tzinfo=_BUSINESS_TZ)
        return resolved.astimezone(_BUSINESS_TZ).strftime("%Y-%m-%d")
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, (int, float)):
        seconds = float(value) / (1000.0 if float(value) > 1e11 else 1.0)
        return datetime.fromtimestamp(seconds, UTC).astimezone(_BUSINESS_TZ).strftime("%Y-%m-%d")
    if not isinstance(value, str) or not value.strip():
        return fallback
    text = value.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return fallback
    resolved = parsed if parsed.tzinfo else parsed.replace(tzinfo=_BUSINESS_TZ)
    return resolved.astimezone(_BUSINESS_TZ).strftime("%Y-%m-%d")


def _money(value: Any, *, where: str) -> int:
    """金额归一化：`*_cents`/`*_yuan` 之外的裸字段按**整数分**读（抖店开放平台约定）。

    - `*_yuan` → `round(x*100)`；
    - 裸字段（`price` / `pay_amount` / `refund_amount`）→ 必须是 int（分）；
    - `float` / `str` / `bool` → `ValueError`：在边界炸，不做"元还是分"的静默猜测。
      （一次猜错就是资损级歧义，见 DECISIONS 附 A「金额一律整数分」。）
    """
    if isinstance(value, bool):
        raise ValueError(f"{where}: 金额不允许 bool")
    if isinstance(value, int):
        return value
    raise ValueError(f"{where}: 金额必须是整数分 int，收到 {value!r}")


def _first_int(source: Mapping[str, Any], names: Sequence[str], *, where: str) -> int | None:
    for name in names:
        if name in source and source[name] is not None:
            return _money(source[name], where=f"{where}.{name}")
    return None


def _as_list(payload: Any, names: Sequence[str]) -> list[Mapping[str, Any]]:
    """从上游 `data` 里取出条目列表（`{"<name>": [...]}` 或裸列表）。"""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for name in names:
            value = payload.get(name)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    return []


# ======================================================================================
# action_log 仓储（执行幂等的唯一来源，§6.2；本模块是它的唯一写入方）
# ======================================================================================


async def load_action_log(idempotency_key: str) -> dict[str, Any] | None:
    """按 `idempotency_key` 取执行记录。"""

    def _fn(session: Any) -> dict[str, Any] | None:
        row = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "action_log_id": row.action_log_id,
            "idempotency_key": row.idempotency_key,
            "action_id": row.action_id,
            "run_id": row.run_id,
            "shop_id": row.shop_id,
            "action_type": row.action_type,
            "target": row.target,
            "request_params": dict(row.request_params or {}),
            "response": dict(row.response) if isinstance(row.response, Mapping) else None,
            "status": row.status,
            "attempt": int(row.attempt or 1),
            "upstream_request_id": row.upstream_request_id,
            "error_category": row.error_category,
            "error_message": row.error_message,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "finished_at": row.finished_at,
        }

    return await arun_db(_fn)


async def insert_action_log(
    *,
    idempotency_key: str,
    action_id: str | None,
    run_id: str,
    shop_id: str,
    action_type: str,
    target: str,
    request_params: Mapping[str, Any],
    status: ActionLogStatus | str = ActionLogStatus.PENDING,
    attempt: int = 1,
    now: datetime | None = None,
) -> bool:
    """insert-if-absent（`UNIQUE(idempotency_key)`）。返回 True = 本次插入成功。

    并发撞 `IntegrityError` 时**重查并跳过**（§6.3 第 6 条：不用 `INSERT OR REPLACE` /
    `ON CONFLICT`，否则会把别人写的结果覆盖掉）。
    """
    from sqlalchemy.exc import IntegrityError

    stamp = now or utcnow()

    def _fn(session: Any) -> bool:
        existing = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return False
        session.add(
            ActionLog(
                action_log_id=idempotency_key,
                idempotency_key=idempotency_key,
                action_id=action_id,
                run_id=run_id,
                shop_id=shop_id,
                action_type=action_type,
                target=target,
                request_params=dict(request_params),
                response=None,
                status=str(status),
                attempt=attempt,
                upstream_request_id=None,
                error_category=None,
                error_message=None,
                created_at=stamp,
                updated_at=stamp,
                finished_at=None,
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            again = session.execute(
                select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            return again is None  # 重查有行 = 别人先插了，本次算跳过
        return True

    return bool(await arun_db(_fn))


async def update_action_log(
    idempotency_key: str,
    *,
    status: ActionLogStatus | str | None = None,
    response: Mapping[str, Any] | None = None,
    upstream_request_id: str | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
    attempt: int | None = None,
    now: datetime | None = None,
) -> None:
    """更新执行记录；写入终态时补 `finished_at`。"""
    stamp = now or utcnow()
    terminal = {
        ActionLogStatus.SUCCEEDED.value,
        ActionLogStatus.FAILED.value,
        ActionLogStatus.UNKNOWN.value,
        ActionLogStatus.SKIPPED_DUPLICATE.value,
    }

    def _fn(session: Any) -> None:
        row = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"action_log 缺少 idempotency_key={idempotency_key}")
        if status is not None:
            row.status = str(status)
            if str(status) in terminal:
                row.finished_at = stamp
        if response is not None:
            row.response = dict(response)
        if upstream_request_id is not None:
            row.upstream_request_id = upstream_request_id
        if error_category is not None:
            row.error_category = error_category
        if error_message is not None:
            row.error_message = error_message
        if attempt is not None:
            row.attempt = attempt
        row.updated_at = stamp

    await arun_db(_fn)


# ======================================================================================
# 1) collect
# ======================================================================================


def is_retryable_doudian_error(exc: BaseException) -> bool:
    """节点级重试的判据（§9.2）：只有抖店异常族里 `retryable=True` 的类别才重试。

    这条判据同时是 `RetryPolicy.retry_on`：`GraphBubbleUp`（HITL 挂起）、
    `ApprovalValidationError`（本地校验错）、`business`/`client_4xx`/`auth`/`signature`
    一律**不重试**——重试一个"参数错了"或"token 没有权限"只会把同一个错误重复四遍。
    """
    return isinstance(exc, DoudianError) and bool(exc.retryable)


def _note_call(source: dict[str, Any], method: str, response: Any) -> None:
    source["methods"].append(method)
    request_id = getattr(response, "request_id", None)
    if request_id:
        source["request_ids"].append(str(request_id))


def _normalize_shop(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    shop = dict(payload)
    shop_id = shop.get("shop_id") or shop.get("id")
    shop["shop_id"] = str(shop_id) if shop_id is not None else None
    name = shop.get("shop_name") or shop.get("name")
    shop["shop_name"] = str(name) if name is not None else None
    return shop


def _normalize_product(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """一个商品/规格 → §5.5 `PRODUCT_FIELDS` 形状；金额一律整数分。"""
    sku_id = item.get("sku_id")
    product_id = item.get("product_id") or item.get("id")
    if sku_id is None and product_id is None:
        return None
    where = f"product[{sku_id or product_id}]"
    price_cents = _first_int(item, ("price_cents", "price"), where=where)
    if price_cents is None:
        price_yuan = item.get("price_yuan")
        if price_yuan is not None:
            price_cents = int(round(float(price_yuan) * 100))
    stock = item.get("stock")
    if stock is not None and (
        isinstance(stock, bool) or not isinstance(stock, int) or stock < 0
    ):
        raise ValueError(f"{where}.stock 必须是非负 int，收到 {stock!r}")
    return {
        "product_id": str(product_id) if product_id is not None else None,
        "sku_id": str(sku_id) if sku_id is not None else None,
        "title": str(item.get("title") or item.get("name") or ""),
        "price_cents": price_cents,
        "stock": int(stock) if stock is not None else 0,
        "gmv_cents": _first_int(item, ("gmv_cents", "sales_gmv_cents"), where=where) or 0,
        "refund_order_count": int(item.get("refund_order_count") or 0),
        "is_on_sale": bool(item.get("is_on_sale", item.get("on_sale", True))),
    }


def _is_refunded(item: Mapping[str, Any], *, where: str) -> bool:
    status = str(item.get("refund_status") or "").strip().upper()
    if status and status in {word.upper() for word in _REFUND_STATUSES}:
        return True
    amount = _first_int(item, ("refund_amount_cents", "refund_amount"), where=where)
    return bool(amount and amount > 0)


#: 上游 `summary` 里可能声明"这段汇总覆盖哪一天"的字段（v1.3.2）
_UV_SCOPE_DATE_KEYS: tuple[str, ...] = ("date", "business_date")


def _summary_uv(summary: Mapping[str, Any]) -> int | None:
    """`summary.uv` → 非负 int；缺键 → `None`；有值但不合法 → `ValueError`。

    `uv` 是**可缺**字段（§5.3：缺 → `uv=None` / `cvr=None`），"没有"不是错；
    但"有值却不是非负整数"必须当场炸：静默丢掉会让"上游改了形状"永远表现成
    "这家店没有访客数据"——把一次数据结构变更伪装成正常的无数据，正是 §5.3 要防的那类错。
    """
    value = summary.get("uv")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"order_summary.summary.uv 必须是非负整数，收到 {value!r}")
    return value


def _declared_uv_scope(summary: Mapping[str, Any]) -> str | None:
    """上游用 `date` / `window.end` 明确声明的作用域（业务日期）；没声明 → `None`。"""
    for key in _UV_SCOPE_DATE_KEYS:
        declared = _business_date_of(summary.get(key), fallback="")
        if declared:
            return declared
    window = summary.get("window")
    if isinstance(window, Mapping):
        declared = _business_date_of(window.get("end"), fallback="")
        if declared:
            return declared
    return None


def _day_uv(payload: Any, *, window_end: str, order_days: set[str]) -> int | None:
    """从上游 `summary` 取出**当日**访客数；无法确认作用域就 `None`（不摊派、不补零）。

    为什么需要这层确认（v1.3.2）：`collect` 一次查近 8 个业务日（§5.3 的历史均值需要），
    而上游 `summary` 是**整段查询窗**的汇总。把 8 天的 uv 当成今天的 uv，`cvr` 会被摊薄成
    约 1/8，"访客转化率掉了"这类规则就永远不响——**静默算错指标比缺数据更糟**。
    因此只在能确认是"当天"时才认：

    ① `summary.date` / `summary.window.end` 声明的作用域**等于** `window_end`；
    ② 本次返回的订单**全部落在** `window_end`（单日响应，`summary` 不可能是别的日子）。

    两者都不成立 → `None`：`compute_metrics` 随之给出 `uv=None` / `cvr=None`（§5.3）。
    """
    summary = payload.get("summary") if isinstance(payload, Mapping) else None
    if not isinstance(summary, Mapping):
        return None
    uv = _summary_uv(summary)
    if uv is None:
        return None
    declared = _declared_uv_scope(summary)
    if declared is not None:
        return uv if declared == window_end else None
    if order_days and order_days <= {window_end}:
        return uv
    return None


def _aggregate_orders(payload: Any, *, window_start: str, window_end: str) -> dict[str, Any]:
    """订单列表 → 按业务日聚合（`gmv_cents` / `order_count` / `paid_order_count` /
    `refund_order_count` / `refund_amount_cents` / `uv`），并保留 `by_date` 供历史指标使用。

    "已支付"的判据是**数据**而不是状态词表：`pay_amount > 0` 或存在 `pay_time`。
    依赖状态字符串会让上游改一次枚举就静默把 GMV 算成 0。

    `uv` 不在订单明细里，只可能来自上游 `summary`；由 :func:`_day_uv` 判定它是否可用于
    **当天**（拿不到就 `None`，绝不按"窗口总数 / 天数"摊派，也不补零）。
    """
    orders = _as_list(payload, ("shop_order_list", "orders", "order_list"))
    buckets: dict[str, dict[str, Any]] = {}

    def _bucket(day: str) -> dict[str, Any]:
        return buckets.setdefault(
            day,
            {
                "gmv_cents": 0,
                "order_count": 0,
                "paid_order_count": 0,
                "refund_order_count": 0,
                "refund_amount_cents": 0,
                "uv": None,
            },
        )

    for index, item in enumerate(orders):
        where = f"order[{index}]"
        day = _business_date_of(item.get("create_time") or item.get("pay_time"),
                                fallback=window_end)
        if not (window_start <= day <= window_end):
            # 上游可能多给了窗口外的单（时区/分页边界），聚合时以业务窗口为准。
            continue
        bucket = _bucket(day)
        paid_amount = _first_int(item, ("pay_amount_cents", "pay_amount"), where=where)
        paid = bool(paid_amount and paid_amount > 0) or item.get("pay_time") is not None
        bucket["order_count"] += 1
        if paid:
            bucket["paid_order_count"] += 1
            bucket["gmv_cents"] += int(paid_amount or 0)
            if _is_refunded(item, where=where):
                bucket["refund_order_count"] += 1
                bucket["refund_amount_cents"] += int(
                    _first_int(item, ("refund_amount_cents", "refund_amount"), where=where) or 0
                )

    order_days = set(buckets)  # 先记下"有订单的那些天"，再补 end 桶（否则 scope 判定会被自己污染）
    today = buckets.get(window_end) or _bucket(window_end)
    day_uv = _day_uv(payload, window_end=window_end, order_days=order_days)
    if day_uv is not None:
        today["uv"] = day_uv
    return {"by_date": {day: buckets[day] for day in sorted(buckets)}, **today}


@traced("collect")
async def collect(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """采集：店铺信息 + 商品列表 + 近 8 个业务日的订单聚合（§3.3 `RawSnapshot`）。

    失败分级（§4.2 的 `collect --(fail)--> finalize`）：

    - **可重试**错误（timeout/connection/5xx/限流）→ 原样抛出，交给节点级 `RetryPolicy`
      按 §9.2 的预算重试；重试用尽才由 `run_agent` 兜住。
    - **不可重试**错误（4xx / business / signature / auth）→ 记 `errors`；其中订单（指标的唯一
      来源）缺失即判定 `failed` 走 `finalize`，其余降级为 `partial=True` 继续。
    """
    runtime = resolve_runtime(config)
    client = resolve_client(config, runtime)
    now = resolve_now(config)
    today = business_date(now)
    window_start = _shift_business_date(today, -HISTORY_WINDOW_DAYS)
    source: dict[str, Any] = {"methods": [], "request_ids": [], "partial": False}
    errors: list[AgentError] = []

    def _record(exc: DoudianError, *, what: str) -> None:
        errors.append(
            make_agent_error(
                node="collect",
                category=exc.category,
                message=f"{what} 采集失败：{exc}",
                retryable=bool(exc.retryable),
            )
        )

    # --- 店铺信息（非必需：缺了只是少了店铺画像） ---
    shop: dict[str, Any] = {}
    try:
        response = await client.get_shop_info()
    except DoudianError as exc:
        if exc.retryable:
            raise
        _record(exc, what="店铺信息")
        source["partial"] = True
    else:
        _note_call(source, "shop.getShopInfo", response)
        shop = _normalize_shop(getattr(response, "data", None))

    # --- 商品列表（非必需：缺了只是这次不提议动作） ---
    products: list[dict[str, Any]] = []
    try:
        response = await client.list_products(page=0, size=100)
    except DoudianError as exc:
        if exc.retryable:
            raise
        _record(exc, what="商品列表")
        source["partial"] = True
    else:
        _note_call(source, "product.listV2", response)
        for item in _as_list(getattr(response, "data", None), ("products", "items", "list")):
            normalized = _normalize_product(item)
            if normalized is not None:
                products.append(normalized)

    # --- 订单（必需：指标的唯一来源） ---
    order_summary: dict[str, Any] | None = None
    try:
        response = await client.search_orders(
            start_time=f"{window_start} 00:00:00", end_time=f"{today} 23:59:59", page=0, size=100
        )
    except DoudianError as exc:
        if exc.retryable:
            raise
        _record(exc, what="订单列表")
        source["partial"] = True
    else:
        _note_call(source, "order.searchList", response)
        aggregate = _aggregate_orders(
            getattr(response, "data", None), window_start=window_start, window_end=today
        )
        by_date = aggregate.pop("by_date")
        order_summary = {**aggregate, "by_date": by_date}

    if order_summary is None:
        errors.append(
            make_agent_error(
                node="collect",
                category=ErrorCategory.UNKNOWN,
                message="订单数据缺失，无法计算指标（这是指标的唯一来源）",
                retryable=False,
            )
        )
        return {
            "errors": _errors(state, errors),
            "audit": _audit(
                state,
                node="collect",
                event="collect.failed",
                data={"window": {"start": window_start, "end": today}, "source": source},
            ),
            "status": RunStatus.FAILED.value,
        }

    raw = RawSnapshot(
        collected_at=utc_iso(now),
        window={"start": window_start, "end": today},
        shop=shop,
        products=products,
        order_summary=order_summary,
        source=source,
    )
    return {
        "raw": raw,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="collect",
            event="collect.done",
            data={
                "window": raw["window"],
                "products": len(products),
                "methods": list(source["methods"]),
                "partial": bool(source["partial"]),
            },
            at=raw["collected_at"],
        ),
    }


def route_after_collect(state: AgentState) -> str:
    """§4.2 唯一的"入口"条件边：采集硬失败就直奔 `finalize`。"""
    if _aborted(state) or not state.get("raw"):
        return "finalize"
    return "compute_metrics"


# ======================================================================================
# 2) compute_metrics
# ======================================================================================


@traced("compute_metrics")
async def compute_metrics_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """算出当日指标 + 近 7 日历史（含今天），供均值/环比使用（§5.3）。"""
    from app.services.metrics import compute_metrics

    if _aborted(state):
        return {}
    raw = state.get("raw")
    if not isinstance(raw, Mapping):
        return {
            "errors": _errors(
                state,
                [
                    make_agent_error(
                        node="compute_metrics",
                        category=ErrorCategory.CONFIG,
                        message="raw 缺失，无法计算指标",
                        retryable=False,
                    )
                ],
            ),
            "status": RunStatus.FAILED.value,
        }
    now = resolve_now(config)
    by_date = dict((raw.get("order_summary") or {}).get("by_date") or {})
    today = str(raw["window"]["end"])

    history: list[MetricsSnapshot] = []
    for day in sorted(by_date):
        day_raw = RawSnapshot(
            collected_at=str(raw.get("collected_at") or ""),
            window={"start": day, "end": day},
            shop={},
            products=[],
            order_summary=dict(by_date[day]),
            source={"methods": [], "request_ids": [], "partial": False},
        )
        # 逐日累积：每一条历史都能带着"它自己那一刻"的 7 日均值，而不是全 None。
        history.append(compute_metrics(day_raw, history=list(history), now=now))

    if history and history[-1]["date"] == today:
        metrics = history[-1]
    else:
        metrics = compute_metrics(raw, history=list(history), now=now)
        history.append(metrics)

    return {
        "metrics": metrics,
        "history": history[-9:],
        "audit": _audit(
            state,
            node="compute_metrics",
            event="metrics.computed",
            data={
                "date": metrics["date"],
                "gmv_cents": metrics["gmv_cents"],
                "history_days": len(history),
                "gmv_7d_avg_cents": metrics["gmv_7d_avg_cents"],
            },
            at=str(metrics["computed_at"]),
        ),
    }


# ======================================================================================
# 3) detect_anomalies
# ======================================================================================


@traced("detect_anomalies")
async def detect_anomalies_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """按 `rules.yaml` 逐条求值；跳过与截断都必须可见（§5.4）。"""
    from app.services.anomalies import evaluate_rules

    if _aborted(state):
        return {}
    metrics = state.get("metrics")
    if not isinstance(metrics, Mapping):
        return {
            "errors": _errors(
                state,
                [
                    make_agent_error(
                        node="detect_anomalies",
                        category=ErrorCategory.CONFIG,
                        message="metrics 缺失，无法检测异常",
                        retryable=False,
                    )
                ],
            ),
            "status": RunStatus.FAILED.value,
        }
    runtime = resolve_runtime(config)
    now = resolve_now(config)
    evaluation = evaluate_rules(
        metrics=metrics,  # type: ignore[arg-type]
        history=state.get("history") or [],
        rules=runtime.rules,
        run_id=str(state["run_id"]),
        now=now,
        max_anomalies=int(runtime.settings.agent.max_anomalies),
    )
    errors = [
        make_agent_error(
            node="detect_anomalies",
            category=ErrorCategory.CONFIG,
            message=f"规则 {rule_id} 求值失败已跳过：{reason}",
            retryable=False,
        )
        for rule_id, reason in evaluation.skipped
    ]
    if evaluation.dropped:
        errors.append(
            make_agent_error(
                node="detect_anomalies",
                category=ErrorCategory.UNKNOWN,
                message=f"异常条数超过 max_anomalies={runtime.settings.agent.max_anomalies}，"
                f"丢弃 {evaluation.dropped} 条",
                retryable=False,
            )
        )
    anomalies: list[Anomaly] = evaluation.anomalies
    return {
        "anomalies": anomalies,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="detect_anomalies",
            event="anomaly.detected",
            data={
                "count": len(anomalies),
                "rule_ids": [item["rule_id"] for item in anomalies],
                "skipped": [rule_id for rule_id, _ in evaluation.skipped],
                "dropped": evaluation.dropped,
            },
        ),
    }


# ======================================================================================
# 4) propose_actions
# ======================================================================================


@traced("propose_actions")
async def propose_actions_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """异常 → 动作提议（白名单类型 + 参数已归一化 + `risk` 已判定，§5.5）。"""
    from app.services.actions import propose_actions_with_report

    if _aborted(state):
        return {}
    anomalies = list(state.get("anomalies") or [])
    if not anomalies:
        # 无异常 = 无提议：直接交给 finalize（不进审批，见 §4.2 的"无提议时直通"）。
        return {
            "pending_actions": [],
            "audit": _audit(
                state, node="propose_actions", event="action.proposed", data={"count": 0}
            ),
        }
    metrics = state.get("metrics")
    raw = state.get("raw")
    if not isinstance(metrics, Mapping) or not isinstance(raw, Mapping):
        return {
            "errors": _errors(
                state,
                [
                    make_agent_error(
                        node="propose_actions",
                        category=ErrorCategory.CONFIG,
                        message="metrics/raw 缺失，无法提议动作",
                        retryable=False,
                    )
                ],
            ),
            "status": RunStatus.FAILED.value,
        }
    runtime = resolve_runtime(config)
    report = propose_actions_with_report(
        anomalies=anomalies,
        metrics=metrics,  # type: ignore[arg-type]
        products=raw.get("products") or [],
        rules=runtime.rules,
        run_id=str(state["run_id"]),
        now=resolve_now(config),
    )
    errors = [
        make_agent_error(
            node="propose_actions",
            category=ErrorCategory.CONFIG,
            message=f"动作模板未展开：{reason}",
            retryable=False,
        )
        for reason in report.dropped
    ]
    proposals: list[ActionProposal] = report.proposals
    return {
        "pending_actions": proposals,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="propose_actions",
            event="action.proposed",
            data={
                "count": len(proposals),
                "action_ids": [item["action_id"] for item in proposals],
                "risks": [str(item["risk"]) for item in proposals],
                "dropped": len(report.dropped),
            },
        ),
    }


def route_after_propose_actions(state: AgentState) -> str:
    """无提议（无异常 / 规则没配动作）→ 不进入审批，直接 `finalize`。

    §4.2 的拓扑图在 `propose_actions` 与 `finalize` 之间画了这条"直通"边，`:514` 的
    "(等待审批 / 无提议时直通)" 就是它；本函数是它的实现。
    """
    if _aborted(state) or not state.get("pending_actions"):
        return "finalize"
    return "approval_gate"


# ======================================================================================
# 5) approval_gate（interrupt 发生在这里，§4.4 逐字）
# ======================================================================================


def _validate_decisions_shape(payload: Any) -> list[Decision]:
    """恢复值的外层形状校验（类型/枚举/edited_params 规则），§4.4 规则 1/3/5。"""
    if not isinstance(payload, Mapping):
        raise ApprovalValidationError(
            "恢复值必须是映射", details={"received_type": type(payload).__name__}
        )
    if payload.get("type") != "approval_decisions":
        raise ApprovalValidationError(
            '恢复值 type 必须是 "approval_decisions"', details={"type": payload.get("type")}
        )
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list):
        raise ApprovalValidationError(
            "恢复值 decisions 必须是列表", details={"type": type(raw_decisions).__name__}
        )
    decisions: list[Decision] = []
    for index, item in enumerate(raw_decisions):
        where = f"decisions[{index}]"
        if not isinstance(item, Mapping):
            raise ApprovalValidationError(f"{where} 必须是映射")
        action_id = item.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise ApprovalValidationError(f"{where}.action_id 必须是非空字符串")
        verdict_raw = item.get("verdict")
        try:
            verdict = Verdict(str(verdict_raw))
        except ValueError as exc:
            raise ApprovalValidationError(
                f"{where}.verdict 非法：{verdict_raw!r}（允许 approve/reject/edit）"
            ) from exc
        edited = item.get("edited_params", None)
        if verdict is Verdict.EDIT:
            if not isinstance(edited, dict) or not edited:
                raise ApprovalValidationError(f"{where}.edited_params 在 verdict=edit 时必须是非空映射")
        elif edited is not None:
            raise ApprovalValidationError(
                f"{where}.edited_params 在 verdict={verdict.value} 时必须为 null（不做宽容处理）"
            )
        operator = item.get("operator")
        if operator is not None and not isinstance(operator, str):
            raise ApprovalValidationError(f"{where}.operator 必须是字符串或 null")
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ApprovalValidationError(f"{where}.reason 必须是字符串或 null")
        decided_at = item.get("decided_at")
        if decided_at is not None and not isinstance(decided_at, str):
            raise ApprovalValidationError(f"{where}.decided_at 必须是 ISO-8601 字符串或 null")
        decisions.append(
            Decision(
                action_id=action_id,
                verdict=verdict,
                edited_params=dict(edited) if edited is not None else None,
                operator=operator,
                reason=reason,
                decided_at=decided_at or utc_iso(),
            )
        )
    return decisions


def validate_resume_payload(payload: Any, *, expected: Sequence[ActionProposal]) -> dict[str, Any]:
    """恢复值严格校验（§4.4 校验规则 1/2/3/4/5）。任一不成立 → `ApprovalValidationError`(400)。

    规则 2 是"zip 静默截断"的根治点（ADR-005）：`set(decisions.action_id)` 必须与
    `set(pending_actions.action_id)` **完全相等** —— 不许多、不许少、不许重复；
    不做任何按位置配对。
    """
    decisions = _validate_decisions_shape(payload)
    expected_ids = [str(item["action_id"]) for item in expected]
    got_ids = [decision["action_id"] for decision in decisions]
    duplicates = sorted({item for item in got_ids if got_ids.count(item) > 1})
    if duplicates:
        raise ApprovalValidationError(
            "decisions 里出现重复 action_id", details={"duplicates": duplicates}
        )
    missing = [item for item in expected_ids if item not in set(got_ids)]
    extra = [item for item in got_ids if item not in set(expected_ids)]
    if missing or extra:
        raise ApprovalValidationError(
            "decisions 与待审批动作集合不一致（既不许多也不许少）",
            details={"missing": missing, "extra": extra, "expected": len(expected_ids)},
        )
    # 规则 4：verdict=edit 的参数必须能过该 action_type 的 schema（在进图之前就拦）。
    from app.services.actions import validate_params

    by_id = {str(item["action_id"]): item for item in expected}
    for decision in decisions:
        if decision["verdict"] is not Verdict.EDIT:
            continue
        proposal = by_id[decision["action_id"]]
        try:
            validate_params(str(proposal["action_type"]), decision["edited_params"] or {})
        except ParamValidationError as exc:
            raise ApprovalValidationError(
                f"{decision['action_id']} 的 edited_params 不符合参数 schema：{exc}",
                details={"action_id": decision["action_id"]},
            ) from exc
    return {"type": "approval_decisions", "decisions": decisions}


#: 契约 §4.4 代码块里的私有名字（保持逐字可对照）
_validate_resume_payload = validate_resume_payload


@traced("approval_gate")
async def approval_gate(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """人工审批闸门：落库 → 通知 → `interrupt()` → 校验恢复值 → 落裁决（§4.4）。"""
    from app.services import approvals

    if _aborted(state):
        return {}
    pending: list[ActionProposal] = list(state.get("pending_actions") or [])
    if not pending:
        # 防御性直通：拓扑上不会走到这里（route_after_propose_actions 已经分流），
        # 但"节点自己不制造等待"是硬约束，不能靠调用方保证。
        return {}

    runtime = resolve_runtime(config)
    now = resolve_now(config)
    already = state.get("approval") or {}
    requested_at = already.get("requested_at") if isinstance(already, Mapping) else None
    payload = {
        "type": "approval_request",
        "run_id": state["run_id"],
        "thread_id": state["thread_id"],
        "shop_id": state["shop_id"],
        "trace_id": state["trace_id"],
        "requested_at": requested_at or utc_iso(now),
        "proposals": pending,
    }

    # 1) 幂等落库（insert-if-absent；interrupt_id 先写 ""，由 resume_agent 回填）
    with session_scope() as session:
        await approvals.upsert_proposals(
            session=session,
            run_id=str(state["run_id"]),
            shop_id=str(state["shop_id"]),
            thread_id=str(state["thread_id"]),
            proposals=pending,
        )

    # 2) 通知（失败不阻断：审批台是主通道，§5.6）
    notification_error: str | None = None
    try:
        await runtime.notifier.send_approval_request(payload)
    except GraphBubbleUp:
        # 控制流类异常不得被"通知失败"的兜底吞掉（§4.3 规则 2：节点体内 except Exception
        # 必须显式排除 GraphInterrupt/GraphBubbleUp，唯一观察点是 traced()）。
        raise
    except Exception as exc:  # noqa: BLE001 - 通知是旁路，任何失败都不得阻断审批
        notification_error = f"{type(exc).__name__}: {exc}"

    # 3) 状态先落 awaiting_approval：interrupt 会抛异常，节点返回值会被丢弃，
    #    所以"挂起中的状态"只能在 interrupt **之前**直接写库（§4.2 状态迁移表）。
    await update_run_trace(
        shop_id=str(state["shop_id"]),
        run_id=str(state["run_id"]),
        status=RunStatus.AWAITING_APPROVAL.value,
        now=now,
    )

    # 4) 挂起（同一个 state 上唯一一次 interrupt）
    from langgraph.types import interrupt

    answer = interrupt(payload)

    # 5) 恢复后重放：校验 + 落裁决
    validated = validate_resume_payload(answer, expected=pending)
    decisions: list[Decision] = list(validated["decisions"])
    with session_scope() as session:
        await approvals.mark_decided(
            session=session, thread_id=str(state["thread_id"]), decisions=decisions
        )

    from app.services.actions import idempotency_key

    by_id = {str(item["action_id"]): item for item in pending}
    approved: list[ActionProposal] = []
    rejected: list[ActionProposal] = []
    for decision in decisions:
        proposal = by_id[decision["action_id"]]
        if decision["verdict"] is Verdict.REJECT:
            rejected.append(proposal)
            continue
        params = (
            dict(decision["edited_params"] or {})
            if decision["verdict"] is Verdict.EDIT
            else dict(proposal["params"])
        )
        effective = ActionProposal(
            action_id=proposal["action_id"],
            action_type=proposal["action_type"],
            target=proposal["target"],
            params=params,
            # §4.5：键落在"参数已经定稿"之后（改价 9900→9600 必须是两个不同的键）。
            idempotency_key=idempotency_key(
                shop_id=str(state["shop_id"]),
                action_type=str(proposal["action_type"]),
                target=str(proposal["target"]),
                params=params,
            ),
            reason=proposal["reason"],
            evidence=proposal["evidence"],
            risk=proposal["risk"],
            created_at=proposal["created_at"],
        )
        approved.append(effective)

    approval_state = {
        "interrupt_id": None,  # 由 resume_agent 回填（节点拿不到自己的 interrupt id）
        "requested_at": payload["requested_at"],
        "decided_at": utc_iso(now),
        "decided_by": decisions[0]["operator"] if decisions else None,
        "decisions": decisions,
    }
    errors: list[AgentError] = []
    if notification_error:
        errors.append(
            make_agent_error(
                node="approval_gate",
                category=ErrorCategory.UNKNOWN,
                message=f"审批通知未送达（不阻断）：{notification_error}",
                retryable=False,
            )
        )
    return {
        "approved_actions": approved,
        "rejected_actions": rejected,
        "approval": approval_state,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="approval_gate",
            event="approval.decided",
            data={
                "approved": len(approved),
                "rejected": len(rejected),
                "verdicts": [str(item["verdict"]) for item in decisions],
            },
        ),
        "status": RunStatus.EXECUTING.value,
    }


# ======================================================================================
# 6) execute
# ======================================================================================


def _dispatch(client: Any, proposal: Mapping[str, Any]) -> Awaitable[Any]:
    """派发到抖店的具体方法（只白名单三类动作）。"""
    action_type = ActionType(str(proposal["action_type"]))
    params = dict(proposal["params"])
    product_id, sku_id = _target_ids(proposal)
    if action_type is ActionType.PRICE_UPDATE:
        return client.edit_price(
            product_id=product_id, sku_id=sku_id, price_cents=int(params["price_cents"])
        )
    if action_type is ActionType.STOCK_UPDATE:
        return client.update_stock(
            product_id=product_id, sku_id=sku_id, stock=int(params["stock"])
        )
    if action_type is ActionType.COUPON_CREATE:
        raise DoudianBusinessError(
            "coupon_create 没有对应的抖店上游方法（DoudianClient 只封装了 editPrice/editStock，"
            "见 CONTRACT §5.1），本次不执行",
            category=ErrorCategory.CONFIG,
            retryable=False,
        )
    raise DoudianBusinessError(
        f"未知动作类型 {action_type!r}", category=ErrorCategory.CONFIG, retryable=False
    )


def _target_ids(proposal: Mapping[str, Any]) -> tuple[str, str]:
    """`proposal` → (product_id, sku_id)。

    上游 `editPrice`/`editStock` 要 `product_id` + `sku_id` 两个；而 §3.3 的 `target` 只有一个。
    两者都来自 `propose_actions` 记录的 `evidence.target_snapshot`（商品列表里的真实条目），
    缺 product_id 时才退回"target 兼任"。
    """
    snapshot = (proposal.get("evidence") or {}).get("target_snapshot") or {}
    target = str(proposal["target"])
    product_id = snapshot.get("product_id")
    sku_id = snapshot.get("sku_id")
    return (
        str(product_id) if product_id is not None else target,
        str(sku_id) if sku_id is not None else target,
    )


@traced("execute")
async def execute(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """执行审批通过的动作（幂等由 `action_log.idempotency_key` 保证，§6.2）。"""
    if _aborted(state):
        return {}
    approved: list[ActionProposal] = list(state.get("approved_actions") or [])
    if not approved:
        return {
            "execution_results": [],
            "audit": _audit(
                state, node="execute", event="action.executed", data={"count": 0}
            ),
            "status": RunStatus.EXECUTING.value,
        }

    from app.services.actions import idempotency_key

    runtime = resolve_runtime(config)
    client = resolve_client(config, runtime)
    now = resolve_now(config)
    results: list[ExecutionResult] = []
    errors: list[AgentError] = []
    shop_id = str(state["shop_id"])

    for proposal in approved:
        params = dict(proposal["params"])
        key = idempotency_key(
            shop_id=shop_id,
            action_type=str(proposal["action_type"]),
            target=str(proposal["target"]),
            params=params,
        )
        previous = await load_action_log(key)
        if previous is not None and previous["status"] in _NO_REEXECUTE_STATUSES:
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.SKIPPED_DUPLICATE.value,
                    upstream_request_id=previous.get("upstream_request_id"),
                    error=None,
                    error_category=None,
                    attempt=int(previous.get("attempt") or 1),
                    executed_at=utc_iso(now),
                )
            )
            continue
        if previous is not None and previous["status"] in {
            ActionLogStatus.PENDING.value,
            ActionLogStatus.UNKNOWN.value,
        }:
            # 已经下发过但没拿到确定结果（崩溃窗口 / ambiguous）：**不能重发**（ADR-011），
            # 交给对账任务（§9.3）；把 pending 也并进 unknown，让"说不清"显式可见。
            if previous["status"] == ActionLogStatus.PENDING.value:
                await update_action_log(key, status=ActionLogStatus.UNKNOWN, now=now)
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.UNKNOWN.value,
                    upstream_request_id=previous.get("upstream_request_id"),
                    error=previous.get("error_message")
                    or "上次执行结果不确定（pending/unknown），不自动重发，等待对账",
                    error_category=ErrorCategory.AMBIGUOUS.value,
                    attempt=int(previous.get("attempt") or 1),
                    executed_at=utc_iso(now),
                )
            )
            continue

        attempt = 1 if previous is None else int(previous.get("attempt") or 1) + 1
        if previous is None:
            await insert_action_log(
                idempotency_key=key,
                action_id=str(proposal["action_id"]),
                run_id=str(state["run_id"]),
                shop_id=shop_id,
                action_type=str(proposal["action_type"]),
                target=str(proposal["target"]),
                request_params=params,
                status=ActionLogStatus.PENDING,
                attempt=attempt,
                now=now,
            )
        try:
            response = await _dispatch(client, proposal)
        except DoudianAmbiguousOutcomeError as exc:
            await update_action_log(
                key,
                status=ActionLogStatus.UNKNOWN,
                error_category=str(exc.category),
                error_message=str(exc),
                attempt=attempt,
                now=now,
            )
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.UNKNOWN.value,
                    upstream_request_id=None,
                    error=str(exc),
                    error_category=str(exc.category),
                    attempt=attempt,
                    executed_at=utc_iso(now),
                )
            )
        except DoudianAuthError as exc:
            # §9.1：鉴权失败（已强制刷过 token）→ 停止整个 run。先落库再抛，证据不丢。
            await update_action_log(
                key,
                status=ActionLogStatus.FAILED,
                error_category=str(exc.category),
                error_message=str(exc),
                attempt=attempt,
                now=now,
            )
            raise
        except DoudianError as exc:
            await update_action_log(
                key,
                status=ActionLogStatus.FAILED,
                error_category=str(exc.category),
                error_message=str(exc),
                attempt=attempt,
                now=now,
            )
            errors.append(
                make_agent_error(
                    node="execute",
                    category=exc.category,
                    message=f"动作 {proposal['action_id']} 执行失败：{exc}",
                    retryable=bool(exc.retryable),
                )
            )
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.FAILED.value,
                    upstream_request_id=None,
                    error=str(exc),
                    error_category=str(exc.category),
                    attempt=attempt,
                    executed_at=utc_iso(now),
                )
            )
            if exc.retryable:
                # 交给节点级 RetryPolicy（§9.2 的预算）：重放时已成功的动作会变成
                # skipped_duplicate（幂等键挡住），不会重复改价。
                raise
        else:
            request_id = getattr(response, "request_id", None)
            await update_action_log(
                key,
                # 只留可审计的最小摘要：上游 data 可能很大且不可 JSON 序列化，整块塞库会让
                # 一次执行写进几百 KB 并且丢掉"能查"的价值。
                status=ActionLogStatus.SUCCEEDED,
                response={
                    "ok": bool(getattr(response, "ok", True)),
                    "request_id": str(request_id) if request_id else None,
                    "data_digest": digest_of(getattr(response, "data", None)),
                },
                upstream_request_id=str(request_id) if request_id else None,
                attempt=attempt,
                now=now,
            )
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.SUCCEEDED.value,
                    upstream_request_id=str(request_id) if request_id else None,
                    error=None,
                    error_category=None,
                    attempt=attempt,
                    executed_at=utc_iso(now),
                )
            )

    return {
        "execution_results": results,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="execute",
            event="action.executed",
            data={
                "count": len(results),
                "statuses": [item["status"] for item in results],
                "keys": [item["idempotency_key"] for item in results],
            },
        ),
        "status": RunStatus.EXECUTING.value,
    }


# ======================================================================================
# 7) record_effect（图内**不等待** T+N，ADR-003）
# ======================================================================================


@traced("record_effect")
async def record_effect(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """只登记 `effect_snapshot`（`status="scheduled"`）然后立刻返回；采样交给调度器。"""
    from app.services.effects import build_effect_report, register_effect_snapshots

    if _aborted(state):
        return {}
    results = list(state.get("execution_results") or [])
    if not results:
        return {
            "effect_report": None,
            "audit": _audit(
                state, node="record_effect", event="effect.registered", data={"windows": 0}
            ),
        }
    runtime = resolve_runtime(config)
    now = resolve_now(config)
    windows_days = [int(item) for item in runtime.settings.effects.windows_days]
    with session_scope() as session:
        windows = await register_effect_snapshots(
            session=session,
            run_id=str(state["run_id"]),
            shop_id=str(state["shop_id"]),
            results=results,
            windows_days=windows_days,
            now=now,
        )
    report = build_effect_report(
        run_id=str(state["run_id"]),
        windows=windows,
        attribution=Attribution.SINGLE_ARM_BEFORE_AFTER,
        now=now,
    )
    return {
        "effect_report": report,
        "audit": _audit(
            state,
            node="record_effect",
            event="effect.registered",
            data={
                "windows": len(windows),
                "offsets": [window["offset_days"] for window in windows],
                "statuses": sorted({window["status"] for window in windows}),
            },
            at=str(report["generated_at"]),
        ),
    }


# ======================================================================================
# 8) finalize
# ======================================================================================


@traced("finalize")
async def finalize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """收口：定终态 + 用 JSONL 校准步数（§4.7 不变量）+ 落 `finished_at`。"""
    from app.agent.adapters import require_log_sink

    now = resolve_now(config)
    status = RunStatus.FAILED.value if _aborted(state) else RunStatus.DONE.value
    errors = list(state.get("errors") or [])
    sink = require_log_sink()
    with session_scope() as session:
        calibrated = await calibrate_run_steps(
            session=session, run_id=str(state["run_id"]), sink=sink
        )
    await update_run_trace(
        shop_id=str(state["shop_id"]),
        run_id=str(state["run_id"]),
        status=status,
        finished=True,
        error=errors[-1]["message"] if errors and status == RunStatus.FAILED.value else None,
        now=now,
    )
    return {
        "status": status,
        "audit": _audit(
            state,
            node="finalize",
            event="run.finished",
            data={
                "status": status,
                "steps": calibrated,
                "errors": len(errors),
                "executed": len(state.get("execution_results") or []),
            },
            at=utc_iso(now),
        ),
    }
