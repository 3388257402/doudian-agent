// 真实浏览器渲染自检：用 headless Chrome + CDP 打开三个页面，断言"对着 fixtures 渲染出来的 DOM"，
// 并在审批页上真的走一遍「编辑参数 + 拒绝 + 提交」。
//
//   npm run dev            # 另开一个终端，起 http://localhost:5173
//   node scripts/dom-check.mjs
//
// 为什么不用 jsdom：jsdom 不跑 CSS，也就证明不了"interrupted 是黄色而不是红色"这一条；
// 这里断言的是浏览器算出来的 computed style。
//
// 依赖：本机 Chrome（可用 CHROME_PATH 覆盖）。不引 puppeteer —— 只用 CDP 的少数几个方法。

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

// 看门狗：自检卡住（Chrome 起不来 / CDP 无响应）时必须自己退出，不能挂在那儿。
const watchdog = setTimeout(() => {
  console.log("FAIL  自检超时（120s）");
  process.exit(1);
}, 120_000);
watchdog.unref?.();

const BASE_URL = process.env.DOM_CHECK_URL ?? "http://localhost:5173";
const PORT = Number(process.env.DOM_CHECK_PORT ?? 9334);
const ROOT = resolve(import.meta.dirname, "..");
const OUT_DIR = resolve(ROOT, ".verify");

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
].filter((p) => typeof p === "string" && p.length > 0);

const failures = [];
const passes = [];

function record(name, ok, detail) {
  if (ok) {
    passes.push(name);
    console.log(`PASS  ${name}`);
  } else {
    failures.push({ name, detail });
    console.log(`FAIL  ${name}`);
    console.log(`      ${detail}`);
  }
}

function assertEqual(name, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  record(name, a === e, `期望 ${e}，实际 ${a}`);
}

function sleep(ms) {
  return new Promise((done) => setTimeout(done, ms));
}

async function waitForCdp(deadlineMs = 20_000) {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (res.ok) return await res.json();
    } catch {
      // 还没起来
    }
    await sleep(250);
  }
  throw new Error(`CDP 在 ${deadlineMs}ms 内没有就绪（端口 ${PORT}）`);
}

/** 极简 CDP 客户端：send(method, params) → result。 */
function connectCdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  const waiting = new Map();
  let nextId = 0;
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (typeof message.id !== "number") return;
    const entry = waiting.get(message.id);
    if (!entry) return;
    waiting.delete(message.id);
    if (message.error) entry.reject(new Error(`${entry.method}: ${JSON.stringify(message.error)}`));
    else entry.resolve(message.result);
  });
  const ready = new Promise((resolveReady, rejectReady) => {
    ws.addEventListener("open", () => resolveReady());
    ws.addEventListener("error", () => rejectReady(new Error("CDP WebSocket 连接失败")));
  });
  const send = (method, params = {}) =>
    new Promise((resolveSend, rejectSend) => {
      const id = ++nextId;
      waiting.set(id, { resolve: resolveSend, reject: rejectSend, method });
      ws.send(JSON.stringify({ id, method, params }));
    });
  return { send, ready, close: () => ws.close() };
}

async function evaluate(send, expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  if (result.exceptionDetails) {
    throw new Error(`页面内表达式抛错：${result.exceptionDetails.text} :: ${expression}`);
  }
  return result.result.value;
}

async function openPage(path, preScript) {
  // 不靠 /json/new?url 打开目标页（它常常先返回 about:blank 的句柄，页面没真的导航），
  // 而是显式 Page.navigate，然后等 #root 真的有内容 —— 这才是"渲染出来了"的证据。
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
  if (!res.ok) throw new Error(`创建 tab 失败：HTTP ${res.status}`);
  const target = await res.json();
  const cdp = connectCdp(target.webSocketDebuggerUrl);
  await cdp.ready;
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  if (typeof preScript === "string") {
    // 必须在导航前注入，才能在应用代码之前接管 setInterval。
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: preScript });
  }
  await cdp.send("Page.navigate", { url: `${BASE_URL}/#/${path}` });
  await waitForRoot(cdp.send);
  // mock 传输层有 90~250ms 的假时延；等首屏数据落地。
  await sleep(1400);
  return { cdp, targetId: target.id };
}

async function waitForRoot(send, deadlineMs = 15_000) {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    let count = 0;
    try {
      count = await evaluate(send, "document.querySelector('#root') ? document.querySelector('#root').children.length : 0");
    } catch {
      count = 0;
    }
    if (typeof count === "number" && count > 0) return;
    await sleep(150);
  }
  throw new Error(`页面在 ${deadlineMs}ms 内没有渲染出 #root 内容（dev server 起了吗？${BASE_URL}）`);
}

async function closePage(targetId) {
  try {
    await fetch(`http://127.0.0.1:${PORT}/json/close/${targetId}`);
  } catch {
    // 关不掉不影响结论
  }
}

function pickChrome() {
  // 不用 `chrome --version` 探测：Windows 上 chrome.exe 是 GUI 启动器，spawnSync 会等它退出（等不到）。
  for (const candidate of CHROME_CANDIDATES) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(`找不到 Chrome/Edge，候选：${CHROME_CANDIDATES.join(", ")}`);
}

function killTree(pid) {
  if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
  else process.kill(pid, "SIGKILL");
}

async function saveSnapshot(cdp, name) {
  const html = await evaluate(cdp.send, "document.documentElement.outerHTML");
  mkdirSync(OUT_DIR, { recursive: true });
  const file = resolve(OUT_DIR, `${name}.html`);
  writeFileSync(file, html, "utf8");
  console.log(`      快照：${file}（${html.length} 字节）`);
}

// ————————————————————— 各页断言 —————————————————————

async function checkApprovals(cdp) {
  const { send } = cdp;

  assertEqual("审批台：3 张待审批卡片（来自 frontend/mock/alerts.json）", await evaluate(send, "document.querySelectorAll('.approval-card').length"), 3);
  assertEqual("审批台：按 thread 分成 2 组（整批裁决）", await evaluate(send, "document.querySelectorAll('.thread-group').length"), 2);
  assertEqual(
    "审批台：异常描述来自 reason（不是前端编的文案）",
    await evaluate(send, "document.body.innerText.includes('GMV 环比 -31.2%，低于近 7 日均值 30% 阈值')"),
    true,
  );
  assertEqual(
    "审批台：影响预估把整数分格式化成两位小数（现价 ¥100.00 / 建议新价 ¥96.00）",
    await evaluate(send, "document.body.innerText.includes('¥100.00') && document.body.innerText.includes('¥96.00')"),
    true,
  );
  assertEqual(
    "审批台：券的让利上限按 面额×张数 算（500×200 分 = ¥1,000.00）",
    await evaluate(send, "document.body.innerText.includes('¥1,000.00')"),
    true,
  );
  assertEqual(
    "审批台：SKU（target）可见",
    await evaluate(send, "document.body.innerText.includes('3400000000000000002')"),
    true,
  );
  assertEqual(
    "审批台：未全部裁决时提交按钮禁用（§4.4 规则 2）",
    await evaluate(send, "[...document.querySelectorAll('.thread-actions .btn-primary')].every((b) => b.disabled)"),
    true,
  );
  assertEqual(
    "审批台：风险档与 evidence 交叉核对（后端 medium vs §5.5 公式 low → 明确提示需确认）",
    await evaluate(send, "document.body.innerText.includes('契约外的风险规则')"),
    true,
  );
  assertEqual(
    "审批台：页头健康检查胶囊（GET /api/healthz，mock）",
    await evaluate(send, "document.querySelector('.app-header .pill') !== null"),
    true,
  );

  await saveSnapshot(cdp, "approvals");
}

async function checkDashboard(cdp) {
  const { send } = cdp;

  assertEqual(
    "看板：三张主指标卡（毛利率/销量/库存周转）",
    await evaluate(send, "[...document.querySelectorAll('.metric-card')].map((e) => e.getAttribute('aria-label'))"),
    ["毛利率", "销量（支付订单数）", "库存周转（日均次数）"],
  );
  // 默认状态 = 契约 §7.5 的真实形状（无 cost_cents / inventory_units）：
  // 毛利率与库存周转必须变成"数据源缺字段"提示，只有销量能画。
  assertEqual(
    "看板：按 §7.5 真实形状返回时，毛利率/库存周转显示为缺字段提示（不画假曲线）",
    await evaluate(send, "document.querySelectorAll('.gap-box').length"),
    2,
  );
  assertEqual("看板：此时只有 1 张图能画（销量）", await evaluate(send, "document.querySelectorAll('.chart-svg').length"), 1);
  assertEqual(
    "看板：缺口提示写明缺哪个字段",
    await evaluate(send, "document.body.innerText.includes('cost_cents') && document.body.innerText.includes('inventory_units')"),
    true,
  );
  assertEqual("看板：7 个次要指标", await evaluate(send, "document.querySelectorAll('.mini-metric').length"), 7);
  assertEqual(
    "看板：明确写出「缺数据不补零」",
    await evaluate(send, "document.body.innerText.includes('不补零')"),
    true,
  );

  // 打开 Mock 扩展开关 → 模拟后端补齐扩展字段，三张图自动恢复。
  const toggled = await evaluate(
    send,
    `(() => {
      const box = document.querySelector('.filter-bar input[type=checkbox]');
      if (!box) return 'no-toggle';
      box.click();
      return 'ok';
    })()`,
  );
  assertEqual("看板：Mock 模式提供「扩展字段」开关", toggled, "ok");
  await sleep(1500);
  assertEqual(
    "看板：打开扩展字段后三张图都画出来（缺口自动恢复）",
    await evaluate(send, "document.querySelectorAll('.chart-svg').length"),
    3,
  );
  assertEqual(
    "看板：扩展开启后明确标注「契约外扩展字段」（不假装契约支持）",
    await evaluate(send, "document.body.innerText.includes('契约外扩展字段')"),
    true,
  );
  assertEqual(
    "看板：折线图的点有真实数据（>20 个）",
    await evaluate(send, "document.querySelectorAll('.chart-dot').length > 20"),
    true,
  );
  assertEqual(
    "看板：毛利率展示为百分比两位小数（不是 0..1 的裸小数）",
    await evaluate(
      send,
      "(() => { const el = document.querySelector('.metric-card .metric-value'); return el ? /(^|\\D)\\d+\\.\\d{2}%/.test(el.innerText) : false; })()",
    ),
    true,
  );

  await saveSnapshot(cdp, "dashboard");
}

async function checkLogs(cdp) {
  const { send } = cdp;

  assertEqual("日志：fixtures 的 38 条日志全部渲染", await evaluate(send, "document.querySelectorAll('.log-row').length"), 38);
  assertEqual(
    "日志：node.trace 三态数量（ok 14 / 等待审批 2 / error 1）",
    await evaluate(
      send,
      "['outcome-ok','outcome-interrupted','outcome-error'].map((c) => document.querySelectorAll('.log-row.'+c).length)",
    ),
    [14, 2, 1],
  );
  assertEqual(
    "日志：interrupted 显示为「等待审批」而不是 error",
    await evaluate(
      send,
      "[...document.querySelectorAll('.log-row.outcome-interrupted .pill')].map((e) => e.textContent).join(',')",
    ),
    "info,等待审批,info,等待审批",
  );
  assertEqual(
    "日志：interrupted 行的背景是黄色（computed style，不是红色）",
    await evaluate(send, "getComputedStyle(document.querySelector('.log-row.outcome-interrupted')).backgroundColor"),
    "rgba(245, 158, 11, 0.07)",
  );
  assertEqual(
    "日志：error 行的背景是红色",
    await evaluate(send, "getComputedStyle(document.querySelector('.log-row.outcome-error')).backgroundColor"),
    "rgba(248, 113, 113, 0.07)",
  );
  assertEqual(
    "日志：耗时列读 data.duration_ms（42.7 ms / 3.01 s 都出现过，非 trace 行显示 —）",
    await evaluate(
      send,
      "(() => { const t = [...document.querySelectorAll('.log-row')].map((r) => r.querySelector('.col-duration').textContent); return [t.includes('42.7 ms'), t.includes('3.01 s'), t.includes('—')]; })()",
    ),
    [true, true, true],
  );
  assertEqual(
    "日志：run 摘要按 run 聚合，出问题的 run 排最前（§7.6.1 步数不变量）",
    await evaluate(
      send,
      "[...document.querySelectorAll('.run-summary-table tbody tr')].map((tr) => tr.querySelector('code').textContent + '/' + tr.children[1].textContent)",
    ),
    [
      "run_20261023T080000Z_5e6f7a8b/1",
      "run_20261023T142530Z_9f3c1a7b/8",
      "run_20261023T090000Z_1a2b3c4d/8",
    ],
  );
  assertEqual(
    "日志：节点列展示节点名（collect/approval_gate/execute…）",
    await evaluate(
      send,
      "[...document.querySelectorAll('.log-row .col-node')].some((e) => e.textContent === 'approval_gate')",
    ),
    true,
  );

  await saveSnapshot(cdp, "logs");
}

/** 在审批页上真的走一遍：价格动作 edit 参数 → 同 thread 的券动作 reject → 提交 → 看回执。 */
async function checkApprovalFlow(cdp) {
  const { send } = cdp;

  const clicked = await evaluate(
    send,
    `(() => {
      const group = [...document.querySelectorAll('.thread-group')].find((g) => g.innerText.includes('改价') && g.innerText.includes('发券'));
      if (!group) return 'no-group';
      const card = [...group.querySelectorAll('.approval-card')].find((c) => c.innerText.includes('改价'));
      if (!card) return 'no-price-card';
      const btn = [...card.querySelectorAll('.verdict-buttons .btn')].find((b) => b.textContent === '编辑参数');
      if (!btn) return 'no-edit-button';
      btn.click();
      return 'ok';
    })()`,
  );
  assertEqual("审批流程：价格动作点「编辑参数」展开参数表单", clicked, "ok");
  await sleep(200);

  const typed = await evaluate(
    send,
    `(() => {
      const group = [...document.querySelectorAll('.thread-group')].find((g) => g.innerText.includes('改价') && g.innerText.includes('发券'));
      const card = [...group.querySelectorAll('.approval-card')].find((c) => c.innerText.includes('改价'));
      const input = card.querySelector('.param-editor input');
      if (!input) return 'no-input';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, '9700');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return 'ok';
    })()`,
  );
  assertEqual("审批流程：编辑 price_cents → 9700", typed, "ok");
  await sleep(200);

  const rejected = await evaluate(
    send,
    `(() => {
      const group = [...document.querySelectorAll('.thread-group')].find((g) => g.innerText.includes('改价') && g.innerText.includes('发券'));
      const card = [...group.querySelectorAll('.approval-card')].find((c) => c.innerText.includes('发券'));
      if (!card) return 'no-coupon-card';
      const btn = [...card.querySelectorAll('.verdict-buttons .btn')].find((b) => b.textContent === '拒绝');
      if (!btn) return 'no-reject-button';
      btn.click();
      return 'ok';
    })()`,
  );
  assertEqual("审批流程：同 thread 的券动作选「拒绝」", rejected, "ok");
  await sleep(200);

  assertEqual(
    "审批流程：本 thread 全裁决后提交按钮启用，另一个 thread 仍禁用",
    await evaluate(
      send,
      "[...document.querySelectorAll('.thread-group')].map((g) => g.querySelector('.thread-actions .btn-primary').disabled)",
    ),
    [false, true],
  );

  await evaluate(
    send,
    `(() => {
      const group = [...document.querySelectorAll('.thread-group')].find((g) => !g.querySelector('.thread-actions .btn-primary').disabled);
      group.querySelector('.thread-actions .btn-primary').click();
      return 'ok';
    })()`,
  );
  await sleep(1500);

  const result = await evaluate(
    send,
    `(() => {
      const panel = document.querySelector('.result-panel');
      if (!panel) return null;
      const rows = [...panel.querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.innerText));
      return { status: panel.querySelector('strong').innerText, rows };
    })()`,
  );
  record("审批流程：提交后显示裁决回执面板", result !== null, "没有 .result-panel");
  if (result !== null) {
    record(
      "审批流程：回执里 edit→succeeded、reject→rejected（reject 不写 action_log）",
      result.rows.length === 2 &&
        result.rows.some((r) => r[1] === "edit" && r[2] === "succeeded") &&
        result.rows.some((r) => r[1] === "reject" && r[2] === "rejected" && r[3] === "—"),
      JSON.stringify(result.rows),
    );
  }

  await sleep(700);
  assertEqual(
    "审批流程：裁决后该 thread 的 2 张卡片从列表消失（重新 GET /api/alerts）",
    await evaluate(send, "document.querySelectorAll('.approval-card').length"),
    1,
  );
  assertEqual(
    "审批流程：另一个 thread 仍有 1 条待审批（未被误提交）",
    await evaluate(send, "document.querySelectorAll('.thread-group').length"),
    1,
  );

  await saveSnapshot(cdp, "approvals-after-submit");
}

// ————————————————————— 轮询纪律（定时器不叠加） —————————————————————

// 在应用代码之前接管 setInterval/clearInterval，统计"当前活动定时器数量"。
const TIMER_SPY = `(() => {
  window.__liveIntervals = new Set();
  const realSet = window.setInterval.bind(window);
  const realClear = window.clearInterval.bind(window);
  window.__fired = 0;
  window.setInterval = function (fn, ms, ...rest) {
    const wrapped = typeof fn === "function" ? function () { window.__fired += 1; return fn.apply(this, arguments); } : fn;
    const id = realSet(wrapped, ms, ...rest);
    window.__liveIntervals.add(id);
    return id;
  };
  window.clearInterval = function (id) {
    window.__liveIntervals.delete(id);
    return realClear(id);
  };
})();`;

async function checkPolling(cdp) {
  const { send } = cdp;
  // 审批页有 2 个轮询源：/api/alerts(8s) 与 /api/healthz(15s)。等两个周期以上，看数量是否涨。
  await sleep(6000);
  const first = await evaluate(send, "window.__liveIntervals.size");
  const firedFirst = await evaluate(send, "window.__fired");
  await sleep(12_000);
  const second = await evaluate(send, "window.__liveIntervals.size");
  const firedSecond = await evaluate(send, "window.__fired");

  record(
    "轮询：定时器已装上（活动定时器 > 0）",
    typeof first === "number" && first > 0,
    `活动定时器=${JSON.stringify(first)}`,
  );
  record(
    "轮询：轮询真的在走（18 秒内回调被触发过）",
    typeof firedSecond === "number" && firedSecond > (typeof firedFirst === "number" ? firedFirst : 0),
    `回调次数 ${JSON.stringify(firedFirst)} → ${JSON.stringify(firedSecond)}`,
  );
  assertEqual("轮询：活动定时器数量不随时间增长（不叠加）", second, first);
}

// ————————————————————— 主流程 —————————————————————

const chrome = pickChrome();
console.log(`浏览器：${chrome}`);
console.log(`目标页面：${BASE_URL}`);
console.log("");

const child = spawn(
  chrome,
  [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${resolve(ROOT, ".verify/chrome-profile")}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);

try {
  await waitForCdp();
  const version = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
  console.log(`引擎：${version.Browser}`);
  console.log("");

  // 每项：[标签, 检查函数, 页面路径(默认=标签), 导航前注入的脚本]
  const pages = [
    ["approvals", checkApprovals, "approvals", null],
    ["dashboard", checkDashboard, "dashboard", null],
    ["logs", checkLogs, "logs", null],
    ["approvals-flow", checkApprovalFlow, "approvals", null],
    ["polling", checkPolling, "approvals", TIMER_SPY],
  ];

  for (const [name, checker, path, preScript] of pages) {
    const page = await openPage(path, preScript);
    try {
      await checker(page.cdp);
    } catch (err) {
      record(`${name}：检查过程中抛错`, false, err instanceof Error ? (err.stack ?? err.message) : String(err));
    } finally {
      await closePage(page.targetId);
      page.cdp.close();
    }
    console.log("");
  }
} catch (err) {
  record("自检无法开始", false, err instanceof Error ? err.message : String(err));
} finally {
  killTree(child.pid);
}

console.log(`${passes.length}/${passes.length + failures.length} 通过`);
if (failures.length > 0) {
  console.log("");
  for (const failure of failures) console.log(`FAILED: ${failure.name}`);
  process.exit(1);
}
