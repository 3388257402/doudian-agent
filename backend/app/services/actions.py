"""动作提议与参数 schema —— 对应 docs/CONTRACT.md §5.5。

两件事在这里定死：

1. **参数 schema**（`ACTION_PARAM_SCHEMA`）：键集合必须**恰好相等**，多一个少一个都错。
   审批台上 `verdict=edit` 提交的参数也走这里（§4.4 校验规则 4），所以这是"人改的参数"
   与"机器生成的参数"共用的唯一闸门。
2. **风险分级**（`risk`）：`auto_approve_risk` 依赖它决定"能不能跳过 interrupt"。判错的后果
   是"高危动作被自动执行"，因此规则写死、不给配置项：改价跌幅 >15% = high，>5% = medium，
   清库存恒 low（它不动钱），发券恒 high（它是真金白银的预算）。

`products` 的元素结构由 `collect` 节点归一化后产生（见 :data:`PRODUCT_FIELDS`）。契约没有把
这个 dict 的键冻结成字面量，所以键名在这里定义一次，`collect` 与 `propose_actions` 都引用它，
避免两处各写一套。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from app.agent.state import ActionProposal, Anomaly, MetricsSnapshot, canonical_json, utc_iso
from app.enums import ActionType, Risk
from app.errors import ParamValidationError
from app.services.rules import Rule, RuleSet

#: 归一化后的商品/规格条目字段（`collect` 产出，`propose_actions` 消费）
PRODUCT_FIELDS: tuple[str, ...] = (
    "product_id",
    "sku_id",
    "title",
    "price_cents",
    "stock",
    "gmv_cents",
    "refund_order_count",
    "is_on_sale",
)

ACTION_PARAM_SCHEMA: dict[ActionType, dict[str, type]] = {
    ActionType.PRICE_UPDATE: {"price_cents": int},
    ActionType.STOCK_UPDATE: {"stock": int},
    ActionType.COUPON_CREATE: {
        "discount_cents": int,
        "total_count": int,
        "begin_time": str,
        "end_time": str,
    },
}

#: 各动作类型的额外数值约束（§5.5）
_POSITIVE_FIELDS = {
    ActionType.PRICE_UPDATE: ("price_cents",),
    ActionType.COUPON_CREATE: ("discount_cents", "total_count"),
}
_NON_NEGATIVE_FIELDS = {ActionType.STOCK_UPDATE: ("stock",)}
_TIME_FIELDS = (ActionType.COUPON_CREATE, ("begin_time", "end_time"))

_TARGET_SOURCES = ("top_gmv_sku", "top_refund_sku", "lowest_stock_sku")


def _action_type(value: ActionType | str) -> ActionType:
    try:
        return ActionType(str(value))
    except ValueError as exc:
        raise ParamValidationError(
            f"未知动作类型：{value!r}（允许 {[t.value for t in ActionType]}）"
        ) from exc


def normalize_params(action_type: ActionType | str, params: Mapping[str, Any]) -> dict[str, Any]:
    """归一化：剔除 `None` 值、金额字段转 int 分。

    只做**可逆**转换（`9900.0`/`"9900"` → `9900`），不做单位换算（元→分是上游适配层的事，
    在这里换会让"9900 元"和"9900 分"变成同一个值——那是资损级歧义）。
    """
    resolved = _action_type(action_type)
    schema = ACTION_PARAM_SCHEMA[resolved]
    normalized: dict[str, Any] = {}
    for key, raw in params.items():
        if raw is None:
            continue
        expected = schema.get(key)
        if expected is int:
            if isinstance(raw, bool):
                raise ParamValidationError(f"参数 {key} 不允许 bool")
            if isinstance(raw, int):
                normalized[key] = raw
            elif isinstance(raw, float) and raw.is_integer():
                normalized[key] = int(raw)
            elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
                normalized[key] = int(raw.strip())
            else:
                raise ParamValidationError(
                    f"参数 {key} 无法归一化为整数分：{raw!r}"
                )
        else:
            normalized[key] = raw
    return normalized


def validate_params(action_type: ActionType | str, params: Mapping[str, Any]) -> dict[str, Any]:
    """校验并返回归一化后的参数。失败抛 `ParamValidationError`（→ 400）。"""
    resolved = _action_type(action_type)
    schema = ACTION_PARAM_SCHEMA[resolved]
    if not isinstance(params, Mapping):
        raise ParamValidationError(f"{resolved} 的参数必须是映射，收到 {type(params).__name__}")
    keys = set(params)
    expected = set(schema)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise ParamValidationError(
            f"{resolved} 参数键集合不匹配：缺少 {missing}，多余 {extra}（要求恰好 {sorted(expected)}）"
        )
    normalized = normalize_params(resolved, params)
    for key, expected_type in schema.items():
        value = normalized[key]
        if expected_type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ParamValidationError(
                    f"{resolved}.{key} 必须是 int（bool 不算 int），收到 {type(value).__name__}"
                )
        elif not isinstance(value, expected_type):
            raise ParamValidationError(
                f"{resolved}.{key} 必须是 {expected_type.__name__}，收到 {type(value).__name__}"
            )
    for key in _POSITIVE_FIELDS.get(resolved, ()):  # > 0
        if normalized[key] <= 0:
            raise ParamValidationError(f"{resolved}.{key} 必须 > 0，收到 {normalized[key]}")
    for key in _NON_NEGATIVE_FIELDS.get(resolved, ()):  # >= 0
        if normalized[key] < 0:
            raise ParamValidationError(f"{resolved}.{key} 必须 >= 0，收到 {normalized[key]}")
    time_action, time_fields = _TIME_FIELDS
    if resolved == time_action:
        for key in time_fields:
            try:
                datetime.fromisoformat(str(normalized[key]))
            except ValueError as exc:
                raise ParamValidationError(
                    f"{resolved}.{key} 不是合法的 ISO-8601 时间：{normalized[key]!r}"
                ) from exc
    return normalized


def payload_digest(
    *, shop_id: str, action_type: ActionType | str, target: str, params: Mapping[str, Any]
) -> str:
    """§4.5 的 `payload_digest`（审批行的参数指纹，参数被改过就变）。"""
    return hashlib.sha256(
        canonical_json(
            {
                "shop_id": shop_id,
                "action_type": str(action_type),
                "target": target,
                "params": dict(params),
            }
        ).encode("utf-8")
    ).hexdigest()


def idempotency_key(
    *, shop_id: str, action_type: ActionType | str, target: str, params: Mapping[str, Any]
) -> str:
    """§4.5 的 `idempotency_key`（执行身份）。

    与 :func:`payload_digest` 的区别是刻意的：`idempotency_key` 用 `"|"` 拼接受契约字面量
    约束（`sha256(f"{shop_id}|{action_type}|{target}|{canonical_json(params)}")`），
    `payload_digest` 用 canonical JSON 包裹。两者都**不含 run_id** —— 同一店铺同一参数的
    动作本来就不该执行两次（ADR-006）。
    """
    return hashlib.sha256(
        f"{shop_id}|{action_type}|{target}|{canonical_json(dict(params))}".encode("utf-8")
    ).hexdigest()


def action_id_for(*, run_id: str, action_type: ActionType | str, target: str) -> str:
    """§4.5：`f"{run_id}:{action_type}:{target}"`（提议身份，审批前生成）。"""
    return f"{run_id}:{action_type}:{target}"


# --------------------------------------------------------------------------------------
# 目标解析与模板参数展开
# --------------------------------------------------------------------------------------


def _resolve_target(target_from: str, products: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not products:
        return None
    if target_from.startswith("sku:"):
        wanted = target_from[len("sku:") :]
        for product in products:
            if str(product.get("sku_id")) == wanted or str(product.get("product_id")) == wanted:
                return product
        return None
    if target_from == "top_gmv_sku":
        return max(products, key=lambda item: int(item.get("gmv_cents") or 0))
    if target_from == "top_refund_sku":
        return max(products, key=lambda item: int(item.get("refund_order_count") or 0))
    if target_from == "lowest_stock_sku":
        return min(products, key=lambda item: int(item.get("stock") or 0))
    raise ParamValidationError(f"未知 target_from：{target_from!r}")


def _target_id(product: Mapping[str, Any]) -> str:
    sku_id = product.get("sku_id")
    if sku_id:
        return str(sku_id)
    product_id = product.get("product_id")
    if product_id:
        return str(product_id)
    raise ParamValidationError("商品条目缺少 sku_id/product_id，无法确定动作目标")


def _expand_params(rule: Rule, action_type: ActionType, product: Mapping[str, Any],
                   template: Mapping[str, Any]) -> dict[str, Any]:
    """§8.3 的"动作模板参数 → 最终参数"冻结映射。"""
    if action_type is ActionType.PRICE_UPDATE:
        pct = template.get("price_drop_pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            raise ParamValidationError(f"price_update 模板参数 price_drop_pct 必须是数值：{pct!r}")
        if not 0 < float(pct) < 100:
            raise ParamValidationError(f"price_drop_pct 必须在 (0,100) 内：{pct!r}")
        current = product.get("price_cents")
        if not isinstance(current, int) or isinstance(current, bool) or current <= 0:
            raise ParamValidationError(
                f"商品 {product.get('sku_id')!r} 缺少有效的 price_cents，无法按比例降价"
            )
        price_cents = round(current * (1 - float(pct) / 100))
        return validate_params(action_type, {"price_cents": price_cents})
    if action_type is ActionType.STOCK_UPDATE:
        return validate_params(action_type, {"stock": template.get("stock")})
    return validate_params(action_type, dict(template))


def _risk_of(action_type: ActionType, params: Mapping[str, Any], product: Mapping[str, Any]) -> Risk:
    """§5.5 冻结的 risk 判定。"""
    if action_type is ActionType.PRICE_UPDATE:
        current = product.get("price_cents")
        new_price = params.get("price_cents")
        if isinstance(current, int) and current > 0 and isinstance(new_price, int):
            drop = (current - new_price) / current
            if drop > 0.15:
                return Risk.HIGH
            if drop > 0.05:
                return Risk.MEDIUM
        return Risk.LOW
    if action_type is ActionType.STOCK_UPDATE:
        return Risk.LOW
    return Risk.HIGH


class ProposalReport(NamedTuple):
    """提议结果 + 被丢弃的原因（去重撞车、目标解析不到、参数模板不成立）。

    契约 §5.5 的 `propose_actions` 只返回提议列表，但同一节要求"撞了说明规则配置重复，
    **记 error** 并丢后者"。节点若只拿到列表就无法记这条 error，因此把同一份计算暴露成带
    报告的形状，`propose_actions` 退化为薄包装（契约签名不变，可观测性补齐）。
    """

    proposals: list[ActionProposal]
    dropped: list[str]


def propose_actions_with_report(
    *,
    anomalies: Sequence[Anomaly],
    metrics: MetricsSnapshot,
    products: Sequence[Mapping[str, Any]],
    rules: RuleSet,
    run_id: str,
    now: datetime | None = None,
) -> ProposalReport:
    from app.config import get_settings  # 延迟导入，避免 config ↔ services 循环

    created_at = utc_iso(now)
    limit = int(get_settings().agent.max_actions_per_run)
    proposals: list[ActionProposal] = []
    dropped: list[str] = []
    seen: set[str] = set()

    for anomaly in anomalies:
        try:
            rule = rules.get(anomaly["rule_id"])
        except KeyError:
            # 异常来自另一份规则集（不该发生），跳过而不是编一个规则出来。
            dropped.append(f"异常 {anomaly['anomaly_id']} 引用了未知规则 {anomaly['rule_id']}")
            continue
        if not rule.actions:
            continue
        for action in rule.actions:
            product = _resolve_target(action.target_from, products)
            if product is None:
                dropped.append(
                    f"规则 {rule.id} 的动作 {action.type.value} 找不到目标 {action.target_from}"
                )
                continue
            target = _target_id(product)
            try:
                params = _expand_params(rule, action.type, product, action.params)
            except ParamValidationError as exc:
                # 单个动作模板不成立（例如商品没有 price_cents）不该让整批提议消失，
                # 但也不该静默——它是规则配置问题，必须能被追到。
                dropped.append(f"规则 {rule.id} 的动作 {action.type.value} 参数不成立：{exc}")
                continue
            candidate_id = action_id_for(run_id=run_id, action_type=action.type, target=target)
            if candidate_id in seen:
                dropped.append(f"action_id 重复，丢弃后者：{candidate_id}（检查规则配置）")
                continue
            seen.add(candidate_id)
            proposals.append(
                ActionProposal(
                    action_id=candidate_id,
                    # `str(...)` 而不是枚举实例：state 会被 LangGraph 的 msgpack serde 序列化，
                    # 未注册的自定义类型会告警并在未来版本被拒收。纯字符串让 checkpoint、
                    # JSONL、DB、HTTP 四处是同一份字面量。
                    action_type=str(action.type),
                    target=target,
                    params=params,
                    reason=f"{anomaly['message']}（规则 {rule.id}）",
                    evidence={
                        "anomaly_id": anomaly["anomaly_id"],
                        "rule_id": rule.id,
                        "metrics": {
                            "date": metrics.get("date"),
                            "gmv_cents": metrics.get("gmv_cents"),
                            "gmv_7d_avg_cents": metrics.get("gmv_7d_avg_cents"),
                            "refund_rate": metrics.get("refund_rate"),
                            "aov_cents": metrics.get("aov_cents"),
                        },
                        "target_snapshot": {
                            # product_id 是上游 editPrice/editStock 的必需参数（§5.1），
                            # 而 `target` 只能携带一个 id，所以两个 id 都留在证据里。
                            "product_id": product.get("product_id"),
                            "sku_id": product.get("sku_id"),
                            "price_cents": product.get("price_cents"),
                            "stock": product.get("stock"),
                        },
                    },
                    risk=str(_risk_of(action.type, params, product)),
                    created_at=created_at,
                )
            )
            if len(proposals) >= limit:
                if len(seen) < len(anomalies):  # 还有没展开的模板时留下痕迹
                    dropped.append(f"达到 max_actions_per_run={limit}，其余动作模板未展开")
                return ProposalReport(proposals=proposals, dropped=dropped)
    return ProposalReport(proposals=proposals, dropped=dropped)


def propose_actions(
    *,
    anomalies: Sequence[Anomaly],
    metrics: MetricsSnapshot,
    products: Sequence[Mapping[str, Any]],
    rules: RuleSet,
    run_id: str,
    now: datetime | None = None,
) -> list[ActionProposal]:
    """契约 §5.5 的公开签名：只返回提议列表（丢弃原因走 `propose_actions_with_report`）。"""
    return propose_actions_with_report(
        anomalies=anomalies,
        metrics=metrics,
        products=products,
        rules=rules,
        run_id=run_id,
        now=now,
    ).proposals


__all__ = [
    "ACTION_PARAM_SCHEMA",
    "PRODUCT_FIELDS",
    "ProposalReport",
    "action_id_for",
    "idempotency_key",
    "normalize_params",
    "payload_digest",
    "propose_actions",
    "propose_actions_with_report",
    "validate_params",
]
