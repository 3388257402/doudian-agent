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

本文件的"N 个 None 与 0 的区别"清单（这些是本仓最贵的一类误报的来源，改动前逐条对一遍）：

| 输入状态 | `SkuCostConfig.costs` | 下游看到什么 |
| --- | --- | --- |
| 文件不存在 | `{}` + 一条 warn | `gross_margin=None`、`loss_making` 不触发（"不知道"） |
| `costs: {}` / 缺 `costs` 段 | `ConfigError` | 进程不该起来（"写了但空"= 配置错误） |
| 某 sku 值为 0 / 负 | `ConfigError` | 同上（0 成本会把毛利率算成 100%） |
| 某 sku 没出现在表里 | 该 sku 不进分子分母 | 覆盖率下降（`cost_coverage_ratio`），毛利率只覆盖一部分成交额 |

"文件不存在"与"内容为空"必须**不同处置**：前者是"商家还没配成本"（正常状态），后者是
"有人写了 but 删了内容"（配置事故）。把两者都当正常 → typo 静默生效；都当错误 → 第一次
部署起不来。
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
#:
#: 为什么允许 `-` 与 `_`：抖店 sku_id 是纯数字串，但本仓的演示/夹具数据里有 `sku-a` 这类
#: 可读 id；两者共用一套正则，避免"夹具能跑、真实 id 被拒"这种只在切换环境时爆炸的差异。
_SKU_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 顶层允许的字段（多一个都是 typo：写成 `cost:` 时静默忽略会让成本表看起来"没生效")
#:
#: 三个字段的分工：`version` 只记录不解释；`costs` 是主体；`effective_from` 只校验 + 展示
#: （见 `SkuCostConfig`）。**未知顶层键一律报错**——这是"配置没生效但没人知道"的唯一防线。
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
        """给 `compute_metrics(cost_table=...)` 用的裸映射（拷贝：调用方改了不该污染缓存）。

        保证：返回**副本**；`get_sku_costs()` 返回的也是副本（两层各拷一次，开销可忽略——
        表是几十行）。
        不保证：`effective_from` 已经生效——本函数不做日期判断（§8.5 明确不支持按时间生效）。
        坑：不要在调用点把这个 dict 长期持有并当作"当前成本"的真相：改文件 + 重启才会变。
        """
        return dict(self.costs)


# 三种 YAML 事故与各自在哪一层被拦（这是本文件最值得先读的一段）：
#
#   ① 重复键      `costs: {sku_a: 100, sku_a: 200}`  → PyYAML **静默取后者**，
#                所以必须靠 `_UniqueKeyLoader`（纯 dict 层已经看不到重复）→ ConfigError
#   ② 单位/类型   `costs: {sku_a: 99.0}`（元、float） → `parse_sku_costs` 的类型检查 → ConfigError
#   ③ 静默失效    `costs: {sku_a: 100}` 上写成 `cost:` → 顶层未知字段检查 → ConfigError
#
# 三者共同点：**都不能靠"看结果对不对"发现**。毛利率会算出一个数，只是那个数是错的；
# 所以全部前置到加载期，宁可进程起不来。
# --------------------------------------------------------------------------------------
# YAML 加载：重复键一律报错
# --------------------------------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` + 重复键检测（`yaml.safe_load` 默认静默取后者）。

    继承 `SafeLoader`（不是 `Loader`）：成本表是**数据**，不需要构造任意 Python 对象；
    用 `Loader` 等于把 `!!python/object/apply:os.system` 这类 payload 请进来。
    坑：`source` 是**类属性**，由 `load_sku_cost_config` 在实例上赋值；同时加载两份文件时
    必须各用各的 loader 实例（本文件的唯一入口就是这么做的），否则报错会指错文件。
    """

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

    实现细节：`has_merge` 必须在 `flatten_mapping` **之前**算——展开会把 merge 标签节点换成
    真正的键值对，之后再扫 `node.tag` 就永远看不到合并键了，`merge_hint` 也就永远不出现
    （用户只会看到一句"重复键"，而不知道自己用了 `<<`）。
    """
    has_merge = any(key_node.tag == _MERGE_TAG for key_node, _ in node.value)
    # `flatten_mapping` 必须在**取键之前**调用：它把 `<<` 展开进 `node.value`，之后逐键构造才
    # 看得到合并来的键。放在这里（而不是构造函数外面）是因为 `has_merge` 要先扫原始节点标签。
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            seen_before = key in mapping
        except TypeError as exc:  # 不可哈希的键（list/dict）不是合法映射键
            # `key in dict` 对不可哈希的键抛 TypeError，包成 ConfigError 是为了让"YAML 里写了
            # 一个列表当键"这种错也走同一种异常类型（加载期只有一种失败类型）。
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
    """构造 `ConfigError`（不抛）——调用点写 `raise _fail(...)`。

    把 `source` 放在消息最前面是刻意的：这个模块的报错会出现在启动日志里，而"哪个文件"是
    运维第一个要找的信息。
    """
    return ConfigError(f"{source}: {message}")


def _check_effective_from(value: Any, *, source: str) -> str:
    """`effective_from` 只接受 `YYYY-MM-DD`（业务日期口径，§0）或完整 ISO-8601 时间戳。

    保证：返回 `strip()` 之后的字符串（去首尾空格），所以 `" 2026-10-24 "` 也能过。
    不保证：与业务日期的时区一致性——本字段**不参与计算**，只做展示（见 `SkuCostConfig`）。
    坑：先 `date.fromisoformat` 再 `datetime.fromisoformat`：前者的合法集合是后者的子集，
    反过来写会让 `2026-10-24` 也走"完整时间戳"的解析路径（能过，但错误信息会变成
    "不是合法的 ISO-8601 日期"之外的另一种措辞，且未来收紧 datetime 格式时会连带拒掉日期）。
    """
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

    保证：返回值里的 `costs` 是**新建** dict（不与入参共享），所以调用方改 `document` 不会
    污染结果。
    不保证：`effective_from` 与今天的先后关系（过去/未来都接受）。本仓不支持按时间生效，
    这个字段只做校验与展示（见 `SkuCostConfig`）。
    坑：`costs: {}` 是**合法**的（空表 = 明确"没有要配的成本"），而 `costs:`（空值）不是。
    两者在 YAML 里只差一对花括号，报错文案已经写清了这个区别——不要为了"宽容"把它们合并成
    "都当没有成本"，那会让"手滑删了内容"变成静默事故。
    """
    if document is None:
        # 空文件 / 只有注释：这不是"没有成本"（那应该删掉文件），而是"写了但没内容"。
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
        # `version` 可缺；给了就必须是非空字符串。"1" 与 1 的差别在这里会被拦下，
        # 因为 YAML 里 `version: 1` 解析成 int，而本仓其它配置的 version 都是字符串。
        raise _fail(source, f"version 必须是非空字符串，收到 {version!r}")

    effective_raw = document.get("effective_from")
    effective_from = (
        None if effective_raw is None else _check_effective_from(effective_raw, source=source)
    )

    raw_costs = document.get("costs")
    if raw_costs is None:
        # 明确区分"缺段"与"空段"：`costs:` 后面什么都不写 → None，就是这里；
        # `costs: {}` → 空 dict，会正常通过（"我确实没有要配的成本"是合法状态）。
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
            # 整数 sku（YAML 里没加引号）会解析成 int，报错信息里直接给出"加引号"的写法——
            # 这是实际发生过的第一号错误，比"键非法"有用得多。
            raise _fail(
                source,
                f"costs 的键必须是符合 ID 字符集 ^[A-Za-z0-9_-]{{1,64}}$ 的 sku_id 字符串，"
                f"收到 {sku_id!r}（YAML 里请加引号，例如 \"3400000000000000002\"）",
            )
        if sku_id in costs:
            # 直接传 dict 的调用方（跳过 YAML 层）在这里才可能重复：Python dict 构造完成时
            # 重复键已被合并，所以这条检查只对"手工拼的 dict"有意义，但成本为零。
            raise _fail(source, f"costs 出现重复 sku_id {sku_id!r}")
        if isinstance(value, bool) or not isinstance(value, int):
            # bool 必须显式排除（`True` 是 int 的子类），否则 `costs: {sku: true}` 会被读成 1 分。
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


# 两条失败语义在这里分岔（§8.5），改动前确认没有把它们合并：
#
#   文件不存在  → 空表 + 一条 `config.sku_costs_missing` warn（正常状态：还没配成本）
#   内容有问题  → ConfigError（事故：有人写坏了，进程不该起来）
#
# 为什么不统一：都当正常 → typo 静默生效（毛利率算错但看板正常）；都当错误 → 第一次部署
# 起不来。代价不对称，所以处置也必须不对称。
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
        # 五个分支里**只有这一个**是"正常状态"：商家还没配成本 → 空表 + warn。
        # 注意这里返回的是 `{}` 而不是 None：下游 `compute_metrics(cost_table={})` 等价于
        # "没有成本"，两个成本字段会是 None（不是 0）。
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
        # 权限/是目录/路径太长等：全部是"配置或部署问题"，启动期就该失败。
        raise _fail(source, f"读取成本表失败：{exc}") from exc

    # 不用 `yaml.safe_load`：那会走默认的 mapping 构造器，重复键被静默覆盖。
    loader = _UniqueKeyLoader(text)
    loader.source = source
    try:
        document = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise _fail(source, f"YAML 解析失败：{exc}") from exc
    finally:
        # `dispose` 必须执行：构造函数里可能注册了要清理的状态（PyYAML 的约定），
        # 漏掉不会立刻出错，但会在长时间运行的进程里积累。
        loader.dispose()

    return parse_sku_costs(document, source=source)


def load_sku_costs(path: str | Path | None = None) -> dict[str, int]:
    """便捷入口：只要 `sku_id -> 成本` 映射（`compute_metrics` 的入参形状）。

    保证：与 `load_sku_cost_config(path).as_table()` 完全等价（含"文件缺失 → 空表 + warn"）。
    不保证：缓存 —— 每次调用都重读文件。需要缓存的走 `get_sku_costs()`。
    坑：它返回的是**新 dict**，但读文件这一侧的失败语义与 config 版完全一样（内容错 →
    `ConfigError`）。不要在测试里用它来"大概看看能不能读"，那条路径会真的去读默认路径。
    """
    return load_sku_cost_config(path).as_table()


def default_sku_costs_path() -> Path:
    """默认成本表路径（`backend/config/sku_costs.yaml`）。

    坑：**不检查存在性**。要判断"配没配成本"，看 `load_sku_cost_config` 返回的 `costs` 是否
    为空（或看那条 `config.sku_costs_missing` warn），不要用 `path.exists()`——两条判断会
    各自演化，最终出现"文件在但读空"的分歧。
    """
    return DEFAULT_SKU_COSTS_PATH


_sku_costs: dict[str, int] | None = None


def get_sku_costs() -> dict[str, int]:
    """进程级缓存（`app.main` lifespan 先加载一次，节点直接取）。

    为什么缓存而**不支持热更新**（§8.5）：成本变化要留下"哪次 run 用的是哪版成本"的审计
    线索；背景重载会让同一个进程内前后两次 run 用不同成本，却没有任何记录说明分界点。
    改成本 = 改文件 + 重启，这正好是一次 code review 的机会。

    保证：返回**副本**（`dict(_sku_costs)`），调用方改了不会污染缓存，下一轮仍然拿到文件里
    的那份值。
    不保证：缓存已加载——`_sku_costs is None` 时才读文件，所以第一次调用是 I/O。
    坑：① 加载失败（`ConfigError`）时缓存**保持 None**，所以下一次调用会再读、再抛
    （不会把"失败"缓存成"空表"）；② 测试改完文件后必须 `reset_sku_costs_cache()`，
    否则看到的是上一个测试的成本表 —— 这是本仓测试最经典的串扰来源。
    """
    global _sku_costs
    if _sku_costs is None:
        _sku_costs = load_sku_costs()
    return dict(_sku_costs)


def reset_sku_costs_cache() -> None:
    """测试用：清掉缓存（`load_settings` 那边的 `reset_settings_cache` 同款）。

    坑：生产代码**不要**调它。在 lifespan 之外重置缓存会让"同一个进程前后两次 run 用不同
    成本"这个 §8.5 明确要避免的情形变成可能——而这正是本文件选择"不支持热更新"的原因。
    """
    global _sku_costs
    _sku_costs = None
