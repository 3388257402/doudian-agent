"""规则引擎：YAML 加载 + 白名单 AST 求值 —— 对应 docs/CONTRACT.md §8.3。

**绝不使用** `eval` / `exec` / `compile` / `ast.literal_eval`。`ast.parse(mode="eval")` 只用来
建 AST，求值是本文件里手写的递归下降解释器（`_Evaluator`）。理由见 DECISIONS ADR-014：
白名单里没有 `Call`，就没有"表达式里能调用任何东西"这一整类攻击面——没有 `abs()`、没有
`len()`、没有 `__import__`，也没有任何属性访问能走到 `__class__`。

三处硬约束（违反即 bug）：

1. **加载期**（`load_rules`）必须完成全部校验：YAML 结构、字段白名单、表达式语法、AST 白名单、
   所有 `Name` 都在 `allowed_names` 内。任何一项失败 → `RuleLoadError`，带 `rule_id` 与位置。
   不允许把"未知字段"或"拼错的指标名"拖到运行期才发现——那时它只会表现为"规则永远不触发"。
2. **运行期**（`CompiledExpr.evaluate`）的失败一律 `RuleEvalError`（除零、None 参与数值比较、
   布尔参与算术、NaN/Inf、结果不是 bool）。调用方（`detect_anomalies`）负责跳过该规则并记录，
   绝不吞掉不说。
3. `allowed_names` 是**冻结集合**（§8.3.3）。多一个名字都要走契约变更流程——因为每加一个名字
   就多一个"规则作者能引用但可能永远是 None"的字段，静默不触发是最难查的一类故障。
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from app.enums import ActionType, Severity

# §8.3.3 的**两个**名字集合。互斥是"两轮判定唯一性"的前提：规则引用了哪一侧的名字，
# 就决定了它在"店铺轮"还是"SKU 轮"求值（§5.4）。一个名字同时属于两边 = 轮次有歧义。
#
# 为什么要显式拆成两个集合而不是一个扁平 frozenset（v1.5.0，ADR-021 A 条）：
# `gross_margin` 在店铺级是"全店有成本覆盖的加权毛利率"，在 SKU 级是"单件售价与单件成本之差"，
# **分母不同、精度不同、用途不同**。扁平集合下，谁把它同时加进两边都没人拦——直到某个
# 为全店调的 `gross_margin < 0` 被套到单品上，把"薄利"判成"亏本"，Agent 就会去给正常商品
# 改价/下架。那是本仓最贵的一类误报，所以这条门禁必须存在，且失败时要报得出来。
SHOP_NAMES: frozenset[str] = frozenset(
    {
        "gmv_cents",
        "order_count",
        "paid_order_count",
        "refund_order_count",
        "refund_amount_cents",
        "aov_cents",
        "refund_rate",
        "uv",
        "cvr",
        "gmv_7d_avg_cents",
        # v1.5.0：店铺级成本口径（`compute_metrics(..., cost_table=…)`，§8.5 的成本表）。
        # 两个名字必须成对使用：只看毛利率会拿"5% 成交量"的结论去改全店的价。
        "gross_margin",
        "cost_coverage_ratio",
        # 派生量（由上面几个算出，不是上游直接给）
        "gmv_delta_ratio",
        "order_delta_ratio",
        "refund_rate_delta",
    }
)

#: SKU 轮命名空间。**按 §8.3.3 冻结清单写全 10 个，即使其中 6 个还没实现**——
#: 写全是这条门禁的意义所在：门禁要拦的是"将来有人把 `gross_margin` 也加进 SKU 侧"，
#: 而不是"现在有几个名字没实现"。未实现的那些仍会被 `ALLOWED_NAMES` 挡在 `load_rules` 之外。
#: 注意：SKU 侧毛利率的**唯一合法名字是 `sku_gross_margin`**（§8.3.3 命名硬规则 / ADR-021）。
SKU_NAMES: frozenset[str] = frozenset(
    {
        "stock",
        "current_price_cents",
        "stock_days",
        "is_on_sale",
        "sku_gross_margin",
        "cost_cents",
        "sales_7d",
        "avg_daily_sales_7d",
        "ad_spend_cents_7d",
        "ad_order_count_7d",
    }
)

#: 当前**可用**的名字 = 店铺级全部 + SKU 级已实现的 4 个。
#: 与 v1.5.0 之前的行为完全一致（19 个名字），SKU 轮次落地时再把其余 6 个加进来。
ALLOWED_NAMES: frozenset[str] = frozenset(SHOP_NAMES) | frozenset(
    {"stock", "current_price_cents", "stock_days", "is_on_sale"}
)


def assert_name_sets_disjoint() -> None:
    """断言 §8.3.3 的两个名字集合互斥，且 `ALLOWED_NAMES` 没有越界。

    **为什么在 import 期就调用一次**：这是**代码写错了**（不是配置写错了），
    越早炸越好——等到某个客户店的 `rules.yaml` 加载时才炸，现场看到的是"规则加载失败"，
    真正的原因（两个常量集合重叠）要翻三层代码才找得到。

    **为什么 `load_rules` 里也调**：契约 §8.3.3 把这条写成了加载期门禁，
    运维在改完 `rules.py` 后只要调用一次 `load_rules` 就该看到结果，不必依赖 import 时机。

    报错必须带**交集名字**并指向 §8.3.3 / ADR-021——不允许裸 `AssertionError`、
    不允许"取其一"式自动改名（那会让阈值在无人察觉时换了含义）。
    """
    overlap = SHOP_NAMES & SKU_NAMES
    if overlap:
        raise RuleLoadError(
            "§8.3.3 命名空间被破坏：以下名字同时属于店铺级与 SKU 级 → "
            f"{sorted(overlap)}。两集合必须互斥（规则引用的名字决定求值轮次，"
            "同时属于两边会让轮次不确定）。SKU 侧毛利率的唯一合法名字是 "
            "`sku_gross_margin`——见 §8.3.3「命名硬规则」/ ADR-021",
            rule_id="<namespace>",
        )
    outside = ALLOWED_NAMES - (SHOP_NAMES | SKU_NAMES)
    if outside:
        raise RuleLoadError(
            f"§8.3.3：ALLOWED_NAMES 里有不属于任何一轮的名字 {sorted(outside)}——"
            "每个可用名字都必须能归入店铺级或 SKU 级，否则规则求值时没有确定的轮次",
            rule_id="<namespace>",
        )
    missing = SHOP_NAMES - ALLOWED_NAMES
    if missing:
        raise RuleLoadError(
            f"§8.3.3：店铺级名字 {sorted(missing)} 未出现在 ALLOWED_NAMES 里——"
            "店铺轮规则会因为'名字不在命名空间'而被拒绝加载",
            rule_id="<namespace>",
        )


# import 期的第一次校验**不能写在这里**：`RuleLoadError` 在下面才定义。
# 那条校验的唯一职责就是失败时抛 `RuleLoadError` 并带上 §8.3.3 / ADR-021 的报错文本；
# 若在异常类定义前调用，失败时抛的是 `NameError`——报错文本全丢，门禁在最需要它的
# 那一刻失效。这个顺序错误是独立审查实测抓出来的（构造重叠集合 → 期望 RuleLoadError，
# 实际 NameError）。真正的调用点在 `RuleLoadError` 定义之后。


_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SKU_TARGET_RE = re.compile(r"^sku:[A-Za-z0-9_-]{1,64}$")
_TARGET_SOURCES = frozenset({"top_gmv_sku", "top_refund_sku", "lowest_stock_sku"})
_WINDOWS = frozenset({"day", "7d"})

_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"


# --------------------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------------------


class RuleLoadError(Exception):
    """加载期错误（结构 / 语法 / 白名单 / 未知名字 / 未知字段）。

    `rule_id` / `position` / `source` 三个字段是给人和前端看的：一条"规则加载失败"必须能直接
    定位到"哪个文件的哪条规则的哪个字符"，否则只能靠通读 yaml。
    """

    def __init__(
        self,
        message: str,
        *,
        rule_id: str | None = None,
        position: str | None = None,
        source: str | None = None,
    ) -> None:
        self.message = message
        self.rule_id = rule_id
        self.position = position
        self.source = source
        parts = [message]
        if rule_id is not None:
            parts.append(f"rule_id={rule_id}")
        if position is not None:
            parts.append(f"at {position}")
        if source is not None:
            parts.append(f"source={source!r}")
        super().__init__(" | ".join(parts))


# import 期立刻验一次：写错常量属于代码缺陷，不该等到某个店的配置加载才暴露。
# 位置必须在 `RuleLoadError` 定义之后，否则失败时抛 `NameError` 而不是带条款指的
# `RuleLoadError`（见上方那段说明）。
assert_name_sets_disjoint()


class RuleEvalError(Exception):
    """运行期错误（除零、None 比较、布尔算术、NaN/Inf、结果非 bool）。

    刻意**不**继承 `RuleLoadError`：一个是"配置坏了，进程不该启动"，一个是"这条规则今天
    算不出来，跳过它"，处置方式完全不同。
    """

    def __init__(self, message: str, *, rule_id: str | None = None) -> None:
        self.message = message
        self.rule_id = rule_id
        super().__init__(f"{message} (rule_id={rule_id})" if rule_id else message)


# --------------------------------------------------------------------------------------
# AST 白名单（§8.3.2）
# --------------------------------------------------------------------------------------

_ALLOWED_NODE_TYPES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.Constant,
    ast.Name,
    ast.Tuple,
    ast.List,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)
_ALLOWED_NODE_TYPE_SET = frozenset(_ALLOWED_NODE_TYPES)

_FORBIDDEN_EXAMPLES = {
    ast.Call: "函数调用（本引擎没有 abs()/len() 之类，见 ADR-014）",
    ast.Attribute: "属性访问（可绕过一切白名单，如 __class__.__mro__）",
    ast.Subscript: "下标访问（同样可绕到类型对象上）",
    ast.Lambda: "lambda",
    ast.Dict: "字典字面量",
    ast.Set: "集合字面量",
    ast.DictComp: "字典推导式",
    ast.SetComp: "集合推导式",
    ast.ListComp: "列表推导式",
    ast.GeneratorExp: "生成器表达式",
    ast.IfExp: "条件表达式（请用 and/or 表达）",
    ast.Starred: "星号展开",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield from",
    ast.JoinedStr: "f-string",
    ast.FormattedValue: "f-string 插值",
    ast.NamedExpr: "海象运算符",
    ast.Slice: "切片",
}


def _position(node: ast.AST) -> str:
    line = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    if line is None:
        return "<expr>"
    return f"{line}:{(col or 0) + 1}"


def _is_dunder(name: str) -> bool:
    return name.startswith("__") or name.endswith("__")


def _check_ast(tree: ast.Expression, *, source: str, rule_id: str | None) -> frozenset[str]:
    """逐节点检查白名单，返回表达式引用的名字集合。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        node_type = type(node)
        if node_type not in _ALLOWED_NODE_TYPE_SET:
            reason = _FORBIDDEN_EXAMPLES.get(
                node_type, f"不在白名单内的语法节点 {node_type.__name__}"
            )
            raise RuleLoadError(
                f"表达式含禁止的语法：{reason}",
                rule_id=rule_id,
                position=_position(node),
                source=source,
            )
        if isinstance(node, ast.Name):
            if _is_dunder(node.id):
                raise RuleLoadError(
                    f"表达式引用了双下划线名字 {node.id!r}",
                    rule_id=rule_id,
                    position=_position(node),
                    source=source,
                )
            names.add(node.id)
        elif isinstance(node, ast.Constant):
            value = node.value
            if not isinstance(value, (int, float, str, bool, type(None))):
                raise RuleLoadError(
                    f"常量类型不允许：{type(value).__name__}",
                    rule_id=rule_id,
                    position=_position(node),
                    source=source,
                )
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                raise RuleLoadError(
                    "常量不允许 NaN/Inf",
                    rule_id=rule_id,
                    position=_position(node),
                    source=source,
                )
    return frozenset(names)


# --------------------------------------------------------------------------------------
# 求值器（手写递归下降，不用 eval）
# --------------------------------------------------------------------------------------


def _require_number(value: Any, *, op: str, node: ast.AST, rule_id: str | None) -> Any:
    if isinstance(value, bool):
        # §8.3.3：bool 不是 int，布尔参与算术没有业务意义 —— 显式拒绝而不是让它悄悄算成 1/0。
        raise RuleEvalError(
            f"布尔值不能参与算术运算（{op}，位置 {_position(node)}）", rule_id=rule_id
        )
    if not isinstance(value, (int, float)):
        raise RuleEvalError(
            f"{op} 的操作数不是数值：{type(value).__name__}（位置 {_position(node)}）",
            rule_id=rule_id,
        )
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise RuleEvalError(f"{op} 的操作数是 NaN/Inf（位置 {_position(node)}）", rule_id=rule_id)
    return value


def _check_finite(value: Any, *, node: ast.AST, rule_id: str | None) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise RuleEvalError(f"结果出现 NaN/Inf（位置 {_position(node)}）", rule_id=rule_id)
    return value


class _Evaluator:
    def __init__(self, namespace: Mapping[str, Any], *, rule_id: str | None) -> None:
        self._ns = namespace
        self._rule_id = rule_id

    def eval(self, node: ast.AST) -> Any:
        method = getattr(self, f"_eval_{type(node).__name__}", None)
        if method is None:  # pragma: no cover - compile_expression 已挡住
            raise RuleEvalError(
                f"无法求值的节点类型 {type(node).__name__}", rule_id=self._rule_id
            )
        return method(node)

    # --- 表达式/字面量 ---
    def _eval_Expression(self, node: ast.Expression) -> Any:
        return self.eval(node.body)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _eval_List(self, node: ast.List) -> list[Any]:
        return [self.eval(item) for item in node.elts]

    def _eval_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.eval(item) for item in node.elts)

    def _eval_Name(self, node: ast.Name) -> Any:
        if node.id not in self._ns:
            # 名字不在 namespace 里 = 调用方没提供这个量。契约要求这是错误，不是 False：
            # 静默 False 会让"忘了传 uv"表现成"uv 规则永远不触发"。
            raise RuleEvalError(
                f"命名空间缺少字段 {node.id!r}（位置 {_position(node)}）", rule_id=self._rule_id
            )
        return self._ns[node.id]

    # --- 逻辑 ---
    def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = self.eval(value_node)
                if not _truthy(result):
                    return result
            return result
        result = False
        for value_node in node.values:
            result = self.eval(value_node)
            if _truthy(result):
                return result
        return result

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.eval(node.operand)
        if isinstance(node.op, ast.Not):
            return not _truthy(operand)
        if operand is None:
            # 缺值传播（§8.3.3）：`-gmv_7d_avg_cents` 在值为 None 时不是错误，而是"仍然缺值"。
            # 缺少这一步，`gmv_cents < gmv_7d_avg_cents * 0.7` 会在算术阶段抛异常，
            # 于是"规则正常求值、未命中"被误报成"求值失败"（运维会去追一个不存在的故障）。
            return None
        number = _require_number(
            operand, op="一元 +/-", node=node, rule_id=self._rule_id
        )
        return -number if isinstance(node.op, ast.USub) else +number

    def _eval_BinOp(self, node: ast.BinOp) -> Any:
        left_raw = self.eval(node.left)
        right_raw = self.eval(node.right)
        if left_raw is None or right_raw is None:
            # 同上：算术任一操作数缺值 → 结果缺值（比较阶段再判 False）。
            return None
        left = _require_number(left_raw, op="算术运算", node=node, rule_id=self._rule_id)
        right = _require_number(right_raw, op="算术运算", node=node, rule_id=self._rule_id)
        if isinstance(node.op, ast.Add):
            return _check_finite(left + right, node=node, rule_id=self._rule_id)
        if isinstance(node.op, ast.Sub):
            return _check_finite(left - right, node=node, rule_id=self._rule_id)
        if isinstance(node.op, ast.Mult):
            return _check_finite(left * right, node=node, rule_id=self._rule_id)
        if isinstance(node.op, (ast.Div, ast.Mod)):
            if right == 0:
                op_name = "除以" if isinstance(node.op, ast.Div) else "对"
                raise RuleEvalError(
                    f"{op_name} 0（位置 {_position(node)}）", rule_id=self._rule_id
                )
            value = left / right if isinstance(node.op, ast.Div) else left % right
            return _check_finite(value, node=node, rule_id=self._rule_id)
        raise RuleEvalError(  # pragma: no cover - 白名单已挡住
            f"不支持的运算符 {type(node.op).__name__}", rule_id=self._rule_id
        )

    # --- 比较 ---
    def _eval_Compare(self, node: ast.Compare) -> bool:
        left = self.eval(node.left)
        for op, comparator_node in zip(node.ops, node.comparators, strict=True):
            right = self.eval(comparator_node)
            if not self._compare(op, left, right, node=node):
                return False
            left = right
        return True

    def _compare(self, op: ast.cmpop, left: Any, right: Any, *, node: ast.AST) -> bool:
        if isinstance(op, (ast.Is, ast.IsNot)):
            same = left is right
            return same if isinstance(op, ast.Is) else not same
        if isinstance(op, (ast.In, ast.NotIn)):
            container = right
            if container is None or isinstance(container, (int, float, bool)):
                raise RuleEvalError(
                    f"'in' 右侧不是容器：{type(container).__name__}（位置 {_position(node)}）",
                    rule_id=self._rule_id,
                )
            try:
                found = left in container
            except TypeError as exc:
                raise RuleEvalError(
                    f"'in' 无法比较：{exc}（位置 {_position(node)}）", rule_id=self._rule_id
                ) from exc
            return found if isinstance(op, ast.In) else not found
        # 数值/字符串比较：任一侧 None => False（§8.3.3：不是异常，也不是 True）
        if left is None or right is None:
            return False
        for side in (left, right):
            if isinstance(side, float) and (math.isnan(side) or math.isinf(side)):
                raise RuleEvalError(
                    f"NaN/Inf 参与比较（位置 {_position(node)}）", rule_id=self._rule_id
                )
        try:
            if isinstance(op, ast.Eq):
                return bool(left == right)
            if isinstance(op, ast.NotEq):
                return bool(left != right)
            if isinstance(op, ast.Lt):
                return bool(left < right)
            if isinstance(op, ast.LtE):
                return bool(left <= right)
            if isinstance(op, ast.Gt):
                return bool(left > right)
            if isinstance(op, ast.GtE):
                return bool(left >= right)
        except TypeError as exc:
            raise RuleEvalError(
                f"类型不可比较：{type(left).__name__} 与 {type(right).__name__}"
                f"（位置 {_position(node)}）",
                rule_id=self._rule_id,
            ) from exc
        raise RuleEvalError(  # pragma: no cover - 白名单已挡住
            f"不支持的比较运算符 {type(op).__name__}", rule_id=self._rule_id
        )


def _truthy(value: Any) -> bool:
    """`and`/`or` 的短路判定。

    这里刻意用 Python 的真值语义（而不是要求 bool），因为 `a and b` 的中间结果可以是任意值；
    但**表达式最终结果**必须是 bool（由 :meth:`CompiledExpr.evaluate` 保证）。
    """
    return bool(value)


# --------------------------------------------------------------------------------------
# 编译后的表达式
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledExpr:
    """契约 §8.3.3 的形状（`source` + `names`）。

    `_tree` 是编译产物，由 :func:`compile_expression` 填入；默认 `None` 时求值会明确报错，
    而不是"悄悄返回 False"。这样"手工构造一个 CompiledExpr 绕过编译"会立刻暴露。
    """

    source: str
    names: frozenset[str]
    _tree: ast.Expression | None = field(default=None, repr=False, compare=False)
    rule_id: str | None = None

    def evaluate(self, namespace: Mapping[str, Any]) -> bool:
        if self._tree is None:
            raise RuleEvalError(
                "CompiledExpr 没有编译产物，请用 compile_expression() 构造", rule_id=self.rule_id
            )
        missing = self.names.difference(namespace)
        if missing:
            raise RuleEvalError(
                f"命名空间缺少字段 {sorted(missing)}", rule_id=self.rule_id
            )
        evaluator = _Evaluator(namespace, rule_id=self.rule_id)
        result = evaluator.eval(self._tree)
        if not isinstance(result, bool):
            raise RuleEvalError(
                f"表达式结果不是 bool 而是 {type(result).__name__}（{result!r}）",
                rule_id=self.rule_id,
            )
        return result

    @property
    def tree(self) -> ast.Expression:
        return self._tree


def compile_expression(
    src: str, allowed_names: frozenset[str] | set[str] | None = None, *, rule_id: str | None = None
) -> CompiledExpr:
    """编译并校验一个条件表达式。任何不合规都抛 :class:`RuleLoadError`。"""
    if not isinstance(src, str) or not src.strip():
        raise RuleLoadError("条件表达式为空", rule_id=rule_id, source=src)
    allowed = frozenset(ALLOWED_NAMES if allowed_names is None else allowed_names)
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        position = f"{exc.lineno or 1}:{exc.offset or 1}"
        raise RuleLoadError(
            f"表达式语法错误：{exc.msg}", rule_id=rule_id, position=position, source=src
        ) from exc
    names = _check_ast(tree, source=src, rule_id=rule_id)
    unknown = names.difference(allowed)
    if unknown:
        raise RuleLoadError(
            f"表达式引用了未定义的字段 {sorted(unknown)}；允许的字段见 CONTRACT §8.3.3",
            rule_id=rule_id,
            source=src,
        )
    return CompiledExpr(source=src, names=names, _tree=tree, rule_id=rule_id)


def evaluate(expr: CompiledExpr, namespace: Mapping[str, Any]) -> bool:
    """§8.3.3 的模块级求值入口（等价于 `expr.evaluate(namespace)`）。"""
    if not isinstance(expr, CompiledExpr):  # pragma: no cover - 类型误用防御
        raise RuleEvalError(f"evaluate 需要 CompiledExpr，收到 {type(expr).__name__}")
    return expr.evaluate(namespace)


# --------------------------------------------------------------------------------------
# RuleSet
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleAction:
    type: ActionType
    target_from: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Rule:
    id: str
    enabled: bool
    severity: Severity
    metric: str
    when: CompiledExpr
    window: str
    description: str
    actions: tuple[RuleAction, ...] = ()


@dataclass(frozen=True)
class RuleSet:
    version: str
    rules: tuple[Rule, ...]
    path: str | None = None

    def get(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    @property
    def enabled_rules(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.enabled)


_RULE_REQUIRED_KEYS = frozenset(
    {"id", "enabled", "severity", "metric", "when", "window", "description"}
)
_RULE_ALLOWED_KEYS = _RULE_REQUIRED_KEYS | {"actions"}
_ACTION_REQUIRED_KEYS = frozenset({"type", "target_from", "params"})
_TOP_LEVEL_ALLOWED_KEYS = frozenset({"version", "rules"})


def _fail(message: str, *, rule_id: str | None = None, position: str | None = None,
          source: str | None = None) -> RuleLoadError:
    return RuleLoadError(message, rule_id=rule_id, position=position, source=source)


def _parse_action(raw: Any, *, rule_id: str, index: int) -> RuleAction:
    where = f"actions[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where} 必须是映射", rule_id=rule_id)
    unknown = set(raw).difference(_ACTION_REQUIRED_KEYS)
    if unknown:
        raise _fail(f"{where} 含未知字段 {sorted(unknown)}", rule_id=rule_id)
    missing = _ACTION_REQUIRED_KEYS.difference(raw)
    if missing:
        raise _fail(f"{where} 缺字段 {sorted(missing)}", rule_id=rule_id)
    try:
        action_type = ActionType(str(raw["type"]))
    except ValueError as exc:
        raise _fail(
            f"{where}.type 非法：{raw['type']!r}（允许 {[t.value for t in ActionType]}）",
            rule_id=rule_id,
        ) from exc
    target_from = raw["target_from"]
    if not isinstance(target_from, str) or (
        target_from not in _TARGET_SOURCES and not _SKU_TARGET_RE.match(target_from)
    ):
        raise _fail(
            f"{where}.target_from 非法：{target_from!r}"
            f"（允许 {sorted(_TARGET_SOURCES)} 或 'sku:<id>'）",
            rule_id=rule_id,
        )
    params = raw["params"]
    if not isinstance(params, dict) or not params:
        raise _fail(f"{where}.params 必须是非空映射", rule_id=rule_id)
    return RuleAction(type=action_type, target_from=target_from, params=dict(params))


def _parse_rule(raw: Any, *, index: int, allowed_names: frozenset[str]) -> Rule:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where} 必须是映射")
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not _RULE_ID_RE.match(rule_id):
        raise _fail(f"{where}.id 非法：{rule_id!r}（须匹配 ^[a-z][a-z0-9_]{{1,63}}$）")
    unknown = set(raw).difference(_RULE_ALLOWED_KEYS)
    if unknown:
        raise _fail(f"未知字段 {sorted(unknown)}", rule_id=rule_id)
    missing = _RULE_REQUIRED_KEYS.difference(raw)
    if missing:
        raise _fail(f"缺字段 {sorted(missing)}", rule_id=rule_id)

    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise _fail(f"enabled 必须是 bool，收到 {type(enabled).__name__}", rule_id=rule_id)
    try:
        severity = Severity(str(raw["severity"]))
    except ValueError as exc:
        raise _fail(
            f"severity 非法：{raw['severity']!r}（允许 {[s.value for s in Severity]}）",
            rule_id=rule_id,
        ) from exc
    metric = raw["metric"]
    if not isinstance(metric, str):
        raise _fail("metric 必须是字符串", rule_id=rule_id)
    if metric not in allowed_names:
        raise _fail(
            f"metric {metric!r} 不在命名空间内；允许的字段见 CONTRACT §8.3.3",
            rule_id=rule_id,
        )
    window = raw["window"]
    if window not in _WINDOWS:
        raise _fail(f"window 非法：{window!r}（允许 {sorted(_WINDOWS)}）", rule_id=rule_id)
    description = raw["description"]
    if not isinstance(description, str) or not description.strip():
        raise _fail("description 必须是非空字符串", rule_id=rule_id)

    when = compile_expression(raw["when"], allowed_names, rule_id=rule_id)

    raw_actions = raw.get("actions") or []
    if not isinstance(raw_actions, list):
        raise _fail("actions 必须是列表", rule_id=rule_id)
    actions = tuple(
        _parse_action(item, rule_id=rule_id, index=i) for i, item in enumerate(raw_actions)
    )
    return Rule(
        id=rule_id,
        enabled=enabled,
        severity=severity,
        metric=metric,
        when=when,
        window=window,
        description=description,
        actions=actions,
    )


def load_rules(path: str | Path | None = None) -> RuleSet:
    """加载并**完整校验** `rules.yaml`。任何问题都在这里抛 `RuleLoadError`（§8.1/§8.3.3）。"""
    # 契约 §8.3.3 要求的"加载期门禁"：命名空间被破坏时，这里就要报出来。
    # import 期已经验过一次；这里再验是为了让"改完 rules.py 调一次 load_rules 就能确认"
    # 成为可操作的步骤，而不是必须重新启动进程才能发现问题。
    assert_name_sets_disjoint()
    rules_path = Path(path) if path is not None else _DEFAULT_RULES_PATH
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail(f"无法读取规则文件：{exc}", source=str(rules_path)) from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _fail(f"YAML 解析失败：{exc}", source=str(rules_path)) from exc

    if not isinstance(document, dict):
        raise _fail("rules.yaml 顶层必须是映射", source=str(rules_path))
    unknown_top = set(document).difference(_TOP_LEVEL_ALLOWED_KEYS)
    if unknown_top:
        raise _fail(f"顶层含未知字段 {sorted(unknown_top)}", source=str(rules_path))
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        raise _fail("version 必须是非空字符串", source=str(rules_path))
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _fail("rules 必须是非空列表", source=str(rules_path))

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        rule = _parse_rule(raw_rule, index=index, allowed_names=ALLOWED_NAMES)
        if rule.id in seen:
            # 重复 id 会让 Anomaly.anomaly_id（sha256(run|rule_id|metric)）撞车，静默合并两条规则。
            raise _fail(f"规则 id 重复：{rule.id}", rule_id=rule.id, source=str(rules_path))
        seen.add(rule.id)
        rules.append(rule)
    return RuleSet(version=version, rules=tuple(rules), path=str(rules_path))


def build_namespace(metrics: Mapping[str, Any], **overrides: Any) -> dict[str, Any]:
    """把指标快照 + 可选派生量组装成求值命名空间。

    只放**命名空间允许的名字**（多放一个就相当于偷偷扩大白名单）。缺失的键不放进去，
    这样规则引用它时会得到 `RuleEvalError`（"名字缺失"），而不是被当成 None 静默 False。
    """
    namespace: dict[str, Any] = {
        name: metrics[name] for name in ALLOWED_NAMES if name in metrics
    }
    for name, value in overrides.items():
        if name not in ALLOWED_NAMES:
            raise RuleEvalError(f"派生量 {name!r} 不在命名空间内（CONTRACT §8.3.3 冻结）")
        namespace[name] = value
    return namespace


__all__ = [
    "ALLOWED_NAMES",
    "SHOP_NAMES",
    "SKU_NAMES",
    "CompiledExpr",
    "Rule",
    "RuleAction",
    "RuleEvalError",
    "RuleLoadError",
    "RuleSet",
    # 门禁必须能被外部显式调用：契约 §8.3.3 要求"改完 rules.py 调一次 load_rules 就能确认"，
    # 而 `import *` 或按名取用的调用方此前根本看不到这个函数（审查 F3）。
    "assert_name_sets_disjoint",
    "build_namespace",
    "compile_expression",
    "evaluate",
    "load_rules",
]
