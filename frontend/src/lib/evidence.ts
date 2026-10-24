// evidence 里的指标展示：审批卡片要把"凭什么提这个动作"讲清楚，
// 所以键名要翻译成人话、金额要按 _cents 规则换算、未知键原样展示（不吞掉）。

import { formatCents, formatCount, formatDecimal, formatPercent } from "./format";

const LABELS: Record<string, string> = {
  gmv_cents: "GMV",
  gmv_7d_avg_cents: "GMV 近 7 日均值",
  current_price_cents: "当前价格",
  paid_order_count: "支付订单数",
  paid_order_count_7d: "近 7 日支付订单数",
  aov_cents: "客单价",
  refund_rate: "退款率",
  refund_amount_cents: "退款金额",
  stock: "当前库存",
  stock_days: "库存天数",
  order_count: "订单数",
  uv: "访客数",
  cvr: "转化率",
};

export interface EvidenceEntry {
  key: string;
  label: string;
  value: string;
}

/** 值 → 展示串。规则与 CONTRACT §0 一致：_cents 除以 100；比例按百分比；null → "—"。 */
export function formatEvidenceValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (key.endsWith("_cents")) return formatCents(value);
    if (key.endsWith("_rate") || key === "cvr") return formatPercent(value);
    if (Number.isInteger(value)) return formatCount(value);
    return formatDecimal(value, 2);
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

/** 把 evidence.metrics 之类的扁平对象摊成有序展示项。 */
export function evidenceEntries(obj: Record<string, unknown> | null | undefined): EvidenceEntry[] {
  if (!obj) return [];
  return Object.entries(obj).map(([key, value]) => ({
    key,
    label: LABELS[key] ?? key,
    value: formatEvidenceValue(key, value),
  }));
}

/** evidence 里除 metrics 之外的标量键（anomaly_id / rule_id 等），用于"证据来源"一行。 */
export function evidenceScalars(obj: Record<string, unknown> | null | undefined): EvidenceEntry[] {
  if (!obj) return [];
  const out: EvidenceEntry[] = [];
  for (const [key, value] of Object.entries(obj)) {
    if (value !== null && typeof value === "object") continue;
    out.push({ key, label: LABELS[key] ?? key, value: formatEvidenceValue(key, value) });
  }
  return out;
}

export function evidenceMetrics(obj: Record<string, unknown> | null | undefined): Record<string, unknown> {
  if (!obj) return {};
  const metrics = obj["metrics"];
  return metrics !== null && typeof metrics === "object" && !Array.isArray(metrics)
    ? (metrics as Record<string, unknown>)
    : {};
}
