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

三种"身份"的顺序关系（读这个文件最容易混的地方）：

```
提议期  action_id  = f"{run_id}:{action_type}:{target}"      ← 审批行主键，审批**之前**生成
落库期  payload_digest = sha256(canonical_json({shop,type,target,params}))
                                                            ← 参数指纹，edit 时重算
执行期  idempotency_key = sha256(f"{shop}|{type}|{target}|{canonical_json(params)}")
                                                            ← 执行身份，审批**之后**才生成
```

`target` 只能携带**一个** id（`sku_id` 优先、退回 `product_id`），但 `editPrice`/`editStock`
两个参数都要，所以完整快照放在 `evidence["target_snapshot"]` 里——利用"证据字段不参与身份
计算"这一点，把排障需要的信息带上而不改变幂等语义。
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
#:
#: 顺序即"写入时的字段顺序"，但**消费方一律用 `.get()`**，所以缺字段不会 KeyError，只会让
#: 目标解析/参数展开拿到 None（然后被 `_expand_params` 拒掉并记 dropped）。
#: 只有前两个是身份字段（`sku_id`/`product_id`），中间四个是决策依据，最后两个是证据快照。
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

#: 每种动作类型**恰好**允许的参数键与类型（§5.5）。
#:
#: 这张表是"人改的参数"与"机器生成的参数"共用的唯一闸门（`verdict=edit` 也走
#: `validate_params`），所以它必须精确到"键集合恰好相等"：多一个键意味着调用方以为它能生效，
#: 而实际上要么被丢弃、要么被上游忽略；少一个键则下游必然 KeyError。
#:
#: 金额统一叫 `*_cents` 且类型是 `int`：类型即单位（见 `normalize_params` 的"不做元→分换算"）。
#: 想加一种动作类型，要同时加：这张表 + `ActionType` 枚举 + `_POSITIVE_FIELDS`/`_NON_NEGATIVE_FIELDS`
#: + `nodes._dispatch` 的分发分支——四处缺一处都会在运行期才炸。
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
#:
#: 为什么分成三个表而不是写进 schema：schema 只表达"键 → 类型"，取值域是**动作语义**——
#: 加价可以、负价不行；库存可以清零、不能为负；券的起止时间要能解析。
#: 把取值域塞进 schema 会让 `{"price_cents": int}` 这种"纯类型表"变成一张什么都管的表。
_POSITIVE_FIELDS = {
    ActionType.PRICE_UPDATE: ("price_cents",),
    ActionType.COUPON_CREATE: ("discount_cents", "total_count"),
}
#: 库存允许 0（清库存动作就用 0），所以它不是 `_POSITIVE_FIELDS`。
_NON_NEGATIVE_FIELDS = {ActionType.STOCK_UPDATE: ("stock",)}
#: 只有券有起止时间；用 `(动作, 字段元组)` 而不是 dict 是因为只服务一个动作类型。
_TIME_FIELDS = (ActionType.COUPON_CREATE, ("begin_time", "end_time"))

#: 三个具名目标来源（与 `rules._TARGET_SOURCES` 必须一致；规则里的 `target_from` 只能取
#: 这三个之一或 `sku:<id>`）。这里再列一次是因为 `_resolve_target` 需要一个可判定的集合，
#: 而它在**不 import rules** 的前提下也要能报错——两边不一致时由 `_resolve_target` 抛
#: `ParamValidationError`，不会被静默忽略。
_TARGET_SOURCES = ("top_gmv_sku", "top_refund_sku", "lowest_stock_sku")


def _action_type(value: ActionType | str) -> ActionType:
    """把 `ActionType | str` 收成枚举；未知值抛 `ParamValidationError`（→ 400）。

    保证：接受枚举实例与字符串两种形态（上游有的是 JSON 字符串，有的是枚举），返回的一定是
    枚举——后续 `ACTION_PARAM_SCHEMA[resolved]` 的查表依赖这一点。
    坑：`ActionType` 是 `StrEnum`，所以 `str(枚举)` 得到的就是值 `"price_update"`；如果哪天改成
    纯 `Enum`，这一行会静默失效（所有动作都变成"未知类型"的字符串），改枚举基类时先看这里。
    """
    try:
        return ActionType(str(value))
    except ValueError as exc:
        raise ParamValidationError(
            f"未知动作类型：{value!r}（允许 {[t.value for t in ActionType]}）"
        ) from exc


# `normalize_params` 的两个调用方，语义**不同**（都保留，别合并成一个"宽松版 validate"）：
#   ① `validate_params` 里：先归一化、再判键集合/类型/值域（完整校验）
#   ② `approvals.effective_params` 里：只归一化已落库的参数（它当初已经校验过）
# 所以本函数必须能在"键集合不完整/有多余键"的情况下工作（多余键原样保留、缺键不报错）；
# 想在这里顺手加键集合检查，会直接让 ② 在"库里存的是老格式"时抛错。
def normalize_params(action_type: ActionType | str, params: Mapping[str, Any]) -> dict[str, Any]:
    """归一化：剔除 `None` 值、金额字段转 int 分。

    只做**可逆**转换（`9900.0`/`"9900"` → `9900`），不做单位换算（元→分是上游适配层的事，
    在这里换会让"9900 元"和"9900 分"变成同一个值——那是资损级歧义）。

    保证：① `None` 一律剔除（它在下游会被上游读成"显式置空"）；② 金额字段的输出一定是
    `int`（float 只在是整数值时才接受，`9900.5` 会抛错）；③ `bool` 被显式拒绝（`True` 不是 1）。
    不保证：**键集合完整**。`schema` 里没有的键会被原样拷进结果（`else` 分支），多余/缺失
    键的检查只在 :func:`validate_params` 里做。只调 normalize 不调 validate 会漏过校验。
    坑：`"9900"` 能转、`"+9900"` 和 `" 9900 "` 也能转（`strip()`），但 `"9_900"` 不能——
    不要在调用方先自己 int()，两种转换规则不一致是最难查的资损来源。
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
                # bool 是 int 的子类，不显式拦就会把 True 当 1 分钱写进库里。
                raise ParamValidationError(f"参数 {key} 不允许 bool")
            if isinstance(raw, int):
                normalized[key] = raw
            elif isinstance(raw, float) and raw.is_integer():
                # 只接受"整数值的 float"：`9900.0` OK，`9900.5` 抛错——分是最小单位，
                # 半个分只能说明上游算错了，四舍五入会悄悄改掉金额。
                normalized[key] = int(raw)
            elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
                # `lstrip("-")` 让负数字符串也进得来（然后被 `_POSITIVE_FIELDS` 拒），
                # 这样"负数"报的是业务约束而不是"无法归一化"，报错更贴近真实原因。
                normalized[key] = int(raw.strip())
            else:
                raise ParamValidationError(
                    f"参数 {key} 无法归一化为整数分：{raw!r}"
                )
        else:
            normalized[key] = raw
    return normalized


def validate_params(action_type: ActionType | str, params: Mapping[str, Any]) -> dict[str, Any]:
    """校验并返回归一化后的参数。失败抛 `ParamValidationError`（→ 400）。

    保证：① 键集合必须**恰好相等**（多一个少一个都错，§4.4 规则 4）；② 类型精确（`bool`
    不算 `int`）；③ 取值域（价格/券面额 `> 0`、库存 `>= 0`）；④ 券的起止时间能被
    `datetime.fromisoformat` 解析。
    不保证：时间窗的**业务**合理性（`begin_time > end_time` 这里不判）——那需要"券的时长上限"
    之类的业务参数，属于规则/产品层，放进参数校验会让 schema 越来越不像 schema。
    坑：校验顺序是"结构 → 归一化 → 类型 → 值域 → 时间"。先归一化再判类型是必要的：
    `"9900"` 要先变成 `9900` 才能过 `isinstance(int)`；颠倒会让所有字符串金额都报类型错。
    """
    resolved = _action_type(action_type)
    schema = ACTION_PARAM_SCHEMA[resolved]
    if not isinstance(params, Mapping):
        raise ParamValidationError(f"{resolved} 的参数必须是映射，收到 {type(params).__name__}")
    keys = set(params)
    expected = set(schema)
    # 键集合**恰好相等**（不是"至少包含"）：多一个键意味着调用方以为它能生效，而它在下面
    # 会被丢弃/被上游忽略——两种结果都比 400 难查。
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
            # 二次类型检查：`normalize_params` 已经把 `"9900"` 转成 int，但返回值也可能来自
            # `edited_params`（人手输入的），所以这里必须再判一次——两道闸不能合并。
            if isinstance(value, bool) or not isinstance(value, int):
                raise ParamValidationError(
                    f"{resolved}.{key} 必须是 int（bool 不算 int），收到 {type(value).__name__}"
                )
        elif not isinstance(value, expected_type):
            raise ParamValidationError(
                f"{resolved}.{key} 必须是 {expected_type.__name__}，收到 {type(value).__name__}"
            )
    for key in _POSITIVE_FIELDS.get(resolved, ()):  # > 0
        # 价格/券面额为 0 是资损：0 元券等于白送、0 元改价会让商品变成免费。
        if normalized[key] <= 0:
            raise ParamValidationError(f"{resolved}.{key} 必须 > 0，收到 {normalized[key]}")
    for key in _NON_NEGATIVE_FIELDS.get(resolved, ()):  # >= 0
        # 库存允许 0（清库存），负库存不是"超卖"而是数据错误——上游会拒绝或算错。
        if normalized[key] < 0:
            raise ParamValidationError(f"{resolved}.{key} 必须 >= 0，收到 {normalized[key]}")
    time_action, time_fields = _TIME_FIELDS
    if resolved == time_action:
        for key in time_fields:
            try:
                # 用 `fromisoformat` 而不是正则：它接受 `2026-10-24`、带时区的完整时间戳、
                # 毫秒精度等多种合法形态，而正则只会在"上游换格式"时变成误拦。
                datetime.fromisoformat(str(normalized[key]))
            except ValueError as exc:
                raise ParamValidationError(
                    f"{resolved}.{key} 不是合法的 ISO-8601 时间：{normalized[key]!r}"
                ) from exc
    return normalized


def payload_digest(
    *, shop_id: str, action_type: ActionType | str, target: str, params: Mapping[str, Any]
) -> str:
    """§4.5 的 `payload_digest`（审批行的参数指纹，参数被改过就变）。

    保证：对 `params` 做 canonical JSON（排序键、紧凑分隔）后再哈希，所以键顺序不同的两份
    等价参数得到同一个摘要——否则审批台的"参数有没有被改"会随机误报。
    不保证：与 `idempotency_key` 相等。两者**故意**用不同的拼接方式（见下面 `idempotency_key`
    的说明），所以**不能**互换，也不能拿一个去查另一个。
    坑：`params` 里若有非 JSON 可序列化的值（datetime/Decimal），`canonical_json` 会抛错；
    调用方必须保证参数已经过 `normalize_params`（那里把金额收成 int）。
    """
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

    保证：同一个 (店铺, 动作类型, 目标, 参数) 永远得到同一个键，跨进程/跨重启稳定（没有
    随机盐、没有时间戳）。
    不保证：唯一性成立需要参数归一化——`{"stock": 5}` 与 `{"stock": 5.0}` 在归一化前是不同
    的分隔串，会得到两个键、执行两次。所以生成键**之前**必须先 `validate_params`。
    坑：`"|"` 拼接的分隔串有注入风险（`target` 里含 `|` 会让两个不同的四元组拼出同一个串），
    这里安全的前提是 `target` 来自 `sku_id`/`product_id` 的 ID 字符集（不含 `|`）。改 ID 规则
    时要回头看这一行。
    """
    return hashlib.sha256(
        f"{shop_id}|{action_type}|{target}|{canonical_json(dict(params))}".encode("utf-8")
    ).hexdigest()


def action_id_for(*, run_id: str, action_type: ActionType | str, target: str) -> str:
    """§4.5：`f"{run_id}:{action_type}:{target}"`（提议身份，审批前生成）。

    保证：不含参数——所以同一 run 里"同类型同目标、参数不同"的两条提议会撞同一个 id，
    由 `propose_actions_with_report` 的 `seen` 去重（记 dropped）。这是刻意的：一次 run
    对同一个 SKU 改两次价，本来就是规则配置重复。
    不保证：全局唯一。它带 run_id，所以只在 run 内唯一；跨 run 的执行去重靠
    `idempotency_key`。
    坑：不含 `shop_id`——`run_id` 已经隐含了店铺（一次 run 只服务一个店），所以别在别处拿
    `action_id` 做跨店查询。
    """
    return f"{run_id}:{action_type}:{target}"


# --------------------------------------------------------------------------------------
# 目标解析与模板参数展开
# --------------------------------------------------------------------------------------


def _resolve_target(target_from: str, products: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """把 `target_from` 解析成一条商品/规格条目；解析不到返回 `None`（由调用方记 dropped）。

    保证：返回的条目一定来自入参 `products`（不构造新 dict），所以下游拿到的
    `price_cents`/`gmv_cents` 就是 `collect` 归一化过的那份值。
    不保证：结果"正确"。`top_gmv_sku` 只是"GMV 最大的那个"，规则作者真正想改的可能不是它；
    这一层只做"把规则里的符号解成一个具体条目"，选得对不对是规则配置的事。
    坑：① 空 `products` 必须先返回 None——`max()`/`min()` 对空序列会抛 `ValueError`，
    那不是"找不到目标"而是"数据为空"，混在一起会让 dropped 里出现无意义的异常；
    ② `sku:<id>` 同时匹配 `sku_id` 和 `product_id`（`editPrice` 两种 id 都可能被规则写）；
    ③ 四个来源的 `key=lambda` 都写了 `int(... or 0)`：字段缺失时按 0 参与比较而不是抛错——
    `max` 里只要不全是 None 就能选出东西，全 0 时返回第一个（顺序稳定，可复现）。
    """
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
        # `min` 配 `or 0`：缺 stock 的条目会被当成"库存 0"从而抢到"最低库存"的位置。
        # 这是刻意的保守方向——宁可对一个缺数据的条目做清库存，也不假装它有货。
        return min(products, key=lambda item: int(item.get("stock") or 0))
    raise ParamValidationError(f"未知 target_from：{target_from!r}")


def _target_id(product: Mapping[str, Any]) -> str:
    """动作目标 id：优先 `sku_id`，退回 `product_id`；两者都没有就抛 `ParamValidationError`。

    保证：返回**非空字符串**（`if sku_id:` 判过真值，"0" 这种字符串也算有值）。
    不保证：这个 id 在上游一定存在——那是 `execute` 节点调用时才知道的事。
    坑：`sku_id` 优先不是随意的：`editPrice`/`editStock` 都以 SKU 为粒度，用 `product_id`
    会让改价打到整个商品（多规格）+ 幂等键与"改的是哪个规格"不一致，跨 run 去重就错了。
    """
    sku_id = product.get("sku_id")
    if sku_id:
        return str(sku_id)
    product_id = product.get("product_id")
    if product_id:
        return str(product_id)
    # 两个 id 都没有 = 上游数据结构变了（或 collect 归一化漏了字段），必须立刻失败：
    # 没有 target 的动作既不能审批也不能执行，编一个空串会让 action_id 变成 "run:type:"。
    raise ParamValidationError("商品条目缺少 sku_id/product_id，无法确定动作目标")


def _expand_params(rule: Rule, action_type: ActionType, product: Mapping[str, Any],
                   template: Mapping[str, Any]) -> dict[str, Any]:
    """§8.3 的"动作模板参数 → 最终参数"冻结映射。

    保证：返回的参数已经过 :func:`validate_params`（键集合/类型/值域全过），所以下游可以直接
    拿去执行；金额一律是整数分（`round()` 之后是 int）。
    不保证：降价后的价格"合理"。`price_drop_pct` 只要求 (0,100)，降到 0.01 元也合法——下限
    是产品/规则层的事，这里不做业务判断（做了就等于替人决定"最低价"）。
    坑：① 降价基准取**当天快照**的 `price_cents`（不是历史价、不是成本价），所以同一规则
    连跑两天会连降两次——这正是 `idempotency_key` 按最终参数算的原因（第二天的参数不同，
    幂等键也不同，不会被误判成"执行过"）；
    ② `price_cents` 必须是 `int` 且 `> 0`：float 会让 `round()` 的银行家舍入在高价位产生
    1 分的偏差，而 `<= 0` 说明上游数据已经坏了，按比例算出负数没有意义。

    注意：`rule` 参数**当前没有被函数体读取**（模板已由调用方从 `rule.actions[i]` 取出并作为
    `template` 传入）。保留它是为了让展开期的签名带上规则上下文；不要以为它会按 `rule.id`
    做任何差异化处理——真正差异化的地方在 `template` 的内容里。
    """
    if action_type is ActionType.PRICE_UPDATE:
        pct = template.get("price_drop_pct")
        # `isinstance(pct, bool)` 显式排除：`True` 是 int 的实例，(0,100) 检查会放它过去，
        # 然后算出"降价 100%"——布尔混进数值参数是最典型的 YAML 手误（`price_drop_pct: yes`）。
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            raise ParamValidationError(f"price_update 模板参数 price_drop_pct 必须是数值：{pct!r}")
        # (0,100) 开区间：0% 是空操作（产生一条永远不改变任何东西的提议），100% 会让价格归零
        # （接着被 `price_cents > 0` 拒掉）。提前在这里给出"规则配置错"的报错，比让它走到
        # validate_params 报"price_cents 必须 > 0"更能指向真正的原因。
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
        # 库存模板直接下发（`stock: 0` 是清库存的常用写法），不做比例换算。
        return validate_params(action_type, {"stock": template.get("stock")})
    # 券：模板就是最终参数（面额/数量/起止时间都由规则给全）。
    return validate_params(action_type, dict(template))


# risk 与"能不能跳过 interrupt"的关系（`agent.auto_approve_risk`，默认 `none`）：
#
#   auto_approve_risk=none   → 所有提议都要人工批
#   auto_approve_risk=low    → 只有 `Risk.LOW` 的提议可自动通过，且必须在 audit 里记
#                             `approval.auto`（§8.2）
#   任何配置下              → **critical 异常**产出的动作永不自动通过
#
# 所以 `_risk_of` 的返回值不是一个"展示用的标签"，而是**唯一**能让人工环节被跳过的开关。
# 改阈值（0.15 / 0.05）等于改"多大幅度的降价可以不看就执行"——那不是调参，是改风控。
def _risk_of(action_type: ActionType, params: Mapping[str, Any], product: Mapping[str, Any]) -> Risk:
    """§5.5 冻结的 risk 判定（`auto_approve_risk` 依赖它，写死不给配置项）。

    保证：判定只依赖"动作类型 + 参数 + 目标快照"三样，不读配置、不读历史，所以同一条提议
    在任何时刻都得到同一个风险等级（可复现、可测）。
    不保证：风险等级的"业务充分性"。它只看跌幅比例，不看销量/库存/毛利——一个高毛利商品
    降 20% 可能完全正常，但这里仍然判 `high`。
    坑：① 阈值是 `> 0.15` / `> 0.05`（**严格大于**）：正好跌 15% 是 `medium`、正好跌 5% 是
    `low`。定成 `>=` 会让"边界值算哪个等级"变成一次契约变更；
    ② `drop` 可以是负数（涨价）：`-0.2 > 0.15` 为假 → 落到 `low`。这是有意的，涨价不是高危；
    ③ 拿不到有效 `price_cents`（None/0/bool）时直接 `low` —— 但这不该发生（`_expand_params`
    已经拒了），所以它只是"别在这里抛"的兜底；
    ④ 清库存恒 `low`（不动钱），券恒 `high`（真金白银的预算），两个 `return` 不依赖参数。
    """
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

    保证：`dropped` 里的每一条都是**人能读的原因字符串**（带 rule.id、动作类型、异常文本），
    节点把它逐条记进 `errors` 即可，不需要再格式化。
    不保证：`dropped` 的条数与"本该有的提议数"能对上——截断（达到
    `max_actions_per_run`）时只补一条汇总说明，不是逐条列。
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
    """按异常展开规则里的动作模板，产出提议列表 + 丢弃原因。

    保证：① 每条提议的 `action_id` 在一批内唯一（`seen` 去重，撞车的丢掉并记录）；② 条数
    不超过 `settings.agent.max_actions_per_run`；③ 提议顺序 = 异常顺序 × action 顺序，而异常
    顺序已经是"severity 降序 + rule_id"的全序（`anomalies.evaluate_rules` 排过），所以截断
    丢掉的永远是**最不重要**的那些。
    不保证：提议"合理"。它只确保参数合法、目标存在、风险等级有值；某个降价是否该做是规则
    和人（审批台）的判断。
    坑：① `limit` 检查在 `append` **之后**，所以最后一条被保留、其它模板直接不再展开——如果
    把检查放在 `for` 开头，达到上限时会多绕一圈（无害但会多一条 dropped 文案）；
    ② 单个动作模板不成立只丢它自己（`continue`），不让整批提议消失：一条规则配错不该让
    当天所有动作都没了；
    ③ `metrics` 里只挑五个字段写进 `evidence`——`evidence` 会进 checkpoint/JSONL，全量塞进去
    会让审计行膨胀，而排障真正要看的就是这几个。
    """
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
            # 为什么不抛：这一批提议里其它异常仍然是有效的，为一条脏数据打死整轮 run 的代价
            # 远大于"少一条提议 + 一条 dropped 记录"。
            dropped.append(f"异常 {anomaly['anomaly_id']} 引用了未知规则 {anomaly['rule_id']}")
            continue
        if not rule.actions:
            # 没有 actions 的规则是"只告警不动手"（例如 loss_making），不是错误，也不记 dropped。
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
                # 同一 run 里"同类型 + 同目标"出现两次 = 规则配置重复（`action_id` 不含参数，
                # 见 `action_id_for`）。保留先展开的那条，后者的参数被静默丢弃是危险的，
                # 所以必须记一条 dropped 让配置问题暴露出来。
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
                    # evidence 是"审批台上给人和审计看的那一屏"：异常 id + 规则 id + 当时的
                    # 指标快照 + 目标快照。快照必须存**当时**的值——执行/对账时商品可能已经
                    # 变了，没有快照就无法复盘"当时为什么这么判"。
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
                # `len(seen) < len(anomalies)` 只是个**粗略**判断"可能还有没展开的模板"：
                # `seen` 数的是动作数、`anomalies` 数的是异常数，两者单位不同。它只决定
                # "要不要补一条截断说明"，不参与任何业务判断——不要拿它当"还差几条"来算。
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
    """契约 §5.5 的公开签名：只返回提议列表（丢弃原因走 `propose_actions_with_report`）。

    保证：与 `propose_actions_with_report(...).proposals` **逐项相同**（这里没有任何额外逻辑，
    也不会再截断一次）。
    不保证：丢弃原因 —— 用这个入口的调用方拿不到"为什么少了某条提议"，所以节点里应当改用
    `propose_actions_with_report`，这个函数只为"契约签名"和"单测里只关心列表"存在。
    坑：不要在这里再套一层 try/except 把异常吞成空列表——空提议看起来和"今天没异常"一样。
    """
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

# 导出面说明（改 `__all__` 前先确认调用方）：
#   - `propose_actions` 是契约 §5.5 的签名，节点**不该**用它（拿不到 dropped）；
#     节点用 `propose_actions_with_report`。
#   - 三个身份函数（`action_id_for` / `payload_digest` / `idempotency_key`）被
#     `approvals`、`notifier`、`nodes` 三处交叉引用，改公式必须三处一起改。
#   - `normalize_params` 对外导出是因为 `approvals.effective_params` 需要在**不校验**的前提下
#     归一化已存库的参数（见那里的说明）。
