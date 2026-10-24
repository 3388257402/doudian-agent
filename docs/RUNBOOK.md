# RUNBOOK —— 跑起来、排障、重生成演示数据

本文件里的每条命令都在 Windows 11 + Python 3.12.8 上**真跑过**（macOS/Linux 把
`.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`）。命令都在**仓库根目录**执行，除特别注明。

---

## 0. 一次性准备

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend; npm install; cd ..
```

> `.venv/`、`node_modules/`、`data/` 都在 `.gitignore` 里。跑完之后 `git status --short` 应当是干净的。

---

## 1. demo：零凭据看完整链路（推荐先跑这个）

```powershell
.\.venv\Scripts\python.exe backend\tests\fixtures\demo\run_demo.py
.\.venv\Scripts\python.exe backend\tests\fixtures\demo\run_demo.py --verdict reject   # 看"人工否决"
```

它做了什么：`create_app()` 的 lifespan（建表 → 日志汇 → 运行时 → 检查点）→ 5 个 HTTP 端点
（ASGI 进程内，不占端口）→ `run_effect_evaluator_once()`。数据来自
`backend/tests/fixtures/doudian/cassettes/`，时钟固定 `2026-10-23 12:00 UTC`（= 上海 20:00），
所以**换任何一天跑，输出都一样**。

| 参数 | 作用 |
|---|---|
| `--verdict reject` | 人工否决：动作记 `rejected`，不写 `action_log`，不登记效果窗口 |
| `--run-id X` | 指定 run_id；同一个 id 再跑会**回放**已存结果（200，不重跑图） |
| `--keep-data` | 保留上次的 `data/demo/`（默认每次清空：幂等键不含 run_id，不清会看到 `skipped_duplicate`） |

现场状态：`data/demo/`（SQLite + checkpoint + JSONL 日志）。要清空就直接删这个目录。

---

## 2. 起服务 + 手工打一遍接口

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
```

另开一个终端：

```powershell
# 1) 存活探针（真的会探 db + checkpointer）
curl.exe http://127.0.0.1:8000/api/healthz
# {"status":"ok","version":"0.1.0","db":"ok","checkpointer":"ok","time":"…"}

# 2) 触发一次 run（离线夹具：dry_run=true）
curl.exe -X POST http://127.0.0.1:8000/api/agent/run -H "Content-Type: application/json" `
  -d '{\"shop_id\":\"12345678\",\"trigger\":\"manual\"}'
```

**注意这条命令的结果取决于"今天"**：离线夹具的订单都在 `2026-10-23`。用真实时钟跑时，
"今天"没有订单 → 退款率算不出（`null`）→ 规则不命中 → 直接 `status="done"`、无待办。
**这不是 bug**（规则没命中就是没命中）；要看"审批中断 → 裁决 → 执行"的完整链路，用 §1 的 demo。

想手工走审批，需要夹具的业务日当天：把那台机器的系统时间或 `settings.yaml` 的
`doudian.dry_run`/夹具换掉都行，或者在 demo 里看（它已经把时钟固定好了）。
审批的请求体形状（`edited_params` 必须**出现**，可以为 `null`）：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/approve -H "Content-Type: application/json" -d @approve.json
# approve.json:
# {
#   "thread_id": "<POST /api/agent/run 返回的 thread_id>",
#   "interrupt_id": "<GET /api/alerts 里那条的 interrupt_id>",
#   "operator": "ops_zhang",
#   "decisions": [
#     {"action_id": "<alerts 里的 action_id>", "verdict": "approve", "edited_params": null}
#   ]
# }
```

其余端点：

```powershell
curl.exe "http://127.0.0.1:8000/api/alerts?shop_id=12345678&status=pending&limit=20"
curl.exe "http://127.0.0.1:8000/api/metrics?shop_id=12345678&date_from=2026-10-16&date_to=2026-10-24"
curl.exe "http://127.0.0.1:8000/api/logs?run_id=<run_id>&limit=50"
```

Swagger：<http://127.0.0.1:8000/docs>

---

## 3. 前端

```powershell
cd frontend
npm install
npm run dev        # http://localhost:5173
npm run build      # tsc --noEmit && vite build → frontend/dist
npm run verify     # 断言 mock fixtures 的字段集与契约同形（不出网）
```

- 默认 **mock 模式**：读 `frontend/mock/*.json`，不需要后端。
- **live 模式**：页面右上角切换，或 `frontend/.env` 里 `VITE_API_MODE=live`
  （后端 CORS 白名单已含 `http://localhost:5173`）。
- 改了后端**事件名 / 字段**之后：`frontend/mock/logs.json` 之类的手写 fixture 会漂，
  用 §4 的检查器验一遍。

---

## 4. 演示数据的重生成与一致性断言（改事件名后必做）

```powershell
# 从一次真实运行导出事件流（会真的跑一遍 demo，然后写 logs_export.jsonl + logs_export.meta.json）
.\.venv\Scripts\python.exe backend\tests\fixtures\demo\export_event_log.py

# 断言：文件里出现的每个事件名，都必须在 backend/app 里找得到生产者
.\.venv\Scripts\python.exe backend\tests\fixtures\demo\check_event_names.py backend\tests\fixtures\demo\logs_export.jsonl
```

`check_event_names.py` 可以检查任意 JSON/JSONL（例如前端 mock 数据）：```powershell
.\.venv\Scripts\python.exe backend\tests\fixtures\demo\check_event_names.py frontend\mock\logs.json
```

- 退出码 `0` = 所有事件名都有生产者、且文件里没有凭据形状；`1` = 事件名漂移；`2` = 命中凭据形状。
- 它还会报告"实现会产出、但该文件没演示"的事件名（漏演示，与漂移是两个方向）。
- **导出脚本自带出口闸门**：保留的事件里一旦出现 `access_token=` / `sign=` / `Bearer …`，
  它会**拒绝写盘**（那种东西进了 git 历史就删不掉）。命中就先去修脱敏，别绕过。

---

## 5. Docker

```powershell
docker compose config          # 只做静态校验，不需要 daemon
docker compose up --build      # 后端 :8000、前端 :5173
docker compose down            # 停；数据在命名卷 doudian-data 里，down 不会删
docker compose down -v         # 连卷一起删（会丢 SQLite 里的 run/审批历史）
```

切 Postgres 只改环境变量（`docker-compose.yml` 里有一段注释掉的 `postgres` 服务）：

```
DATABASE_URL=postgresql+psycopg://doudian:doudian@postgres:5432/doudian
CHECKPOINT_TYPE=postgres
CHECKPOINT_URL=postgresql://doudian:doudian@postgres:5432/doudian
```

> 注：`docs/CONTRACT.md` 里引用过的 `docs/ENV.md`（本机环境探测记录）没有随开源仓库提供，
> 它和审查报告一起放在 `docs/internal/`（已 `.gitignore`）。契约内容本身是自足的，
> 只有"某台机器上 pip 索引里的版本号"这类事实引用了它。

---

## 6. 排障

| 现象 | 原因 / 处理 |
|---|---|
| 启动直接退出，日志里 `ConfigError` | 配置问题：`settings.yaml` 未知字段、`${VAR}` 未解析、`notifier.mode=feishu` 缺 `FEISHU_WEBHOOK_URL`、`checkpoint.type=postgres` 缺 URL。**故意不让带病启动** |
| `拒绝在 prod 下以 doudian.dry_run=true 启动` | `app.env=prod` 时必须真连上游：填三个 `DOUDIAN_*` 并把 `doudian.dry_run` 改 `false` |
| `400 VALIDATION_ERROR ... body.interrupt_id` | 请求体缺字段。`decisions[].edited_params` 必须**出现**（可为 `null`） |
| `409 RUN_IN_PROGRESS` | 同一个 `(shop_id, run_id)` 还在跑；等它收敛或换 run_id |
| `409 DUPLICATE_DECISION` | 这些动作已经裁决过（同一个审批页面点了第二次） |
| `404 APPROVAL_NOT_FOUND` | `interrupt_id` 不是**当前**挂起的中断（旧页面/已过期） |
| `502 UPSTREAM_ERROR` | 上游抖店错误（分类见 `docs/CONTRACT.md` §9.1）；`dry_run` 下检查 cassette 是否有对应 `match.method` |
| `/api/metrics` 返回 `404 RESOURCE_NOT_FOUND` | 该店铺该区间还没有指标（指标源是 checkpoint，只覆盖跑过 run 的业务日） |
| `/api/logs` 为空 | 看 `settings.observability.log_file`（默认 `data/logs/app.jsonl`）；进程刚启动且还没跑 run 时就是空的 |
| demo 里动作被标成 `skipped_duplicate` | 你没清 `data/demo/`：幂等键不含 `run_id`，同一动作第二次跑会被正确判重（去掉 `--keep-data`） |
| 端口被占 | `--port 8001`（同时改前端 live 模式的 baseURL 或 nginx 反代目标） |

**对账**（进程在"写日志"和"DB +1"之间被杀过）：启动时 lifespan 会对每个非终态 run 调
`calibrate_run_steps`，把 `agent_traces.step_count` 校准到 `count(node.trace)`。
日志里看 `app.reconciled` / `app.reconcile_failed`。

---

## 7. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q          # 全量（零凭据、零外网）
.\.venv\Scripts\python.exe -m pytest backend\tests\api -q      # 只跑 API 层
.\.venv\Scripts\python.exe -m pytest backend\tests -q -k collect
```

---

## 8. 提交前自检（维护者）

```powershell
git status --short                      # 必须干净，且**不得**出现 .venv/ node_modules/ data/
git check-ignore -v data\doudian.db     # 应输出命中 .gitignore 的那一行
.\.venv\Scripts\python.exe backend\tests\fixtures\demo\check_event_names.py backend\tests\fixtures\demo\logs_export.jsonl
```
