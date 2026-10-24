// 状态/风险/等级的小胶囊。颜色语义集中在这里，页面不各写一套。

export type PillTone = "neutral" | "info" | "ok" | "warn" | "bad" | "muted";

export interface StatusPillProps {
  label: string;
  tone?: PillTone;
  title?: string;
}

export function StatusPill({ label, tone = "neutral", title }: StatusPillProps): JSX.Element {
  return (
    <span className={`pill pill-${tone}`} title={title}>
      {label}
    </span>
  );
}

/** CONTRACT §2 的 Risk = low/medium/high */
export function riskTone(risk: "low" | "medium" | "high"): PillTone {
  return risk === "high" ? "bad" : risk === "medium" ? "warn" : "ok";
}

export function riskLabel(risk: "low" | "medium" | "high"): string {
  return risk === "high" ? "高风险" : risk === "medium" ? "中风险" : "低风险";
}

/** CONTRACT §2 的 Severity = info/warn/critical */
export function severityTone(severity: "info" | "warn" | "critical"): PillTone {
  return severity === "critical" ? "bad" : severity === "warn" ? "warn" : "info";
}

export function levelTone(level: "debug" | "info" | "warn" | "error"): PillTone {
  return level === "error" ? "bad" : level === "warn" ? "warn" : level === "debug" ? "muted" : "info";
}

export function approvalStatusTone(status: "pending" | "approved" | "rejected" | "expired"): PillTone {
  switch (status) {
    case "pending":
      return "warn";
    case "approved":
      return "ok";
    case "rejected":
      return "bad";
    case "expired":
      return "muted";
  }
}

export function approvalStatusLabel(status: "pending" | "approved" | "rejected" | "expired"): string {
  switch (status) {
    case "pending":
      return "待审批";
    case "approved":
      return "已通过";
    case "rejected":
      return "已拒绝";
    case "expired":
      return "已过期";
  }
}

/** CONTRACT §2 的 ActionType */
export function actionTypeLabel(actionType: "price_update" | "stock_update" | "coupon_create"): string {
  switch (actionType) {
    case "price_update":
      return "改价";
    case "stock_update":
      return "改库存";
    case "coupon_create":
      return "发券";
  }
}
