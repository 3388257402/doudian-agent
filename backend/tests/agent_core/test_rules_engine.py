"""规则引擎：YAML + 白名单 AST 求值（契约 §8.3）。

这一组用例守的是"**在加载期炸，不在运行期沉默**"：

- 语法错误 / 未知字段 / 禁用节点（`Call`/`Attribute`/`Subscript`/推导式/dunder）全部在
  `load_rules` 抛 `RuleLoadError`，带 `rule_id` 与 `position`；
- 求值期只有"值层面的问题"（除零、`None` 参与数值比较、布尔参与算术、结果非 bool）
  抛 `RuleEvalError`，并且**只跳过那一条规则**，其它规则照常判定；
- 全仓没有 `eval` / `exec` / `compile` / `ast.literal_eval`。
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

import pytest

from app.services.anomalies import evaluate_rules
from app.services.rules import (
    ALLOWED_NAMES,
    RuleEvalError,
    RuleLoadError,
    compile_expression,
    load_rules,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
RULES_SOURCE = BACKEND_ROOT / "app" / "services" / "rules.py"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_HEAD = """
version: "1"
rules:
  - id: probe_rule
    enabled: true
    severity: warn
    metric: gmv_cents
    window: day
    description: "探针"
"""


def test_repo_rules_yaml_loads(repo_rules: object) -> None:
    """仓里的 `config/rules.yaml` 必须能加载（否则线上启动就没规则可用）。"""
    rules = repo_rules
    assert rules.version == "1"  # type: ignore[attr-defined]
    assert len(rules.rules) >= 3  # type: ignore[attr-defined]
    assert "gmv_drop_day" in {rule.id for rule in rules.rules}  # type: ignore[attr-defined]


def test_syntax_error_fails_at_load_with_position(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _HEAD + '    when: "gmv_cents < "\n',
    )
    with pytest.raises(RuleLoadError) as excinfo:
        load_rules(path)
    assert "语法错误" in str(excinfo.value)
    assert excinfo.value.rule_id == "probe_rule"
    assert excinfo.value.position  # 1:12 之类的行列位置，不是 None


def test_unknown_field_fails_at_load(tmp_path: Path) -> None:
    """拼错指标名不许"静默不触发"：这是最贵的静默失败（规则永远为 False 没人发现）。"""
    path = _write(tmp_path, _HEAD + '    when: "gmv_cent < 100"\n')
    with pytest.raises(RuleLoadError) as excinfo:
        load_rules(path)
    assert "gmv_cent" in str(excinfo.value)


def test_unknown_rule_key_fails_at_load(tmp_path: Path) -> None:
    path = _write(tmp_path, _HEAD + '    when: "gmv_cents > 0"\n    threshhold: 1\n')
    with pytest.raises(RuleLoadError) as excinfo:
        load_rules(path)
    assert "threshhold" in str(excinfo.value)


@pytest.mark.parametrize(
    "expression",
    [
        "abs(gmv_cents) > 0",  # Call
        "gmv_cents.real > 0",  # Attribute
        "gmv_cents[0] > 0",  # Subscript
        "[x for x in gmv_cents]",  # ListComp
        "(lambda: 1)()",  # Lambda + Call
        "{'a': 1}['a']",  # Dict + Subscript
        "f'{gmv_cents}' != ''",  # JoinedStr
        "__import__('os')",  # dunder + Call
        "(x := gmv_cents) > 0",  # NamedExpr
        "gmv_cents if uv else 0",  # IfExp
    ],
)
def test_forbidden_ast_nodes_rejected(expression: str) -> None:
    with pytest.raises(RuleLoadError):
        compile_expression(expression, ALLOWED_NAMES)


def test_allowed_names_is_frozen_and_rejects_extras() -> None:
    with pytest.raises(RuleLoadError):
        compile_expression("stock_days_ratio > 1", ALLOWED_NAMES)
    # 白名单里的名字可以用
    compiled = compile_expression("stock_days > 0 and is_on_sale", ALLOWED_NAMES)
    assert compiled.names == frozenset({"stock_days", "is_on_sale"})


def test_no_eval_exec_compile_in_rules_module() -> None:
    """静态检查：求值器是手写 AST 解释器，源码里不允许出现 eval/exec/compile 调用。

    注意**不能**按 token 名判断：`_Evaluator.eval` 这个合法方法名本身就含 `eval`。
    正确的判据是"有没有把它当**内置函数**调用/导入"——所以看 AST 里的 `ast.Call` 与 import。
    """
    source = RULES_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"eval", "exec", "compile", "literal_eval"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert forbidden.isdisjoint(called), f"出现内置危险调用：{forbidden & called}"

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {alias.name for alias in node.names}
    assert "builtins" not in imported
    assert forbidden.isdisjoint(imported), f"导入了危险内置：{forbidden & imported}"


def test_runtime_div_zero_skips_only_that_rule(tmp_path: Path) -> None:
    """除零只跳过该规则：另一条规则命中必须照常产出异常。"""
    path = _write(
        tmp_path,
        """
version: "1"
rules:
  - id: div_zero_rule
    enabled: true
    severity: warn
    metric: gmv_cents
    when: "gmv_cents / paid_order_count > 0"
    window: day
    description: "分母为 0 的规则"
  - id: refund_rule
    enabled: true
    severity: critical
    metric: refund_rate
    when: "refund_rate is not None and refund_rate > 0.08"
    window: day
    description: "退款率超过 8%"
""",
    )
    rules = load_rules(path)
    metrics = {
        "date": "2026-10-23",
        "gmv_cents": 100,
        "order_count": 1,
        "paid_order_count": 0,  # 除零
        "refund_order_count": 0,
        "refund_amount_cents": 0,
        "aov_cents": 0,
        "refund_rate": 0.5,
        "uv": None,
        "cvr": None,
        "gmv_7d_avg_cents": None,
        "computed_at": "2026-10-23T12:00:00Z",
    }
    evaluation = evaluate_rules(metrics=metrics, history=[], rules=rules, run_id="r1")
    assert [item["rule_id"] for item in evaluation.anomalies] == ["refund_rule"]
    assert [rule_id for rule_id, _ in evaluation.skipped] == ["div_zero_rule"]


def test_none_comparison_is_false_not_exception() -> None:
    """`gmv_7d_avg_cents` 为 None 时数值比较为 False（§8.3.3），`is None` 正常工作。"""
    compiled = compile_expression(
        "gmv_cents < gmv_7d_avg_cents * 0.7", ALLOWED_NAMES
    )
    assert compiled.evaluate({"gmv_cents": 100, "gmv_7d_avg_cents": None}) is False
    is_none = compile_expression("gmv_7d_avg_cents is None", ALLOWED_NAMES)
    assert is_none.evaluate({"gmv_7d_avg_cents": None}) is True


def test_missing_name_at_runtime_raises() -> None:
    """名字不在命名空间 = 调用方漏传，必须报错（"值缺失"与"名字缺失"是两件事）。"""
    compiled = compile_expression("gmv_cents > 0", ALLOWED_NAMES)
    with pytest.raises(RuleEvalError):
        compiled.evaluate({})


@pytest.mark.parametrize(
    "expression,namespace",
    [
        ("paid_order_count / 0 > 1", {"paid_order_count": 1}),  # 除零
        ("paid_order_count % 0 > 1", {"paid_order_count": 1}),  # 取模 0
        ("is_on_sale + 1 > 0", {"is_on_sale": True}),  # 布尔参与算术
        ("gmv_cents", {"gmv_cents": 1}),  # 结果不是 bool
        ('"x"', {"gmv_cents": 1}),  # 结果不是 bool
    ],
)
def test_runtime_eval_errors(expression: str, namespace: dict[str, object]) -> None:
    compiled = compile_expression(expression, ALLOWED_NAMES)
    with pytest.raises(RuleEvalError):
        compiled.evaluate(namespace)


def test_none_propagation_through_arithmetic() -> None:
    """缺值沿算术传播到比较阶段判 False，且**不进** skipped 列表（§8.3.3）。"""
    compiled = compile_expression("gmv_cents < gmv_7d_avg_cents * 0.7", ALLOWED_NAMES)
    assert compiled.evaluate({"gmv_cents": 100, "gmv_7d_avg_cents": None}) is False
    # 有值时照常比较
    assert compiled.evaluate({"gmv_cents": 100, "gmv_7d_avg_cents": 1000}) is True
    # 一元负号 + 组合条件也走同一条路
    negative = compile_expression(
        "gmv_delta_ratio is not None and -gmv_delta_ratio > 0.2", ALLOWED_NAMES
    )
    assert negative.evaluate({"gmv_delta_ratio": None}) is False
    assert negative.evaluate({"gmv_delta_ratio": -0.5}) is True


def test_nan_comparison_raises() -> None:
    """NaN 参与比较必须报错：NaN 的所有比较都是 False，"规则静默不触发"最难查。"""
    compiled = compile_expression("gmv_cents > 0", ALLOWED_NAMES)
    with pytest.raises(RuleEvalError):
        compiled.evaluate({"gmv_cents": float("nan")})
