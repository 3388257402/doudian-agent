// 逐字照抄 docs/CONTRACT.md §12（契约 v1.2.0，2026-10-23 冻结）。
// 规则：JSON 键与后端 Python 字段名 1:1 相同（snake_case），此处不做 camelCase 转换；
// 字面量联合类型用枚举字符串；金额一律整数分且字段名以 _cents 结尾；"没有"用 null 而非缺键。
// 改这里之前必须先改 CONTRACT.md 并升版本（§13 变更流程）。

export type RunStatus =
  | "running" | "awaiting_approval" | "executing" | "done" | "failed" | "cancelled";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";
export type ActionType = "price_update" | "stock_update" | "coupon_create";
export type Verdict = "approve" | "reject" | "edit";
export type Severity = "info" | "warn" | "critical";
export type Risk = "low" | "medium" | "high";
export type Attribution = "single_arm_before_after" | "ab_test" | "none";

export type ApiErrorCode =
  | "VALIDATION_ERROR" | "THREAD_NOT_FOUND" | "APPROVAL_NOT_FOUND" | "RESOURCE_NOT_FOUND"
  | "DUPLICATE_DECISION" | "RUN_IN_PROGRESS" | "RUN_STATE_CONFLICT"
  | "UPSTREAM_ERROR" | "INTERNAL_ERROR";

export interface ApiError {
  error: {
    code: ApiErrorCode;
    message: string;
    details: Record<string, unknown> | null;
    trace_id: string;
  };
}

export type Money = number;   // 整数分，字段名以 _cents 结尾

export interface PendingApproval {
  action_id: string;
  run_id: string;
  thread_id: string;
  shop_id: string;
  trace_id: string;
  interrupt_id: string;
  action_type: ActionType;
  target: string;
  params: Record<string, unknown>;
  reason: string;
  evidence: Record<string, unknown>;
  risk: Risk;
  status: ApprovalStatus;
  created_at: string;
}

export interface RunResponse {
  run_id: string;
  thread_id: string;
  trace_id: string;
  status: RunStatus;
  pending_approvals: PendingApproval[];
  effect_report: EffectReport | null;
}

export interface DecisionInput {
  action_id: string;
  verdict: Verdict;
  edited_params: Record<string, unknown> | null;
  operator?: string | null;
  reason?: string | null;
}

export interface ApproveRequest {
  thread_id: string;
  interrupt_id: string;
  operator: string;
  decisions: DecisionInput[];
}

export interface AppliedDecision {
  action_id: string;
  verdict: Verdict;
  status: "pending" | "succeeded" | "failed" | "unknown" | "skipped_duplicate" | "rejected";
  action_log_id: string | null;
  error: string | null;
}

export interface EffectWindow {
  offset_days: number;
  due_at: string;
  snapshot_id: string;
  status: "scheduled" | "evaluated" | "missed";
}

export interface EffectReport {
  run_id: string;
  generated_at: string;
  attribution: Attribution;
  windows: EffectWindow[];
  conclusion: string;
  caveats: string[];
}

export interface ApproveResponse {
  thread_id: string;
  run_id: string;
  trace_id: string;
  status: RunStatus;
  applied: AppliedDecision[];
  pending_approvals: PendingApproval[];
  effect_report: EffectReport | null;
}

export interface AlertsResponse {
  items: PendingApproval[];
  next_cursor: string | null;
}

export interface MetricPoint {
  date: string;                 // YYYY-MM-DD (Asia/Shanghai)
  gmv_cents: Money;
  order_count: number;
  paid_order_count: number;
  refund_order_count: number;
  refund_amount_cents: Money;
  aov_cents: Money;
  refund_rate: number | null;
  uv: number | null;
  cvr: number | null;
  gmv_7d_avg_cents: Money | null;
  computed_at: string;
}

export interface MetricsResponse {
  shop_id: string;
  granularity: "day";
  unit: { money: "cent"; date: string; ratio: string };
  points: MetricPoint[];
}

export interface LogItem {
  seq: number;
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  event: string;
  run_id: string | null;
  trace_id: string | null;
  shop_id: string | null;
  node: string | null;
  message: string;
  data: Record<string, unknown>;
}

export interface LogsResponse {
  items: LogItem[];
  next_cursor: string | null;
}
