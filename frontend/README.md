# 前端：审批台 / 效果看板 / 执行日志

React 18 + TypeScript + Vite。**不引图表库、不引 UI 库**（折线图是手写 SVG，样式是单文件 CSS）。

## 环境结论（实测）

| 项 | 结果 |
|---|---|
| `npm ping` | 通（npmmirror，PONG 249ms） |
| `node -v` / `npm -v` | v25.8.2 / 11.11.1 |
| `npm install` | 成功，67 包，56.7s |
| 依赖 | react/react-dom 18.3.1、typescript 5.6.3、vite 5.4.11、@vitejs/plugin-react 4.3.4 |
| `npx tsc --noEmit` | 通过（strict + noUncheckedIndexedAccess + verbatimModuleSyntax） |
| `npm run build` | 通过（`dist/` JS 241,952 B = vite 报的 234.28 kB；CSS 13,350 B = 13.04 kB；另出 `*.js.map`） |
| `node scripts/verify.mjs` | 33/33 通过（§12 逐字 diff 1 条 + `verify-suite.ts` 的自检 32 条） |
| `node scripts/dom-check.mjs` | 46/46 通过（headless Chrome 真实渲染 + 审批闭环 + 轮询定时器） |

## 可复现命令

```bash
cd frontend
npm install                # 首次
npm run typecheck          # tsc --noEmit
npm run build              # tsc --noEmit && vite build → dist/
npm run verify             # 契约字段自检（含 §12 逐字 diff）→ 33 条断言
npm run dev                # http://localhost:5173
npm run verify:dom         # 另开一个终端、dev 起着；headless Chrome 打开三个页面并断言 DOM
```

`npm run dev` 时 `/api/*` 由 Vite proxy 转发到 `http://127.0.0.1:8000`（`vite.config.ts`），前端代码里**没有**任何后端绝对地址。

## 两种数据源模式

页面右上角切换（切换值存 `localStorage`，优先级高于 `VITE_API_MODE`）：

| 模式 | 行为 |
|---|---|
| **Mock**（默认） | 读 `frontend/mock/*.json` fixtures，跑一个**有状态**的浏览器内假后端（`src/mock/mockServer.ts`）：裁决会改状态、追加日志，重复裁决返回 `409 DUPLICATE_DECISION`，缺裁决返回 `400 VALIDATION_ERROR`。用于无后端时静态渲染三个页面 + 验证错误 UI。 |
| **实时** | 真实 `fetch` 到 `/api/*`。后端没起会如实报 `NETWORK_ERROR`（不偷偷降级到 mock）。 |

生产构建请设 `VITE_API_MODE=live`（见 `.env.example`）。

## 契约对齐（docs/CONTRACT.md **v1.5.0**）

| 契约 | 前端位置 |
|---|---|
| §12 TS 类型 | `src/api/types.ts` —— **逐字照抄**（`npm run verify` 第一步就是把它和 §12 的 `ts` 代码块逐行 diff，不一致直接 FAIL）；扩展字段单独放 `src/api/extension.ts` |
| §7.1 错误包络 + 9 个 code | `src/api/errors.ts`（code → 文案/建议动作/是否可重试）、`src/api/client.ts`（解析包络） |
| §7.2 `POST /api/agent/run` | `ApiClient.runAgent()`（审批台"触发新一轮采集"按钮，`dry_run: true`） |
| §7.3 `GET /api/alerts` | `ApiClient.listAlerts()` |
| §7.4 `POST /api/approve` | `ApiClient.approve()`；请求体由纯函数 `src/lib/approve.ts` 构造（见下） |
| §7.5 `GET /api/metrics` | `ApiClient.getMetrics()` |
| §7.6 / §7.6.1 `GET /api/logs` | `ApiClient.getLogs()`；三态语义在 `src/lib/outcome.ts` |
| §7.7 `GET /api/healthz` | `ApiClient.healthz()`（页头状态胶囊） |
| §11 自检：只请求 `/api/*` | `transport.ts` 的 `buildUrl()` 对非 `/api/*` 直接抛错 |
| §11 自检：按 `error.code` 分支 | 全站只有 `errors.ts` 一处把 code 映射成 UI 行为；没有任何 `if (status === 409)` 式的猜测 |
| §11 自检：金额 `*_cents` | `lib/format.ts` 的 `formatCents()` 统一 `/100` 保留两位；前端内部不做 float 金额运算 |
| §5.5 参数 schema | `lib/paramSchema.ts` 镜像 `ACTION_PARAM_SCHEMA` + `validate_params` 规则，编辑表单与 mock 共用同一份 |
| §4.4 规则 2（裁决集合不多不少不重复） | `pages/ApprovalsPage.tsx` 按 `thread_id` 分组，**整批提交**；未全部裁决时提交按钮禁用并说明原因 |
| §4.5 幂等键分离 | 提交只带 `action_id` + 生效参数；`idempotency_key` 由后端在审批后算，前端不参与 |
| §7.6.1 三态 `data.outcome` | `pages/LogsPage.tsx` + `lib/outcome.ts`：`ok` 绿 / `interrupted` 黄「等待审批」/ `error` 红；**只按 outcome 上色，不按 level**；缺键按 `ok` 读，未知值原样显示 |

### 审批提交体（与 §7.4 完全一致）

```json
{
  "thread_id": "12345678:run_20261023T142530Z_9f3c1a7b",
  "interrupt_id": "6f1e9c2a7b3d4e5f",
  "operator": "ops_zhang",
  "decisions": [
    { "action_id": "run_x:price_update:3400000000000000002", "verdict": "edit",
      "edited_params": { "price_cents": 9700 }, "operator": "ops_zhang", "reason": "降幅收敛到 3%" },
    { "action_id": "run_x:coupon_create:3400000000000000005", "verdict": "reject",
      "edited_params": null, "operator": "ops_zhang", "reason": "让利上限过高" }
  ]
}
```

两条硬规则由**纯函数** `buildApproveRequest()`（`src/lib/approve.ts`）统一出口，不写在 JSX 里：
`verdict=edit` → `edited_params` 必须是按 §5.5 校验通过的参数对象；`verdict=approve|reject` → **恒为 `null`**（不是 `{}`、不是省略）。
`scripts/verify-suite.ts` 直接断言这两种情形，并在 mock 假后端上验证"edit 缺 edited_params → 400"、"reject 带 edited_params → 400"。

## 三个页面

| 页面 | 数据源 | 说明 |
|---|---|---|
| 审批台 `#/approvals` | `GET /api/alerts` | 卡片含异常描述（`reason`）、建议动作（`action_type` + `params`）、影响预估（从 `params`/`evidence` 算得出才写，算不出的明说）、SKU（`target`）与当前指标（`evidence.metrics`）。按 thread 分组整批提交 `POST /api/approve`。 |
| 效果看板 `#/dashboard` | `GET /api/metrics` | 毛利率 / 销量 / 库存周转为主图 + 7 个次要指标；手写 SVG 折线，`null` 断点不补零。 |
| 执行日志 `#/logs` | `GET /api/logs` | 节点、耗时（`data.duration_ms`）、状态（`data.outcome` 三态）、摘要（`message`），行可展开看 `data` 全量；顶部有从 `node.trace` 派生的 run 摘要。 |

### 看板口径：毛利率来自后端，库存周转仍是契约缺口（这是设计，不是 bug）

毛利率与成本覆盖率 **v1.5.0 起是契约字段**（§7.5/§12 的 `gross_margin` / `cost_coverage_ratio`）：后端按**店铺级加权**口径算（只对**有成本的 SKU** 加权），前端**原样展示、不做任何再计算**。

| 指标 | 数据来源 | 契约原样下的表现 |
|---|---|---|
| 销量 | `paid_order_count` | 正常画 |
| 毛利率 | `gross_margin`（契约字段） | 正常画；`null` → 卡片写「未配置成本」（**不是 0%**，也不是含糊的 `—`） |
| 毛利率的口径前提 | `cost_coverage_ratio`（契约字段） | 覆盖率 < 1 时卡片写「仅覆盖 X% 成交额」；> 1 如实显示不裁剪；缺失（`null`）时说「未给出」而不猜；恰好 `0` 时说「一个成本都没配（或未按 SKU 归集）」——`0` 不是"覆盖率很小"，而且合规后端在 `coverage=0` 时 `gross_margin` 必为 `null`，这个分支只在后端违约时出现 |
| 库存周转 | `inventory_units`（**契约外**） | 卡片显示"数据源缺字段"，**不画假曲线** |

> **旧口径已删除，不要加回来**：前端曾读契约外扩展字段 `cost_cents`，按 `(GMV − 退款 − 成本) / (GMV − 退款)` 自算毛利率。那与后端的 `gross_margin` **不是同一个数**（一个含退款净额、一个不含），同一个中文名"毛利率"出现两个数，看板上那个数无法被后端任何测试证明。现在只允许有后端口径，不保留开关，也没有"两套口径并存"的分支（`verify-suite.ts` 里有一条反向断言钉死：只给 `cost_cents`、不给 `gross_margin` 时必须一个数都算不出来）。

`frontend/mock/metrics.json` 是**严格按 §7.5 真实响应同形**的 fixtures（字段集恰好等于 `MetricPoint`，自检里有断言；13 个点里含 1 天 `gross_margin=null` 演示"未配置成本"、1 天 `gross_margin=0` 演示"不赚不亏"、1 天覆盖率 100% 演示"无前提"）。契约外扩展字段单独放 `mock/metrics.extended.json`，由**只在 Mock 模式可见**的开关「Mock：扩展字段」驱动：
关（默认）→ 库存周转那张卡变成缺口提示；开 → 模拟后端补齐 `inventory_units`，图自动恢复，并明确标注"这是契约外扩展字段"。这样"缺口真实存在"和"字段补齐后前端自动好"两件事都能被演示，而不是靠嘴说。**毛利率不受这个开关影响**（它吃的是契约字段，自检里也有断言）。

### 执行日志的 run 摘要

`agent_traces` 是**表**（一次 run 一行摘要，§6.1），没有对应的 REST 接口；`/api/logs` 只给日志行。前端不编造接口，而是按 §7.6.1 的不变量从 `node.trace` 派生：
`count(run_id=X, event="node.trace") == agent_traces[X].step_count`（不区分 outcome）。
摘要表头写明"从当前已加载的 N 条 node.trace 派生，筛选/分页会改变计数"——不冒充表读数。

## fixtures（`frontend/mock/`）

| 文件 | 内容 |
|---|---|
| `alerts.json` | 3 条待审批，跨 2 个 thread（演示"整批裁决"）；字段集与 §7.3 一致（没有 `approval_id`） |
| `metrics.json` | 13 个点（缺 `2026-10-18` 一天，演示"缺数据不补零"）。刻意保留的四种口径：`2026-10-09` 的 `gross_margin=null` + `cost_coverage_ratio=0`（未配置成本）、`2026-10-14` 的 `gross_margin=0`（不赚不亏，上屏 `0.00%`）、`2026-10-16` 的 `cost_coverage_ratio=1`（全覆盖，不补口径提示）、`2026-10-11` 的 `uv` / `cvr=null`（分母为 0）。`refund_rate` **每天都有值**（fixtures 里没有 `refund_rate=null` 的天）；**字段集恰好等于 §12 的 `MetricPoint`** |
| `metrics.extended.json` | 同上 + `inventory_units`（契约外，只在 Mock 扩展开关打开时回给前端）；**没有 `cost_cents`**——那条自算路径已删除 |
| `logs.json` | 38 条，覆盖 3 个 run：成功（8 步）、挂起后恢复（8 步，含 `approval_gate` 的 `interrupted` 与恢复各一条）、`collect` 失败（1 步，`outcome=error`）。每条 `node.trace` 都按 §7.6.1 带 `outcome`/`duration_ms`/`input_digest`/`output_digest`/`error`，`level↔outcome` 映射合规 |

## 验证（不靠肉眼）

| 命令 | 证明什么 | 结果 |
|---|---|---|
| `npm run typecheck` | 严格 TS 下能编译（含 `noUncheckedIndexedAccess`） | 通过 |
| `npm run verify` | §12 逐字 diff、§7.4 提交体两条硬规则、§7.1 按 code 分支、§0 金额格式、§5.5 参数校验、§7.6.1 三态与不变量、fixtures 与 §7 同形、§7.5 毛利率直读契约字段且必须与覆盖率成对展示（`null` 与 `0` 严格区分）、mock 的 400/409 分支 | 33/33 |
| `npm run verify:dom` | 真浏览器里三个页面**对着 fixtures 渲染**：卡片/图表/日志三态（含 computed style 黄 vs 红）、耗时列、run 摘要、缺口 banner 的文案（只讲库存缺字段）、`gross_margin=null` → 「未配置成本」与 `gross_margin=0` → `0.00%` 两种口径都真的上屏，真的走一遍「编辑参数 + 拒绝 + 提交 → 回执 → 列表刷新」，并用注入的定时器探针证明**轮询不叠加** | 46/46 |
| `npm run build` | 生产构建 | 通过 |

`verify:dom` 用 CDP 驱动本机 Chrome（`CHROME_PATH` 可覆盖），不引 puppeteer；断言失败会打印实际值并把每页 DOM 快照写到 `frontend/.verify/*.html`。

## 错误 UI 行为（按 code，不按 HTTP 状态）

| code | UI |
|---|---|
| `VALIDATION_ERROR` | 表单标红 + 逐条展示 `details`（如 `missing_action_ids`、`params.<field>`） |
| `THREAD_NOT_FOUND` / `APPROVAL_NOT_FOUND` | "该审批已失效，请刷新列表" + 自动刷新 |
| `RESOURCE_NOT_FOUND` | 空态（不是错误横幅） |
| `DUPLICATE_DECISION` | "已被裁决" + 自动刷新 |
| `RUN_IN_PROGRESS` | 提示并发冲突，禁用重复触发 |
| `RUN_STATE_CONFLICT` | 提示状态冲突 + 刷新 |
| `UPSTREAM_ERROR` | 展示 message + 重试按钮 |
| `INTERNAL_ERROR` | 展示 `trace_id`（细节只在后端日志里） |
| `NETWORK_ERROR` / `TIMEOUT` / `MALFORMED_RESPONSE` | 前端合成的 code（后端不会返回），分别提示"后端未连接/超时/响应不符 §7.1 包络" |

## 轮询纪律

`hooks/useResource.ts` 一个数据源一个 hook：

- **不会叠加定时器**：`setInterval` 只在 `[enabled, intervalMs]` 变化时重建，卸载/换 key 时 `clearInterval`；`tick` 只触发一次拉取，不再建新定时器。
- 请求序列号守卫：晚到的旧响应不许覆盖新响应（快速切筛选不会串数据）。
- 换 key / 卸载时 `abort()` 上一个请求；`ABORTED` 不算错误、不弹横幅。
- 页面 `hidden` 时跳过轮询，回到前台立刻补一次。

## 刻意不做的事

- 不做乐观更新：审批是不可逆副作用，`POST /api/approve` 成功后才刷新列表。
- 不在前端算幂等键/重试预算：那是后端的职责，前端算一遍只会造成两套真相。
- 不自动重试审批请求：网络失败时"到底提交没提交"是未知的，交给用户决定（避免重复裁决）。
- 不做权限/登录：审批台假定部署在内网或已有反向代理鉴权（`operator` 只是审计字段，不是身份凭证）。
- 不引图表库 / UI 库 / 测试框架：折线是手写 SVG，样式是纯 CSS，自检脚本只用 Node + esbuild。

## 目录

```
frontend/
├─ package.json / tsconfig.json / vite.config.ts / index.html / .env.example
├─ mock/                        # fixtures（有状态假后端的数据源）
│  ├─ alerts.json               # 3 条待审批，跨 2 个 thread
│  ├─ metrics.json              # §7.5 同形（含 gross_margin / cost_coverage_ratio）
│  ├─ metrics.extended.json     # + inventory_units（契约外，开关驱动；不含 cost_cents）
│  └─ logs.json                 # 38 条，3 个 run，覆盖 ok/interrupted/error 三态
├─ scripts/
│  ├─ verify.mjs                # 自检驱动（§12 逐字 diff + esbuild 打包套件）
│  ├─ verify-suite.ts           # 32 条契约断言（纯 Node；+verify.mjs 的 §12 逐字 diff = 33）
│  └─ dom-check.mjs             # headless Chrome + CDP 的真实渲染断言
└─ src/
   ├─ api/
   │  ├─ types.ts               # §12 逐字
   │  ├─ extension.ts           # 契约缺口的可选扩展字段 + 说明
   │  ├─ errors.ts              # ApiFailure + code → UI 行为映射 + details 摊平
   │  ├─ transport.ts           # HttpTransport / 错误包络构造 / /api/* 路径守卫
   │  ├─ client.ts              # 类型化客户端（超时、包络解析、契约外 code 拒绝）
   │  └─ mode.ts                # mock/live 模式 + 默认 shop_id
   ├─ mock/
   │  ├─ mockServer.ts          # 浏览器内假后端（按 §7 实现，含 §4.4/§5.5 校验）
   │  └─ flags.ts               # 仅 Mock 的开关（扩展字段）
   ├─ hooks/                    # useClient / useResource（轮询+竞态守卫）/ useElementWidth
   ├─ lib/                      # approve / outcome / format / metrics / impact / paramSchema / evidence / dates
   ├─ components/               # LineChart / MetricCard / ApprovalCard / ParamEditor / StatusPill / Feedback
   ├─ pages/                    # ApprovalsPage / DashboardPage / LogsPage
   └─ App.tsx / main.tsx / styles.css
```
