// 契约缺口（不是偏离，是显式声明）：docs/CONTRACT.md §7.5 的 MetricPoint 里没有成本与库存字段，
// 因此"毛利率"与"库存周转"**无法**从 GET /api/metrics 的冻结字段推导出来。
//
// 处理方式（三选一都不做假数据）：
//   1. 本文件声明"可选扩展字段"：后端若在 points[] 里多带这些键，前端就能画出毛利率/库存周转；
//   2. 后端未带这些键时，看板不画假曲线，改为显示明确的"数据源缺字段"提示（见 lib/metrics.ts 的 gaps）；
//   3. 已向 spec-lead 提议在 CONTRACT §7.5/§12 增补字段并升版本；版本落地后把这里合并进 types.ts。
//
// 这些字段**不在** types.ts（那是 §12 的逐字拷贝，禁止加东西），只在派生层使用。
// 金额仍是整数分、字段名仍以 _cents 结尾。

import type { MetricPoint } from "./types";

export interface MetricPointExtension {
  /**
   * 当日商品成本（COGS，整数分）。毛利率 = (gmv_cents - refund_amount_cents - cost_cents) / (gmv_cents - refund_amount_cents)
   * 提议来源：抖店 order.searchList 的 sku 级成本；后端 compute_metrics 可聚合。
   */
  cost_cents?: number;
  /**
   * 当日可售库存件数（SKU 库存求和，时点快照取当日 23:59 Asia/Shanghai）。
   * 库存周转率（日）= paid_order_count / 平均库存件数；库存周转天数 = 1 / 该值。
   */
  inventory_units?: number;
}

/** 契约字段 + 可选扩展字段的运行时视图。缺失的扩展字段是 undefined，不是 0。 */
export type MetricPointWithExt = MetricPoint & MetricPointExtension;

/** 后端实际返回的 points 元素（JSON 里可能带扩展键，类型上仍以 §12 为准）。 */
export type RawMetricPoint = MetricPoint & Partial<MetricPointExtension>;
