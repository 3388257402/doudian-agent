// 浏览器内的假后端：按 CONTRACT §7 实现 GET /api/alerts、GET /api/metrics、GET /api/logs、
// POST /api/approve、POST /api/agent/run、GET /api/healthz。
//
// 为什么不是"每个页面读 JSON 直接渲染"：
//   - 那样审批页永远走不到 400/409 分支，错误 UI 就没被验证过；
//   - 裁决后列表不会变，看不到"已裁决 → 列表消失 + 日志新增"的闭环。
// 因此这里实现了一个有状态的假后端：裁决会改状态、追加日志，重复裁决返回 409 DUPLICATE_DECISION，
// 参数不合法返回 400 VALIDATION_ERROR，参数形状完全按 §5.5 校验（与编辑表单共用 paramSchema）。
//
// 它**不是**后端替身：不实现鉴权、不实现真实幂等键、不实现重试预算。真实行为以 backend/ 为准。

import alertsFixture from "../../mock/alerts.json";
import metricsFixture from "../../mock/metrics.json";
import metricsExtendedFixture from "../../mock/metrics.extended.json";
import logsFixture from "../../mock/logs.json";
import type {
  AlertsResponse,
  ApiErrorCode,
  AppliedDecision,
  ApproveResponse,
  DecisionInput,
  EffectReport,
  LogItem,
  LogsResponse,
  MetricPoint,
  MetricsResponse,
  PendingApproval,
  RunResponse,
} from "../api/types";
import type { ApiRequest, ApiTransport, TransportResponse } from "../api/transport";
import { envelope } from "../api/transport";
import { validateParams } from "../lib/paramSchema";
import { getMockFlags } from "./flags";

const SHOP = "12345678";
const APPROVAL_SEED = alertsFixture as unknown as AlertsResponse;
const METRICS_SEED = metricsFixture as unknown as MetricsResponse;
// 契约外扩展字段（inventory_units）只在 mock 开关打开时才回给前端，
// 见 src/mock/flags.ts 的说明：默认响应必须与 §7.5 的真实形状一致。
// 毛利率/成本覆盖率是**契约字段**（§7.5），两个 fixtures 里都已经有，不受开关影响。
const METRICS_EXTENDED_SEED = metricsExtendedFixture as unknown as MetricsResponse;
const LOGS_SEED = logsFixture as unknown as LogsResponse;

const OPERATOR_RE = /^[A-Za-z0-9_.-]{1,64}$/;
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

interface DecisionRecord {
  verdict: DecisionInput["verdict"];
  params: Record<string, unknown>;
  operator: string;
  decided_at: string;
}

function nowIso(): string {
  return new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1Z");
}

function traceId(): string {
  return fakeHex(32);
}

/** 确定性 hex（xorshift 伪随机，仅用于 mock 的 id 外观；不是密码学哈希）。 */
function fakeHex(len: number, seed = Date.now()): string {
  let s = seed >>> 0 || 0x9e3779b9;
  let out = "";
  while (out.length < len) {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    s >>>= 0;
    out += s.toString(16).padStart(8, "0");
  }
  return out.slice(0, len);
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

interface MockState {
  approvals: PendingApproval[];
  decisions: Map<string, DecisionRecord>;
  logs: LogItem[];
  nextSeq: number;
  metrics: MetricsResponse;
  runCounter: number;
}

function initialState(): MockState {
  const maxSeq = LOGS_SEED.items.reduce((max, item) => Math.max(max, item.seq), 0);
  return {
    approvals: APPROVAL_SEED.items.map((item) => ({ ...item })),
    decisions: new Map(),
    logs: LOGS_SEED.items.map((item) => ({ ...item })),
    nextSeq: maxSeq + 1,
    metrics: METRICS_SEED,
    runCounter: 0,
  };
}

const state: MockState = initialState();

function appendLog(entry: Omit<LogItem, "seq" | "ts">): void {
  state.logs.push({ ...entry, seq: state.nextSeq++, ts: nowIso() });
}

function pendingFor(shopId: string | null): PendingApproval[] {
  return state.approvals
    .filter((a) => (shopId === null ? true : a.shop_id === shopId))
    .filter((a) => !state.decisions.has(a.action_id))
    .map((a) => ({ ...a }));
}

function statusFor(actionId: string): PendingApproval["status"] {
  const decided = state.decisions.get(actionId);
  if (!decided) return "pending";
  return decided.verdict === "reject" ? "rejected" : "approved";
}

function jsonResponse(status: number, body: unknown, trace: string): TransportResponse {
  return { status, body, headers: { "x-trace-id": trace, "content-type": "application/json; charset=utf-8" } };
}

function bad(
  code: ApiErrorCode,
  message: string,
  details: Record<string, unknown> | null,
  status: number,
  trace: string,
): TransportResponse {
  return envelope(status, code, message, details, trace);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function buildEffectReport(runId: string): EffectReport {
  const base = Date.now();
  return {
    run_id: runId,
    generated_at: new Date(base).toISOString().replace(/\.\d{3}Z$/, "Z"),
    attribution: "single_arm_before_after",
    windows: [1, 3, 7].map((offset) => ({
      offset_days: offset,
      due_at: new Date(base + offset * 86_400_000).toISOString().replace(/\.\d{3}Z$/, "Z"),
      snapshot_id: fakeHex(32, base + offset),
      status: "scheduled" as const,
    })),
    conclusion: "效果窗口已登记，尚未到达评估时间（最早 T+1）。",
    caveats: ["无对照组，指标变化可能来自季节性/活动/竞品等外部因素", "归因强度：弱（单臂前后对比）"],
  };
}

function actionLogIdFor(decision: DecisionInput, operator: string): string {
  // 真实后端是 sha256(idempotency_key)；mock 只需要稳定的 64 位 hex 外观。
  return fakeHex(64, hashSeed(`${decision.action_id}|${JSON.stringify(decision.edited_params)}|${operator}`));
}

function hashSeed(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h;
}

// ——————————————————— 路由 ———————————————————

function handleAlerts(req: ApiRequest, trace: string): TransportResponse {
  const shopId = typeof req.query?.shop_id === "string" && req.query.shop_id.length > 0 ? req.query.shop_id : null;
  const status = typeof req.query?.status === "string" ? req.query.status : "pending";
  const limit = typeof req.query?.limit === "number" ? Math.min(Math.max(req.query.limit, 1), 200) : 50;

  if (shopId !== null && !ID_RE.test(shopId)) {
    return bad("VALIDATION_ERROR", "shop_id 不合法", { shop_id: shopId }, 400, trace);
  }

  let items = state.approvals.filter((a) => (shopId === null ? true : a.shop_id === shopId));
  if (status !== "all") {
    items = items.filter((a) => statusFor(a.action_id) === status);
  }
  items = items
    .slice()
    .sort((a, b) => (a.created_at === b.created_at ? 0 : a.created_at < b.created_at ? 1 : -1))
    .slice(0, limit)
    .map((a) => ({ ...a, status: statusFor(a.action_id) }));

  return jsonResponse(200, { items, next_cursor: null } satisfies AlertsResponse, trace);
}

function handleMetrics(req: ApiRequest, trace: string): TransportResponse {
  const shopId = typeof req.query?.shop_id === "string" ? req.query.shop_id : "";
  if (!shopId) return bad("VALIDATION_ERROR", "缺少必填参数 shop_id", { shop_id: "missing" }, 400, trace);
  if (!ID_RE.test(shopId)) return bad("VALIDATION_ERROR", "shop_id 不合法", { shop_id: shopId }, 400, trace);

  const dateRe = /^\d{4}-\d{2}-\d{2}$/;
  const dateFrom = typeof req.query?.date_from === "string" ? req.query.date_from : null;
  const dateTo = typeof req.query?.date_to === "string" ? req.query.date_to : null;
  if (dateFrom !== null && !dateRe.test(dateFrom)) {
    return bad("VALIDATION_ERROR", "date_from 必须是 YYYY-MM-DD", { date_from: dateFrom }, 400, trace);
  }
  if (dateTo !== null && !dateRe.test(dateTo)) {
    return bad("VALIDATION_ERROR", "date_to 必须是 YYYY-MM-DD", { date_to: dateTo }, 400, trace);
  }
  if (dateFrom !== null && dateTo !== null && dateFrom > dateTo) {
    return bad("VALIDATION_ERROR", "区间倒置：date_from 晚于 date_to", { date_from: dateFrom, date_to: dateTo }, 400, trace);
  }

  if (shopId !== state.metrics.shop_id) {
    return bad("RESOURCE_NOT_FOUND", `店铺 ${shopId} 没有指标数据`, { shop_id: shopId }, 404, trace);
  }

  // 默认按 §7.5 的真实形状返回；只有显式打开 Mock 扩展开关时，才附上契约外的 inventory_units。
  const source = getMockFlags().extendedMetrics ? METRICS_EXTENDED_SEED : state.metrics;
  const points = source.points.filter((p: MetricPoint) => {
    if (dateFrom !== null && p.date < dateFrom) return false;
    if (dateTo !== null && p.date > dateTo) return false;
    return true;
  });
  return jsonResponse(200, { ...source, points } satisfies MetricsResponse, trace);
}

function handleLogs(req: ApiRequest, trace: string): TransportResponse {
  const runId = typeof req.query?.run_id === "string" && req.query.run_id.length > 0 ? req.query.run_id : null;
  const level = typeof req.query?.level === "string" && req.query.level.length > 0 ? req.query.level : null;
  const eventPrefix = typeof req.query?.event === "string" && req.query.event.length > 0 ? req.query.event : null;
  const limit = typeof req.query?.limit === "number" ? Math.min(Math.max(req.query.limit, 1), 500) : 100;
  const beforeSeq = typeof req.query?.before_seq === "number" ? req.query.before_seq : null;

  let items = state.logs.slice().sort((a, b) => b.seq - a.seq);
  if (runId !== null) items = items.filter((i) => i.run_id === runId);
  if (level !== null) items = items.filter((i) => i.level === level);
  if (eventPrefix !== null) items = items.filter((i) => i.event.startsWith(eventPrefix));
  if (beforeSeq !== null) items = items.filter((i) => i.seq < beforeSeq);

  const page = items.slice(0, limit);
  const last = page[page.length - 1];
  const hasMore = items.length > page.length && last !== undefined;
  return jsonResponse(200, { items: page, next_cursor: hasMore && last ? String(last.seq) : null } satisfies LogsResponse, trace);
}

function handleApprove(req: ApiRequest, trace: string): TransportResponse {
  const body = asRecord(req.body);
  if (!body) return bad("VALIDATION_ERROR", "请求体必须是 JSON 对象", null, 400, trace);

  const threadId = body["thread_id"];
  const interruptId = body["interrupt_id"];
  const operator = body["operator"];
  const rawDecisions = body["decisions"];

  // —— 第 1 段：请求体 schema（§7.4 字段表）——
  const fieldErrors: Record<string, unknown> = {};
  if (typeof threadId !== "string" || threadId.length === 0) fieldErrors["thread_id"] = "必填字符串";
  if (typeof interruptId !== "string" || interruptId.length === 0) fieldErrors["interrupt_id"] = "必填字符串";
  if (typeof operator !== "string" || !OPERATOR_RE.test(operator)) fieldErrors["operator"] = "必须匹配 ^[A-Za-z0-9_.-]{1,64}$";
  if (!Array.isArray(rawDecisions) || rawDecisions.length === 0) fieldErrors["decisions"] = "至少 1 条裁决";
  if (Object.keys(fieldErrors).length > 0) {
    return bad("VALIDATION_ERROR", "请求体字段校验失败", fieldErrors, 400, trace);
  }
  // 显式再收窄一次：上面把错误记进 fieldErrors 的写法不会让 TS 保留类型收窄。
  if (typeof threadId !== "string" || typeof interruptId !== "string" || typeof operator !== "string") {
    return bad("VALIDATION_ERROR", "请求体字段校验失败", fieldErrors, 400, trace);
  }

  const decisions: DecisionInput[] = [];
  for (const [index, raw] of (rawDecisions as unknown[]).entries()) {
    const item = asRecord(raw);
    if (!item) return bad("VALIDATION_ERROR", `decisions[${index}] 必须是对象`, null, 400, trace);
    const actionId = item["action_id"];
    const verdict = item["verdict"];
    const edited = item["edited_params"];
    if (typeof actionId !== "string" || actionId.length === 0) {
      return bad("VALIDATION_ERROR", `decisions[${index}].action_id 必填`, null, 400, trace);
    }
    if (verdict !== "approve" && verdict !== "reject" && verdict !== "edit") {
      return bad("VALIDATION_ERROR", `decisions[${index}].verdict 必须是 approve/reject/edit`, { verdict }, 400, trace);
    }
    const editedRecord = edited === null || edited === undefined ? null : asRecord(edited);
    if (edited !== null && edited !== undefined && editedRecord === null) {
      return bad("VALIDATION_ERROR", `decisions[${index}].edited_params 必须是对象或 null`, null, 400, trace);
    }
    // §4.4 规则 4/5：edit 必须带非空 edited_params；非 edit 必须为 null。
    if (verdict === "edit" && (editedRecord === null || Object.keys(editedRecord).length === 0)) {
      return bad("VALIDATION_ERROR", `decisions[${index}]: verdict=edit 必须提供非空 edited_params`, {
        action_id: actionId,
      }, 400, trace);
    }
    if (verdict !== "edit" && editedRecord !== null) {
      return bad("VALIDATION_ERROR", `decisions[${index}]: verdict=${verdict} 时 edited_params 必须为 null`, {
        action_id: actionId,
      }, 400, trace);
    }
    const reason = item["reason"];
    const itemOperator = item["operator"];
    decisions.push({
      action_id: actionId,
      verdict,
      edited_params: editedRecord,
      operator: typeof itemOperator === "string" ? itemOperator : operator,
      reason: typeof reason === "string" ? reason : null,
    });
  }

  // —— 第 2 段：thread / interrupt（404）——
  const threadApprovals = state.approvals.filter((a) => a.thread_id === threadId);
  if (threadApprovals.length === 0) {
    return bad("THREAD_NOT_FOUND", `thread ${threadId} 在 checkpointer 中不存在`, { thread_id: threadId }, 404, trace);
  }
  const expectedInterrupt = threadApprovals[0]?.interrupt_id ?? "";
  if (expectedInterrupt !== interruptId) {
    return bad(
      "APPROVAL_NOT_FOUND",
      "提交的 interrupt_id 与当前挂起中断不一致（可能已被他人裁决）",
      { expected_interrupt_id: expectedInterrupt, got: interruptId },
      404,
      trace,
    );
  }

  // —— 第 3 段：裁决集合（§4.4 规则 2：不多、不少、不重复）——
  const pendingIds = new Set(threadApprovals.filter((a) => !state.decisions.has(a.action_id)).map((a) => a.action_id));
  const submittedIds = decisions.map((d) => d.action_id);
  const submittedSet = new Set(submittedIds);
  if (submittedSet.size !== submittedIds.length) {
    const seen = new Set<string>();
    const dupes = submittedIds.filter((id) => (seen.has(id) ? true : (seen.add(id), false)));
    return bad("VALIDATION_ERROR", "decisions 含重复 action_id", { duplicate_action_ids: [...new Set(dupes)] }, 400, trace);
  }
  const unknown = submittedIds.filter((id) => !pendingIds.has(id));
  if (unknown.length > 0) {
    const decidedOnes = unknown.filter((id) => state.decisions.has(id));
    if (decidedOnes.length === unknown.length) {
      return bad("DUPLICATE_DECISION", "该动作已裁决", { action_ids: decidedOnes }, 409, trace);
    }
    return bad("VALIDATION_ERROR", "decisions 含不属于当前 pending_actions 的 action_id", {
      unknown_action_ids: unknown,
    }, 400, trace);
  }
  const missing = [...pendingIds].filter((id) => !submittedSet.has(id));
  if (missing.length > 0) {
    return bad(
      "VALIDATION_ERROR",
      `decisions 缺少 ${missing.length} 个 action_id 的裁决（同一 thread 必须整批裁决）`,
      { missing_action_ids: missing },
      400,
      trace,
    );
  }

  // —— 第 4 段：参数 schema（§5.5）——
  for (const decision of decisions) {
    if (decision.verdict !== "edit") continue;
    const approval = threadApprovals.find((a) => a.action_id === decision.action_id);
    if (!approval) continue;
    const validated = validateParams(approval.action_type, decision.edited_params);
    if (!validated.ok) {
      return bad("VALIDATION_ERROR", `edited_params 不符合 ${approval.action_type} 的参数 schema`, {
        action_id: decision.action_id,
        params: validated.errors,
      }, 400, trace);
    }
    decision.edited_params = validated.value;
  }

  // —— 第 5 段：落裁决 ——
  const decidedAt = nowIso();
  const applied: AppliedDecision[] = decisions.map((decision) => {
    const effectiveParams =
      decision.verdict === "edit" && decision.edited_params ? decision.edited_params : null;
    const approval = threadApprovals.find((a) => a.action_id === decision.action_id);
    const params = effectiveParams ?? approval?.params ?? {};
    state.decisions.set(decision.action_id, {
      verdict: decision.verdict,
      params,
      operator: decision.operator ?? operator,
      decided_at: decidedAt,
    });
    if (decision.verdict === "reject") {
      return { action_id: decision.action_id, verdict: decision.verdict, status: "rejected", action_log_id: null, error: null };
    }
    const logId = actionLogIdFor(decision, operator);
    appendLog({
      level: "info",
      event: "action.executed",
      run_id: approval?.run_id ?? null,
      trace_id: approval?.trace_id ?? trace,
      shop_id: approval?.shop_id ?? null,
      node: "execute",
      message: `mock: ${decision.verdict === "edit" ? "参数已改，按生效参数执行" : "按原参数执行"}`,
      data: { action_id: decision.action_id, params, action_log_id: logId, operator },
    });
    return { action_id: decision.action_id, verdict: decision.verdict, status: "succeeded", action_log_id: logId, error: null };
  });

  const runId = threadApprovals[0]?.run_id ?? "";
  appendLog({
    level: "info",
    event: "approval.decided",
    run_id: runId,
    trace_id: threadApprovals[0]?.trace_id ?? trace,
    shop_id: threadApprovals[0]?.shop_id ?? null,
    node: "approval_gate",
    message: `${decisions.length} 条裁决已落库`,
    data: { thread_id: threadId, interrupt_id: interruptId, operator, verdicts: decisions.map((d) => d.verdict) },
  });

  const remaining = pendingFor(null).filter((a) => a.thread_id === threadId);
  const response: ApproveResponse = {
    thread_id: threadId,
    run_id: runId,
    trace_id: threadApprovals[0]?.trace_id ?? trace,
    status: "done",
    applied,
    pending_approvals: remaining,
    effect_report: buildEffectReport(runId),
  };
  return jsonResponse(200, response, trace);
}

function handleRunAgent(req: ApiRequest, trace: string): TransportResponse {
  const body = asRecord(req.body) ?? {};
  const shopId = body["shop_id"];
  if (typeof shopId !== "string" || !ID_RE.test(shopId)) {
    return bad("VALIDATION_ERROR", "shop_id 不合法", { shop_id: shopId ?? null }, 400, trace);
  }
  const providedRunId = body["run_id"];
  if (providedRunId !== null && providedRunId !== undefined) {
    if (typeof providedRunId !== "string" || !ID_RE.test(providedRunId)) {
      return bad("VALIDATION_ERROR", "run_id 不合法", { run_id: providedRunId }, 400, trace);
    }
    const existing = state.approvals.find((a) => a.run_id === providedRunId);
    if (existing) {
      // 同一 (shop_id, run_id) 正在运行 → 409（契约 §7.1 RUN_IN_PROGRESS）
      return bad("RUN_IN_PROGRESS", "该 (shop_id, run_id) 正在运行", { run_id: providedRunId }, 409, trace);
    }
  }

  state.runCounter += 1;
  const runId = typeof providedRunId === "string" ? providedRunId : `run_${new Date().toISOString().replace(/[-:]/g, "").slice(0, 15)}Z_${fakeHex(8)}`;
  const threadId = `${shopId}:${runId}`;
  const interruptId = fakeHex(16);
  const newTrace = trace;
  const actionId = `${runId}:price_update:3400000000000000007`;

  const approval: PendingApproval = {
    action_id: actionId,
    run_id: runId,
    thread_id: threadId,
    shop_id: shopId,
    trace_id: newTrace,
    interrupt_id: interruptId,
    action_type: "price_update",
    target: "3400000000000000007",
    params: { price_cents: 8900 },
    reason: `mock 第 ${state.runCounter} 轮：退款率 5.4% 高于阈值，试探性降价 5% 观察转化`,
    evidence: {
      anomaly_id: fakeHex(16),
      rule_id: "refund_rate_high",
      metrics: { refund_rate: 0.054, current_price_cents: 9400, paid_order_count: 302, aov_cents: 39600 },
    },
    risk: "medium",
    status: "pending",
    created_at: nowIso(),
  };
  state.approvals.push(approval);
  appendLog({
    level: "info",
    event: "collect.started",
    run_id: runId,
    trace_id: newTrace,
    shop_id: shopId,
    node: "collect",
    message: `run started (dry_run=${body["dry_run"] === true ? "true" : "false"})`,
    data: { trigger: body["trigger"] ?? "manual", dry_run: body["dry_run"] === true },
  });
  appendLog({
    level: "info",
    event: "approval.requested",
    run_id: runId,
    trace_id: newTrace,
    shop_id: shopId,
    node: "approval_gate",
    message: "挂起等待人工审批（1 条动作）",
    data: { proposals: 1, notifier: "dry_run" },
  });

  const response: RunResponse = {
    run_id: runId,
    thread_id: threadId,
    trace_id: newTrace,
    status: "awaiting_approval",
    pending_approvals: [approval],
    effect_report: null,
  };
  return jsonResponse(202, response, trace);
}

function handleHealthz(trace: string): TransportResponse {
  return jsonResponse(
    200,
    {
      status: "ok",
      version: "0.1.0-mock",
      db: "ok",
      checkpointer: "ok",
      time: nowIso(),
    },
    trace,
  );
}

class MockTransport implements ApiTransport {
  readonly kind = "mock" as const;

  async send(req: ApiRequest, signal: AbortSignal): Promise<TransportResponse> {
    if (!req.path.startsWith("/api/")) throw new Error(`mock 拒绝非 /api/* 路径: ${req.path}`);
    const trace = traceId();
    // 模拟网络时延，让 loading / 轮询状态是真实可见的，而不是瞬间完成。
    await sleep(90 + Math.floor(Math.random() * 160), signal);

    const key = `${req.method} ${req.path}`;
    switch (key) {
      case "GET /api/alerts":
        return handleAlerts(req, trace);
      case "GET /api/metrics":
        return handleMetrics(req, trace);
      case "GET /api/logs":
        return handleLogs(req, trace);
      case "POST /api/approve":
        return handleApprove(req, trace);
      case "POST /api/agent/run":
        return handleRunAgent(req, trace);
      case "GET /api/healthz":
        return handleHealthz(trace);
      default:
        return bad("RESOURCE_NOT_FOUND", `mock 未实现 ${key}`, { path: req.path, method: req.method }, 404, trace);
    }
  }
}

let singleton: MockTransport | null = null;

/** mock 传输层单例：审批状态与日志在页面切换之间保持一致。 */
export function getMockTransport(): MockTransport {
  singleton ??= new MockTransport();
  return singleton;
}

/** 仅测试/调试用：重置假后端状态。 */
export function resetMockState(): void {
  const fresh = initialState();
  state.approvals = fresh.approvals;
  state.decisions = fresh.decisions;
  state.logs = fresh.logs;
  state.nextSeq = fresh.nextSeq;
  state.metrics = fresh.metrics;
  state.runCounter = fresh.runCounter;
}

export const MOCK_SHOP_ID = SHOP;
