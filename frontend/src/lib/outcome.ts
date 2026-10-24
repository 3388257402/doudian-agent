// node.trace 事件的读取语义（CONTRACT §7.6.1，v1.2.0 冻结）。
//
// 单独成文件的原因：这一层是"契约怎么读"的判定规则，页面只是渲染。
// 拆开后 scripts/verify-suite.ts 能直接断言三态映射（包括"缺键按 ok""未知值原样显示"），
// 而不用去 DOM 里猜。

import type { LogItem } from "../api/types";

export type OutcomeTone = "ok" | "warn" | "bad" | "neutral";

export interface RunTraceSummary {
  runId: string;
  /** node.trace 条数 —— 按 §7.6.1 不变量等于 agent_traces.step_count（不区分 outcome） */
  steps: number;
  ok: number;
  interrupted: number;
  error: number;
  /** duration_ms 求和；全部缺失时为 null（不是 0） */
  totalDurationMs: number | null;
  lastTs: string | null;
}

/** data.outcome 是否真的存在于事件里。 */
export function hasOutcome(row: LogItem): boolean {
  const raw = row.data["outcome"];
  return typeof raw === "string" && raw.length > 0;
}

/**
 * 读取 data.outcome。缺失 → "ok"。
 * 理由：§7.6.1 把它定为必填，缺失只可能是后端还没升到 v1.2.0；此时把一条正常完成的节点
 * 标红的风险大于把失败标成 ok（后者仍有 level=error 可见）。未知值则原样返回，不猜。
 */
export function outcomeOf(row: LogItem): string {
  const raw = row.data["outcome"];
  if (typeof raw === "string" && raw.length > 0) return raw;
  return "ok";
}

/** data.duration_ms；不是有限数字 → null，展示为 "—"（"没有字段" ≠ "耗时 0ms"）。 */
export function durationOf(row: LogItem): number | null {
  const raw = row.data["duration_ms"];
  return typeof raw === "number" && Number.isFinite(raw) && raw >= 0 ? raw : null;
}

export function digestOf(row: LogItem, key: "input_digest" | "output_digest"): string | null {
  const raw = row.data[key];
  return typeof raw === "string" && raw.length > 0 ? raw : null;
}

/** error 只在 outcome="error" 时非空，形状固定 {type, message}（§7.6.1）。 */
export function errorOf(row: LogItem): { type: string; message: string } | null {
  const raw = row.data["error"];
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const type = typeof record["type"] === "string" ? record["type"] : "UnknownError";
  const message = typeof record["message"] === "string" ? record["message"] : "";
  return { type, message };
}

/** 三态的展示文案：interrupted 说人话（「等待审批」），而不是把内部枚举丢给运维看。 */
export function outcomeLabel(outcome: string): string {
  switch (outcome) {
    case "ok":
      return "ok";
    case "interrupted":
      return "等待审批";
    case "error":
      return "error";
    default:
      return outcome;
  }
}

export function outcomeTone(outcome: string): OutcomeTone {
  switch (outcome) {
    case "ok":
      return "ok";
    // 黄色中性态：interrupt 是正常控制流，不是故障（§7.6.1）。
    case "interrupted":
      return "warn";
    case "error":
      return "bad";
    default:
      return "neutral";
  }
}

export function outcomeTitle(outcome: string): string {
  switch (outcome) {
    case "ok":
      return "data.outcome=ok：节点正常完成";
    case "interrupted":
      return "data.outcome=interrupted：HITL 挂起，等待人工裁决（正常控制流，不是错误）";
    case "error":
      return "data.outcome=error：节点执行失败";
    default:
      return `data.outcome=${outcome}：不在 §7.6.1 的取值域内，原样显示`;
  }
}

/**
 * 从已加载的日志行派生 run 摘要。
 * agent_traces 是表、没有 REST 接口，所以这里不编造接口，而是按 §7.6.1 的不变量聚合 node.trace。
 * 注意：计数只覆盖"当前已加载的行"，筛选/分页会改变它 —— 调用方必须把这句话展示出去。
 */
export function deriveRunSummaries(rows: LogItem[]): RunTraceSummary[] {
  const byRun = new Map<string, RunTraceSummary>();
  for (const row of rows) {
    if (row.event !== "node.trace" || row.run_id === null) continue;
    let summary = byRun.get(row.run_id);
    if (!summary) {
      summary = { runId: row.run_id, steps: 0, ok: 0, interrupted: 0, error: 0, totalDurationMs: null, lastTs: null };
      byRun.set(row.run_id, summary);
    }
    summary.steps += 1;
    const outcome = outcomeOf(row);
    if (outcome === "ok") summary.ok += 1;
    else if (outcome === "interrupted") summary.interrupted += 1;
    else if (outcome === "error") summary.error += 1;
    const duration = durationOf(row);
    if (duration !== null) summary.totalDurationMs = (summary.totalDurationMs ?? 0) + duration;
    if (summary.lastTs === null || row.ts > summary.lastTs) summary.lastTs = row.ts;
  }
  // 出问题的 run 排前面，其余按 run_id 倒序（新的在前）。
  return [...byRun.values()].sort((a, b) => {
    const severity = (s: RunTraceSummary): number => (s.error > 0 ? 0 : s.interrupted > 0 ? 1 : 2);
    const diff = severity(a) - severity(b);
    return diff !== 0 ? diff : a.runId < b.runId ? 1 : -1;
  });
}
