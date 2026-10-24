# CONTRACT.md — 抖店智能运营 Agent 契约（单一事实源）

- 版本：**1.5.2**（冻结；**v1.5.2 = 行级账目补全（纯增量）**——把 v1.5.1 漏登记/漏实现的四处补上：①`collect.row_rejected` 的 `data` **确实**带 `rows_seen` / `rows_accepted`（v1.5.1 的代码已写，本版把"不在 JSONL"那句改成事实），并新增 `rows_out_of_window`；②`collect.done` / `collect.failed` / `collect.row_rejected` 三处都带 `rows_out_of_window`，冻结行级恒等式 `rows_seen == rows_accepted + rows_rejected + rows_out_of_window`（`collect.row_rejected` 里"被拒"项就是它自己的 `count`，不重复等值键），由代码显式断言；③行级判定表补 **`no_known_fields`** 档（`{}` / `{"foo": 1}` 不是"一笔 0 元订单"）；④新增 **`collect.date_fallback`**（warn）：行被**接受**但 `create_time` 存在却不可用、靠 `pay_time` 才解析成功时必须留痕且 `source.partial=true`，**不计入** `row_rejected`。**`node.trace` 键集合、REST 形状、错误码、路由、DB 表列均未改**；v1.5.1 = 审计事件登记（§7.6.3），纯增量**——把 `backend/app/agent/nodes.py` 已经在写、契约里却没有位置的 6 个审计事件（`metrics.computed`、`anomaly.detected` 的 `summary` 与 `detail` 两形态、`collect.row_rejected`、`collect.units_degraded`、`collect.zero_revenue_paid`、`sku_costs.unmatched`）按**代码实际字段**登记进 §7.6，并补采集层**行级判定表**、`samples` 截断语义与 `source.partial` 的含义；**`node.trace` 键集合一个字都没改**，`/api/logs` 的 `event` 仍是自由字符串（前端不需要改）；v1.5.0 = 成本/毛利率落地，纯增量——§3.3 `MetricsSnapshot` 新增 `gross_margin` / `cost_coverage_ratio`（**店铺级**成本口径）、§5.3 `compute_metrics(..., cost_table=…)` 由"已入册未实现"转为**已实现**并新增数据面 `order_summary["by_sku"]`、§8.3.3 `ALLOWED_NAMES` 新增这两个名字且冻结「SKU 侧必须叫 `sku_gross_margin`」、§8.3 新增 `loss_making` 规则（**只告警不给动作**）、§8.5 补成本表实现细节并把"启动期交叉校验"改写为**运行期审计**；**未完成项见 §13 的 1.5.0 行**；v1.3.1 补 §4.7 规则 5c2 与 §7.6.1 的 `error.type`；v1.3.2 登记 `app/api/queries.py` 的只读边界与 §7.5 数据源；**v1.4.0 合并此前未登记的两批增量**——原拟 1.3.3 的 **§7.6.2 脱敏两层与「必须测通道」**（并入本版，不再单独计版本）+ 正文里**已存在但 §13 未登记**的 **SKU 维度与成本表**条文（§3.3 / §5.1 / §5.3.1 / §5.4 / §8.3.3 / §8.5 / §10.3 / §11 的 `v1.4.0` 标注自此有 §13 依据），并结清「正文标 1.4.0、版本行停在 1.3.x」的欠账；**纯增量**：不改既有签名（新增参数全部 keyword-only 带默认值）、不改表列、不改 JSON 键、不改错误码，见 §13）
- 维护者：spec-lead（唯一有权修改本文件的人）
- 适用方：agent-core / platform / frontend 三方**并行开发**
- 冻结规则：本文件中的**路径、名称、签名、字段名、枚举值、JSON 键、错误码**均为契约正文。任何一方需要偏离，必须先提给 spec-lead 改本文件并升版本，**不得**在自己代码里"先这么写"。
- 读到歧义 = 停下问，不许猜。猜出来的东西集成时一定对不上。

契约正文 `<code>` 块中标「逐字」的代码必须与实现完全一致（可复制粘贴）。其余为规范描述。

---

## 0. 全局约定（先读，违反即集成失败）

| 约定 | 规定 |
|---|---|
| 语言/运行 | Python **3.12.8**，解释器 `G:\SN\doudian-agent\.venv\Scripts\python.exe`；Node 见 `docs/ENV.md` |
| 命名 | Python/JSON/DB 一律 **snake_case**；类名 PascalCase；常量 UPPER_SNAKE |
| JSON 键 | 与 Python 字段名 **1:1 相同**，不做 camelCase 转换。前端直接用 snake_case |
| 时间 | 所有时间戳是 **UTC ISO-8601 带 `Z`**：`2026-10-23T14:25:30Z`（毫秒可选：`...:30.113Z`）。**业务日期**（`date` 字段）是 `Asia/Shanghai` 的 `YYYY-MM-DD` |
| 金额 | **整数分（cent）**，字段名必须以 `_cents` 结尾。禁止 float 金额、禁止元 |
| 比例 | float，保留 6 位小数（`round(x, 6)`）。分母为 0 时返回 `None`，**不是 0** |
| ID | `run_id` `^[A-Za-z0-9_-]{1,64}$`；`shop_id` `^[A-Za-z0-9_-]{1,64}$`；`trace_id` 32 位 hex；`action_id` 见 §4.5；`idempotency_key`/`payload_digest` 64 位 hex（sha256） |
| 空值 | "没有"统一用 `null`，不用 `""`、不用 `-1`、不省略键 |
| 布尔 | 配置与响应里一律 `true/false` |
| 接口风格 | 四个 agent 入口全是 `async def`（`build_graph` 除外，它是同步工厂）；服务层 I/O 全 async |
| 日志 | 结构化 JSON（§7.6），字段名同样 snake_case |
| 测试 | 无真实凭据必须能跑通全链路（§10） |

### 0.1 冻结的仓库结构

```
doudian-agent/
├─ docs/                       # 本目录（spec-lead 独占写权限）
│  ├─ ENV.md
│  ├─ CONTRACT.md
│  └─ DECISIONS.md
├─ backend/
│  ├─ requirements.txt
│  ├─ pytest.ini
│  ├─ config/
│  │  ├─ settings.yaml
│  │  ├─ rules.yaml
│  │  └─ sku_costs.yaml       # SKU 成本表（§8.5，v1.4.0；抖店 API 无成本字段）
│  ├─ app/
│  │  ├─ __init__.py
│  │  ├─ main.py               # create_app() + lifespan
│  │  ├─ config.py             # Settings / load_settings / load_rules / get_settings
│  │  ├─ db.py                 # engine / session / init_db / run_db / to_utc / enable_sqlite_wal
│  │  ├─ models.py             # ORM：agent_traces / approval_request / action_log / effect_snapshot
│  │  ├─ schemas.py            # FastAPI 请求体/响应体（pydantic v2）
│  │  ├─ enums.py              # RunStatus/Severity/ErrorCategory/ActionType/Verdict/...
│  │  ├─ errors.py             # 应用层异常 + HTTP 映射（§5.0）
│  │  ├─ observability/
│  │  │  ├─ __init__.py
│  │  │  └─ logging.py         # JsonlLogSink / LogEvent / trace_id contextvar
│  │  ├─ agent/
│  │  │  ├─ __init__.py        # 只 re-export 四个公开入口
│  │  │  ├─ state.py           # AgentState + 子结构（§3）
│  │  │  ├─ graph.py           # build_graph/run_agent/resume_agent/list_pending_approvals（§4）
│  │  │  ├─ nodes.py           # 8 个节点（§4.3）
│  │  │  ├─ adapters.py         # 进程级运行时装配 + 依赖解析（§4.8，ADR-017；禁止任何表仓储）
│  │  │  ├─ checkpoint.py      # checkpoint_saver()（§5.7）
│  │  │  └─ tracing.py         # traced() 节点包装器（§4.7，唯一允许写 step_count 的地方）
│  │  ├─ services/
│  │  │  ├─ __init__.py
│  │  │  ├─ errors.py          # 抖店侧异常族（§5.1.2）
│  │  │  ├─ doudian_client.py  # DoudianClient（§5.1）
│  │  │  ├─ signing.py         # 纯函数签名实现（§5.2）
│  │  │  ├─ metrics.py         # compute_metrics（§5.3）
│  │  │  ├─ anomalies.py       # detect_anomalies（§5.4）
│  │  │  ├─ actions.py         # propose_actions + 参数 schema 校验（§5.5）
│  │  │  ├─ rules.py           # 白名单 AST 求值（§8.3）
│  │  │  ├─ notifier.py        # Notifier 抽象 + DryRun + Feishu（§5.6）
│  │  │  ├─ approvals.py       # approval_request 仓储（§5.8）
│  │  │  └─ effects.py         # 快照登记 + 调度器（§5.9）
│  │  └─ api/
│  │     ├─ __init__.py
│  │     ├─ deps.py            # get_session / get_runtime / get_doudian_client
│  │     ├─ queries.py         # 只读辅助查询（§0.1 边界：仅 SELECT，禁止任何写；v1.3.2 登记）
│  │     ├─ routes_agent.py    # POST /api/agent/run
│  │     ├─ routes_alerts.py   # GET  /api/alerts
│  │     ├─ routes_approve.py  # POST /api/approve
│  │     ├─ routes_metrics.py  # GET  /api/metrics
│  │     ├─ routes_logs.py     # GET  /api/logs
│  │     └─ routes_health.py   # GET  /api/healthz
│  └─ tests/                   # §10
├─ frontend/
│  ├─ package.json             # scripts: dev / build / preview
│  ├─ src/
│  │  ├─ api/
│  │  │  ├─ client.ts          # fetch 封装，错误包络解析
│  │  │  └─ types.ts           # §12 的 TS 类型（逐字）
│  │  ├─ components/
│  │  ├─ pages/                # Approvals / Dashboard / Logs
│  │  └─ main.tsx
│  └─ vite.config.ts           # dev proxy: /api -> http://127.0.0.1:8000
├─ data/                       # 运行时产物，git 忽略
│  ├─ doudian.db
│  ├─ checkpoint.db
│  └─ logs/app.jsonl
├─ docker-compose.yml
├─ Dockerfile.backend / Dockerfile.frontend
├─ .env.example
├─ .gitignore
├─ README.md
└─ DECISIONS.md                # 根目录放指针，正文在 docs/DECISIONS.md
```

**归属（captain 指派，v1.1.0 落表、v1.2.0 补重叠裁定）**：本文件冻结路径与签名；下列归属表冻结"谁能写哪个文件"。越界修改 = 集成冲突，不是风格问题。

| 角色 | 拥有路径（写权限） |
|---|---|
| spec-lead | `docs/**`（`docs/RUNBOOK.md`、`docs/VERIFY.md`、`docs/REVIEW.md` 三个文件除外，逐文件裁定见本节末尾） |
| agent-core | `backend/app/enums.py`、`backend/app/errors.py`、`backend/app/agent/**`（`state.py` / `graph.py` / `nodes.py` / **`adapters.py`**（v1.3.0 入表，§4.8）/ `checkpoint.py` / `tracing.py`）、`backend/config/settings.yaml`、`backend/config/rules.yaml`、**`backend/config/sku_costs.yaml`**（v1.4.0 入表，§8.5）、`backend/app/services/{rules,anomalies,actions,approvals,effects}.py`、`backend/tests/agent_core/**` |
| platform | `backend/app/config.py`、`backend/app/db.py`、`backend/app/models.py`、`backend/app/schemas.py`、`backend/app/observability/**`、`backend/app/services/{errors,signing,doudian_client,metrics,notifier}.py`；**t5 阶段追加**：`backend/app/main.py`、`backend/app/api/**`、`backend/scheduler.py`、`backend/tests/e2e_mock.py` |
| frontend | `frontend/**` |
| devops | `docker-compose.yml`、`Dockerfile.backend`、`Dockerfile.frontend`、`.env.example`、`.gitignore`、`README.md`、`docs/RUNBOOK.md` |
| qa | `backend/tests/**`（`backend/tests/agent_core/**` 与 `backend/tests/e2e_mock.py` 除外）、`scripts/**`、`docs/VERIFY.md` |
| reviewer | `docs/REVIEW.md`（只读代码，不改实现） |

归属边界的三条硬规则：
1. `docs/` 下除 `RUNBOOK.md`(devops) / `VERIFY.md`(qa) / `REVIEW.md`(reviewer) 之外的文件全部归 spec-lead。**任何角色都不许改 `docs/CONTRACT.md` 与 `docs/DECISIONS.md`**，要改走 §13 变更流程。
2. 一个文件只有一个 owner。跨角色需要改动对方文件时，发消息给对方改，不"顺手改一行"。
3. 共享只读面：所有角色都可以读全部文件；"只读"不构成写权限。

**`docs/` 归属重叠的裁定（captain 于 v1.2.0 复核确认，冻结）**

| 文件 | owner | 说明 |
|---|---|---|
| `docs/CONTRACT.md` | spec-lead | 任何角色不得改；变更走 §13 |
| `docs/DECISIONS.md` | spec-lead | 同上 |
| `docs/ENV.md` | spec-lead | 环境探测结果 |
| `docs/RUNBOOK.md` | devops | 部署/排障手册；spec-lead 不代写 |
| `docs/VERIFY.md` | qa | 验收证据；spec-lead 不代写 |
| `docs/REVIEW.md` | reviewer | 代码评审结论；spec-lead 不代写 |

即：spec-lead 的 `docs/**` 写权限**排除**上面三份他人文件；三份文件的 owner 也**不得**借此改动 CONTRACT/DECISIONS。

**`app/api/queries.py` 的登记与边界（v1.3.2）**

`backend/app/api/queries.py` 是入口层新增的**只读**辅助查询模块，归属 **API 层（platform，`backend/app/api/**`）**。它存在的理由是两条**冻结签名覆盖不到的读路径**：

1. §5.8 的 `list_by_status` 返回 `PendingApproval[]`（**不含行 `id`**），而 §7.3 的 `next_cursor` 要求"最后一条 `id`"；
2. §4.7 的 `agent_traces` 仓储只按 `(shop_id, run_id)` 取单行，而 §7.5 的 `/api/metrics` 需要"这家店铺有哪些 run"。

**这与 `adapters.py` 是同一个教训（ADR-017）：树里没有的文件会自由生长**——它不会被任何人认领，也不会被任何断言约束。所以处理方式不是禁止，而是**登记 + 把边界写成可检查的形态**：

- **只读**：只允许 `SELECT`；**禁止**任何 `INSERT` / `UPDATE` / `DELETE`（写路径各自唯一：`approval_request` → `services/approvals.py`，`agent_traces` → `agent/tracing.py`）。
- **禁止触达 `step_count`**：不得派生、不得校准、不得 `+= 1`——那仍是 `tracing.py` 的唯一职责（§4.7 断言 A）。
- **禁止绕过 `tracing.py` 的仓储做 run 摘要写操作**：本模块没有写 run 摘要的能力；`status` / `finished_at` 只由 `run_agent` / `resume_agent` / `finalize` 经 `tracing.py` 落库。
- **不做业务判断**：不改状态机、不算幂等键、不分组告警；只把库里的值原样交给调用方（时间列过 `to_utc()` 后转 ISO）。
- 可检查形态：`grep -nE '\.add\(|\.delete\(|\.flush\(|\.commit\(' backend/app/api/queries.py` 为空；`grep -nE 'step_count\s*=[^=]|step_count\s*\+=' backend/app/api/queries.py` 为空。
  > 注：**不要**用 `grep -n "step_count" ...` 这种"名字出现次数"写法——本文件的 docstring 正当地提到"禁止派生 `step_count`"就会命中，那会制造假阳性并逼出错误修法（§4.7 断言取向，v1.3.0 的同一个教训）。断言只落在**赋值/增量**形态上。

---

### 0.2 逐字冻结块的写法（v1.4.0，通则）

> **逐字冻结块（§3.3 的 TypedDict、§12 的 ts、§5.x 的签名、§4.4 的协议片段）内部只允许放代码本身。**
> 说明性文字一律写在块**外**（`> 注：` 引用块或普通段落）；块内任何**非代码行**都视为契约变更，必须在 §13 登记。

**为什么要有这条**：这本文档里的代码块是**下游逐字比对的目标**——`frontend/scripts/verify.mjs` 把 §12 的 ts 块抽出来与 `src/api/types.ts` 逐行 diff，后端对应模块的 docstring 也写着"逐字对应 §2/§5.0"。所以块内插一行注释，不是"文档更可读"，而是**下游的验收直接变红**。

v1.4.0 真实发生过：为了让读者看懂"净改动为零"，往 §12 的 ts 块里插了 5 行 `//` 注释——`types.ts` 一个字没改，frontend 的 28 项验证却挂了一项（`第 129 行：契约="// v1.4.0 对 §12 的净改动：零。" 实现="export interface LogItem {"`）。**文档编辑变成别人的构建失败**，这是最不该出现的一类耦合。

自查方式（提交前一分钟能做完）：
```bash
grep -n "^//" docs/CONTRACT.md     # 不得命中任何位于 ts/python 代码围栏内部的行
```

---

## 1. 模块导入路径（唯一合法写法）

```python
from app.agent import build_graph, run_agent, resume_agent, list_pending_approvals
from app.agent.state import AgentState, ActionProposal, ExecutionResult, EffectReport, PendingApproval
from app.agent.tracing import NODE_TRACE_EVENT, traced, reconcile_step_count
from app.agent.adapters import (AgentRuntime, build_runtime, get_runtime, set_runtime, reset_runtime,
                                resolve_runtime, resolve_client, resolve_now, digest_of,
                                get_log_sink, set_log_sink, refresh_log_sink, require_log_sink)
from app.services.doudian_client import DoudianClient, DoudianResponse, RateLimitConfig, create_doudian_client
from app.services.errors import (DoudianError, DoudianTransportError, DoudianUpstreamError,
                                 DoudianRateLimitedError, DoudianClientError, DoudianSignatureError,
                                 DoudianAuthError, DoudianBusinessError, DoudianAmbiguousOutcomeError)
from app.services.metrics import compute_metrics
from app.services.anomalies import detect_anomalies
from app.services.actions import propose_actions
from app.services.rules import load_rules, compile_expression, evaluate, RuleSet, RuleLoadError, RuleEvalError
from app.services.notifier import Notifier, DryRunNotifier, build_notifier
from app.services.effects import register_effect_snapshots, run_effect_evaluator_once
from app.db import get_session, session_scope, init_db, run_db, to_utc
from app.config import get_settings, load_settings, Settings
from app.enums import RunStatus, Severity, ErrorCategory, ActionType, Verdict, ApprovalStatus, ActionLogStatus
from app.errors import (AppError, ValidationError, ThreadNotFound, ApprovalNotFound, ResourceNotFound,
                        DuplicateDecision, RunInProgress, RunStateConflict, UpstreamError, InternalError,
                        ApprovalValidationError, ParamValidationError, NoPendingInterrupt, ConfigError)
from app.observability.logging import get_logger, JsonlLogSink, LogEvent, new_trace_id
```

- `app/services/errors.py` 是**抖店上游**异常的唯一定义处；`app/errors.py` 是**应用层**异常的唯一定义处。两者不混用（§5.0）。
- LangGraph 的公共类型来自 `langgraph-checkpoint` 分发包（它是 `langgraph` 的依赖，提供 `langgraph.checkpoint.base` 与 `langgraph.checkpoint.memory`）：
  `from langgraph.checkpoint.base import BaseCheckpointSaver`；测试用 `from langgraph.checkpoint.memory import InMemorySaver`（别名 `MemorySaver`）。
  不要在 `requirements.txt` 里单独 pin 它，跟随 `langgraph==1.2.16` 的传递依赖即可（实测解析到 `langgraph-checkpoint 4.3.0`）。
- `app/agent/__init__.py` 只做 re-export，不得有逻辑。

### 1.1 冻结签名的追加规则（v1.4.0，通则）

> **冻结签名允许追加 keyword-only 参数，但必须同时满足三条：**
> ① **有默认值**，不破坏既有调用（调用方不改一行也能跑）；
> ② **在契约版本里登记**——§13 变更记录里记一行，并在对应节（§4.1 / §5.1 / …）写明语义；
> ③ **在 docstring 里写明"为什么必须在这里，而不是别处"**——即说明该行为值为什么没有别的合法落点。
>
> 未登记的自创参数 = **接口漂移**，评审按 `needs_revision` 处理，不接受"先加上回头补文档"。

**这条为什么存在**：本仓已经三次踩同一个坑——`adapters.py` 没有归属（冻结点漏文件）、`total_budget_s` 没有入口（§9.2 要求了行为、§5.1 没给位置）、`trigger` / `expected_interrupt_id` 没有落点（§7.2/§4.4 要求了行为、§4.1 没给位置）。三次都是同一个模式：**契约冻结了形状，却没冻结边界**。后果固定：实现者只能自己发明落点，而一个"自己发明的落点"就是下一次接口漂移的起点——同一件事在两处各有一套（`AgentRuntime` 两份单例就是这个模型的产物，ADR-017）。

规则的取舍很清楚：**把漂移的成本从下游挪回上游**。实现者发现"没位置"时，正确动作是回来改契约（一次评审），而不是在自己文件里造一个参数（永久分叉）。反过来说，如果这条规则让实现者卡住不动，那说明契约缺的是"落点"而不是"纪律"——那就补落点（`trigger` / `expected_interrupt_id` / `total_budget_s` 三个参数就是这么来的）。

---

## 2. 枚举（逐字，`app/enums.py`）

```python
from enum import StrEnum

class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"

class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class ActionType(StrEnum):
    PRICE_UPDATE = "price_update"
    STOCK_UPDATE = "stock_update"
    COUPON_CREATE = "coupon_create"

class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"

class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"

class ActionLogStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"                # 上游结果不确定，待对账
    SKIPPED_DUPLICATE = "skipped_duplicate"

class ErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER_5XX = "server_5xx"
    RATE_LIMITED = "rate_limited"
    CLIENT_4XX = "client_4xx"
    SIGNATURE = "signature"
    AUTH = "auth"
    BUSINESS = "business"
    AMBIGUOUS = "ambiguous"
    CONFIG = "config"
    UNKNOWN = "unknown"

class Attribution(StrEnum):
    SINGLE_ARM_BEFORE_AFTER = "single_arm_before_after"
    AB_TEST = "ab_test"
    NONE = "none"

class ApiErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    THREAD_NOT_FOUND = "THREAD_NOT_FOUND"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    DUPLICATE_DECISION = "DUPLICATE_DECISION"
    RUN_IN_PROGRESS = "RUN_IN_PROGRESS"
    RUN_STATE_CONFLICT = "RUN_STATE_CONFLICT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
```

枚举序列化后就是上面这些字符串字面量，DB 里存的也是它们（`String` 列，不建 PG enum）。

---

## 3. AgentState（`app/agent/state.py`）

### 3.1 reducer 规则（硬性）

- **所有字段默认 replace 语义**（后写覆盖前写）。**禁止**使用 `operator.add` / 自定义 append reducer。
  原因：LangGraph 的 `interrupt` 恢复会**从节点开头重放该节点**（见 DECISIONS ADR-010），append reducer + 重放 = 重复行。
- 节点返回"该字段的完整目标值"。
- 需要审计事件时，调用 `append_audit(state, entry)`（§3.3），它内部按 `event_id` 去重。

### 3.2 字段表

| 字段 | 类型 | 谁写 | 说明 |
|---|---|---|---|
| `run_id` | `str` | 入口 | 一次运行 |
| `trace_id` | `str` | 入口 | 32 hex，跨进程/HTTP/日志贯穿 |
| `shop_id` | `str` | 入口 | 店铺 |
| `thread_id` | `str` | 入口 | `f"{shop_id}:{run_id}"`，**不可含冒号**（由 §0 字符集保证） |
| `trigger` | `str` | 入口 | `manual` / `scheduled` / `retry` |
| `started_at` | `str` | 入口 | UTC ISO |
| `status` | `RunStatus` | 各节点 | 状态机见 §4.2 |
| `iteration` | `int` | 各节点 | 节点自增守卫，> 20 强转 `failed` |
| `raw` | `RawSnapshot \| None` | collect | 原始数据（§3.3） |
| `metrics` | `MetricsSnapshot \| None` | compute_metrics | 指标（§3.3） |
| `history` | `list[MetricsSnapshot]` | compute_metrics | 近 7 天（含今天），用于均值/环比 |
| `anomalies` | `list[Anomaly]` | detect_anomalies | 异常 |
| `pending_actions` | `list[ActionProposal]` | propose_actions | 待审批提议 |
| `approved_actions` | `list[ActionProposal]` | approval_gate | 审批通过/改写后的**生效**提议 |
| `rejected_actions` | `list[ActionProposal]` | approval_gate | 被拒（保留证据） |
| `approval` | `ApprovalState \| None` | approval_gate | 审批状态（§3.3） |
| `execution_results` | `list[ExecutionResult]` | execute | 执行回执 |
| `effect_report` | `EffectReport \| None` | record_effect | 效果占位报告 |
| `errors` | `list[AgentError]` | 各节点 | 非致命错误累积 |
| `audit` | `list[AuditEntry]` | 各节点 | 审计流（去重追加） |

### 3.3 逐字定义

```python
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from app.enums import ActionType, ApprovalStatus, Attribution, ErrorCategory, Risk, RunStatus, Severity, Verdict


class Window(TypedDict):
    start: str           # 业务日期 YYYY-MM-DD (Asia/Shanghai)
    end: str


class RawSnapshot(TypedDict):
    collected_at: str                      # UTC ISO
    window: Window
    shop: dict[str, Any]                   # 店铺基础信息（原样字段 + 归一化字段）
    products: list[dict[str, Any]]         # 每个商品/规格一条，金额已归一化为 *_cents
    order_summary: dict[str, Any]          # 订单聚合（金额 *_cents）；v1.5.0 起另含 by_sku（见本节末）
    source: dict[str, Any]                 # {"methods": [...], "request_ids": [...], "partial": bool}


class MetricsSnapshot(TypedDict):
    date: str                              # 业务日期 YYYY-MM-DD
    gmv_cents: int
    order_count: int                       # 下单数
    paid_order_count: int                  # 支付数
    refund_order_count: int
    refund_amount_cents: int
    aov_cents: int                          # gmv_cents / paid_order_count，分母 0 => 0
    refund_rate: float | None               # refund_order_count / paid_order_count，分母 0 => None
    uv: int | None                          # 访客数，无数据 => None
    cvr: float | None                       # paid_order_count / uv，任一缺失 => None
    gmv_7d_avg_cents: int | None            # 近 7 日（不含今天）均值，历史不足 => None
    gross_margin: float | None              # v1.5.0 店铺级：只对**有成本**的 SKU 加权，算不出 => None（绝不是 0）
    cost_coverage_ratio: float | None       # v1.5.0 店铺级：有成本成交额 / 全店 GMV，分母 0 => None（**不裁剪**）
    skus: list[SkuSnapshot]                 # SKU 级明细，按 sku_id 升序
    computed_at: str


class SkuSnapshot(TypedDict):
    sku_id: str
    product_id: str
    current_price_cents: int
    cost_cents: int | None                  # 无配置 => None，绝不填 0
    gross_margin: float | None              # 任一缺失或分母 0 => None
    stock: int
    stock_days: float | None                # 分母 0/None => None
    sales_7d: int
    avg_daily_sales_7d: float | None
    is_on_sale: bool
    ad_spend_cents_7d: int
    ad_order_count_7d: int


class Anomaly(TypedDict):
    anomaly_id: str                         # sha256(f"{run_id}|{rule_id}|{metric}|{sku_id or ''}")[:16]
    rule_id: str
    metric: str
    observed: float | int | None
    threshold: float | int | None
    comparator: str                         # "<" "<=" ">" ">=" "==" "!=" 或 "and"/"or" 组合的可读摘要
    severity: Severity
    message: str
    window: str                             # "day" / "7d"
    sku_id: str | None                      # SKU 级异常带该 SKU；店铺级为 None
    detected_at: str


class ActionProposal(TypedDict):
    action_id: str                          # f"{run_id}:{action_type}:{target}"（提议身份，审批前生成）
    action_type: ActionType
    target: str                             # sku_id 或 product_id
    params: dict[str, Any]                  # 归一化后的生效参数（金额用 *_cents）
    idempotency_key: NotRequired[str | None]  # 审批前必须缺省/None；执行阶段由 §4.5 计算
    reason: str
    evidence: dict[str, Any]                # {"anomaly_id":..., "rule_id":..., "sku_id": <str|null>, "metrics": {...}}
                                            # v1.4.0：SKU 级提议必须带 sku_id（与 target 一致）；店铺级为 null
    risk: Risk
    created_at: str


class Decision(TypedDict):
    action_id: str
    verdict: Verdict
    edited_params: dict[str, Any] | None
    operator: str | None
    reason: str | None
    decided_at: str


class ApprovalState(TypedDict):
    interrupt_id: str | None
    requested_at: str | None
    decided_at: str | None
    decided_by: str | None
    decisions: list[Decision]


class ExecutionResult(TypedDict):
    action_id: str
    idempotency_key: str
    status: str                             # ActionLogStatus
    upstream_request_id: str | None
    error: str | None
    error_category: str | None              # ErrorCategory
    attempt: int
    executed_at: str


class EffectWindow(TypedDict):
    offset_days: int                        # 0 / 1 / 3 / 7
    due_at: str
    snapshot_id: str
    status: str                             # "scheduled" | "evaluated" | "missed"


class EffectReport(TypedDict):
    run_id: str
    generated_at: str
    attribution: Attribution
    windows: list[EffectWindow]
    conclusion: str                         # 必须符合 §5.9.3 文案规则
    caveats: list[str]


class AgentError(TypedDict):
    node: str
    category: str                           # ErrorCategory
    message: str
    retryable: bool
    at: str


class AuditEntry(TypedDict):
    event_id: str                           # sha256(json(event))[:16]，去重键
    at: str
    node: str
    event: str                              # "node.start" / "anomaly.detected" / "action.proposed" ...
    data: dict[str, Any]


class AgentState(TypedDict):
    run_id: str
    trace_id: str
    shop_id: str
    thread_id: str
    trigger: str
    started_at: str
    status: RunStatus
    iteration: int
    raw: RawSnapshot | None
    metrics: MetricsSnapshot | None
    history: list[MetricsSnapshot]
    anomalies: list[Anomaly]
    pending_actions: list[ActionProposal]
    approved_actions: list[ActionProposal]
    rejected_actions: list[ActionProposal]
    approval: ApprovalState | None
    execution_results: list[ExecutionResult]
    effect_report: EffectReport | None
    errors: list[AgentError]
    audit: list[AuditEntry]


def append_audit(state: AgentState, entry: AuditEntry) -> list[AuditEntry]:
    """按 event_id 去重追加，返回**完整**新列表（不要用 reducer）。"""
```

`build_graph` 用 `StateGraph(AgentState)`，**不**给任何字段加 `Annotated[..., reducer]`。

**`history` 里 `skus` 必须为 `[]`（v1.4.0 冻结）**：`history` 是"近 7 天店铺级对比"用的，只读其店铺级字段；7 天的 SKU 明细塞进 state 会把 LangGraph checkpoint 放大到 8×N（N = SKU 数），且没有任何消费者（SKU 级 7 日均值由 `raw` 的 7 日聚合直接算出，见 §5.3.1）。**禁止**在 `history` 元素里填 `skus`，也**禁止**新增 `sku_history` 字段（要加得先提契约变更）。

**派生方向（v1.4.0，单向）**：`raw.products`（每个 SKU 一条，金额已是整数分）→ `metrics.skus`（`compute_metrics` 产出）→ `anomalies`（`detect_anomalies` 两轮求值）→ `pending_actions`（`propose_actions`）。**反向禁止**：`compute_metrics` 不得读 `anomalies`，`detect_anomalies` 不得回写 `metrics`。原因是 `interrupt` 恢复会重放节点（ADR-010），单向派生保证重放幂等。

**成本口径的两个字段是「店铺级」的（v1.5.0 冻结，ADR-021）**：`gross_margin` / `cost_coverage_ratio` 描述的是**全店有成本覆盖的那部分成交额**，**不是**任何单个 SKU 的毛利率。三条硬语义（任何一条被"优化"掉都会直接变成误报）：

1. **只对有成本的 SKU 加权**。成本未知的 SKU 既不进分子也不进分母——把它们的成交额塞进分母，等于假设"这些 SKU 的成本 = 售价"（毛利 0），那是**编出来的数**，而且方向不可控（真亏的被摊薄成"还行"，真赚的被摊薄成"亏本"）。
2. **算不出就是 `None`，绝不是 `0`**。四种来源：①老快照没有 `by_sku`（没做按 SKU 归集）；②一个有成本的 SKU 都没有（或配了但今天没卖）；③覆盖率的分母（全店 GMV）为 0；④有成本成交额为 0。写成 `0` 会被读成"不赚不亏"这个**假事实**，而 `0 < 阈值` 还会直接判亏本 → Agent 去改价/下架（本仓最贵的一类误报）。
3. **覆盖率不裁剪**。`cost_coverage_ratio > 1` 是**合法输出**（上游汇总与订单明细对不上），必须如实暴露，**禁止** `min(…, 1.0)` 抹平——抹平等于把数据不一致藏起来。

**`order_summary["by_sku"]` 是成本口径的唯一数据面（v1.5.0 新增，由 `collect` 归集，公式见 §5.3）**：`{sku_id: {"gmv_cents": int, "units": int}}`。口径逐条冻结：

- 只统计**已支付**的订单行（"已支付"的判据是数据不是状态词表：`pay_amount > 0` 或存在 `pay_time`）；
- 明细里**没有 `sku_id`** 的已支付行**不放进 `by_sku`**，只进全店 GMV 分母（计为未覆盖）——它无法归属到任何 SKU，**绝不**为了凑满覆盖率摊到别的 SKU 上；
- **退款单仍计入**成交额与件数（与 `gmv_cents` 的既有口径一致：GMV 含已退款支付额）；退款把成本冲回来（退货入库）是另一个口径，需要单独的净额指标，不在这里偷偷做掉；
- **件数缺失按 1 件**，且这个假设**有方向**：件数偏小 → 成本额偏小 → 毛利率偏高 → 更不容易报"亏本"，即它只会造成**漏报**（可见、可补字段），不会造成"把好货判成亏本"的误报；
- **件数有值但不是非负整数**（float / 字符串 / bool / 负数）→ `ValueError`：静默取 1 会把"上游改了字段类型"永远藏成"每单都只买一件"。

---

## 4. agent 模块对外接口（`app/agent/graph.py`）

### 4.1 四个公开入口（逐字签名）

```python
from collections.abc import Sequence
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.state import ActionProposal, AgentState, Decision, EffectReport


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    """构建并编译状态机。checkpointer=None 时不启用持久化（仅限内部工具/单测）。"""


async def run_agent(
    shop_id: str,
    run_id: str,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    trigger: str = "manual",        # v1.4.0 新增（keyword-only）：manual / scheduled / retry
) -> RunResult: ...


async def resume_agent(
    thread_id: str,
    decisions: Sequence[Decision],
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    expected_interrupt_id: str | None = None,   # v1.4.0 新增（keyword-only）
) -> ResumeResult: ...


async def list_pending_approvals() -> list[PendingApproval]: ...
```

**v1.4.0 两个新增 keyword-only 参数的语义（冻结，逐条实现）**

| 参数 | 为什么必须**在这里** | 语义 |
|---|---|---|
| `run_agent(trigger="manual")` | §7.2 的请求体有 `trigger`，而 §3.3 的 `AgentState.trigger` 必须有值。没有参数就只能从环境猜或硬编码 `"manual"`——请求里写了 `scheduled`、库里存的却是 `manual`，`agent_traces.trigger` 从此不可信 | 取值 `manual`/`scheduled`/`retry`（§0 之外不自造）；原样写进 `initial_state["trigger"]`，不再二次推断。API 层必须把请求体的 `trigger` 透传进来（默认值只用于脚本/调度器直接调用） |
| `resume_agent(expected_interrupt_id=None)` | §4.4 要求"决策对应的 `interrupt_id` 必须等于当前中断"，这条校验需要一个落点。否则要么 API 层自己比一遍（两处真相）、要么根本不比 | `str` → 与 checkpoint 里当前 `interrupt_id` 比对，不等抛 `ApprovalNotFound`（404）；**`None` = 放弃该校验（不是"校验通过"）**，且**必须打一条 `level="warn"` 日志**（`resume_agent.expected_interrupt_id_absent`）。**API 层必须总是传值**，`None` 只给脚本/测试用——把 `None` 理解成"没问题"是本条要堵的错 |

**为什么放在这个位置**：两者都是"契约要求了行为、但签名没给位置"的历史欠账（`trigger` 在 §7.2 有字段、`expected_interrupt_id` 在 §4.4 有校验要求），实现者只能自己发明落点。补成 keyword-only + 默认值，既不破坏既有调用，也不需要调用方改动。

返回值（数据类，`app/agent/state.py` 里定义，dataclass(frozen=True)）：

```python
@dataclass(frozen=True)
class PendingApproval:
    action_id: str          # 批准对象的唯一身份 = approval_request.action_id（见 §5.8，不另设 approval_id）
    run_id: str
    thread_id: str
    shop_id: str
    trace_id: str
    interrupt_id: str
    action_type: str
    target: str
    params: dict[str, Any]
    reason: str
    evidence: dict[str, Any]
    risk: str
    status: str            # ApprovalStatus
    created_at: str

@dataclass(frozen=True)
class RunResult:
    run_id: str
    thread_id: str
    trace_id: str
    status: str            # RunStatus
    pending_approvals: list[PendingApproval]
    effect_report: EffectReport | None
    state: AgentState

@dataclass(frozen=True)
class ResumeResult:
    run_id: str
    thread_id: str
    trace_id: str
    status: str
    applied: list[AppliedDecision]
    pending_approvals: list[PendingApproval]
    effect_report: EffectReport | None
    state: AgentState

@dataclass(frozen=True)
class AppliedDecision:
    action_id: str
    verdict: str
    status: str            # ActionLogStatus
    action_log_id: str | None
    error: str | None
```

### 4.2 图拓扑与状态机（冻结）

```
START
  └─> collect ──(ok)──> compute_metrics ──> detect_anomalies ──> propose_actions
        │                                                              │
        └──(fail)──> finalize <───────────────────────────────────────┘
                                    (等待审批 / 无提议时直通)
                                          │
                                     approval_gate   ← interrupt() 发生在这里
                                          │
                                       execute
                                          │
                                    record_effect
                                          │
                                       finalize ──> END
```

节点名（字符串常量，**就是这些**）：`collect`、`compute_metrics`、`detect_anomalies`、`propose_actions`、`approval_gate`、`execute`、`record_effect`、`finalize`。

条件边：`collect → route_after_collect(state) -> Literal["compute_metrics", "finalize"]`。其余为普通边。

`status` 迁移：

| 时刻 | status |
|---|---|
| `run_agent` 入口（checkpoint 初始写入前） | `running` |
| `approval_gate` 即将 `interrupt()` 前 | `awaiting_approval` |
| `resume_agent` 校验通过后 | `executing` |
| `finalize` 正常结束 | `done` |
| collect 硬失败 / 未捕获异常 / `iteration > 20` | `failed` |
| 审批超时被调度器取消 | `cancelled` |

### 4.3 节点签名（逐字，`app/agent/nodes.py`）

```python
async def collect(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
async def compute_metrics_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
async def detect_anomalies_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
async def propose_actions_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
async def approval_gate(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
async def execute(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
async def record_effect(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
async def finalize(state: AgentState, config: RunnableConfig) -> dict[str, Any]: ...
```

**节点硬约束（生产前提，违反即 bug）：**

1. 节点可能被**重放**（`interrupt` 恢复从节点开头重跑）。因此：任何副作用必须是**幂等 upsert**；任何"读一次就变"的行为（如 `now()`）不得影响最终状态一致性。
2. 节点**体内**不得捕获 `GraphInterrupt`（LangGraph 内部异常），`except Exception` 必须显式排除它。唯一允许**观察**它的是 `traced()` 包装器（§4.7 规则 5），且它只记录、不吞（ADR-016：控制流中断 ≠ 失败）。
3. `approval_gate` 在**同一个** state 上只允许一个 `interrupt()` 调用（否则恢复值顺序语义复杂化）。
4. 节点抛出的非 `GraphBubbleUp` 异常由 `run_agent`/`resume_agent` 兜住 → `status=failed` + `errors` 追加 + `finalize`。`GraphBubbleUp`（含 `GraphInterrupt`）不在此列：它是挂起，不是失败（§4.7 / ADR-016）。
5. 节点签名只有 `(state, config)`，**依赖只能经 `app/agent/adapters.py` 解析**（`resolve_runtime` / `resolve_client` / `resolve_now` / `require_log_sink`，§4.8）：**禁止**在节点体内现建 `create_doudian_client()` / `get_settings()` / `JsonlLogSink`，也**禁止**定义进程级单例（§4.8 禁令 4/5）。

### 4.4 `approval_gate` 的 interrupt 协议（逐字，最重要的一段）

```python
# 0) 组装挂起载荷（纯函数、只依赖 state，重放时结果一致）
payload = {
    "type": "approval_request",
    "run_id": state["run_id"],
    "thread_id": state["thread_id"],
    "shop_id": state["shop_id"],
    "trace_id": state["trace_id"],
    "requested_at": requested_at,                       # 取 state 里已定稿的值或可注入的 now
    "proposals": state["pending_actions"],               # list[ActionProposal]
}

# 1) 幂等落库（insert-if-absent，绝不覆盖已裁决行；interrupt_id 先写 ""；见 §5.8）
await approvals.upsert_proposals(session=..., run_id=..., shop_id=..., thread_id=..., proposals=...)

# 2) 通知（dry-run 只写日志；失败不阻断）
await notifier.send_approval_request(approval=payload)

# 3) 挂起（唯一一次 interrupt）
answer = interrupt(payload)                              # 下面全部是"恢复后重放"的逻辑

# 4) 恢复值严格校验（不通过就 raise ApprovalValidationError → API 400）
#    形状：{"type": "approval_decisions", "decisions": [Decision, ...]}
validated = _validate_resume_payload(answer, expected=state["pending_actions"])

# 5) 落裁决（先查 status 仍是 pending；否则 DuplicateDecision）
await approvals.mark_decided(session=..., thread_id=state["thread_id"], decisions=validated["decisions"])
```

> `interrupt_id` 的回填**不在**节点里做：节点拿不到自己的 interrupt id。它由 `resume_agent` 在 `ainvoke` 之前完成（见下）。
> 重放的后果：第 1/2 步在重放时会**再执行一次**。这正好是 `insert-if-absent` 与"通知失败不阻断"两条设计的由来（ADR-010）。第 4 步用 `answer`（真实恢复值），所以重放不会误判。


**恢复值形状（冻结）**

```json
{
  "type": "approval_decisions",
  "decisions": [
    {"action_id": "run_x:price_update:3400...002", "verdict": "approve", "edited_params": null,
     "operator": "ops_zhang", "reason": null, "decided_at": "2026-10-23T14:31:00Z"}
  ]
}
```

**校验规则（全部必须成立，否则 400 `VALIDATION_ERROR`）**

1. `type == "approval_decisions"`，`decisions` 是 list。
2. `set(d.action_id) == set(pending_actions.action_id)`：**既不许多、也不许少、也不许重复**。
   → 这就是"zip 静默截断"的根治点（ADR-005）：不做任何按位置配对。
3. `verdict in {approve, reject, edit}`。
4. `verdict == "edit"` 时：`edited_params` 必须是非空 dict，并通过该 `action_type` 的参数 schema（§5.5）。
5. `verdict != "edit"` 时：`edited_params` 必须为 `null`（传了非空 → 400，不做"宽容处理"）。
6. 每个 `action_id` 在 DB 中必须仍是 `pending`；任一已裁决 → 409 `DUPLICATE_DECISION`（先查 DB，再 resume）。

**恢复调用（冻结）**

```python
# resume_agent 内部顺序（冻结）
snapshot = await graph.aget_state(config)
if not snapshot.interrupts:
    raise NoPendingInterrupt(thread_id)                    # -> 404 THREAD_NOT_FOUND
interrupt_id = snapshot.interrupts[0].id                   # 单挂起点，取第一个
if interrupt_id != request.interrupt_id:
    raise ApprovalNotFound(...)                            # -> 404 APPROVAL_NOT_FOUND
pending = [p["action_id"] for p in snapshot.values["pending_actions"]]
await approvals.assert_all_pending(session=..., thread_id=thread_id, action_ids=pending)  # 已裁决 -> 409
await approvals.backfill_interrupt_id(session=..., action_ids=pending, interrupt_id=interrupt_id)
await graph.ainvoke(
    Command(resume={interrupt_id: {"type": "approval_decisions", "decisions": decisions}}),
    config=config,
)
```

- `config = {"configurable": {"thread_id": thread_id}, "recursion_limit": settings.agent.recursion_limit}`（默认 50）
- `decisions` 的**完整校验在 resume_agent 里先做一遍**（§4.4 校验规则 1/2/3/5），再进图；节点内的 `_validate_resume_payload` 是第二道闸（防"绕过 API 直接调 resume_agent"）。两道都要有，不做取舍。
- **必须**用 `{interrupt_id: payload}` 的键映射形式；**禁止** `Command(resume=[...])` 列表形式（列表是按位置消费的，正是被否掉的方案）。
- `thread_id` 查不到 checkpoint → `THREAD_NOT_FOUND`（404）；`snapshot.interrupts` 为空但 thread 存在 → 409 `RUN_STATE_CONFLICT`。

### 4.5 幂等键与时序（冻结）

| 键 | 何时生成 | 内容 | 用途 |
|---|---|---|---|
| `action_id` | 提议生成时（审批**前**） | `f"{run_id}:{action_type}:{target}"` | 提议身份；审批对应关系；**不用于执行去重** |
| `payload_digest` | 提议落库时（用 `proposal.params`）；`mark_decided` 时若参数被改则用生效参数**重算并更新** | `sha256(canonical_json({shop_id, action_type, target, params}))` | `approval_request` 一行的参数指纹；digest 变化 = 参数在审批时被改过（可审计） |
| `idempotency_key` | 执行**前一刻**（审批后，用生效参数） | `sha256(f"{shop_id}|{action_type}|{target}|{canonical_json(params_effective)}")` | `action_log.idempotency_key` **UNIQUE**，真正的执行去重 |

`canonical_json(x)` = `json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`。
`params_effective`：`verdict==edit ? edited_params : proposal.params`，且已通过 §5.5 的归一化（金额 `*_cents` 为 int、`None` 键被剔除）。

**为什么这样分**（ADR-006）：审批时改参数是正常操作。若幂等键在审批前生成，改价 9900→9600 两个不同动作会撞同一个键 → 第二次被错误去重；反过来若键不含参数，则同一动作重跑会被重复执行。键必须落在"参数已经定稿"之后、`INSERT` 之前。

### 4.6 入口的持久化顺序（`run_agent`）

```python
async def run_agent(shop_id, run_id, *, checkpointer=None):
    thread_id = f"{shop_id}:{run_id}"
    trace_id = new_trace_id()
    # 1) 幂等：agent_traces 里 (shop_id, run_id) 已存在？
    #    - status=running/awaiting_approval/executing  -> raise RunInProgress(409)
    #    - status 终态                                  -> 直接返回已存结果（ReplayResult）
    # 2) init_db() 已由 lifespan 保证；insert agent_traces(status=running)
    # 3) graph = build_graph(checkpointer or get_checkpointer())
    # 4) await graph.ainvoke(initial_state, config)   # initial_state 见 §3.2 全字段
    # 5) 读 snapshot，映射成 RunResult，并 upsert agent_traces 终态
```

`run_agent` 返回的 `status` 在需要审批时是 `awaiting_approval`（**不是** `done`）。

### 4.7 节点 traced 包装器（`app/agent/tracing.py`，唯一允许写 `step_count`/`status` 的地方）

**为什么有这个模块**：`agent_traces` 是"一次 run 一行"的运行摘要，节点级 trace 走 JSONL（DECISIONS ADR-015）。
两张"真相"（表里的计数、日志里的节点事件）必须由**同一个**写入点产生，否则它们迟早各自为政。这个包装器就是那个唯一写入点。

```python
NODE_TRACE_EVENT = "node.trace"

# 三态 outcome（v1.2.0 冻结，理由见 DECISIONS **ADR-016**）。控制流中断与执行失败是两件事，不许混在一个字段里。
# 依据 langgraph.errors 的继承关系（实测 langgraph 1.2.16）：
#   GraphBubbleUp(Exception) <- GraphInterrupt, GraphDrained, ParentCommand
#   GraphRecursionError(RecursionError)  **不是** GraphBubbleUp —— 它是真失败
NODE_TRACE_OUTCOME_OK = "ok"                  # 正常返回
NODE_TRACE_OUTCOME_INTERRUPTED = "interrupted"  # 捕获 GraphBubbleUp（HITL 挂起等控制流）
NODE_TRACE_OUTCOME_ERROR = "error"            # 其它异常，含 GraphRecursionError；也含节点显式宣告失败

# 节点"显式宣告失败"时 data.error.type 的固定标记（v1.3.1，ADR-019）。
# 这条路径没有异常类可写，所以用一个稳定字符串说明"是节点主动声明失败、未抛异常"。
NODE_DECLARED_FAILURE_TYPE = "NodeDeclaredFailure"

def traced(node_name: str) -> Callable[[NodeFn], NodeFn]:
    """装饰节点函数。语义（逐条实现，不许省）：

    1. 每次**实际执行**写且只写一条 JSONL 事件 event="node.trace"（成功、挂起、失败都写）；
    2. 同一次执行把 agent_traces.step_count 原子 +1，并按需更新 status/updated_at；
    3. duration_ms 用 time.perf_counter() 计；
    4. input_digest  = sha256(canonical_json(节点声明的入参视图))[:16]
       output_digest = sha256(canonical_json(节点返回的 partial state))[:16]
       —— 只存摘要，不存原文（原文已在 checkpoint 里，日志不重复承载）；
       挂起/失败时 output_digest 为 None（没有 partial state 可摘要）；
    5. **失败/挂起分流（v1.2.0 冻结，ADR-016；v1.3.1 补 c2，ADR-019）**，必须按下面这个
       顺序判断，先控制流、后失败、最后才看"成功返回"：
       a) `except GraphBubbleUp:`  → data.outcome="interrupted", data.error=None, level="info"
          然后 `raise`（原样 re-raise，绝不吞）。**必须捕基类 GraphBubbleUp**，
          不能只捕 GraphInterrupt —— GraphDrained / ParentCommand 同样是控制流。
       b) `except Exception as exc:` → data.outcome="error", level="error",
          data.error={"type": type(exc).__name__, "message": redact(str(exc))}，然后 `raise`。
          GraphRecursionError 落到这一支（它是失败，不是挂起）。
       c) 正常返回 → 再按**状态是否发生"非 failed → failed"迁移**分两种：
          c1) 没有迁移 → data.outcome="ok", data.error=None, level="info"（v1.2.0 行为不变）；
          c2) 迁移了（v1.3.1 / ADR-019）→ data.outcome="error", level="error",
              output_digest=None,
              data.error={"type": "NodeDeclaredFailure",
                          "message": redact(节点本次 update 中最后一条 AgentError.message)}
              —— 节点**没有抛异常**，但它把 run 推进到了 failed（例如 §4.2 的
              `collect --(fail)--> finalize`：collect 写 status=failed 后正常返回，图层再走
              条件边去收口）。若这一支不认，采集硬失败会被记成 outcome="ok" 的绿灯。
              **判据是状态迁移，不是"返回值里带不带 errors"**：已在 failed 的 run 上收尾
              （如 finalize 复述 status=failed）**不得**被判成第二个 error。
    6. 写序固定：① JSONL append（拿 seq）② DB 更新（同一事务里 step_count+1 与 status）。
       **写事件必须发生在 re-raise 之前**，否则挂起/失败就没有 trace。
    """

async def reconcile_step_count(*, session: Session, run_id: str, sink: JsonlLogSink) -> int:
    """以 JSONL 为准校准 agent_traces.step_count，返回校准后的值。
    调用时机：finalize 节点结束前一次；进程启动时对所有非终态 run 各一次。
    语义：step_count := count(events where run_id=X and event=="node.trace")（只增不减，差异记 audit）。
    注意：计数不看 outcome —— 挂起也是一次真实执行。
    """
```

**不变量（冻结，可被断言）**

```
对任意 run X：count(JSONL 中 run_id=X 且 event=="node.trace" 的事件数) == agent_traces[X].step_count
```

- 该等式在 `reconcile_step_count` 执行后**必须**成立；它由 `test_trace_invariant.py` 断言（见 §10.3）。
- `step_count` 计的是**节点执行次数**（含 `interrupt` 恢复导致的重放，也含以 `outcome="interrupted"` 结束的那次执行），**不是**唯一节点数。8 节点的一次带审批 run 正常是 9 次执行（`approval_gate` 被重放一次）。
- **`outcome` 与 `level` 的对应是硬规则**（v1.2.0，ADR-016）：`ok`→`info`、`interrupted`→`info`、`error`→`error`。HITL 挂起是**正常控制流**，不许在日志/看板里染成红色——否则告警永久为红，等于没有告警。`level` 是 `outcome` 的**派生**值，判 `level` 只许看 `outcome`，不许看"有没有异常抛出"（`interrupt` 就是靠抛异常挂起的）。
- **`outcome="error"` 有两条来源**（v1.3.1，ADR-019）：①节点抛了非 `GraphBubbleUp` 异常（规则 5b）；②节点**正常返回但把 run 从非 `failed` 推进到 `failed`**（规则 5c2，`data.error.type="NodeDeclaredFailure"`）。判据是**状态迁移**而不是"返回值里有没有 errors"：后者会把 finalize 这类"在已失败的 run 上收尾"的节点也判成 error，一个失败 run 就会长出两盏红灯。`step_count` 不因这条分支改变——计数不看 `outcome`（ADR-016）。
- 崩溃窗口是真实存在的：JSONL 写完、DB 未更新时进程死掉 → 等式暂时不成立。**这正是要 `reconcile_step_count` 的原因**：JSONL 是 `step_count` 的事实源，`agent_traces.step_count` 是它的物化值（为了看板能 SQL 聚合而不必扫日志）。所以处置不是"假装原子"，而是"启动即对账"。
- 除本模块外，任何地方**不得**写 `agent_traces.step_count`。落地用**三条**机械断言（v1.3.0 冻结，逐字可跑）：
  - **A. 写入点唯一**：`grep -rnE 'step_count\s*=[^=]|step_count\s*\+=' backend/app --include=*.py` → **只允许命中 `tracing.py`**（实测命中 3 行：`values(step_count=0)` 初始化、`row.step_count = … + step_delta` 增量、`row.step_count = calibrated` 校准）。
    注意两件事：①模式必须包含 `values(step_count=0)` 这种 **INSERT 关键字形态**，否则漏掉初始化那一处（`tracing.py` 自己就这么写）；②**不要用 lookahead `(?!=)`**——ripgrep 默认 Rust regex 不支持，命令会直接报错，断言变成"跑不起来"而不是"通过"。
  - **B′. 表定义唯一**：`grep -rn '__tablename__' backend/app --include=*.py` → **恰好 4 行，全部在 `models.py`**（`agent_traces` / `approval_request` / `action_log` / `effect_snapshot`）。多出一张表 = 有人在自己文件里建表。
  - **C. 单例唯一**：`grep -rn 'class AgentRuntime' backend/app` → **恰好 1 行**（`adapters.py`，见 §4.8）。两处定义 = 进程级单例变两个，lifespan 注入的 client 节点读不到（§4.8 禁令 4）。
- **断言的取向（v1.3.0 明确，别再用错的写法）**：grep 管的是「**定义与写入**的唯一性」，**不管「名字被提到几次」**。读取侧提及、docstring、`import`、把 `reconcile_step_count(...)` 的返回值放进 dict 都是**允许**的，命中它们不等于违反不变量。
  为什么不用「`grep -rn "step_count" backend/app` 只允许命中 `tracing.py` 与 `models.py`」这种写法：它与本节的另一条冻结条款**互斥**——`reconcile_step_count` 的调用时机是「`finalize` 结束前一次」，而 `finalize` 按 §0.1/§4.3 就在 `nodes.py`，于是 `nodes.py` 必然 `import` 并调用它，字面 grep 必然命中；`graph.py` / `observability/logging.py` 的 docstring 同理。这条断言**无论实现多干净都不可能通过**，而它唯一能"过"的修法是把 `reconcile_step_count` 从 `finalize` 挪走——那恰好拆掉本节真正要保的不变量。**假阳性会逼出错误修法**，所以断言必须落在"赋值/增量/定义"这种形态上，而不是名字出现次数。
- **机器能证的 vs 只能人审的（必须分开写）**：A/B′/C 三条只能证「**定义与写入**的唯一性」——它们**证不了**下面两类事，**不要**让 grep 假装覆盖：
  1. 有人新写一个**承接写入**的函数（例如在 `services/` 里加 `bump_step_count(session, run_id)` 再被节点调用）——grep 不命中，不变量已破。这靠 **§0.1 的归属禁令**（同一张表只有一个 owner）+ 评审时的调用链追踪。
  2. 有人给同一张表加**第二个仓储**（方法名不叫 `insert_`/`update_` 也能写库）——这靠 **§4.8 禁令 1/2** 与 reviewer 的人工判断（t9/t8）。
  把"机器能自动证的"和"只能人审的"分开写，比让一条命令假装覆盖一切更有用：前者可进 CI，后者进评审清单。
- **证据的时效性（v1.3.0，评审最容易自欺的地方）**：任何门槛结论**必须在相关代码 mtime 静止后实测**，并在证据里写**实测时间**。本仓已出现过实例：同一门槛在 t9 期间不同时刻命中数 `3 → 2`（`nodes.py` 正在被改），拿前一次的输出当"当前证据"就是过期证据，它会让评审以为门槛已过（或未过），而实际状态早已不同。**报告门槛时，命令、时间、输出三者缺一不可。**

---

### 4.8 装配层：进程级运行时与依赖解析（`app/agent/adapters.py`，冻结 v1.3.0）

**为什么必须有这个文件**：§4.3 把节点签名冻结成 `(state, config)`——节点不能多收一个 `client`、`session` 或 `settings`。那"节点怎么拿到 `DoudianClient` / `Notifier` / `RuleSet` / `now` / log sink"就必须有**唯一答案**。散在 8 个节点里各自 `create_doudian_client()` 会造出 8 个连接池，而 ADR-004 要求限流桶与并发信号量必须挂在**同一个** `httpx.AsyncClient` 上——那样一来限流形同虚设。这个文件就是那个唯一答案。

**它是什么**：进程级运行时装配 + 依赖解析 + JSONL 汇装配 + 摘要工具。**它不是**：任何表的仓储，也不是业务逻辑。边界见下表与禁令。

**导出符号（逐字，`__all__` 冻结）**

```python
__all__ = ["AgentRuntime", "build_runtime", "digest_of", "get_log_sink", "get_runtime",
           "refresh_log_sink", "require_log_sink", "reset_runtime", "resolve_client",
           "resolve_now", "resolve_runtime", "set_log_sink", "set_runtime"]
```

**`AgentRuntime` 形状（逐字）**

```python
@dataclass(frozen=True)
class AgentRuntime:
    settings: Settings
    client: Any                      # DoudianClient，必须是**进程级单例**（ADR-004）
    notifier: Any                    # Notifier（§5.6）
    rules: Any                       # RuleSet（§8.3）
    clock: Callable[[], datetime] = utcnow
    extras: Mapping[str, Any] = field(default_factory=dict)
```

**依赖解析的注入键（逐字冻结，节点只许用这些键，不许自创）**

| 解析函数 | 读的 `config["configurable"]` 键（按优先级） | 兜底 |
|---|---|---|
| `resolve_runtime(config)` | `runtime`（必须是 `AgentRuntime` 实例） | 进程级单例 `get_runtime()` |
| `resolve_client(config, runtime=None)` | `client` | `(runtime or get_runtime()).client` |
| `resolve_now(config)` | `now`（`datetime`）→ `clock`（callable） | `utcnow()` |
| `require_log_sink()` | 无（进程级 `_sink`） | 按 `settings.observability.log_file` 现建一个 |

**职责（只许可这些）**
1. 构建/持有/注入进程级单例：`AgentRuntime`（`_runtime`，`threading.Lock` 保护）、`JsonlLogSink`（`_sink`）。
2. 提供 `build_runtime(settings, *, client, notifier, rules, clock)`：任一依赖可显式注入（测试与 lifespan 共用同一条路径）。
3. 依赖解析：`resolve_runtime` / `resolve_client` / `resolve_now`——**先 config 注入、后进程单例**，顺序不许反过来（测试替身必须能盖住生产单例）。
4. 摘要工具 `digest_of(value, *, redact_keys=None)`：脱敏后 canonical JSON 的 sha256 前 16 位（§7.6.1 的 `input_digest`/`output_digest` 由它产出）。
5. 汇生命周期：`set_log_sink` / `get_log_sink` / `refresh_log_sink` / `require_log_sink`。

**禁令（越界即 bug，reviewer 机械检查项）**
1. **禁止持有任何表的仓储**：本文件不得出现 `insert_*` / `update_*` / `select(...)` / `session` 之类的数据访问代码。
2. **禁止出现任何表的写入逻辑**，尤其**禁止写 `agent_traces.step_count`**（§4.7 断言 A 覆盖）：同一张表两个仓储部件 = 两处各自 `+= 1`，那正是 §4.7 要防的两份真相。
3. **禁止业务逻辑**：不判规则、不算指标、不拼 action 参数——那是 `services/` 与 `nodes.py` 的事。
4. **禁止任何其它模块定义同名的进程级单例**：`class AgentRuntime` 在 `backend/app` 下**恰好 1 处**（本文件）。两处定义 = 两个模块级 `_runtime`，`app.main` lifespan 注入的 client 落在 A 模块的单例里，而节点经 B 模块的单例读——**e2e 也能"跑通"（各自懒构建一份），生产才炸**（限流桶分裂 + 连接池翻倍 + 关闭时漏关）。这条禁令是 v1.3.0 的直接动因之一。
5. **禁止节点自建依赖**：节点只许 `resolve_*` 取依赖（§4.3 规则 5），不许在节点体内 `create_doudian_client()` / `get_settings()` 现建。

**落地要求（platform 的 t5 责任）**：`app.main` 的 lifespan 必须 `set_runtime(build_runtime(settings))` 与 `set_log_sink(...)`（或 `refresh_log_sink()`），并在 shutdown 关闭 `runtime.client` 与 notifier 的 HTTP 客户端；测试用 `reset_runtime()` / `set_runtime(None)` 隔离用例。

**为什么这样对生产更好**（ADR-017）：装配层随**部署形态**变（进程内 lifespan / 容器 / 测试替身），仓储层随**表结构**变。混在一个文件里时，改仓储会牵动装配，而装配的测试替身又会把仓储的写入点带进测试路径——§4.7 的单写点不变量就守不住；反过来，装配层被复制时进程级单例会变成多个，注入直接失效（禁令 4）。分开之后两边各有一个可断言的唯一性（写入点唯一 = 断言 A；单例唯一 = 断言 C）。

---

## 5. 服务层接口

### 5.0 应用层异常与 HTTP 映射（`app/errors.py`，逐字）

两套异常族**分开**，不许合并：

- `app/services/errors.py` —— **抖店上游**异常族（`DoudianError` 及其子类，带 `category`/`retryable`），只在 client 与服务层内部使用。
- `app/errors.py` —— **应用层**异常，直接对应 HTTP 错误码（§7.1）。API 层只抛/映射这一族。

```python
from typing import Any
from app.enums import ApiErrorCode

class AppError(Exception):
    code: ApiErrorCode
    http_status: int
    message: str
    details: dict[str, Any] | None = None
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None: ...

class ValidationError(AppError):        code = ApiErrorCode.VALIDATION_ERROR;     http_status = 400
class ThreadNotFound(AppError):         code = ApiErrorCode.THREAD_NOT_FOUND;     http_status = 404
class ApprovalNotFound(AppError):       code = ApiErrorCode.APPROVAL_NOT_FOUND;   http_status = 404
class ResourceNotFound(AppError):       code = ApiErrorCode.RESOURCE_NOT_FOUND;   http_status = 404
class DuplicateDecision(AppError):      code = ApiErrorCode.DUPLICATE_DECISION;   http_status = 409
class RunInProgress(AppError):          code = ApiErrorCode.RUN_IN_PROGRESS;      http_status = 409
class RunStateConflict(AppError):       code = ApiErrorCode.RUN_STATE_CONFLICT;   http_status = 409
class UpstreamError(AppError):          code = ApiErrorCode.UPSTREAM_ERROR;       http_status = 502
class InternalError(AppError):          code = ApiErrorCode.INTERNAL_ERROR;       http_status = 500

# 语义子类（都是 ValidationError 的子类，API 层统一按 400 处理）
class ApprovalValidationError(ValidationError): ...   # 恢复值不合规
class ParamValidationError(ValidationError): ...      # 动作参数 schema 不符
class NoPendingInterrupt(ThreadNotFound): ...         # get_state 无 interrupts

class ConfigError(Exception):
    """启动期配置错误。不是 API 错误：进程必须退出（非 0），由 lifespan/入口捕获。"""
```

映射规则（冻结）：
1. `DoudianError` 在 API 边界统一包成 `UpstreamError(str(e), details={"doudian_category": e.category, "err_no": e.err_no, "request_id": e.request_id})`。**唯一例外**：`ValidationError`/`ParamValidationError` 类的本地校验错**不**包成 502（本地错就是本地错）。
2. `AppError` 由全局 exception handler 序列化成 §7.1 包络，`trace_id` 从 contextvar 取。
3. 其它未捕获异常 → `InternalError`（`message` 固定 `"内部错误"`，细节只进日志）。
4. `thread_id` 存在但无挂起中断：`get_state` 返回空 checkpoint → `ThreadNotFound`；checkpoint 存在但 `interrupts` 为空 → `RunStateConflict`。

### 5.1 DoudianClient（`app/services/doudian_client.py`）

```python
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

class TokenSource(Protocol):
    async def get_access_token(self, *, force_refresh: bool = False) -> str: ...

@dataclass(frozen=True)
class RateLimitConfig:
    rate_per_sec: float = 8.0
    burst: int = 16
    max_concurrency: int = 4
    max_attempts: int = 4
    backoff_base_s: float = 0.4
    backoff_max_s: float = 8.0

@dataclass(frozen=True)
class DoudianResponse:
    ok: bool
    err_no: int
    message: str
    data: Any
    http_status: int
    request_id: str | None
    raw: dict[str, Any]
    attempts: int

class DoudianClient:
    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        base_url: str = "https://openapi-fxg.jinritemai.com",
        token_source: TokenSource | None = None,
        version: int = 2,
        sign_method: str = "hmac-sha256",
        timeout: httpx.Timeout | None = None,
        limits: httpx.Limits | None = None,
        rate_limit: RateLimitConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
        total_budget_s: float | None = None,                        # v1.4.0 入册（keyword-only）；**v1.5.0 确认：代码未实现**，见 §13(m)
        redact_keys: Sequence[str] = DEFAULT_REDACT_KEYS,           # v1.4.0 新增（keyword-only）
    ) -> None: ...

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        mutating: bool = False,
        timeout: httpx.Timeout | None = None,
    ) -> DoudianResponse: ...

    # 业务封装（每个都必须存在，签名固定）
    async def get_shop_info(self) -> DoudianResponse: ...
    async def search_orders(self, *, start_time: str, end_time: str, page: int = 0, size: int = 50) -> DoudianResponse: ...
    async def list_products(self, *, page: int = 0, size: int = 50) -> DoudianResponse: ...
    async def edit_price(self, *, product_id: str, sku_id: str, price_cents: int) -> DoudianResponse: ...
    async def update_stock(self, *, product_id: str, sku_id: str, stock: int) -> DoudianResponse: ...

    async def aclose(self) -> None: ...
    async def __aenter__(self) -> "DoudianClient": ...
    async def __aexit__(self, *exc: object) -> None: ...

def create_doudian_client(settings: Settings) -> DoudianClient: ...
```

要点：

1. **一个进程一个 client**（应用 lifespan 里创建、关闭）。`__init__` 里创建**唯一**的 `httpx.AsyncClient`：
   `httpx.AsyncClient(base_url=..., transport=transport, timeout=timeout, limits=limits)`。
   `limits` 默认 `httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)`；
   `timeout` 默认 `httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)`。
   **禁止**每次请求 `AsyncClient()`（ADR-004）。
2. `method` 是**点号形式**（`product.editPrice`）。HTTP 路径 = `/` + `method.replace(".", "/")`，请求体 = `param_json`。
3. **限流**：进程内令牌桶（`rate_per_sec` + `burst`）+ 并发信号量（`max_concurrency`）。桶用注入的 `clock`/`sleep` 实现，测试里 `clock` 用假时钟 → 限流测试零等待。
4. **重试**（见 §9 表）：仅对 `retryable` 类别；`max_attempts` 次；指数退避 `min(backoff_max_s, backoff_base_s * 2**n)` + 抖动（`random.uniform(0, 0.1*delay)`）；`429` 且带 `Retry-After` 时以它为准（上限 `backoff_max_s`）。
5. `mutating=True` 时**只对 `rate_limited` 重试**；`timeout`/`connection`/`server_5xx` 一律转 `DoudianAmbiguousOutcomeError`（ADR-011）。
6. 响应解析：HTTP 2xx 且 body 是 JSON 且 `body["err_no"] == 0` → `ok=True`；`err_no != 0` → 抛对应 `DoudianError` 子类（**不返回 ok=False 让调用方自己判断**——必须抛）。
   `request_id`：取响应头 `x-tt-logid` / `x-request-id` / body 的 `log_id`，都没有则 `None`（不编造）。
7. 日志：每次 `call` 结束写一条 `doudian.call` 事件（`method`、`http_status`、`err_no`、`attempts`、`duration_ms`、`request_id`），**脱敏走 §7.6.2 的两层并集**：**键名层** 6 键（`app_secret`/`access_token`/`sign`/`authorization`/`cookie`/`password`）+ **值级层** 9 键（`app_secret`/`access_token`/`app_key`/`token`/`secret`/`password`/`sign`/`authorization`/`cookie`，见 §7.6.2）。只做键名层等于对**字符串** `message` 完全不设防（上游错误原文正是 `"… access_token=SECRET …"` 这种形态），因此两层都必须走。

**v1.4.0 新增两个 keyword-only 参数（冻结，含默认值与理由）**

```python
DEFAULT_REDACT_KEYS: tuple[str, ...] = ("app_secret", "access_token", "sign", "authorization", "cookie")
```

| 参数 | 默认 | 语义 | 为什么必须**在这里** |
|---|---|---|---|
| `total_budget_s: float \| None` | `None`（= `settings.app.request_timeout_s`） | **单次 `call` 的绝对截止时间预算**：从**第一个请求发出前**起算的秒数，覆盖"每次尝试 + 全部退避 + 重试"的**总耗时**；超预算立即停止重试并抛最后一个错误类别（§9.2）。**不是每次尝试重新计时**——那正是"重试把请求总时长放大到不可控"的成因。`None` → 由 `create_doudian_client(settings)` 从 `settings.app.request_timeout_s` 填入，保证生产路径永远不是 `None`。**⚠ v1.5.0 状态：已入册、代码未实现**（见 §13(m)）：当前重试上界只有 `max_attempts` × 退避，**不存在**绝对截止时间；一次 `call` 的最坏耗时仍可能到几十秒 | §9.2 早就要求了"单次 call 的总时间预算"，但 §5.1 的签名里没有位置。没有入口时，实现只能把 `settings` 塞进 client（client 就不纯了）或干脆不实现预算（§9.2 变成空话）。注入一个 float 是唯一不改依赖关系的落点 |
| `redact_keys: Sequence[str]` | `DEFAULT_REDACT_KEYS` | §5.1-7 脱敏用的键集合，**两层并集、同一个 `redact_keys` 同时喂给两层**（§7.6.2）：**键名层**＝dict 的键名 ∈ `redact_keys`（大小写不敏感）就把整个值递归替换为 `"***"`，默认 5 键；**值级层**＝自由文本里 `key=value` / `key: value` / `"key":"value"` 的**值**替换，键集 = `DEFAULT_SCRUB_KEYS ∪ redact_keys`（**只增不减**），默认 9 键。少任一层都有一整类输入 100% 漏遮：键名层对 `str` 是 no-op（而错误原文恰好写在 `message` 里），值级层看不见 dict 键。完整语义、已知边界与测试要求见 §7.6.2 | **client 不持有 `Settings`**（这是刻意的：`DoudianClient` 要能在没有配置系统的单测里构造）。而 §5.1-7 又要求按 `settings.observability.redact_keys` 脱敏——那只能注入。**不要**为这件事引入模块级全局配置：全局状态与 §5.1-1 的"一个进程一个 client"会互相掩盖，也会让测试之间的脱敏键互相污染 |

`create_doudian_client(settings)` 必须把 `redact_keys=settings.observability.redact_keys` 显式传进去（单一装配点，§4.8）——这一条**已实现**。
`total_budget_s=settings.app.request_timeout_s` 同样**必须**传，但**该参数在代码里还不存在**（v1.5.0 状态：已入册、未实现，见 §13(m)）。所以现在生产路径上**没有绝对截止预算**，只有 `max_attempts` × 退避。**不要**为了让这条"看起来成立"而在装配点硬塞一个位置参数——那会制造一个"传了但没人用"的假接线（本项目已因同类问题返工过一次）。

#### 5.1.2 抖店异常族（`app/services/errors.py`，逐字）

```python
class DoudianError(Exception):
    category: ErrorCategory
    retryable: bool
    http_status: int | None
    err_no: int | None
    request_id: str | None
    raw: dict[str, Any] | None
    def __init__(self, message: str, *, category: ErrorCategory, retryable: bool,
                 http_status: int | None = None, err_no: int | None = None,
                 request_id: str | None = None, raw: dict[str, Any] | None = None) -> None: ...

class DoudianTransportError(DoudianError):        # timeout | connection        retryable=True
    ...
class DoudianUpstreamError(DoudianError):         # server_5xx                 retryable=True
    ...
class DoudianRateLimitedError(DoudianError):      # rate_limited               retryable=True
    retry_after: float | None
class DoudianClientError(DoudianError):           # client_4xx                 retryable=False
    ...
class DoudianSignatureError(DoudianClientError):  # signature                  retryable=False
    ...
class DoudianAuthError(DoudianClientError):       # auth                       retryable=False（内部允许强制刷 token 一次）
    ...
class DoudianBusinessError(DoudianError):         # business                   retryable=False
    ...
class DoudianAmbiguousOutcomeError(DoudianError): # ambiguous                  retryable=False
    ...
```

`category`/`retryable` 的取值必须与 §9.1 的表逐行一致；实现里**不允许**在 `except` 分支里手写 `retryable=True`，必须由类别查表得出（单一事实源）。

### 5.2 签名（`app/services/signing.py`，纯函数，已按官方 SDK 核实）

```python
def canonical_string(app_key: str, method: str, param_json: str, timestamp: str, version: int | str) -> str: ...
def sign(app_secret: str, *, app_key: str, method: str, param_json: str,
         timestamp: str, version: int | str, sign_method: str = "hmac-sha256") -> str: ...
def format_timestamp(dt: datetime) -> str: ...          # '%Y-%m-%d %H:%M:%S'
def normalize_param_json(params: Mapping[str, Any]) -> str: ...   # sort_keys + separators(',',':') + ensure_ascii=False
```

算法（**逐字**）：

```
canonical = "app_key" + app_key + "method" + method + "param_json" + param_json + "timestamp" + timestamp + "v" + str(version)
message   = app_secret + canonical + app_secret
sign      = HMAC-SHA256(key=app_secret.encode(), msg=message.encode()).hexdigest()      # 小写 hex
sign_method = "hmac-sha256"
```

- `timestamp` 格式 `%Y-%m-%d %H:%M:%S`，**必须只生成一次**，同一个字符串同时用于签名和 URL 查询参数（ADR-001）。
- 签名**只覆盖** `app_key` / `method` / `param_json` / `timestamp` / `v` 五个字段；`access_token`、`sign_method`、`sign` **不参与**签名，且都在 URL 查询串里，**不塞进 body**。
- 请求形态：`POST {base_url}{path}?app_key=..&method=..&access_token=..&timestamp=..&v=..&sign_method=hmac-sha256&sign=..`，
  body = `param_json` 原文，`Content-Type: application/json`。
- `sign_method != "hmac-sha256"` → 抛 `DoudianSignatureError("unsupported sign_method")`。**不实现猜测版本**。
- 消息推送回调验签（独立函数，不用在 API 调用上）：`event_sign = md5(app_key + body_text + app_secret).hexdigest()`。

**黄金向量（逐字写进 `backend/tests/fixtures/doudian/signing_vectors.json`，测试必须断言）**

```json
{
  "app_key": "7000000000000000001",
  "app_secret": "test_secret_do_not_use",
  "timestamp": "2024-01-01 00:00:00",
  "version": 2,
  "vectors": [
    {
      "name": "edit_price",
      "method": "product.editPrice",
      "params": {"product_id": "3400000000000000001", "sku_id": "3400000000000000002", "price": 9900},
      "param_json": "{\"price\":9900,\"product_id\":\"3400000000000000001\",\"sku_id\":\"3400000000000000002\"}",
      "sign": "d51d5ea0311439786664d17e957cf3d1c4a734c2ac64c27bfbab453a876d7163"
    },
    {
      "name": "empty_params",
      "method": "shop.getShopInfo",
      "params": {},
      "param_json": "{}",
      "sign": "c3901cae95b0043b60b57c1a494aeb927045419a17b7d7b032db5f98dd1ad83f"
    }
  ],
  "callback": {
    "body": "{\"msg_id\":\"1\",\"type\":\"order.new\"}",
    "event_sign": "35b25ee40affe3b74045b3c7fda228d3"
  }
}
```

> 这三个值由 spec-lead 用上述算法实算得出（脚本归档可复核）。任何实现改动导致这三个值变化 = 契约被破坏，测试必须变红。

### 5.3 指标（`app/services/metrics.py`）

```python
def compute_metrics(
    raw: RawSnapshot,
    *,
    previous: MetricsSnapshot | None = None,
    history: Sequence[MetricsSnapshot] | None = None,
    now: datetime | None = None,
    cost_table: Mapping[str, int] | None = None,   # v1.5.0 **已实现**（keyword-only）：sku_id -> 成本整数分
) -> MetricsSnapshot: ...
```

- **纯函数**：无 I/O、无全局状态、`now` 可注入（`cost_table` 是**参数**而不是文件读取——读 `config/sku_costs.yaml` 是 `app.main` 装配时的责任，见 §8.5）。
- 公式（逐字）：

| 字段 | 公式 | 分母 0 |
|---|---|---|
| `gmv_cents` | `order_summary["gmv_cents"]` | — |
| `order_count` | `order_summary["order_count"]` | — |
| `paid_order_count` | `order_summary["paid_order_count"]` | — |
| `refund_order_count` | `order_summary["refund_order_count"]` | — |
| `refund_amount_cents` | `order_summary["refund_amount_cents"]` | — |
| `aov_cents` | `round(gmv_cents / paid_order_count)`（**四舍五入到整数分**） | `0` |
| `refund_rate` | `round(refund_order_count / paid_order_count, 6)` | `None` |
| `uv` | `order_summary.get("uv")` | `None` |
| `cvr` | `round(paid_order_count / uv, 6)`；`uv` 为 `None`/0 → `None` | `None` |
| `gmv_7d_avg_cents` | `round(mean(history[*].gmv_cents))`，只取**日期 < 今天**的、最多 7 条；不足 3 条 → `None` | `None` |
| `gross_margin` | **v1.5.0，店铺级加权**：`round((covered_revenue - covered_cost) / covered_revenue, 6)`；`covered_revenue = Σ by_sku[s].gmv_cents`（`s ∈ cost_table`），`covered_cost = Σ cost_table[s] × by_sku[s].units` | `None` |
| `cost_coverage_ratio` | **v1.5.0**：`round(covered_revenue / gmv_cents, 6)`；**不裁剪**（`> 1` 合法，用于暴露上游汇总与明细对不上） | `None` |
| `date` | `raw["window"]["end"]` | — |
| `computed_at` | `(now or utcnow()).isoformat()` → 转 `...Z` | — |

- 入参 `raw` 里的金额**必须已经是整数分**；若出现 `*_yuan`/float 金额字段 → 抛 `ValueError`（在边界处炸，不要静默转换）。
- **`uv` 的来源与作用域（v1.3.2，公式不变）**：`uv` 不在订单明细里，只可能来自上游 `order.searchList` 的 `data.summary`（夹具/demo 约定；抖店真实接口不返回访客数）。`collect` 只有在**能确认这段汇总就是当天**时才把它写进 `order_summary["uv"]`——①`summary.date` / `summary.window.end` 显式声明的业务日期等于 `window.end`，或②本次返回的订单**全部落在** `window.end`（单日响应）。判不出来就写 `None`：**禁止**按"窗口汇总 / 天数"摊派（`cvr` 会被摊薄成约 1/8，所有 uv/cvr 规则静默失效），也**禁止**补 0。`uv` 有值但不是非负整数 → `ValueError`（不静默降级成"这家店没有访客数据"，那会把上游的数据结构变更永远藏起来）。

- **成本口径：已实现（v1.5.0，ADR-021）**。`cost_table` 是 keyword-only 的**可选**参数（`sku_id -> 成本整数分`，来自 `config/sku_costs.yaml`，§8.5）；**不传**等价于"这家店没配成本"，两个成本字段都是 `None`。传了也**不会凭空造出按 SKU 的成交额**——那必须由 `collect` 归集进 `order_summary["by_sku"]`（§3.3），否则我们只知道"成本是多少"，不知道"卖了多少件、按什么价卖的"，算不出任何有意义的毛利率。边界逐条冻结（全部是**数据形状错误** → `ValueError`，**不静默降级**）：

  | 情形 | 处置 |
  |---|---|
  | `by_sku` 缺失（`None`） | 两个字段都是 `None`（老快照/历史桶没做归集，无法归属） |
  | `by_sku` 不是映射 / 某条 entry 不是映射 | `ValueError` |
  | `entry["gmv_cents"]` / `entry["units"]` 缺失或非 `int` | `ValueError`（报错带 `order_summary.by_sku[<sku>]` 位置） |
  | `entry["gmv_cents"] < 0` 或 `entry["units"] < 0` | `ValueError` |
  | `cost_table[s]` 是 `bool` / 非 `int` / `<= 0` | `ValueError`（成本表加载期已校验，这里再查一次是因为本函数是**公开纯函数**，调用方可以直接传字典进来） |
  | `entry["gmv_cents"] > 0` 但 `entry["units"] == 0` | `ValueError`（有成交额却没件数，成本摊不出去） |

- **`None` 与 `0` 在成本口径上不是同义词**：`gross_margin = None` 是"不知道"，`0.0` 是"不赚不亏"，两者在规则求值里结果**不同**（前者数值比较为 `False`、规则不触发、**不进 skipped**；后者会被阈值判断真实地命中或错过）。`metrics.computed` 审计里两个字段必须原样落盘（`None` 与 `0.0` 可区分），否则事后无法解释"为什么这次 `loss_making` 没响"。
- **成本表加载失败 ≠ 没有成本**：`compute_metrics` 节点取成本表时若抛 `ConfigError`，按 `category="config"` 记 `errors` 并让 run `failed`——**绝不**降级成空表继续跑，那会把一次配置事故伪装成"这家店没配成本"的正常状态。
- **`sku_costs.unmatched` 审计（v1.5.0，§8.5）**：本次**商品列表非空**且成本表里有对不上的 `sku_id` 时，`compute_metrics` 节点记一条 `{"event": "sku_costs.unmatched", "data": {"sku_ids": [...], "count": n}}`。**只记不阻断**（一条过期配置不该让当天所有指标都算不出来）；商品列表为空时**不报**（采集降级时每一条都会"看起来对不上"，报出来是一串假警报，反而把真问题淹掉）。

#### 5.3.1 SKU 级指标（`skus`，v1.4.0 新增，ADR-018）

`compute_metrics` 除店铺级 **14** 个字段外（v1.5.0 起含 `gross_margin` / `cost_coverage_ratio` 两个成本口径字段），必须产出 `skus: list[SkuSnapshot]`：**每个在售或近 7 日有销量的 SKU 一条**，**按 `sku_id` 升序**（字符串比较，确定性排序让重放/哈希/diff 稳定）。

公式（逐字；**分母为 0 或任一操作数为 `None` → 结果为 `None`，绝不返回 0**）：

| 字段 | 公式 | 缺失 / 分母 0 |
|---|---|---|
| `sku_id` | `raw.products[i]["sku_id"]` | 缺失 → 跳过该条并记 `errors`（`category="validation"`） |
| `product_id` | `raw.products[i]["product_id"]` | 同上 |
| `current_price_cents` | `raw.products[i]["price_cents"]`（必须是 int 分） | 缺失 → 跳过该条 |
| `cost_cents` | `cost_table.get(sku_id)`（`config/sku_costs.yaml`，§8.5） | **无配置 → `None`（不填 0）** |
| `gross_margin` | `round((current_price_cents - cost_cents) / current_price_cents, 6)` | `cost_cents is None` 或 `current_price_cents <= 0` → `None` |
| `stock` | `raw.products[i]["stock"]` | 缺失 → `0` 并记 `errors` |
| `sales_7d` | `raw.products[i]["sales_7d"]`（抖店订单聚合，**按 sku 归集**，见下） | 缺失 → `0` 并记 `errors` |
| `avg_daily_sales_7d` | `round(sales_7d / 7, 6)`（7 是固定常数，恒不为 0） | `sales_7d == 0` → `0.0`（**这是真值，不是缺失**） |
| `stock_days` | `round(stock / avg_daily_sales_7d, 6)` | `avg_daily_sales_7d` 为 `0`/`None` → `None` |
| `is_on_sale` | `raw.products[i]["is_on_sale"]`（bool） | 缺失 → `False` 并记 `errors` |
| `ad_spend_cents_7d` | `raw.products[i].get("ad_spend_cents_7d", 0)` | 无数据 → `0`，并记 audit `ad_spend.unavailable`（见下） |
| `ad_order_count_7d` | `raw.products[i].get("ad_order_count_7d", 0)` | 无数据 → `0`，同上 |

**v1.5.0 命名冲突预告（必读）**：本表的 `gross_margin` 是 **SKU 级**（`(current_price_cents - cost_cents) / current_price_cents`），与 §5.3 / §3.3 新增的**店铺级** `gross_margin` **同名不同义**。SKU 轮次落地时本字段**必须**改名为 `sku_gross_margin`（§8.3.3 的硬规则），**不得**沿用同名——同名会让规则作者无意中把店铺级阈值套到单品上。

**数据来源（必须如实，不许编造接口）**：
- `products` / `订单`：`DoudianClient.list_products` / `search_orders`（§5.1）→ **`collect` 节点按 `sku_id` 归集**成 `raw.products[i]` 上的字段，金额在此处就算成整数分（§0）。
- **成本价：抖店开放平台没有这个字段**。成本是商家侧自维护数据，唯一来源是 `config/sku_costs.yaml`（§8.5）。不要试图从采购单/历史售价反推——那是猜测，而猜测的成本会直接变成"亏本告警对不对"的答案。
- **广告花费/广告成交：本契约的数据面里没有**。抖店开放平台（`get_shop_info` / `search_orders` / `list_products`）不返回千川投放数据，本契约也没有对接广告 API。因此这两个字段是 **`raw` 的可选注入**：有就给，没有就 `0`，并**必须**记一条 audit `{"event": "ad_spend.unavailable", "sku_id": ...}`。
- **`0` 的语义是"没有数据或没有投放"，不是"广告零成本"**。规则作者必须知道这一点：**禁止**用 `ad_spend_cents_7d` 单独作为告警依据（否则"没接广告数据"会被读成"零投入高回报"）。要比较广告效率，必须与 `ad_order_count_7d` 成对使用，且两者都为 0 时该规则不触发。

**禁止事项（机械可查）**：
- 禁止把 `cost_cents` / `gross_margin` 的 `None` 当 `0`：`grep -rn "cost_cents or 0\|gross_margin or 0" backend/app` 必须为空。
- 禁止在 `compute_metrics` 内部读文件/读 DB（它是纯函数）：`cost_table` 只能由参数进来。
- 禁止让 `history` 元素带 `skus`（§3.3 已冻结：`[]`）。

### 5.4 异常检测（`app/services/anomalies.py`）

```python
def detect_anomalies(
    *,
    metrics: MetricsSnapshot,
    history: Sequence[MetricsSnapshot],
    rules: RuleSet,
    run_id: str,
    now: datetime | None = None,
) -> list[Anomaly]: ...
```

- **两轮求值（v1.4.0 冻结，ADR-018）**：
  1. **店铺轮**：对每条规则，若其 `when` **不引用任何 SKU 级名字** → 用店铺级命名空间（12 个名字 + 3 个 delta，§8.3.3）求值一次，`Anomaly.sku_id = None`。
  2. **SKU 轮**：对每条规则，若其 `when` **引用至少一个 SKU 级名字** → 对 `metrics.skus` 里**每个 SKU** 各求值一次，命名空间 = 店铺级名字 **∪** 该 SKU 的字段，`Anomaly.sku_id = 该 sku_id`。
  - 轮次归属由**表达式引用的名字集合**决定（`expr.names ∩ SKU_NAMES ≠ ∅` → SKU 轮），**不需要在 `rules.yaml` 里配 `scope`**：两类名字集合互斥（§8.3.3 冻结的 disjoint 校验），因此这个判定**总是有唯一答案**。禁止实现里另加 `scope`/`level` 字段来"帮忙"（那是自创参数，§1.1）。
  - 一个 SKU 级规则命中多个 SKU → **产出多条 `Anomaly`**（每条带自己的 `sku_id`），不是一条。
- `anomaly_id` **确定性且含 `sku_id`**：`sha256(f"{run_id}|{rule_id}|{metric}|{sku_id or ''}")[:16]`。店铺级用空串占位，因此店铺级 id 与 v1.3.0 一致；SKU 级必须带 `sku_id`——**否则同一条规则命中 5 个 SKU 会撞出同一个 `anomaly_id`**，审批面板上 5 张卡片共享一个 `action_id`，批一次全吞（这是本版必须修死的 bug）。
- `message` 必须带 SKU 标识：SKU 级的 `description` 前缀 + `sku_id`（例如 `"毛利为负（sku 3400000000000000002）"`），否则审批人看不出是哪件货。
- `RuleEvalError`（除零、类型错、bool 参与算术、NaN/Inf）**不得**向外抛：该规则（该 SKU）跳过，并记一条可见记录，保证"跳过"可见。
- **`None` 与"真错误"必须分开记（v1.4.0，两者混淆过一次）**：
  - **`None` 参与比较 → 结果为 `False`**，这是**规则正常未命中**，**不进 skipped**（`gross_margin is None` 的 SKU 只是"不知道是否亏本"，不是"求值失败"）；
  - **真错误**（除零、bool 参与算术、类型不匹配、NaN/Inf）→ `RuleEvalError` → 该规则/SKU 进 **skipped 记录**。
  - 两者的记录形状相同（`errors` 一条，`data.reason` 取 `"none_operand"` / `"rule_eval_error"`），但**语义不同**：前者是数据不足，后者是配置或实现问题。**禁止**把 `None` 混进 `RuleEvalError`（那会让"没配成本"看起来像"规则写错了"）。
- 结果按 `severity` 降序（critical > warn > info）、同 severity 按 `rule_id` 升序、再按 `sku_id` 升序（`None` 排前）—— 稳定的全序，便于前端 diff 与上限截断的可复现。
- 条数上限 `settings.agent.max_anomalies`（默认 20），超出**丢弃尾部**（按上面的全序）并记 error。

### 5.5 动作提议与参数 schema（`app/services/actions.py`）

```python
ACTION_PARAM_SCHEMA: dict[ActionType, dict[str, type]] = {
    ActionType.PRICE_UPDATE: {"price_cents": int},
    ActionType.STOCK_UPDATE: {"stock": int},
    ActionType.COUPON_CREATE: {"discount_cents": int, "total_count": int, "begin_time": str, "end_time": str},
}

def normalize_params(action_type: ActionType, params: Mapping[str, Any]) -> dict[str, Any]: ...
def validate_params(action_type: ActionType, params: Mapping[str, Any]) -> dict[str, Any]: ...  # 失败抛 ParamValidationError

def propose_actions(
    *,
    anomalies: Sequence[Anomaly],
    metrics: MetricsSnapshot,
    products: Sequence[Mapping[str, Any]],
    rules: RuleSet,
    run_id: str,
    now: datetime | None = None,
) -> list[ActionProposal]: ...
```

- `validate_params` 规则：键集合必须**恰好等于** schema 的键集合（多/少都错）；`int` 类型字段必须是 `int` 且 `bool` 不算 `int`；`price_cents > 0`、`stock >= 0`、`discount_cents > 0`、`total_count > 0`、时间字段必须能被 `datetime.fromisoformat` 解析。
- `propose_actions` 只产出**白名单动作类型**，条数上限 `settings.agent.max_actions_per_run`（默认 5）；`target` 必须能在 `products` 中找到。
- `risk` 判定（冻结，供 `auto_approve_risk` 使用）：
  `price_update` 且相对现价跌幅 `> 15%` → `high`；`> 5%` → `medium`；否则 `low`。`stock_update` 恒 `low`。`coupon_create` 恒 `high`。
- 同一 run 内 `action_id` 去重（撞了说明规则配置重复，记 error 并丢后者）。

### 5.6 通知（`app/services/notifier.py`）

```python
@dataclass(frozen=True)
class NotificationResult:
    delivered: bool
    mode: str            # "dry_run" | "feishu"
    channel: str
    detail: str
    payload_digest: str

class Notifier(Protocol):
    mode: str
    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult: ...

class DryRunNotifier:
    mode = "dry_run"
    def __init__(self, *, logger: logging.Logger, sink: JsonlLogSink) -> None: ...
    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult: ...

class FeishuWebhookNotifier:
    mode = "feishu"
    def __init__(self, *, webhook_url: str, http_client: httpx.AsyncClient, logger: logging.Logger) -> None: ...
    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult: ...

def build_notifier(settings: Settings, *, http_client: httpx.AsyncClient, sink: JsonlLogSink) -> Notifier: ...
```

- `DryRunNotifier`：写一条 `notifier.dry_run` 日志（含 `payload_digest`），返回 `delivered=False, mode="dry_run"`。**不抛异常**、不等待。
- `FeishuWebhookNotifier`：`POST {"msg_type":"interactive","card":{...}}`；失败（非 2xx / `code != 0`）→ 返回 `delivered=False` 并记 warn；通知失败**不得**阻断审批流程（审批台是主通道）。
- `build_notifier` 依据 `settings.notifier.mode` 选择；`feishu` 但 `FEISHU_WEBHOOK_URL` 缺失 → 启动期 `ConfigError`（不要静默降级成 dry_run）。

### 5.7 检查点工厂（`app/agent/checkpoint.py`）

```python
@asynccontextmanager
async def checkpoint_saver(settings: Settings) -> AsyncIterator[BaseCheckpointSaver[str]]: ...
```

- `settings.checkpoint.type == "sqlite"` → `AsyncSqliteSaver.from_conn_string(settings.checkpoint.path)`
  （`langgraph.checkpoint.sqlite.aio`；**入参是文件路径**，不是 SQLAlchemy URL，如 `data/checkpoint.db`）。
- `type == "postgres"` → `AsyncPostgresSaver.from_conn_string(settings.checkpoint.url)`
  （`langgraph.checkpoint.postgres.aio`；`url` 形如 `postgresql://user:pass@host:5432/db`）。
  若该版本不存在 `from_conn_string`，用 `AsyncPostgresSaver(AsyncConnectionPool(url, open=False))` 作等价路径（**待核实**，见 DECISIONS ADR-002 风险栏）。
- 首次使用前必须 `await saver.setup()`（LangGraph 幂等，重复调用安全）。
- **禁止** `MemorySaver` / `InMemorySaver` 出现在 `app/` 任何路径（只允许 `tests/`）。

### 5.8 审批仓储（`app/services/approvals.py`）

```python
async def upsert_proposals(*, session: Session, run_id: str, shop_id: str, thread_id: str,
                           proposals: Sequence[ActionProposal]) -> None: ...
async def backfill_interrupt_id(*, session: Session, action_ids: Sequence[str],
                                interrupt_id: str) -> None: ...
async def list_by_status(*, session: Session, status: ApprovalStatus | None = None,
                         shop_id: str | None = None, limit: int = 50,
                         before_id: int | None = None) -> list[PendingApproval]: ...
async def mark_decided(*, session: Session, thread_id: str,
                       decisions: Sequence[Decision]) -> list[str]: ...   # 返回被更新的 action_id
async def assert_all_pending(*, session: Session, thread_id: str,
                             action_ids: Sequence[str]) -> None: ...      # 有已裁决 -> DuplicateDecision
```

**`upsert_proposals` 的语义（冻结）**：以 `action_id` 为身份，已存在时**什么都不做**（绝不回写 `status`）。
理由：`approval_gate` 在 resume 时会被重放，若 upsert 覆盖 `status=approved` → `pending`，会出现"审批完成后又冒出来等审批"的假象（ADR-010）。
实现：先 `SELECT ... WHERE action_id IN (...)`，对缺失的 `INSERT`；并发撞 `IntegrityError` 时**重查并跳过**（不做 `INSERT OR REPLACE`，不做原生 `ON CONFLICT`）。

**`interrupt_id` 回填（冻结）**：`approval_gate` 里 `interrupt()` 是**阻塞**的，它的返回值只在恢复后才拿到，所以 `interrupt_id` 无法在首次落库时写入。流程固定为：
① `upsert_proposals`（`interrupt_id` 列写空串 `""`）→ ② 通知 → ③ `interrupt(payload)` → ④ 恢复后立刻
`await backfill_interrupt_id(session, action_ids=[...], interrupt_id=snapshot.interrupts[0].id)` → ⑤ `mark_decided`。
`backfill_interrupt_id` 的更新条件必须带 `AND interrupt_id = ''`（幂等且不覆盖）。

**读路径的补充（v1.3.2，不改签名）**：本模块的签名与返回形状**全部不变**（`list_by_status` 仍返回 `PendingApproval[]`、仍不含行 `id`）。列表/游标场景允许经 `app/api/queries.py` 做 keyset 分页查询（`ORDER BY created_at DESC, id DESC` + `id < before_id`，多取一条判断"还有没有下一页"），因为 `PendingApproval` 表达不了 §7.3 的 `next_cursor`。**写路径不变**：提议落库、裁决、`interrupt_id` 回填仍只经本模块的 `upsert_proposals` / `mark_decided` / `backfill_interrupt_id`，`queries.py` 不得有任何写操作（边界见 §0.1）。

### 5.9 效果快照与调度（`app/services/effects.py`）

```python
async def register_effect_snapshots(*, session: Session, run_id: str, shop_id: str,
                                    results: Sequence[ExecutionResult],
                                    windows_days: Sequence[int],
                                    now: datetime | None = None) -> list[EffectWindow]: ...

async def run_effect_evaluator_once(*, session: Session, client: DoudianClient,
                                    now: datetime | None = None) -> int: ...   # 返回本次评估条数

def build_effect_report(*, run_id: str, windows: Sequence[EffectWindow],
                        attribution: Attribution, now: datetime | None = None) -> EffectReport: ...
```

**5.9.1 图内不做等待**：`record_effect` 节点只为每个**成功执行**的动作写入 `offset_days ∈ windows_days` 的 `effect_snapshot` 行（`due_at = executed_at + offset_days`，`status="scheduled"`），然后立刻返回。等待 T+1/T+3/T+7 由**独立调度器**（`settings.effects.scheduler_enabled`）按 `due_at <= now` 拉取评估（ADR-003）。

**5.9.2 快照幂等**：`snapshot_id = sha256(f"{action_log_id}|{offset_days}")[:32]`，`UNIQUE(action_log_id, offset_days)`。

**5.9.3 归因与文案规则（硬性）**

- `attribution` 只能是 `single_arm_before_after`（有前后窗口、无对照组）、`ab_test`（真有对照组）、`none`（连基线都不稳）。
- 默认实现产出 `single_arm_before_after`；**没有 A/B 数据时禁止**出现"提升""带来""因为"这类因果措辞。
- `conclusion` 必须形如：`"T+1 观察窗内 GMV 环比 -12.4%（基线：前 7 日同时段均值），未做对照实验，不能归因于本次改价。"`
- `caveats` 至少包含：`"无对照组，指标变化可能来自季节性/活动/竞品等外部因素"`、`"归因强度：弱（单臂前后对比）"`。
- 测试里要有一条断言：`attribution != "ab_test"` 时 `conclusion` 不得包含 `["提升","带来","导致","因为","归因于本次"]` 中的因果断言词（`"不能归因于"` 除外，用否定式白名单）。

---

## 6. 数据库契约（`app/models.py`）

### 6.1 四张表（冻结）

**表数量仍为 4（v1.4.0 复核）**：SKU 级指标（`skus`）与成本表（§8.5）**都不进 DB** —— 前者是 `compute_metrics` 的**派生结果**（state 内、可重算），后者是商家自维护的**配置**（`config/sku_costs.yaml`）。加第 5 张表要动 §4.7 断言 B′、动迁移、动本节，而这两类数据都没有"需要 SQL 聚合/join"的形态。`grep -rn '__tablename__' backend/app` 仍须恰好 4 行。

#### `agent_traces`

| 列 | 类型 | 约束 |
|---|---|---|
| `id` | `BigInteger` (sqlite 变体 `Integer`) | PK, autoincrement |
| `run_id` | `String(64)` | NOT NULL |
| `trace_id` | `String(32)` | NOT NULL |
| `shop_id` | `String(64)` | NOT NULL |
| `thread_id` | `String(160)` | NOT NULL |
| `trigger` | `String(16)` | NOT NULL, default `manual` |
| `status` | `String(24)` | NOT NULL |
| `step_count` | `Integer` | NOT NULL, default 0 |
| `started_at` | `DateTime(timezone=True)` | NOT NULL |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL |
| `finished_at` | `DateTime(timezone=True)` | NULL |
| `error` | `Text` | NULL |

索引/约束：
- `UNIQUE(shop_id, run_id)`
- `UNIQUE(thread_id)`
- `INDEX(trace_id)`
- `INDEX(status, updated_at)`

**表的定位（冻结，v1.1.0）**：`agent_traces` 是**「一次 run 一行」的运行摘要**，不是节点级 trace 表。
它只承载"这个 run 现在到哪了"这类**要能被 SQL 查、被看板聚合**的状态（`status` / `step_count` / `updated_at` / `error`）。
**节点级 trace 不进表**，走 JSONL `event="node.trace"`（§7.6，理由见 DECISIONS **ADR-015**）：
节点事件是高频 append-only、查询形态是"某 run 最近 N 条日志"、不做 join 与聚合——用表会带来写放大与索引 churn。
`step_count` / `status` 的写入点唯一：`app/agent/tracing.py` 的 `traced()` 包装器（§4.7），不变量「JSONL 节点事件数 == `step_count`」由 `reconcile_step_count` 校准、由 `test_trace_invariant.py` 断言。

#### `approval_request`

| 列 | 类型 | 约束 |
|---|---|---|
| `id` | `BigInteger`(sqlite `Integer`) | PK |
| `run_id` | `String(64)` | NOT NULL |
| `thread_id` | `String(160)` | NOT NULL |
| `shop_id` | `String(64)` | NOT NULL |
| `trace_id` | `String(32)` | NOT NULL |
| `interrupt_id` | `String(64)` | NOT NULL, default `""`（恢复后回填） |
| `action_id` | `String(160)` | NOT NULL |
| `action_type` | `String(32)` | NOT NULL |
| `target` | `String(128)` | NOT NULL |
| `params` | `JSON` | NOT NULL |
| `payload_digest` | `String(64)` | NOT NULL |
| `reason` | `Text` | NOT NULL |
| `evidence` | `JSON` | NOT NULL |
| `risk` | `String(8)` | NOT NULL |
| `status` | `String(16)` | NOT NULL, default `pending` |
| `verdict` | `String(16)` | NULL |
| `decided_by` | `String(64)` | NULL |
| `decided_at` | `DateTime(timezone=True)` | NULL |
| `decision_reason` | `Text` | NULL |
| `created_at` | `DateTime(timezone=True)` | NOT NULL |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL |

索引/约束：
- **`UNIQUE(action_id)`** ← 审批行的身份（**不设 `approval_id` 列**：`action_id` 已唯一标识"这个 run 的这条提议"，再挂一个 `f"{run_id}:{action_id}"` 只会让 ID 里出现两次 `run_id`）
- `UNIQUE(thread_id, action_id)`（冗余但便于按 thread 查重；实现可只建前一条）
- `INDEX(status, created_at)`
- `INDEX(run_id)`
- `INDEX(shop_id, status)`

`interrupt_id` 的写入时序：`interrupt()` 是阻塞的，它的 `id` 只有在**恢复后**才可能拿到，而落库发生在 `interrupt()` **之前** →
首次 INSERT 写 `interrupt_id=""`，恢复后由 `backfill_interrupt_id` 以 `WHERE interrupt_id=''` 条件回填（见 §5.8）。**实现必须按这个顺序**。

#### `action_log`

| 列 | 类型 | 约束 |
|---|---|---|
| `id` | `BigInteger`(sqlite `Integer`) | PK |
| `action_log_id` | `String(64)` | NOT NULL（**就等于 `idempotency_key`**，见下） |
| `idempotency_key` | `String(64)` | NOT NULL |
| `action_id` | `String(160)` | NULL（自动执行的动作无审批行） |
| `run_id` | `String(64)` | NOT NULL |
| `shop_id` | `String(64)` | NOT NULL |
| `action_type` | `String(32)` | NOT NULL |
| `target` | `String(128)` | NOT NULL |
| `request_params` | `JSON` | NOT NULL |
| `response` | `JSON` | NULL |
| `status` | `String(24)` | NOT NULL |
| `attempt` | `Integer` | NOT NULL, default 1 |
| `upstream_request_id` | `String(64)` | NULL |
| `error_category` | `String(24)` | NULL |
| `error_message` | `Text` | NULL |
| `created_at` | `DateTime(timezone=True)` | NOT NULL |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL |
| `finished_at` | `DateTime(timezone=True)` | NULL |

索引/约束：
- **`UNIQUE(idempotency_key)`** ← 执行幂等的唯一来源
- `INDEX(run_id)`
- `INDEX(shop_id, created_at)`
- `INDEX(status, created_at)`（调度器对账扫 `unknown`）

`action_log_id = idempotency_key`（就是 64 hex 本身），简单且天然幂等。

#### `effect_snapshot`

| 列 | 类型 | 约束 |
|---|---|---|
| `id` | `BigInteger`(sqlite `Integer`) | PK |
| `snapshot_id` | `String(32)` | NOT NULL |
| `action_log_id` | `String(64)` | NOT NULL |
| `run_id` | `String(64)` | NOT NULL |
| `shop_id` | `String(64)` | NOT NULL |
| `action_type` | `String(32)` | NOT NULL |
| `target` | `String(128)` | NOT NULL |
| `offset_days` | `SmallInteger` | NOT NULL |
| `status` | `String(16)` | NOT NULL, default `scheduled` |
| `due_at` | `DateTime(timezone=True)` | NOT NULL |
| `evaluated_at` | `DateTime(timezone=True)` | NULL |
| `baseline` | `JSON` | NULL |
| `followup` | `JSON` | NULL |
| `delta` | `JSON` | NULL |
| `attribution` | `String(32)` | NOT NULL |
| `caveats` | `JSON` | NULL |
| `created_at` | `DateTime(timezone=True)` | NOT NULL |

索引/约束：
- `UNIQUE(action_log_id, offset_days)`
- `INDEX(status, due_at)`
- `INDEX(run_id)`

### 6.2 幂等键总表（背下来）

| 场景 | 键 | 约束位置 |
|---|---|---|
| 一次运行 | `(shop_id, run_id)` | `agent_traces` UNIQUE |
| 线程 | `thread_id` | `agent_traces` UNIQUE |
| 一条审批提议 | `action_id` | `approval_request` UNIQUE |
| 一次执行 | `idempotency_key`（生效参数哈希） | `action_log` UNIQUE |
| 一个效果窗口 | `(action_log_id, offset_days)` | `effect_snapshot` UNIQUE |

### 6.3 SQLite ↔ Postgres 可移植规则（硬性，违反则生产切库必炸）

1. **只用** `sa.JSON`（不用 `JSONB`、不用 `postgresql.JSONB`、不用 `sqlite.JSON`）。
2. **所有 `String` 必须带长度**（PG 无所谓，但契约要求统一），`Text` 用于长文。
3. **主键**：`sa.BigInteger().with_variant(sa.Integer, "sqlite")`（SQLite 的 AUTOINCREMENT 只认 `INTEGER PRIMARY KEY`）。
4. **时间**：列用 `sa.DateTime(timezone=True)`；值一律 Python 侧 `datetime.now(timezone.utc)`。**禁止** `server_default=func.now()` / `CURRENT_TIMESTAMP`（SQLite 与 PG 语义不同）。默认值用 `default=_utcnow`（Python callable）。
5. **读时间**必须过 `to_utc(dt)`：SQLite 返回 naive datetime，`to_utc` 把 naive 当 UTC 并补 tzinfo；naive 之外的情况原样返回。
6. **禁止**任何方言专有 SQL：`INSERT OR REPLACE`、`ON CONFLICT`、`PRAGMA`（WAL 除外，见下）、`json_extract`、`strftime`、`||` 拼接。
   冲突处理统一走「先查后插 + 捕获 `IntegrityError` 后重查」。
7. **唯一约束**在 SQLite 与 PG 都是真约束，可以依赖；**部分索引/表达式索引禁止使用**。
8. SQLite 开发期附加：启动时执行 `PRAGMA journal_mode=WAL`（仅当 `database.url` 以 `sqlite` 开头），提高并发读写；这是**唯一**允许的 `PRAGMA`。
9. DSN 差异只体现在 `database.url` 与 `checkpoint.type/path/url` 两个配置项；**业务代码**里不得按方言分支（`if sqlite:` / `if postgresql:` 一律禁止）。
   **例外：连接配置**（`create_engine` 的 `pool` / `poolclass` / `connect_args`）**必须**按方言分支——SQLite 需要 `connect_args={"check_same_thread": False}` 且内存库要 `poolclass=StaticPool`（否则测试之间看不到同一份库），PG 需要连接池参数。这类分支**只许集中在一处**（`app/db.py` 的 `create_engine_from_settings`），且必须注释原因（`# SQLite: ...` / `# PG: ...`）。
   **为什么**：可移植性的目标是"**SQL 与 ORM 用法**不绑方言"，不是"代码里不能出现数据库名字"。把连接层也硬塞进"零分支"教条，结果是人人暗中违反，最后整条规则被当形式（v1.4.0 前的字面写法就是这样失效的）。判据要落在"**行为面**"上：换方言时，只有 `db.py` 一个文件可以变。

### 6.4 会话获取（`app/db.py`）

```python
def create_engine_from_settings(settings: Settings) -> Engine: ...
def init_db(engine: Engine) -> None: ...                       # create_all（dev）；生产由 alembic/init SQL 负责
@contextmanager
def session_scope() -> Iterator[Session]: ...                  # 提交/回滚/关闭
def get_session() -> Iterator[Session]: ...                    # FastAPI Depends 用
def run_db(fn: Callable[[Session], T]) -> T: ...               # 同步执行一个事务
async def arun_db(fn: Callable[[Session], T]) -> T: ...         # await asyncio.to_thread(run_db, fn)
def to_utc(dt: datetime | None) -> datetime | None: ...
def _utcnow() -> datetime: ...
```

- ORM 是**同步** SQLAlchemy 2.x（`Session`）。异步场景（agent 节点 / async 路由）里调 DB 一律 `await arun_db(lambda s: ...)`。
- `session_scope()` 是模块级单例工厂（由 `init_db`/lifespan 设置好 engine），测试用 `init_db` 覆盖。
- 事务边界：一个 HTTP 请求 = 一个 `session`；一个 agent 节点内的多次写 = 一个 `session_scope()`。

---

## 7. REST 契约

- Base：`http://127.0.0.1:8000`；前缀 `settings.app.api_prefix`（默认 `/api`）。
- 全部 `application/json; charset=utf-8`。
- FastAPI `response_model` 必须开启，**未知字段禁止透传**。
- trace：每个响应头带 `X-Trace-Id`；日志里也有。

### 7.1 错误包络（所有 4xx/5xx 统一）

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "decisions 缺少 1 个 action_id 的裁决",
    "details": {"missing_action_ids": ["run_x:price_update:3400...002"]},
    "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b"
  }
}
```

错误码表（**冻结**）：

| code | HTTP | 触发条件 | 前端处理 |
|---|---|---|---|
| `VALIDATION_ERROR` | 400 | 请求体 schema 校验失败；`decisions` 多/少/重复；`edit` 无 `edited_params`；参数 schema 不符；`edited_params` 出现在非 edit | 标红表单，展示 `details` |
| `THREAD_NOT_FOUND` | 404 | `thread_id` 在 checkpointer 中无 checkpoint | 提示"该审批已失效，请刷新列表" |
| `APPROVAL_NOT_FOUND` | 404 | `interrupt_id` 与当前挂起中断不一致 | 同上 |
| `RESOURCE_NOT_FOUND` | 404 | `/api/metrics` 的 `shop_id` 无数据 | 空态 |
| `DUPLICATE_DECISION` | 409 | 该 `action_id` 已裁决 | 刷新列表 |
| `RUN_IN_PROGRESS` | 409 | 同一 `(shop_id, run_id)` 正在运行 | 禁用"再跑一次" |
| `RUN_STATE_CONFLICT` | 409 | thread 存在但不处于 `awaiting_approval` | 刷新/提示 |
| `UPSTREAM_ERROR` | 502 | 上游抖店不可重试错误 / 不可用 | 展示 `message`，提供重试 |
| `INTERNAL_ERROR` | 500 | 未捕获异常（`message` 固定为"内部错误"，细节只进日志与 `trace_id`） | 展示 trace_id |

**规则**：`502` 只在"上游（抖店）"问题时用；本地 bug 一律 500。禁止把 502 当兜底。

### 7.2 `POST /api/agent/run`

请求：

```json
{"shop_id": "12345678", "run_id": null, "trigger": "manual", "dry_run": true}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `shop_id` | str | 是 | `^[A-Za-z0-9_-]{1,64}$` |
| `run_id` | str \| null | 否 | 同上；`null` → 服务端生成 `run_{YYYYMMDDTHHMMSSZ}_{8hex}` |
| `trigger` | str | 否 | `manual`(默认) / `scheduled` / `retry` |
| `dry_run` | bool | 否 | 默认取 `settings.doudian.dry_run`；`true` 时用 fixtures transport，不触达真实上游 |

响应 `202 Accepted`：

```json
{
  "run_id": "run_20261023T142530Z_9f3c1a7b",
  "thread_id": "12345678:run_20261023T142530Z_9f3c1a7b",
  "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
  "status": "awaiting_approval",
  "pending_approvals": [
    {
      "action_id": "run_20261023T142530Z_9f3c1a7b:price_update:3400000000000000002",
      "run_id": "run_20261023T142530Z_9f3c1a7b",
      "thread_id": "12345678:run_20261023T142530Z_9f3c1a7b",
      "shop_id": "12345678",
      "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
      "interrupt_id": "6f1e9c2a7b3d4e5f",
      "action_type": "price_update",
      "target": "3400000000000000002",
      "params": {"price_cents": 9600},
      "reason": "GMV 环比 -31.2%，低于近 7 日均值 30% 阈值",
      "evidence": {"anomaly_id": "3f7a9c1b2d4e5f60", "rule_id": "gmv_drop_day", "metrics": {"gmv_cents": 12840000, "gmv_7d_avg_cents": 18660000}},
      "risk": "medium",
      "status": "pending",
      "created_at": "2026-10-23T14:25:30Z"
    }
  ],
  "effect_report": null
}
```

> 审批行的身份就是 `action_id`（`approval_request.action_id` UNIQUE）。**没有** `approval_id` 字段：多一层 `f"{run_id}:..."` 只会让 ID 里出现两次 `run_id`，且两个字段永远同值。

错误：`400 VALIDATION_ERROR`、`409 RUN_IN_PROGRESS`、`502 UPSTREAM_ERROR`。
已存在同 `(shop_id, run_id)` 且终态 → `200`（不是 202），返回同样结构 + 已存结果。

### 7.3 `GET /api/alerts`

Query：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `shop_id` | str \| null | null | 过滤店铺 |
| `status` | str | `pending` | `pending`/`approved`/`rejected`/`expired`/`all` |
| `limit` | int | 50 | 1..200 |
| `before_id` | int \| null | null | keyset 分页：取 `id < before_id` |

响应 `200`：

```json
{
  "items": [
    {
      "action_id": "run_x:price_update:3400000000000000002",
      "run_id": "run_x",
      "thread_id": "12345678:run_x",
      "shop_id": "12345678",
      "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
      "interrupt_id": "6f1e9c2a7b3d4e5f",
      "action_type": "price_update",
      "target": "3400000000000000002",
      "params": {"price_cents": 9600},
      "reason": "GMV 环比 -31.2%",
      "evidence": {"anomaly_id": "3f7a9c1b2d4e5f60", "rule_id": "gmv_drop_day"},
      "risk": "medium",
      "status": "pending",
      "created_at": "2026-10-23T14:25:30Z"
    }
  ],
  "next_cursor": null
}
```

`next_cursor`：还有下一页时返回 `"<最后一条 id>"`（字符串形式的整数）；否则 `null`。
排序：`created_at DESC, id DESC`（稳定）。

**`next_cursor` 的取法（v1.3.2）**：这一页允许经 `app/api/queries.py` 直接 `SELECT`（keyset：`created_at DESC, id DESC` + `id < before_id`，多取一条判断"还有没有下一页"），因为 §5.8 的 `list_by_status` 返回的 `PendingApproval[]` **不含行 `id`**，表达不了游标。这只是**读**路径的补充：提议的写入与裁决仍只走 §5.8 的仓储，`queries.py` 不得有任何写操作（边界见 §0.1）。

**SKU 维度的落点（v1.4.0，明确）**：`/api/alerts` 返回的是 `PendingApproval[]`（审批提议），**不是 `Anomaly[]`**。SKU 级信息通过 `target`（= `action_id` 里的 `sku_id`，§4.5）与 `evidence` 体现：
- SKU 级提议的 `target` 就是 `sku_id`；`evidence` **必须**带 `sku_id` 与 `rule_id`（`evidence = {"anomaly_id": ..., "rule_id": ..., "sku_id": <str|null>, "metrics": {...}}`）。
- **`Anomaly` 本身不出现在任何 HTTP 响应里**（它只在 state、`audit` 与 JSONL 日志中）。因此 §12 不需要 `sku_id` 的顶层 TS 字段：`PendingApproval.evidence` 已是 `Record<string, unknown>`，前端要显示就按需取 `evidence["sku_id"]`。
- 这与"审批人要知道是哪件货"的需求不冲突：**提议行已经带 sku_id**。真正需要 SKU 明细看板时再提契约变更（那要先解决 `/api/metrics` 的数据来源问题，见 §7.5）。

### 7.4 `POST /api/approve`

请求：

```json
{
  "thread_id": "12345678:run_20261023T142530Z_9f3c1a7b",
  "interrupt_id": "6f1e9c2a7b3d4e5f",
  "operator": "ops_zhang",
  "decisions": [
    {"action_id": "run_x:price_update:3400000000000000002", "verdict": "edit",
     "edited_params": {"price_cents": 9700}, "operator": "ops_zhang", "reason": "降幅收敛到 3%"},
    {"action_id": "run_x:stock_update:3400000000000000003", "verdict": "reject",
     "edited_params": null, "operator": "ops_zhang", "reason": "该 SKU 已停售"}
  ]
}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `thread_id` | str | 是 | 必须存在于 checkpointer |
| `interrupt_id` | str | 是 | 必须等于当前挂起 interrupt 的 `id` |
| `operator` | str | 是 | `^[A-Za-z0-9_.-]{1,64}$` |
| `decisions` | list | 是 | 至少 1 条，见 §4.4 校验规则 |
| `decisions[].action_id` | str | 是 | 必须属于当前 `pending_actions` |
| `decisions[].verdict` | str | 是 | `approve`/`reject`/`edit` |
| `decisions[].edited_params` | obj \| null | 是（可 null） | `edit` 必非空；其他必须 `null` |
| `decisions[].operator` | str \| null | 否 | 缺省用顶层 `operator` |
| `decisions[].reason` | str \| null | 否 | 建议填写 |

响应 `200`：

```json
{
  "thread_id": "12345678:run_20261023T142530Z_9f3c1a7b",
  "run_id": "run_20261023T142530Z_9f3c1a7b",
  "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
  "status": "done",
  "applied": [
    {"action_id": "run_x:price_update:3400000000000000002", "verdict": "edit",
     "status": "succeeded", "action_log_id": "9a1b...64hex", "error": null},
    {"action_id": "run_x:stock_update:3400000000000000003", "verdict": "reject",
     "status": "rejected", "action_log_id": null, "error": null}
  ],
  "pending_approvals": [],
  "effect_report": {
    "run_id": "run_x",
    "generated_at": "2026-10-23T14:31:05Z",
    "attribution": "single_arm_before_after",
    "windows": [
      {"offset_days": 1, "due_at": "2026-10-24T14:30:05Z", "snapshot_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "status": "scheduled"}
    ],
    "conclusion": "效果窗口已登记，尚未到达评估时间（最早 T+1）。",
    "caveats": ["无对照组，指标变化可能来自季节性/活动/竞品等外部因素", "归因强度：弱（单臂前后对比）"]
  }
}
```

`applied[].status` 的取值域 = `ActionLogStatus`（`pending`/`succeeded`/`failed`/`unknown`/`skipped_duplicate`）**∪** `{"rejected"}`：
`reject` 的动作**不写 `action_log`**，其 `status` 固定为 `"rejected"`、`action_log_id` 为 `null`。因此 `AppliedDecision.status` 必须是普通 `str`，不能声明为 `ActionLogStatus`。

错误：`400 VALIDATION_ERROR` / `404 THREAD_NOT_FOUND` / `404 APPROVAL_NOT_FOUND` / `409 DUPLICATE_DECISION` / `409 RUN_STATE_CONFLICT` / `502 UPSTREAM_ERROR`。

### 7.5 `GET /api/metrics`

Query：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `shop_id` | str | 必填 | |
| `date_from` | str | 今天-6 | `YYYY-MM-DD`（Asia/Shanghai） |
| `date_to` | str | 今天 | 含端点 |
| `granularity` | str | `day` | 仅支持 `day` |

响应 `200`：

```json
{
  "shop_id": "12345678",
  "granularity": "day",
  "unit": {"money": "cent", "date": "Asia/Shanghai", "ratio": "0..1"},
  "points": [
    {
      "date": "2026-10-22",
      "gmv_cents": 12840000,
      "order_count": 342,
      "paid_order_count": 318,
      "refund_order_count": 11,
      "refund_amount_cents": 215000,
      "aov_cents": 40377,
      "refund_rate": 0.034591,
      "uv": 5120,
      "cvr": 0.062109,
      "gmv_7d_avg_cents": 11980000,
      "gross_margin": 0.1832,
      "cost_coverage_ratio": 0.7418,
      "computed_at": "2026-10-23T01:00:00Z"
    }
  ]
}
```

错误：`400 VALIDATION_ERROR`（日期格式/区间倒置）、`404 RESOURCE_NOT_FOUND`（无该店铺任何数据）。
`points` 按 `date` 升序；没有数据的日期**不补零**（缺就是缺，前端自己决定怎么画）。

**数据源（v1.3.2 明确）**：本端点**没有指标表**（§6.1 的四张表里没有），唯一已存在的指标事实源是 LangGraph checkpoint 的 state——`compute_metrics` 把当日 `metrics` 与近 8 天 `history` 写进 state（§3.3），本端点按 `date` 合并去重（后写的 run 覆盖先写的）后输出。由此固定三条：

1. 该店铺在请求区间内**没有任何 state 数据** → `404 RESOURCE_NOT_FOUND`（不是 200 + 空数组）；
2. **缺的日期不补零、缺的字段不补零**：`uv` / `cvr` 等"算不出来"的一律 `null`（§5.3），**禁止**用 0 或均值填充；
3. run 枚举经 `app/api/queries.py` 只读取 `agent_traces`（`run_id` / `thread_id` / `status` / `started_at`），不写、不校准 `step_count`（边界见 §0.1）。

**v1.5.0 新增两个可选字段（`MetricPoint`，纯增量）**：`gross_margin` 与 `cost_coverage_ratio`，两者都是**店铺级**成本口径（§5.3 / §3.3），**默认 `null`**：

- **语义**：`gross_margin` = 只对有成本的 SKU 加权的毛利率；`cost_coverage_ratio` = 有成本成交额 / 全店 GMV。**必须成对读**——覆盖率 10% 的 `-0.5` 不代表"全店在亏"。
- **`null` 不是 `0`**：`null` = 没配成本 / 没做按 SKU 归集 / 分母为 0；`0.0` = "不赚不亏"这个真事实。消费者**禁止**把 `null` 当 0 参与比较或画成 0 的柱子。
- **老 checkpoint 兼容**：`MetricsSnapshot` 里没有这两个键的历史快照，经 `MetricPoint` 的默认值校验为 `null`（响应模型 `model_config` 只 forbid extra，不改默认），**不需要回填、不需要重跑**。
- **`cost_coverage_ratio` 可能 `> 1`**：上游汇总与订单明细对不上时会如实给出 `> 1`，**不裁剪**（§5.3）。消费者不要把它当"百分比必然 ≤ 100%"来格式化。

**✅ 前端 TS 类型已同步（v1.5.0 落地，不是未完成项）**：`frontend/src/api/types.ts` 的 `MetricPoint` 已加 `gross_margin?: number | null` 与 `cost_coverage_ratio?: number | null`（:124-125），**同一次提交**里改完了 §12 的逐字块（所以 `frontend/scripts/verify.mjs` 的 §12 diff 是绿的）。

前端看板的"毛利率"卡片**已改为直接读后端字段**（`frontend/src/lib/metrics.ts`：取值零计算，`null` 显示「未配置成本」而不是 `0.00%` 或 `—`），并且**删除了**原先那段自算口径——旧实现用**契约外字段** `cost_cents` 与另一套公式 `(GMV − 退款 − 成本) / (GMV − 退款)`，与后端店铺级加权**同名不同义**，同一个中文名下曾同时存在两个数。现在 `cost_cents` 已从 `src/api/extension.ts` 移除（文件头写明"不要加回来"），`verify-suite.ts` 里加了**反向断言**：只给 `cost_cents`、不给 `gross_margin` 时，毛利率必须一个数都算不出来——这条锁死旧路径不得回潮。

覆盖率提示：`cost_coverage_ratio < 1` 时卡片显示「仅覆盖 X% 成交额」；`> 1` **原样显示不裁剪**（用来暴露上游汇总与明细对不上）；缺失时说"未给出"。**`0` 与 `null` 都不得被判成"全店不赚不亏"。**

**`/api/metrics` 仍是店铺级（v1.4.0 明确，v1.5.0 不变）**：响应里**没有** `skus`（v1.5.0 加的两个字段是**店铺级**的，不是 SKU 明细）。原因不是"不需要"，而是**当前没有可服务的数据源**——四张表里没有指标表（§6.1），历史指标从哪来本节此前就没定义。把 `skus` 挂到一个没有落库的来源上，只会制造一条实现不了的契约。
**处置**：SKU 级指标只在 `MetricsSnapshot`（state 内）可见，通过 `Anomaly.sku_id` → `ActionProposal.target` → `/api/alerts` 这条链路被运营看到（§7.3）。**要彻底解决**（SKU 明细看板 + 历史趋势）需要先做"指标持久化"决策（新增表或 JSONL 汇，走契约变更 + 一条新 ADR），不在 v1.4.0 范围。
`/api/logs` 里的 `anomaly.detected` 事件可以带 `sku_id`（`data` 内自由字段），这是当前唯一能看到 SKU 级异常明细的地方。

### 7.6 `GET /api/logs`

Query：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `run_id` | str \| null | null | |
| `level` | str \| null | null | `debug`/`info`/`warn`/`error` |
| `event` | str \| null | null | 事件名前缀匹配，如 `action.` |
| `limit` | int | 100 | 1..500 |
| `before_seq` | int \| null | null | 取 `seq < before_seq` |

响应 `200`：

```json
{
  "items": [
    {
      "seq": 1287,
      "ts": "2026-10-23T14:26:02.113Z",
      "level": "info",
      "event": "action.executed",
      "run_id": "run_x",
      "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
      "shop_id": "12345678",
      "node": "execute",
      "message": "price updated",
      "data": {"action_id": "run_x:price_update:3400...002", "idempotency_key": "9a1b..."}
    }
  ],
  "next_cursor": "1287"
}
```

- 数据来源：JSONL 日志汇（`settings.observability.log_file`，默认 `data/logs/app.jsonl`）。行结构就是上面 `items[]` 的元素（`seq` 由写入端分配）。
- 排序：`seq DESC`。`next_cursor` 为最后一条 `seq` 的字符串形式，无更多则 `null`。
- 日志汇是**启动时**从文件末尾续上 `seq`（读取最后一行 `seq` +1）；文件不存在则从 1 开始。
- `event` 是**自由字符串**，不是封闭枚举：服务端只做**前缀**过滤、**不校验**枚举值（未知事件名照样原样返回）。已登记的形状只有两类：`node.trace` 见 §7.6.1，业务审计事件见 §7.6.3。
- 脱敏是**两层**（v1.3.3；v1.5.0 键名层补 `password`）：`data` 里的敏感**键名**（`app_secret`/`access_token`/`sign`/`authorization`/`cookie`/`password`，大小写不敏感）整个值替换为 `"***"`，**且** `message` 与 `data` 里的**自由文本**还要做值级 scrub（`access_token=xxx` 这类）——只做键名层时 `message` 完全不受保护，而上游错误原文恰好写在那里。完整语义、已知边界与测试要求见 **§7.6.2**。

#### 7.6.1 节点级 trace 的事件形状（`event="node.trace"`，冻结 v1.2.0）

**节点级 trace 只存在于这里，不写表**（表只放 run 摘要，见 §6.1 与 DECISIONS ADR-015）。该事件由 `app/agent/tracing.py` 的 `traced()` 包装器写出（§4.7），是 `agent_traces.step_count` 的事实源。三态 `outcome` 的取舍理由见 **DECISIONS ADR-016**（控制流中断 ≠ 失败），本节只冻结形状。

正常执行（`outcome="ok"`）：

```json
{
  "seq": 1288,
  "ts": "2026-10-23T14:26:02.113Z",
  "level": "info",
  "event": "node.trace",
  "run_id": "run_x",
  "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
  "shop_id": "12345678",
  "node": "execute",
  "message": "node ok",
  "data": {
    "outcome": "ok",
    "duration_ms": 42.7,
    "input_digest": "3f7a9c1b2d4e5f60",
    "output_digest": "8b2c4d6e0f1a3b5c",
    "error": null
  }
}
```

HITL 挂起（`outcome="interrupted"`，**这是正常控制流，不是故障**）：

```json
{
  "seq": 1289,
  "ts": "2026-10-23T14:26:02.360Z",
  "level": "info",
  "event": "node.trace",
  "run_id": "run_x",
  "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
  "shop_id": "12345678",
  "node": "approval_gate",
  "message": "node interrupted (awaiting human decision)",
  "data": {
    "outcome": "interrupted",
    "duration_ms": 12.1,
    "input_digest": "3f7a9c1b2d4e5f60",
    "output_digest": null,
    "error": null
  }
}
```

失败（`outcome="error"`）：

```json
{
  "seq": 1290,
  "ts": "2026-10-23T14:26:05.001Z",
  "level": "error",
  "event": "node.trace",
  "run_id": "run_x",
  "trace_id": "7c1f0b9e2d4a4c1e9b3a5d7f1c2e4a6b",
  "shop_id": "12345678",
  "node": "collect",
  "message": "node failed",
  "data": {
    "outcome": "error",
    "duration_ms": 3011.4,
    "input_digest": "3f7a9c1b2d4e5f60",
    "output_digest": null,
    "error": {"type": "DoudianUpstreamError", "message": "upstream 503 after 4 attempts"}
  }
}
```

字段规则（冻结）：

- **不新增顶层字段**：`node` 用 §7.6 的顶层 `node`；`outcome` / `duration_ms` / `input_digest` / `output_digest` / `error` 一律放在 `data` 里。这样 `/api/logs` 的响应 schema 只有一套。
- `data.outcome ∈ {"ok", "interrupted", "error"}`（必填，v1.2.0 新增；写入端必须写，见 §11 的 agent-core 项）。
- **读取端兼容规则（v1.2.0，向后兼容旧日志）**：v1.1.0 及更早写入的 `node.trace` 没有 `outcome` 键。读取方（`/api/logs` 之外的任何消费者，含前端）**缺键时按 `"ok"` 读**，不得因缺键报错、不得因缺键把该事件当失败；遇到三态之外的未知值**原样显示、不猜**。这条规则让升级不需要回填历史日志（见 §13 兼容性声明）。
- **`outcome` ↔ `level` 的对应是硬规则**：`ok`→`info`、`interrupted`→`info`、`error`→`error`。判 `level` 只许看 `outcome`，不许看"有没有异常抛出"——`interrupt` 就是靠抛异常挂起的。
- `error` 只在 `outcome="error"` 时非空，形状固定 `{"type": "<…>", "message": "..."}`（`message` 过 `redact_keys`（实际走 §7.6.2 的两层；`redact_keys` 同时喂给键名层与值级层））；`ok` / `interrupted` 必须为 `null`。
- **`error.type` 的取值（v1.3.1，ADR-019）**：可以是**异常类名**（`type(exc).__name__`，对应 §4.7 规则 5b：节点抛了异常），**或固定标记 `"NodeDeclaredFailure"`**（对应 §4.7 规则 5c2：节点主动声明失败、**未抛异常**——它把 run 从非 `failed` 推进到 `failed`，走的是 §4.2 里 `collect --(fail)--> finalize` 这类**正常边**）。键集合仍是 `{type, message}` 两个键，**没有新增键、没有改键位置**，因此 **JSONL 消费者、`/api/logs`、前端都不需要改**：`type` 本来就只被当字符串展示与 grep，读取端不对它做枚举校验。
- **不要**用"`error` 是否非空"或"`error.type` 是不是异常类名"来判"节点是否抛过异常"——`outcome` 只表达**这次执行的结果**（§4.7 规则 5），失败有两个来源（抛异常 / 显式宣告），二者在 `outcome` 上同为 `error`，区分它们靠 `error.type` 的取值而不是靠猜。
- `output_digest` 在 `interrupted` / `error` 时为 `null`（没有 partial state 可摘要）。
- `input_digest` / `output_digest` 是 16 位 hex（sha256 前缀），**不存原文**——原文在 LangGraph checkpoint 里，日志不重复承载。
- `message` 取值固定三种：`"node ok"` / `"node interrupted (awaiting human decision)"` / `"node failed"`（便于 grep 与前端分组）。
- 每个节点**每次实际执行**恰好一条（含 `interrupt` 恢复导致的重放，含 `interrupted` 的那一次）。因此 `count(run_id=X, event=="node.trace") == agent_traces[X].step_count`（§4.7 不变量），且该计数**不区分 outcome**。

**前端约定**（v1.2.0，ADR-016）：执行日志页按 `data.outcome` 分色，不按 `level` 分色——`interrupted` 显示为"等待审批"的中性态（灰/蓝），`error` 才是红。`level` 只用于服务端过滤与告警阈值。缺 `outcome` 的旧事件按 `"ok"` 读（见上一条读取端兼容规则）。**禁止**用"`data.error` 是否非空"或"`level=="error"`"替代 `outcome` 判断挂起态——那正是 v1.1.0 把正常暂停画成故障的成因。

#### 7.6.2 脱敏的两层与边界（v1.3.3 冻结，ADR-020）

**唯一入口**：`app/observability/logging.py` 的 `redact_any()`。`redact()` 是它的**兼容别名**，语义完全相同（v1.3.3 起 `redact("access_token=SECRET")` 也会遮）。两层在**同一次递归**里完成，不存在"只调了一层"的调用方式。

**两层各自的职责，以及为什么两层都要**

| 层 | 作用对象 | 命中判据 | 碰不到什么 |
|---|---|---|---|
| **键名层** | 任意嵌套 dict 的**键名** | 键名（大小写不敏感）∈ `settings.observability.redact_keys`（默认 6 键：`app_secret`/`access_token`/`sign`/`authorization`/`cookie`/`password`），命中就把该键的**整个值**（不论类型、不论深度）换成 `"***"` | 非 dict 结构；**`message` 这类自由文本**——那个键名叫 `message`，永远不叫 `access_token` |
| **值级层** | 任意**字符串**（`message`、`data` 里的字符串值、`error.message` 等） | 文本里出现 `key=value` / `key: value` / `"key":"value"` 的**值**；键集 = `DEFAULT_SCRUB_KEYS` ∪ `redact_keys`（**并集，只增不减**，默认 9 个：`access_token`/`app_secret`/`app_key`/`token`/`secret`/`password`/`sign`/`authorization`/`cookie`） | 结构化 `data` 的**键**（dict 键不是文本，正则看不见它） |

- **只做键名层是不够的（这是真实事故，不是假设）**：上游错误原文是 `str`——`DoudianUpstreamError("… access_token=SECRET-abc123 …")`——它被写进 `message`（和 `data.error.message`），而按键名的脱敏对字符串是 no-op。于是"已脱敏"只对 dict 成立、对**真正会泄露的那条通道**不成立：契约与 DECISIONS 有 5 处声称已脱敏，`access_token=SECRET-abc123` 却原样进了 JSONL（REVIEW-core F1）。
- **只做值级层也不行**：`{"access_token": "…"}` 的键是 dict 键，值级正则要求文本里有 `=`/`:` 分隔符；一旦调用方传的是 dict（`data={"access_token": secret}`），值级层从头到尾看不见它。
- 两层是**两个不同的作用域**，不是"多加一道保险"：任何一层单独存在都有一整类输入 100% 漏遮。

**支持的形态（值级层）**

- **分隔符两种**：`=`（query string / 我们自己的日志）与 `:`（HTTP 头 / JSON）；键名两边可带单引号或双引号，于是有三种常见写法：`access_token=xxx`、`token: xxx`、`"access_token":"xxx"`。
- **值的五种形态**：双引号标量、**单引号标量**（Python `dict` 的 repr `{'sign': 'x'}` 与手拼的 `key='value'` 都是单引号，漏掉它等于对这两类最常见的日志完全不脱敏）、单层对象 `{...}`、单层数组 `[...]`、裸值。
- 对象/数组**整段**替换成 `"***"`（不遮内部字段）：只遮标量会产出 `{"token": ***"v": 1}}` 这种非法 JSON，下游 `json.loads` 直接崩。标量**沿用原来的引号字符**，保证替换后 JSON / Python repr 仍然合法。
- **`_` 与 `-` 互通**（默认键名里的 `_` 同时匹配 `-`）：HTTP 头用连字符（`X-App-Key`、`X-Auth-Token`），SDK 参数用下划线（`app_key`、`access_token`）。只认一种，另一类就**整类**漏遮。
- **左边界只排除字母数字**：`refresh_token=` / `client_secret=` / `id_token=` / `X-Auth-Token:` 会命中（复合键是真实日志里最常见的写法）；`design=1` / `sign_method=hmac-sha256` / `token_type=Bearer` 不会（误伤正常字段和漏遮一样糟）。键名按**长度倒序**匹配，`access_token` 先于 `token`，否则只遮得住 `_token=…` 那一段。已遮过的 `***` 再遮一次仍是 `***`（幂等）。
- `:` 形态的裸值必须**停在下一个 `key=` 之前**：`sign: missing shop_id=12345 order=678` 只遮 `missing`，不吃掉整行（单行 100KB 日志里出现一个 `token:` 就足以让排障线索归零）；反过来 `Authorization: Bearer xxx` 后面没有 `key=`，必须**整段**吞掉——只遮 `Bearer` 等于令牌原样留在后面。

**已知不覆盖（5 条；写在这里，是为了不让它再被当成"已脱敏"）**

| # | 形态 | 为什么不覆盖 / 风险多大 |
|---|---|---|
| 1 | URL 编码的键名（`access%5Ftoken=`、`token%3Dxxx`） | 正则只认字面键名与字面分隔符。httpx 的错误原文一般输出解码后的 URL，所以少见；一旦出现就是 **100% 漏遮**（中风险）。 |
| 2 | 跨行续行（`Authorization:` 换行后再接 `Bearer xxx`） | `:` 形态的值边界显式不跨行（排除 `\n`）。本仓所有脱敏调用点都是单行 `str(exc)`，多行 header dump 目前不存在；将来若有 `repr(headers)`，那是**单行带引号**的形态，仍能遮住（中风险，取决于新增调用点）。 |
| 3 | 非 `str` 的 dict 键（`b"access_token"`、枚举键） | 键名层只匹配 `isinstance(key, str)`，其它键原样下钻、不替换（`bytes` 键里的值不会被遮）。本仓 `data` 全是 str 键（§7.6 顶层字段 + `extra` 展开），风险低；把上游 dict 原样透传时才有影响。 |
| 4 | 自引用 / 循环结构 | 递归没有环检测，`a["self"] = a` 会 `RecursionError`。`JsonlHandler.emit` 有 `handleError` 兜底（不让异常回灌业务），但**直接调 `sink.write()` 的调用方会看到异常**——低概率、高影响（"日志把业务搞挂"这一类）。 |
| 5 | 值里出现敏感词但没有 `key=` 前缀的散文（"令牌是 XYZ"、"the secret is …"） | 值级层需要"键名 + 分隔符"，自然语言里没有分隔符。这是自由文本的**固有极限**，不可能靠正则消除；唯一防线是纪律——不要把凭据拼进散文（§5.1-7 要求 `raw`/`query` 走 `redact`，而不是手拼字符串）。 |

**`stderr` 通道同样脱敏**：`_ScrubbingFormatter` **先** `format()` **再** scrub。不能在 `record` 层脱敏——`%(message)s` 的最终文本只有 format 之后才存在，而容器/journald 收集的正是这段文本。只遮 JSONL 等于同一份凭据换个通道明码落地。

**测试要求（v1.3.3，硬性）**

1. 脱敏的验收**必须包含通道级断言**：调用真实的 `JsonlLogSink.write(event, message=…, data=…)`，**读回落盘文件**断言明文串不出现（`SECRET not in sink.path.read_text()`）。只测 `redact()` / `redact_any()` **不算验收**——把 `write()` 里那行 `_scrub_text` 删掉，函数级用例仍然全绿，这正是本事故的同一个失效模式（测被调函数，不测通道）。
2. **反向验证必须真做**（改完就跑，不是写在文档里）：删掉 `write()` 里对 `message` 的 `_scrub_text(...)` → `test_sink_write_scrubs_message` 必须变红；把 `data` 的 `redact_any(...)` 换成原样透传 → `test_sink_write_scrubs_data_values` / `test_sink_write_scrubs_dict_key` 必须变红。**一条"删掉实现仍然全绿"的测试等于没有测试。**
3. 两份文件都要在（命令见 §10.3）：`backend/tests/platform/test_platform_redaction_channel.py`（通道级）与 `backend/tests/platform/test_platform_redaction_scrub.py`（函数级对抗：复合键、`Bearer` 整段吞、JSON 合法性、误伤回归）。

#### 7.6.3 审计事件（JSONL，v1.5.1 新增登记；v1.5.2 补登记行级账目与 `no_known_fields` / `date_fallback`）

**为什么有这一节（根因，不是措辞补充）**：`backend/app/agent/nodes.py` 的 `_log_audit(...)` 已经在往同一个 JSONL 汇写 6 个事件名、7 个调用点，而 §7.6 只登记过 `node.trace`——也就是说**代码长期在发一种契约里没有位置的事件形状**。这正是本项目反复出事的同一个模式（`adapters.py`、`queries.py` 都是"树里没有的文件自己长出来"：没有位置的东西不会被审查，坏了也没人知道它坏了）。本节把这些形状按**代码实际字段**登记下来；它不是新发明的形状，而是把已经在生产路径上写出的形状补记为契约。

**与 `node.trace` 的分工（硬边界）**

| | `node.trace`（§7.6.1） | 审计事件（本节） |
|---|---|---|
| 回答的问题 | 这个节点**执行**了吗、多久、成功/挂起/失败 | 业务上**发生了什么事实**：指标算完 / 发现异常 / 成本对不上 / 行被拒 |
| 写入者 | 只有 `app/agent/tracing.py` 的 `traced()` | 只有 `app/agent/nodes.py` 的 `_log_audit(...)`（内部调 `JsonlLogSink.write`） |
| `data` 键集合 | **冻结 5 键**（`outcome`/`duration_ms`/`input_digest`/`output_digest`/`error`） | 每个事件名各自一套（见下表）；**不带** `outcome`/`duration_ms`/`*_digest` |
| 条数 | 每个节点**每次实际执行**恰好一条 | 按事实发生：可能 0 条，也可能一轮多条（`anomaly.detected` 的 detail 行） |
| 到达 | 同一个 JSONL 汇、同一个 `/api/logs` | 同左 |

- 两类事件**同处 `/api/logs` 的同一个响应**，靠 `event` 名区分（`node.trace` vs 本节的点分名）。`/api/logs?event=` 是**前缀**匹配，所以 `event=anomaly.` 一次拿到 summary + detail，`event=collect.` 拿到采集层全部。
- **`node.trace` 的键集合没有被本节改动**：`tracing.py` 写入的 `data` 仍然恰好是那 5 个键，`nodes.py` 不写 `node.trace`。审计事件是**新增的行**，不是往 `node.trace` 里加键。因此 §4.7 的不变量「`count(event=="node.trace") == agent_traces.step_count`」照旧只看 `node.trace`，审计事件再多也不影响它。
- 顶层键仍是 §7.6 的 10 个；`node` 由 `_log_audit` 填成节点名（`collect` / `compute_metrics` / `detect_anomalies`），`run_id` / `trace_id` / `shop_id` 取自 state（可为 `null`）。

**事件表（按代码实际字段填；v1.5.1 登记，v1.5.2 补 `rows_out_of_window` / `no_known_fields` / `date_fallback`）**

| `event` | `level` | 触发条件（代码判据） | `data` 键 | 消费者 |
|---|---|---|---|---|
| `metrics.computed` | `info`（恒） | `compute_metrics` 成功算出当日指标（每轮 1 条；run 已被判死时不写） | `date`（业务日 `YYYY-MM-DD`）、`gmv_cents`、`history_days`（含今天，最多 8）、`gmv_7d_avg_cents`（可为 `null`）、`gross_margin`、`cost_coverage_ratio`（后两个 `float` 或 `null`，**绝不填 0**） | 运营："这天算出了什么"；`loss_making` 的唯一解释入口（`gross_margin` 是 `null` 还是 `0.0` 决定它该不该响）；qa；demo 导出 |
| `anomaly.detected`（`kind="summary"`） | `info`（恒） | `detect_anomalies` 跑完（每轮 1 条，**0 命中也有**） | `kind="summary"`、`count`、`rule_ids`、`skipped`、`dropped` | 汇总："今天命中几条、跳过几条、截断几条" |
| `anomaly.detected`（`kind="detail"`） | 由 `severity` 派生：`critical`→`error`、`warn`→`warn`、`info`→`info` | 每条命中异常 1 条（截断后最多 `agent.max_anomalies` 条） | `kind="detail"`、`anomaly_id`、`rule_id`、`metric`、`observed`、`threshold`、`comparator`、`severity`、`window`、`detected_at` | 告警流与排障：alert-only 规则（如 `loss_making`）在人类入口上的**唯一**可见通道（§8.3 的已知缺口） |
| `collect.units_degraded` | `warn` | 有订单行**件数缺失/非正**（且该行最终被接受，已支付 + 有 `sku_id` + 成交额 > 0），按 1 件计 | `kind="units"`、`count`、`reasons`、`sample_limit`、`samples_truncated`、`samples` | 排障："这次为什么漏报亏本"（方向**偏乐观**，§5.3 有算例） |
| `collect.zero_revenue_paid` | `warn` | 有**已支付 + 有 `sku_id` + 成交额 ≤ 0** 的订单行（不进成本口径，但仍进 GMV/订单数） | `kind="zero_revenue_paid"`、`count`、`reasons`、`sample_limit`、`samples_truncated`、`samples` | 同上；`samples[].unit_cost_cents` 用来估"漏了多少成本" |
| `collect.row_rejected` | `warn` | 有订单行**整行被拒**（字段类型/负数金额/件数非 int/`sku_id` 形状错/元素不是对象/**一个已知订单字段都没有 → `reason="no_known_fields"`**），且**不是**"全行被拒"那一档（见下表） | `kind="row_rejected"`、`count`、`reasons`、`sample_limit`、`samples_truncated`、`samples`、`excluded_from_metrics`（恒 `true`）、`rows_seen`、`rows_accepted`、`rows_out_of_window`（v1.5.2） | 归因"GMV 为什么少一截"；qa 钉住"整行不进任何指标"与行级账目恒等式（被拒项 = `count`） |
| `collect.date_fallback` | `warn` | 行**被接受**（进入指标），但 `create_time` **存在却不可用**（falsy / 解析失败），靠 `pay_time` 才解析成功；`create_time` 键根本不存在（只给 `pay_time`）**不算**降级 | `kind="date_fallback"`、`count`、`reasons`（`falsy_value` / `unparseable`）、`sample_limit`、`samples_truncated`、`samples` | 发现"上游把 `create_time` 系统性写坏、靠 `pay_time` 兜住"——指标看起来正常，日期口径却已经悄悄变了；该行**不**进 `collect.row_rejected`，但 `source.partial=true` |
| `sku_costs.unmatched` | `warn` | 本次**商品列表非空**且成本表里有 `sku_id` 不在商品列表中（商品列表为空时**不报**：那会是一串假警报） | `sku_ids`（升序去重）、`count` | 成本配置漂移告警（§8.5）：只记不阻断 |

补充规则（每条都是代码事实，不是建议）：

- **`metrics.computed` 的 `data` 里没有 `computed_at`**：业务日看 `data.date`，写入时刻看行级 `ts`（sink 的写入时间）。不要用 `ts` 反推"指标是哪一刻算的"并当成契约值。
- **`reasons` 恒出现**，但 `zero_revenue_paid` 的样本里**没有** `reason` 字段（样本形状见下），所以该事件的 `reasons` **恒为 `{"unknown": count}`**。**不要**拿它做分类——这一档没有分类可言（每行都是"已支付但成交额为 0"）。
- **`anomaly.detected` 的 summary `skipped` 只有规则 id**：跳过原因不进 JSONL，只在 `state.audit` 的 `errors[].message` 里（`规则 <id> 求值失败已跳过：<reason>`）。要展示原因，请另开出口，不要指望 JSONL 有它。
- **`rule_ids` 是截断后真正产出的异常**（长度 == `count`），`dropped` 才是被 `agent.max_anomalies` 丢掉的条数。
- detail 的 `message` 是人读文案（`<rule.description>（<metric>=<observed>，条件 <comparator>）`）；**机器读取一律用 `data`**。

**`anomaly.detected` 的两种形态（按 `kind` 分支，不许靠"有没有 `rule_id`"猜）**

| 形态 | 必有的键 | **不会**出现的键 |
|---|---|---|
| `kind="summary"` | `kind`、`count`、`rule_ids`、`skipped`、`dropped` | `rule_id`、`observed`、`threshold`、`metric`、`anomaly_id`、`severity` |
| `kind="detail"` | `kind`、`rule_id`、`observed`、`threshold`、`metric`、`anomaly_id`、`severity`、`comparator`、`window`、`detected_at` | `count`、`rule_ids`、`skipped`、`dropped` |

- 消费方**必须按 `data.kind` 分支**（`/api/logs` 的消费方、前端、qa、导出脚本一视同仁）。**禁止**用"`data` 里有没有 `rule_id`"区分——那是 v1.5.2 之前的临时做法：明细没有 `count`、汇总没有 `rule_id`，靠 `.get()` 兜底只会把汇总行静默当明细（或反之），而且**一改代码就悄悄错**。
- 缺 `kind` 的同名事件行（例如只写了 `anomaly_ids` 的旧夹具）**不是本节定义的形状**：不得按 summary 硬读，按"未知形状"原样显示或跳过。
- `observed` / `threshold` 可为 `null`（规则求值时该指标缺失）；`window ∈ {"day", "7d"}`；`comparator` 是规则 `when` 的**来源表达式原文**（如 `"gmv_cents < gmv_7d_avg_cents * 0.7"`），不是算子枚举，**不许**拿它做 `==`/`<` 之类的语义解析。

**行级契约（v1.5.1：一行脏不许打死一轮，也不许静默少算；v1.5.2 补 `no_known_fields` 与 `date_fallback`）**

判定表（判据、结果、JSONL 可见性三列必须一起读）：

| 情形 | 代码判据 | 结果 | 这一档在 JSONL 上的样子 |
|---|---|---|---|
| 一行解析失败（字段类型错 / 负数金额 / 件数非 int / `sku_id` 形状错 / 元素不是对象） | `_read_order_row` 抛 `_RowRejected`（未分级的 `ValueError` → `reason="unclassified"`） | **整行拒绝**：该行**不进入任何指标**（GMV / 订单数 / `by_sku` / 退款一律不进），其余行照常算；`source.partial=true` | `collect.row_rejected`（warn，`count` + `reasons` + ≤5 条样本 + `rows_seen`/`rows_accepted`/`rows_out_of_window`） |
| **一行一个已知订单字段都没有**（`{}` / `{"foo": 1}`，v1.5.2 补登记） | `_read_order_row` 第 0 条：已知字段集合与读取点同源，交集为空 → `_RowRejected(reason="no_known_fields")`。必须在日期判定**之前**判，否则空对象会回退成今天 | **整行拒绝**：它不是"一笔 0 元订单"，不进 `order_count`；整批都是这种行 → 走"全行被拒"（下一档） | `collect.row_rejected`（warn，`reasons.no_known_fields`） |
| 能猜出含义的脏值（件数缺失/非正；已支付但成交额 ≤ 0；**日期靠后续字段才解析成功**） | `_order_units` / `_aggregate_orders` 的**有界降级**；日期降级见 `_row_business_date` 的 `fallbacks` | **有界降级**：件数按 1 件；0 元单不进成本口径（但进 GMV / 订单数）；日期按解析到的字段计入指标；`source.partial=true` | `collect.units_degraded` / `collect.zero_revenue_paid` / **`collect.date_fallback`**（均 warn）；日期降级**不**进 `collect.row_rejected` |
| **整批契约坏**（`data` 不是映射或列表、订单列表字段不是 list、一个已知列表字段都没有、`summary.uv` 形状不对） | `_BatchContractError` | **硬失败**：记一条 `errors` + `status=failed`，走 §4.2 的 `collect --(fail)--> finalize`；**不抛到 API（不出 500）**；**绝不降级成 0 单 0 元** | `collect.failed`（error，`reason="batch_contract"`，行级四数全 0）+ 该节点的 `node.trace`：`outcome="error"`、`error.type="NodeDeclaredFailure"`（§4.7 规则 5c2） |
| **全行被拒**：`rows_seen > 0 且 rows_accepted == 0`（全部被拒 / 全部不是对象 / 全部落在业务窗口外） | `_AllRowsRejected`（`_BatchContractError` 的子类） | **视同整批失败**：与上一档**同一条**路径（`failed` → `finalize`，不 500） | `collect.failed`（error，`reason="all_rows_rejected"`，带 `rows_seen` / `rows_accepted=0` / `rows_rejected` / `rows_out_of_window` / `reject_reasons` / 样本）+ `node.trace` 的 `error`；**没有** `collect.row_rejected`（该分支在写降级事件之前就 return 了） |
| **零行**（订单列表真的是空的，"今天没卖东西"） | `rows_seen == 0` | **正常成功**：指标为 0，继续走规则/审批/执行 | `metrics.computed`（`gmv_cents=0`）+ 各节点 `node.trace`；`collect.*` 一条都不出 |

- **顺序也是契约**：窗口判定**先于**金额解析——窗口外的行既不算脏、也不进指标（`rows_seen` 计入、`rows_accepted` 不计入），把它算成"脏行"只会让拒绝计数虚高。
- **行级拒绝必须原子**：先校验成值、再累加到桶里。不存在"订单数 +1 了但金额没加"的半行（旧实现就是这样留下半行并最终 500）。
- **行级账目在 JSONL 里（v1.5.2 更正 v1.5.1 的"不在 JSONL"）**：`rows_seen`（上游给的元素个数，含非对象元素与窗口外的行）、`rows_accepted`（真正进入指标的行数）、`rows_rejected`（整行被拒的行数）与 **`rows_out_of_window`**（v1.5.2）一起进 `collect.done` / `collect.failed` 的 `data`；`collect.row_rejected` 的 `data` 也带 `rows_seen` / `rows_accepted` / `rows_out_of_window`（该事件的 `count` 即被拒行数）。四者必须满足**行级账目恒等式**：

  `rows_seen == rows_accepted + rows_rejected + rows_out_of_window`

  （在 `collect.row_rejected` 里写作 `rows_seen == rows_accepted + count + rows_out_of_window`：该事件本身只汇总被拒行，不在同一行重复一个等值的 `rows_rejected` 键。）代码在 `_aggregate_orders` 返回前、`collect.failed` / `collect.done` / `collect.row_rejected` 每个出口写入前都显式断言（`_check_row_accounting`，`raise AssertionError` 而不是 `assert`，所以 `python -O` 下也生效）。**`rows_seen - rows_accepted` 不是"被拒行数"**，它 = 被拒 + 窗口外；只读这份差值的看板会把窗口外的行误报成脏行。

**`samples` 的截断语义（`count` 是全量）**

- `count` 是**全量**条数；`samples` 最多前 `sample_limit`（当前 **5**）条；`samples_truncated` 说明后面还有没贴出来的。
- **`count` 的口径边界（v1.5.1 明确）**：`rows_seen` / `count` 是**本次采集看到的行数**，而采集目前只取第一页（`page=0, size=100`）。所以"全行被拒 → failed"这条判据是**页级**的：第一页 100 行全坏、而后面还有好行时，这一轮会 failed。这是刻意的（宁可让人知道第一批就不可用，也不要静默少算 150 行），但**不许**把 `rows_seen` 说成"当天订单总数"。

- **禁止**把 `samples` 的长度当成规模（"看到 5 条样本" ≠ "只有 5 行被拒"）。判断规模只许用 `count`；读样本必须同时读 `sample_limit` 与 `samples_truncated`，**不许**假定 5 这个魔法数（它会变）。
- `message` 里那句"仅前 N 条样本（其余已截断）"只是给 grep/人看的一致性文案，**不是**第二份事实源。
- 样本 schema（逐键，四种事件各一套）：

| 事件 | `samples[]` 的键 |
|---|---|
| `collect.units_degraded` | `order_id`（`str` 或 `null`）、`where`（`"order[i]"`）、`keys`（候选件数字段清单）、`key`（命中字段，`missing` 时为 `null`）、`value`（非法值，`missing` 时为 `null`）、`reason`（`"missing"` / `"non_positive"`） |
| `collect.zero_revenue_paid` | `order_id`、`where`、`sku_id`、`units`（`int` 或 `null`，尽力值）、`unit_cost_cents`（`int` 或 `null`，成本表读不到就 `null`） |
| `collect.row_rejected` | `order_id`（`str` 或 `null`）、`where`、`reason`（`no_known_fields` / `bad_create_time` / `bad_pay_amount` / `bad_units` / `bad_refund_amount` / `bad_sku_id` / `not_an_object` / `unclassified`）、`error`（原始异常原文，含字段路径） |
| `collect.date_fallback` | `order_id`（`str` 或 `null`）、`where`（`"order[i]"`）、`reason`（`"falsy_value"` / `"unparseable"`，取第一个被跳过的候选字段）、`used_key`（真正用上的日期字段，如 `"pay_time"`）、`skipped_keys`（此前存在但给不出日期的字段，如 `["create_time"]`） |

**`source.partial`（`raw.source.partial`）**

- 语义是**"这份指标不完整"**，**不是**"出错了"。任一降级/拒绝（`units_degraded` / `zero_revenue_paid` / `row_rejected` / **`date_fallback`**）、任一非重试的采集失败（店铺信息 / 商品列表 / 订单列表）、整批契约坏，都必须置 `true`。v1.5.2 明确：日期降级（行被接受、但靠 `pay_time` 才拿到业务日期）同样是降级——它没让指标少一行，却换了日期口径。
- 反向**不成立**：`partial=false` 不等于"数据没问题"——窗口外的行不置位，本表没覆盖的上游语义变化也不置位。
- **已知的设计债（v1.5.1，不许读成"已经好了"）**：这一个 bool 目前同时承载三种后果——①整块数据缺失（店铺/商品/订单调用失败）、②某行被降级（缺件数按 1 件 / 日期靠 `pay_time`）、③某行被拒。三者的处置完全不同，但压成了一个字段。后果不是当前 bug 而是**未来的陷阱**：一旦有规则开始用 `partial` 判断"数据够不够算指标"，对那些不返回 `product_count` 的店，它会**恒为 true**，等于永远"数据不够"。要拆就拆成 `missing_blocks` / `degraded_rows` / `rejected_rows` 三个维度，且必须先改本节再改代码。
- 现状（v1.5.2 更正）：`collect.done.data.partial` 与 `collect.failed.data.source.partial`（失败时恒 `true`）**已暴露在 JSONL**；`state.audit` / checkpoint 里的 `raw.source` 是同一份值的完整副本。此前"没有任何 REST 响应或 JSONL 事件暴露它"的说法只对 v1.5.0 及更早成立。

**脱敏（与 `node.trace` 同一条通道，同两层）**

- 这些事件走**同一个** sink：`JsonlLogSink.write` 落盘前对 `message` 做值级 scrub、对 `data` 走 `redact_any`（键名层 + 值级层，§7.6.2）。
- 因此 `data` 里出现 `password` / `access_token` / `sign` / `authorization` / `cookie` 这类**键名**时，整个值被遮成 `"***"`；`message`、`samples[].error` 这类**自由文本**里的 `access_token=…`、`password=…` 也按值级层遮。
- 审计事件**不得**自建 logger 或绕过 sink（`_log_audit` 存在的理由之一就是复用这两层，而不是 new 一个 logger）。
- 写盘失败是**旁路失败**：`_log_audit` 接住异常并打一条 stdlib `logging` warning（带 `event` 名与异常类型，缺陷因此仍可见），业务结论不受影响；`state.audit` 那一份仍随节点返回值进 checkpoint，**不会**因为日志写不进去而丢。

**`trace.reconcile`（已登记，归属 §4.7）**

这是本节之外唯一一个**已经进 JSONL** 的旁路事件，之前一直没登记（审查发现）：`tracing.py` 在启动对账（`reconcile_step_count`）改动 `step_count` 时会写一条，`level="warn"`，`data` 形如 `{log_count, before, after}`。

- 它**不是**审计事件，而是 §4.7 那条"`count(node.trace) == step_count`"不变量的**校准留痕**：出现它就说明进程重启后对账发现了两边不一致。
- 消费者看到它应当理解为"有一次自动校准"，而不是"有业务异常"。**看到它就该去查为什么日志行数与 `step_count` 曾经不一致**（常见原因：日志文件被手工截断、或某次写入部分失败）。
- 它的字段定义以 `tracing.py` 为准，本节不重复；改它的形状必须同时改 §4.7 与本节。

**`collect.done` / `collect.failed`（v1.5.1 起进 JSONL，已登记）**

这两个是采集层的**结果**事件，`v1.5.1` 之前只进 `state.audit`，于是"整批契约坏 / 全行被拒"在 `/api/logs` 上只剩一句 `node.trace` 的 `outcome=error`——**和"上游 503"长得一模一样**，运营分不出"这批数据不能用"和"接口挂了"。现在两者都走 sink：

| 事件 | level | 关键 `data` |
|---|---|---|
| `collect.done` | `info` | `window`/`products`/`methods`/`partial`/`rows_seen`/`rows_accepted`/`rows_rejected`/`rows_out_of_window`（v1.5.2；**没有** `source`——完整快照在 checkpoint 的 `raw` 里） |
| `collect.failed` | `error` | `window`/`source`/`reason`/`rows_seen`/`rows_accepted`/`rows_rejected`/`rows_out_of_window`（v1.5.2）/`reject_reasons`/`row_reject_samples`(≤5)/`row_reject_samples_truncated`（**没有** `products`/`methods`：这两个块失败时本来就没取到；`partial` 在 `source` 里且恒 `true`） |

`collect.failed.data.reason` 是**三档互斥**的枚举，消费者按它分流，不许靠 `message` 文本猜：
- `batch_contract` —— 整批结构坏（不是 dict / 缺订单列表字段 / `summary` 形状不对），一行都没读到；
- `all_rows_rejected` —— 行都在，但一行都没通过校验（`rows_accepted == 0` 且 `rows_seen > 0`）；
- `orders_unavailable` —— 订单接口根本没取到数（网络/鉴权/业务错误），**不是数据坏**。缺这一档就会把"接口挂了"写成"数据坏"。

**本节未登记的事件（故意的，不是漏了）**

`nodes.py` 仍在往 `state.audit` 写这些事件，但它们**不进** JSONL，所以不在上表：`action.proposed`、`approval.decided`、`action.executed`、`effect.registered`、`run.finished`。判据是"**有没有别的人类出口**"（`nodes.py` 模块 docstring 的原话）：它们分别有表级出口（`approval_request` / `action_log` / `effect_snapshot` / `agent_traces`）或已有 `node.trace`，再抄一份进 JSONL 只会让日志翻倍而不增加可查性。把 `event=approval.decided` 写进本节，会制造一个 `/api/logs?event=approval.decided` **永远查不到**的空承诺——这正是本项目反复返工的那类"文档声称的属性在代码里不成立"。要让它们进 JSONL，**先改本节、再改代码**，同一份提交。

反过来也要防一手：`frontend/mock/logs.json` 与 `frontend/src/mock/mockServer.ts` 里的 `collect.started` / `approval.requested` 在 `backend/app` 里**没有生产者**——它们是 mock 自造的事件名，不是"未登记的审计事件"，**不许**照它们反推实现。

### 7.7 `GET /api/healthz`（非契约必需，但三方都要用）

```json
{"status": "ok", "version": "0.1.0", "db": "ok", "checkpointer": "ok", "time": "2026-10-23T14:20:00Z"}
```

任一依赖不可用 → `503`，`{"status":"degraded","db":"error","detail":"..."}`。

---

## 8. 配置契约

### 8.1 加载规则（`app/config.py`）

```python
@dataclass(frozen=True)
class Settings: ...          # 见 §8.2（用 pydantic-settings BaseSettings 也可，但字段路径必须一致）

def load_settings(path: str | Path | None = None) -> Settings: ...
def get_settings() -> Settings: ...        # 进程级缓存
def load_rules(path: str | Path | None = None) -> RuleSet: ...
```

- 优先级（高 → 低）：**环境变量** > `config/settings.yaml` > 代码默认值。
- `settings.yaml` 里可用 `${VAR}` / `${VAR:-default}` 占位；**未解析且无默认 → 启动期 `ConfigError`**（不静默留空）。
- **密钥禁止写进 yaml**：`app_secret` / `access_token` / `webhook_url` 只从环境变量取，yaml 里只写变量名（`*_env` 字段）。
- 加载失败必须是**启动期失败**（进程退出码非 0），不能等到第一个请求。

### 8.2 `config/settings.yaml` 字段语义（逐字）

```yaml
app:
  name: doudian-agent
  env: dev                      # dev | prod；prod 下禁止 dry_run=true 静默启动（warn+严重日志）
  api_prefix: /api
  cors_origins: ["http://localhost:5173"]
  request_timeout_s: 30         # 单请求整体预算（含重试），超时 -> 504? 否：500 INTERNAL_ERROR
database:
  url: "sqlite+pysqlite:///./data/doudian.db"
  echo: false
  pool_size: 5
checkpoint:
  type: sqlite                  # sqlite | postgres
  path: "data/checkpoint.db"    # type=sqlite：文件路径（不是 URL）
  url: ""                       # type=postgres：postgresql://user:pass@host:5432/db
doudian:
  base_url: "https://openapi-fxg.jinritemai.com"
  version: 2
  sign_method: hmac-sha256
  app_key_env: DOUDIAN_APP_KEY
  app_secret_env: DOUDIAN_APP_SECRET
  access_token_env: DOUDIAN_ACCESS_TOKEN
  dry_run: true                 # true => create_doudian_client 注入 fixtures transport
  timeout:
    connect_s: 3.0
    read_s: 10.0
    write_s: 5.0
    pool_s: 2.0
  limits:
    max_connections: 20
    max_keepalive_connections: 10
    keepalive_expiry_s: 30.0
  rate_limit:
    rate_per_sec: 8.0
    burst: 16
    max_concurrency: 4
    max_attempts: 4
    backoff_base_s: 0.4
    backoff_max_s: 8.0
agent:
  max_anomalies: 20
  max_actions_per_run: 5
  approval_timeout_min: 1440    # 调度器把超时未裁决的置 expired
  auto_approve_risk: none       # none | low
  recursion_limit: 50
notifier:
  mode: dry_run                 # dry_run | feishu
  feishu_webhook_env: FEISHU_WEBHOOK_URL
effects:
  windows_days: [0, 1, 3, 7]
  scheduler_enabled: true
  poll_interval_s: 60
observability:
  log_level: INFO
  log_file: "data/logs/app.jsonl"
  redact_keys: ["app_secret", "access_token", "sign", "authorization", "cookie"]
```

字段语义要点（其余自解释）：

- `doudian.dry_run=true` 时 `create_doudian_client` **必须**返回注入了 `httpx.MockTransport` 的 client（§10.2），且**不读** `DOUDIAN_APP_KEY`（用假凭据），保证零凭据可跑。
- `effects.windows_days` 必须是 `[0,1,3,7]` 的**子集**且升序；非法 → 启动期 `ConfigError`。
- `agent.auto_approve_risk=low` 时只有 `risk=="low"` 的提议可跳过 `interrupt`，且必须在 `audit` 里记 `approval.auto`；`critical` 异常产出的动作**永不**自动通过。
- `checkpoint.type=postgres` 时 `checkpoint.url` 必填，缺失 → 启动期 `ConfigError`。

### 8.3 `config/rules.yaml` 与条件表达式求值契约

```yaml
version: "1"
rules:
  - id: gmv_drop_day
    enabled: true
    severity: warn
    metric: gmv_cents
    when: "gmv_cents < gmv_7d_avg_cents * 0.7"
    window: day
    description: "当日 GMV 低于近 7 日均值的 70%"
    actions:
      - type: price_update
        target_from: top_gmv_sku
        params: {price_drop_pct: 3}

  - id: refund_spike
    enabled: true
    severity: critical
    metric: refund_rate
    when: "refund_rate is not None and refund_rate > 0.08"
    window: day
    description: "退款率超过 8%"
    actions:
      - type: stock_update
        target_from: top_refund_sku
        params: {stock: 0}

  # v1.5.0：店铺级加权毛利率。**故意不给 actions**——理由见本节下方"为什么 loss_making 没有 actions"。
  - id: loss_making
    enabled: true
    severity: critical
    metric: gross_margin
    when: "gross_margin is not None and gross_margin < 0.0 and cost_coverage_ratio is not None and cost_coverage_ratio >= 0.6"
    window: day
    description: "有成本覆盖的成交额整体低于商品成本价（加权毛利率 < 0，且成本覆盖 ≥ 60%）"
```

字段语义：

| 字段 | 必须 | 说明 |
|---|---|---|
| `id` | 是 | `^[a-z][a-z0-9_]{1,63}$`，全局唯一 |
| `enabled` | 是 | bool |
| `severity` | 是 | `info`/`warn`/`critical` |
| `metric` | 是 | 主指标名，必须在命名空间内（用于 `Anomaly.metric`） |
| `when` | 是 | 条件表达式源码（§8.3.1 文法） |
| `window` | 是 | `day` / `7d` |
| `description` | 是 | 人读描述，直接进 `Anomaly.message` 前缀 |
| `actions` | 否 | 动作模板列表；缺省 = 只报警不动手 |
| `actions[].type` | 是 | `price_update`/`stock_update`/`coupon_create` |
| `actions[].target_from` | 是 | `top_gmv_sku` / `top_refund_sku` / `lowest_stock_sku` / 或具体 `sku:<id>` |
| `actions[].params` | 是 | **动作模板参数**，不是最终参数 |

动作模板参数 → 最终参数（冻结映射）：

| action type | 模板参数 | 最终 `params` |
|---|---|---|
| `price_update` | `price_drop_pct: number` | `{"price_cents": round(current_price_cents * (1 - price_drop_pct/100))}` |
| `stock_update` | `stock: int` | `{"stock": stock}` |
| `coupon_create` | `discount_cents`,`total_count`,`begin_time`,`end_time` | 原样 |

**SKU 级规则的 `current_price_cents` 解析（v1.4.0，冻结）**：上表里的 `current_price_cents` 在 **SKU 轮**里取**该 `Anomaly.sku_id` 对应 SKU 的** `metrics.skus[*].current_price_cents`（不是店铺级、不是"top sku"）；`target_from` 也随之确定：
- SKU 级异常的 `target` = `anomaly.sku_id`（不需要 `target_from` 再挑一次）；
- 店铺级异常才用 `target_from`（`top_gmv_sku` / `top_refund_sku` / `lowest_stock_sku` / `sku:<id>`）。
- `propose_actions` 的入参 `metrics` 已含 `skus`（§5.3.1），因此**签名不需要改**即可解析；`products` 仍用于校验 `target` 存在与取 `product_id`（`price_update` 需要 `product_id`+`sku_id` 两个参数，见 §5.5 schema 之外的调用侧要求）。
- 一句话：**店铺级异常 → 挑 SKU；SKU 级异常 → 已经是那个 SKU**。两者不要混着写。

**`loss_making` 为什么没有 `actions`（v1.5.0，ADR-021）**：`gross_margin` 是**店铺级加权**指标，它**指不出具体是哪件货在亏**。现有三种动作类型（`price_update` / `stock_update` / `coupon_create`）都要求一个**确定的 `target`**，而店铺级异常只能靠 `target_from` 去挑（`top_gmv_sku` / `lowest_stock_sku`）——那是**猜**。猜错的代价不对称：给"GMV 最高"的好货降价或下架，等于用一次误报去伤害店里最赚钱的商品，且**不可回滚**（价格改了、库存清零了，已经成交的订单收不回来）。所以本规则**只告警、不动手**（`actions` 缺省 = 只报警不动手，见上表）。等 §5.3.1 的 SKU 级 `skus` 落地后，这条规则应改为 **SKU 级**并对亏损 SKU 出 `price_update`（**提价**，不是降价）或停售——那时目标不再需要猜。

**已知缺口（必须登记，不许读成"运营已经能看到"）**：alert-only 的异常（命中了无 `actions` 规则的 `Anomaly`）**不出现在 `/api/alerts`**——该端点读的是 `approval_request` 表，只有**产生了审批行**的异常才可见（§7.3）。因此运营**从审批台看不到** `loss_making`。当前唯一可见通道是 `/api/logs`：`anomaly.detected`（summary 含 `rule_ids`；每条命中异常另有 `kind="detail"` 的明细行，带 `observed` / `threshold` / `severity`，形状见 §7.6.3）与 `metrics.computed`（含 `gross_margin` / `cost_coverage_ratio`）。**需要一个独立的告警出口**（把 `Anomaly` 直接暴露成可读告警流），那属于**设计债**，不是本次实现——见 §13 的 1.5.0 未完成项。

#### 8.3.1 文法（EBNF，冻死）

```
expr        := or_expr
or_expr     := and_expr ( "or" and_expr )*
and_expr    := not_expr ( "and" not_expr )*
not_expr    := "not" not_expr | comparison
comparison  := arith ( comp_op arith )?          # 无 comp_op 时按真值判定，但结果必须是 bool
comp_op     := "<" | "<=" | ">" | ">=" | "==" | "!=" | "in" | "not in" | "is" | "is not"
arith       := term ( ("+" | "-") term )*
term        := factor ( ("*" | "/" | "%") factor )*
factor      := ["+"|"-"] atom
atom        := NUMBER | STRING | "True" | "False" | "None" | NAME | "(" expr ")" | list_literal
list_literal:= "[" [ expr ( "," expr )* ] "]"
NAME        := /[A-Za-z_][A-Za-z0-9_]*/
NUMBER      := /[0-9]+(\.[0-9]+)?/
STRING      := /'[^']*'/ | /"[^"]*"/
```

`not in` / `is not` 是**两个 token 的组合**，实现时可在解析前把 `not in`/`is not` 归一化处理；但语义必须与 Python 一致。

#### 8.3.2 AST 白名单（`compile_expression` 里逐节点检查）

允许的 `ast` 节点（**只有这些**）：

```
Expression, BoolOp(And|Or), UnaryOp(Not|USub|UAdd), BinOp(Add|Sub|Mult|Div|Mod),
Compare(Eq|NotEq|Lt|LtE|Gt|GtE|In|NotIn|Is|IsNot), Constant(int|float|str|bool|None),
Name, Tuple, List, Load, And, Or, Not, USub, UAdd, Add, Sub, Mult, Div, Mod
```

**禁止**（出现即 `RuleLoadError`，并给出节点类型与位置）：

```
Call, Attribute, Subscript, Lambda, Dict, Set, DictComp, SetComp, ListComp,
GeneratorExp, IfExp, Starred, Await, Yield, JoinedStr(formatted string), NamedExpr,
comprehension, 以及任何 dunder 名称（NAME 以 "__" 开头或结尾）
```

> **禁止 `Call`** 意味着表达式中不存在函数调用：没有 `abs()`、没有 `len()`。
> 这是刻意的（ADR 见 DECISIONS ADR-014）：白名单里少一个入口，攻击面少一整类。

#### 8.3.3 求值契约

```python
@dataclass(frozen=True)
class CompiledExpr:
    source: str
    names: frozenset[str]
    def evaluate(self, namespace: Mapping[str, Any]) -> bool: ...

def compile_expression(src: str, allowed_names: AbstractSet[str]) -> CompiledExpr: ...
def evaluate(expr: CompiledExpr, namespace: Mapping[str, Any]) -> bool: ...
```

- **加载期**（`load_rules`）必须校验：语法（`ast.parse(mode="eval")`）、AST 白名单、**所有 `Name` 都在 `allowed_names` 内**。任一失败 → `RuleLoadError`（带 `rule_id`、`position`、`message`）。**不允许**延迟到运行期才发现。
- `allowed_names`（命名空间，冻结）：

  ```
  gmv_cents, order_count, paid_order_count, refund_order_count, refund_amount_cents,
  aov_cents, refund_rate, uv, cvr, gmv_7d_avg_cents,
  gross_margin, cost_coverage_ratio,          # v1.5.0：**店铺级**成本口径（已实现，§5.3）
  gmv_delta_ratio, order_delta_ratio, refund_rate_delta,
  stock, current_price_cents, stock_days, is_on_sale,
  sku_gross_margin, cost_cents, sales_7d, avg_daily_sales_7d,
  ad_spend_cents_7d, ad_order_count_7d
  ```

  多一个名字都要走契约变更流程。

  **两个名字集合，互斥（v1.4.0 冻结；v1.5.0 更新 `gross_margin` 的归属）**：

  | 集合 | 名字 | 求值轮次 |
  |---|---|---|
  | **店铺级**（12 + 3） | 上面第 1–4 行共 15 个（12 个指标名 `gmv_cents` … `gmv_7d_avg_cents` + `gross_margin` / `cost_coverage_ratio`，再加 3 个派生量 `gmv_delta_ratio` / `order_delta_ratio` / `refund_rate_delta`） | 店铺轮，`Anomaly.sku_id=None` |
  | **SKU 级**（10） | `stock`、`current_price_cents`、`stock_days`、`is_on_sale`、**`sku_gross_margin`**、`cost_cents`、`sales_7d`、`avg_daily_sales_7d`、`ad_spend_cents_7d`、`ad_order_count_7d` | 每个 SKU 一轮，`Anomaly.sku_id=该 SKU` |

  两个集合**必须互斥**：`load_rules` 要断言 `SHOP_NAMES ∩ SKU_NAMES == ∅`（常量集合写死在 `services/rules.py`，`RuleLoadError` 兜底）。互斥是**两轮判定唯一性**的前提——规则引用集合决定轮次，若一个名字同时属于两边，轮次就有歧义（§5.4）。

  **v1.5.0 实现状态**：`services/rules.py` 里已有 `SHOP_NAMES`（15）/ `SKU_NAMES`（按本节清单写全 10 个，含未实现的 6 个）/ `ALLOWED_NAMES = SHOP_NAMES ∪ 已实现的 4 个 SKU 名字`，并有 `assert_name_sets_disjoint()`：**import 期就验一次**（写错常量属代码缺陷，不该等到某个店的配置加载才暴露），`load_rules` 里再调一次（契约要求的加载期门禁）。报错带**交集名字**并指向本节与 ADR-021。

  **命名硬规则（v1.5.0 冻结，ADR-021 A 条，违反即 `RuleLoadError`）**：`gross_margin` **只属于店铺级**（全店有成本覆盖的加权毛利率，§5.3）；**SKU 轮次落地时，SKU 侧的毛利率必须命名为 `sku_gross_margin`**。这条规则没有"建议"成分，落地形态写死三条：

  1. `SHOP_NAMES` 里含 `gross_margin` / `cost_coverage_ratio`；`SKU_NAMES` 里含 `sku_gross_margin`、**不得**含 `gross_margin`。
  2. "两集合互斥"断言一旦触发，错误信息**必须**是明确可读的（带两个集合的交集名字），并**必须**指向本条（`§8.3.3 命名硬规则 / ADR-021`）——**不允许**静默失败、不允许只抛一个裸 `AssertionError`、不允许"取其一"式自动改名。
  3. `rules.yaml` 里 SKU 级规则写 `gross_margin` 时，加载期按"店铺级名字用在了 SKU 轮"报错，而不是当成店铺级规则悄悄求值。
  4. **改名范围是全文级的**：正文里所有 **SKU 级** `gross_margin` 的表述——§3.3 `SkuSnapshot.gross_margin`、§5.3.1 的公式表、§5.4 的"`gross_margin is None` 的 SKU"、§10.3 的 `test_sku_metrics.py` 断言——在 SKU 轮次落地时**一律**改成 `sku_gross_margin`。本节只立规则、不改那些逐字块；改动它们必须与 SKU 轮次的代码在同一次提交里完成。

  **理由（同名不同含义的代价）**：两个 `gross_margin` 的**分母不同、精度不同、用途也不同**——店铺级是"有成本成交额的加权毛利率"（一个数代表一部分成交），SKU 级是"单件售价与单件成本之差"（一个数代表一件货）。同名会让规则作者**无意中把店铺级阈值用到单品上**：一条为全店调的 `gross_margin < 0.0`，套到单品上会把"薄利"直接判成"亏本"，而这条规则一旦误报，Agent 就会去给本来正常的商品改价/下架——本仓最贵的一类误报。**同名不同义不是风格问题，是让阈值悄悄改变含义的问题。**

- **求值上下文（v1.4.0；v1.5.0 更正店铺级名字计数）**：
  - 店铺轮命名空间 = **15** 个店铺级名字（12 个指标名 + 3 个派生量）。**原文标称"12 + 3 = 15"，但当时实际只列了 13 个名字**（第 1、2 行是 10 个指标名，不是 12 个）——v1.5.0 加进 `gross_margin` / `cost_coverage_ratio` 两个名字之后，"12 + 3 = 15"才真正成立。
  - SKU 轮命名空间 = **15 个店铺级名字 ∪ 该 SKU 的 10 个名字**（共 **25** 个键）。因此 SKU 级规则可以写 `"sku_gross_margin < 0.05 and gmv_delta_ratio < -0.2"`——"这个 SKU 亏本 **且** 店铺在掉量"这种跨层条件必须能写，否则运营只能拆两条规则、又无法表达"同时"。
  - **SKU 轮里两个成本名字同时存在，含义不同（v1.5.0）**：`gross_margin` = **店铺级**加权毛利率（全店一个数），`sku_gross_margin` = **该 SKU** 的毛利率（每个 SKU 不同）。这正是命名硬规则要防的歧义——写 `"gross_margin < 0.0"` 的 SKU 级规则，在 N 个 SKU 上会得到**同一个**店铺级结果（要么全命中要么全不命中），而作者以为在逐件判断。
  - 命名空间里**每个名字都必须存在**（值为 `None` 也算存在）；缺键 → `RuleEvalError`。
- **运行期**：
  - `Name` 不在 `namespace`（例如 `gmv_7d_avg_cents` 为 `None` 是值缺失，但名字必须在）→ `RuleEvalError`。
  - **数值比较任一侧为 `None` → 该比较结果为 `False`**（**不是**异常，**不是** `True`）；但 `is None` / `is not None` 正常工作。**这是"数据不足，正常未命中"，不进 skipped 记录**（§5.4 已冻结两者分开）。典型例子：`cost_cents` / `sku_gross_margin` 为 `None` 的 SKU，`"sku_gross_margin < 0.05"` 求值为 `False`，规则**不触发**——但**绝不允许**有人先把 `None` 换成 `0` 再比较（那会让"没配成本的 SKU"被判成亏本并触发改价，§8.5）。同理 `gross_margin`（店铺级）为 `None` 时，`"gross_margin < 0.0"` 为 `False`，`loss_making` 不触发。
  - `bool` **不是** `int`：`True < 2` 这种写法在求值时不报错但没有业务意义；实现要显式拒绝"布尔参与算术"→ `RuleEvalError`。
  - 除以 0 / 取模 0 → `RuleEvalError`（由调用方跳过该规则并记录）。
  - 表达式结果**必须是 `bool`**；得到数字/字符串 → `RuleEvalError`。
  - NaN/Inf 参与比较 → `RuleEvalError`（避免"静默不触发"）。
- **绝不使用 `eval` / `exec` / `compile` / `ast.literal_eval`**。求值器是手写的 AST 解释器（递归下降即可），`ast.parse` 只用于建 AST。

### 8.4 `.env.example`（环境变量名单，冻结名字）

```
DOUDIAN_APP_KEY=
DOUDIAN_APP_SECRET=
DOUDIAN_ACCESS_TOKEN=
DOUDIAN_SHOP_ID=12345678
FEISHU_WEBHOOK_URL=
DATABASE_URL=sqlite+pysqlite:///./data/doudian.db
CHECKPOINT_TYPE=sqlite
CHECKPOINT_PATH=data/checkpoint.db
CHECKPOINT_URL=
LOG_LEVEL=INFO
```

环境变量名与 yaml 字段的映射由 `Settings` 显式声明（`DATABASE_URL` → `database.url` 等），**不搞**自动前缀推导歧义。

### 8.5 `config/sku_costs.yaml`（SKU 成本表，v1.4.0 入册 / **v1.5.0 已实现**，冻结）

```yaml
version: "1"
effective_from: "2026-10-23"   # 可选：YYYY-MM-DD 或完整 ISO-8601；只做校验与展示，**不参与计算**
# sku_id -> 成本价（整数分）。没有条目 = 成本未知，不是 0。
costs:
  "3400000000000000002": 8200
  "3400000000000000003": 4500
```

| 规则 | 规定 |
|---|---|
| 归属 | **agent-core**（`backend/config/` 与 `settings.yaml`/`rules.yaml` 同组，§0.1）；加载器是 `app/services/costs.py` |
| 顶层 | **只允许** `version` / `effective_from` / `costs` 三个键，多一个都是 typo → `ConfigError`（写成 `cost:` 而被静默忽略，会让成本表"看起来没生效"）。`costs` **必需**；"没有成本"的正确写法是**删掉整个文件**，**不是**写一个空值把它伪装成"配置过但没内容" |
| 键 | `sku_id` 字符串，必须匹配 `^[A-Za-z0-9_-]{1,64}$`（§0 字符集）。写成整数 `3400…` 也拒收——上游 `raw.products[i]["sku_id"]` 是**字符串**，类型不一致会让 `cost_table.get()` 永远取不到 |
| 值 | `int` 且 `> 0`（整数分）。`bool` **不算** `int`；float / 字符串 / `None`（YAML 里 `"sku":` 后面什么都不写）/ `0` / 负数 → **全部 `ConfigError`** |
| 重复键 | **`ConfigError`**。`yaml.safe_load` 对重复键**静默取后者**——手写的成本表里同一个 SKU 出现两次时，前一行（很可能是财务核对过的正确值）被无声覆盖，而这类错误在量小的配置里没人会发现。所以加载器**自己检测**（`_UniqueKeyLoader`）。一个 Python `dict` 构造完成时重复键已经被合并、"有没有重复"这个信息就不存在了，因此这一项**只可能**在 YAML 加载层做 |
| 编码 | **非 UTF-8**（例如财务把表另存成 GBK）→ `ConfigError`。**绝不**"尽力而为"地读一半：读错的成本会被当真成本参与"是否亏本"的判断，比没有成本危险得多 |
| 文件缺失 | **不是错误**：记一条结构化 `warn`（`event="config.sku_costs_missing"`）后视为**空表** → `gross_margin = None`、`loss_making` 不触发。缺失是"这个商家还没配成本"（正常状态），内容错误是"有人写坏了"（必须当场失败）——两者都当错误会让第一次部署起不来，都当正常会让 typo 静默生效 |
| 缺条目（某个 SKU 没写） | 该 SKU 视为**成本未知**：SKU 级 `cost_cents = None`（§5.3.1）；店铺级口径里它**既不进分子也不进分母**（§5.3），因此覆盖率下降——这正是 `cost_coverage_ratio` 要暴露的事。**绝不填 0、绝不猜** |
| 与规则的关系 | `gross_margin` 为 `None` 时，数值比较为 `False` → 规则**不触发**，且**不进 skipped**（§5.4/§8.3.3）。所以"没配成本"的后果是**不报警**，而不是**被判亏本**——前者是漏报（可见、可补配置），后者是误报（会让 Agent 去改价/下架） |
| 加载时机 | `app.main` lifespan 读取 → 注入 `compute_metrics(cost_table=...)`（§4.8 装配点 + §5.3 参数）。热更新不支持：改文件要重启（成本变化的审计价值 > 免重启的便利）；`get_sku_costs()` 是进程级缓存，缓存而**不热重载**是为了让"哪次 run 用的是哪版成本"有确定的边界 |

**成本键能否在 `raw.products` 里对上：改成运行期审计（v1.5.0 改写，ADR-021 C 条）**

原条文写的是"对不上的条目 → **启动期** `ConfigError`"。这条**物理上做不到**：启动期（`app.main` lifespan）根本没有 `raw`——商品列表来自一次上游调用（`collect`），lifespan 只加载配置、不发业务请求。要在启动期做静态交叉校验，只能把上游数据搬进启动流程，那等于让**进程启动依赖抖店可用性**。

改写为：**运行期在 `compute_metrics` 节点做审计**。判据与处置写死：

- **能判断时**（本次 `raw.products` 非空）→ 记一条 `{"event": "sku_costs.unmatched", "data": {"sku_ids": [...], "count": n}}`（审计条目，随 run 落 `audit`，可在 `/api/logs` 看到）。**只记不阻断**：一条过期的成本配置不该让当天所有指标都算不出来——阻断 = 一个 typo 换掉全天看板，比漏一条提示贵得多。
- **商品列表为空时（采集降级 `partial=True`）→ 不报**：此时**每一条**成本都会"看起来对不上"，报出来是一串假警报，反而把真问题淹掉。
- 顺序上它追加在 `metrics.computed` 之后，所以同一轮 run 里"算了什么（`gross_margin` / `cost_coverage_ratio`）"与"哪些成本没对上"都能看到。

**为什么这个改法比原来的更好**：原条文把一条**静态可查性**当前置门禁，但它依赖的数据在那一刻不存在——一条永远无法执行的条款，最后一定变成"没人用的条款"（§8.3.3 的同一个教训）。而"过期成本配置"的真实后果是**漏报**（那件货不再有毛利率，`loss_making` 的覆盖率分子变小），不是**误报**（不会把好货判成亏本）。对漏报，正确的处置是**让它在日志里可见、让人去补配置**，不是让整条流水线停下来。

**三条写死的原因（逐条对应一次真实事故形态）**：
1. **抖店开放平台没有成本字段**。任何"从历史售价/采购单反推成本"的做法都是猜测；猜测直接决定"是否亏本"的判断结果，比没有这个指标更危险。
2. **缺失 ≠ 0**。把缺失当 0 成本时，覆盖部分的毛利额等于成交额 → `gross_margin = 1.0`（看起来暴利，规则永不触发，问题被藏起来）；而一旦有人写成**反方向**（缺成本时把该 SKU 当"成本 0 参与"，或干脆把 margin 当 0），`0 < 阈值` → 直接判亏本 → 触发改价/下架。**误报里最贵的一种**：Agent 主动把好货降价。所以缺失的唯一正确表达是 `None`，且 `None` 必须让规则**不触发**（§8.3.3）。
3. **成本不进 DB**（不做第 5 张表）。原因是双向的：①它是**商家自维护的配置**，随部署走、要能 code review，不是运行时业务数据；②§4.7 断言 B′ 冻结"`__tablename__` 恰好 4 行"，加表要动断言、动迁移、动 §6——为一个几乎不变的映射表付这个代价不值得。若将来要支持"按时间生效的成本"（成本会变），那时再提契约变更加 `sku_cost_history` 表，别现在猜。

---

## 9. 错误分类与处理动作（冻结）

### 9.1 两级分类：HTTP 层 + 业务层

抖店返回 **HTTP 200 + body `err_no != 0`** 是常态，所以**只看 HTTP 状态码必然漏判**。

| 分类 | 触发 | `retryable` | `ErrorCategory` | 处理动作 |
|---|---|---|---|---|
| 超时 | `httpx.TimeoutException` | ✅ | `timeout` | 非 mutating：退避重试；mutating：`AMBIGUOUS` |
| 连接错误 | `httpx.ConnectError` / `RemoteProtocolError` | ✅ | `connection` | 同上 |
| 上游 5xx | `500/502/503/504` | ✅ | `server_5xx` | 非 mutating 重试；mutating → `AMBIGUOUS` |
| 限流 | `429` 或 body `err_no` ∈ 限流码 | ✅ | `rate_limited` | 按 `Retry-After` 或指数退避重试；mutating 也重试 |
| 客户端 4xx | `400/401/403/404/413` 非签名/鉴权原因 | ❌ | `client_4xx` | 不重试；记 `failed`；把 `message` 上报 |
| 签名错误 | 签名相关 `err_no`（如 10000/10008 类）/ 本地签名配置缺失 | ❌ | `signature` | 不重试；**告警**（这是部署事故，不是业务问题）；不刷新 token |
| 鉴权错误 | token 失效/过期/无权限 | ❌ 当次 | `auth` | 强制刷新 token **一次**并重试；再失败 → 停止整个 run（`status=failed`） |
| 业务错误 | `err_no != 0` 且非上述 | ❌ | `business` | 不重试；该动作 `failed`；写 `error_message`；继续下一个动作 |
| 结果不确定 | mutating 调用遇到 timeout/connection/5xx | ❌ 当次 | `ambiguous` | `action_log.status="unknown"`；**不重试**；交给对账任务（§9.3） |
| 配置错误 | 缺 `app_secret`/非法 base_url/未知 sign_method | ❌ | `config` | 启动期或首次调用抛 `ConfigError`；不重试 |

### 9.2 重试预算（冻结）

- `max_attempts=4`（含首次），指数退避 `min(backoff_max_s, backoff_base_s * 2**n)` + 抖动 `random.uniform(0, 0.1*delay)`。
- `429` 且响应头有 `Retry-After`（秒）→ 用 `min(Retry-After, backoff_max_s)`。
- 单次 `call` 的总时间预算 = `settings.doudian.timeout` 各项之和 + 退避总和；超过 `settings.app.request_timeout_s` → 放弃并抛最后一个错误类别。
- 重试**不**重算签名以外的东西？**必须重算**：每次尝试生成新的 `timestamp` 与新 `sign`（时间戳过期会导致签名失效）。`param_json` 不变 → 签名不同只因为 timestamp。

### 9.3 结果对账（`ambiguous` 的唯一出路）

- 抖店没有服务端幂等键，**不可**靠重试保证"只执行一次"。
- 处置：`action_log.status="unknown"` → 调度器（与效果评估同一个 tick）取 `unknown` 且 `updated_at < now - 5min` 的行，**读**上游真实状态（`list_products`/`get_shop_info`）判断意图是否已生效：
  - 已生效 → `succeeded`（`response` 补 `{"reconciled": true}`）
  - 未生效 → 允许**一次**重试（`attempt+1`，上限 3）
  - 说不清 → 保持 `unknown`，`audit` 记 `action.reconcile_pending`，人工处理
- **禁止**：把 `unknown` 当 `succeeded`、把 `unknown` 自动重试超过 3 次。

---

## 10. 离线可测性（无真实凭据跑通全链路）

### 10.1 注入点（三选一，优先级从高到低）

1. `DoudianClient(transport=...)` — **主路径**。传 `httpx.AsyncBaseTransport` 实例，任何测试都不依赖 monkeypatch。
2. `create_doudian_client(settings)` — 当 `settings.doudian.dry_run=true` 时内部自动用 fixtures 构造 `MockTransport` 注入。**全链路 e2e 测试走这条**。
3. `respx` — 仅在**不传** `transport` 时可用（respx 通过 patch 默认 transport 生效）。**测试里禁止同时用 1/2 和 3**，混用会出现"谁先匹配谁赢"的不可重复结果。

### 10.2 fixtures 位置与格式

目录：`backend/tests/fixtures/doudian/`

- `signing_vectors.json` — §5.2 的黄金向量（**逐字**）。
- `cassettes/*.json` — 每个文件一个 cassette：

```json
{
  "name": "shop_info_ok",
  "match": {
    "method": "shop.getShopInfo",
    "body_contains": {}
  },
  "response": {
    "status": 200,
    "headers": {"x-tt-logid": "20261023143000ABCDEF"},
    "json": {
      "err_no": 0,
      "message": "success",
      "data": {"shop_id": "12345678", "shop_name": "示例旗舰店"}
    }
  },
  "delay_ms": 0
}
```

匹配规则（冻结）：

- `match.method`：与请求的 `method` 查询参数**全等**。
- `match.body_contains`：请求 body（JSON 解析后）的**子集**匹配（递归子集，`null` 值为"键必须存在且为 null"）。
- 多个 cassette 命中时**按文件名字典序**取第一个（确定性，不依赖注册顺序）。
- 无命中 → 返回 `{"status": 599, "json": {"err_no": -1, "message": "no cassette matched: <method>"}}`（让失败显式，不要静默 200）。

签名夹具：cassette 里的 `status/json` 与签名无关；但**每个 cassette 都要能被签名校验流程走通**——即 handler 必须忽略 `sign` 但记录它，测试可断言 `sign` 是 64 位小写 hex。

### 10.3 必需的测试（缺一条就不算离线可测）

| 测试文件 | 断言 |
|---|---|
| `test_signing.py` | 三个黄金向量逐一相等；`timestamp` 只生成一次（签名与 URL 用同一字符串）；未知 `sign_method` 抛错 |
| `test_doudian_client_transport.py` | 注入 `MockTransport` 全链路；5xx→重试 3 次后抛 `DoudianUpstreamError`；429+`Retry-After`→按头等待；`mutating=True` + timeout → `DoudianAmbiguousOutcomeError` |
| `test_rate_limit.py` | 假时钟下 20 次调用耗时不小于 `(20-burst)/rate_per_sec`；并发不超过 `max_concurrency` |
| `test_rules.py` | `eval`/`Call`/`Attribute`/`__import__`/`open` 全部被拒；未知字段加载期报错；语法错误报错带位置；除零只跳该规则 |
| `test_db_portability.py` | 四张表在 SQLite 上建表成功；`to_utc` 往返；`UNIQUE` 冲突能被捕获并重查（不抛未捕获异常）；`CHECKPOINT_TYPE=postgres` 时仅做 URL 校验（不连库） |
| `test_hitl_roundtrip.py` | `run_agent` → `awaiting_approval`；`resume_agent` 少一条/多一条/重复 action_id 全部 400；正确恢复后 `status=done` + `action_log` 有行 |
| `test_approval_replay_idempotent.py` | 同一 `approval_gate` 重放两次，`approval_request` 行数不变、`status` 不被重置 |
| `test_idempotency_key.py` | 审批改价（9900→9600）产生的 `idempotency_key` 与改前不同；同参数重复执行第二次 `skipped_duplicate` |
| `test_effects_scheduler.py` | 图内不 sleep：`record_effect` 用时 < 1s 且 `effect_snapshot` 有 4 行 `scheduled`；调度器把 `due_at<=now` 的置 `evaluated` |
| `test_attribution_wording.py` | 非 A/B 的 `conclusion` 不含因果断言词（§5.9.3） |
| `test_trace_invariant.py` | 跑完一次带审批的 run 后 `count(event=="node.trace") == agent_traces.step_count`（§4.7 不变量）；`approval_gate` 重放后计数为 9 而非 8；人为把 DB 计数改小后 `reconcile_step_count` 能校准回来；**挂起那次事件 `data.outcome=="interrupted"` 且 `level=="info"`、`data.error is None`**（不得是 `error`）；`collect` 抛 `DoudianUpstreamError` 时 `outcome=="error"` 且 `level=="error"`；`GraphRecursionError` 归 `error` 不归 `interrupted` |
| `test_api_contract.py` | 5 个端点的成功/400/404/409/502 全绿；错误包络字段齐全；响应头有 `X-Trace-Id` |
| `test_platform_redaction_channel.py` | **通道级**脱敏断言（v1.3.3，§7.6.2；与 `test_platform_redaction_scrub.py` 是一对）：`JsonlLogSink.write` 的 `message` 带 `access_token=SECRET-abc123` → **落盘文件里查不到该串**；`data` 的字符串值同样；`data={"access_token": SECRET}` 的**键名层**仍然命中（值级层不能取代它）；同目录的 `test_platform_redaction_scrub.py` 钉住函数级对抗——复合键 `refresh_token`/`client_secret`、`Authorization: Bearer xxx` 整段吞（不只遮 `Bearer`）、`{"token":{"v":1}}` 遮成 `"***"` 后仍是合法 JSON、`design=1`/`sign_method=` 不误伤。**反向验证**：删掉 `write()` 里的 scrub → 前三条必须变红 |
| `test_static_invariants.py` | 用 subprocess 跑 §4.7 三条 grep 断言并比对期望：A 只命中 `tracing.py`、B′ 恰好 4 行全在 `models.py`、C `class AgentRuntime` 恰好 1 行；**断言按"赋值/增量/定义"形态匹配，不按名字出现次数**（否则 `nodes.py` 的 `import reconcile_step_count` 会假阳性，见 §4.7 断言取向）；另断言 `adapters.py` 内无 `insert_`/`update_`/`select(`/`session` 等数据访问（§4.8 禁令 1）；**v1.4.0 追加**：`grep -rn "cost_cents or 0\|gross_margin or 0" backend/app` 为空（§8.5） |
| `test_sku_metrics.py` | `raw.products` 3 条 → `metrics.skus` 恰好 3 条且按 `sku_id` 升序；**无成本配置的 SKU：`cost_cents is None` 且 `gross_margin is None`（不得是 0 或 1.0）**；`current_price_cents=0` → `gross_margin is None`；`avg_daily_sales_7d=0` → `stock_days is None`（不是 0/inf）；`sales_7d=0` → `avg_daily_sales_7d=0.0`（真值）；`history` 元素的 `skus` 恒为 `[]`；`grep "cost_cents or 0"` 为空（§5.3.1/§8.5） |
| `test_sku_anomalies.py` | 一条 SKU 级规则命中 3 个 SKU → 产出 3 条 `Anomaly`，`sku_id` 各不相同、**`anomaly_id` 互不相同**（含 `sku_id` 的哈希）；店铺级规则 → `sku_id is None` 且 `anomaly_id` 与 v1.3.0 公式一致；`gross_margin is None` 的 SKU：规则不触发、**不进 skipped**、记 `none_operand`；除零规则记 `rule_eval_error`（两者**分开**）；`SHOP_NAMES ∩ SKU_NAMES == ∅` 断言；SKU 级提议的 `target == anomaly.sku_id` 且 `evidence.sku_id == anomaly.sku_id`（§5.4/§8.3.3/§7.3） |

`pytest.ini`：

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
addopts = -q --strict-markers
filterwarnings =
    error::DeprecationWarning:app.*
```

### 10.4 全链路离线跑法（README 里要写这一条）

```powershell
$env:DOUDIAN_APP_KEY="dummy"; $env:DOUDIAN_APP_SECRET="dummy"
G:\SN\doudian-agent\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
# 另开一个：POST /api/agent/run {"shop_id":"12345678"} -> 拿 thread_id/interrupt_id -> POST /api/approve
```

`dry_run=true` + fixtures 即可完成"采集→指标→异常→审批→执行→效果登记"全链路，**零网络、零凭据**。

---

## 11. 三方对齐自检清单（各自开工前过一遍）

**agent-core**
- [ ] `AgentState` 字段名逐个照抄 §3.3，order 不重要，**名字必须一致**
- [ ] 节点函数名/签名照抄 §4.3；只有 8 个节点
- [ ] **8 个节点全部用 `traced()` 包起来**，且 `step_count` 只由 `tracing.py` 写（§4.7）
- [ ] `interrupt` payload 与恢复 payload 形状照抄 §4.4
- [ ] 恢复用 `{interrupt_id: payload}`，不用 list
- [ ] `approval_request` 的 upsert 是 insert-if-absent
- [ ] 图内不等待 T+N，只登记 `effect_snapshot`
- [ ] `traced()` 的异常分流按 §4.7 规则 5 的**顺序**写：先 `except GraphBubbleUp`（→`outcome="interrupted"`/`error=null`/`level="info"`/原样 re-raise），再 `except Exception`（→`outcome="error"`/`level="error"`/re-raise）；捕的是**基类** `GraphBubbleUp`，不是只捕 `GraphInterrupt`；`step_count` 照旧 +1（计数不看 `outcome`，ADR-016）
- [ ] `app/agent/adapters.py` 只做装配与解析（§4.8）：**不含任何表仓储**、不写 `step_count`、不含业务逻辑；`class AgentRuntime` 全仓**恰好 1 处**且在 `adapters.py`；节点取依赖一律走 `resolve_*`，不在节点体内现建 client/settings/sink
- [ ] §4.7 的三条断言本地跑过且符合期望：A（`step_count` 赋值/增量只命中 `tracing.py`）、B′（`__tablename__` 恰好 4 行全在 `models.py`）、C（`class AgentRuntime` 恰好 1 行）
- [ ] **v1.4.0**：`detect_anomalies` 按 §5.4 写**两轮**（店铺轮 + 每 SKU 一轮），轮次由「`when` 引用的名字是否含 SKU 级名字」判定，**不自创 `scope` 参数**（§1.1）；`anomaly_id` 含 `sku_id`（`sha256(f"{run_id}|{rule_id}|{metric}|{sku_id or ''}")[:16]`）；`Anomaly.sku_id` 贯通到 `ActionProposal.target` 与 `evidence.sku_id`
- [ ] **v1.4.0**：`SHOP_NAMES` / `SKU_NAMES` 两个常量集合在 `services/rules.py` 里且**互斥**（`load_rules` 断言）；`None` 参与比较 → `False` 且**不进 skipped**；真错误 → `RuleEvalError` 且**进 skipped**（两者分开记，§5.4）
- [ ] **v1.4.0**：`config/sku_costs.yaml`（§8.5）由 agent-core 维护；缺条目 → `cost_cents=None`，**绝不填 0**；`grep -rn "cost_cents or 0\|gross_margin or 0" backend/app` 为空

**platform**
- [ ] ORM 列名/类型/唯一约束照抄 §6.1
- [ ] 5 个路由的路径、query 参数名、响应键名照抄 §7
- [ ] 错误包络与 9 个错误码照抄 §7.1
- [ ] `DoudianClient` 关键参数名（`mutating`、`transport`、`rate_limit`）照抄 §5.1
- [ ] **v1.4.0**：`DoudianClient.__init__` 加 `redact_keys` 一个 **keyword-only + 默认值**参数（§5.1）——**已实现**；同批入册的 `total_budget_s` **仍未实现**（§13(m)），`create_doudian_client(settings)` 目前只显式传 `settings.app.request_timeout_s` 与 `settings.observability.redact_keys`；`total_budget_s` 是**绝对截止**（从第一个请求起算），不是每尝试重新计时
- [ ] **v1.4.0**：`compute_metrics(..., cost_table=None)` 产出 `skus`（§5.3.1），**纯函数不读文件**；`cost_table` 由 lifespan 读 `config/sku_costs.yaml` 后注入；`history` 元素的 `skus` 恒为 `[]`（§3.3）
- [ ] 签名实现与三个黄金向量一致
- [ ] 只用 `sa.JSON`、`String(n)`、`with_variant` 主键、Python 侧时间默认值
- [ ] `JsonlLogSink` 支持 `event="node.trace"` 的形状（§7.6.1）；**不写** `agent_traces.step_count`（那是 agent-core 的 `tracing.py`）
- [ ] `/api/logs` 原样透传 `data.outcome`，**不改写、不补默认值、不据此重算 `level`**；`level` 是写入端按 §7.6.1 硬规则给出的派生值，服务端只做过滤（ADR-016）
- [ ] lifespan 负责装配（§4.8 落地要求）：`set_runtime(build_runtime(settings))` + `set_log_sink(...)`/`refresh_log_sink()`，shutdown 关闭 `runtime.client` 与 notifier 的 HTTP 客户端；**不得**另建 `AgentRuntime` 定义或第二份单例（§4.7 断言 C）
- [ ] **v1.4.0**：`/api/metrics` **不加** `skus`（§7.5）；`/api/alerts` 的 `evidence` 带 `sku_id`（§7.3）

**frontend**
- [ ] `src/api/types.ts` 照抄 §12（TS 类型与 §7 的 JSON 键一一对应，字面量联合类型用枚举字符串）
- [ ] 只请求 `/api/*`，dev 用 vite proxy
- [ ] 错误按 `error.code` 分支，不按 HTTP 状态猜
- [ ] 执行日志页**按 `data.outcome` 分支**：`ok`/`interrupted` 中性（`interrupted` 文案"等待审批"），`error` 才红；**不按 `level` 分色、不按 `data.error` 是否非空判故障**（§7.6.1、ADR-016）；缺 `outcome` 的旧事件按 `"ok"` 读，未知值原样显示
- [ ] 金额字段一律 `*_cents`，展示时 `/100` 并保留两位
- [ ] 审批提交时 `verdict=edit` 必须带 `edited_params`，`reject` 必须 `null`
- [ ] **v1.4.0 无必改项**：§12 净改动为零（`MetricPoint` 无 `skus`、无 `Anomaly` TS 类型）。可选增强：审批卡片读 `evidence["sku_id"]` 显示"哪件货"（§7.3/§12）

---

## 12. 前端 TS 类型（逐字，`frontend/src/api/types.ts`）

> **本节代码块是逐字冻结块**：`frontend/scripts/verify.mjs` 会把本节**第一个 ts 围栏块**（即下面那个块，本节正文里不许再出现三个反引号 + ts 的围栏标记——它会把抽取位置提前到正文中间，v1.5.0 修过一次）抽出来与 `src/api/types.ts` **逐行 diff**（不多、不少、不重排）。因此块内**只允许放类型定义**，任何说明、注释、版本标注都写在块外——块内多一行注释就等于下游构建失败（v1.4.0 就是这么把 frontend 验收跑红的，见 §0.2）。
> **v1.4.0 对 §12 的净改动为零**：`MetricPoint` 没有 `skus`（`/api/metrics` 仍是店铺级，§7.5）；也**没有** `Anomaly` 的 TS 接口，因为 `Anomaly` 从不出现在 HTTP 响应里（§7.3）——SKU 级信息走 `PendingApproval.target`（= `sku_id`）与 `evidence.sku_id`。可选增强（不改类型）：审批卡片读 `evidence["sku_id"]` 显示"哪件货"。
> **v1.5.0 对 §12 的净改动 = `MetricPoint` 加两行可选字段**：`gross_margin?: number | null` 与 `cost_coverage_ratio?: number | null`，与 §7.5 的同名契约字段一一对应（店铺级成本口径，默认 `null`）。**两处必须同一次改完**：`frontend/src/api/types.ts` 与本节逐字块——只改一边会让 `verify.mjs` 的逐行 diff 立刻变红（v1.4.0 就是这么红的，§0.2）。看板取值在 `frontend/src/lib/metrics.ts`：**直接读后端这两个字段**，不再用契约外扩展字段 `cost_cents` 自算（旧自算口径已删除，见 §7.5 的成本口径说明）。

```ts
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
  gross_margin?: number | null;
  cost_coverage_ratio?: number | null;
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
```

**`LogItem.data` 保持 `Record<string, unknown>`（v1.2.0 不新增顶层字段）**：`node.trace` 的三态 `outcome` 在 `data` 里（§7.6.1），因此 §12 的类型**不需要改**。前端读取规则：`const outcome = row.data["outcome"]`，缺键按 `"ok"`、未知值原样显示（§7.6.1 读取端兼容规则、ADR-016）。不要为此把 `data` 改成联合类型或给 `LogItem` 加 `outcome?: string` 顶层字段——那会与 §7.6 的响应形状分叉。

---

## 13. 变更记录

| 版本 | 日期 | 变更 | 影响方 |
|---|---|---|---|
| 1.0.0 | 2026-10-23 | 首版冻结 | 全部 |
| 1.1.0 | 2026-10-23 | ①§0.1 新增**归属表**（7 个角色的写权限边界 + 3 条边界硬规则，含 `docs/` 下三份文件的例外归属）；②新增 **§4.7 节点 traced 包装器**（`app/agent/tracing.py`、`NODE_TRACE_EVENT`、`reconcile_step_count`）与不变量「JSONL `node.trace` 事件数 == `agent_traces.step_count`」；③新增 **§7.6.1** `node.trace` 事件形状；④§6.1 `agent_traces` 补「一次 run 一行摘要、节点级 trace 不进表」定位与交叉引用；⑤§10.3 增 `test_trace_invariant.py`；⑥§11 自检清单增 `traced()`/`JsonlLogSink` 两项；⑦DECISIONS 新增 **ADR-015** | 全部（agent-core：新增 `tracing.py` 与包装责任；platform：`JsonlLogSink` 需承载 `node.trace` 形状且不得写 `step_count`；devops/qa/reviewer：新增归属条目） |
| 1.2.0 | 2026-10-23 | ①§0.1 补 **`docs/` 归属重叠裁定表**（CONTRACT/DECISIONS/ENV 归 spec-lead；RUNBOOK/VERIFY/REVIEW 归 devops/qa/reviewer，spec-lead 不代写）；②**§4.7 规则 5 异常分流**：`node.trace` 的 `data` 新增三态 **`outcome`**（`ok`/`interrupted`/`error`），先 `except GraphBubbleUp`（`interrupted`+`info`+`error=null`+原样 re-raise）后 `except Exception`（`error`+`level=error`+re-raise），`step_count` 不变量不变；③§4.3 规则 2/4 明确"节点体内不得捕获、`traced()` 是唯一观察点"且 `GraphBubbleUp` 不算失败；④**§7.6.1** 补三种 outcome 的事件样例、`outcome↔level` 硬规则、读取端兼容规则（缺键按 `ok`）与前端按 `outcome` 分支约定；⑤**§11** 增 agent-core 的 outcome 分流项、platform 的 `outcome` 透传项，并强化 frontend 的「按 `outcome` 分支」项；⑥§12 注明 `LogItem.data` 保持 `Record<string, unknown>`、不需改类型；⑦§10.3 `test_trace_invariant.py` 增 outcome 断言；⑧DECISIONS 新增 **ADR-016**（控制流中断 ≠ 失败）并在附 A 记 3 条被否方案 | 全部（agent-core：`tracing.py` 分流顺序 + 捕基类 `GraphBubbleUp`；platform：`JsonlLogSink`/`/api/logs` 原样透传 `outcome`、不重算 `level`；frontend：日志页改按 `outcome` 分支；qa：`test_trace_invariant.py` 补 outcome 断言；reviewer：新增可查项） |
| 1.3.0 | 2026-10-23 | ①**§0.1 目录树补 `app/agent/adapters.py`**（归属表记给 agent-core）：树里没有的文件没有归属，就没有"只有一份"的约束——`AgentRuntime` 已因此在 `adapters.py`/`nodes.py` 各长出一份；②**新增 §4.8 装配层**：写死 `adapters.py` 只做进程级运行时装配与依赖解析（`AgentRuntime`/`build_runtime`/`get_runtime`/`set_runtime`/`reset_runtime`/`resolve_runtime`/`resolve_client`/`resolve_now`/`digest_of`/log sink），冻结 `__all__`、`AgentRuntime` 形状、`config["configurable"]` 注入键，禁令 5 条（禁仓储/禁表写入/禁 `step_count`/禁第二处 `AgentRuntime`/节点禁自建依赖）；③§4.3 增规则 5：节点依赖只能经 `adapters.py` 解析；④**§4.7 机械断言改写**：废掉「`grep "step_count"` 只命中两文件」（与「`reconcile_step_count` 在 `finalize` 前调用、`finalize` 在 `nodes.py`」互斥，永不可能通过，且唯一"过"的修法是挪走正确逻辑），改为 A（写入点唯一）/B′（表定义唯一）/C（单例唯一）三条 + 断言取向（只管定义与写入，不管提及次数）+「机器能证的 vs 只能人审的」分开写 + 证据时效性（mtime 静止后实测并记时间）；⑤§1 增 `app.agent.adapters` 导入行；⑥§10.3 增 `test_static_invariants.py`；⑦§11 增 agent-core 的 adapters 禁令项与三条断言项、platform 的 lifespan 装配项；⑧DECISIONS 新增 **ADR-017**（装配层与仓储层必须分文件）并在附 A 记 4 条被否方案，同步修正 ADR-015 里两处旧的 grep 形态 | 全部（agent-core：`adapters.py` 定性 + 删掉重复的 `AgentRuntime` 与 `agent_traces` 仓储；platform：lifespan 负责 `set_runtime`/`set_log_sink` 且不得另建单例；qa：新增 `test_static_invariants.py`；reviewer：新增机械检查项与"只能人审"的边界清单） |
| 1.3.1 | 2026-10-24 | ①**§4.7 规则 5 补 c2 分支**：`node.trace` 的 `outcome="error"` 新增第二个来源——节点**正常返回但把 run 从非 `failed` 推进到 `failed`**（§4.2 的 `collect --(fail)--> finalize` 这类**正常边**），`level="error"`、`output_digest=None`、`data.error={"type":"NodeDeclaredFailure","message":<节点本次 update 里最后一条 AgentError.message，过 `redact_keys`>}`；判据是**状态迁移**而不是"返回值里带不带 errors"：已在 `failed` 的 run 上收尾（如 `finalize` 复述 `status=failed`）不得被判成第二个 error；②**§7.6.1** 补 `error.type` 取值说明（异常类名 **或** 固定标记 `NodeDeclaredFailure`），键集合仍是 `{type,message}`、读取端与前端无需改；③§4.7 常量块补 `NODE_DECLARED_FAILURE_TYPE`；④DECISIONS 新增 **ADR-019**（为什么用"状态迁移"而不是"让节点抛异常"）并在附 A 记 2 条被否方案 | 全部（agent-core：`tracing.py` 认下显式失败信号；qa：`test_node_semantics.py::test_non_retryable_collect_error_is_not_retried` 钉住 `outcome=error`/`error.type=NodeDeclaredFailure`/`finalize` 不染红；platform 与 frontend：**无需改动**，`outcome`/`error` 原样透传与展示即可；reviewer：新增"失败有两条来源"的可查项） |
| 1.3.2 | 2026-10-24 | ①**§0.1 登记 `app/api/queries.py`**（归属 API 层 / platform）并冻结边界：**只读**（仅 `SELECT`）、禁 `INSERT/UPDATE/DELETE`、禁触达 `step_count`、禁绕过 `tracing.py` 的仓储写 run 摘要、禁业务判断；登记理由是两条**冻结签名覆盖不到的读路径**（§5.8 的 `list_by_status` 不含行 `id` ↔ §7.3 的 `next_cursor`；§4.7 的 run 仓储只按 `(shop_id, run_id)` 取单行 ↔ §7.5 的 `/api/metrics` 需要 run 枚举）；②**§5.8 / §7.3** 补读路径说明：列表/游标允许经 `queries.py` 做 keyset 分页（`created_at DESC, id DESC` + `id < before_id`），**写路径不变**；③**§7.5** 补数据源说明：数据源是 checkpoint state（契约没有指标表），缺数据 `404`、**不补零**；④**§5.3** 补 `uv` 的来源与作用域判据（**公式未改**）：`collect` 只在能确认"这段汇总就是当天"时写 `order_summary["uv"]`，否则 `None`（不摊派、不补零），`uv` 有值但非非负整数 → `ValueError` | 全部（agent-core：`nodes.py` 的 `collect` 写 `order_summary.uv`；platform：`queries.py` 的只读边界 + `/api/metrics` 的 checkpoint 数据源；qa：`test_collect_uv.py` 钉住"有 uv / 无 uv / 作用域不明 / 声明作用域 / 非法值"五条边界；frontend：**无需改动**，`uv`/`cvr` 仍可为 `null`；reviewer：新增 `queries.py` 的两条 grep 可查项） |
| 1.3.3 | 2026-10-24 | **（已并入 1.4.0：本行保留作留痕，内容不删不改，不再单独计版本；当前版本见本表末行 1.4.0）** ①**新增 §7.6.2「脱敏的两层与边界」**：**键名层**（`redact_keys` 命中的 dict 键名，整个值递归替换为 `"***"`）与**值级层**（自由文本里 `key=value` / `key: value` / `"key":"value"` 的**值**替换，键集 = `DEFAULT_SCRUB_KEYS` ∪ `redact_keys`）**在同一次递归里完成**，统一入口 `redact_any`（`redact` 降为**兼容别名**）；写明"两层为什么都要"（键名层对 `str` 是 no-op，而消息原文恰好写在 `message` 里）、两种分隔符与五种值形态、`_`/`-` 互通、`:` 形态裸值的上界、**已知不覆盖 5 条**、`stderr` 经 `_ScrubbingFormatter` 先 format 再 scrub；②**§10.3 新增通道级测试要求**（`test_platform_redaction_channel.py`，含**反向验证**：删掉 `write()` 里的 scrub 后必须变红）；③§7.6 原有的脱敏条目**补全**（补 `cookie`、补"值级层也要"、加 §7.6.2 指针）；④DECISIONS 新增 **ADR-020**（脱敏必须两层，且必须测通道），记 5 条被否方案并在附 A 补 4 行 | platform（`observability/logging.py`：两层同调、`write()` 对 `message`/`data` 走统一入口、`stderr` 走 `_ScrubbingFormatter`）；qa（通道级 + 反向验证两份用例）；agent-core 与 frontend：**无需改动**——JSONL 顶层键、`/api/logs` 响应形状、§12 的 `LogItem` 一字未动；reviewer：新增"声称已脱敏"的可查项（每一处都要能指到 §7.6.2 的某一层或某条已知不覆盖） |
| 1.4.0 | 2026-10-24 | **合并此前未登记的两批增量**：原拟 **1.3.3** 的「§7.6.2 脱敏的两层与边界 + 必须测通道」并入本版（不再单独计版本），以及正文里**早已存在、§13 却从未登记**的「SKU 维度 + 成本表」条文（§0.2 / §1.1 / §3.3 / §4.1 / §5.1 / §5.3.1 / §5.4 / §8.3.3 / §8.5 / §10.3 / §11 的 `v1.4.0` 标注自此有 §13 依据）；同时结清"正文标 `v1.4.0`、§13 无 1.4.0 行、版本行停在 1.3.x"的欠账。**纯增量：不改既有签名（新增参数全部 keyword-only 带默认值）、不改表列、不改 JSON 键、不改错误码、不改路由。** **① 已落地（契约正文与代码同时成立，可按下面的锚点复核）**：(a) **§7.6.2「脱敏的两层与边界」**（:1936-1976）有正文；代码侧 `backend/app/observability/logging.py` 有统一入口 `redact_any`（21 处引用）、`DEFAULT_REDACT_KEYS`（5 键，含 `cookie`）与 `DEFAULT_SCRUB_KEYS`（6 处引用），两份用例齐备——`backend/tests/platform/test_platform_redaction_channel.py`（通道级：调真实 `JsonlLogSink.write` 后**读回落盘文件**断言明文不出现）与 `test_platform_redaction_scrub.py`（函数级对抗）。(b) **§5.3 的 `uv` 来源与作用域判据**（:1194；**归属 §5.3，不是 §5.3.1**）有正文；代码侧 `backend/app/agent/nodes.py` 的 `_summary_uv` / `_declared_uv_scope` / `_day_uv` 落地了"判不出作用域就写 `None`（不摊派、不补零）、有值但非非负整数抛 `ValueError`"，`backend/tests/agent_core/test_collect_uv.py` 在。(c) **`app/api/queries.py` 的只读边界登记**（§0.1 目录树 :80、边界与可检查形态 :140-153）有正文；文件存在，两条 grep 实测均为 **0**（无 `.add(`/`.delete(`/`.flush(`/`.commit(`；无 `step_count` 赋值或自增）。(d) **`DoudianClient` 的 `redact_keys` keyword-only 参数**（§5.1 签名 :1015、语义与理由 :1056-1067）有正文；代码侧 `doudian_client.py` 的 `__init__` 已收该参数（:357），`create_doudian_client` 显式传 `settings.observability.redact_keys`（:842）。(e) **补正一处漏改**：§5.1 的 `DEFAULT_REDACT_KEYS` 逐字块补上 `cookie`（原块只有 4 键，而 §7.6.2 的键名层、`backend/config/settings.yaml` 与代码一直是 5 键——1.3.3 补 §7.6 时漏了这一个块）。**② 已入册、代码未实现（不得读作"已落地"）**：(f) **`total_budget_s`**（§5.1 签名 :1014、语义 :1056）**正文有、代码无**：`grep -rn "total_budget_s" backend/` 为 0，`DoudianClient.__init__` 目前只落了 `redact_keys` 一个 v1.4.0 参数。(g) **SKU 级成本归属与毛利率未实现**：`grep -rn "gross_margin" backend/` 为 **0**，`grep -rn "cost" backend/`（`*.py`+`*.yaml`，排除 `__pycache__`）为 **0**，`SkuSnapshot` 为 **0**；`backend/config/sku_costs.yaml` 不存在，`backend/tests` 里没有 `test_sku_metrics.py` / `test_sku_anomalies.py`。因此下面这些**只有契约条文**：`SkuSnapshot`（§3.3 :377-382，含 `gross_margin` / `cost_cents`）、`MetricsSnapshot.skus`（§3.3 :373）、`Anomaly.sku_id`（§3.3 :402；代码 `backend/app/agent/state.py` 的 `Anomaly` 至今没有这个键）、`compute_metrics(..., cost_table=…)`（§5.3.1 :1171 起）、§5.4 的**两轮求值**（:1241 起）、§8.3.3 的 SKU 名字集合（:2196-2207）、§8.5 的 `config/sku_costs.yaml`（:2241 起）。(h) 因此 **`loss_making` 规则不存在**——正文与代码都没有：`grep -rn "loss_making" backend/` 为 0，`backend/config/rules.yaml` 里没有该规则。README 的「已知限制」第 4 条有对应说明（`README.md:277-279`，其中"SKU 级成本归属未实现"一句与本节一致）；但该条同时把成本口径写成"店铺级加权"、称需自维护 `config/sku_costs.yaml`，而仓库里**该文件与该口径的实现同样不存在**——本行只按 grep 事实登记，**README 不在本次可改文件范围**（`docs/` 外），留给下一轮单独修。 | 全部（**platform**：脱敏两层、`write()` 对 `message`/`data` 走统一入口、`stderr` 走 `_ScrubbingFormatter` 均已落地；**agent-core**：`uv` 作用域判据已落地，但 §3.3 / §5.1 `total_budget_s` / §5.3.1 / §5.4 / §8.3.3 / §8.5 的 SKU 与成本条目是**待实现项**，不是既有能力；**frontend**：**无需改动**——§12 净改动为零、`/api/metrics` 仍店铺级；**qa**：脱敏两份用例已登记，`test_sku_metrics.py` / `test_sku_anomalies.py` 随 SKU 条目一并待实现；**reviewer**：新增可查项——正文里每一处 `v1.4.0` 标注都要能在本行找到落点，且"已入册未实现"不得被读成"已实现"） |
| 1.5.0 | 2026-10-24 | **成本/毛利率落地（纯增量）**：把 v1.4.0 里"已入册、代码未实现"的**成本表 + 店铺级毛利率**真正实现，并把三处"写不出来 / 做不到 / 会误伤"的条文改成可执行形态。**① 已落地**：(a) **§3.3 `MetricsSnapshot` 新增 `gross_margin` / `cost_coverage_ratio`**（`float` 或 `None`，**店铺级**口径）；(b) **§5.3 `compute_metrics(..., cost_table=…)` 由"已入册未实现"转为已实现**，补两条公式（只对有成本 SKU 加权；覆盖率 = 有成本成交额 / 全店 GMV；分母 0 → `None`；**覆盖率不裁剪**，`> 1` 用于暴露上游汇总与明细对不上）+ 6 条数据形状错误的 `ValueError` 边界；(c) **新增数据面 `order_summary["by_sku"] = {sku_id: {gmv_cents: int, units: int}}`**：只统计**已支付**订单行、无 `sku_id` 的已支付行只进 GMV 分母（计为未覆盖）、**退款单仍计入**、件数缺失按 1 件（**偏乐观**：成本被少算 → 毛利率偏高 → 只会**漏报**亏损，不会误报；算例见 §5.3 边界表）、件数非 int / 负数 → `ValueError`；(d) **§8.3.3 `ALLOWED_NAMES` 新增 `gross_margin` / `cost_coverage_ratio`（店铺级）**，店铺级名字 13→15（12 个指标名 + 3 个派生量，并更正原文"12+3=15"的计数错误）、SKU 轮命名空间 23→25 键；(e) **§8.3 `rules.yaml` 新增 `loss_making`**：`severity=critical`、`metric=gross_margin`、条件含 `cost_coverage_ratio >= 0.6`、**无 `actions`（只告警）**；(f) **§8.5 补实现细节**：顶层 `version` / `effective_from` / `costs`、值 `int > 0`、键 `^[A-Za-z0-9_-]{1,64}$`、**重复键 → `ConfigError`**（PyYAML 默认静默取后者，加载器自检）、空值 / 0 / 负数 / float / 字符串 / bool → `ConfigError`、非 UTF-8 → `ConfigError`、**文件缺失 → 记 warn、视为空表**；(g) **§7.5 `MetricPoint` 新增两个可选字段**（默认 `null`，老 checkpoint 无这两个键仍通过校验）。**② 三条裁定（ADR-021）**：(h) **命名冲突**——`gross_margin` 被**店铺级**占用，而 §8.3.3 的 SKU 名字集合里原本也写着它，现冻结硬规则：**SKU 轮次落地时 SKU 侧必须命名 `sku_gross_margin`**，两集合互斥断言触发时必须报**明确错误**并指向本条，**不允许静默失败**；(i) **`loss_making` 无 `actions` 的取舍**——店铺级加权毛利率**指不出具体 SKU**，现有动作类型都要求确定 `target`，用"GMV 最高 SKU"去猜会给好货降价 / 下架，所以**只告警不给动作**；(j) **§8.5 的"启动期校验成本键能在 `raw.products` 里对上"物理上做不到**（启动期没有 `raw`），改写为**运行期在 `compute_metrics` 节点审计 `sku_costs.unmatched`**（只记不阻断；商品列表为空时不报）。**③ 完成情况（逐条按实际状态；"未完成"不得读作"已完成"，"已结清"也不许留在"未完成"里）**：(k) **已结清**（原列在"未完成项"里是归类错误，v1.5.1 更正）：**前端 TS 类型已同步（原"未同步"已结清）**——`frontend/src/api/types.ts` 的 `MetricPoint` 已加 `gross_margin?: number | null` / `cost_coverage_ratio?: number | null`，§12 的逐字块在同一次提交里改完（`verify.mjs` 的 §12 diff 绿）；看板的毛利率已改为**零计算读后端字段**，契约外字段 `cost_cents` 与那套 `(GMV−退款−成本)/(GMV−退款)` 公式**已删除**，并加了"只给 cost_cents 时必须算不出数"的反向断言锁死回潮；(l) **未完成（设计债）**：**alert-only 的 anomaly 不出现在 `/api/alerts`**——该端点读 `approval_request` 表，只有**产生了审批行**的异常才可见，因此运营**从审批台看不到** `loss_making`，当前唯一可见通道是 `/api/logs` 的 `anomaly.detected`（summary 含 `rule_ids`，**detail 行还带 `observed` / `threshold` / `severity`**，形状见 §7.6.3）与 `metrics.computed`（含两个成本字段）；**需要一个独立的告警出口，属于设计债，不是本次实现**。**代码锚点**：`backend/app/services/costs.py`（新）、`backend/config/sku_costs.yaml`（新）、`backend/app/services/metrics.py`、`backend/app/agent/state.py`、`backend/app/agent/nodes.py`、`backend/app/services/rules.py`、`backend/app/services/anomalies.py`、`backend/app/main.py`、`backend/app/schemas.py`、`backend/config/rules.yaml`、`backend/tests/platform/test_platform_gross_margin.py`（**34 用例全绿**）。 | 全部（agent-core：成本表加载器 + 毛利率 / 覆盖率公式 + `by_sku` 归集 + 未匹配审计；platform：lifespan 加载成本表、`MetricPoint` 两个默认 `null` 的字段；qa：`test_platform_gross_margin.py` 34 例；**frontend：无需改动**——§12 净改动为零，但**看板口径已对齐（`types.ts` + §12 逐字块 + 看板取值 + `verify-suite.ts` 断言同一次提交完成；基线 verify 的 27/28 红也已修，根因是 §12 正文内嵌围栏标记），**(k) 已结清**（v1.5.1 更正：原文把它列进"未完成项"是归类错误）**；reviewer：新增可查项——正文里每一处 `v1.5.0` 标注都要能在本行找到落点，且"未完成项"不得被读成"已实现"） |
| 1.5.1 | 2026-10-25 | **审计事件登记（纯增量）**：§7.6 新增 **7.6.3「审计事件（JSONL）」**，把 `nodes.py` 已在写、契约里却没有位置的形状补登记。**① 已登记（正文与代码同时成立）**：(a) 与 `node.trace` 的分工表（谁写、`data` 键集合、条数、靠 `event` 名区分；`node.trace` 仍只由 `tracing.py` 写、键集合未改）；(b) 7 行事件表：`metrics.computed`（`info`，`date` / `gmv_cents` / `history_days` / `gmv_7d_avg_cents` / `gross_margin` / `cost_coverage_ratio`）、`anomaly.detected` 的 `kind="summary"`（`info`，`count` / `rule_ids` / `skipped` / `dropped`）与 `kind="detail"`（`level` 由 `severity` 派生 `critical→error` / `warn→warn` / `info→info`，`anomaly_id` / `rule_id` / `metric` / `observed` / `threshold` / `comparator` / `severity` / `window` / `detected_at`）、`collect.units_degraded` / `collect.zero_revenue_paid` / `collect.row_rejected`（均 `warn`：`kind` / `count` / `reasons` / `sample_limit` / `samples_truncated` / `samples`，`row_rejected` 另有 `excluded_from_metrics=true`）、`sku_costs.unmatched`（`warn`：`sku_ids` / `count`）；(c) **`anomaly.detected` 两形态的硬规则**：消费方按 `data.kind` 分支，**禁止**用"有没有 `rule_id`"猜（临时做法作废）；明细行带 `observed` / `threshold` / `severity`，因此 §8.3 "alert-only 规则的唯一可见通道"由"只有 `rule_ids`"升级为"有明细"；(d) **采集层行级判定表**（一行脏 → 整行拒绝且不进任何指标；能猜出含义 → 有界降级；整批契约坏 → 硬失败不 500；全行被拒（`rows_seen>0` 且 `rows_accepted==0`）→ 视同整批失败；零行 → 正常成功），并写明"整批坏与全行被拒在 JSONL 上**没有** `collect.*` 事件，只有 `node.trace` 的 `outcome="error"`"；(e) `samples` 截断（`count` 是全量、`sample_limit` 当前 5、`samples_truncated`）与三种事件的样本 schema；(f) `source.partial` 的语义（"这份指标不完整"≠"出错了"，且**目前不在任何 REST / JSONL 出口**）；(g) 脱敏沿用 §7.6.2 两层，并写明"写盘失败是旁路失败"；同时明确 **`action.proposed` / `approval.decided` / `action.executed` / `effect.registered` / `run.finished` 仍只进 `state.audit`、不进 JSONL**，而 **`collect.done` / `collect.failed` 本版起进 JSONL**（见本节末）（登记它们等于许一个 `/api/logs` 永远查不到的空承诺）。**② 顺带更正（v1.5.0 行）**：把 1.5.0 行的 (k)（前端 TS 类型已同步）从"未完成项"里移出并标"已结清"（原文与实际不符），(l) 的"唯一可见通道"补上明细行指针。**③ 未修（属前端夹具，不在本次范围）**：`frontend/mock/logs.json` 的 `anomaly.detected`（`data` 只有 `anomaly_ids`、无 `kind`）与 `metrics.computed`（缺 `date` / `history_days` / 两个成本字段）仍是旧形状，按 §7.6.3 属**不合规夹具**；`frontend/mock/logs.json` 里还有 `collect.started` / `approval.requested` 两个 backend **没有生产者**的事件名（`frontend/src/mock/mockServer.ts` 同样在写这两个），因此 `python backend/tests/fixtures/demo/check_event_names.py frontend/mock/logs.json` 现在是**红**的（exit 1） | 全部（platform：`/api/logs` 原样透传、`event` 前缀匹配不变，**不按事件名做分支**；agent-core：新事件必须走 `_log_audit`（不得自建 logger），`node.trace` 不得加键；frontend：非 trace 行仍按 `level` 展示、按 `event` 前缀过滤，**若渲染 `anomaly.detected` 必须按 `data.kind` 分支**；qa：`test_node_audit_sink.py` / `test_collect_row_contract.py` 已覆盖，新增形状沿用"读回落盘文件"的验收方式；reviewer：新增可查项——正文里每一处 event 名都要能在 `nodes.py` 找到 `_log_audit` 调用点，反之亦然） |
| 1.5.2 | 2026-10-25 | **行级账目补全（纯增量）**：①**把"契约没跟上代码"改对**——v1.5.1 的代码已把 `rows_seen` / `rows_accepted` 写进 `collect.row_rejected` 的 `data`，正文却写"不在 JSONL，除非先改本节"；本版按代码改正，并补 `rows_out_of_window`（`_aggregate_orders` 早已在算、但没进任何事件）。②`collect.done` / `collect.failed` / `collect.row_rejected` 三处都带 `rows_out_of_window`，冻结行级账目恒等式 `rows_seen == rows_accepted + rows_rejected + rows_out_of_window`（`collect.row_rejected` 的被拒项就是它自己的 `count`），并由 `_check_row_accounting` 在每个出口写入前显式断言。③行级判定表补 **`no_known_fields`** 档（`{}` / `{"foo": 1}` 不是"一笔 0 元订单"）。④新增 **`collect.date_fallback`**（warn）：行被**接受**但 `create_time` 存在却不可用（falsy / 解析失败）、靠 `pay_time` 才解析成功时必须留痕且 `source.partial=true`，**不计入** `row_rejected`（否则"上游把 `create_time` 全写成 `False`"永远查不出来）。⑤顺带把 §7.6.3 里与代码不符的可见性/键集合改对：`collect.done` **没有** `source`、`collect.failed` 的键集合、`collect.failed.reason` 三档、`source.partial` 已在 JSONL、`_business_date_of` 不再接 `TypeError`。**为什么升 1.5.2 而不是写成"1.5.1 的登记补全"**：本版不只是文字追上代码，还新增了 JSONL 键（`rows_out_of_window`）、新事件名（`collect.date_fallback`）与新行为（日期降级置 `source.partial=true`）；按 §0 的冻结规则，这些必须占一个补丁版本号，否则"冻结的 1.5.1 正文"仍会与代码不一致。历史 1.5.1 行保留不改。**代码锚点**：`backend/app/agent/nodes.py`（`_row_business_date` / `_read_order_row` / `_aggregate_orders` / `_check_row_accounting` / `collect`）；测试 `backend/tests/agent_core/test_collect_row_contract.py`。**未完成项**：无 | 全部（agent-core：新增/补全事件与键；platform：`/api/logs` 原样透传、前缀过滤，**不改代码**；frontend：非 `node.trace` 行按 `level` 展示，**不改代码**；qa：新增 8 条用例（falsy 日期回落留痕 / 4 桶恒等式 / 全窗口外失败 / TypeError 两处口径）；reviewer：新增可查项——每个 `rows_*` 键都能在对应出口找到，恒等式在代码里有显式 `raise`） |

**1.5.2 的兼容性**：**纯增量，不改任何既有签名 / 表列 / 错误码 / 路由 / `node.trace` 键集合**。具体：
- **JSONL 只加键、不改既有键的含义**：`collect.row_rejected` / `collect.done` / `collect.failed` 的 `data` 新增 `rows_out_of_window`（`collect.row_rejected` 另外补上代码里早已写出的 `rows_seen` / `rows_accepted`）；`collect.date_fallback` 是新事件名——`/api/logs` 的 `event` 本来就是自由字符串、只做前缀过滤，服务端不需要枚举校验，前端不需要改代码（未知事件名原样显示）。
- **旧日志仍合法**：没有 `rows_out_of_window` 的历史行照样能读；若看板用 `rows_seen - rows_accepted` 估算"被拒数"，请改为读 `rows_rejected`——那个差值含窗口外的行（v1.5.1 的正文曾把它读成"被拒"）。
- **唯一的行为变化有两处**：①`create_time` 不可用而 `pay_time` 可用的行，现在会多一条 `collect.date_fallback`（warn）且 `source.partial=true`；指标数值**不变**（仍按解析到的日期计入），变的是"这次采集不完整"的可见性。②行级账目在写盘前多一道断言；若恒等式不成立（只可能是代码 bug——某个桶漏记），`AssertionError` 会从节点传播出去，而**不是**伪装成"数据脏"或写出一条对不上的审计（错误的审计比没有审计更坏）。
- **`node.trace` 一字未动**：`data` 仍是 5 键、`outcome` 仍是三态；§4.7 的不变量「`count(event=="node.trace") == agent_traces.step_count`」不受影响。

**1.5.1 的兼容性**：**纯增量，不改任何既有签名 / 表列 / 错误码 / 路由 / `node.trace` 键集合**。具体：
- **`/api/logs` 不新增参数、响应形状不变**：`event` 本来就是自由字符串（§7.6 的 Query 表），新增事件名只是新的取值；服务端只做前缀过滤，**不需要**枚举校验。前端不改代码也能显示这些行——非 `node.trace` 行按 `level` 上色、`outcome` 列显示"—（非节点 trace）"，未知事件名原样显示。
- **`node.trace` 一字未动**：`data` 仍是 5 键、`outcome` 仍是三态、`message` 仍是三种取值；§4.7 的不变量与 `reconcile_step_count` 行为不变，旧日志无需回填。
- **旧行仍合法**：本节只是把**已经在写**的形状登记下来，不要求历史 JSONL 里有这些事件——没有 `metrics.computed` 的旧 run 照样能读、能回放。
- **行为变化为零**：代码侧没有一行为本节而改（本节是"补登记"，不是"新实现"）。若正文与代码不一致，以代码为准并**改本节**——不许反过来让实现去追一个从没人写过的形状。
- **需要同步的只有夹具**：`frontend/mock/logs.json` 里那两条旧形状行是**演示数据**，不是接口契约；修它们是"让 mock 别骗人"，不影响线上读取逻辑。

**1.5.0 的未完成项 (m)：`total_budget_s` 已入册、代码未实现。** §5.1 的签名里列了它、§1083 要求装配点显式传它、§9.2 规定了它的行为，但 `backend/app/services/doudian_client.py` 里**没有这个参数**（`grep total_budget_s backend` = 0）。v1.5.0 期间曾试做一版，因**未接线**（`create_doudian_client` 从未传它，生产路径恒为 `None`，功能是死代码）被整版回退；同时那次实现还有三处与 §9.2 不符（截止起算点、令牌桶/信号量等待未计入、错误类别选错）。要真做必须先补齐：接线 + 起算点对齐 + `_send` 内等待计入 + 新增 `ErrorCategory.BUDGET`（不占用 `CONFIG`）+ 相等边界用 `>=` + 测试能证明"第 N+1 次尝试前就被掐掉"。**在那之前，任何文档/看板/简历都不得声称"有单次调用总预算"。**

**1.5.0 的兼容性**：**纯增量，不改任何既有签名 / 表列 / JSON 键 / 错误码 / 路由**。具体：
- **签名只加不删**：`compute_metrics` 的 `cost_table` 早在 v1.4.0 就入册（keyword-only + 默认 `None`），本版只是把它**实现**——老调用点一行都不用改，不传就是"这家店没配成本"（两个成本字段 `None`）。
- **没有新增表**：`__tablename__` 仍恰好 4 行（§4.7 断言 B′ 不变，§6.1）。成本是**配置文件**（§8.5）、`by_sku` 是 `raw` 里的**归集结果**，两者都不进 DB。
- **JSON 只增两个可选键**：`/api/metrics` 的 `points[]` 多出 `gross_margin` / `cost_coverage_ratio`，默认 `null`；**老 checkpoint 没有这两个键仍能通过 `MetricPoint` 校验**（`model_config` 只 forbid extra、不改默认），**不需要回填、不需要重跑**。旧消费者读到 `null` 的行为与"没有这个字段"一致。
- **唯一的行为变化**：`metrics` 里多了两个成本口径数字；当成本覆盖 ≥ 60% 且加权毛利率 < 0 时 `loss_making` 会命中并记 `anomaly.detected`。**它不产生审批行、不产生任何动作**（无 `actions`），所以审批台的可见性**不变**——这正是未完成项 (l) 要单独解决的问题。
- **一个刻意的"不对称"**：成本表**内容**坏了（重复键 / 非 UTF-8 / 空值 / 0 / 负数 / float / bool）→ 进程启动即失败（`ConfigError`）；文件**缺失** → 只记 `warn` 并按空表继续。这两条不对称是设计（§8.5），不是遗漏：前者是"有人写坏了"，后者是"这家店还没配成本"。
- **§12 已改（v1.5.0 同步完成）**：`types.ts` 的两个字段与 §12 的 ts 块在**同一次提交**里改完，`verify.mjs` 的 §12 逐行 diff 已 PASS。**顺带修掉一个长期潜伏的陷阱**：§12 正文里原本内嵌了字面的围栏标记，`verify.mjs` 用 `indexOf` 取第一个围栏，命中的是**正文**而不是代码块——所以基线 `verify` 本来就是红的（HEAD 上就红）。已去掉正文里的围栏字样，并写死"本节正文不许再出现围栏标记"。

- **键名层默认集 5 → 6（v1.5.0 补 `password`）**：`password` 原先只在**文本层**的 `DEFAULT_SCRUB_KEYS` 里，而文本层只认 `key=value` 形态——于是 `data={"password": "SECRET"}` 这种**结构化**值两层都碰不到、明文落盘。判据不是"这个名字敏不敏感"，而是"它出现在什么**位置**"：出现在 dict 键名上，就必须进键名层。已有回归用例锁死（`test_password_as_dict_key_is_masked`）。
- **一处 docstring 与实现不符已改正**：`_log_audit` 曾声称"同时写进 JSONL 汇（`state.audit` 原样保留）"，但它只写 sink；`state` 是不可变 `Mapping`，审计条目要随节点返回进 state。现在 docstring 写明"只写汇，`state.audit` 由调用点 `append_audits(...)` 成对写"。**注意这条不是措辞纠错，它揭示了一个真实风险**：双出口靠每个调用点自觉，漏写一侧不会报错。

**1.4.0 的兼容性**：**纯增量，不改任何既有签名 / 表列 / JSON 键 / 错误码 / 路由**。所有新增都是**追加型**，既有调用点与既有消费者一个都不需要改。具体：
- **签名只加不删**：`DoudianClient.__init__` 末尾追加 `redact_keys` 一个 **keyword-only + 带默认值**参数（`total_budget_s` 同期入册，但**至今未实现**，见 §13(m)），`compute_metrics` 追加 `cost_table=None`；三处都不改既有位置参数、不改返回类型，老调用点原样可用。
- **没有新增表**：`__tablename__` 仍恰好 4 行（§4.7 断言 B′ 不变，§6.1 已复核）。SKU 明细是**派生结果**（state 内、可重算），成本是**配置文件**（§8.5），两者都不进 DB。
- **JSONL 与 HTTP 形状一字未动**：§7.6 的 10 个顶层键、`/api/logs` 响应、§12 的 `LogItem` 均未改；`/api/metrics` 仍是店铺级、**不加** `skus`（§7.5）；`Anomaly` 从不出现在任何 HTTP 响应里，因此 §12 不需要新的 TS 类型（净改动为零）。
- **与 1.3.3 的关系**：原拟 1.3.3 的脱敏条目**全部保留**在 §7.6.2（正文一句未删），本版只把它并入统一的一行、不再单独计一个版本号；上表的 1.3.3 行标"已并入 1.4.0"保留留痕。
- **唯一的行为变化仍来自脱敏**：`message` / `stderr` 里的 `access_token=…` 从明文变成 `***`（取舍见 1.3.3 的兼容性一节）。SKU / 成本条文**不产生任何行为变化**——因为没有对应代码。
- **未实现项的性质**：本行 ② 是**契约先行**（先把"该怎么算"钉死，再补实现），不是"已具备的能力"。把契约先行写成"已实现"，正是 REVIEW-core F1 的同一个失效模式（文档声称的属性在代码里不成立），所以这里逐条附了 grep 依据而不是一句"已完成"。

**1.3.3 的兼容性**：**纯增量，不改任何签名 / 表列 / JSON 键 / 错误码 / 路由**。具体：
- 契约里**新增**的只有两处：§7.6.2（新小节）与 §10.3 的一行测试要求。§7.6 原有的脱敏条目只是**补全**（补 `cookie`、补"值级层也要"、加 §7.6.2 指针），规定的事实未变。
- **JSONL 形状一字未动**：一行仍是 §7.6 的 10 个顶层键，`/api/logs` 与 §12 的 `LogItem` 不需要改任何代码——脱敏只改"写进去的值"，不改键名也不改结构。
- **`redact()` 的行为变强、签名不变**：对 `str` 从"原样返回"变成"会遮"（`redact("access_token=…")` 不再原样返回）。这正是本版目的；"把 `redact` 的返回值当原文用"的依赖不存在——那种依赖本身就是这次的事故。
- **旧日志不回填**：已经落盘的明文凭据擦不掉（日志是 append-only 的旁路事实源），本版只保证**新写入**的行不再出现。若历史日志已被外部采集，按 §5.1-7 的纪律走**凭据轮换**，不要指望"日志脱敏"补救。
- **唯一的行为变化**：`message` 与 `stderr` 里的 `access_token=…` 从明文变成 `***`，排障时看到的是遮罩而不是原值。这是刻意的取舍——§7.6.2 的"已知不覆盖第 5 条"要求靠纪律（不把凭据拼进散文）来承担，而不是靠遮罩兜底。
- **欠账已结清（v1.4.0 补正）**：本节原登记过一条欠账——"正文 §0.2 / §1.1 / §4.1 / §3.3 / §5.3.1 / §5.4 / §8.5 / §10.3 / §11 已标注 `v1.4.0`（DECISIONS 的 ADR-018 也按 v1.4.0 记状态），但 §13 没有 1.4.0 行、版本行停在 1.3.x"。**该欠账已由 v1.4.0 行结清**：1.4.0 行已补录（见上表），版本行随之升到 1.4.0，本节 1.3.3 的全部内容并入该行、不再单独计版本。这句保留在此只为留痕，**不再是当前状态**。

**1.3.2 的兼容性**：**纯增量，不改任何已有签名 / 表列 / JSON 键 / 错误码 / 路由形状**。具体：
- 新增内容只有"文件归属 + 读路径说明 + 一处字段来源"：§0.1 登记 `app/api/queries.py` 并冻结其**只读**边界；§5.8 / §7.3 说明列表/游标可以经它做 keyset 分页（**写路径不变**，§5.8 的五个签名一个字没改）；§7.5 明确数据源是 checkpoint state、缺数据 404、不补零；§5.3 补 `uv` 的来源与作用域判据（**公式未改**：仍是 `cvr = paid_order_count / uv`，`uv` 缺 → 两者 `None`）。
- **`/api/metrics` 的响应形状未变**：`MetricPoint` 的 `uv` / `cvr` 本来就是"可为 `null`"，本版只是让 `collect` 在**上游确实给了当天 uv** 时填上真实值；给不出时保持 `null`。
- **旧数据无影响**：历史 run 的 checkpoint 里 `order_summary` 没有 `uv` → 仍按 `None` 读，不需要回填、不需要重跑。
- **没有新增表**：`__tablename__` 仍恰好 4 行（§4.7 断言 B′ 不变）；`queries.py` 只做 `SELECT`。
- 唯一的行为变化：**demo/dry_run 下看板的 `uv`/`cvr` 两列从恒空变成有真实数字**（cassette 的 `summary.uv=480` 现在会被 `collect` 认下），这正是 v1.3.2 的验收项之一。

**1.3.1 的兼容性**：**纯增量，不改任何已有签名 / JSON 键 / 表列 / 枚举值 / 错误码 / 路由**。具体：
- 新增内容只有两样语义 + 一处常量：§4.7 规则 5 的 **c2 分支**（正常返回但把 run 从非 `failed` 推进到 `failed` → `outcome="error"`），以及 §7.6.1 里 `error.type` 多**一种取值**（固定标记 `NodeDeclaredFailure`）。§4.7 常量块补 `NODE_DECLARED_FAILURE_TYPE`，它只是那个字符串的**名字**。
- **JSONL 形状一字未动**：`data.error` 仍是 `{"type", "message"}` 两个键，`outcome` 仍是三态，顶层字段不变。因此 `/api/logs`、JSONL 消费者、前端**都不需要改代码**——`type` 只被当字符串展示/grep，读取端不对它做枚举校验（§7.6.1）。
- **旧日志无影响**：历史 `node.trace` 里要么没有 `outcome`（按 §7.6.1 读取端规则当 `"ok"` 读），要么 `error.type` 是异常类名。两种都仍然合法，无需回填。
- **没有错误码/状态迁移变化**：`collect` 硬失败在 v1.3.0 就已经是 `status=failed` + `collect --(fail)--> finalize`（§4.2），本版只是让**日志**把这次执行标成 `error`，DB 的 `status`/`step_count` 写入行为与 `agent_traces` 表结构完全不变。
- **唯一的行为变化是日志语义**：之前"采集硬失败"在日志页是 `outcome="ok"` 的绿灯（告警看不到），现在是 `error` + 红色，且 `finalize` 不会被连坐成第二个红灯（判据是状态**迁移**，见 ADR-019）。

**1.2.0 的兼容性**：**纯增量，无破坏性变更**。不改任何顶层字段名、JSON 键位置（`outcome` 在 `data` 内，`/api/logs` 顶层 schema 与 §12 的 `LogItem` 一字未动）、签名、枚举值、表列、错误码、路由。因此：
- 已按 v1.1.0 写的代码**不需要回退**，只需补 §4.7 规则 5 的分流顺序（不补的后果是 HITL 挂起在日志里仍为红色 error，功能可用但告警不可用）；
- **旧日志无需回填**：v1.1.0 写入的 `node.trace` 没有 `outcome`，读取端按 §7.6.1「缺键按 `ok` 读」处理；
- `step_count` 语义与不变量**未变**（计数不看 `outcome`），因此 §4.7 的可断言等式与 `reconcile_step_count` 行为不变；
- 唯一的行为变化是**日志与看板的显示语义**：正常审批挂起从"红色 error"变成"中性 interrupted"。这正是本版目的。

**1.3.0 的兼容性**：**纯增量，不改任何已有签名/表列/JSON 键/枚举值/错误码/路由**。具体：
- 新增内容只有三样：§0.1 里 `adapters.py` 的一行目录树 + 一行归属（该文件**本来就存在**，本版只是给它定性与归属）、§4.8 的新节、§4.7 断言的**改写**（不是新增约束——`step_count` 单写点这条约束 v1.1.0 就有，本版换的是**取证方式**）。
- **对已写代码的影响是"删重复"，不是"改接口"**：agent-core 需删掉 `nodes.py` 里重复的 `AgentRuntime`/`get_runtime`/`set_runtime` 与 `adapters.py` 里的 `agent_traces`/`action_log` 仓储，统一到 §4.8 定义的边界；`nodes.py` 里对 `resolve_*` 的调用与 `finalize` 里的 `reconcile_step_count` 调用**照旧保留**（它们是允许的读取侧调用，§4.7 断言取向已写明）。
- **§4.7 断言从"过不了"变成"可过"**：v1.2.0 的字面 grep 与冻结的调用时机互斥，任何实现都无法通过；v1.3.0 的 A/B′/C 已在当前代码上实测成立（A=3 行全在 `tracing.py`、B′=4 行全在 `models.py`、C=1 行在 `adapters.py`），因此它从"信仰条款"变成"回归门禁"。
- `app.main` 尚不存在，§4.8 的 lifespan 装配要求是**对 platform t5 的前置约定**，不是对既有代码的追改。

**1.1.0 的兼容性**：不改任何已有签名、字段名、JSON 键、枚举值、表列、错误码——**纯增量**。t2/t3/t4 已在写的代码不需要回退，只需补 §4.7 的包装器与 §7.6.1 的事件形状。

**变更流程**：提出方发消息给 spec-lead（列出：要改哪个签名/字段、为什么现契约不成立、影响的调用点）→ spec-lead 改本文件并升版本 → 三方同步。**不接受**"我先在代码里改，文档回头补"。
