"""指标计算（CONTRACT §5.3，纯函数）。

设计要点（每一条都会被追问）：

1. **纯函数**：无 I/O、无全局状态、`now` 可注入 → 同一份 `raw` 在任何时刻算出同一结果，
   指标可复现、可对账。
2. **`None` 与 `0` 严格区分**：分母为 0 的比率返回 `None`（"没有意义"），不是 `0`
   （"等于零"）。把两者混起来，看板会把"没有访客数据"画成"转化率 0%"，
   运营会据此得出错误结论。
3. 金额**必须已是整数分**：出现 `*_yuan` 或 `*_cents` 是 float → 立刻 `ValueError`。
   在边界炸，而不是静默转换——静默转换会让 9900 分 vs 99.00 元的歧义一路传到下单。
4. 缺字段语义：`order_summary` 的五个金额/计数是**必需字段**（缺 → `KeyError`，
   因为缺了就算不出契约要求的字段）；`uv` 是可缺字段（缺 → `uv=None`, `cvr=None`）。
5. 成本口径（`gross_margin` / `cost_coverage_ratio`）：成本是**参数**（`cost_table`）不是文件
   读取（§8.5：读 `config/sku_costs.yaml` 是装配层的责任）。没有成本 → 两个字段都是 `None`，
   **不是 0**：把"没配成本"当 0 会把正常的商品判成亏本，然后 Agent 去改价/下架——本仓里
   最贵的一类误报。`cost_coverage_ratio` 存在的唯一理由，是让消费者看到"这个毛利率只覆盖了
   一部分成交额"，而不是把它当成全店的结论。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型（避免 metrics → agent 的模块级依赖）
    from app.agent.state import MetricsSnapshot, RawSnapshot

__all__ = ["compute_metrics", "REQUIRED_ORDER_SUMMARY_KEYS", "MIN_HISTORY_DAYS"]

#: `order_summary` 里必需存在的字段（缺一个就算不出契约字段，直接 KeyError 报名字）。
#:
#: 为什么这五个是"必需"而不是"缺了算 None"：它们参与的都是**加法/除法**，缺一个就等于
#: "这一天的 GMV 少了一部分"。把一个缺字段的日当成"GMV 偏低"直接喂给规则，`gmv_drop_day`
#: 会立刻报警——那是"数据缺失"伪装成"业务异常"，本仓最贵的一类误报。所以宁可 500。
REQUIRED_ORDER_SUMMARY_KEYS: tuple[str, ...] = (
    "gmv_cents",
    "order_count",
    "paid_order_count",
    "refund_order_count",
    "refund_amount_cents",
)

#: 近 7 日均值的最少历史天数（不足 → None，不给"用 1 天算均值"的假精确）。
#:
#: 取值 3 是契约 §5.3 冻结的（`不足 3 条 → None`）。它选 3 的理由可以自己推：均值要用来
#: 判"今天比平时低多少"，2 天里只要有一天是活动日，均值本身就带 50% 的偏差；3 天是"能算
#: 出一点基线、又不需要历史冷启动太久"的最小值。**不要**为了"让规则能触发"把它改成 1：
#: 那会让新店第一天就产生 `gmv_drop_day` 误报。
MIN_HISTORY_DAYS = 3

#: 近 7 日均值最多取多少条。
#: 7 是"一个完整自然周"（抹平周末/工作日差异），与 `effects.windows_days` 的 T+7 同一个理由。
MAX_HISTORY_DAYS = 7


# 调用方契约（`collect` 节点 → `compute_metrics`）：
#   raw         上游原始快照，**金额必须是整数分**（`_reject_non_cent_amounts` 会校验）
#   history     该店的历史快照（含今天也行，内部按 `< today` 过滤），用于 `gmv_7d_avg_cents`
#   previous    只用于"历史没被倒序"的一致性校验，不参与任何计算（见 docstring）
#   cost_table  `costs.get_sku_costs()` 的结果（可为空 dict = 没配成本）
#
# 四个参数都是 keyword-only（`raw` 除外）：`history` 与 `previous` 语义相关但完全不同，
# 位置传参会把"昨天"传成"全部历史"，而那不会报错，只会算出一个看起来正常的错数字。
def compute_metrics(
    raw: RawSnapshot,
    *,
    previous: MetricsSnapshot | None = None,
    history: Sequence[MetricsSnapshot] | None = None,
    now: datetime | None = None,
    cost_table: Mapping[str, int] | None = None,
) -> MetricsSnapshot:
    """由原始快照算出 `MetricsSnapshot`（公式见 §5.3 表，逐行对应）。

    `previous` 不在 `MetricsSnapshot` 里产出环比字段（§3.3 没有这些字段）：环比在节点层
    用 `history` 算出来放进规则命名空间（§8.3.3 的 `gmv_delta_ratio` 等）。
    这里对 `previous` 只做**顺序一致性校验**——把"历史倒序传入"这种错误在源头拦住，
    否则环比符号会整体翻转，而结果看起来完全正常。

    `cost_table` 是 keyword-only 的**可选**参数（`sku_id -> 成本整数分`，§8.5）：不传等价于
    "这家店没配成本"，两条成本口径字段都是 `None`。传了也不会凭空造出按 SKU 的成交额——那
    必须由 `collect` 归集进 `order_summary["by_sku"]`，否则我们只知道"成本是多少"，不知道
    "卖出去了多少件、按什么价卖的"，算不出任何有意义的毛利率。

    保证：① 纯函数（无 I/O、无缓存、`now` 可注入），同一份 `raw` 在任何时刻算出同一结果；
    ② 返回的 dict **键集合固定**（14 个键，缺值写 None 而不是省略），所以消费方可以无条件
    `snapshot["gross_margin"]`；③ 金额一律是整数分，出现 `*_yuan`/非 int 的 `*_cents` 直接抛错。
    不保证：`previous` 参与任何**计算** —— 它只被用来做顺序校验（见下面那段注释），环比要
    由调用方用 `history` 算好放进规则命名空间。名字叫 `previous` 但**不产出**任何环比字段，
    这是读这个签名最容易误解的一点。
    坑：① `raw["window"]["end"]` 是**业务日期**（`YYYY-MM-DD`），不是时间戳；拿时间戳当 date
    会让 `previous.date >= date` 的字符串比较失效（`"2026-10-24T00:00:00Z" >= "2026-10-24"`
    为真，于是同一天被当成"倒序"）。
    ② 缺 `uv` 合法（`uv=None` → `cvr=None`），缺 `gmv_cents` 不合法（KeyError）：前者是
    "上游可能没有访客数据"，后者是"这一天的成交额必须有"。
    """
    # 第一步就把"单位错"的输入炸掉（`*_yuan`、非 int 的 `*_cents`）。放在最前面是因为后面
    # 每一步都假设"分是整数"——等到算不出整数时再报，报错位置会指向除法而不是数据。
    _reject_non_cent_amounts(raw)

    try:
        window = raw["window"]
        date = str(window["end"])
    except (KeyError, TypeError) as exc:
        # `TypeError` 也在这里接：`raw` 可能是个 list/None（上游结构变了），那样
        # `raw["window"]` 抛的是 TypeError 而不是 KeyError，两种都要变成"缺业务日期"。
        raise KeyError(f"raw 缺少 window.end（业务日期）: {exc}") from exc

    summary = raw.get("order_summary")
    if not isinstance(summary, Mapping):
        raise KeyError("raw.order_summary 缺失或不是映射")
    # 五个必需字段逐个取（不是 `for key in REQUIRED...` 循环）：这样每个字段的类型错误都带
    # 自己的字段名，报错能直接指到"哪个字段是字符串"。
    gmv_cents = _require_int(summary, "gmv_cents", where="raw.order_summary")
    order_count = _require_int(summary, "order_count", where="raw.order_summary")
    paid_order_count = _require_int(summary, "paid_order_count", where="raw.order_summary")
    refund_order_count = _require_int(summary, "refund_order_count", where="raw.order_summary")
    refund_amount_cents = _require_int(summary, "refund_amount_cents", where="raw.order_summary")

    # `uv` 是**可缺**字段：先看原始值，缺了就直接 None（不进 `_require_int`，否则会 KeyError）。
    uv_raw = summary.get("uv")
    uv = None if uv_raw is None else _require_int(summary, "uv", where="raw.order_summary")

    # aov：分母 0 → 0（契约 §5.3 表；"客单价 0"是业务上有意义的 0，不是 None）
    # 注意这与下面两个比率**方向相反**，不是笔误：客单价是"这一天的平均值"，没有订单时它是
    # 0（可以画在图上）；比率没有订单时是"没有意义"（不能画成 0%）。
    aov_cents = 0 if paid_order_count == 0 else int(round(gmv_cents / paid_order_count))
    # 退款率 / 转化率：分母 0 → None（"没有支付订单"算不出退款率）
    refund_rate = (
        None if paid_order_count == 0 else round(refund_order_count / paid_order_count, 6)
    )
    cvr = None if not uv else round(paid_order_count / uv, 6)

    if previous is not None:
        # 顺序一致性校验：`previous` 必须是**更早**的一天。这个参数不参与计算，它的存在意义
        # 就是在这里挡住"历史倒序传入"——那会让外面算的环比符号整体翻转，而结果看起来完全
        # 正常（本仓里最难发现的一类错）。字符串比较在这里成立的前提是 date 是 `YYYY-MM-DD`
        # （见 `window.end` 上方注释）。
        prev_date = previous.get("date")
        if not prev_date:
            raise ValueError("previous 缺少 date")
        if str(prev_date) >= date:
            raise ValueError(
                f"previous.date({prev_date}) 必须早于本次 date({date})：历史被倒序传入"
            )

    # 7 日均值只看**严格早于今天**的历史（不含今天）：把今天算进均值会让"今天跌了"同时把
    # 基准也拉低，跌幅被系统性低估。
    gmv_7d_avg_cents = _gmv_7d_avg(history or (), today=date)

    # 成本口径：两个字段要么都有值、要么都是 None（成对出现，见函数内说明）。
    gross_margin, cost_coverage_ratio = _margin_and_coverage(
        summary, gmv_cents=gmv_cents, cost_table=cost_table
    )

    # `computed_at` 在**最后**取：它表示"这一份指标是什么时候算出来的"，取早了会与实际计算
    # 时刻差出整个计算耗时（虽然很小，但报告里的顺序会看起来"指标比它自己新"）。
    computed_at = _iso_z(now)

    return {
        "date": date,
        "gmv_cents": gmv_cents,
        "order_count": order_count,
        "paid_order_count": paid_order_count,
        "refund_order_count": refund_order_count,
        "refund_amount_cents": refund_amount_cents,
        "aov_cents": aov_cents,
        "refund_rate": refund_rate,
        "uv": uv,
        "cvr": cvr,
        "gmv_7d_avg_cents": gmv_7d_avg_cents,
        "gross_margin": gross_margin,
        "cost_coverage_ratio": cost_coverage_ratio,
        "computed_at": computed_at,
    }


def _margin_and_coverage(
    summary: Mapping[str, Any],
    *,
    gmv_cents: int,
    cost_table: Mapping[str, int] | None,
) -> tuple[float | None, float | None]:
    """店铺级加权毛利率 + 有成本 SKU 的成交额占比（两个都可能为 `None`）。

    公式（只对**有成本**的那部分 SKU 算，成本未知的 SKU 既不进分子也不进分母）：

        covered_revenue = Σ by_sku[sku].gmv_cents        （sku ∈ cost_table）
        covered_cost    = Σ cost_table[sku] × by_sku[sku].units
        gross_margin        = (covered_revenue - covered_cost) / covered_revenue
        cost_coverage_ratio = covered_revenue / gmv_cents

    为什么分母是"有成本的成交额"而不是全店 GMV：把没有成本的 SKU 塞进分母，等于假设
    "这些 SKU 的成本等于售价"（毛利 0）。那是**编出来的数**，而且方向不可控——真亏的会被
    摊薄成"还行"，真赚的会被摊薄成"亏本"。宁可只报一个覆盖了一部分的毛利率，也不报一个
    假装覆盖了全店的。

    为什么需要 `cost_coverage_ratio`：上面那个"只报一部分"的性质必须**可见**，否则
    `gross_margin = -0.5` 会被读成"全店在亏"，其实它可能只代表 5% 的成交额。规则层因此
    要求覆盖率下限；本函数只负责如实给出这个数。

    `None` 的四种来源（都**不能**用 0 代替）：
    - `by_sku` 缺失：`collect` 没做按 SKU 归集（老快照/历史桶）→ 无法归属，两个都是 `None`；
    - `covered_revenue == 0`：一个 SKU 都没配上成本（或配了但今天没卖）→ 毛利率 `None`；
    - `gmv_cents == 0`：全店没成交 → 覆盖率的分母是 0 → `None`（§5.3 的除法边界）；
    - 覆盖率不裁剪：`covered_revenue > gmv_cents` 说明上游汇总与订单明细对不上，如实给出
      > 1 的数，让数据不一致暴露出来，而不是用 `min(…, 1.0)` 把它抹平。
    """
    by_sku = summary.get("by_sku")
    if by_sku is None:
        # 老快照/历史桶没有按 SKU 归集 → 无法归属成本 → 两个都是 None。
        # 这里返回 (None, None) 而不是抛错：这是**已知的缺数据类型**，不是损坏。
        return None, None
    if not isinstance(by_sku, Mapping):
        # 用 ValueError 而不是 TypeError：与同文件的 `_require_int` / `_reject_non_cent_amounts`
        # 保持一致——本模块把"上游给的数据形状不对"统一表达成 ValueError（调用方只需处理一种）。
        raise ValueError(  # noqa: TRY004
            f"order_summary.by_sku 必须是映射（sku_id -> {{gmv_cents, units}}），"
            f"收到 {type(by_sku).__name__}"
        )

    table = cost_table or {}
    covered_revenue = 0
    covered_cost = 0
    for raw_sku, entry in by_sku.items():
        sku_id = str(raw_sku)
        where = f"order_summary.by_sku[{sku_id!r}]"
        if not isinstance(entry, Mapping):
            raise ValueError(  # noqa: TRY004 - 同上：数据形状错误统一用 ValueError
                f"{where} 必须是映射，收到 {type(entry).__name__}"
            )
        entry_revenue = _require_int(entry, "gmv_cents", where=where)
        entry_units = _require_int(entry, "units", where=where)
        if entry_revenue < 0:
            raise ValueError(f"{where}.gmv_cents 必须 >= 0，收到 {entry_revenue}")
        if entry_units < 0:
            raise ValueError(f"{where}.units 必须 >= 0，收到 {entry_units}")

        cost = table.get(sku_id)
        if cost is None:
            continue  # 成本未知：不计入毛利，但它的成交额也不会被塞进分母（见 docstring）
        # 成本表本身在加载期已校验（§8.5），这里再查一次是因为 `compute_metrics` 是公开
        # 纯函数：调用方可以直接传字典进来，边界上炸比让 float 成本静默参与运算更好。
        if isinstance(cost, bool) or not isinstance(cost, int):
            raise ValueError(  # noqa: TRY004 - 同上：金额类型错误统一用 ValueError
                f"cost_table[{sku_id!r}] 必须是整数分 int，收到 {cost!r}"
            )
        if cost <= 0:
            raise ValueError(f"cost_table[{sku_id!r}] 必须 > 0（整数分），收到 {cost}")
        if entry_revenue > 0 and entry_units == 0:
            # 有成交额却没有件数：成本摊不出去（金额/件数任一为 0 都会让毛利率失真）。
            #
            # 为什么采集层容错、这里却拒绝（v1.5.1，两层**态度不同是刻意的**）：
            # - 采集层（`nodes._order_units`）面对的是**上游真实脏数据**：`product_count: 0`
            #   如果直接抛，`collect` 节点会把整轮 run 打成 failed → `POST /api/agent/run` 500
            #   → 一条脏数据打死当天所有指标。所以那一层对"件数 <= 0 / 缺件数"按 1 件降级，
            #   并记一条 warn 级审计（JSONL 可见）；
            # - 本层是**纯函数边界**，面对的是"已经归集好的 by_sku"。到这里 `units` 已经被
            #   下游当成真值参与 `成本额 = 单价成本 × 件数`，再猜一次就等于在真值上盖一层猜测，
            #   而且会让"有人绕开 collect 手工拼 summary"这类错误永远不被发现。所以这里拒绝，
            #   不降级、不兜底。
            #
            # 换句话说：容错属于**数据入口**，拒绝属于**计算契约**；两者都在做自己的本职。
            raise ValueError(
                f"{where}: 成交额 {entry_revenue} > 0 但 units = 0，成本无法折算"
                "（按 SKU 归集时件数必须 >= 1）"
            )

        covered_revenue += entry_revenue
        covered_cost += cost * entry_units

    gross_margin = (
        None
        if covered_revenue <= 0
        else round((covered_revenue - covered_cost) / covered_revenue, 6)
    )
    cost_coverage_ratio = None if gmv_cents <= 0 else round(covered_revenue / gmv_cents, 6)
    return gross_margin, cost_coverage_ratio


def _gmv_7d_avg(history: Sequence[Mapping[str, Any]], *, today: str) -> int | None:
    """近 7 日（**不含今天**）GMV 均值；可用天数 < 3 → None。

    保证：① 只用**严格早于** `today` 的条目（同一天重复跑不会把今天算进基线）；② 取日期最新的
    7 条（`sort(reverse=True)[:7]`），所以历史里混着很老的桶也不影响；③ 返回 `int` 分
    （`round` 后转 int），与其它金额字段同类型。
    不保证：历史里日期重复时是哪一条胜出——`sort` 是稳定的，重复日期会按传入顺序保留，然后
    一起被计入均值（同一个日期出现两次 = 那天的权重翻倍）。上游理论上不该给重复日。
    坑：`len(entries) < MIN_HISTORY_DAYS` 用的是"过滤后"的条数，所以传 10 条历史但 8 条都是
    今天/未来日期时，仍然返回 None —— 这是对的（"有数据"不等于"有可比的历史"）。
    """
    # 先把"非映射"和"日期不早于今天"的条目筛掉：`isinstance` 判断不是防御性代码，历史桶来自
    # JSONL/DB 反序列化，混入 None 是真实发生过的。
    entries = [
        item for item in history
        if isinstance(item, Mapping) and str(item.get("date") or "") < today
    ]
    entries.sort(key=lambda item: str(item["date"]), reverse=True)
    entries = entries[:MAX_HISTORY_DAYS]
    if len(entries) < MIN_HISTORY_DAYS:
        # 不足 3 天宁可不给值：给一个"用 1 天算的均值"会让 `gmv_drop_day` 在开业第二天就误报。
        return None
    values = [_require_int(item, "gmv_cents", where="history[]") for item in entries]
    return int(round(sum(values) / len(values)))


def _require_int(container: Mapping[str, Any], key: str, *, where: str) -> int:
    """取整数字段：缺失 → KeyError（带位置），`bool` 不算 `int`，float 一律拒绝。

    保证：返回的**一定是 int**（不是 bool、不是 float、不是字符串数字）——所有金额/计数都靠
    这一点保证"分"的口径不被 float 污染。
    不保证：非负。负 GMV/负订单由上游决定要不要拦（本层不替业务做判断，除了 `_margin_and_coverage`
    里对 by_sku 的显式非负校验）。
    坑：**不做**字符串数字的转换（`"9900"` 直接抛）：转换属于边界适配层，在这里宽容会把
    "上游类型变了"这件事永远掩盖掉。`bool` 要显式排除——它是 int 的子类，混进来会把
    `True` 当 1 分钱。
    """
    if key not in container:
        raise KeyError(f"{where} 缺少必需字段 {key!r}")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}.{key} 必须是 int（整数分/计数），收到 {value!r}")
    return value


def _reject_non_cent_amounts(raw: Any, *, path: str = "raw") -> None:
    """金额字段必须是整数分：出现 `*_yuan` 或非 int 的 `*_cents` → ValueError。

    保证：递归覆盖**整个** `raw`（映射的每一层 + 列表的每一项），所以 `by_sku[*].gmv_cents`
    这种深层金额也被检查；路径（`raw.order_summary.by_sku['x'].gmv_cents`）直接进报错信息。
    不保证：键名不带 `_cents` 的金额（例如 `price`、`amount`）——它们是上游原始字段，本函数
    按命名约定判断，不做"猜单位"。
    坑：`_cents` 分支里的 `not isinstance(value, bool)` 是**放行**而不是拦截：`xxx_cents: true`
    这类脏值会先从这里溜过去，最终在 `_require_int` 取值时被拒。两层分工不要合并——本函数
    负责"单位口径"，`_require_int` 负责"类型"，合并后任何一处放宽都会同时削弱两道闸。
    """
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            key_str = str(key)
            here = f"{path}.{key_str}"
            if key_str.endswith("_yuan"):
                # 元字段一律拒：`9900` 是分还是元，这一层的歧义一旦放过就会变资损。
                raise ValueError(f"{here}: 禁止元为单位的金额字段（一律整数分 *_cents）")
            if key_str.endswith("_cents") and not isinstance(value, bool):
                if not isinstance(value, int):
                    raise ValueError(f"{here}: 金额必须是整数分 int，收到 {value!r}")
            _reject_non_cent_amounts(value, path=here)
    elif isinstance(raw, (list, tuple)):
        for index, item in enumerate(raw):
            _reject_non_cent_amounts(item, path=f"{path}[{index}]")
    return


def _iso_z(dt: datetime | None) -> str:
    """UTC ISO-8601、**毫秒**精度、`Z` 结尾（§0 时间规则 + §7.5 示例）。

    统一毫秒而不是 `isoformat()` 的微秒：日志/`X-Trace-Id`/API 响应里的时间戳
    必须是同一个形状，否则前端按字符串比较/排序、以及"两个事件谁先谁后"的对账
    都会因为精度不同而出现假差异（reviewer F5）。毫秒截断不是四舍五入——时间戳
    只做展示与排序，截断不会让它偏离真实时刻超过 1ms。
    """
    value = dt if dt is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
