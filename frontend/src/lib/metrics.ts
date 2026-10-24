// 看板派生层：把 GET /api/metrics 的 points 变成可画的序列。
//
// 一条重要的诚实性约束：契约 §7.5 的 MetricPoint **没有**成本与库存字段，
// 所以"毛利率"与"库存周转"无法从冻结字段算出。这里不造假曲线：
//   - 后端若带了可选扩展字段（cost_cents / inventory_units，见 api/extension.ts），就画；
//   - 没带，就产出 MetricGap，由页面显示"数据源缺字段"，并把缺口原样写进 notes。

import type { MetricPoint } from "../api/types";
import type { RawMetricPoint } from "../api/extension";

export type SeriesUnit = "cents" | "count" | "ratio";

export interface SeriesPoint {
  date: string;
  /** null = 该日无值（缺数据或分母为 0），图表断开而不是画成 0 */
  value: number | null;
}

export interface MetricSeries {
  key: string;
  label: string;
  unit: SeriesUnit;
  description: string;
  points: SeriesPoint[];
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

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 毛利率 = (净收入 - 成本) / 净收入，净收入 = GMV - 退款金额。缺成本或缺分母 → null。 */
function grossMargin(point: RawMetricPoint): number | null {
  const cost = num(point.cost_cents);
  if (cost === null) return null;
  const net = point.gmv_cents - point.refund_amount_cents;
  if (!Number.isFinite(net) || net <= 0) return null;
  return (net - cost) / net;
}

/** 库存周转（日均次数）= 当日销量 / 当日库存件数。缺库存或库存为 0 → null（不是 Infinity）。 */
function turnoverRate(point: RawMetricPoint): number | null {
  const inventory = num(point.inventory_units);
  if (inventory === null || inventory <= 0) return null;
  return point.paid_order_count / inventory;
}

function series(
  key: string,
  label: string,
  unit: SeriesUnit,
  description: string,
  points: RawMetricPoint[],
  pick: (p: RawMetricPoint) => number | null,
): MetricSeries {
  return { key, label, unit, description, points: points.map((p) => ({ date: p.date, value: pick(p) })) };
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

  const marginSeries = series(
    "gross_margin",
    "毛利率",
    "ratio",
    "毛利率 = (GMV − 退款金额 − 成本) / (GMV − 退款金额)",
    raw,
    grossMargin,
  );
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

  if (raw.length > 0 && nonNullCount(marginSeries) === 0) {
    gaps.push({
      key: "gross_margin",
      label: "毛利率",
      missingFields: ["cost_cents"],
      reason:
        "契约 §7.5 的 MetricPoint 没有成本字段，毛利率无法计算。后端补上 points[].cost_cents（或等价的 COGS 聚合）后本卡片自动恢复。",
      affectedDays: raw.length,
    });
  }
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

  const partialCost = raw.length - nonNullCount(marginSeries);
  if (nonNullCount(marginSeries) > 0 && partialCost > 0) {
    notes.push(`有 ${partialCost} 天缺 cost_cents，这些天的毛利率断点不画（不是 0）。`);
  }
  if (nonNullCount(marginSeries) > 0 || nonNullCount(turnoverSeries) > 0) {
    // 说清楚来源：这两条曲线用到的字段不在 §7.5 的 MetricPoint 里，是后端带的扩展字段。
    // 不写这一句，看图的人会以为契约支持它们，等后端只按契约返回时就变成"看板坏了"。
    const used: string[] = [];
    if (nonNullCount(marginSeries) > 0) used.push("cost_cents（成本，用于毛利率）");
    if (nonNullCount(turnoverSeries) > 0) used.push("inventory_units（库存件数，用于库存周转）");
    notes.push(
      `毛利率/库存周转使用了契约外的扩展字段：${used.join("、")}。§7.5 的 MetricPoint 不含这两个字段，` +
        "后端不提供时对应卡片会变成「数据源缺字段」提示，而不是画一条假曲线。",
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
