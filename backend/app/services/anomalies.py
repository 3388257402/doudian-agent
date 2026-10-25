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

# 这个映射有两个消费者，两处都依赖"数值小 = 严重"这个方向：
#   1. `evaluate_rules` 的全序排序（critical 排最前）——它决定了 `max_anomalies` 截断时
#      **丢掉谁**：被丢的必须是 info/warn，绝不能是 critical；
#   2. 前端展示顺序（接口返回的就是排好序的列表）。
# 所以数值不能反着写（"越大越严重"），否则截断方向会在无人察觉时翻转——那是"高危告警被安静
# 丢掉、看板一切正常"的类型。新增等级时也必须同时给出序号（缺 key 会在排序处 KeyError）。

#: 可以进命名空间的字段 = §8.3.3 冻结的全集（**唯一事实源**是 `rules.ALLOWED_NAMES`）。
#:
#: v1.5.1 修过一次漂移：原实现里 :func:`_threshold_of` 手抄了一份"指标名清单"，与
#: `rules.ALLOWED_NAMES` 各自演化——`gmv_delta_ratio` / `order_delta_ratio` /
#: `refund_rate_delta` 这三个环比派生量只在 ALLOWED_NAMES 里有，于是引用它们的规则
#: （例如 `aov_drop_day`）永远提取不到 `threshold`，前端只能显示 `observed vs None`。
#: 现在两处都用同一个来源，新增名字只改 `ALLOWED_NAMES` 一处。
_METRIC_FIELDS: tuple[str, ...] = tuple(ALLOWED_NAMES)


def _previous(metrics: MetricsSnapshot, history: Sequence[MetricsSnapshot]) -> MetricsSnapshot | None:
    """历史里**日期严格早于今天**的最近一条（不含今天，避免自己和自己比）。

    保证：返回的一定是"更早的一天"——`< today` 是严格比较，所以把今天的快照混进 history 也
    不会被选中（"自己比自己"会算出 0% 环比，看起来像"今天没有变化"）。
    不保证：唯一性/连续性。"最近一条"可能是三天前（中间断了），环比就会变成"三天环比"却仍
    叫 `*_delta_ratio`——这是已知局限：本函数不检查日期间隔。
    坑：比较用的是**字符串**（`YYYY-MM-DD` 按字典序等于按时间序），所以日期格式必须是契约的
    零填充格式；`2026-9-1` 这种不补零的写法会让排序整个错位。
    """
    today = metrics["date"]
    candidates = [item for item in history if str(item.get("date", "")) < today]
    if not candidates:
        return None
    # 用 `max` 而不是 `sorted`：只要最近一条，O(n) 足够，且避免复制整个历史（调用方传的
    # 可能是几百天的桶）。
    return max(candidates, key=lambda item: str(item.get("date", "")))


def _ratio(current: Any, previous: Any) -> float | None:
    """环比比值 `(current - previous) / previous`，保留 6 位；任一侧无效 → `None`。

    保证：`previous` 为 None/0 时返回 `None`（不是 0、不是 inf）——"没有可比的基准"和
    "变化为零"是两件事，混起来会让看板显示"今天和昨天一样"。
    不保证：`current` 的类型——它只做 `is None` 判断，非数值会在减法处被 `except TypeError`
    兜成 None（所以字符串/布尔不会炸，只会静默变成 None）。
    坑：`previous in (None, 0)` 里的 `==` 语义让 `False` 也命中（`False == 0`）——即
    "上一期该字段是布尔 false"会被当成"分母为 0"。对现有指标（全是金额/计数）无害，
    但它说明这行**不是** `is None or == 0` 的严格写法。
    """
    if current is None or previous in (None, 0):
        return None
    try:
        return round((current - previous) / previous, 6)
    except (TypeError, ZeroDivisionError):
        return None


def _derived_namespace(metrics: MetricsSnapshot, history: Sequence[MetricsSnapshot]) -> dict[str, Any]:
    """算出三个环比派生量（命名空间里的 `ALLOWED_NAMES` 有它们，指标本体里没有）。

    口径（与 §8.3.3 的一致，改动前先对一遍）：

    ```
    gmv_delta_ratio   = (今日 gmv - 上一可比日 gmv) / 上一可比日 gmv      # 比值，round 6
    order_delta_ratio = (今日订单 - 上一可比日订单) / 上一可比日订单        # 比值，round 6
    refund_rate_delta = 今日退款率 - 上一可比日退款率                     # **绝对差**（百分点）
    ```

    `refund_rate` 用**差**而不是比值是刻意的：退款率本身就是"比率"，两个比率再相除会让
    "8% → 8.8%" 显示成 `+10%`——运营读出来的意思是"退款涨了一成"，而实际只涨了 0.8 个百分点。
    名字里的 `delta` 就是"绝对差"的意思，别按 `*_ratio` 的直觉读它。

    保证：三个键**永远都在**（没有上一期时写 None）——名字缺失会让规则求值抛
    `RuleEvalError`，而"没有历史"是正常状态，不是配置错误。
    不保证：上一期与今天间隔一天。"最近一条早于今天"可能是三天前，那时这三个量实际是"三天
    环比"（见 `_previous`）。
    """
    previous = _previous(metrics, history)
    derived: dict[str, Any] = {}
    if previous is not None:
        derived["gmv_delta_ratio"] = _ratio(metrics.get("gmv_cents"), previous.get("gmv_cents"))
        derived["order_delta_ratio"] = _ratio(
            metrics.get("order_count"), previous.get("order_count")
        )
        current_rate = metrics.get("refund_rate")
        previous_rate = previous.get("refund_rate")
        # 退款率是"率"，所以用减法；任一侧为 None 时给 None（不能把"没数据"当 0 参与相减，
        # 那会算出"退款率上升 8 个百分点"这种凭空结论）。
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
    """组装求值命名空间：指标字段 + 环比派生量。

    保证：① 只放 `ALLOWED_NAMES` 里**确实存在**于快照的字段（缺失的不放 → 规则引用时得到
    "缺名"错误，而不是 None 静默不命中）；② 派生量后写、可覆盖同名指标（与本仓
    `rules.build_namespace` 的 `overrides` 语义一致）。
    不保证：`history` 足够长。没有历史时三个派生量都是 None，引用它们的规则**不会触发**——
    这在"新店第一天"是正确行为，但看板上看不出来"是没数据还是没异常"。
    坑：`metrics.get(name)` 用的是 `.get` 而不是 `[]`：`MetricsSnapshot` 是 TypedDict，
    运行期就是普通 dict，缺键在这里不应该抛（缺**引用**才该抛，由表达式求值负责）。
    """
    base = {name: metrics.get(name) for name in _METRIC_FIELDS if name in metrics}
    return build_namespace(base, **_derived_namespace(metrics, history))


# 判定与展示的分工（这两条不能混，混了就会出现"阈值影响规则命中"）：
#   判定：`rule.when.evaluate(namespace)` → 严格 bool，任何提取/展示代码都不参与
#   展示：`_threshold_of(...)` → 尽力而为的数值，提取不到就是 None（前端显示 `observed vs None`）
# 所以 `threshold` 只能出现在 `Anomaly`（给人看的字段）里，**不能**被任何判定逻辑回读。
def _threshold_of(expr: CompiledExpr, namespace: dict[str, Any]) -> float | int | None:
    """从表达式里提取"这条规则的阈值"，用于前端展示 `observed vs threshold`。

    只做**保守**提取：取第一个"左侧就是主指标名、右侧可独立求值成数值"的比较，把它右侧的值
    当作阈值。提取不到（组合条件、指标在右侧且含派生量等）就返回 `None` —— 不猜。
    这个值只用于展示，判定结果永远来自整条表达式的布尔值。

    "主指标名"直接取 `rules.ALLOWED_NAMES`（见模块级 `_METRIC_FIELDS` 的注释）：曾经这里手抄
    过一份清单，与冻结集合漂移之后 `gmv_delta_ratio` 这类派生量永远提取不到阈值。
    """
    tree = expr.tree
    if tree is None:  # pragma: no cover - compile_expression 一定填了 tree
        return None
    metric_names = ALLOWED_NAMES
    # `ast.walk` 是**广度优先**、顺序按 AST 结构：所以"第一个能提取到数值的比较"就是文本里
    # 最靠左/最靠上的那个。这个顺序对展示够用（人不关心是哪一个阈值），对判定没有影响。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for comparator in node.comparators:
            candidate: ast.AST | None = None
            # 两种写法都支持：`metric 比较 常量` 与 `常量 比较 metric`（例如
            # `0.08 < refund_rate`）。只认左侧会把一半规则写成 `observed vs None`。
            if isinstance(left, ast.Name) and left.id in metric_names:
                candidate = comparator
            elif isinstance(comparator, ast.Name) and comparator.id in metric_names:
                candidate = left
            if candidate is None:
                continue
            try:
                # 复用**白名单求值器**（不是 `eval`）：候选子树同样只含白名单节点，
                # 这样"提取阈值"这条附加路径不会打开任何新的执行面（ADR-014 的边界不因为
                # 一个展示用功能而放宽）。私有类在这里被直接使用是刻意的：它不接受任意源码，
                # 只接受已经过 `compile_expression` 校验的子树。
                from app.services.rules import _Evaluator  # noqa: PLC0415 - 私有求值器，仅本模块用

                value = _Evaluator(namespace, rule_id=expr.rule_id).eval(candidate)
            except RuleEvalError:
                # 提取失败**不是**判定失败：continue 去看下一个比较。这里绝不能抛——
                # 阈值只是展示字段，让它影响"规则是否命中"就是把展示逻辑混进判定。
                continue
            if isinstance(value, bool):
                # `is not None` 这类比较的右侧是个**名字**，求值出来是 bool，不是阈值。
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

    三个字段的口径（写日志时别混）：
    - `anomalies`：**已截断**的列表，长度 ≤ `agent.max_anomalies`；
    - `skipped`：被 `RuleEvalError` 跳过的规则，`(rule_id, reason)`，与截断无关（是"算不出来"）；
    - `dropped`：因为截断而**没进** `anomalies` 的条数（是"算出来了但太多"）。
    所以 `len(anomalies) + dropped` = 命中总数，而 `skipped` 里的规则**不在这两个数里**。
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
    """逐条求值所有 `enabled=True` 的规则，返回 :class:`RuleEvaluation`。

    保证：① 一条规则的 `RuleEvalError` 只影响它自己（跳过并记进 `skipped`），绝不中断整批；
    ② 输出按"severity 降序 + rule_id"全序排列后才做 `max_anomalies` 截断，所以被丢掉的
    永远是低等级的（`dropped` 里给出条数）；
    ③ `anomaly_id` 只由 `(run_id, rule_id, metric)` 决定 —— 同一轮同一规则重复求值得到同一
    id（下游按它去重就不会出现两条一样的告警）。
    不保证：`threshold` 的正确性（那是 `_threshold_of` 的保守提取，可能为 None）；也不保证
    `observed` 与真实业务一致——它直接取 `metrics[rule.metric]`，规则把 `metric` 配错了就是
    None，本函数不纠正。
    坑：① `skipped` 是 `(rule_id, reason)` 元组列表，调用方负责把它记进 `errors`——本函数的
    签名（§5.4 的 `detect_anomalies`）只返回异常列表，往里塞第二份返回值等于改契约，所以
    可观测性通过 `RuleEvaluation` 暴露；
    ② `max_anomalies=None` 时**才**去读 settings：显式传 0 表示"一条都不要"，不能被
    `or` 吞成默认值（这里用的是 `is None` 判断，别改成 `or`）。
    """
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
                # threshold 只是展示值：提取不到就是 None，不影响 `hit`（判定早就算完了）。
                threshold=_threshold_of(rule.when, namespace),
                # 原文表达式（不是算子枚举）：契约明确禁止拿它做语义解析，只用于展示。
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

    # 全序排序必须在截断**之前**：反过来的话被丢的是"规则文件里靠后的"，与严重性无关。
    found.sort(key=lambda item: (_SEVERITY_ORDER[Severity(item["severity"])], item["rule_id"]))
    limit = _anomaly_limit() if max_anomalies is None else int(max_anomalies)
    # `max(0, ...)`：负数上限（配置写错）会让 `found[: -3]` 变成"去掉最后三条"，
    # 那是完全相反的语义。
    dropped = max(0, len(found) - limit)
    return RuleEvaluation(anomalies=found[:limit], skipped=skipped, dropped=dropped)


def _anomaly_limit() -> int:
    """读 `settings.agent.max_anomalies`（延迟导入避免 config ↔ services 的循环）。"""
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
    """契约 §5.4 的公开签名：只返回异常列表（跳过/截断的可见性走 `evaluate_rules`）。

    保证：与 `evaluate_rules(...).anomalies` 逐项相同（没有任何额外逻辑）。
    不保证：`skipped`/`dropped` —— 用这个入口的调用方看不到"有规则被跳过"，这正是节点应当
    改用 `evaluate_rules` 的原因。
    坑：注意它**不**接受 `max_anomalies`：截断上限只能从 settings 来。想在测试里控制截断，
    用 `evaluate_rules(max_anomalies=N)`。
    """
    return evaluate_rules(
        metrics=metrics, history=history, rules=rules, run_id=run_id, now=now
    ).anomalies


__all__ = ["RuleEvaluation", "build_eval_namespace", "detect_anomalies", "evaluate_rules"]
