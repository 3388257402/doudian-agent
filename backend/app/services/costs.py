"""SKU 成本表加载（`config/sku_costs.yaml`，CONTRACT §8.5）。

为什么成本是**配置文件**而不是表（§8.5 已论证，这里只留最关键的一句）：
抖店开放平台**没有**成本字段。成本是商家自维护、随部署走、要能 code review 的数据；
放进库里等于为一张几乎不变的映射表付"多一张表 + 多一次迁移"的代价。

三条硬规矩：

1. **加载期完成全部校验**：结构 / 类型 / 取值域 / 重复键。任何一条不合格都是 `ConfigError`
   （与 `app.config` 同一个异常类型），进程不该起来。留到运行期才发现时，它只会表现为
   "毛利率算得不对"——那已经是在拿错误的数做改价决策了。
2. **文件缺失不是错误**：没配成本 = "我们不知道成本"，不是"成本为 0"。记一条 warn（结构化
   日志，可被日志页看见）后返回空表，于是 `gross_margin` 为 `None`、`loss_making` 不触发。
   宁可漏报（可见、可补配置），不可误报（Agent 主动给好货降价/下架）。
3. **缺失 ≠ 0**：空值（`"sku":` 后面什么都不写 → `None`）是**写法错误** → `ConfigError`，
   绝不能读成 0 成本。0 成本会把毛利率算成 100%，而它的反方向（缺省当 0 参与比较）会直接把
   "没配成本"判成"亏本"——这是本仓反复强调的"最贵的一次误报"。

为什么重复键必须自己检测：`yaml.safe_load` 对重复键**静默取后者**，于是一份手写的成本表里
同一个 sku 出现两次时，前一行（可能是财务核对的正确值）被无声覆盖。这类错误在量小的配置里
没人会发现，所以只能在这里拦住（:class:`_UniqueKeyLoader`）。

`<<` 合并键**不是**无条件支持的（v1.5.1 实测更正，详见 :func:`_construct_unique_mapping`）：
合并块只提供**新增**键时可用；一旦与显式键同名，就按重复键报 `ConfigError`，而不是按 YAML
语义让显式键胜出——"同一个成本有两处来源"正是上面那条要防的事故。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from app.errors import ConfigError

__all__ = [
    "DEFAULT_SKU_COSTS_PATH",
    "SkuCostConfig",
    "default_sku_costs_path",
    "get_sku_costs",
    "load_sku_cost_config",
    "load_sku_costs",
    "parse_sku_costs",
    "reset_sku_costs_cache",
]

logger = logging.getLogger("app.services.costs")

#: `backend/` 目录（`app/services/costs.py` 的上三级：services → app → backend）。
BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: 默认成本表位置（与 `settings.yaml` / `rules.yaml` 同目录，§0.1 的 agent-core 归属）
DEFAULT_SKU_COSTS_PATH = BACKEND_ROOT / "config" / "sku_costs.yaml"

#: sku_id 的字符集（§0 的 ID 约定）。成本表的键必须能在 `raw.products` 里对上，
#: 所以字符集与上游 sku_id 保持同一套；对不上的写法在加载期就拒收。
_SKU_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 顶层允许的字段（多一个都是 typo：写成 `cost:` 时静默忽略会让成本表看起来"没生效")
_TOP_LEVEL_KEYS = frozenset({"version", "costs", "effective_from"})

#: YAML 的 `<<` 合并键标签（PyYAML 在 `flatten_mapping` 里把它展开成真正的键值对）
_MERGE_TAG = "tag:yaml.org,2002:merge"


@dataclass(frozen=True, slots=True)
class SkuCostConfig:
    """一次加载的结果：`sku_id -> 成本（整数分）` + 可选的生效日期。

    `effective_from` 目前只做**校验与展示**（§8.5 明确"热更新不支持：改文件要重启"），
    不参与计算——按时间生效的成本需要 `sku_cost_history` 表，那是另一次契约变更。
    """

    costs: dict[str, int]
    effective_from: str | None
    source: str

    def as_table(self) -> dict[str, int]:
        """给 `compute_metrics(cost_table=...)` 用的裸映射（拷贝：调用方改了不该污染缓存）。"""
        return dict(self.costs)


# --------------------------------------------------------------------------------------
# YAML 加载：重复键一律报错
# --------------------------------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` + 重复键检测（`yaml.safe_load` 默认静默取后者）。"""

    #: 出错信息里要带上的文件路径（由 :func:`load_sku_cost_config` 赋值）
    source: str = "<sku_costs>"


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode
) -> dict[Any, Any]:
    """逐键构造映射；重复键 → `ConfigError`（而不是静默覆盖）。

    先 `flatten_mapping` 把 `<<` 合并键展开成真正的键值对（PyYAML 的既有行为，无合并时是
    no-op）。

    `<<` 合并的**实际**行为（v1.5.1 实测更正；旧注释写成"合并产生的覆盖是允许的"，与事实不符）：

    - 合并块里的键**不**与显式键冲突 → 正常生效：`costs: {<<: {sku_a: 100}, sku_b: 200}`
      解析成 `{sku_a: 100, sku_b: 200}`；
    - 合并块里的键**与**显式键同名 → `ConfigError`，**不是**按 YAML 语义让显式键胜出。
      原因：PyYAML 6 的 `flatten_mapping` 把合并来的键**前置**进 `node.value` 且不按显式键
      去重，同一个键于是在展开后的列表里出现两次，本函数的重复键检测会拦住它。

    为什么不去实现"显式键胜出"：这张表是扁平的 `sku_id -> 整数分`，`<<` 在这里唯一能做的事
    就是**让某条成本的来源变成两处**——而"成本被无声覆盖 / 来源不可追"正是本文件开头第 3 条
    硬规矩要防的事故（财务核对过的旧值被一份抄来的块盖掉，没人会发现）。所以维持"冲突即
    报错"，并把原因写进错误信息（下面的 `has_merge`），而不是让人对着"重复键"猜自己哪儿写了两次。
    """
    has_merge = any(key_node.tag == _MERGE_TAG for key_node, _ in node.value)
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            seen_before = key in mapping
        except TypeError as exc:  # 不可哈希的键（list/dict）不是合法映射键
            raise ConfigError(
                f"{loader.source}: YAML 映射键必须是标量，收到 {type(key).__name__}"
            ) from exc
        if seen_before:
            merge_hint = (
                "；本次映射里有 `<<` 合并键：合并块与显式键同名时也会走到这里——"
                "本仓不支持用合并覆盖显式成本（同一个成本出现两处来源就等于来源不可追）"
                if has_merge
                else ""
            )
            raise ConfigError(
                f"{loader.source}: 映射出现重复键 {key!r}"
                "（PyYAML 默认静默取后者，这里必须拦住——成本被无声覆盖比缺成本更危险）"
                f"{merge_hint}"
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor("tag:yaml.org,2002:map", _construct_unique_mapping)


# --------------------------------------------------------------------------------------
# 纯校验（不碰 I/O，测试可以直接喂字典）
# --------------------------------------------------------------------------------------


def _fail(source: str, message: str) -> ConfigError:
    return ConfigError(f"{source}: {message}")


def _check_effective_from(value: Any, *, source: str) -> str:
    """`effective_from` 只接受 `YYYY-MM-DD`（业务日期口径，§0）或完整 ISO-8601 时间戳。"""
    if not isinstance(value, str) or not value.strip():
        raise _fail(source, f"effective_from 必须是非空字符串（YYYY-MM-DD），收到 {value!r}")
    text = value.strip()
    try:
        date.fromisoformat(text)
    except ValueError:
        try:
            datetime.fromisoformat(text)
        except ValueError as exc:
            raise _fail(
                source, f"effective_from 不是合法的 ISO-8601 日期：{value!r}"
            ) from exc
    return text


def parse_sku_costs(document: Any, *, source: str = "<sku_costs>") -> SkuCostConfig:
    """校验一份已解析的 YAML 文档，返回 :class:`SkuCostConfig`。

    校验项（任何一项失败都是 `ConfigError`，不带默认值、不降级）：

    - 顶层必须是映射，且只允许 `version` / `costs` / `effective_from`；
    - `costs` 必须存在且是映射；
    - 键必须是符合 §0 字符集的 `sku_id` 字符串（写成整数 `3400...` 也拒收：
      上游 `raw.products[i]["sku_id"]` 是字符串，类型不一致会让 `cost_table.get()` 永远取不到）；
    - 值必须是 `int`（`bool` 不算 int，float/字符串/`None` 全拒），且 `> 0`。

    重复 `sku_id` 的检测**只可能在 YAML 加载层**（:class:`_UniqueKeyLoader`）：一个 Python
    dict 构造完成时重复键已经被合并，"有没有重复"这个信息已经不存在。所以直接传字典的调用方
    由调用方自己保证不重复（本仓唯一的写入路径是文件）。
    """
    if document is None:
        raise _fail(
            source,
            "成本表是空的：要么删掉该文件表示『没有成本』，要么写 costs: {…}",
        )
    if not isinstance(document, Mapping):
        raise _fail(source, f"顶层必须是映射，收到 {type(document).__name__}")

    unknown = set(document).difference(_TOP_LEVEL_KEYS)
    if unknown:
        raise _fail(source, f"顶层含未知字段 {sorted(unknown)}（允许 {sorted(_TOP_LEVEL_KEYS)}）")

    version = document.get("version")
    if version is not None and (not isinstance(version, str) or not version.strip()):
        raise _fail(source, f"version 必须是非空字符串，收到 {version!r}")

    effective_raw = document.get("effective_from")
    effective_from = (
        None if effective_raw is None else _check_effective_from(effective_raw, source=source)
    )

    raw_costs = document.get("costs")
    if raw_costs is None:
        raise _fail(
            source,
            "缺少 costs 段；『没有成本』的正确写法是删掉整个文件，"
            "不要用一个空值把它伪装成『配置过但没内容』",
        )
    if not isinstance(raw_costs, Mapping):
        raise _fail(source, f"costs 必须是映射（sku_id -> 整数分），收到 {type(raw_costs).__name__}")

    costs: dict[str, int] = {}
    for sku_id, value in raw_costs.items():
        if not isinstance(sku_id, str) or not _SKU_ID_RE.match(sku_id):
            raise _fail(
                source,
                f"costs 的键必须是符合 ID 字符集 ^[A-Za-z0-9_-]{{1,64}}$ 的 sku_id 字符串，"
                f"收到 {sku_id!r}（YAML 里请加引号，例如 \"3400000000000000002\"）",
            )
        if sku_id in costs:
            raise _fail(source, f"costs 出现重复 sku_id {sku_id!r}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(
                source,
                f"costs[{sku_id}] 必须是整数分 int（bool/float/字符串都不行），收到 {value!r}",
            )
        if value <= 0:
            # 0 与"没配"在数值上都会让毛利率失真，方向相反且都能骗过阈值判断，所以一律拒收。
            raise _fail(
                source,
                f"costs[{sku_id}] 必须 > 0（整数分），收到 {value}；"
                "成本为 0 会把毛利率算成 100%，而『没配成本』应当直接不写这个 sku",
            )
        costs[sku_id] = value

    return SkuCostConfig(costs=costs, effective_from=effective_from, source=source)


# --------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------


def load_sku_cost_config(path: str | Path | None = None) -> SkuCostConfig:
    """读并校验成本表。**文件缺失 → 空表 + warn**；内容有问题 → `ConfigError`。

    这两条刻意不对称（§8.5）：缺失是"这个商家还没配成本"，属于正常状态；内容错误是
    "有人写坏了"，必须当场失败。把两者都当错误会让第一次部署起不来；都当正常则会让
    typo 静默生效——都不可接受。
    """
    target = Path(path) if path is not None else DEFAULT_SKU_COSTS_PATH
    source = str(target)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "没有成本表，毛利率相关判断将一律缺失（gross_margin=None，loss_making 不触发）：%s",
            target,
            extra={"event": "config.sku_costs_missing", "data": {"path": source}},
        )
        return SkuCostConfig(costs={}, effective_from=None, source=source)
    except UnicodeDecodeError as exc:
        # 不是 UTF-8（例如财务把表另存成了 GBK）：绝不"尽力而为"地读一半——读错的成本
        # 会被当成真成本参与"是否亏本"的判断，比没有成本危险得多。
        raise _fail(source, f"成本表不是合法的 UTF-8 文本：{exc}") from exc
    except OSError as exc:
        raise _fail(source, f"读取成本表失败：{exc}") from exc

    loader = _UniqueKeyLoader(text)
    loader.source = source
    try:
        document = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise _fail(source, f"YAML 解析失败：{exc}") from exc
    finally:
        loader.dispose()

    return parse_sku_costs(document, source=source)


def load_sku_costs(path: str | Path | None = None) -> dict[str, int]:
    """便捷入口：只要 `sku_id -> 成本` 映射（`compute_metrics` 的入参形状）。"""
    return load_sku_cost_config(path).as_table()


def default_sku_costs_path() -> Path:
    return DEFAULT_SKU_COSTS_PATH


_sku_costs: dict[str, int] | None = None


def get_sku_costs() -> dict[str, int]:
    """进程级缓存（`app.main` lifespan 先加载一次，节点直接取）。

    为什么缓存而**不支持热更新**（§8.5）：成本变化要留下"哪次 run 用的是哪版成本"的审计
    线索；背景重载会让同一个进程内前后两次 run 用不同成本，却没有任何记录说明分界点。
    改成本 = 改文件 + 重启，这正好是一次 code review 的机会。
    """
    global _sku_costs
    if _sku_costs is None:
        _sku_costs = load_sku_costs()
    return dict(_sku_costs)


def reset_sku_costs_cache() -> None:
    """测试用：清掉缓存（`load_settings` 那边的 `reset_settings_cache` 同款）。"""
    global _sku_costs
    _sku_costs = None
