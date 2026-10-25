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
#
# 另外两条约束也是刻意的，不要"顺手简化"：
# - 集合是 `frozenset`：`ALLOWED_NAMES` 会在 import 期算一次，之后**没有任何**合法路径能
#   在运行期往里加名字。可变集合意味着"某处 add 一个名字"就能绕过 §8.3.3 的冻结流程。
# - `ALLOWED_NAMES` 由 SHOP_NAMES ∪（4 个已实现的 SKU 名字）**推导**而来，不是手抄的第二份
#   清单。手抄清单会漂移（见 `anomalies._METRIC_FIELDS` 那次事故），而这里的推导关系让
#   "加了 SHOP_NAMES 却忘了 ALLOWED_NAMES"这种错在 import 期就被 `assert_name_sets_disjoint`
#   的 missing 检查抓住。
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
#:
#: "可用"与"冻结"是两个不同的问题，别混：
#: - 冻结（`SKU_NAMES` 有 10 个）= 契约允许将来实现的名字，写全是为了**门禁**能拦住越界；
#: - 可用（本集合）= 现在真的有值可算的名字，`load_rules` 只认这一份。
#: 所以一个规则写了 `stock_days`（在 SKU_NAMES 里、不在这里）会被加载期拒绝——那不是
#: 门禁漏了，而是"这个名字还没实现"，报错信息里给出的允许字段就是本集合。
ALLOWED_NAMES: frozenset[str] = frozenset(SHOP_NAMES) | frozenset(
    {"stock", "current_price_cents", "stock_days", "is_on_sale"}
)


# 这个门禁被调**两次**，两次各有职责（ADR-021）：
#   import 期（本文件底部）：抓"代码写错了"——两个常量集合被改重叠，进程根本不该起来；
#   load_rules 期：把契约 §8.3.3 的"加载期门禁"变成可操作步骤——运维改完 rules.py 后调一次
#   `load_rules` 就能确认，不必依赖 import 时机（进程已启动的情况下也能验）。
# 两次都是 O(1) 的集合运算，没有性能理由省掉任何一次。
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


# 四条"形状"门禁。都放在加载期，因为它们拦的都是"规则作者写错了"，而写错的规则最危险的
# 表现不是报错，是**永远不触发**（看板上什么都正常）或**永远触发**（Agent 一直改价）。
#
#   `_RULE_ID_RE`        2~64 字符、小写开头：id 进日志/`anomaly_id`/文件，必须可读且稳定；
#   `_SKU_TARGET_RE`     `sku:<id>` 的形状，字符集与 §0 的 ID 约定一致；
#   `_TARGET_SOURCES`    三个具名目标来源（与 `actions._TARGET_SOURCES` 必须一致）；
#   `_WINDOWS`           只有 day/7d：窗长决定 `Anomaly.window` 的取值域，多一个是契约变更。
_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SKU_TARGET_RE = re.compile(r"^sku:[A-Za-z0-9_-]{1,64}$")
_TARGET_SOURCES = frozenset({"top_gmv_sku", "top_refund_sku", "lowest_stock_sku"})
_WINDOWS = frozenset({"day", "7d"})

#: `backend/config/rules.yaml`。`parents[2]` = services → app → backend（见 `costs.BACKEND_ROOT`
#: 的同类注释）。相对 `Path(__file__)` 而不是进程 cwd：测试从 `backend/` 跑、从仓库根跑都对。
_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"


# --------------------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------------------


class RuleLoadError(Exception):
    """加载期错误（结构 / 语法 / 白名单 / 未知名字 / 未知字段）。

    `rule_id` / `position` / `source` 三个字段是给人和前端看的：一条"规则加载失败"必须能直接
    定位到"哪个文件的哪条规则的哪个字符"，否则只能靠通读 yaml。

    保证：`str(exc)` 里**一定有** message，其余三段按需追加（` | ` 分隔），所以日志里不会
    出现"只有 rule_id"的空报错。
    不保证：`position` 是 yaml 的行号——它是**表达式内部**的 `行:列`（`ast` 编译后重新计行），
    一条规则写成单行时它恒为 `1:x`。要和文件行号对上，只能靠 `rule_id` 去找。
    坑：三个字段都是**关键字参数**且都可缺省；`load_rules` 的顶层错误（读文件/YAML 语法）
    只有 `source` 没有 `rule_id`，别在前端假设 rule_id 一定存在。
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

    保证：`str(exc)` 带 `rule_id`（有的话），所以跳过原因能直接定位到规则。
    不保证：`position`（它是加载期字段）。运行期报错里的位置是**表达式内部**坐标，写在
    消息文本里（例如 `（位置 1:12）`），不是结构化字段。
    坑：捕获它的**只有** `anomalies.evaluate_rules`（跳过 + 记 skipped）。别在别处吞它：
    "这条规则算不出来"必须可见，否则一个写错的规则会在两周内安静地什么都不做。
    """

    def __init__(self, message: str, *, rule_id: str | None = None) -> None:
        self.message = message
        self.rule_id = rule_id
        super().__init__(f"{message} (rule_id={rule_id})" if rule_id else message)


# --------------------------------------------------------------------------------------
# AST 白名单（§8.3.2）
# --------------------------------------------------------------------------------------
# 这张表就是"规则表达式能做哪些事"的全部答案。它不是"够用就好"的清单，而是**攻击面清单**：
# 下表里每一个节点类型都是被逐个判定过"拿它绕不到执行"才留下的。三条判定标准：
#
# 1. 能不能**执行**任何东西？`Call` 不在表里，于是 `abs()` / `len()` / `__import__()` /
#    `eval()` 整类消失——不是"运行时拦住"，而是**语法上就不存在**（ADR-014）。
# 2. 能不能**摸到对象内部**？`Attribute` / `Subscript` 不在表里，于是 `x.__class__`、
#    `().__class__.__bases__`、`[][0]` 这一整类逃逸路径也不存在。它们比 `Call` 更危险：
#    一次属性访问就能拿到类对象，剩下的只是链式拼装。
# 3. 体积/时序上有没有副作用？`Lambda` / 推导式 / 生成器 / `Await` / `Yield` / `NamedExpr`
#    （海象）都不在表里：它们要么能建出闭包，要么能让"一次求值"变成惰性执行，要么能在
#    表达式里赋值——而这些都会让"纯函数、总终止"这个前提失效。
#
# `ast.Load` 为什么必须在表里：`Name` 节点的 `ctx` 本身就是 `Load` 实例，`ast.walk` 会走到它。
# 少了它，任何名字引用都会被判成"不在白名单内"——白名单立刻变成"什么都不能写"。
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

# 禁止节点的**人话解释**。它存在的理由只有一条：报错要能让规则作者当场改对。
# "不允许的语法节点 Call" 对一个只写 YAML 的运营没有信息量；"函数调用（本引擎没有
# abs()/len() 之类，见 ADR-014）" 才告诉他"你得换个写法，用 and/or 组合比较"。
# 所以这张表是**错误信息的一部分**，不是文档：改它等于改用户看到的报错文案。
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
    """把 AST 节点位置变成 `行:列`（列从 1 开始，给人看）。

    保证：永远返回非空字符串（拿不到 lineno 时退化成 `<expr>`），所以报错信息里不会出现
    "None:None" 这种既指不到位置、又看着像 bug 的片段。
    不保证：列号精确到"用户数到的那一列"——`col_offset` 是 0 基的字节偏移，这里 +1 转成
    1 基；单字节字符下就是直觉列号，多字节字符（中文）下会偏小。
    坑：这是**加载期**的位置（来自 `ast.parse` 的源码位置），不是运行期的；运行期报错拿到的
    位置是规则表达式内部的偏移，不是 rules.yaml 的行号。
    """
    line = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    if line is None:
        return "<expr>"
    return f"{line}:{(col or 0) + 1}"


def _is_dunder(name: str) -> bool:
    """名字是否"双下划线"（前后任意一侧是 `__`）。

    为什么白名单里既然没有 `Attribute`/`Subscript`、还要单独拦这个名字：两道闸的失效方式
    不同。禁止 `Attribute` 挡住的是"从对象摸出 `__class__`"；而 `Name` 是**允许**的
    （规则必须能引用指标名），于是 `__builtins__` / `__import__` 这类名字能作为一个裸
    `Name` 出现在表达式里。它们现在只会因为"不在 namespace"而报 `RuleEvalError`——但那是
    *命名空间*的偶然性质，不是语法层面的保证。这一层让"名字"这条路径也变成结构性拒绝。
    """
    return name.startswith("__") or name.endswith("__")


# 白名单的执行顺序（两闸的分工，别合并）：
#   ① 节点类型闸：`_ALLOWED_NODE_TYPE_SET` —— 挡"能执行/能摸对象"的语法（Call/Attribute/...）
#   ② 名字闸：`_is_dunder` + 后面的 `allowed_names` 检查 —— 挡`Name` 这条唯一还能进执行面的路
# 只做①：`__builtins__` 作为裸 `Name` 仍会被求值器看见（它只因"不在 namespace"才失败，
# 那是数据层的偶然性质）；只做②：`x.__class__` 根本不需要名字就能链出类型对象。
# 所以①必须在前（先排除整类语法），②在后覆盖剩下的唯一入口。
def _check_ast(tree: ast.Expression, *, source: str, rule_id: str | None) -> frozenset[str]:
    """逐节点检查白名单，返回表达式引用的名字集合。

    保证：① 只要有一个节点不在 `_ALLOWED_NODE_TYPES` 里就抛 `RuleLoadError`，报错里带
    禁止原因 + 位置 + 原文；② 返回的 `names` 是**完整**的名字集合（`ast.walk` 覆盖全树），
    所以调用方可以拿它一次性判"有没有未知字段"，不必再走一遍树。
    不保证：表达式的语义正确。`gmv_cents < gmv_7d_avg_cents` 能过检查，但单位/口径对不对
    是人和契约的事——静态检查看不出"拿分和元比较"。
    坑：常量类型限制到 `int/float/str/bool/None` 是**白名单**而不是黑名单（bytes/complex/
    Ellipsis 都会拦）：`complex` 会让 `<` 抛 TypeError，`bytes` 让 `in` 的行为变得反直觉，
    放进来只是把加载期的明确报错换成运行期的难查错误。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        node_type = type(node)
        if node_type not in _ALLOWED_NODE_TYPE_SET:
            reason = _FORBIDDEN_EXAMPLES.get(
                node_type, f"不在白名单内的语法节点 {node_type.__name__}"
            )
            # 先查"有名字解释"的表、再退化成通用文案：两条分支都带位置，所以即使遇到
            # 表里没有的新节点类型（Python 升级新增语法），报错仍然能定位到字符。
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
            # NaN/Inf 必须在**加载期**拦：它们参与比较永远 False/True（NaN 与任何值比较都是
            # False），规则会"静默永不触发"——正是本文件开头第 1 条硬约束要消灭的那类故障。
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
    """算术/一元运算的操作数校验：必须是有限实数（`bool` 不算）。

    保证：返回的就是入参本身（不做类型转换），所以调用方拿到的一定是"能参与算术"的值。
    不保证：值在业务上合理（负数库存之类）。这是类型边界，不是语义边界。
    坑：`bool` 被显式拒绝**在** `isinstance(value, int)` 之前——顺序颠倒的话 `True` 会被
    当成 `1` 参与算术（`is_on_sale + 1` 这种写法会算出 2 然后静默通过），而布尔参与金额
    运算一定是规则写错了。
    """
    if isinstance(value, bool):
        # §8.3.3：bool 不是 int，布尔参与算术没有业务意义 —— 显式拒绝而不是让它悄悄算成 1/0。
        # 唯一的例外场景是 `is_on_sale is True`（比较，不走这里），那是允许的。
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
    """算术**结果**的 NaN/Inf 检查（加载期查常量，这里查计算结果）。

    为什么两处都要查：常量干净不代表结果干净。`gmv_cents / 0` 走的是显式的除零分支，
    但 `1e308 * 10` 会静默溢出成 `inf`——它再参与 `>` 比较时结果永远是 True，规则会
    "永远触发"，这比永远不触发更危险（Agent 会一直改价）。
    保证：只做检查，不试图修正（不 clamp、不 replace 成 0）。
    坑：整型除法的结果也是 float，所以 `covered_cost / covered_revenue` 这类正常算式也会
    走到这里；被判为 NaN/Inf 时应当视为"这条规则今天算不出来"，由调用方跳过（`anomalies`
    就是这么做的），不要在这里兜底成 0。
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise RuleEvalError(f"结果出现 NaN/Inf（位置 {_position(node)}）", rule_id=rule_id)
    return value


class _Evaluator:
    """手写递归下降求值器（**不用** `eval` / `compile`，ADR-014）。

    保证：只处理"白名单里存在的节点类型"——方法是按节点类名派发（`_eval_<NodeName>`），
    白名单外没有对应方法就等于"求值不到"。所以新增一个允许的语法节点时，必须同时加
    白名单条目与 `_eval_*` 方法，两边缺一个都会在运行期抛 `RuleEvalError`（fail-loud）。
    不保证：这是解释器的**语义**必须与 Python 一致的地方之一——`and`/`or` 的返回值、
    `is` 的对象同一性、链式比较（`a < b < c`）都刻意照搬 Python 语义，规则作者看到的行为
    才和他们的直觉一致。
    坑：`namespace` 是**每次求值**传进来的快照，求值器自身不持有状态，因此可以安全复用
    （`anomalies._threshold_of` 就临时 new 了一个只用来取阈值的实例）。
    """

    def __init__(self, namespace: Mapping[str, Any], *, rule_id: str | None) -> None:
        self._ns = namespace
        self._rule_id = rule_id

    def eval(self, node: ast.AST) -> Any:
        """按节点类型派发到 `_eval_<Name>`；没有对应方法就是"这个节点不该走到求值期"。

        保证：派发只依赖 `type(node).__name__`，没有 if/elif 长链——加一个节点类型只加一个
        方法，不会漏改某个分支。
        不保证：`namespace` 里有这个值——缺值由 `_eval_Name` 报错，不走这里。
        坑：`getattr` 会顺带找到本类的其它方法（例如有人写 `_eval_()`），所以方法名约定
        必须以 `_eval_` 开头且与 AST 类名精确匹配，不要起同前缀的辅助方法。
        """
        method = getattr(self, f"_eval_{type(node).__name__}", None)
        if method is None:  # pragma: no cover - compile_expression 已挡住
            raise RuleEvalError(
                f"无法求值的节点类型 {type(node).__name__}", rule_id=self._rule_id
            )
        return method(node)

    # --- 表达式/字面量 ---
    def _eval_Expression(self, node: ast.Expression) -> Any:
        """模块入口：只求 `body` 一个子节点（`mode="eval"` 的固定形状）。"""
        return self.eval(node.body)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        """字面量原样返回（类型/NaN 已在加载期校验，这里不再判）。"""
        return node.value

    def _eval_List(self, node: ast.List) -> list[Any]:
        """列表字面量：逐项求值（`in` 的右侧用它，例如 `"sku_a" in [...]`）。"""
        return [self.eval(item) for item in node.elts]

    def _eval_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        """元组字面量：逐项求值（注意返回 **tuple** 而不是 list，`in` 的语义两者一致）。"""
        return tuple(self.eval(item) for item in node.elts)

    def _eval_Name(self, node: ast.Name) -> Any:
        """引用一个命名空间名字；缺名字是**错误**，不是 `None`、也不是 `False`。

        保证：返回值就是调用方放进 namespace 的那个对象（不做任何转换/倍率）。
        不保证：值是"今天有数据"的——`None` 是合法的命名空间值，缺值/缺名是两件事：
        前者会被比较运算判成 False（§8.3.3），后者会抛 `RuleEvalError`。
        坑：把缺名改成返回 None 会让"忘了传 uv"表现成"uv 规则永远不触发"，而界面上一切正常
        ——这正是加载期/求值期都要 fail-loud 的原因。
        """
        if node.id not in self._ns:
            # 名字不在 namespace 里 = 调用方没提供这个量。契约要求这是错误，不是 False：
            # 静默 False 会让"忘了传 uv"表现成"uv 规则永远不触发"。
            raise RuleEvalError(
                f"命名空间缺少字段 {node.id!r}（位置 {_position(node)}）", rule_id=self._rule_id
            )
        return self._ns[node.id]

    # --- 逻辑 ---
    def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        """`and`/`or`：**短路**求值，返回决定结果的那个操作数（照搬 Python 语义）。

        保证：短路是真的短路——后面的子表达式根本不会被求值，所以"前半段为 False 时
        后半段可能抛错"的组合（例如 `x is not None and x > 0`）是安全的写法。
        不保证：结果是 bool。`a and b` 的中间结果可以是任意值（例如 `0 and ...` 得到 `0`），
        最终结果是不是 bool 由 `CompiledExpr.evaluate` 在**整条表达式**上判。
        坑：如果哪个分支里出现除零，`and`/`or` 的短路性决定了它会不会被触发；把
        `a / b > 1 and b != 0` 写成这个顺序，`b == 0` 时仍会除零——顺序本身就承载语义。
        """
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
        """一元 `-` / `+` / `not`；操作数为 `None` 时返回 `None`（缺值传播）。

        保证：`not None` 是 **True**（Python 语义），而 `-None` 是 `None`（缺值传播，不抛错）。
        这个不对称是刻意的：`is not None` 是规则里最常见的写法，`not` 必须能处理 None。
        不保证：`-None` 的"缺值"会一路传播到比较阶段变 False —— 那正是契约要的行为，
        但它意味着"数据缺失"和"条件不成立"在结果上**不可区分**（详见 `_compare`）。
        坑：`-`/`+` 对 bool 会抛错（`_require_number` 拦的），所以 `-is_on_sale` 是加载期
        能过、运行期必炸的写法。
        """
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
        """四则运算（`+ - * / %`）；任一操作数为 `None` → 结果 `None`（缺值传播）。

        保证：除零/模零抛 `RuleEvalError`（而不是 Python 的 `ZeroDivisionError`），调用方
        只需处理一种异常；结果是 NaN/Inf 也抛（见 `_check_finite`）。
        不保证：单位一致。`gmv_cents * 0.7` 与 `gmv_cents * 70` 都能算出来，后者错得很安静。
        坑：`None` 判断在**两**个操作数上、且在类型检查之前——顺序颠倒会让"缺值"变成
        "操作数不是数值"的报错，误导成"上游字段类型错了"。
        """
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
            # `right == 0` 同时挡 `0` 与 `0.0`；`-0.0 == 0` 也成立，所以负零同样被拦。
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
        """比较（含**链式** `a < b < c`）：逐对比较、全部成立才 True。

        保证：链式比较里 `b` 只求值**一次**（Python 语义，`left = right` 复用中间结果）；
        任意一对不成立立刻返回 False（短路）。
        不保证：`None` 参与数值比较的结果——那一定返回 False（不是异常、也不是 True），
        所以"数据缺失"会让规则**不命中**，而调用方无法从结果里区分"没数据"和"条件不满足"。
        坑：链式比较与 `(a < b) and (b < c)` 在 `b` 有副作用/开销时不等价，但这里没有副作用，
        所以两种写法等价；真正要小心的是**链式比较的方向**（`a > b > c` 表示 b 同时小于 a、
        大于 c），写成 `a > b and c < b` 更容易读错。
        """
        left = self.eval(node.left)
        for op, comparator_node in zip(node.ops, node.comparators, strict=True):
            right = self.eval(comparator_node)
            if not self._compare(op, left, right, node=node):
                return False
            left = right
        return True

    def _compare(self, op: ast.cmpop, left: Any, right: Any, *, node: ast.AST) -> bool:
        """单对比较的实现（六种数值比较 + `in`/`is`）。

        保证：返回**严格的 bool**；类型不可比、`in` 右侧不是容器、NaN/Inf 参与比较，都抛
        `RuleEvalError` 而不是让 Python 的意外行为漏出去。
        不保证：跨类型比较的"合理性"（`"5" < 5` 会因 TypeError 变成 RuleEvalError，这是
        想要的；但 `"5" == 5` 只是 False，不会报错——相等比较永远不抛）。
        坑：`is`/`is not` 比的是**对象同一性**，对 int/str 常量来说等价于 `==` 是因为 CPython
        对小整数/驻留字符串的缓存，**不要**据此把 `is` 当 `==` 用；`x is not None` 是唯一
        可靠的用法，也是规则里出现 `is` 的真实理由。
        """
        if isinstance(op, (ast.Is, ast.IsNot)):
            same = left is right
            return same if isinstance(op, ast.Is) else not same
        if isinstance(op, (ast.In, ast.NotIn)):
            container = right
            # `bool` 也挡掉：`True in ...` 在 Python 里是合法的（True == 1），但规则作者写出来
            # 只可能是把"指标"和"容器"写反了，所以按右侧不是容器处理。
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
        # 这一行是"缺值 = 不命中"这条契约的**唯一**落点：放在 NaN 检查之前，是因为
        # `None > 0` 在 Python 里本来就抛 TypeError，而"缺数据"不该被当成求值失败。
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
        """对一份命名空间求值，返回**严格 bool**。

        保证：① 求值前先一次性检查**所有**缺失名字并报出完整列表（不是遇到第一个就停）——
        运维一次就能把缺的字段补齐；② 结果必须是 `bool`，`0`/空串/非空列表这类"真值"不算。
        不保证：`namespace` 里没有多余名字的检查。多传一个名字不会报错（`build_namespace`
        负责在构造端挡住越界），这里只关心"引用的名字在不在"。
        坑：结果非 bool 的报错路径是真实存在的——规则写成 `when: gmv_cents`（漏了比较）时，
        这里会抛 `RuleEvalError` 而不是把非零值当 True。别为了"宽容一点"改成 `bool(result)`，
        那等于把"规则写错了"变成"规则永远触发"。
        """
        if self._tree is None:
            raise RuleEvalError(
                "CompiledExpr 没有编译产物，请用 compile_expression() 构造", rule_id=self.rule_id
            )
        # 先查缺名、再求值：缺名时求值到一半才炸，报错里只有"第一个"缺失字段；这样能一次
        # 报全（`sorted` 保证信息稳定、可 diff、可断言）。
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
        """编译产物（`anomalies._threshold_of` 靠它做静态提取）。

        坑：可能为 `None`（手工构造的实例），调用方必须先判——`_threshold_of` 就是这么写的。
        """
        return self._tree


def compile_expression(
    src: str, allowed_names: frozenset[str] | set[str] | None = None, *, rule_id: str | None = None
) -> CompiledExpr:
    """编译并校验一个条件表达式。任何不合规都抛 :class:`RuleLoadError`。

    保证：返回的 `CompiledExpr` **一定**已经过四道检查（非空 / 语法 / AST 白名单+双下划线 /
    名字都在 `allowed_names` 内），所以运行期只需要处理"值"的问题，不需要再怀疑语法。
    不保证：运行期一定能算出 bool（缺值/除零/类型不可比仍会发生 → `RuleEvalError`）。
    坑：`allowed_names=None` 时用**全局** `ALLOWED_NAMES`；`load_rules` 传的是同一份，所以
    两者现在一致——但将来若做"按店铺裁剪字段"，必须显式传，别依赖默认值悄悄变化。
    """
    if not isinstance(src, str) or not src.strip():
        # 空表达式是最容易被"顺手放过"的一种：`ast.parse("", mode="eval")` 会抛 SyntaxError，
        # 报错文本是"invalid syntax"——不如直接说"条件表达式为空"。
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
        # 报"未定义字段"而不是"白名单外语法"：对规则作者来说，`uvv` 这种拼写错误最需要的是
        # "这个名字不在允许列表里"，而不是"某个 AST 节点被拒"。
        raise RuleLoadError(
            f"表达式引用了未定义的字段 {sorted(unknown)}；允许的字段见 CONTRACT §8.3.3",
            rule_id=rule_id,
            source=src,
        )
    return CompiledExpr(source=src, names=names, _tree=tree, rule_id=rule_id)


def evaluate(expr: CompiledExpr, namespace: Mapping[str, Any]) -> bool:
    """§8.3.3 的模块级求值入口（等价于 `expr.evaluate(namespace)`）。

    保证：行为与 `CompiledExpr.evaluate` 完全一致（这里只是薄包装，没有第二份逻辑）。
    不保证：类型容错——传一个"长得像 CompiledExpr"的鸭子类型会抛 `RuleEvalError`
    （不是 `AttributeError`），所以调用方可以只 catch 一种异常。
    坑：新增求值入口请一律走这里或 `CompiledExpr.evaluate`，不要在别处直接 new `_Evaluator`；
    绕过去就绕过了"缺名检查"和"结果必须是 bool"这两条契约（`anomalies._threshold_of` 是
    唯一被允许的例外，它只要取一个展示用的阈值、不产生判定结果）。
    """
    if not isinstance(expr, CompiledExpr):  # pragma: no cover - 类型误用防御
        raise RuleEvalError(f"evaluate 需要 CompiledExpr，收到 {type(expr).__name__}")
    return expr.evaluate(namespace)


# --------------------------------------------------------------------------------------
# RuleSet
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleAction:
    """一条规则命中后要发起的动作**模板**（还不是最终参数）。

    `params` 里存的是模板量（例如 `price_drop_pct: 3`），真正的 `price_cents` 要等
    `actions._expand_params` 拿目标商品算出来——这个分层不能合并：模板在加载期校验一次，
    最终参数要按"当天那个 SKU 的现价"重算，两件事的失败时机不同。
    `target_from` 允许三种具名来源或 `sku:<id>`；解析失败是**丢弃该动作**而不是整批失败。
    """

    type: ActionType
    target_from: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Rule:
    """一条已通过加载期全部校验的规则（求值只读这个对象，不再碰 YAML）。"""

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
    """一份 `rules.yaml` 的不可变快照（`version` 原样保留，不做解释/比较）。

    保证：`rules` 是 tuple，运行期没有人能往里面塞规则——"规则集在跑的过程中变了"是最难
    复现的一类不一致。
    不保证：`version` 的语义。它只被记录与展示，本文件不按版本做任何分支（那会让"改版本号"
    变成"改行为"）。
    """

    version: str
    rules: tuple[Rule, ...]
    path: str | None = None

    def get(self, rule_id: str) -> Rule:
        """按 id 取规则；取不到抛 **`KeyError`**（不是返回 None）。

        保证：O(n) 线性查找**够用**——规则集是个位数到几十条，且只在 proposer 里按异常数量
        调用；换成 dict 索引要额外维护一份"id 唯一"的保证，加载期已经用重复检查保证了。
        不保证：大小写/空白的容错。`"GMV_DROP_DAY"` 取不到就是取不到（规则 id 有字符集门禁）。
        坑：调用方（`actions.propose_actions_with_report`）把 KeyError 当作"异常来自另一份
        规则集"处理并记 dropped——那是刻意的降级，不要在这里替它编一个默认规则。
        """
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    @property
    def enabled_rules(self) -> tuple[Rule, ...]:
        """`enabled=True` 的规则（每次调用都重新过滤，规则集不可变所以结果稳定）。

        坑：`enabled: false` 的规则**不参与求值**，所以它写错了表达式也不会被发现——
        `load_rules` 仍然会校验它的语法/白名单（加载期检查不看 enabled）。
        """
        return tuple(rule for rule in self.rules if rule.enabled)


# 字段白名单（三层：顶层 / 规则 / 动作）。四张集合的分工说明一件重要的事：
# "未知字段"是**错误**而不是忽略——`enable: false`（少个 d）被静默忽略时，规则照样生效，
# 而作者以为自己关掉了它。这类事故只有"多一个键就报错"能拦住。
#
# `_RULE_REQUIRED_KEYS` ⊂ `_RULE_ALLOWED_KEYS`：差集就是 `actions`（可选，缺省 = 只告警）。
_RULE_REQUIRED_KEYS = frozenset(
    {"id", "enabled", "severity", "metric", "when", "window", "description"}
)
_RULE_ALLOWED_KEYS = _RULE_REQUIRED_KEYS | {"actions"}
# 动作的三个字段都是必填：`params` 空模板在展开期必然失败，`target_from` 缺了不知道改谁。
_ACTION_REQUIRED_KEYS = frozenset({"type", "target_from", "params"})
# 顶层只允许 version + rules。两种常见手误各有一条报错路径：把 `rules:` 拼成 `rule:` 会撞
# "顶层含未知字段"（直接指出拼错的名字）；`rules:` 写成空值则 `raw_rules` 为 None → 撞
# "rules 必须是非空列表"。两条都比"加载成功但一条规则都没有"好得多。
_TOP_LEVEL_ALLOWED_KEYS = frozenset({"version", "rules"})


def _fail(message: str, *, rule_id: str | None = None, position: str | None = None,
          source: str | None = None) -> RuleLoadError:
    """构造并返回 `RuleLoadError`（不抛）——调用点写 `raise _fail(...)`。

    为什么做成"返回异常"而不是"抛"：加载期有十几处 `raise _fail(...)`，其中几处要在
    `except` 里 `raise ... from exc`。返回式让每个调用点自己决定链不链因果，也让"哪里会
    失败"在 grep `_fail(` 时一目了然。
    """
    return RuleLoadError(message, rule_id=rule_id, position=position, source=source)


def _parse_action(raw: Any, *, rule_id: str, index: int) -> RuleAction:
    """校验并构造一条 `RuleAction`；`index` 只用于报错定位（`actions[0]` 这种）。

    保证：未知字段 **先**报、缺字段后报 —— 顺序是刻意的：字段名拼错（`target_form`）时
    两个检查都会命中，先报"含未知字段"能直接指出拼错的那个名字；反过来只报"缺 target_from"，
    作者得自己去找拼错的地方。
    不保证：`params` 的内容合法 —— action 模板的字段是**动作类型相关**的（`price_drop_pct`
    vs `stock`），要到 `actions._expand_params` 才有上下文校验，这里只要求"非空映射"。
    坑：`params` 必须非空：空模板能通过这一层，然后在展开期炸在"缺 price_drop_pct"上，
    报错会指向商品而不是规则——所以这里提前拦住。
    """
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
    # 两种合法形状：三个具名来源（`_TARGET_SOURCES`）或 `sku:<id>`。后者是本仓唯一能让规则
    # 直接指定目标的写法，字符集正则与 `sku_id` 的 ID 约定一致（`_SKU_TARGET_RE`）。
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
    # `dict(params)` 拷贝：YAML 解析出来的对象在 `RuleSet` 生命周期里可能被别处持有，
    # 拷一份让 frozen dataclass 里的 dict 至少不会被"外部改一个字段"影响到。
    return RuleAction(type=action_type, target_from=target_from, params=dict(params))


def _parse_rule(raw: Any, *, index: int, allowed_names: frozenset[str]) -> Rule:
    """校验一条规则并编译它的条件表达式。

    保证：字段校验顺序是固定的 —— 先 `id`（后面所有报错都要带它）、再未知字段、再缺字段、
    再逐个字段的取值域、最后才 `compile_expression`。这个顺序让报错信息永远"从最外层往里"：
    结构错了不会先报表达式，表达式错了也一定已经知道 rule_id。
    不保证：语义正确。`gmv_cents < gmv_7d_avg_cents * 0.7` 在结构上完美，口径错了照样过。
    坑：`metric` 必须在 `allowed_names` 里 —— 它决定 `Anomaly.metric` 和 `observed` 取哪个
    值域，不在集合里的话 `observed` 恒为 None（前端会显示 `observed vs None`）。
    """
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where} 必须是映射")
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not _RULE_ID_RE.match(rule_id):
        # id 的形状限制不是洁癖：它同时是日志字段、`anomaly_id` 的输入和文件里给人看的主键。
        raise _fail(f"{where}.id 非法：{rule_id!r}（须匹配 ^[a-z][a-z0-9_]{{1,63}}$）")
    unknown = set(raw).difference(_RULE_ALLOWED_KEYS)
    if unknown:
        raise _fail(f"未知字段 {sorted(unknown)}", rule_id=rule_id)
    missing = _RULE_REQUIRED_KEYS.difference(raw)
    if missing:
        raise _fail(f"缺字段 {sorted(missing)}", rule_id=rule_id)

    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        # YAML 1.1 里 `enabled: yes` 会被解析成 bool，`"false"`（带引号）却是字符串——
        # 后者是"看着关了其实开着"的经典事故，所以类型必须严格。
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
    # `window` 只有 day/7d：它决定 `Anomaly.window` 与前端展示口径，多一个值就是一次契约变更。
    if window not in _WINDOWS:
        raise _fail(f"window 非法：{window!r}（允许 {sorted(_WINDOWS)}）", rule_id=rule_id)
    description = raw["description"]
    if not isinstance(description, str) or not description.strip():
        # 空 description 会让 `Anomaly.message` 变成"（metric=...，条件 ...）"，人看不出
        # 这条异常到底在说什么——它是告警文案的唯一来源，必须非空。
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
    """加载并**完整校验** `rules.yaml`。任何问题都在这里抛 `RuleLoadError`（§8.1/§8.3.3）。

    保证：返回的 `RuleSet` 里每条规则都通过了结构/字段/语法/AST/名字五类检查，所以运行期
    只可能遇到"数据问题"（缺值、除零），不可能遇到"配置问题"。
    不保证：`version` 的语义，也不保证规则在业务上有意义 —— 这层只做形式校验。
    坑：① `path=None` 时用 `config/rules.yaml`（相对 `backend/`），测试要传 tmp 路径；
    ② 这个函数**不缓存**：每次调用都重读文件 + 重新编译。缓存是装配层的事（谁需要谁自己
    持有 `RuleSet`），在这里加缓存会让"改了 yaml 却还是旧规则"变成一个静默故障。
    """
    # 契约 §8.3.3 要求的"加载期门禁"：命名空间被破坏时，这里就要报出来。
    # import 期已经验过一次；这里再验是为了让"改完 rules.py 调一次 load_rules 就能确认"
    # 成为可操作的步骤，而不是必须重新启动进程才能发现问题。
    assert_name_sets_disjoint()
    rules_path = Path(path) if path is not None else _DEFAULT_RULES_PATH
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        # 读不到文件也包成 RuleLoadError（不是 FileNotFoundError）：加载期只有一种失败类型，
        # 调用方/装配层只需要 catch 一种。
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
        # 空规则集不是"合法但没内容"：它意味着一次都不会报警，而看板上一切正常。
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

    保证：① 输出里只有 `ALLOWED_NAMES` 里的键——调用方（节点）没法顺手把"临时调试量"塞进
    规则可见域；② 缺失的指标**不出现在**结果里（不是 `None`），所以缺名/缺值在行为上可区分。
    不保证：派生量的正确性。环比之类由调用方算好当 `overrides` 传进来（见
    `anomalies._derived_namespace`：`gmv_delta_ratio` = (今日 - 上一可比日)/上一可比日，
    `refund_rate_delta` = 两个绝对率的差，不是比值）；本函数只负责"名字合不合法"。
    坑：`overrides` 里出现一个不在 `ALLOWED_NAMES` 的名字会抛 `RuleEvalError`（不是
    `RuleLoadError`）——因为这是**运行期**发现的越界，加载期工具已经过了。新增派生量必须先
    走契约（§8.3.3 冻结集合），不要在调用点临时加名字把这个检查撞红。
    """
    # 只按 `ALLOWED_NAMES` 过滤 `metrics`（而不是 `dict(metrics)` 全量拷贝）：全量拷贝会把
    # `date`/`computed_at` 这类非指标字段也带进命名空间，规则就能引用到"没在契约里"的字段。
    namespace: dict[str, Any] = {
        name: metrics[name] for name in ALLOWED_NAMES if name in metrics
    }
    for name, value in overrides.items():
        if name not in ALLOWED_NAMES:
            raise RuleEvalError(f"派生量 {name!r} 不在命名空间内（CONTRACT §8.3.3 冻结）")
        # 派生量**可以**覆盖同名指标（后写赢）：调用方显式传进来就是"以它为准"，
        # 这样"用历史重算一个 gmv_cents"这类测试不需要再拼一份 metrics。
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
