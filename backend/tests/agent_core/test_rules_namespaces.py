"""§8.3.3 两个名字集合的互斥门禁（v1.5.0，ADR-021 A 条）。

为什么值得单独一组用例：这条门禁**平时看不见**（常量集合不重叠时它什么都不做），
只有到有人把 `gross_margin` 同时加进店铺级与 SKU 级时才会响。那种错误一旦落地，
后果不是报错而是**阈值悄悄换了含义**——为全店调的 `gross_margin < 0` 会被套到单品上，
把"薄利"判成"亏本"，然后 Agent 去给正常商品改价/下架。

所以这里钉三件事：
1. 现在两个集合确实不相交（防止改动时无意引入重叠）；
2. `ALLOWED_NAMES` 里每个名字都能归入某一轮（否则规则求值时轮次不确定）；
3. 门禁函数**真的会抛**——用一个临时构造的重叠集合验证，而不是只断言"现在没抛"。
"""

from __future__ import annotations

import pytest

from app.services.rules import (
    ALLOWED_NAMES,
    SHOP_NAMES,
    SKU_NAMES,
    RuleLoadError,
    assert_name_sets_disjoint,
)


def test_shop_and_sku_name_sets_are_disjoint() -> None:
    overlap = SHOP_NAMES & SKU_NAMES
    assert overlap == frozenset(), f"两集合重叠，轮次会变得不确定: {sorted(overlap)}"


def test_every_allowed_name_belongs_to_exactly_one_round() -> None:
    """`ALLOWED_NAMES` ⊆ `SHOP_NAMES ∪ SKU_NAMES`：不许有"哪轮都不属于"的名字。"""
    assert ALLOWED_NAMES <= (SHOP_NAMES | SKU_NAMES), (
        f"越界名字: {sorted(ALLOWED_NAMES - (SHOP_NAMES | SKU_NAMES))}"
    )


def test_shop_names_are_all_usable_today() -> None:
    """店铺级名字必须全部可用——少一个，`rules.yaml` 里的店铺规则就加载不了。"""
    assert SHOP_NAMES <= ALLOWED_NAMES, f"未启用: {sorted(SHOP_NAMES - ALLOWED_NAMES)}"


def test_gross_margin_belongs_to_shop_level_only() -> None:
    """命名硬规则：`gross_margin` 只属于店铺级；SKU 侧唯一合法名是 `sku_gross_margin`。"""
    assert "gross_margin" in SHOP_NAMES
    assert "gross_margin" not in SKU_NAMES
    assert "sku_gross_margin" in SKU_NAMES


def test_unimplemented_sku_names_are_not_enabled_yet() -> None:
    """SKU 侧 6 个名字仍属"已入册未实现"：写进规则会被白名单拒绝，而不是静默放行。"""
    unimplemented = SKU_NAMES - ALLOWED_NAMES
    assert unimplemented, "如果 SKU 轮已落地，这条断言与门禁注释都要同步更新"
    assert unimplemented & ALLOWED_NAMES == frozenset()


def test_guard_actually_raises_on_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """门禁必须**真的抛**，且报错要带交集名字并指向 §8.3.3 / ADR-021。

    用 monkeypatch 注入一个故意重叠的集合——只断言"调用不抛"是测不出门禁的
    （把函数体改成 `pass` 那种写法照样绿，这正是本仓反复抓到的"测试不敏感"）。
    """
    import app.services.rules as rules_module

    monkeypatch.setattr(rules_module, "SKU_NAMES", rules_module.SKU_NAMES | {"gross_margin"})
    with pytest.raises(RuleLoadError) as excinfo:
        rules_module.assert_name_sets_disjoint()

    message = str(excinfo.value)
    assert "gross_margin" in message, "报错必须点名交集里的名字"
    assert "8.3.3" in message and "ADR-021" in message, "报错必须指向条款与决策记录"


def test_guard_raises_when_an_allowed_name_belongs_to_no_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ALLOWED_NAMES` 里出现"哪轮都不属于"的名字必须抛。

    这条分支原先没有用例（独立审查 F7 指出）：规则引用的名字必须能决定求值轮次
    （店铺轮还是 SKU 轮），不属于任何一轮的名字会让 `detect_anomalies` 拿到一个
    没有轮次的指标——那种错误在运行期表现为"这条规则永远不触发"，极难排查。
    """
    import app.services.rules as rules_module

    monkeypatch.setattr(rules_module, "ALLOWED_NAMES", rules_module.ALLOWED_NAMES | {"bogus_metric"})
    with pytest.raises(RuleLoadError) as excinfo:
        rules_module.assert_name_sets_disjoint()

    message = str(excinfo.value)
    assert "bogus_metric" in message
    assert "8.3.3" in message


def test_guard_raises_when_a_shop_name_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """店铺级名字漏出 `ALLOWED_NAMES` 必须抛（否则店铺规则会被白名单拒绝加载）。"""
    import app.services.rules as rules_module

    monkeypatch.setattr(rules_module, "ALLOWED_NAMES", rules_module.ALLOWED_NAMES - {"gmv_cents"})
    with pytest.raises(RuleLoadError) as excinfo:
        rules_module.assert_name_sets_disjoint()

    message = str(excinfo.value)
    assert "gmv_cents" in message
    assert "8.3.3" in message
