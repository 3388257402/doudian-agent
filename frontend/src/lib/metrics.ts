// 看板派生层：把 GET /api/metrics 的 points 变成可画的序列。
//
// 两条硬约束，都是"不许编数"：
//   1. **毛利率直接读后端字段 `gross_margin`**（CONTRACT §7.5 / §5.3：店铺级，只对**有成本的
//      SKU** 加权）。前端**不再自算**——旧实现读契约外扩展字段 `cost_cents`，按
//      (GMV − 退款 − 成本) / (GMV − 退款) 计算，与后端不是同一个数：同一个中文名"毛利率"
//      会出现两个数，而看板上那个数无法被后端任何测试证明。旧路径已整体删除，不保留开关。
//   2. `gross_margin` 只覆盖"有成本的 SKU"，必须与 `cost_coverage_ratio` **成对读**：
//      覆盖率 10% 的 −50% 不代表"全店在亏"。所以显示数值时必须同时说清它覆盖了多少成交额，
//      覆盖率小于 100% 时卡片上给出"仅覆盖 X% 成交额"。`null` ≠ `0`：null 是"不知道（没配
//      成本 / 没按 SKU 归集 / 分母为 0）"，0 是"不赚不亏"这个真事实。
//
// 契约 §7.5 里**仍然没有**库存字段，所以"库存周转"只能读契约外扩展字段（inventory_units，
// 见 api/extension.ts）；没带就产出 MetricGap，由页面显示"数据源缺字段"，不画假曲线。

import type { MetricPoint } from "../api/types";
import type { RawMetricPoint } from "../api/extension";
import { formatPercent } from "./format";

export type SeriesUnit = "cents" | "count" | "ratio";

export interface SeriesPoint {
  date: string;
  /** null = 该日无值（缺数据或分母为 0），图表断开而不是画成 0 */
  value: number | null;
  /**
   * 显示 value 时必须一起说清的前提（目前只有毛利率的"覆盖率"）。
   * 跟值同挂一个点而不是另开并行数组：并行数组一旦顺序错位，就会把 A 日的口径贴到 B 日的数上。
   * null / 省略 = 该点无需附加前提。
   */
  qualifier?: string | null;
}

export interface MetricSeries {
  key: string;
  label: string;
  unit: SeriesUnit;
  description: string;
  points: SeriesPoint[];
  /**
   * value 为 null 时卡片显示的文案（说明"为什么没有"，而不是含糊的 "—"）。
   * 毛利率用它写「未配置成本」：把 null 显示成 0% 或 "—" 都会让人当成"毛利为零/数据没刷新"。
   */
  nullLabel?: string;
  /**
   * 卡片头条值取哪个点：
   * - 省略（默认）"latest-valued"：最后一个**有值**的点。趋势卡惯例，也用于环比（跳过 null 才比得起来）。
   * - "newest"：最后一个点，哪怕它是 null。毛利率用它——最新一天没配成本时必须显示「未配置成本」，
   *   而不是把上周那个有值的数当成今天的（卡片上的日期会跟着变旧，但"最新一天到底有没有数"这件事
   *   不该只靠一个不起眼的日期去猜）。
   */
  headline?: "latest-valued" | "newest";
}

export interface MetricGap {
  key: string;
  label: string;
  missingFields: string[];
  reason: string;
  affectedDays: number;
}

export interface DashboardModel {
  primary: MetricSeries[];
  secondary: MetricSeries[];
  gaps: MetricGap[];
  notes: string[];
  missingDates: string[];
}

/** gross_margin 为 null 时的卡片文案：null = 没配成本 / 没归集 / 分母为 0，绝不是"毛利 0%"。 */
export const MARGIN_NULL_LABEL = "未配置成本";

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * 毛利率：**直接读后端字段 `gross_margin`**（§7.5），前端不做任何再计算。
 * 返回 null 表示这一天后端算不出（没配成本 / 没按 SKU 归集 / 有成本成交额为 0），
 * 由 nullLabel 显示「未配置成本」；绝不在这里用 0 兜底。
 */
function grossMargin(point: MetricPoint): number | null {
  return num(point.gross_margin);
}

/**
 * 毛利率的口径前提：这个数只覆盖"有成本的 SKU"，所以必须同时给出它覆盖了多少成交额。
 * - 覆盖率 null → 覆盖率未知，无法判断这数代表多少成交额
 * - 覆盖率 == 0 → "一个成本都没配"，不是"很小"（见下方单独分支的理由）
 * - 覆盖率 < 1（且 > 0）→ "仅覆盖 X% 成交额"（这是它敢显示这个数的前提）
 * - 覆盖率 > 1 → 如实说明上游汇总与订单明细对不上（契约§5.3 要求不裁剪，也不许按 ≤100% 格式化）
 * - 覆盖率 == 1 → 没有前提要补，返回 null
 */
function coverageQualifier(point: MetricPoint): string | null {
  const ratio = num(point.cost_coverage_ratio);
  if (ratio === null) {
    return "成本覆盖率未给出（全店 GMV 为 0 或未按 SKU 归集），无法判断这个毛利率代表多少成交额";
  }
  if (ratio < 0) {
    return `成本覆盖率为负（${formatPercent(ratio, 2)}），上游数据异常`;
  }
  if (ratio === 0) {
    // 为什么与 "< 1" 分开：`coverage = 0` 的语义是"一个成本都没配 / 没归集"，不是"覆盖率很小"。
    // 合规后端下 coverage=0 ⟹ gross_margin 必为 null ⟹ 卡片走 nullLabel「未配置成本」，
    // 本分支根本到不了；能到这儿说明后端违约（给了非 null 的毛利率）。这时更要直说，
    // 不能让「仅覆盖 0.00% 成交额」这种轻描淡写的说法被读成"只是覆盖得少一点"。
    return "成本覆盖率为 0：一个成本都没配（或未按 SKU 归集成交额）——这个毛利率不代表任何成交额，出现非 null 值说明后端违约";
  }
  if (ratio < 1) return `仅覆盖 ${formatPercent(ratio, 2)} 成交额`;
  if (ratio > 1) {
    return `覆盖 ${formatPercent(ratio, 2)} 成交额（> 100%：上游汇总与订单明细对不上，契约要求如实显示、不裁剪）`;
  }
  return null;
}

/** 库存周转（日均次数）= 当日销量 / 当日库存件数。缺库存或库存为 0 → null（不是 Infinity）。 */
function turnoverRate(point: RawMetricPoint): number | null {
  const inventory = num(point.inventory_units);
  if (inventory === null || inventory <= 0) return null;
  return point.paid_order_count / inventory;
}

/**
 * 组装一条序列。
 * qualifier 可选：只有需要"值 + 口径前提"成对展示的序列（毛利率）才传；其余序列的点不带 qualifier。
 * 值本身为 null 时不挂 qualifier——此时该说的事由 nullLabel 说，避免"未配置成本 + 仅覆盖 0%"这种叠字。
 */
function series(
  key: string,
  label: string,
  unit: SeriesUnit,
  description: string,
  points: MetricPoint[],
  pick: (p: MetricPoint) => number | null,
  qualifier?: (p: MetricPoint) => string | null,
): MetricSeries {
  return {
    key,
    label,
    unit,
    description,
    points: points.map((p) => {
      const value = pick(p);
      if (value === null || qualifier === undefined) return { date: p.date, value };
      return { date: p.date, value, qualifier: qualifier(p) };
    }),
  };
}

function nonNullCount(s: MetricSeries): number {
  return s.points.reduce((acc, p) => (p.value === null ? acc : acc + 1), 0);
}

/** 业务日期序列里缺了哪些天（契约 §7.5：缺就是缺，不补零）。 */
export function findMissingDates(points: MetricPoint[]): string[] {
  if (points.length < 2) return [];
  const present = new Set(points.map((p) => p.date));
  const sorted = points.map((p) => p.date).slice().sort();
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  if (!first || !last) return [];
  const start = Date.parse(`${first}T00:00:00Z`);
  const end = Date.parse(`${last}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return [];
  const missing: string[] = [];
  for (let t = start + 86_400_000; t < end; t += 86_400_000) {
    const date = new Date(t).toISOString().slice(0, 10);
    if (!present.has(date)) missing.push(date);
  }
  return missing;
}

export function deriveDashboard(points: MetricPoint[]): DashboardModel {
  const raw = points as RawMetricPoint[];
  const gaps: MetricGap[] = [];
  const notes: string[] = [];

  // 毛利率：只做"取值 + 口径提示"，不做任何计算（口径由后端 §5.3 决定，前端算就是对不上）。
  const marginSeries = series(
    "gross_margin",
    "毛利率",
    "ratio",
    "毛利率 = 后端字段 gross_margin（店铺级：只对**有成本的 SKU** 加权 = (Σ有成本成交额 − Σ对应成本) / Σ有成本成交额）；须与 cost_coverage_ratio 成对读",
    points,
    grossMargin,
    coverageQualifier,
  );
  marginSeries.nullLabel = MARGIN_NULL_LABEL;
  // 头条值取"最后一个点"而不是"最后一个有值点"：最新一天 gross_margin=null 时卡片必须显示
  // 「未配置成本」，不能拿更早那天的数冒充今天（这正是"含糊过去"最典型的形态）。
  marginSeries.headline = "newest";
  const salesSeries = series("paid_order_count", "销量（支付订单数）", "count", "当日支付订单数", raw, (p) =>
    num(p.paid_order_count),
  );
  const turnoverSeries = series(
    "inventory_turnover",
    "库存周转（日均次数）",
    "count",
    "库存周转 = 当日销量 / 当日库存件数",
    raw,
    turnoverRate,
  );

  // 毛利率**不再是"契约缺口"**：两个字段都已入契约（§7.5）。值为 null 是"这家店没配成本"这个
  // 业务事实，卡片用 nullLabel 说清楚；把它报成"数据源缺字段"会让运维去查网络/后端，方向就错了。
  if (raw.length > 0 && nonNullCount(turnoverSeries) === 0) {
    gaps.push({
      key: "inventory_turnover",
      label: "库存周转",
      missingFields: ["inventory_units"],
      reason:
        "契约 §7.5 的 MetricPoint 没有库存字段，库存周转无法计算。后端补上 points[].inventory_units（SKU 库存求和）后本卡片自动恢复。",
      affectedDays: raw.length,
    });
  }

  if (nonNullCount(marginSeries) > 0) {
    notes.push(
      "毛利率直接取自后端字段 gross_margin（CONTRACT §7.5 / §5.3：店铺级、只对有成本的 SKU 加权）。" +
        "前端不再用成本字段自算，也不读契约外的 cost_cents——同一个中文名不允许有两种口径。",
    );
  }
  const marginNullDays = raw.length - nonNullCount(marginSeries);
  if (raw.length > 0 && nonNullCount(marginSeries) === 0) {
    notes.push(
      `毛利率：区间内 ${raw.length} 天的 gross_margin 都是 null —— 这家店没配 SKU 成本表（或没按 SKU 归集），` +
        `卡片显示「${MARGIN_NULL_LABEL}」。null 不是 0%："不知道"和"不赚不亏"是两件事。`,
    );
  } else if (marginNullDays > 0) {
    notes.push(
      `有 ${marginNullDays} 天 gross_margin 为 null（未配置成本 / 未按 SKU 归集 / 有成本成交额为 0），` +
        `这些天的曲线断开、不画成 0；若最新一天就是 null，卡片头条显示「${MARGIN_NULL_LABEL}」，` +
        "不会拿更早那天的数冒充今天（看卡片上的日期）。",
    );
  }
  // 覆盖率必须可见：只报"毛利率 −50%"而不说覆盖率，会被读成"全店在亏"（§7.5 要求成对读）。
  const coverages = raw
    .map((p) => num(p.cost_coverage_ratio))
    .filter((r): r is number => r !== null);
  const partialCoverage = coverages.filter((r) => r < 1);
  if (partialCoverage.length > 0) {
    notes.push(
      `有 ${partialCoverage.length} 天 cost_coverage_ratio < 1（最低 ${formatPercent(Math.min(...partialCoverage), 2)} 成交额）：` +
        "这些天的毛利率只覆盖部分成交额，卡片上标注「仅覆盖 X% 成交额」，不能读成全店结论。",
    );
  }
  const overCoverage = coverages.filter((r) => r > 1);
  if (overCoverage.length > 0) {
    notes.push(
      `有 ${overCoverage.length} 天 cost_coverage_ratio > 1（最高 ${formatPercent(Math.max(...overCoverage), 2)}）：` +
        "上游汇总与订单明细对不上，契约要求如实显示、不裁剪，也不要按「必然 ≤ 100%」格式化。",
    );
  }
  if (nonNullCount(turnoverSeries) > 0) {
    // 说清来源：库存周转用的字段不在 §7.5 的 MetricPoint 里，是后端带的扩展字段。
    // 不写这一句，看图的人会以为契约支持它，等后端只按契约返回时就变成"看板坏了"。
    notes.push(
      "库存周转使用了契约外的扩展字段 inventory_units（§7.5 的 MetricPoint 不含库存字段）；" +
        "后端不提供时该卡片会变成「数据源缺字段」提示，而不是画一条假曲线。",
    );
  }
  const partialInventory = raw.length - nonNullCount(turnoverSeries);
  if (nonNullCount(turnoverSeries) > 0 && partialInventory > 0) {
    notes.push(`有 ${partialInventory} 天缺 inventory_units 或库存为 0，这些天的库存周转断点不画。`);
  }

  const nullRefundRate = raw.filter((p) => p.refund_rate === null).length;
  if (nullRefundRate > 0) {
    notes.push(`有 ${nullRefundRate} 天 refund_rate 为 null（当日支付订单为 0，分母为 0 → 契约要求返回 null 而不是 0）。`);
  }

  const missingDates = findMissingDates(points);
  if (missingDates.length > 0) {
    notes.push(
      `区间内 ${missingDates.length} 天没有数据（${missingDates.join("、")}）：契约 §7.5 规定不补零，趋势图按真实日期断点绘制。`,
    );
  }

  const secondary: MetricSeries[] = [
    series("gmv_cents", "GMV", "cents", "当日成交金额（整数分）", raw, (p) => num(p.gmv_cents)),
    series("aov_cents", "客单价", "cents", "客单价 = GMV / 支付订单数", raw, (p) => num(p.aov_cents)),
    series("refund_rate", "退款率", "ratio", "退款率 = 退款订单数 / 支付订单数", raw, (p) => num(p.refund_rate)),
    series("refund_amount_cents", "退款金额", "cents", "当日退款金额（整数分）", raw, (p) => num(p.refund_amount_cents)),
    series("uv", "访客数 UV", "count", "当日店铺访客数", raw, (p) => num(p.uv)),
    series("cvr", "转化率 CVR", "ratio", "转化率 = 支付订单数 / 访客数", raw, (p) => num(p.cvr)),
    series(
      "gmv_7d_avg_cents",
      "GMV 近 7 日均值",
      "cents",
      "只取日期 < 今天的、最多 7 条历史；不足 3 条为 null",
      raw,
      (p) => num(p.gmv_7d_avg_cents),
    ),
  ];

  return { primary: [marginSeries, salesSeries, turnoverSeries], secondary, gaps, notes, missingDates };
}

/** 卡片摘要：最新值 + 上一有值点（用于环比）。null 值不参与"最新值"的选取。 */
export function summarize(s: MetricSeries): { latest: SeriesPoint | null; previous: SeriesPoint | null } {
  const valued = s.points.filter((p) => p.value !== null);
  const latest = valued[valued.length - 1] ?? null;
  const previous = valued[valued.length - 2] ?? null;
  return { latest, previous };
}

/** 整段区间的最小/最大值（跳过 null），给 SVG 定纵轴。 */
export function extent(s: MetricSeries): { min: number; max: number } | null {
  const values = s.points.map((p) => p.value).filter((v): v is number => v !== null);
  if (values.length === 0) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) return { min: min * 0.98, max: max === 0 ? 1 : max * 1.02 };
  return { min, max };
}
