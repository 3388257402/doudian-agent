// 前端契约自检套件（纯 Node，无测试框架依赖）。
//
// 断言的是"契约正文里写死的字段/枚举/映射"，不是"我们的实现看起来对"：
//   §7.4  审批提交体字段逐字 + edit 必带 edited_params / reject 必须 null
//   §7.1  错误按 error.code 分支，不按 HTTP 状态猜
//   §0    金额整数分展示 /100 两位；null 显示 "—"
//   §5.5  参数 schema 校验（edit 的参数在本地先挡一次）
//   §7.6.1 node.trace 的 data.outcome 三态 + level↔outcome 硬映射 + error/output_digest 规则
//   §7.5  毛利率**直读**契约字段 gross_margin（不许再自算）+ 必须与 cost_coverage_ratio 成对展示；
//         `null`（未配置成本）与 `0`（不赚不亏）严格区分，0 不得折成 null 显示；
//         库存周转仍只能从契约外扩展字段派生（缺字段不画假曲线）
//
// 由 scripts/verify.mjs 用 esbuild 打包后执行（它负责把 TS/JSON 变成可 import 的模块）。

import alertsFixture from "../mock/alerts.json";
import logsFixture from "../mock/logs.json";
import metricsFixture from "../mock/metrics.json";
import metricsExtendedFixture from "../mock/metrics.extended.json";
import { ApiClient } from "../src/api/client";
import { ApiFailure } from "../src/api/errors";
import type { ApiTransport, ApiRequest, TransportResponse } from "../src/api/transport";
import { envelope } from "../src/api/transport";
import type {
  AlertsResponse,
  ApproveResponse,
  LogItem,
  LogsResponse,
  MetricPoint,
  MetricsResponse,
  PendingApproval,
} from "../src/api/types";
import { buildApproveRequest, type DecisionDraft } from "../src/lib/approve";
import { formatCents, formatDuration, formatPercent } from "../src/lib/format";
import { deriveDashboard, MARGIN_NULL_LABEL, summarize } from "../src/lib/metrics";
import {
  deriveRunSummaries,
  durationOf,
  errorOf,
  hasOutcome,
  outcomeLabel,
  outcomeOf,
  outcomeTone,
} from "../src/lib/outcome";
import { draftFromParams } from "../src/lib/paramSchema";
import { getMockTransport, resetMockState } from "../src/mock/mockServer";

export interface CheckResult {
  name: string;
  ok: boolean;
  detail: string;
}

const results: CheckResult[] = [];

function check(name: string, fn: () => void | Promise<void>): void {
  try {
    const out = fn();
    if (out instanceof Promise) throw new Error("check() 不支持异步回调，请用 checkAsync");
    results.push({ name, ok: true, detail: "" });
  } catch (err) {
    results.push({ name, ok: false, detail: err instanceof Error ? err.message : String(err) });
  }
}

const pending: { name: string; fn: () => Promise<void> }[] = [];

/**
 * 异步检查**按注册顺序串行执行**：mock 假后端是模块级单例（有状态），
 * 并发跑会让 A 的裁决污染 B 的断言（这一步踩过：Promise.all 把 3 条待审批跑成了 1 条）。
 */
function checkAsync(name: string, fn: () => Promise<void>): void {
  pending.push({ name, fn });
}

function fail(message: string): never {
  throw new Error(message);
}

function assert(condition: boolean, message: string): void {
  if (!condition) fail(message);
}

function assertEqual(actual: unknown, expected: unknown, label: string): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) fail(`${label}: 期望 ${e}，实际 ${a}`);
}

function assertDeepKeys(obj: object, expectedKeys: string[], label: string): void {
  assertEqual(Object.keys(obj).slice().sort(), expectedKeys.slice().sort(), `${label} 的键集合`);
}

// ————————————————————————— fixtures —————————————————————————

const ALERTS = alertsFixture as unknown as AlertsResponse;
const METRICS = metricsFixture as unknown as MetricsResponse;
const LOGS = logsFixture as unknown as LogsResponse;

const priceApproval = ALERTS.items.find((item) => item.action_type === "price_update") ?? fail("fixtures 里没有 price_update");
const couponApproval =
  ALERTS.items.find((item) => item.action_type === "coupon_create") ?? fail("fixtures 里没有 coupon_create");

function draftFor(approval: PendingApproval, verdict: DecisionDraft["verdict"], reason = ""): DecisionDraft {
  return { verdict, draft: draftFromParams(approval.action_type, approval.params), reason };
}

// ————————————————————————— §7.4 提交体 —————————————————————————

check("§7.4 提交体顶层只有 thread_id/interrupt_id/operator/decisions", () => {
  const built = buildApproveRequest({
    threadId: "12345678:run_x",
    interruptId: "6f1e9c2a7b3d4e5f",
    operator: "ops_zhang",
    items: [priceApproval, couponApproval],
    decisionOf: (a) => draftFor(a, a.action_id === priceApproval.action_id ? "edit" : "reject", "理由"),
  });
  assert(built.ok, "应当构造成功");
  if (!built.ok) return;
  assertDeepKeys(built.body, ["thread_id", "interrupt_id", "operator", "decisions"], "body");
  assertDeepKeys(
    built.body.decisions[0] as object,
    ["action_id", "verdict", "edited_params", "operator", "reason"],
    "decisions[0]",
  );
});

check("§7.4 verdict=edit 必须带非空 edited_params（且是校验后的类型）", () => {
  const built = buildApproveRequest({
    threadId: "t",
    interruptId: "i",
    operator: "ops_zhang",
    items: [priceApproval],
    decisionOf: () => ({ verdict: "edit", draft: { price_cents: "9700" }, reason: "降幅收敛到 3%" }),
  });
  assert(built.ok, "应当构造成功");
  if (!built.ok) return;
  const decision = built.body.decisions[0];
  assert(decision !== undefined, "缺少 decisions[0]");
  if (!decision) return;
  assertEqual(decision.edited_params, { price_cents: 9700 }, "edited_params（int 已从表单字符串转成数字）");
});

check("§7.4 verdict=reject / approve 的 edited_params 必须为 null（不是 {}、不是省略）", () => {
  for (const verdict of ["reject", "approve"] as const) {
    const built = buildApproveRequest({
      threadId: "t",
      interruptId: "i",
      operator: "ops_zhang",
      items: [couponApproval],
      decisionOf: (a) => draftFor(a, verdict, ""),
    });
    assert(built.ok, `${verdict} 应当构造成功`);
    if (!built.ok) continue;
    const decision = built.body.decisions[0];
    assert(decision !== undefined, "缺少 decisions[0]");
    if (!decision) continue;
    assert(decision.edited_params === null, `${verdict} 的 edited_params 必须是 null，实际 ${JSON.stringify(decision.edited_params)}`);
    assert("edited_params" in decision, `${verdict} 的 edited_params 键不能省略`);
  }
});

check("§0 空理由写 null，不写空字符串", () => {
  const built = buildApproveRequest({
    threadId: "t",
    interruptId: "i",
    operator: "ops_zhang",
    items: [priceApproval],
    decisionOf: () => draftFor(priceApproval, "approve", "   "),
  });
  assert(built.ok, "应当构造成功");
  if (!built.ok) return;
  assert(built.body.decisions[0]?.reason === null, "空白理由应归一为 null");
});

check("§4.4 少一条裁决 → 不产出提交体，并给出 missing_action_ids", () => {
  const built = buildApproveRequest({
    threadId: "t",
    interruptId: "i",
    operator: "ops_zhang",
    items: [priceApproval, couponApproval],
    decisionOf: (a) => (a.action_id === priceApproval.action_id ? draftFor(a, "approve") : draftFor(a, null)),
  });
  assert(!built.ok, "未全部裁决时不得构造提交体");
  if (built.ok) return;
  assertEqual(built.missingActionIds, [couponApproval.action_id], "missing_action_ids");
});

check("§5.5 edit 参数非法（price_cents=0 / 小数）→ 本地拒绝提交", () => {
  for (const bad of ["0", "-100", "9.9", "", "abc"]) {
    const built = buildApproveRequest({
      threadId: "t",
      interruptId: "i",
      operator: "ops_zhang",
      items: [priceApproval],
      decisionOf: () => ({ verdict: "edit", draft: { price_cents: bad }, reason: "" }),
    });
    assert(!built.ok, `price_cents=${JSON.stringify(bad)} 必须被拒绝`);
    if (built.ok) continue;
    assert(built.paramErrors[priceApproval.action_id]?.["price_cents"] !== undefined, "缺少 price_cents 字段错误");
  }
});

// ————————————————————————— §7.1 错误码 —————————————————————————

function fakeTransport(res: TransportResponse): ApiTransport {
  return { kind: "http", send: async (_req: ApiRequest) => res };
}

function clientWith(res: TransportResponse): ApiClient {
  return new ApiClient(fakeTransport(res), { timeoutMs: 2_000 });
}

async function expectFailure(action: () => Promise<unknown>, label: string): Promise<ApiFailure> {
  try {
    await action();
  } catch (err) {
    assert(err instanceof ApiFailure, `${label}：应当抛 ApiFailure，实际 ${String(err)}`);
    return err as ApiFailure;
  }
  return fail(`${label}：本应失败却成功了`);
}

checkAsync("§7.1 同一个 HTTP 409 下按 code 区分（DUPLICATE_DECISION ≠ RUN_STATE_CONFLICT）", async () => {
  const dup = await expectFailure(
    () => clientWith(envelope(409, "DUPLICATE_DECISION", "已裁决", null, "t1")).approve({ thread_id: "t", interrupt_id: "i", operator: "o", decisions: [] }),
    "DUPLICATE_DECISION",
  );
  const conflict = await expectFailure(
    () => clientWith(envelope(409, "RUN_STATE_CONFLICT", "状态冲突", null, "t2")).approve({ thread_id: "t", interrupt_id: "i", operator: "o", decisions: [] }),
    "RUN_STATE_CONFLICT",
  );
  assertEqual(dup.code, "DUPLICATE_DECISION", "409 的第一个 code");
  assertEqual(conflict.code, "RUN_STATE_CONFLICT", "409 的第二个 code");
});

checkAsync("§7.1 HTTP 500 + UPSTREAM_ERROR 保留 UPSTREAM_ERROR（不被状态码改写成 INTERNAL_ERROR）", async () => {
  const failure = await expectFailure(
    () => clientWith(envelope(500, "UPSTREAM_ERROR", "上游挂了", null, "t3")).listAlerts(),
    "UPSTREAM_ERROR",
  );
  assertEqual(failure.code, "UPSTREAM_ERROR", "code");
  assertEqual(failure.traceId, "t3", "trace_id 透传");
});

checkAsync("§7.1 契约外 code / 缺包络 → MALFORMED_RESPONSE（不猜）", async () => {
  const weird = await expectFailure(
    () => clientWith(envelope(400, "SOMETHING_NEW" as never, "?", null, "t4")).listAlerts(),
    "契约外 code",
  );
  assertEqual(weird.code, "MALFORMED_RESPONSE", "契约外 code");
  const noEnvelope = await expectFailure(
    () => clientWith({ status: 502, body: "<html>bad gateway</html>", headers: {} }).listAlerts(),
    "无包络",
  );
  assertEqual(noEnvelope.code, "MALFORMED_RESPONSE", "缺 §7.1 包络");
  assertEqual(noEnvelope.httpStatus, 502, "httpStatus 只作为诊断字段保留");
});

// ————————————————————————— §0 金额/比例展示 —————————————————————————

check("§0 金额整数分 → /100 保留两位；null → —", () => {
  assertEqual(formatCents(9600), "¥96.00", "9600 分");
  assertEqual(formatCents(12840000), "¥128,400.00", "12840000 分");
  assertEqual(formatCents(0), "¥0.00", "0 分");
  assertEqual(formatCents(null), "—", "null");
  assertEqual(formatPercent(0.034591), "3.46%", "refund_rate");
  assertEqual(formatPercent(null), "—", "分母为 0 → null 显示 —，不是 0.00%");
  assertEqual(formatDuration(42.7), "42.7 ms", "duration_ms < 1s");
  assertEqual(formatDuration(3011.4), "3.01 s", "duration_ms > 1s");
  assertEqual(formatDuration(null), "—", "duration_ms 缺失不是 0");
});

// ————————————————————————— §7.6.1 fixtures 形状 —————————————————————————

check("§7.6.1 每条 node.trace 的 data 形状合规（outcome/duration_ms/error/output_digest）", () => {
  const traces = LOGS.items.filter((item) => item.event === "node.trace");
  assert(traces.length >= 5, `fixtures 至少要覆盖 5 条 node.trace，实际 ${traces.length}`);
  const outcomesSeen = new Set<string>();
  for (const item of traces) {
    const data = item.data;
    const outcome = data["outcome"];
    assert(typeof outcome === "string", `seq=${item.seq} 缺 data.outcome`);
    if (typeof outcome !== "string") continue;
    outcomesSeen.add(outcome);
    assert(
      outcome === "ok" || outcome === "interrupted" || outcome === "error",
      `seq=${item.seq} outcome=${outcome} 不在三态取值域内`,
    );
    assert(typeof data["duration_ms"] === "number", `seq=${item.seq} 缺 data.duration_ms（不是 elapsed_ms）`);
    // level ↔ outcome 硬映射
    const expectedLevel = outcome === "error" ? "error" : "info";
    assertEqual(item.level, expectedLevel, `seq=${item.seq} 的 level↔outcome 映射`);
    // error 只在 outcome="error" 时非空
    if (outcome === "error") {
      const err = data["error"];
      assert(err !== null && typeof err === "object", `seq=${item.seq} outcome=error 必须有 error 对象`);
      const record = err as Record<string, unknown>;
      assert(typeof record["type"] === "string", `seq=${item.seq} error.type 必须是字符串`);
    } else {
      assertEqual(data["error"], null, `seq=${item.seq} outcome=${outcome} 的 error 必须为 null`);
    }
    // output_digest 在非 ok 时为 null
    if (outcome === "ok") {
      assert(typeof data["output_digest"] === "string", `seq=${item.seq} outcome=ok 应有 output_digest`);
    } else {
      assertEqual(data["output_digest"], null, `seq=${item.seq} outcome=${outcome} 的 output_digest 必须为 null`);
    }
    assert(typeof data["input_digest"] === "string", `seq=${item.seq} 缺 input_digest`);
    // message 取值固定三种
    assert(
      item.message === "node ok" ||
        item.message === "node interrupted (awaiting human decision)" ||
        item.message === "node failed",
      `seq=${item.seq} message=${JSON.stringify(item.message)} 不在冻结的三种取值里`,
    );
  }
  assertEqual([...outcomesSeen].sort(), ["error", "interrupted", "ok"], "fixtures 覆盖 ok/interrupted/error 三态");
});

check("§7.6.1 不变量：同一 run 的 node.trace 条数 == 派生出的步数", () => {
  const perRun = new Map<string, number>();
  for (const item of LOGS.items) {
    if (item.event !== "node.trace" || item.run_id === null) continue;
    perRun.set(item.run_id, (perRun.get(item.run_id) ?? 0) + 1);
  }
  assert(perRun.size >= 3, `fixtures 至少覆盖 3 个 run，实际 ${perRun.size}`);
  for (const [runId, count] of perRun) {
    assert(count > 0, `run ${runId} 步数为 0`);
  }
  // 14:25 那个 run：collect/metrics/anomaly/propose/approval_gate(interrupted)/approval_gate(恢复)/execute/effect
  assertEqual(perRun.get("run_20261023T142530Z_9f3c1a7b"), 8, "run B 的步数（含 interrupt 与恢复各一次）");
});

// ————————————————————————— §7.5 派生指标 —————————————————————————

check("§7.5 毛利率逐点等于契约字段 gross_margin（前端不再自算）", () => {
  const model = deriveDashboard(METRICS.points as MetricPoint[]);
  const margin = model.primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
  // 逐点对齐：任何"自己再算一遍"的实现（含/不含退款、加不加成本字段）都会在这里的某一位小数上露馅。
  assertEqual(
    margin.points.map((p) => p.value),
    METRICS.points.map((p) => p.gross_margin ?? null),
    "毛利率序列必须与 fixtures 的 gross_margin 逐点相同",
  );
  // 首点 gross_margin 为 null（那家店那天没配成本）→ 必须还是 null：既不能变 0，也不能变 1.0。
  assertEqual(margin.points[0]?.value ?? null, null, "gross_margin=null 的点必须保持 null");
  // 文案断言用字面量，不用 MARGIN_NULL_LABEL：拿常量跟同一个常量比，改文案永远不会红。
  assertEqual(margin.nullLabel, "未配置成本", "null 值的卡片文案（不是 0% 也不是 —）");
  assertEqual(margin.headline, "newest", "卡片头条取最新一天（null 也要显示「未配置成本」）");
  // 销量来自契约内字段，任何情况下都要能画
  const sales = model.primary.find((s) => s.key === "paid_order_count") ?? fail("缺销量序列");
  assert(sales.points.some((p) => p.value !== null), "销量来自 paid_order_count（契约内），必须有值");
});

check("§7.5 gross_margin=0 是「不赚不亏」这个真事实，不得落进 null 分支显示「未配置成本」", () => {
  // 这条防的是"把 0 当假"（`if (!v)` / `v || null` 式判断）：那会让 0 走进 nullLabel，
  // 把"不赚不亏"显示成"没配成本"。契约 §7.5 明写 null ≠ 0（null=不知道，0=真事实）。
  // 常量值单独用字面量钉一次——不拿 MARGIN_NULL_LABEL 跟自己比（那样的断言改文案永远不红）。
  assertEqual(MARGIN_NULL_LABEL, "未配置成本", "导出常量的字面值");

  const base = METRICS.points[METRICS.points.length - 1] ?? fail("fixtures 为空");
  const zeroDay: MetricPoint = { ...base, gross_margin: 0, cost_coverage_ratio: 0.75 };
  const margin = deriveDashboard([zeroDay]).primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
  const point = margin.points[0] ?? fail("毛利率序列为空");
  assert(point.value !== null, "gross_margin=0 不得被读成 null（null 才会走 nullLabel）");
  assertEqual(point.value, 0, "gross_margin=0 必须原样保留为 0");
  // 卡片取值逻辑是 `value === null ? nullLabel : renderValue(...)`，所以钉住 0 的展示字符串：
  // 一旦有人把 0 折成 null，这里会变成 "未配置成本" 而红。
  assertEqual(formatPercent(point.value, 2), "0.00%", "0 的展示字符串（不是「未配置成本」）");

  // fixtures 里必须真的有一天 0：否则上面只是人造点，"不赚不亏"从没在默认数据里上过屏。
  assertEqual(
    METRICS.points.filter((p) => p.gross_margin === 0).map((p) => p.date),
    ["2026-10-14"],
    "默认 fixtures 里必须有且只有一天 gross_margin=0（演示不赚不亏）",
  );
  const fixtureMargin =
    deriveDashboard(METRICS.points).primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
  const fixtureZero = fixtureMargin.points.find((p) => p.date === "2026-10-14") ?? fail("缺 2026-10-14");
  assertEqual(fixtureZero.value, 0, "fixtures 的 0 传到派生层仍然是 0（不是 null）");
  assert(
    typeof fixtureZero.qualifier === "string" && fixtureZero.qualifier.includes("76.37%"),
    `0 那天仍要带覆盖率前提，实际 ${String(fixtureZero.qualifier)}`,
  );
});

check("§7.5 库存周转仍按契约缺口处理：缺 inventory_units 报缺口并全 null，扩展字段回来后自动可算", () => {
  const baseline = deriveDashboard(METRICS.points as MetricPoint[]);
  // 毛利率不再是缺口（字段已入契约），只剩库存周转
  assertEqual(baseline.gaps.map((g) => g.key), ["inventory_turnover"], "缺字段时必须且只报库存周转一个缺口");
  const turnover = baseline.primary.find((s) => s.key === "inventory_turnover") ?? fail("缺库存周转序列");
  assert(turnover.points.every((p) => p.value === null), "缺 inventory_units 时库存周转必须是 null，不是 0");

  const extended = (metricsExtendedFixture as unknown as MetricsResponse).points;
  const withInventory = deriveDashboard(extended);
  assertEqual(withInventory.gaps.length, 0, "有 inventory_units 时不应报缺口");
  const turnoverExt = withInventory.primary.find((s) => s.key === "inventory_turnover") ?? fail("缺库存周转序列");
  assert(turnoverExt.points.some((p) => p.value !== null), "带 inventory_units 时库存周转应有值");

  // 毛利率不吃扩展字段：Mock 扩展开关（只加 inventory_units）不得改变任何一个毛利率的数
  const marginExt = withInventory.primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
  assertEqual(
    marginExt.points.map((p) => p.value),
    METRICS.points.map((p) => p.gross_margin ?? null),
    "毛利率不因 Mock 扩展字段开而变化",
  );
});

check("§7.5 最新一天 gross_margin=null → 头条是 null（不拿更早那天的数冒充今天）", () => {
  const valued = METRICS.points[METRICS.points.length - 1] ?? fail("fixtures 为空");
  assert(valued.gross_margin !== null && valued.gross_margin !== undefined, "末点应当有值（否则这条断言测不到东西）");
  // 追加一天"没配成本"：卡片头条必须是 null → 显示「未配置成本」；环比仍用最后一个有值点。
  const nullDay: MetricPoint = { ...valued, date: "2026-10-23", gross_margin: null, cost_coverage_ratio: 0 };
  const margin =
    deriveDashboard([valued, nullDay]).primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
  assertEqual(margin.headline, "newest", "头条取最新一天");
  assertEqual(margin.points[margin.points.length - 1]?.value ?? null, null, "最新一天是 null");
  assertEqual(summarize(margin).latest?.date, "2026-10-22", "环比基线仍是最后一个有值点");
});

check("§7.5 旧自算口径已删除：只给契约外 cost_cents、不给 gross_margin → 毛利率必须是 null", () => {
  // 这条是防回退：旧实现读 cost_cents 按 (GMV − 退款 − 成本) / (GMV − 退款) 算，会得到 0.24 这种数。
  // 现在同一个点必须一个数都算不出来——"两套口径并存"在这里被钉死。
  const legacy = [
    {
      date: "2026-10-22",
      gmv_cents: 12_840_000,
      order_count: 342,
      paid_order_count: 318,
      refund_order_count: 11,
      refund_amount_cents: 215_000,
      aov_cents: 40_377,
      refund_rate: 0.034591,
      uv: 5_120,
      cvr: 0.062109,
      gmv_7d_avg_cents: null,
      cost_cents: 7_960_800,
      inventory_units: 10_812,
      computed_at: "2026-10-22T17:00:00Z",
    },
  ] as unknown as MetricPoint[];
  const margin =
    deriveDashboard(legacy).primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
  assertEqual(margin.points.map((p) => p.value), [null], "只有 cost_cents 时不得算出任何毛利率");
  // 同上：字面量断言，不拿 MARGIN_NULL_LABEL 跟自己比。
  assertEqual(margin.nullLabel, "未配置成本", "算不出来时显示「未配置成本」而不是 0%");
});

check("§7.5 毛利率与 cost_coverage_ratio 成对读：覆盖率<1 必须在卡片上给出「仅覆盖 X%」", () => {
  const model = deriveDashboard(METRICS.points as MetricPoint[]);
  const margin = model.primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
  const last = margin.points[margin.points.length - 1] ?? fail("毛利率序列为空");
  // fixtures 末点覆盖率 0.7418 → 提示必须带出这个数（这是"敢显示这个毛利率"的前提）
  assertEqual(last.qualifier, "仅覆盖 74.18% 成交额", "覆盖率<1 的口径提示");
  // 覆盖率恰好 100% 的点没有前提要补，不要硬塞一句废话
  const full = margin.points.find((p) => p.date === "2026-10-16") ?? fail("缺 2026-10-16");
  assertEqual(full.qualifier ?? null, null, "覆盖率=100% 时不补口径提示");
  // 值为 null 时不挂提示（否则会出现「未配置成本 + 仅覆盖 0.00%」这种叠字）
  assertEqual(margin.points[0]?.qualifier ?? null, null, "null 值不挂口径提示");
});

check("§7.5 cost_coverage_ratio 的三条边界：>1 如实显示不裁剪、缺失时不猜、0 明说「一个成本都没配」", () => {
  const base = METRICS.points[METRICS.points.length - 1] ?? fail("fixtures 为空");
  const marginOf = (point: MetricPoint): { value: number | null; qualifier: string | null } => {
    const series = deriveDashboard([point]).primary.find((s) => s.key === "gross_margin") ?? fail("缺毛利率序列");
    const first = series.points[0] ?? fail("毛利率序列为空");
    return { value: first.value, qualifier: first.qualifier ?? null };
  };

  // >1：上游汇总与订单明细对不上时后端如实给 >1，前端不许 min(…,1.0) 抹平，也不许格式化成 100%
  const over = marginOf({ ...base, cost_coverage_ratio: 2 });
  assertEqual(over.value, base.gross_margin ?? null, "毛利率数值本身不受覆盖率影响");
  assert(over.qualifier !== null && over.qualifier.includes("200.00%"), `>1 必须如实显示，实际 ${String(over.qualifier)}`);

  // 覆盖率缺失（分母为 0 / 未按 SKU 归集）：不能说成"覆盖 100%"或"覆盖 0%"，要讲"未给出"
  const unknown = marginOf({ ...base, cost_coverage_ratio: null });
  assert(
    unknown.qualifier !== null && unknown.qualifier.includes("未给出"),
    `覆盖率缺失时必须说明未给出，实际 ${String(unknown.qualifier)}`,
  );

  // 覆盖率恰好 0：语义是"一个成本都没配 / 没归集"，不是"覆盖率很小"。
  // 合规后端下 coverage=0 ⟹ gross_margin=null ⟹ 卡片走「未配置成本」，这个分支到不了；
  // 能到这儿必然是后端违约（给了非 null 的毛利率），所以文案要直白，不能是「仅覆盖 0.00% 成交额」。
  const zeroCoverage = marginOf({ ...base, gross_margin: 0.12, cost_coverage_ratio: 0 });
  assert(
    zeroCoverage.qualifier !== null && zeroCoverage.qualifier.includes("一个成本都没配"),
    `coverage=0 必须明说"一个成本都没配"，实际 ${String(zeroCoverage.qualifier)}`,
  );
  assert(
    zeroCoverage.qualifier !== null && !zeroCoverage.qualifier.includes("仅覆盖"),
    `coverage=0 不得退化成"仅覆盖 0.00%"，实际 ${String(zeroCoverage.qualifier)}`,
  );
});

// ————————————————————————— mock 假后端的 §7.4 校验 —————————————————————————

function signal(): AbortSignal {
  return new AbortController().signal;
}

checkAsync("§4.4 mock：edit 缺 edited_params → 400 VALIDATION_ERROR", async () => {
  resetMockState();
  const res = await getMockTransport().send(
    {
      method: "POST",
      path: "/api/approve",
      body: {
        thread_id: "12345678:run_20261023T142530Z_9f3c1a7b",
        interrupt_id: "6f1e9c2a7b3d4e5f",
        operator: "ops_zhang",
        decisions: [
          { action_id: priceApproval.action_id, verdict: "edit", edited_params: null },
          { action_id: couponApproval.action_id, verdict: "reject", edited_params: null },
        ],
      },
    },
    signal(),
  );
  assertEqual(res.status, 400, "HTTP 状态");
  const body = res.body as { error: { code: string } };
  assertEqual(body.error.code, "VALIDATION_ERROR", "错误码");
});

checkAsync("§4.4 mock：reject 带非 null edited_params → 400 VALIDATION_ERROR", async () => {
  resetMockState();
  const res = await getMockTransport().send(
    {
      method: "POST",
      path: "/api/approve",
      body: {
        thread_id: "12345678:run_20261023T142530Z_9f3c1a7b",
        interrupt_id: "6f1e9c2a7b3d4e5f",
        operator: "ops_zhang",
        decisions: [
          { action_id: priceApproval.action_id, verdict: "approve", edited_params: null },
          { action_id: couponApproval.action_id, verdict: "reject", edited_params: { discount_cents: 500 } },
        ],
      },
    },
    signal(),
  );
  assertEqual(res.status, 400, "HTTP 状态");
  assertEqual((res.body as { error: { code: string } }).error.code, "VALIDATION_ERROR", "错误码");
});

checkAsync("§7.4 mock：合法的 edit+reject 整批提交 → 200，applied 状态分别为 succeeded / rejected", async () => {
  resetMockState();
  const built = buildApproveRequest({
    threadId: "12345678:run_20261023T142530Z_9f3c1a7b",
    interruptId: "6f1e9c2a7b3d4e5f",
    operator: "ops_zhang",
    items: [priceApproval, couponApproval],
    decisionOf: (a) =>
      a.action_id === priceApproval.action_id
        ? { verdict: "edit", draft: { price_cents: "9700" }, reason: "降幅收敛到 3%" }
        : draftFor(a, "reject", "该 SKU 已停售"),
  });
  assert(built.ok, "本地构造应当成功");
  if (!built.ok) return;

  const res = await getMockTransport().send({ method: "POST", path: "/api/approve", body: built.body }, signal());
  assertEqual(res.status, 200, "HTTP 状态");
  const body = res.body as ApproveResponse;
  assertEqual(body.status, "done", "run status");
  const applied = new Map(body.applied.map((a) => [a.action_id, a]));
  assertEqual(applied.get(priceApproval.action_id)?.status, "succeeded", "edit 的应用状态");
  const rejected = applied.get(couponApproval.action_id);
  assertEqual(rejected?.status, "rejected", "reject 的应用状态");
  assertEqual(rejected?.action_log_id, null, "reject 不写 action_log（action_log_id 为 null）");
  assert(body.effect_report !== null, "应带 effect_report");
});

checkAsync("§7.1 mock：重复裁决 → 409 DUPLICATE_DECISION", async () => {
  resetMockState();
  const body = {
    thread_id: "12345678:run_20261023T142530Z_9f3c1a7b",
    interrupt_id: "6f1e9c2a7b3d4e5f",
    operator: "ops_zhang",
    decisions: [
      { action_id: priceApproval.action_id, verdict: "approve", edited_params: null },
      { action_id: couponApproval.action_id, verdict: "approve", edited_params: null },
    ],
  };
  const first = await getMockTransport().send({ method: "POST", path: "/api/approve", body }, signal());
  assertEqual(first.status, 200, "首次提交");
  const second = await getMockTransport().send({ method: "POST", path: "/api/approve", body }, signal());
  assertEqual(second.status, 409, "重复提交的 HTTP 状态");
  assertEqual((second.body as { error: { code: string } }).error.code, "DUPLICATE_DECISION", "重复提交的错误码");
});

checkAsync("§7.3 mock：GET /api/alerts 只返回 pending 且字段集与 §7.3 一致", async () => {
  resetMockState();
  const res = await getMockTransport().send(
    { method: "GET", path: "/api/alerts", query: { status: "pending", limit: 50 } },
    signal(),
  );
  assertEqual(res.status, 200, "HTTP 状态");
  const body = res.body as AlertsResponse;
  assertEqual(body.items.length, 3, "fixtures 的 3 条待审批");
  const first = body.items[0] ?? fail("items 为空");
  assertDeepKeys(
    first,
    [
      "action_id",
      "run_id",
      "thread_id",
      "shop_id",
      "trace_id",
      "interrupt_id",
      "action_type",
      "target",
      "params",
      "reason",
      "evidence",
      "risk",
      "status",
      "created_at",
    ],
    "PendingApproval",
  );
  assert(!("approval_id" in first), "§7.2/§7.3 明确没有 approval_id 字段");
});

checkAsync("§7.6 mock：GET /api/logs 按 seq DESC 且 next_cursor 是最后一条 seq 的字符串", async () => {
  resetMockState();
  const res = await getMockTransport().send({ method: "GET", path: "/api/logs", query: { limit: 3 } }, signal());
  assertEqual(res.status, 200, "HTTP 状态");
  const body = res.body as LogsResponse;
  assertEqual(body.items.length, 3, "limit=3");
  const seqs = body.items.map((item) => item.seq);
  assertEqual(seqs, seqs.slice().sort((a, b) => b - a), "seq DESC");
  assertEqual(body.next_cursor, String(seqs[seqs.length - 1]), "next_cursor");
});

// ————————————————————————— §12 字段集 / fixtures 同形 —————————————————————————

const METRIC_POINT_KEYS = [
  "date",
  "gmv_cents",
  "order_count",
  "paid_order_count",
  "refund_order_count",
  "refund_amount_cents",
  "aov_cents",
  "refund_rate",
  "uv",
  "cvr",
  "gmv_7d_avg_cents",
  "gross_margin",
  "cost_coverage_ratio",
  "computed_at",
];

check("§7.5/§12 fixtures 与真实响应同形：metrics.json 的字段集恰好等于 MetricPoint", () => {
  for (const point of METRICS.points) {
    assertDeepKeys(point as unknown as Record<string, unknown>, METRIC_POINT_KEYS, `points[${point.date}]`);
  }
});

check("扩展字段单独放 metrics.extended.json（不在默认 fixtures 里，避免假装契约支持）", () => {
  const extended = (metricsExtendedFixture as unknown as MetricsResponse).points;
  assert(extended.length > 0, "扩展 fixtures 不能为空");
  for (const point of extended) {
    // 契约外只剩 inventory_units：cost_cents 随"毛利率自算路径"一起删除了，不许回潮
    assertDeepKeys(
      point as unknown as Record<string, unknown>,
      [...METRIC_POINT_KEYS, "inventory_units"],
      `extended points[${point.date}]`,
    );
  }
  // 两个文件必须逐日对齐，否则开关切换会"少几天数据"这种莫名现象。
  assertEqual(
    extended.map((p) => p.date),
    METRICS.points.map((p) => p.date),
    "扩展 fixtures 的日期序列与默认 fixtures 一致",
  );
});

// ————————————————————————— §7.6.1 三态读取语义 —————————————————————————

function logRow(data: Record<string, unknown>): LogItem {
  return {
    seq: 1,
    ts: "2026-10-23T14:26:02.113Z",
    level: "info",
    event: "node.trace",
    run_id: "run_x",
    trace_id: "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
    shop_id: "12345678",
    node: "approval_gate",
    message: "node ok",
    data,
  };
}

check("§7.6.1 缺失 data.outcome → 按 ok 读（并把「缺失」和「真的是 ok」区分开）", () => {
  const row = logRow({ duration_ms: 3 });
  assertEqual(outcomeOf(row), "ok", "缺失 outcome 的读取结果");
  assertEqual(hasOutcome(row), false, "缺失标记");
  assertEqual(outcomeOf(logRow({ outcome: "ok" })), "ok", "显式 ok");
  assertEqual(hasOutcome(logRow({ outcome: "ok" })), true, "显式 ok 的标记");
});

check("§7.6.1 三态映射：interrupted → 「等待审批」+ 黄色；只有 error 才红", () => {
  assertEqual(outcomeLabel("interrupted"), "等待审批", "interrupted 文案");
  assertEqual(outcomeTone("interrupted"), "warn", "interrupted 色调");
  assertEqual(outcomeTone("error"), "bad", "error 色调");
  assertEqual(outcomeTone("ok"), "ok", "ok 色调");
  assertEqual(outcomeLabel("error"), "error", "error 文案");
});

check("§7.6.1 未知 outcome 原样显示（不硬塞成 ok/error）", () => {
  const row = logRow({ outcome: "draining" });
  assertEqual(outcomeOf(row), "draining", "未知值读取");
  assertEqual(outcomeLabel("draining"), "draining", "未知值文案");
  assertEqual(outcomeTone("draining"), "neutral", "未知值色调");
});

check("§7.6.1 duration_ms / error / digest 的读取不猜值", () => {
  assertEqual(durationOf(logRow({ duration_ms: 42.7 })), 42.7, "duration_ms=42.7");
  assertEqual(durationOf(logRow({})), null, "缺 duration_ms → null（不是 0）");
  assertEqual(durationOf(logRow({ duration_ms: "42" })), null, "duration_ms 是字符串 → null");
  assertEqual(errorOf(logRow({ error: null })), null, "error=null");
  assertEqual(errorOf(logRow({ error: { type: "DoudianUpstreamError", message: "503" } })), {
    type: "DoudianUpstreamError",
    message: "503",
  }, "error 对象");
  assertEqual(errorOf(logRow({ error: "boom" })), null, "error 是字符串 → null（形状不符不硬读）");
});

check("§7.6.1 不变量：派生 run 摘要的步数 = 该 run 的 node.trace 条数（不区分 outcome）", () => {
  const summaries = deriveRunSummaries(LOGS.items);
  const byRun = new Map(summaries.map((s) => [s.runId, s]));
  const b = byRun.get("run_20261023T142530Z_9f3c1a7b");
  assert(b !== undefined, "缺少 run B 的摘要");
  if (!b) return;
  assertEqual(b.steps, 8, "run B 步数");
  assertEqual([b.ok, b.interrupted, b.error], [7, 1, 0], "run B 三态计数（approval_gate 挂起一条）");
  const failed = byRun.get("run_20261023T080000Z_5e6f7a8b");
  assertEqual(failed?.error, 1, "失败 run 的 error 计数");
  assertEqual(summaries[0]?.runId, "run_20261023T080000Z_5e6f7a8b", "出问题的 run 排最前");
});

export async function run(): Promise<CheckResult[]> {
  for (const item of pending) {
    try {
      await item.fn();
      results.push({ name: item.name, ok: true, detail: "" });
    } catch (err) {
      results.push({ name: item.name, ok: false, detail: err instanceof Error ? err.message : String(err) });
    }
  }
  return results;
}
