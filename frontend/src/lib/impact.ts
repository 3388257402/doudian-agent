// "影响预估"——审批卡片上最容易被追问的一块。
// 原则：只展示**能从证据算出来**的量，算不出来的（例如降价对 GMV 的弹性）明确说算不出来，
// 不用一个看起来专业的假数字糊过去。

import type { PendingApproval, Risk } from "../api/types";
import { formatCents, formatCount, formatPercent, safeMultiply } from "./format";

export interface ImpactLine {
  label: string;
  value: string;
  tone: "neutral" | "warn" | "bad";
}

export interface ImpactAssessment {
  lines: ImpactLine[];
  /** 无法量化/需要人工判断的部分，逐条写清楚 */
  notes: string[];
  /** 按 CONTRACT §5.5 的公式从参数反推的风险档（用于和后端 risk 交叉核对） */
  riskFromParams: Risk | null;
  /** 后端 risk 与 §5.5 公式不一致时为 true —— 说明存在契约外的风险规则，值得追问 */
  riskMismatch: boolean;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function metricsOf(approval: PendingApproval): Record<string, unknown> {
  const metrics = approval.evidence["metrics"];
  return metrics !== null && typeof metrics === "object" && !Array.isArray(metrics)
    ? (metrics as Record<string, unknown>)
    : {};
}

/** CONTRACT §5.5 的 risk 判定（冻结公式）。 */
function riskFromPriceChange(currentCents: number | null, nextCents: number | null): Risk | null {
  if (currentCents === null || nextCents === null || currentCents <= 0) return null;
  const drop = (currentCents - nextCents) / currentCents;
  if (drop > 0.15) return "high";
  if (drop > 0.05) return "medium";
  return "low";
}

export function assessImpact(approval: PendingApproval): ImpactAssessment {
  const metrics = metricsOf(approval);
  const lines: ImpactLine[] = [];
  const notes: string[] = [];
  let riskFromParams: Risk | null = null;

  if (approval.action_type === "price_update") {
    const current = num(metrics["current_price_cents"]);
    const next = num(approval.params["price_cents"]);
    lines.push({ label: "现价", value: formatCents(current), tone: "neutral" });
    lines.push({ label: "建议新价", value: formatCents(next), tone: "neutral" });
    if (current !== null && next !== null && current > 0) {
      const drop = (current - next) / current;
      lines.push({
        label: "价格变动",
        value: `${drop >= 0 ? "降价" : "提价"} ${formatPercent(Math.abs(drop))}`,
        tone: Math.abs(drop) > 0.15 ? "bad" : Math.abs(drop) > 0.05 ? "warn" : "neutral",
      });
      riskFromParams = riskFromPriceChange(current, next);
    } else {
      notes.push("evidence 里没有 current_price_cents，无法计算变动幅度与风险档。");
    }
    notes.push("GMV 影响无法由现有证据量化：缺少价格弹性数据，本卡片不做弹性外推。");
  } else if (approval.action_type === "stock_update") {
    const currentStock = num(metrics["stock"]);
    const nextStock = num(approval.params["stock"]);
    lines.push({ label: "当前库存", value: currentStock === null ? "—" : `${formatCount(currentStock)} 件`, tone: "neutral" });
    lines.push({ label: "目标库存", value: nextStock === null ? "—" : `${formatCount(nextStock)} 件`, tone: "neutral" });
    if (currentStock !== null && nextStock !== null) {
      const diff = nextStock - currentStock;
      lines.push({
        label: "库存变化",
        value: `${diff > 0 ? "+" : ""}${formatCount(diff)} 件`,
        tone: diff < 0 ? "warn" : "neutral",
      });
      if (nextStock === 0) {
        lines.push({ label: "业务含义", value: "该 SKU 将不可售（下架/断货）", tone: "bad" });
        notes.push("库存置 0 等价于停售：后续订单、评价、搜索权重都会受影响，属于不可逆操作里较重的那个。");
      }
    }
    const stockDays = num(metrics["stock_days"]);
    if (stockDays !== null) {
      lines.push({
        label: "库存天数",
        value: `${stockDays.toFixed(1)} 天`,
        tone: stockDays < 3 ? "bad" : stockDays < 7 ? "warn" : "neutral",
      });
    }
    riskFromParams = "low"; // §5.5：stock_update 恒 low
  } else {
    const discount = num(approval.params["discount_cents"]);
    const total = num(approval.params["total_count"]);
    lines.push({ label: "券面额", value: formatCents(discount), tone: "neutral" });
    lines.push({ label: "发放张数", value: total === null ? "—" : `${formatCount(total)} 张`, tone: "neutral" });
    const cap = discount !== null && total !== null ? safeMultiply(discount, total) : null;
    lines.push({
      label: "让利上限",
      value: cap === null ? "—（金额乘法溢出/缺参数）" : formatCents(cap),
      tone: "warn",
    });
    notes.push("让利上限按「全部核销」计算，是成本上界，不是预估成本；实际成本 = 核销量 × 面额。");
    notes.push("券带来的增量 GMV 依赖投放与人群，本卡片不做预估。");
    riskFromParams = "high"; // §5.5：coupon_create 恒 high
  }

  const riskMismatch = riskFromParams !== null && riskFromParams !== approval.risk;
  if (riskMismatch) {
    notes.push(
      `后端给出的 risk=${approval.risk}，按 §5.5 冻结公式反推应为 ${riskFromParams}：说明存在契约外的风险规则或参数被改过，需向后端确认。`,
    );
  }

  return { lines, notes, riskFromParams, riskMismatch };
}
