# doudian-agent · 可人工介入的业务处置工作流（Human-in-the-Loop Workflow）

> **一句业务担心**：「这个店昨天还卖得动，今天 GMV 掉了、退款还突然多了 —— 到底出了什么事，
> 要不要现在降价或者清库存？等我看完数据，黄花菜都凉了。」

**先说清楚这是什么，也说清楚它现在不是什么。**

它是一条**步骤固定、状态可存、涉钱必须人点头、失败可回放**的编排工作流：

```
采集 → 算指标 → 按规则找异常 → 提议动作 → 【人审批：挂起】→ 恢复 → 执行 → T+1/T+3 回看效果
```

**它目前不含大模型调用。** 判定完全由 `config/rules.yaml` 里的白名单规则给出
（可读、可审计、改一行就改行为）。这个选择是有意的：涉钱的动作如果需要"模型觉得该改价"
才执行，你就得为它的错觉负责；规则能被人逐条 review，模型不能。

> 什么时候会引入 LLM：只在**它出错也不致命**的位置——异常的自然语言解释、
> 给运营的动作建议草稿、日志摘要。引入时的硬约束是：结构化输出 + schema 校验 +
> 超时 + 失败降级回规则结论，**且不参与最终决策**（人仍是最后一票）。
> 这一项在契约 §13 里记为未落地，没有假装已实现。

这条工作流的三个工程要点，也是它跟"demo 级 Agent"的区别：

| 工作流要点 | 具体做法 |
|---|---|
| **可中断、可恢复** | 审批点用 LangGraph `interrupt()` 挂起，状态落 SQLite/Postgres checkpoint：进程重启、部署、OOM 之后，人回来点"通过"仍然能接着跑（不是内存里的假挂起） |
| **挂起 ≠ 失败** | `node.trace` 用三态 `outcome ∈ {ok, interrupted, error}` 区分，`level` 由 outcome 派生——把"等人审批"画成红色告警，是这类系统最常见的自欺 |
| **涉钱动作必须留痕** | 动作有 `action_id` 身份、`action_log` 账本、幂等键在审批之后生成，重复裁决返回 409 而不是执行两次 |

**业务场景是抖店运营，但结构是通用的**：把"采集/判定/动钱/回测"换成别的行业，
换掉的只是规则和动作类型，工作流骨架不动。没有抖店凭据也能跑
（`demo` 模式走离线夹具，零外网、零密钥）。

它解决的运营问题也很具体：

| 运营卡点 | 这里怎么解 |
|---|---|
| 发现太慢——等报表出来，改价的窗口已经过了 | 规则引擎在采集后立刻判定，异常进审批台而不是等人翻表 |
| 不敢放手——让 Agent 自动改价/下架，改错了谁负责 | 涉钱动作一律 `interrupt` 挂起，人批了才执行；审批记录带 `action_id` 可追溯 |
| 说不清效果——改了之后到底有没有用 | T+1/T+3 观察窗 + `effect_snapshot`，并且**明写"无 A/B 时只说明相关、不说明因果"** |

- 有并发、有幂等、有对账：同一个 `(shop_id, run_id)` 重复触发不会跑两遍（回放已存结果）
- 动作有账本：`action_log` 记下发参数与回执，`effect_snapshot` 记观察窗，`agent_traces` 记 run 摘要
- 观测有两条路：run 摘要进表（看板/联调），节点明细进 JSONL（日志页/对账）

### 这个项目**不是**什么

避免读者按错误的预期打开它：

| 它不是 | 为什么容易被误认 | 实际是什么 |
|---|---|---|
| 聊天机器人 | 提到 Agent 就想到对话 | 没有对话循环；是一条固定步骤的工作流，判定由 YAML 规则给，不由模型自由发挥 |
| 全自动改价工具 | 能改价、能下架 | **默认不自动动钱**：涉钱动作挂在 `interrupt` 上等人点头；无人批就一直挂着，不会超时自动放行 |
| 电商专用系统 | 业务是抖店 | 骨架（采集/判定/审批/回测）与行业无关；换行业只换规则与动作类型 |
| 一个"跑通就行"的 demo | 有前端、有 demo 模式 | demo 只是入口；真正的工作量在幂等、断点恢复、错误分类、可回放审计这些不出彩但决定能不能上生产的地方 |

---

## 架构

```
   浏览器（看板 / 审批台 / 日志页）
        │  frontend: Vite + React + TS，mock 模式读本地 fixtures 或 live 模式打 /api/*
        │  HTTP JSON（金额一律**整数分**，时间一律 UTC，业务日期另用 Asia/Shanghai）
        ▼
   FastAPI（backend/app/api）—— 5 个端点 + 统一错误包络 + 每个响应带 X-Trace-Id
     POST /api/agent/run   触发一次 run（202；已终态则 200 回放）
     POST /api/approve     提交人工裁决并恢复同一 thread
     GET  /api/alerts      审批台首屏（读 approval_request 表）
     GET  /api/metrics     店铺级日粒度指标
     GET  /api/logs        执行日志（读 JSONL）
     GET  /api/healthz     存活探针（db + checkpointer）
        │
        ▼
   LangGraph 状态机（backend/app/agent/graph.py）

     START → collect ──(订单缺失: failed)──────────────────────────┐
                │                                                   │
                ▼                                                   │
          compute_metrics → detect_anomalies → propose_actions ──(无动作)──┤
                                                   │                   │
                                                   ▼                   │
                                            approval_gate              │
                                          interrupt()  ── 人工裁决 ──►  │
                                                   │ (resume)          │
                                                   ▼                   │
                                               execute → record_effect ┤
                                                                       ▼
                                                                   finalize → END
        │                    │                        │
        ▼                    ▼                        ▼
   SQLite/Postgres       LangGraph checkpoint     JSONL 日志汇
   4 张表：agent_traces  （HITL 的持久化底座：   node.trace → /api/logs
   approval_request      进程重启后审批台还能    （step_count 对账的事实源）
   action_log            看到待办）
   effect_snapshot
```

---

## 三个设计决策（以及为什么不那么做）

### 1. 涉及钱的动作必须 `interrupt()`，不自动放行

改价、改库存打到上游是**不可撤销**的，而抖店开放平台**没有客户端幂等键**：
请求超时了，你不知道"那次到底生效没有"。

- 图在 `approval_gate` 用 LangGraph 的 `interrupt()` 真的**停下来**，进程重启也不会丢
  （检查点持久化，见 `docs/DECISIONS.md` 的 ADR-002）。
- 幂等键（`(shop_id, action_type, target, params)`）在**审批之后**才生成——先审批后执行，
  所以"同一个动作点两次"不会变成两次改价（ADR-006）。
- `agent.auto_approve_risk=none` 是默认值：只有显式配置成 `low` 才会自动通过低风险动作；
  `critical` 异常产出的动作**永不**自动通过。
- 审批接口要求带 `interrupt_id` 且必须等于当前挂起的中断：拿旧页面点第二次会被
  404 `APPROVAL_NOT_FOUND` 挡住，而不是把旧裁决喂给新的挂起点。

> 备选方案（否掉）：让 LLM 直接执行、事后再看结果。失败模式是"钱已经花了才发现批错"，
> 且没有任何一处可以放人。对一个碰钱的系统，"能停下来等人"比"能自己跑完"更值钱。

### 2. `4xx` 不重试；只有 `429` 才重试可变操作

抖店返回 **HTTP 200 + body `err_no != 0`** 是常态，所以只看状态码必然误判（ADR-011）：

| 类别 | 重试？ | 理由 |
|---|---|---|
| `429` 限流 | ✅（可变操作也重试） | 请求被明确拒绝，上游没执行 |
| 超时 / 连接错误 / `5xx` | 只读重试；可变操作**不重试** | 可变操作遇超时 = **结果不确定**，重发等于赌"上一次没生效" |
| `4xx`（400/401/403/404/413） | ❌ | 请求本身错了。重试只会用同样的错误再打一次，白耗限流配额，还把真实原因埋在重试日志里 |
| 签名错误 | ❌ + 告警 | 这是部署事故（密钥/时钟/拼装错了），不是业务问题，重试一万次也不会好 |
| `auth`（token 失效） | 强制刷新 token **一次**再试 | 唯一"重试有意义"的鉴权场景；再失败就整个 run 停 |
| 业务错误 `err_no != 0` | ❌ | 该动作记 `failed` + `error_message`，继续下一个动作，不让一个坏动作吞掉整轮 |

结果不确定的写操作进 `action_log.status="unknown"` 并对账（§9.3），不靠重试掩盖。

### 3. 效果必须 T+1/T+3/T+7 回看，而图里绝不等

一个 run 不可能把线程挂三天：审批、进程重启、上游限流都会把它拖死。所以（ADR-003）：

- 图内只登记**观察窗**（`effect_snapshot`，`offset_days ∈ {0,1,3,7}`，`due_at` 由执行时刻算出），
  然后 `finalize` 结束这次 run。`approval` 响应里的 `effect_report` 会明确写
  「尚未到达评估时间」，而不是编一个结论。
- 到点由 `run_effect_evaluator_once()` 拉上游**当前**状态做 `followup`，与 `baseline` 求数值差，
  写 `delta` —— 只记差，不写因果（ADR-008）。
- `attribution` 字段固定 `single_arm_before_after`，`caveats` 里写明"无对照组"。
  说"提升了"需要 A/B 对照；没有对照就只许说"这段时间这两个数变了"。

---

## 5 分钟跑起来

### 0. 前置

| 依赖 | 版本（本仓库实测） |
|---|---|
| Python | 3.12（`py -3 --version` → 3.12.8） |
| Node | ≥ 18（实测 v24/v25 均可；前端用 `npm`，**不用 pnpm/yarn**） |
| Docker（可选） | Compose v2；不装 Docker 也能本地跑 |

### 1. 装依赖 + 跑 demo（**零凭据**，30 秒）

```powershell
# 在仓库根目录
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# demo：采集 → 指标 → 检测 → 审批 → 执行 → 效果窗口，全程离线
.\.venv\Scripts\python.exe backend\tests\fixtures\demo\run_demo.py
```

macOS / Linux 把 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`，其余相同。

demo 会打印一条真实跑出来的链路（下面是本机实测输出，只删了重复行）：

```
==============================================================================
doudian-agent · demo（零凭据 / 离线夹具 / 固定时钟 2026-10-23 20:00 Asia/Shanghai）
==============================================================================
  业务日期          2026-10-23
  店铺            12345678（夹具里的「示例旗舰店」）
  上游模式          dry_run=true → CassetteTransport 回放 order.searchList / product.listV2 …
  本次 run        demo-export-fixture
  数据目录          G:\SN\doudian-agent\data\demo（每次运行前清空；--keep-data 保留）
  夹具目录          G:\SN\doudian-agent\backend\tests\fixtures\doudian\cassettes
...
[1/6] 触发 run：POST /api/agent/run（采集 → 指标 → 检测 → 待审批）
  HTTP 202 status=awaiting_approval trace_id=67639c357ddc4b98bf49bb3cbc59232f
  thread_id     12345678:demo-export-fixture
  → 图停在 approval_gate：涉及资金/库存的动作必须人工裁决，不自动放行。
...
[2/6] 读审批台：GET /api/alerts（数据源是 approval_request 表，不是内存）
  HTTP 200 待办 1 条
  action_id     demo-export-fixture:stock_update:3400000000000000002
  动作            stock_update → 3400000000000000002
  参数            {"stock": 0}
  风险            low
  理由            退款率超过 8%（refund_rate=0.333333，条件 refund_rate is not None and refund_rate > 0.08）（规则 refund_spike）
  证据            {"anomaly_id": "…", "metrics": {"aov_cents": 18567, "date": "2026-10-23", "gmv_7d_avg_cents": null, "gmv_cents": 55700, "refund_rate": 0.333333}, "rule_id": "refund_spike", "target_snapshot": {…}}
...
[3/6] 人工裁决：POST /api/approve（operator=demo_ops, verdict=approve）
  HTTP 200 status=done
  applied       {"action_id": "…:stock_update:3400000000000000002", "action_log_id": "4949…", "error": null, "status": "succeeded", "verdict": "approve"}
  归因口径          single_arm_before_after
  报告结论          效果窗口已登记，尚未到达评估时间（最早 T+1）。
...
[4/6] 回测：run_effect_evaluator_once()（图内不等 T+1/T+3/T+7，由调度器到点评估）
  评估时间推到 2026-10-24T12:00:00+00:00（T+1），本次评估 2 个窗口
  T+0  status=evaluated  due=2026-10-23T12:00:00 delta={"stock": {"after": 42, "before": 0, "diff": 42}}
  T+3  status=scheduled  due=2026-10-26T12:00:00 delta={}
  T+7  status=scheduled  due=2026-10-30T12:00:00 delta={}
  caveats: ["无对照组，指标变化可能来自季节性/活动/竞品等外部因素", "归因强度：弱（单臂前后对比）"]
...
[5/6] 看板数据：GET /api/metrics（金额单位：分）
  HTTP 200 共 1 个业务日
  date            gmv(分)    订单    支付    退款单     aov      退款率    uv     cvr
  2026-10-23       55700     3     3      1   18567   0.3333   480  0.0063
[6/6] 执行日志：GET /api/logs（节点级事实源是 JSONL，不是表）
  HTTP 200 本次 run 共 9 条事件
  #12   info  node.trace                 node=approval_gate
  #18   info  node.trace                 node=execute
  ...（collect / compute_metrics / detect_anomalies / propose_actions / record_effect / finalize 各一条）
==============================================================================
healthz → HTTP 200 {"checkpointer": "ok", "db": "ok", "status": "ok", "time": "…", "version": "0.1.0"}
全链路跑通：采集 → 指标 → 检测 → 审批（interrupt/resume）→ 执行 → 效果窗口。
```

想演示"人工否决"：`run_demo.py --verdict reject`（动作记 `rejected`，不写 `action_log`，
效果窗口一个都不登记）。

### 2. 起 API 服务

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
# 另一个终端
curl.exe http://127.0.0.1:8000/api/healthz
# → {"status":"ok","version":"0.1.0","db":"ok","checkpointer":"ok","time":"…"}
```

接口文档：<http://127.0.0.1:8000/docs>（FastAPI 自带）。

### 3. 起前端（看板 / 审批台 / 日志页）

```powershell
cd frontend
npm install
npm run dev      # → http://localhost:5173
```

默认 **mock 模式**（读 `frontend/mock/*.json`，不需要后端）。要打真后端：页面右上角切到
`live`，或把 `frontend/.env` 的 `VITE_API_MODE` 设成 `live`
（后端已配好 CORS：`http://localhost:5173`）。

### 4. 跑测试

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

零外网、零凭据（`dry_run` + `httpx.MockTransport` + 固定夹具）。**当前 210 passed。**

### 5. Docker（一条命令起前后端）

```powershell
docker compose up --build
# 后端 http://localhost:8000/docs ／ 前端 http://localhost:5173（nginx 反代 /api → backend）
```

开发默认 SQLite（卷挂 `./data`）；**生产切 Postgres 只改一行** `DATABASE_URL`
（`postgresql+psycopg://…`），代码零改动（ADR-012）。

---

## 接真实抖店（需真实凭据）

1. `Copy-Item .env.example .env`，填 `DOUDIAN_APP_KEY` / `DOUDIAN_APP_SECRET` / `DOUDIAN_ACCESS_TOKEN`
   （`.env` 已被 `.gitignore` 忽略；仓库里没有任何加载 `.env` 的代码，由 shell/容器注入环境变量）。
2. 把 `backend/config/settings.yaml` 的 `doudian.dry_run` 改成 `false`。
3. 需要飞书通知就把 `notifier.mode` 改成 `feishu` 并填 `FEISHU_WEBHOOK_URL`（缺了会**启动失败**，不静默降级）。

> `app.env=prod` 且 `doudian.dry_run=true` 时进程**拒绝启动**：那种组合会让接口全 200、
> `action_log` 全 `succeeded`，而上游一个字节都没改——属于没人会去翻日志的沉默型事故。

⚠️ **签名实现只用黄金向量验证过，没有在真实环境跑通过**（见下方已知限制 3）。

---

## 目录结构

```
backend/
  app/
    main.py            # create_app + lifespan（配置闸 → 日志汇 → 建表 → 运行时 → 检查点 → 对账）
    config.py          # yaml + 环境变量 → Settings（未知字段/未解析占位一律启动期报错）
    models.py db.py    # 4 张表（agent_traces/approval_request/action_log/effect_snapshot）
    schemas.py         # §7 全部请求/响应模型（pydantic v2，金额 *_cents）
    api/               # 6 个端点 + 错误包络 + X-Trace-Id 中间件
    agent/             # graph.py（状态机）nodes.py（8 个节点）tracing.py（trace/失败判定）
    services/          # 抖店 client（签名/限流/重试）、指标、规则、审批、效果
    observability/     # JSONL 日志汇（带脱敏）
  config/              # settings.yaml + rules.yaml（密钥只写环境变量名，不写值）
  tests/               # 210 个测试；tests/fixtures/ 是离线夹具（也是 demo 的数据源）
frontend/              # Vite + React + TS：看板 / 审批台 / 日志页（mock + live 两种数据源）
docs/                  # CONTRACT.md（契约，冻结）+ DECISIONS.md（19 条 ADR）+ RUNBOOK.md
backend/tests/fixtures/demo/   # demo 入口 + 从真实运行导出的事件流 fixture（附生成脚本）
```

> 项目还维护内部的契约评审与决策记录；**对外仓库只包含 `docs/CONTRACT.md`、
> `docs/DECISIONS.md` 与 `docs/RUNBOOK.md`**（前两份最能说明"为什么这么设计"），
> 过程中的人评报告与本机环境探测记录不入库。

---

## 已知限制（别把它们当"待办事项"，它们都是有意的取舍）

1. **`uv` / `cvr` 在上游不返回访客数时是 `null`，不是 `0`**。看板显示 `—`。
   `null`（没有数据）与 `0`（转化率真的是 0）必须区分，混起来会让运营按"0% 转化"决策。
2. **没有 A/B 对照时，效果回测只能说明"相关"，不能说明"因果"**。
   `effect_snapshot.attribution` 固定 `single_arm_before_after`，`caveats` 里写死这条。
3. **抖店签名以官方 SDK 为准**：本仓库的签名实现（HMAC-SHA256 + 规范串）有黄金向量测试，
   但**未在真实环境验证过**。接真机第一件事是拿官方 SDK 的签名结果对一遍。
4. **成本口径是店铺级加权，会稀释单品亏损**。毛利率只对**配了成本**的 SKU 加权
   （`config/sku_costs.yaml`，商家自维护——抖店 API 不提供成本）。两个真实后果：
   - 一个成本都没配 → `gross_margin` 是 `null` 且 `loss_making` **不触发**（宁可漏报，
     不误判成本为 0——那会让 Agent 去给正常商品改价下架）；
   - 单品亏损会被盈利单品稀释：demo 里两件真丝衬衫各亏 ~40%，一条围巾赚 ~77%，
     加权后只剩 **−11.3%**。若结构是"少数亏、多数赚"，店铺级规则**数学上不会触发**。
   这是"先店铺级、再 SKU 级"的分阶段口径的固有代价，不是 bug。**SKU 级成本归属未实现**，
   所以 `loss_making` 目前是**只告警不给动作**：它指不出该动哪个 SKU，用"GMV 最高的 SKU"
   去猜只会误伤好货。
5. **效果评估没有常驻调度器**：仓库只提供 `run_effect_evaluator_once()`（一次 tick），
   生产上要由 cron / 任务队列定时调用；`effects.scheduler_enabled` 目前只是配置位。
6. **`/api/metrics` 的数据源是 checkpoint**（四张表里没有指标表），所以只覆盖"跑过 run 的
   那些业务日"，缺的日期**不补零**；要真正的历史趋势得先做指标持久化（契约变更 + ADR）。

---

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/CONTRACT.md`](docs/CONTRACT.md) | 冻结的接口/数据/错误码契约（字段、语义、不变量） |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | 19 条 ADR：为什么 interrupt、为什么 4xx 不重试、为什么效果移出图…… |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | 跑起来/排障/demo 数据重生成的具体命令 |
