"""异常检测 —— 对应 docs/CONTRACT.md §5.4。

纯函数：不碰 I/O，`now` 可注入。每条 `enabled=True` 的规则求值一次，`True` 就产出一条
`Anomaly`；`RuleEvalError`（除零 / None 参与数值比较 / 布尔算术 / 结果非 bool）**不向外抛**，
该规则跳过——但"跳过"必须可见，所以调用方（`nodes.detect_anomalies_node`）会另外记一条
`errors`。这里不记，是因为本函数的签名（§5.4）只返回 `list[Anomaly]`，往里塞第二份返回值
等于悄悄改契约。
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple

from app.agent.state import MetricsSnapshot, Anomaly, utc_iso
from app.enums import Severity
from app.services.rules import ALLOWED_NAMES, CompiledExpr, RuleEvalError, RuleSet, build_namespace

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARN: 1, Severity.INFO: 2}

#: `MetricsSnapshot` 里可以进命名空间的字段（其余派生量由 :func:`_derived_namespace` 补）
_METRIC_FIELDS = tuple(name for name in ALLOWED_NAMES)


def _previous(metrics: MetricsSnapshot, history: Sequence[MetricsSnapshot]) -> MetricsSnapshot | None:
    """历史里**日期严格早于今天**的最近一条（不含今天，避免自己和自己比）。"""
    today = metrics["date"]
    candidates = [item for item in history if str(item.get("date", "")) < today]
    if not candidates:
        return None
    return max(candidates, key=lambda item: str(item.get("date", "")))


def _ratio(current: Any, previous: Any) -> float | None:
    if current is None or previous in (None, 0):
        return None
    try:
        return round((current - previous) / previous, 6)
    except (TypeError, ZeroDivisionError):
        return None


def _derived_namespace(metrics: MetricsSnapshot, history: Sequence[MetricsSnapshot]) -> dict[str, Any]:
    previous = _previous(metrics, history)
    derived: dict[str, Any] = {}
    if previous is not None:
        derived["gmv_delta_ratio"] = _ratio(metrics.get("gmv_cents"), previous.get("gmv_cents"))
        derived["order_delta_ratio"] = _ratio(
            metrics.get("order_count"), previous.get("order_count")
        )
        current_rate = metrics.get("refund_rate")
        previous_rate = previous.get("refund_rate")
        derived["refund_rate_delta"] = (
            round(current_rate - previous_rate, 6)
            if current_rate is not None and previous_rate is not None
            else None
        )
    else:
        # 名字必须在（缺名 = 规则写错了）；值缺失用 None，比较结果为 False（§8.3.3）。
        derived.update(
            {"gmv_delta_ratio": None, "order_delta_ratio": None, "refund_rate_delta": None}
        )
    return derived


def build_eval_namespace(
    metrics: MetricsSnapshot, history: Sequence[MetricsSnapshot] = ()
) -> dict[str, Any]:
    """组装求值命名空间：指标字段 + 环比派生量。"""
    base = {name: metrics.get(name) for name in _METRIC_FIELDS if name in metrics}
    return build_namespace(base, **_derived_namespace(metrics, history))


def _threshold_of(expr: CompiledExpr, namespace: dict[str, Any]) -> float | int | None:
    """从表达式里提取"这条规则的阈值"，用于前端展示 `observed vs threshold`。

    只做**保守**提取：取第一个"左侧就是主指标名、右侧可独立求值成数值"的比较，把它右侧的值
    当作阈值。提取不到（组合条件、指标在右侧且含派生量等）就返回 `None` —— 不猜。
    这个值只用于展示，判定结果永远来自整条表达式的布尔值。
    """
    tree = expr.tree
    if tree is None:  # pragma: no cover - compile_expression 一定填了 tree
        return None
    metric_names = {"gmv_cents", "order_count", "paid_order_count", "refund_order_count",
                    "refund_amount_cents", "aov_cents", "refund_rate", "uv", "cvr",
                    "gmv_7d_avg_cents"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for comparator in node.comparators:
            candidate: ast.AST | None = None
            if isinstance(left, ast.Name) and left.id in metric_names:
                candidate = comparator
            elif isinstance(comparator, ast.Name) and comparator.id in metric_names:
                candidate = left
            if candidate is None:
                continue
            try:
                from app.services.rules import _Evaluator  # noqa: PLC0415 - 私有求值器，仅本模块用

                value = _Evaluator(namespace, rule_id=expr.rule_id).eval(candidate)
            except RuleEvalError:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return value
    return None


class RuleEvaluation(NamedTuple):
    """一次检测的完整结果：命中的异常 + "被跳过"与"被截断"的可见记录。

    契约 §5.4 的 `detect_anomalies` 只返回 `list[Anomaly]`，但同一节又要求
    "`RuleEvalError` 该规则跳过，并在 `Anomaly` 之外记一条 `errors`（由节点负责）"。节点若只
    拿到 `list[Anomaly]`，"跳过"就会静默消失（规则写错了却没人知道）。所以把同一份计算暴露
    成带报告的形状，`detect_anomalies` 退化为它的薄包装——契约签名不变，可观测性补齐。
    """

    anomalies: list[Anomaly]
    skipped: list[tuple[str, str]]  # (rule_id, reason)
    dropped: int  # 因 max_anomalies 截断而丢弃的条数


def evaluate_rules(
    *,
    metrics: MetricsSnapshot,
    history: Sequence[MetricsSnapshot],
    rules: RuleSet,
    run_id: str,
    now: datetime | None = None,
    max_anomalies: int | None = None,
) -> RuleEvaluation:
    """逐条求值所有 `enabled=True` 的规则，返回 :class:`RuleEvaluation`。"""
    namespace = build_eval_namespace(metrics, history)
    detected_at = utc_iso(now)
    found: list[Anomaly] = []
    skipped: list[tuple[str, str]] = []
    for rule in rules.enabled_rules:
        try:
            hit = rule.when.evaluate(namespace)
        except RuleEvalError as exc:
            # 该规则跳过，但"跳过"必须可见（§5.4）。绝不把它变成"False 且不说"。
            skipped.append((rule.id, str(exc)))
            continue
        if not hit:
            continue
        observed = metrics.get(rule.metric)
        found.append(
            Anomaly(
                anomaly_id=hashlib.sha256(
                    f"{run_id}|{rule.id}|{rule.metric}".encode()
                ).hexdigest()[:16],
                rule_id=rule.id,
                metric=rule.metric,
                observed=observed,
                threshold=_threshold_of(rule.when, namespace),
                comparator=rule.when.source,
                # 刻意用 `str(...)` 而不是直接塞枚举实例：LangGraph 的 msgpack serde 对未注册
                # 的自定义类型会告警、并在未来版本直接拒收（实测 "Deserializing unregistered
                # type app.enums.Severity from checkpoint"）。state 里只放纯 JSON 值，
                # 接口/DB/日志三处共用同一批字符串字面量。
                severity=str(rule.severity),
                message=f"{rule.description}（{rule.metric}={observed}，条件 {rule.when.source}）",
                window=rule.window,
                detected_at=detected_at,
            )
        )

    found.sort(key=lambda item: (_SEVERITY_ORDER[Severity(item["severity"])], item["rule_id"]))
    limit = _anomaly_limit() if max_anomalies is None else int(max_anomalies)
    dropped = max(0, len(found) - limit)
    return RuleEvaluation(anomalies=found[:limit], skipped=skipped, dropped=dropped)


def _anomaly_limit() -> int:
    from app.config import get_settings  # 延迟导入：避免 config ↔ rules 的循环导入

    return int(get_settings().agent.max_anomalies)


def detect_anomalies(
    *,
    metrics: MetricsSnapshot,
    history: Sequence[MetricsSnapshot],
    rules: RuleSet,
    run_id: str,
    now: datetime | None = None,
) -> list[Anomaly]:
    """契约 §5.4 的公开签名：只返回异常列表（跳过/截断的可见性走 `evaluate_rules`）。"""
    return evaluate_rules(
        metrics=metrics, history=history, rules=rules, run_id=run_id, now=now
    ).anomalies


__all__ = ["RuleEvaluation", "build_eval_namespace", "detect_anomalies", "evaluate_rules"]
