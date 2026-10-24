// 契约缺口（不是偏离，是显式声明）：docs/CONTRACT.md §7.5 的 MetricPoint 里没有库存字段，
// 因此"库存周转"**无法**从 GET /api/metrics 的冻结字段推导出来。
//
// 处理方式（都不做假数据）：
//   1. 本文件声明"可选扩展字段"：后端若在 points[] 里多带这些键，前端就能画出库存周转；
//   2. 后端未带这些键时，看板不画假曲线，改为显示明确的"数据源缺字段"提示（见 lib/metrics.ts 的 gaps）。
//
// ⚠️ 成本/毛利率**不在本文件里**（v1.5.0 起）：`gross_margin` / `cost_coverage_ratio` 已是契约字段
// （§7.5 / §12），由后端按店铺级加权口径算出。前端曾在这里声明扩展字段 `cost_cents` 并自算
// (GMV − 退款 − 成本) / (GMV − 退款)，与后端口径不是同一个数——那条路径已整体删除，
// **不要**再把成本字段加回这里：同一个中文名"毛利率"不允许并存两套口径。
//
// 这些字段**不在** types.ts（那是 §12 的逐字拷贝，禁止加东西），只在派生层使用。

import type { MetricPoint } from "./types";

export interface MetricPointExtension {
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
