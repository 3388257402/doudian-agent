# DECISIONS.md — 架构决策记录（ADR）

每条 ADR 结构固定：**状态 / 原方案 / 问题 / 决定 / 影响（可验证）/ 为什么这样对生产更好**。
「可验证的影响」= 别人能跑一条命令或读一段代码确认的东西，不是形容词。

- 状态取值：`已决策`（契约已冻结并落实到 CONTRACT.md）、`待核实`（引用了未能实测的外部事实）、`待沙箱实测`（算法已按官方 SDK 实现，缺真实凭据验证）
- 本文件与 `docs/CONTRACT.md` **同源**；冲突时以 CONTRACT.md 的签名/字段为准，然后立刻修本文件。

---

## ADR-001 签名：timestamp 单一来源，签名只覆盖协议字段

**状态**：已决策（`待沙箱实测` 的是"服务端是否接受该时间格式"，见 ADR-007）

**原方案**
```python
ts_sign = datetime.utcnow().strftime("%Y%m%d%H%M%S")   # 用于签名
payload = {"app_key": ..., "access_token": ..., "v": "2",
           "timestamp": str(int(time.time())), "params": params}   # 用于请求
sign = _sign(params)                                   # 只对 params 签名
```

**问题**
1. **两个 timestamp 不同源**：签名用 A，请求传 B。服务端验签时用**请求里携带的** timestamp 重算签名串 → 必然不等于客户端算的 → 100% 签名失败。更隐蔽的版本是同一时钟但不同格式（`%Y%m%d%H%M%S` vs epoch 秒），差 8 小时/差格式都是全错。
2. **签名覆盖范围与请求内容不一致**：`_sign` 只对 `params` 签名，但 body 里还塞了 `app_key/access_token/v`。服务端按"body 里的 `param_json` 原文"重算签名，而客户端的 `param_json` 是另一个字符串 → 签名对不上。而且 `access_token` 参与 body 意味着它进了签名材料，token 一刷新签名就变，缓存/重试全部失效。
3. 时间戳在重试路径上不重算：第一次超时后重试仍用旧 timestamp，超过平台容忍窗口就是签名过期。

**决定**（已落到 CONTRACT.md §5.2）
- **一个 timestamp 变量贯穿全程**：`format_timestamp(datetime.now(tz_local))` 生成一次字符串，签名与查询串用**同一个对象**。
- 签名材料固定为：`app_secret + "app_key"+ak+"method"+m+"param_json"+pj+"timestamp"+ts+"v"+v + app_secret`，`HMAC-SHA256(key=app_secret)`。
- **`access_token` / `sign_method` / `sign` 不参与签名**，且只出现在 URL 查询串里；**body 只有 `param_json` 原文**。
- 每次重试**重新生成** timestamp 与 sign（`param_json` 不变）。

**影响（可验证）**
- `backend/tests/test_signing.py`：三个黄金向量逐一相等；另有一条断言"签名串里只出现一次 `timestamp` 值"。
- `backend/tests/test_doudian_client_transport.py::test_retry_resigns`：捕获两次尝试的 `query["timestamp"]` 与 `query["sign"]`，断言**两者都变了**、`body` 未变。
- 代码评审可查：`doudian_client.call` 里 `timestamp` 只赋值一次（grep `timestamp =` 只命中一处）。

**为什么这样对生产更好**
签名是"客户端认为的请求"与"服务端看到的请求"的一次哈希承诺。任何一个字节不一致，成功率为 0，而且失败发生在**每一次**调用上——不是偶发抖动，是整条链路不可用。把它收敛成"一个字符串 + 一份固定材料清单"，等于把不可测的隐式耦合变成可测的纯函数（`signing.py` 没有 I/O，黄金向量锁死）。生产上这意味着：签名 bug 在 CI 就红，不会等到线上第一笔改价失败。

---

## ADR-002 检查点必须持久化：MemorySaver 让 HITL 形同虚设

**状态**：已决策

**原方案**：`build_graph(MemorySaver())`

**问题**
1. `MemorySaver`/`InMemorySaver` 把 checkpoint 存在**进程内的 dict**。进程重启（部署、OOM、崩溃、`--reload` 热重载）→ **所有挂起的 `interrupt` 全部消失**。
2. 用户在看到审批台待办后点"通过"：`GET /api/alerts` 有数据（来自 DB 索引），但 `POST /api/approve` 里 `graph.aget_state(thread_id)` 拿不到 checkpoint → `interrupts` 为空 → 只能 404。**审批台上有一堆待办，一个也点不动。**
3. 多副本/多 worker 更糟：worker A 挂起，worker B 接请求 → 永远 404。这不是规模问题，`uvicorn --workers 2` 就会触发。
4. 审计断了：LangGraph 的 checkpoint 是"图走到哪一步"的唯一权威记录，丢了就无法复盘"这条审批为什么没执行"。

**决定**（CONTRACT.md §5.7）
- dev：`AsyncSqliteSaver.from_conn_string("data/checkpoint.db")`（`langgraph.checkpoint.sqlite.aio`，3.1.2 实测存在该类与 `from_conn_string`）。
- prod：`AsyncPostgresSaver.from_conn_string(settings.checkpoint.url)`，与业务库同实例。
- 首次使用前 `await saver.setup()`（幂等）。
- `app/` 下**禁止**出现 `InMemorySaver`（只在 `tests/` 允许，用 `MemorySaver` 跑纯图逻辑）。
- `approval_request` 表只是**索引**（给 UI 查），权威状态在 checkpoint；两者不一致时以 checkpoint 为准并记 `audit`。

**影响（可验证）**
- `test_hitl_roundtrip.py::test_survives_process_restart`：`run_agent` 后**销毁 graph/client 实例**，用新 checkpointer 实例（同一文件）`resume_agent`，断言 `status=done`。
- `grep -rn "InMemorySaver\|MemorySaver" backend/app` 必须为空。
- 部署验证：`docker compose restart backend` 后 `POST /api/approve` 仍成功（README 的手工验证步骤里有一条）。

**为什么这样对生产更好**
HITL 的全部价值在于"人可以在几小时甚至几天后做决定"。把待审批状态放在内存里，等于把审批窗口限制在"进程不重启"这个事实上不可控的区间。持久化 checkpoint 把"待审批"变成一个**有耐久性保证的业务状态**，同时换来可水平扩容（任一副本都能接审批）、可重启（发布不丢审批）、可审计（状态迁移有历史）。代价是多一个存储对象和一次 `setup()`，收益是 HITL 从"演示"变成"能上生产"。

**风险/待核实**：`langgraph-checkpoint-postgres==3.2.0` 的 `AsyncPostgresSaver.from_conn_string` 签名未在本机实测（本轮只核实了 sqlite 包源码）。备用路径写入 CONTRACT.md §5.7：`AsyncPostgresSaver(AsyncConnectionPool(url, open=False))`。切换时只需改 `checkpoint.py` 一个函数。

---

## ADR-003 效果跟踪移出图：图内绝不等待 T+1/T+3/T+7

**状态**：已决策

**原方案**：在 `track_effects` 节点里 `socket.sleep` / `await asyncio.sleep` 等到 T+1、T+3、T+7 再采样效果。

**问题**
1. **一次图执行要活三天**。进程必须持续存活；任何重启/发布/扩缩容都让这次执行丢失，且恢复点不明。
2. 三天里的图状态是"半执行"的，无法回答"这条 run 结束了吗"——`status` 只能写 `executing`，前端看板永远是转圈。
3. 占用连接/超时预算/内存（每个挂起 run 一个协程 + 一份 state）。
4. 阻塞式等待**不可测**：测试要么等三天，要么把 sleep 打桩——打桩后就测不到真实的调度逻辑（`due_at` 算错、时区错、错过多窗口）。
5. 一个 run 里多个动作要等 4 个窗口 → 复杂度 O(动作数 × 窗口数) 的挂起协程。

**决定**（CONTRACT.md §5.9）
- `record_effect` 节点只做一件事：为每个**执行成功**的动作写 `effect_snapshot` 行（`offset_days ∈ windows_days`，`due_at = executed_at + offset`，`status="scheduled"`），然后立刻返回。
- `finalize` 里 `effect_report.windows[*].status = "scheduled"`，`conclusion = "效果窗口已登记，尚未到达评估时间（最早 T+1）。"`
- 真正的采样由**独立调度器**（`effects.scheduler_enabled`，`poll_interval_s`）按 `status="scheduled" AND due_at <= now` 拉取，写入 `baseline/followup/delta`，置 `evaluated`。
- 调度器与"`unknown` 动作对账"共用同一个 tick（减少一个调度器）。

**影响（可验证）**
- `test_effects_scheduler.py`：`record_effect` 节点耗时 < 1s（断言 wall clock），且 `effect_snapshot` 恰好 `len(成功动作数) × len(windows_days)` 行、全部 `scheduled`。
- 同一测试用 `now=fixture_time + 2d` 调 `run_effect_evaluator_once`，断言 T+0/T+1 变 `evaluated`、T+3/T+7 仍 `scheduled`（证明窗口判定正确）。
- 人可验证：`docker compose up` 后 `POST /api/approve` 立即返回 `status=done`，不再挂起。

**为什么这样对生产更好**
"等待"是最贵的资源，也是最不可靠的资源。把时间维度从**控制流**里剥出来，变成**数据**（`due_at` 一行），得到三个性质：图执行时间是秒级、效果评估可重放（同一行可反复评估）、调度器可独立扩缩/独立重启。这也是所有真实生产系统做延迟任务的标准形态——延迟任务进表，不进线程。附带好处：`effect_snapshot` 表本身就是效果评估的审计日志，能回答"这个动作我们到底有没有看效果、看了几次、什么时候看的"。

---

## ADR-004 一个进程一个 AsyncClient：连接池 + 令牌桶必须挂在同一个实例上

**状态**：已决策

**原方案**：每次请求 `async with httpx.AsyncClient() as c: await c.post(...)`，文档里另外声称有"令牌桶限流"。

**问题**
1. **没有连接池**：每次 TLS 握手 + TCP 建连。抖店是公网 HTTPS，握手 100–300ms，而改价接口本身可能只要 50ms —— 开销比业务大一个量级。
2. **文件描述符抖动**：高频调用下 TIME_WAIT 堆积，Windows 上更容易撞临时端口耗尽。
3. **限流是假的**：令牌桶的状态要么挂在 client 上（每次 new → 每次新桶，限流完全失效），要么挂全局变量（多协程竞争但没有连接级背压，桶通过了但连接还在排队）。文档的承诺与实现对不上，这是最糟的一类不一致——评审时无法自证。
4. 超时/重定向/UA/代理配置散落在每个调用点，改一处忘一处。
5. 无法在测试里注入 transport（`AsyncClient()` 每次新建，respx 之外没有稳定注入点）。

**决定**（CONTRACT.md §5.1）
- `DoudianClient.__init__` 创建**唯一**的 `httpx.AsyncClient`，接受 `transport` / `timeout` / `limits` 注入。
- `httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)`；`httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)`。
- 令牌桶 + 并发信号量**作为 client 实例的成员**，因此"限流"与"连接池"作用在同一层背压上。
- 生命周期在 FastAPI `lifespan` 里：启动建、关闭 `aclose()`。
- `clock` / `sleep` 可注入 → 限流测试用假时钟，零等待。

**影响（可验证）**
- `test_rate_limit.py`：假时钟 + 20 次 `call`，断言"虚拟耗时 ≥ `(20-burst)/rate_per_sec`"；断言并发峰值 ≤ `max_concurrency`（在 handler 里计数）。
- 性能对照（可复现，README 里给数字）：`ab`/简单循环 50 次调用，单例 vs 每请求新建，比较 p50 延迟与 `netstat` 的 TIME_WAIT 数。
- 代码评审：`grep -n "AsyncClient(" backend/app` 只命中 `doudian_client.py` 一处，且不在循环里。

**为什么这样对生产更好**
连接池与限流不是两个独立优化——它们必须共用同一个"容量"概念。分成两处，就会出现"桶放行 8 QPS，但连接池只允许 20 条、每条排队 2s"这种互相踩的情况。挂到同一实例上，退避、重试、背压、keep-alive 全部有一致的口径，可解释、可调参、可测。另外单例是注入 transport 的前提，直接决定"零凭据可测"（ADR-013）能不能成立。

---

## ADR-005 interrupt 恢复：用 `interrupt_id` 键映射 + 全量集合校验，绝不 zip 配对

**状态**：已决策

**原方案**
```python
resume = [d["verdict"] for d in decisions]
graph.invoke(Command(resume=resume), config)      # 或 dict(zip(action_ids, decisions))
```

**问题**
1. `zip` 的语义是**按最短的那个截断，且不报错**。审批 3 条、前端只提交 2 条 → 第 3 条静默丢弃；反之多提交则静默忽略。
2. 位置配对比"丢弃"更危险：前端按自己的顺序渲染、后端按另一个顺序 `zip`，**A 的审批被作用到 B 的动作上**。用户批准了"把 SKU-1 降价 3%"，系统执行"把 SKU-2 库存清零"。这是资损级 bug，而且日志上看每一步都"成功"。
3. 字段不匹配同理：`dict(zip(ids, decisions))` 里 decision 少了 `verdict` 键就变成"没有裁决"，走默认分支。
4. 恢复值用位置列表还有个致命点：**重放**。`interrupt()` 恢复会从节点开头重跑，若节点里有多个 `interrupt`，位置语义依赖执行顺序——只要中间加一行代码，映射就全错。

**决定**（CONTRACT.md §4.4）
- 恢复值固定形状：`{"type": "approval_decisions", "decisions": [Decision, ...]}`。
- 传递方式固定：`Command(resume={interrupt_id: payload})` —— **按 interrupt id 键映射**，LangGraph 1.2.16 的 `Command.resume` 支持 `dict[str, Any]`（键=interrupt id）与单值两种形态；`Interrupt` 对象带 `id` 字段（源码 `langgraph/types.py`：`id: str  """The ID of the interrupt. Can be used to resume the interrupt directly."""`）。
- 校验（任一不过 → 400 `VALIDATION_ERROR`）：`set(decisions.action_id) == set(pending_actions.action_id)`，**多/少/重复都不许**；`verdict` 合法；`edit` 必带通过 schema 的 `edited_params`；非 `edit` 必须 `edited_params is null`。
- 恢复前查 DB：任一 `action_id` 已非 `pending` → 409 `DUPLICATE_DECISION`（DB 是幂等闸门，不能靠"再 resume 一次试试"）。

**影响（可验证）**
- `test_hitl_roundtrip.py`：三个负例各自返回 400 且 `details` 指出缺失/多余/重复的 `action_id`；一个正例返回 `done`。
- 断言实现里 `Command(resume=` 的实参是 `dict`（不是 list）——一条 `assert isinstance(resume_arg, dict)` 的类型级测试，防止后来人"顺手改回列表"。
- `grep -rn "zip(" backend/app/agent` 必须为空。

**为什么这样对生产更好**
审批是**人与系统的合同**：3 条待批 → 必须 3 条有明确结论，没有"默认值"。把"缺一条"从"静默成功"变成"400 并且告诉缺哪条"，等于把一类不可见的数据丢失变成一次可见的接口错误。用 `interrupt_id` 而不是位置，也让恢复语义与代码行序解耦——节点里加/删逻辑不会改变已挂起审批的对应关系，这对"审批可能挂了三天、期间我们发了三个版本"的场景是必需的。

---

## ADR-006 幂等键必须在审批之后生成，且提议身份与执行身份分离

**状态**：已决策

**原方案**：提议生成时算 `idempotency_key = hash(action_type + target + params)`，审批通过后直接拿它去执行。

**问题**（两个方向都坏）
1. **键在审批前冻结**：运营在审批台把 9900 改成 9600（`verdict=edit`）。执行时若沿用旧键 → 新参数、旧键 → 与"9900 那次"的键相同 → 第二次执行被自己的幂等约束判成 `skipped_duplicate`，**改了价的请求根本没发出去，回执却是成功**。
2. **若键含旧参数**：同一次审批被重放（ADR-010）时算出两个不同键 → 同一个价格改动被执行两次。改价两次在抖店是"第二次覆盖第一次"，看起来没事；但库存清零、发券这类动作重复执行就是真事故。
3. 反过来，若键**不含参数**（只用 `type+target`），则"同一个 SKU 今天改两次价"会被永久去重，第二次永远不生效。
4. 键在审批前生成还有个审计缺陷：无法区分"用户批准的就是原方案"还是"用户改了参数"。

**决定**（CONTRACT.md §4.5）
- 三个标识分离：
  - `action_id = f"{run_id}:{action_type}:{target}"` —— **提议身份**，审批前生成，只用于"这次提议 ↔ 这条裁决"的对应关系，**不用于执行去重**。
  - `payload_digest = sha256(canonical_json({shop_id, action_type, target, params_effective}))` —— 落在 `approval_request.payload_digest`，用于审计"参数在审批时被改过"。
  - `idempotency_key = sha256(f"{shop_id}|{action_type}|{target}|{canonical_json(params_effective)}")` —— **执行身份**，在 `approve` 落地之后、`action_log` INSERT 之前计算；`action_log.idempotency_key` 上有 UNIQUE。
- `params_effective = verdict==edit ? edited_params : proposal.params`，且必须先过 §5.5 的 `normalize_params`（金额转 `*_cents` int、去 `None` 键），保证"9900 与 9900.0 与 '9900'"归一成同一个键。
- 重复执行返回 `status="skipped_duplicate"` 且 HTTP 200（幂等成功，不是错误）。

**影响（可验证）**
- `test_idempotency_key.py`：`edit 9900→9600` 后的键 ≠ 改前键（断言两个 64 hex 不相等）；同参数执行两次，第二次 `status="skipped_duplicate"` 且 `action_log` 只 1 行。
- `test_idempotency_key.py::test_same_params_different_run`：同 SKU 同参数、不同 `run_id` → 键**相同**（按 `shop_id+type+target+params` 算，不含 run_id）→ 第二次被去重。这是预期的：同一店铺同一参数的动作本来就不该执行两次。
- 断言 `canonical_json` 对 `{price_cents: 9900}` 与插入顺序不同的等价 dict 输出同字节。

**为什么这样对生产更好**
幂等的本质是"重放同一个**已定稿**的意图，只生效一次"。参数在审批台被改，意图就换了——键必须跟着换。把"提议身份"和"执行身份"拆开，让两者各管一段：前者回答"谁批的这一条"，后者回答"这个动作执行过没有"。合在一起必然有一头错。代价是多一次哈希、多一列，换来的是"改价不会静默丢失执行"和"重放不会重复执行"这两个同时成立——这俩在单键方案里是互斥的。

---

## ADR-007 抖店签名算法按官方 SDK 核实后重写（HMAC-SHA256 + 规范串）

**状态**：`待沙箱实测`（算法与格式已按官方 SDK 源码核实；真实 app_key 下的字节级一致性未实测）

**原方案**：`sign = md5(app_secret + "".join(sorted(params.items())))` 这类"简化串接"。

**问题**
1. 抖店开放平台 v2 的签名**不是**对业务参数做 canonical 排序串，而是对**协议字段**做固定顺序拼接：
   `app_key{ak}method{method}param_json{param_json}timestamp{ts}v{v}`。业务参数只以 `param_json`（一个已 `sort_keys` 的 JSON 字符串）的身份参与。按业务参数自己拼串，服务端重算必然不匹配。
2. **算法不是 MD5 而是 HMAC-SHA256**，且 `app_secret` 既是 HMAC 的 key，又被前后各拼一次进消息。
3. `param_json` 的字节形态有要求：`json.dumps(params, sort_keys=True, separators=(',',':'))`（紧凑分隔符、无空格、UTF-8 不转义）。用默认 `json.dumps`（带 `", "` 和 `": "` 空格）签出来的串与服务端不一致 → 签名错误。
4. `timestamp` 是 `%Y-%m-%d %H:%M:%S` 格式的**字符串**（含空格），不是 epoch 数字；它同时出现在签名串和 URL 里。
5. 请求方式：POST 到 `/{method.replace('.','/')}`，`param_json` 放在 **body**，其余协议字段在 query。把 `app_key/access_token/v` 塞进 body 的写法与协议不符（见 ADR-001）。

**核实依据**
- 官方 Python SDK `doudian==0.6`（PyPI，源码 `github.com/minibear2021/doudian` 的 `doudian/core.py`）：
  - `_sign`：`canonical = 'app_key{}method{}param_json{}timestamp{}v{}'.format(app_key, method, param_json, timestamp, version)`，`sign_pattern = app_secret + canonical + app_secret`，`_hash_hmac(sign_pattern)`。
  - `_hash_hmac`：`HMAC(key=app_secret.encode(), SHA256()).update(pattern.encode()).finalize().hex()`。
  - `_request`：`timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')`；`param_json = json.dumps(params, sort_keys=True, separators=(',', ':'))`；`method = path[1:].replace('/', '.')`；URL 携带 `app_key/method/access_token/timestamp/v/sign_method=hmac-sha256/sign`，body = `param_json`。
  - 网关：prod `https://openapi-fxg.jinritemai.com`，sandbox `https://openapi-sandbox.jinritemai.com`；`v=2`。
- 回调验签（另一套，别混）：`event_sign = md5(app_key + body_text + app_secret)`，请求头 `app-id` / `event-sign`。

**决定**（CONTRACT.md §5.2）
- 按上述算法实现 `app/services/signing.py`（纯函数），`sign_method` 只支持 `hmac-sha256`；请求其它值抛 `DoudianSignatureError`（**不猜 MD5 变体**，没有官方样例的实现等于埋雷）。
- `param_json` 必须过 `normalize_param_json`（`sort_keys=True, separators=(',',':'), ensure_ascii=False`）。
- 三个黄金向量写进 `tests/fixtures/doudian/signing_vectors.json` 并断言（实现漂移立刻变红）。

**影响（可验证）**
- `test_signing.py` 三个向量全绿；改成任一字段顺序/分隔符，测试必须变红（这是一条"测试有效"的自检）。
- 与真实平台对齐的验收动作（README 的"接入真实店铺"章节）：用 sandbox 凭据调 `shop.getShopInfo`，期望 `err_no=0`。未做之前本 ADR 保持 `待沙箱实测`。

**为什么这样对生产更好**
签名是唯一"出问题就 100% 失败且信息量极低"的环节：服务端只会告诉你 `err_no=10008 签名错误`，不会告诉你差在哪一个字节。按官方实现 + 黄金向量锁死，等于把这个不可调试的黑盒变成一组可断言的字节。反过来，凭"简化串接"上线，第一次联调就会卡在签名上，而且没有任何本地手段能定位——这是最典型的"省 20 行代码，赔两天联调"。

**待核实（明确列出，不当成已解决）**
| 项 | 现状 | 风险 |
|---|---|---|
| 真实 app_key 下该串是否被接受 | 未实测（无凭据、无网访问抖店） | 若平台对 `timestamp` 时区/URL 编码有额外要求，需微调 |
| 是否存在 `sign_method=md5` 的历史模式 | 未核实 | 当前实现显式拒绝，不会静默算错 |
| `err_no` vs 新文档的 `code` 字段名 | SDK 用 `err_no`，部分新文档用 `code` | 客户端按 `err_no` 解析，若无 `err_no` 有 `code` 则用 `code`（已写入 §5.1 解析规则）|
| 各接口的具体 QPS 限额 | 未核实（按 app 分级，需后台查看） | 默认 8 QPS 保守；撞限流时会有 `rate_limited` 分类与 `Retry-After` 退避，不会硬失败 |

---

## ADR-008 效果归因：没有 A/B 就不许说"提升"

**状态**：已决策

**原方案**：`effect_report = {"gmv_change": "+8.2%", "message": "改价带来 GMV 提升 8.2%"}`

**问题**
1. 单店、单次改价、前后对比，**没有任何对照组**。GMV 变化里混着：自然波动、周末效应、平台活动、竞品调价、流量分配、季节、上次改价的滞后效应。把这些归因给"本次改价"是**统计上不成立**的断言。
2. 这种文案会进周报、会进复盘、会影响下一次决策。错一次之后，后续所有决策都建立在错误因果上——比没有效果评估更糟。
3. `delta` 只给一个百分比，不说是**哪个窗口**、**哪个基线**、**样本量多少**。同一批数据可以算出 +8%（对比上周同日）和 −3%（对比近 7 日均值）。
4. 前端把 `+8.2%` 渲染成绿色上升箭头，等于给这个不可靠结论加了一层"系统认可"的背书。

**决定**（CONTRACT.md §5.9.3）
- 新增 `attribution` 字段，取值只允许 `single_arm_before_after` / `ab_test` / `none`，**默认必须是 `single_arm_before_after`**（我们是单臂前后对比，"none" 留给连基线都不稳的情况）。
- `conclusion` 必须给出：窗口、指标、变化量、**基线定义**，并且必须带否定句。模板：
  `"T+1 观察窗内 GMV 环比 -12.4%（基线：前 7 日同时段均值），未做对照实验，不能归因于本次改价。"`
- `caveats` 至少两条：外部因素可能、归因强度弱。
- **测试断言**（`test_attribution_wording.py`）：`attribution != "ab_test"` 时 `conclusion` 不得包含 `提升 / 带来 / 导致 / 因为 / 归因于本次`（允许"不能归因于本次"这种否定式，用白名单排除）。
- 前端展示时必须同时展示 `attribution` 徽标（"弱归因/无对照"），不许只显示涨跌箭头。

**影响（可验证）**
- 上述测试用例在 CI 跑；把 `conclusion` 改成"改价带来 GMV 提升"立刻红。
- 人可验证：`GET /api/approve` 的响应里 `effect_report.attribution` 与 `caveats` 恒存在（不是 optional）。
- 数据上：`effect_snapshot.delta` 记录 `{"baseline_definition": "...", "window": "T+1", "metric": "gmv_cents", "delta_ratio": -0.124}`，任何结论都能回溯到基线定义。

**为什么这样对生产更好**
一个运营系统的产出不是"数字"，是"决策依据"。给出无法支撑的因果断言，会把系统变成"随机 reinforce 错误策略的机器"。把归因强度做成**一等公民字段**，让机器输出的事实与机器输出的判断在结构上分开：事实（窗口、数值、基线）可以自动算，判断（因果）必须由人确认或被 A/B 支撑。这在面试/评审里的价值也最高——它证明作者知道"能算出来的"和"能下结论的"是两件事。

---

## ADR-009 金额一律整数分；时间一律 UTC；业务日期单独用 Asia/Shanghai

**状态**：已决策

**原方案**：`gmv = 128400.0`（元，float），`timestamp = datetime.now()`（naive 本地时间）。

**问题**
1. float 金额：`0.1 + 0.2 != 0.3`。累加几百条订单后出现 `128399.99999999999`，四舍五入到分时偶发差 1 分；对账时永远解释不清。抖店部分接口返回"元"字符串，部分返回"分"整数，混用必错。
2. naive 本地时间：`datetime.now()` 在容器里是 UTC，在开发机是 +8，同一条记录在两处解释不同；写进 SQLite 后无 tzinfo，切 Postgres（`timestamptz`）后读出来带 tz —— 同一段代码两种行为。
3. **业务日界与时间戳是两个概念**：抖店的日报按当地自然日（Asia/Shanghai）切分，"10 月 23 日 GMV"指 23 日 00:00–24:00 +08:00。用 UTC 切日界，每天会漏掉 8 小时数据，且异常检测的"当日"基准整体偏移。

**决定**（CONTRACT.md §0 + §6.3）
- 金额：状态/DB/API/前端**一律整数分**，字段名 `*_cents`。归一化在 `DoudianClient` 边界完成（`_yuan` → `*_cents`）；出现 float 金额直接 `ValueError`（在边界炸，不静默转换）。
- 时间戳：一律 `datetime.now(timezone.utc)`，序列化成 `...Z`；DB 列 `DateTime(timezone=True)`；读出来一律过 `to_utc()`（naive 视为 UTC）。
- 业务日期：`date` 字段是 `Asia/Shanghai` 的 `YYYY-MM-DD`，由 `zoneinfo.ZoneInfo("Asia/Shanghai")` 计算，**不**用 UTC 切日界。
- 图内的 `now` 全部可注入（`now: datetime | None`），测试用固定时刻。

**影响（可验证）**
- `test_metrics.py`：`compute_metrics` 对固定 fixture 的 `aov_cents` 断言为精确整数；构造 1000 条 0.1 元订单的用例断言 `gmv_cents == 10000`（float 实现会得到 9999 或 10001 的浮点误差被 `round` 掩盖，用断言锁死）。
- `test_db_portability.py`：写入后读出的 `dt.tzinfo is not None`；UTC 往返相等。
- `test_business_date.py`：`2026-10-23T16:30:00Z`（= 24 日 00:30 +08）必须落在业务日期 `2026-10-24`。

**为什么这样对生产更好**
金额与时间是电商系统里唯一两样"错了会在财务上体现"的东西。整数分消除了浮点误差这一类不可复现的 bug；显式时区消除了"开发机对、容器里错"这一类只在生产出现、本地永远复现不了的 bug；业务日期独立定义让"日报"与"事件时间"各归各位。这三条都是零成本约定，但违反其中任何一条，后期修复都要动数据库和所有下游报表。

---

## ADR-010 LangGraph 会在 resume 时重放节点：所有副作用必须幂等，列表字段禁用 append reducer

**状态**：已决策

**原方案**：`AgentState` 用 `Annotated[list[X], operator.add]` 累积 `execution_results`/`errors`；节点里"先写 DB、再 `interrupt()`"。

**问题**
1. LangGraph 的 `interrupt` 是"抛异常 + 从**节点开头**重跑"。源码 `langgraph/types.py` 的 `interrupt` docstring 明写：*"The graph resumes from the start of the node, **re-executing** all logic."*
2. 所以 `approval_gate` 的写库逻辑会在恢复时**再执行一次**。若 upsert 覆盖 `status`，已裁决的记录会被写回 `pending` → 审批台出现"批完又回来"的幽灵待办，且第二次 `POST /api/approve` 会被 409 挡住，形成死锁。
3. `operator.add` reducer + 重放 = 同一批回执被追加两次 → `execution_results` 出现重复行、前端统计翻倍。
4. 更隐蔽：节点里 `now()`、`random()`、`uuid4()` 在重放时得到不同值 → 幂等键不稳定（与 ADR-006 联动）。

**决定**（CONTRACT.md §3.1 / §4.3 / §5.8）
- **所有 state 字段默认 replace 语义**，禁止 `operator.add` 与自定义 append reducer；节点返回"完整目标列表"。
- 审计事件走 `append_audit(state, entry)`，按 `event_id`（内容哈希）去重后返回**完整新列表**。
- `approval_gate` 的三段顺序固定：① `upsert_proposals`（**insert-if-absent，绝不回写 status**）② 通知 ③ `interrupt()` ④ 恢复后校验 + `mark_decided`。
- `approval_request` 的身份就是 `action_id`（UNIQUE），**不引入** `approval_id` 这层别名（会让 ID 里出现两次 `run_id`，且两个字段永远同值）；`interrupt_id` 因 `interrupt()` 阻塞而无法在首次落库时写入，改为恢复后按 `interrupt_id=''` 条件回填。
- 节点内一切"会变的值"（时间、id）必须来自 state 或可注入的 `now`。

**影响（可验证）**
- `test_approval_replay_idempotent.py`：手工让 `approval_gate` 执行两次（先 `interrupt` 捕获，再 resume），断言 `approval_request` 行数不变、`status` 仍为 `approved`（不是 `pending`）。
- `grep -rn "operator.add\|Annotated\[list" backend/app/agent` 必须为空。
- 一条元测试：对同一 state 连续两次调用 `append_audit`（同一 entry），长度不变。

**为什么这样对生产更好**
"节点可能被执行多次"是任何持久化工作流引擎的基本事实（Temporal/Step Functions 都如此），但 LangGraph 把它藏在 `interrupt` 语义里，不写进契约就一定会踩。把幂等责任放在**每个副作用点**（insert-if-absent / UNIQUE 约束 / 内容哈希去重），而不是寄希望于"只执行一次"，得到一个可安全重放的图：崩溃、超时、人工重试、发布期间 resume，都不会产生重复或状态回退。这也是为什么本项目的幂等键全部落到 DB 唯一约束上——约束是唯一不依赖代码正确性的保证。

---

## ADR-011 错误两级分类；可变操作只对 429 重试；不确定的结果记为 unknown 并对账

**状态**：已决策

**原方案**：`try: r = call(); except Exception: retry(3)` + 统一按 HTTP 状态码判断成功。

**问题**
1. **抖店的失败大多是 HTTP 200 + body `err_no != 0`**。只看状态码会把"签名错误""无权限""库存不足"全部当成功，静默丢弃失败。
2. 无差别重试把**不可恢复**错误也重试：签名错误重试 3 次 → 3 倍告警噪音 + 3 倍延迟，最后仍失败。
3. 相反，把 `timeout` 从重试里去掉又太保守——但**对可变操作重试是真的危险**：改价请求超时，服务端可能已经改成功了，重试就是改两次（第二次覆盖第一次，成本翻倍；对发券/清零则是重复事故）。
4. `err_no` 业务错误没有落到"动作级"：一个动作失败不应该让整个 run 失败。

**决定**（CONTRACT.md §9）
- 两级分类：HTTP 层（timeout/connection/5xx/429/4xx）+ 业务层（`err_no`）。9 个 `ErrorCategory` 枚举值，每个带 `retryable` 语义与处理动作表。
- 重试集合：`{timeout, connection, server_5xx, rate_limited}`。**`mutating=True` 时收缩为 `{rate_limited}`**。
- `mutating` 遇到 `timeout/connection/5xx` → 抛 `DoudianAmbiguousOutcomeError`（category `ambiguous`）→ `action_log.status="unknown"` → **不重试**，交给对账（读上游真实状态判断意图是否生效），最多补一次重试，超过 3 次保持 unknown 等人处理。
- `auth` 错误：强制刷 token **一次**再试；再失败整个 run `failed`（token 全失效时继续跑没有意义）。
- `signature` 错误：不重试、不刷 token、直接告警（这是部署事故）。
- `business` 错误：该动作 `failed`，run 继续执行下一个动作。
- 每次重试**重算 timestamp 与 sign**（ADR-001）。

**影响（可验证）**
- `test_doudian_client_transport.py`：5xx → 恰好 3 次尝试后抛 `DoudianUpstreamError`（handler 计数断言）；`err_no=10008` → **1 次**尝试 + `DoudianSignatureError`；`mutating=True` + timeout → `DoudianAmbiguousOutcomeError` 且仅 1 次尝试。
- `test_effects_scheduler.py::test_reconcile_unknown`：`unknown` 行 + 上游状态已生效 → 置 `succeeded` 且 `response.reconciled=true`。
- 断言 `action_log.status` 取值集合 ⊆ `ActionLogStatus`。

**为什么这样对生产更好**
"能不能重试"取决于**语义**（这个操作重复执行会不会造成额外影响），而不是**错误类型**好不好看。把这条规则写进 client 的签名（`mutating` 参数），让调用方在写代码的瞬间就必须回答"我这个操作重试安全吗"，而不是在 catch 块里赌。`unknown` 这个状态是诚实性的核心：不知道就是不知道，读回来对账比赌一次重试好——因为赌错了的代价是重复执行真实业务动作。

---

## ADR-012 SQLite 开发 / Postgres 生产，代码零改动（可移植性规则）

**状态**：已决策

**原方案**：`engine = create_engine("sqlite:///./app.db")` + `INSERT OR REPLACE` + `server_default=func.now()` + `BigInteger` 主键，文档承诺"生产切 PG 不改代码"。

**问题**（每一条都是切库当天才会炸的）
1. `INSERT OR REPLACE` / `ON CONFLICT` 是方言语法，PG 不认（SQLite 也不支持 `ON CONFLICT DO UPDATE` 的全部形式）。
2. `server_default=func.now()`：SQLite 写 UTC 字符串、无 tz；PG 写 `timestamptz`。同一段代码两种时间语义。
3. `sa.BigInteger` 主键 + `autoincrement=True`：**SQLite 的 AUTOINCREMENT 只对 `INTEGER PRIMARY KEY` 生效**，用 BIGINT 建表后主键不自增，插入直接 `NOT NULL constraint failed`。
4. `sa.JSON` vs `JSONB` vs `sqlite.JSON`：选错就要改列类型（= 迁移）。
5. 表达式索引 / 部分索引（`WHERE status='pending'`）：SQLite 与 PG 语法不同。
6. `strftime`/`json_extract`/`||` 拼接：方言函数。
7. SQLite 默认 `journal_mode=DELETE`，并发读写会 `database is locked`；而"并发"在这个系统里是常态（API + 图 + 调度器）。

**决定**（CONTRACT.md §6.3）
- 只用 `sa.JSON`、`String(n)`、`Text`、`BigInteger().with_variant(Integer, "sqlite")`、`DateTime(timezone=True)`、`SmallInteger`。
- 时间默认值一律 Python 侧 callable（`default=_utcnow`），禁用 `server_default`/`CURRENT_TIMESTAMP`。
- 冲突处理统一「先 SELECT 后 INSERT，捕获 `IntegrityError` 再重查」——两种方言行为一致。
- 禁用所有方言专有 SQL 与表达式/部分索引；只允许一个 `PRAGMA`：`journal_mode=WAL`（仅 sqlite）。
- 代码里禁止 `if "sqlite" in url:` 分支（除 WAL 与 `with_variant`），切库只改配置。

**影响（可验证）**
- `test_db_portability.py`：四张表在 SQLite 上 `create_all` 成功；重复插入触发 `IntegrityError` 后重查成功且行数仍为 1；`to_utc` 往返；`BigInteger().with_variant` 自增插入可用（这条直接防 §3）。
- 静态检查：`grep -rnE "INSERT OR REPLACE|ON CONFLICT|json_extract|strftime|server_default" backend/app` 为空。
- 切库验收（README 里给命令）：`docker compose --profile pg up`，`DATABASE_URL=postgresql+psycopg://...` + `CHECKPOINT_TYPE=postgres`，跑同一套 `pytest` 与 e2e 脚本，**不改一行代码**。

**为什么这样对生产更好**
"开发用 SQLite、生产用 PG"是极常见的形态，而它失败的方式永远一致：开发全绿，上线建表/插入/时间全错。可移植性不是"抽象层写得好"，而是**禁用一组具体语法**。把这些禁令写成契约条款 + grep 断言，成本几乎为零，收益是把一次上线事故提前到 CI。WAL 那一条则是把"本地开发偶尔卡住"这个不可复现的困扰直接消灭。

---

## ADR-013 离线可测性：transport 注入 + 固定 fixtures + 黄金向量，零凭据跑全链路

**状态**：已决策

**原方案**：测试里 `monkeypatch.setattr(requests, "post", fake)`，或用 `unittest.mock.patch` 打 client 方法；无凭据时"跑一下试试"。

**问题**
1. monkeypatch 哪个函数取决于实现细节（`requests.post`？`httpx.AsyncClient.send`？），重构一次就全废，且**测试通过不代表真的请求形状对**（patch 掉的层可能就是出错的那层）。
2. 打 client 方法（`patch.object(DoudianClient, "call")`）等于把被测对象换成桩——签名、序列化、重试、限流全测不到，"全链路"是假的。
3. 无凭据时无法启动服务 → 面试/评审时对方跑不起来 = 项目不可验证。
4. 网络一旦可访问，测试就变成"依赖公网的集成测试"，慢且不稳定。

**决定**（CONTRACT.md §10）
- `DoudianClient` 接受 `transport: httpx.AsyncBaseTransport | None`，**这是主注入点**（不依赖任何 monkeypatch）。测试传 `httpx.MockTransport(handler)`。
- `create_doudian_client(settings)` 在 `doudian.dry_run=true` 时自动用 fixtures 构造 `MockTransport` 注入，且**不读凭据**——服务可以零凭据启动，全链路（采集→指标→异常→提议→审批→执行→效果登记）跑通。
- fixtures 是**数据文件**（`backend/tests/fixtures/doudian/cassettes/*.json`），带声明式匹配规则（method 全等 + body 子集）与确定性顺序；无命中返回 599 让失败显式。
- respx 仅作为"不传 transport 时"的第二路径，**禁止与注入混用**（谁先匹配谁赢，结果不可重复）。
- 状态机的纯逻辑测试用 `MemorySaver`（只在 `tests/`），持久化测试用文件型 saver。

**影响（可验证）**
- 一条命令跑全绿：`python -m pytest backend/tests -q`，**不设任何环境变量**。
- `test_doudian_client_transport.py` 断言请求形状：`content-type`、query 里 7 个协议字段、body 恰为 `param_json`、`sign` 是 64 位小写 hex。
- e2e：`uvicorn` 起来后 `POST /api/agent/run` + `POST /api/approve` 完整走完，`GET /api/logs` 能看到 `doudian.call` 事件。

**为什么这样对生产更好**
可测性的关键不是覆盖率，是**注入点选在哪一层**。选在最外层（transport）时，测试覆盖的路径 = 生产的路径（签名、序列化、重试、限流全在里面），而桩只替换"字节怎么进网络"。这带来两个生产收益：一是它能证明"我们发出的请求长什么样"（这正是签名类 bug 的现场）；二是它是唯一能让"无凭据的人"验证系统的形态——这在协作与交接里等价于"可复现"。顺带把 respx/monkeypatch 混用的不确定性明确禁掉，避免出现"本地过、CI 不过、重跑又过"的测试。

---

## ADR-014 规则表达式不提供任何函数调用（白名单 AST 里没有 `Call`）

**状态**：已决策

**原方案**：`eval(rule["when"], {"__builtins__": {}}, metrics)`，或白名单里保留 `Call` 以便写 `abs(x)`。

**问题**
1. `eval` 即使清空 `__builtins__` 也能被 `().__class__.__base__.__subclasses__()` 这类链逃逸；禁 `eval` 是必须的。
2. 但"禁了 eval、白名单却允许 `Call`"等于**半开门**：`Call` 让表达式可以调用命名空间里的任何可调用对象，一旦某个指标的 value 是对象而非数字，就有 `getattr`/`__import__` 的现实路径。白名单只要放行一个 `Call`，攻击面就回到"所有可调用物"。
3. 规则文件是**运营可编辑**的（不是代码），这意味着它属于不可信输入。对不可信输入执行带调用的解释器，是把配置面变成 RCE 面。
4. 我们真正需要的语义只有：数值比较、`and/or/not`、四则运算、`is None`、`in`。**没有任何一条需要函数调用。**

**决定**（CONTRACT.md §8.3）
- 白名单 AST：`Expression/BoolOp/UnaryOp/BinOp/Compare/Constant/Name/Tuple/List` + 对应算子节点 + `Load`。
  显式**禁止**：`Call`、`Attribute`、`Subscript`、`Lambda`、推导式、`IfExp`、`NamedExpr`、f-string、以及任何 `__dunder__` 名称。
- 手写递归下降求值器（`CompiledExpr.evaluate`），**不** `eval`/`exec`/`compile`/`ast.literal_eval`；`ast.parse(mode="eval")` 只用于建树。
- **加载期**（`load_rules`）完成：语法检查、AST 白名单检查、**所有 `Name` ∈ 冻结命名空间**。任一失败 → `RuleLoadError`（带 `rule_id`、位置、消息），启动即失败。
- 运行期：`None` 参与数值比较 → `False`（不抛、不静默 True）；布尔参与算术 → `RuleEvalError`；除零/NaN → `RuleEvalError`；结果必须是 `bool`。`RuleEvalError` 只跳过该规则并记录，不炸整个 run。

**影响（可验证）**
- `test_rules.py`：`"__import__('os').system('echo 1')"`、`"abs(x) > 1"`、`"x.__class__"`、`"x[0] > 1"`、`"(lambda: 1)()"`、`"[i for i in x]"` 全部 `RuleLoadError`。
- 未知字段 `"gmv_typo > 1"` → `RuleLoadError`（**加载期**，不是运行期）。
- `"1/0 > 1"` → 加载期通过、运行期 `RuleEvalError`、规则被跳过且有可见记录。
- `grep -rn "eval(\|exec(\|literal_eval" backend/app/services/rules.py` 为空。

**为什么这样对生产更好**
安全边界要画在"能力"上，不是"字符串黑名单"上。去掉 `Call` 之后，表达式语言在理论上就是**全函数、无副作用、无 I/O** 的：它能做的事情只有"比较几个数字和一个常量"，即使攻击者完全控制规则文件，也拿不到任何能力（没有对象访问、没有调用、没有下标）。代价是运营不能写 `round(x, 2)` 或 `abs(x)`——这个代价用预计算指标（`gmv_delta_ratio` 等）就能补回来，而且预计算还让表达式更容易审计。同时"加载期校验未知字段"把规则写错从"线上静默不触发"变成"启动就报错"，这在异常检测系统里是决定性的：一条永远不触发的规则，比一条报错的规则危险得多。

---

## ADR-015 运行摘要进表、节点 trace 进 JSONL；两者由同一个包装器维护

**状态**：已决策（v1.1.0，CONTRACT §4.7 / §6.1 / §7.6.1）

**原方案**：`agent_traces` 里塞节点级 trace —— 每个节点执行写一行（或一行里堆 JSON 数组），字段含 `node` / `duration_ms` / `input` / `output` / `error`，靠 `run_id` 关联回 run。

**问题**
1. **写放大与索引 churn**：一次 run 8 个节点、`interrupt` 重放后 9 次执行，每次一条带索引的 INSERT；`INDEX(run_id)` / `INDEX(created_at)` 的 B-tree 页被高频改写。这正是 SQLite 上最容易触发 `database is locked` 的模式（WAL 也只能缓解）。
2. **查询形态不匹配**：节点 trace 的真实查询是「某 run 最近 N 条」「最近失败节点」——这是 **append-only 日志 + tail** 的形态，不是关系查询。用表反而要为分页、保留期、清理各写一套逻辑。
3. **不做 join 与聚合**：没人会 `JOIN node_trace ON agent_traces` 去算业务指标；也没有"按节点聚合"的需求。表的价值（关系、约束、聚合）一样用不上。
4. **与 run 摘要混在一张表**：`agent_traces` 被设计成"一次 run 一行"（`UNIQUE(shop_id, run_id)`），一个 run 多行 trace 会直接破坏这个唯一约束的语义，只能退化成"run 摘要 + 明细"两张表——那就回到了现在的方案，只是晚一步。
5. 保留期不同：run 摘要要长期留（看板、复盘），节点 trace 只需要几天（排查用）。同表同生命周期是浪费。

**决定**（CONTRACT §4.7 / §6.1 / §7.6.1）
- `agent_traces` **只做运行摘要**：一次 run 一行，只承载 `status` / `step_count` / `updated_at` / `error` 这类**要能被 SQL 查、被看板聚合**的状态。
- 节点级 trace 走 **JSONL**：`event="node.trace"`，字段 `trace_id` / `node` / `duration_ms` / `input_digest` / `output_digest` / `error`（`digest` 是 16 位 sha256 前缀，**不存原文**；原文在 LangGraph checkpoint 里，日志不重复承载）。脱敏沿用 `settings.observability.redact_keys`（**v1.4.0 起值级层用 `DEFAULT_SCRUB_KEYS ∪ redact_keys`，见 ADR-020**；键名层仍直接用 `redact_keys`，两层在同一次递归里完成）。
- 写入点唯一：`app/agent/tracing.py` 的 `traced()` 包装器。它一次调用里做两件事——① append JSONL ② 同一事务把 `step_count` +1 并更新 `status`/`updated_at`。
- **不变量**：`count(JSONL 中 run_id=X 且 event=="node.trace") == agent_traces[X].step_count`。
- 崩溃窗口不假装原子：JSONL 是 `step_count` 的**事实源**，表里是**物化值**；`reconcile_step_count()` 在 `finalize` 前与进程启动时（对所有非终态 run）以 JSONL 计数校准，差异记 `audit`。
- 除 `tracing.py` 外任何地方不得写 `step_count`。可断言的形态见 CONTRACT §4.7 的 A/B′/C 三条（v1.3.0 改写：**按"定义/写入"形态匹配，不按名字出现次数**——字面 `grep "step_count"` 会命中 `nodes.py` 的 `import reconcile_step_count`，那种断言无论实现多干净都过不了，只会逼出"把 reconcile 从 finalize 挪走"的错误修法）。

**影响（可验证）**
- `test_trace_invariant.py`：一次带审批的 run 结束后，`count(event=="node.trace") == step_count`，且 `step_count == 9`（8 节点 + `approval_gate` 重放一次）；人为把 DB 的 `step_count` 改成 3，`reconcile_step_count` 能校准回 9。
- 静态断言（CONTRACT §4.7，v1.3.0 形态）：`grep -rnE 'step_count\s*=[^=]|step_count\s*\+=' backend/app --include=*.py` 只命中 `tracing.py`；`grep -rn '__tablename__' backend/app --include=*.py` 恰好 4 行全在 `models.py`；`grep -rn 'class AgentRuntime' backend/app` 恰好 1 行（ADR-017）。
- `GET /api/logs?run_id=X&event=node.trace&limit=50` 返回该 run 的节点事件，形状与 §7.6.1 逐字一致（`test_api_contract.py` 校验）。
- 表行数守恒：同一 run 无论执行几次，`agent_traces` 恒为 1 行（`UNIQUE(shop_id, run_id)` 兜底）。

**为什么这样对生产更好**
两类数据有不同的写入频率、查询形态、保留期，硬塞进一张表必然要在一头妥协：要么牺牲摘要表的查询性能（写放大、索引 churn），要么牺牲 trace 的完整性（截断、丢字段）。按"访问模式"分家是唯一自洽的切法：**要 SQL 聚合的进表，要 tail 的进日志**。但分家立刻带来"两份真相"的风险——所以这条 ADR 的重点不在分家，而在**把两个写入点收敛成一个包装器 + 一条可断言的不变量**。没有那条不变量，分家就是给自己埋一个"计数永远对不上"的坑；有了它，任何一次不一致都会被测试或启动对账抓出来。这也是本 ADR 在评审里最值得讲的一点：拆分本身不产生复杂度，**拆完没有守恒关系**才产生复杂度。

---

## ADR-016 控制流中断 ≠ 失败：混在一个字段里，告警就永久失去意义

**状态**：已决策（v1.2.0，CONTRACT §4.7 / §7.6.1 / §11）

**原方案（v1.1.0 §4.7 规则 5）**：节点抛异常 → `data.error={"type","message"}`、`level="error"`、原样 re-raise。判定"失败"的依据是"有没有异常抛出来"。

**问题**
1. **HITL 挂起本身就是抛异常**：`approval_gate` 第一次执行靠 `interrupt()` 抛 `GraphInterrupt` 挂起（CONTRACT §4.4）。按原方案，每一次**正常**的"等人审批"都会在 `data/logs/app.jsonl` 与 `GET /api/logs` 里留下一条 `level="error"` 的红色事件。
2. **告警被自己淹没**：真失败（`collect` 抛 `DoudianUpstreamError`、`GraphRecursionError`）与正常挂起在日志里长得一模一样。运维只有两条路——要么忽略 error（那告警就没用了），要么每次挂起都报警（那就是噪声）。**告警一旦长期为红，等于没有告警**。
3. **看板把流程状态画成故障**：执行日志页按 `level` 分色时，"等待审批"的 run 满屏红色，而它恰恰是**该人工介入的正常状态**。UI 在告诉使用者"系统坏了"，实际上系统在等他点按钮。
4. **把控制流塞进错误通道，语义上就不成立**：`GraphInterrupt` 是 LangGraph 用来**向上传递控制权**的机制（挂起/排空/父命令），它不是一个失败信号。用"是否抛异常"来判失败，是把**实现手段**当成了**语义**。同一类错误的根源在 v1.1.0 就已经出现过一次（§4.3 规则 2 要求节点 `except Exception` 显式排除 `GraphInterrupt`）——两处都在替"异常 ≠ 失败"打补丁，说明缺的是**一个显式的语义字段**，不是更多的特判。
5. **升级风险**：`GraphBubbleUp` 的成员会变（langgraph 1.2.16 下为 `GraphInterrupt` / `GraphDrained` / `ParentCommand`）。只捕 `GraphInterrupt` 的实现，在框架新增一种控制流异常时会静默退化成"把它当失败"。

**决定**（CONTRACT §4.7 / §7.6.1）
- `node.trace` 的 `data` 里新增**必填**三态字段 `outcome ∈ {"ok", "interrupted", "error"}`（`data` 内新增，**不动顶层字段**，`/api/logs` 响应 schema 仍只有一套）。
- 分流顺序**先控制流、后失败**，顺序即语义：
  1. `except GraphBubbleUp:` → `outcome="interrupted"`、`error=None`、`level="info"`，然后**原样 re-raise**（绝不吞）。必须捕**基类** `GraphBubbleUp`，不许只捕 `GraphInterrupt`。
  2. `except Exception as exc:` → `outcome="error"`、`level="error"`、`error={"type": type(exc).__name__, "message": redact(str(exc))}`，然后原样 re-raise。`GraphRecursionError`（`RecursionError` 子类，**不是** `GraphBubbleUp`）落这一支——它是真失败。
  3. 正常返回 → `outcome="ok"`、`error=None`、`level="info"`。
- **`outcome` ↔ `level` 是硬规则**：`ok`→`info`、`interrupted`→`info`、`error`→`error`。判 `level` 只许看 `outcome`，不许看"有没有异常抛出来"。
- `step_count` 不变量不变：每次**实际执行**仍恰好一条事件，**计数不看 outcome**（挂起也是一次真实执行，`approval_gate` 重放后仍是 9 次）。
- 前端按 `data.outcome` 分色，不按 `level` 分色；`interrupted` 用中性态（"等待审批"），只有 `error` 才是红。

**影响（可验证）**
- `test_trace_invariant.py`：带审批的 run 结束后，`approval_gate` 那条事件 `data.outcome=="interrupted"`、`level=="info"`、`data.error is None`；`collect` 抛 `DoudianUpstreamError` 时 `outcome=="error"` 且 `level=="error"`；`GraphRecursionError` 归 `error` 不归 `interrupted`（CONTRACT §10.3）。
- `count(event=="node.trace") == agent_traces.step_count` 仍成立且与 outcome 分布无关。
- `GET /api/logs?level=error` 在一次**只挂起、无失败**的 run 上返回 0 条 —— 这是"告警还有意义"的可执行定义。
- `grep -rn "GraphInterrupt" backend/app/agent` 只允许命中 `tracing.py` 的 `except GraphBubbleUp` 说明与节点内"显式排除"两处；不得出现"捕获后当失败处理"的写法。

**为什么这样对生产更好**
一个字段同时承载"控制流状态"和"失败原因"时，它就只能取二者之一——于是无论取哪个，另一方的信息都会丢。这里丢的恰好是**告警的信噪比**：错误日志的价值不在"有 error 字样"，而在"error 出现时它一定是坏的"。把 `interrupted` 单独标出来，`level="error"` 才重新变成一个可信的信号；同时挂起态获得了自己的语义（等待人工），前端可以据此显示"等待审批"而不是故障。代价是 `data` 多一个键、多一条分支判断——**代价是局部的，收益是全局的**（整个日志/告警链路恢复可用）。这也是本 ADR 最值得在评审里讲的一点：**不要把控制流当异常处理**，两者共享 `raise` 这个机制，但语义正交；区分它们的成本是一次 `except` 分支，不区分它们的成本是永久失去告警。

---

## ADR-017 装配层与仓储层必须分文件：一个随部署形态变，一个随表结构变

**状态**：已决策（v1.3.0，CONTRACT §0.1 / §4.7 / §4.8 / §11）

**原方案（v1.2.0 的空白）**：`backend/app/agent/adapters.py` 已经存在，但 §0.1 的目录树里没有它。于是它没有定性、没有归属、没有禁令，实现了两件事：进程级运行时装配（`AgentRuntime` / `build_runtime` / `get_runtime` / `resolve_client` / `resolve_now` / `digest_of`）**和一张表的仓储**（`agent_traces` / `action_log` 的 `db_*` 写入函数）。

**问题**
1. **两个写入点，同一个 `+= 1`**：`adapters.py` 里的 `agent_traces` 仓储与 `tracing.py` 的仓储是两处独立实现，直接撞 §4.7 的「`step_count` 唯一写入点」不变量。不变量靠"大家都记得只在一个地方写"是守不住的，它需要一个**结构性**理由让人不可能在第二个地方写。
2. **测试替身会把写入点带进测试路径**：装配层是测试最先替换的东西（假 client、固定时钟、tmp 日志汇）。当仓储和装配在同一个模块里，替换装配就等于把仓储一起放进测试的 import 路径——于是"测试里那次写入"和"生产写入"是两份代码，覆盖率高但覆盖的不是同一条路径。
3. **变更驱动方向相反**：装配层随**部署形态**变（进程内 lifespan / 容器 / 测试替身 / 多个 worker），仓储层随**表结构**变（加列、加索引、换方言）。合成一个文件时，加一列会牵动 lifespan 的装配代码，换部署形态又会碰数据访问——每次改动都在两个变化速率不同的轴上同时施加影响。
4. **复制装配层 = 进程级单例变多个**（决定性的一条）：装配层没有归属就没有"只有一份"的约束。实际后果已经发生——`AgentRuntime` 曾在 `adapters.py` 与 `nodes.py` 各有一份，各自带一个模块级 `_runtime`。`app.main` lifespan 往 A 的单例注入 client，节点经 B 的单例读，B 会懒构建**自己那份**：于是 e2e 依然"跑通"，生产上却是两个连接池、两套限流桶（ADR-004 本意全废）、shutdown 只关掉一个。**这类 bug 不会在小流量测试里暴露，它只在生产放大。**
5. **没有仲裁依据**：目录树是"谁能写哪个文件"的唯一依据。树里没有的文件，下一轮谁写、谁改、谁删都没有依据；归属空白处不会保持空白，它会长出恰好没人负责的代码。

**决定**（CONTRACT §0.1 / §4.7 / §4.8）
- §0.1 目录树补 `app/agent/adapters.py`，归属记给 **agent-core**（与 `state/graph/nodes/checkpoint/tracing` 同组）。
- §4.8 写死职责：**只做进程级运行时装配与依赖解析**（`AgentRuntime` / `build_runtime` / `get_runtime` / `set_runtime` / `reset_runtime` / `resolve_runtime` / `resolve_client` / `resolve_now` / `digest_of` / log sink 装配），因为 §4.3 冻结了节点签名 `(state, config)`，节点只能经由它取依赖。
- §4.8 禁令：**不得持有任何表的仓储**、**不得出现任何表的写入逻辑**、**不得写 `step_count`**、**不得有业务逻辑**、**不得有第二处 `class AgentRuntime`**、**节点不得自建依赖**。
- 每条禁令配一条**机器能证的**断言（§4.7 A/B′/C）或**只能人审的**检查项（§4.8 禁令 1/2 由 reviewer 追调用链），两类分开写，不让 grep 假装覆盖一切。
- 表 → 唯一仓储位置的映射冻结在 §4.8：`agent_traces`→`tracing.py`、`action_log`→`nodes.py`、`approval_request`→`services/approvals.py`、`effect_snapshot`→`services/effects.py`。

**影响（可验证）**
- `grep -rn 'class AgentRuntime' backend/app` → 恰好 1 行（`adapters.py`）。落盘实测（2026-10-24 01:11，代码 mtime 静止后）：1 行。
- `grep -rn '__tablename__' backend/app --include=*.py` → 恰好 4 行全在 `models.py`。落盘实测：4 行。
- `grep -rnE 'step_count\s*=[^=]|step_count\s*\+=' backend/app --include=*.py` → 只命中 `tracing.py`（3 行）。落盘实测：3 行全在 `tracing.py`。
- `grep -rn "insert_\|update_\|select(\|session" backend/app/agent/adapters.py` 在代码上为空（docstring 提及不计）。
- `test_static_invariants.py`（§10.3）把上面三条钉成 CI 断言。

**为什么这样对生产更好**
两个部件的变化速率不同，就不该共享一个文件的修改边界——这不是审美，是把"改动的影响半径"限制在它真正要影响的范围里。但本 ADR 的重点不是分文件，而是**分文件之后给每一边补一条唯一性**：装配层要有"只有一个单例定义"的约束（否则复制它就等于把注入静默废掉），仓储层要有"只有一处写入"的约束（否则计数迟早对不上）。这两条唯一性各自可断言，而断言又必须落在"定义/写入"的形态上——用名字出现次数去数，只会制造假阳性，把实现者逼向"为了过 grep 而挪走正确逻辑"的错误修法（§4.7 记录了那个反面例子）。**契约的作用不是让人猜不出边界，而是让越界在 CI 和评审里都留下痕迹。**

---

## ADR-018 SKU 维度必须进契约：店铺级均值会把"亏本卖"稀释成看不见

**状态**：已决策（v1.4.0，CONTRACT §3.3 / §5.3.1 / §5.4 / §8.3.3 / §8.5 / §7.3）

**原方案（v1.3.0 及以前的契约）**：`MetricsSnapshot` 是**店铺级**单条（12 个字段：GMV / 订单数 / 退款率 / UV / CVR / 7 日均值…），没有任何 SKU 维度的载体。而 `§8.3.3` 的命名空间里已经摆着 `stock` / `current_price_cents` / `stock_days` / `is_on_sale` 四个 **SKU 级名字**——**名字在、载体不在**，也没有"按 SKU 逐条求值"的契约。全仓没有 `cost`、没有 `gross_margin`。

**问题（按严重度排序）**

1. **头号场景写不出来**。立项时的原话是"老板担心有货在亏本卖"。这句话在旧契约里**无法表达**：没有成本字段 → 没有毛利率 → "亏本"不是一个可计算的谓词。不是"少三个指标"，是这个产品**没有立项理由**。契约缺口比实现债危险：实现债会被 debug 出来，契约缺口会被"已知限制"永远糊过去。
2. **店铺级均值会把它稀释掉（这条才是本质）**。假设店铺有 3 个 SKU：A 售价 100 / 成本 120（亏 20），B 售价 200 / 成本 140（赚 60），C 售价 50 / 成本 30（赚 20）。今天各卖 10 件：
   - 店铺级毛利率 = `(3500 - 2900) / 3500 = 17.1%` —— **看起来很健康**，任何"店铺毛利率 < 10%"的规则都不会响；
   - 而 SKU A 的毛利率 = `-20%`，它是**实打实在亏钱**，而且卖得越多亏得越多。
   均值是**加性**的：一个爆款可以永久掩盖任意多个亏损 SKU。用店铺级指标做"亏本"告警，**在数学上就不可能触发**——不是阈值没调好，是聚合把信号消掉了。
3. **命名空间自相矛盾**。`stock`/`stock_days` 是 SKU 级事实，却要求用店铺级上下文求值。实现者只有三条路：把某个 SKU 的值当店铺值（错）、按某个隐式顺序取第一个（更错）、或者悄悄忽略这四条规则（最常见）。**契约里的一个不可能满足的条款，最终一定变成"没人用的条款"**。
4. **没有成本 → 也就没有"该不该降价"的依据**。这次缺的不是报表字段，是**动作的判据**：`price_update` 的提议必须有"降到多少还赚钱"的边界，否则 Agent 会把亏损品降得更亏。
5. **成本是唯一不能从上游拿到的数据**。抖店开放平台没有成本价，它是**商家侧自维护**的数据。这意味着它必须显式建模（配置文件 + 缺失语义），而不可能"等接口有了再说"。

**决定**（CONTRACT §3.3 / §5.3.1 / §5.4 / §8.3.3 / §8.5）
- 新增 `SkuSnapshot`（12 字段）作为 SKU 级载体，`MetricsSnapshot.skus: list[SkuSnapshot]`，**按 `sku_id` 升序**（确定性排序，重放/哈希/diff 稳定）。
- 成本来自 `config/sku_costs.yaml`（§8.5）：**缺失 → `cost_cents=None` → `gross_margin=None`**，规则**不触发**，**不填 0**。分母为 0（`current_price_cents<=0` / `avg_daily_sales_7d==0`）同样返回 `None`，绝不返回 0（沿用 §5.3 的既有风格）。
- 求值分**两轮**：店铺轮（15 个店铺级名字）+ 每 SKU 一轮（15 ∪ 该 SKU 的 10 个名字 = 25 个键）。轮次由**规则引用的名字集合**判定，两个集合**互斥**（`load_rules` 断言），因此判定总有唯一答案，**不需要也不允许**在 `rules.yaml` 里加 `scope` 字段。
- `Anomaly` 加 `sku_id: str | None`；`anomaly_id = sha256(f"{run_id}|{rule_id}|{metric}|{sku_id or ''}")[:16]` —— **`sku_id` 必须进哈希**，否则同一条规则命中 5 个 SKU 会撞出同一个 id，审批面板 5 张卡片共享一个 `action_id`，批一次全吞。
- **`None` 与"求值出错"分开记**：`None` 参与比较 → `False`（数据不足，正常未命中，**不进 skipped**）；除零/类型错/bool 参与算术/NaN → `RuleEvalError`（**进 skipped**，`data.reason` 分别为 `none_operand` / `rule_eval_error`）。
- SKU 级指标**不出 HTTP 面**（§7.5 `/api/metrics` 仍是店铺级；`Anomaly` 从不出现在响应里）。SKU 信息经 `ActionProposal.target`（= `sku_id`）与 `evidence.sku_id` → `/api/alerts` 让运营看到。**先把"能判"做对，再谈"能看"**；SKU 明细看板需要先做指标持久化（新表或 JSONL 汇），那是另一条 ADR。

**影响（可验证）**
- `test_sku_metrics.py`：3 个 SKU → `metrics.skus` 3 条且升序；**无成本配置 → `gross_margin is None`（不是 0、不是 1.0）**；`avg_daily_sales_7d=0` → `stock_days is None`；`sales_7d=0` → `avg_daily_sales_7d=0.0`（真值，不是缺失）。
- `test_sku_anomalies.py`：一条 SKU 规则命中 3 个 SKU → 3 条 `Anomaly`、**3 个不同的 `anomaly_id`**；店铺级规则 `sku_id is None`；`gross_margin is None` 时不触发且不进 skipped。
- `grep -rn "cost_cents or 0\|gross_margin or 0" backend/app` 为空（禁止把缺失当 0）。
- `grep -rn '__tablename__' backend/app --include=*.py` 仍是 **4 行**（成本与 SKU 明细都不进 DB，§6.1/§8.5）。
- 反面对照（面试时最好用的那个）：把上面 2 的例子代进旧契约 → 店铺毛利率 17.1%（健康），SKU A −20%（亏）。**同一份数据，一个看健康、一个看亏损**——这就是"聚合层级选错"的全部代价。

**为什么这样对生产更好**
告警系统的价值只取决于一件事：**它能不能在正确的粒度上评估**。粒度错了，阈值再准也没用——店铺级均值把所有 SKU 的损益加成一个数，等于把"哪个商品在亏钱"这个信息**在聚合时扔掉**，之后再怎么调参都找不回来。把 SKU 维度放进契约，本质是**把决策粒度对齐到业务事实的粒度**：成本、售价、库存、动销都是 SKU 级事实，那么"要不要降价/下架"的判断就必须在 SKU 级做出。代价是契约变大（多一个结构、多一轮求值、多一个配置文件），收益是整个产品**第一次能回答立项时的那个问题**。
本 ADR 还固化了一条更一般的取向：**缺失数据的语义必须是"不知道"，不是"零"**。成本没配、动销为零、广告数据没接，三者在数值上都可能被写成 0，但业务含义完全不同——把"不知道"写成 0，会让 Agent 对着一个它并不了解的 SKU 采取真实世界的动作（改价、下架），这是这类系统里**唯一真正不可逆的错**。

---

## ADR-019 失败的第二条来源：用"状态迁移"判节点失败，而不是让每个节点抛异常

**状态**：已决策（v1.3.1，CONTRACT §4.7 规则 5c2 / §7.6.1）

**原方案（v1.2.0 §4.7 规则 5）**：`outcome` 只有三条分支——抛 `GraphBubbleUp` → `interrupted`，抛其它异常 → `error`，正常返回 → `ok`。判定"这次执行失败"的唯一依据是"有没有异常抛出来"。

**问题**
1. **图里本来就有一条"正常边"在承载失败**。§4.2 的 `collect --(fail)--> finalize` 是一条**条件边**：`collect` 硬失败（订单数据缺失 = 指标的唯一来源没了）时的实现是"记 `errors`、把 `status` 写成 `failed`、**正常返回**"，图层据此走条件边去收口。它不抛异常，于是按 v1.2.0 的规则被记成 `outcome="ok"` + `level="info"`——**一次真失败在日志页上是绿灯**，`GET /api/logs?level=error` 也查不到它。
2. **这不是 `collect` 一处的偶然**。`compute_metrics` / `detect_anomalies` / `propose_actions` 的硬失败分支是**同一个写法**（写 `failed` 后正常返回，下游节点靠 `_aborted` 短路）。ADR-016 刚把"控制流 ≠ 失败"修好，这里冒出来的是同一个病的第二面：**状态层宣告的失败 ≠ 异常通道抛出的失败**。只修一面，另一面继续撒谎。
3. **只改 `collect` 是补丁，不是根因修复**。让它改抛异常，等于往图里塞一个契约里没有的捕获节点（LangGraph 没有"异常 → 边"的原生机制，只能自己包一层），而另外三个节点继续把失败记成绿灯——**缺陷会换个位置再来一次**（"越修 bug 越多"的典型路径）。
4. **告警的信噪比再次被吃掉**。v1.2.0 之后 `level="error"` 已经能可信地表示"抛了异常"，但"没抛异常却失败了"这一类仍然不可见。运维看到 `collect outcome=ok`、run 却是 `failed`，只能去翻 state/DB 才知道为什么——日志不但没回答问题，还制造了矛盾。
5. **判据选错会把收口节点染红**。若改成"返回值里带 `errors` 或 `status=failed` 就算失败"，那么 `finalize` 在**已经失败**的 run 上收尾时也带 `status=failed`，每个失败 run 都会长出第二盏红灯。失败被重复计数，等于又一次稀释信号——和 ADR-016 要修的问题是同一个形状。

**决定**（CONTRACT §4.7 规则 5c2 / §7.6.1）
- `traced()` 认**两条**失败来源：①抛非 `GraphBubbleUp` 异常（规则 5b，v1.2.0 已有，不变）；②**正常返回，但把 run 从非 `failed` 推进到 `failed`**（规则 5c2，v1.3.1）。
- 判据是**状态迁移**（`state.status != failed` 且 `update.status == failed`），不是"返回值里有没有 `errors`"。已在 `failed` 的 run 上收尾或跳过（`finalize` 的收口、`_aborted` 的早退）一律不算，**`finalize` 因此不会被连坐成第二个 error**。
- 规则 5c2 的形状：`outcome="error"`、`level="error"`、`output_digest=None`、`data.error={"type": "NodeDeclaredFailure", "message": redact(本次 update 里最后一条 AgentError.message)}`。`data.error` 的键仍是 `{type, message}`、`outcome` 仍是三态——**JSONL 形状不变**（§7.6.1）。
- `step_count` 与 `agent_traces.status` 的写入行为**完全不变**：`collect` 早在 v1.3.0 就已经带着 `status=failed` 落库，本版只改"那条事件在日志里被记成什么"。

**影响（可验证）**
- `test_node_semantics.py::test_non_retryable_collect_error_is_not_retried`：`collect` 那条事件 `outcome=="error"`、`level=="error"`、`output_digest is None`、`error.type=="NodeDeclaredFailure"` 且 `message` 含"订单数据缺失"；紧随其后的 `finalize` 仍是 `outcome=="ok"` / `level=="info"`（收口不染红）。
- `test_graph_paths.py::test_no_anomaly_ends_without_approval`：正常直通（无异常、无失败）的 run，全部事件仍是 `outcome=="ok"` —— 新分支不影响成功路径。
- `test_trace_invariant.py` 的 `count(node.trace) == agent_traces.step_count` 不变量不受影响（计数不看 outcome，ADR-016）。
- 机械可查：`grep -rn "NodeDeclaredFailure" backend/app` **只命中 `tracing.py`**（常量定义与使用处）；不得出现在各节点里——节点只负责"写 status"，不负责"决定日志语义"。

**为什么这样对生产更好**
"失败"在这套系统里有两条物理通道：**异常**（抛出即中断）与**状态**（写 `status=failed` 后走条件边收口）。只认其中一条，另一条上的失败就永久不可见——而"看不见的失败"比"误报"贵得多：误报会被人骂到改掉，漏报会安静地让数据错下去。修法只有两种取向：把第二条通道改造成第一条（节点必须抛异常），或者让**观察点同时认两条**。前者的代价是一个契约里没有的捕获节点 + 四个节点的写法改造 + 以后每加一个节点都要记得"必须抛异常才被记为失败"；后者只改 §4.7 里那**唯一的写入点**，一处修完全局生效，且不动任何签名、表列、状态迁移。选后者还有一条更一般的理由：**观察点应当比被观察者更宽容**。节点用什么方式表达失败是它自己的事（抛异常或写状态都合法），把两者归一成同一个 `outcome` 是日志层的职责；反过来要求所有业务节点"必须抛异常才能被记为失败"，等于把日志层的实现细节反向钉进每个节点。

---

## ADR-020 脱敏必须两层，且必须测通道

**状态**：已决策（v1.3.3，CONTRACT §7.6 / §7.6.2 / §10.3）

**原方案（v1.3.2 及以前）**：脱敏只有**一层**——`redact(value, keys)` 递归进 dict，**键名**命中 `redact_keys` 就把该键的值换成 `"***"`；`JsonlLogSink.write()` 对 `data` 调它，对 `message` 原样写入。而契约 §7.6 / §7.6.1、DECISIONS、以及 `tracing.py` 的 docstring 里共有 **5 处**写着"已脱敏"/"过 `redact_keys`"/"日志不承载凭据"，读起来像一条已经成立的保证。

**问题**

1. **对 `str` 是 no-op（决定性的一条）**。**旧版**的 `_redact_inner` 只有三道有用的分支：Mapping（看键名）、list/tuple（递归）、str（**原样返回**），其余落 else 也原样返回。于是 `redact("access_token=SECRET-abc123", ("access_token",))` 返回的仍是那串明文。
2. **真正会泄露的通道恰好是 `str`**。上游错误原文是 `DoudianUpstreamError("… access_token=SECRET-abc123 …")`，它被 `traced()` 写进 `data.error.message`（§4.7 规则 5b/5c2），被 `JsonlHandler` 写进 `message`。两条都是字符串，两次"脱敏"都是空转。实测：整条 JSONL 里 `SECRET-abc123` 原样落盘（REVIEW-core F1 / 探针 C）。
3. **声明与实现脱节，且脱节了 5 处**。这 5 处的措辞都不假，但**没有一处成立**。这是比 bug 更坏的一种状态：**读到声明的人不会再去检查**——评审、运维、后来的实现者都以为这件事已经做完了。
4. **唯一门禁测的是函数，不是通道**。已有的 15 条用例全部只调 `redact()/redact_any()`。把 `write()` 里那行 scrub 整段删掉，它们**照样全绿**——门禁与泄露路径错位，等于对真实泄露路径零覆盖。
5. **`stderr` 是第二条通道，而且在 `record` 层遮不住**。容器/journald 收的是格式化后的文本；给 `LogRecord` 的 `msg`/`args` 做处理只是插值的第一步，`asctime`/`levelname`/traceback 还在后面拼，遮完落地的仍是明文。
6. **同一件事有两个名字**。`redact`（只做键名）与 `redact_any`（两层）并存时，读代码的人无法从名字判断"这个会不会遮字符串"——同名不同义会把坑重新掩盖一次。

**决定**（CONTRACT §7.6 / §7.6.2）

- **键名层 + 值级层在同一次递归里完成**，唯一入口 `redact_any()`；`redact()` 保留为**兼容别名**（语义完全相同）。不新增第二种调用方式，也就不存在"只调了一层"的写法。
- 键名层：`redact_keys`（默认 `app_secret`/`access_token`/`sign`/`authorization`/`cookie`）命中的 dict 键，**整个值**（不论类型、深度）换成 `"***"`。
- 值级层：任意字符串里的 `key=value` / `key: value` / `"key":"value"` 的**值**换成 `***`；键集 = `DEFAULT_SCRUB_KEYS` ∪ `redact_keys`（**并集，只增不减**——显式传参不许把默认键集替掉，否则"越指定越不安全"）。值形态五种（双引号/单引号标量、单层对象、单层数组、裸值）；对象与数组**整段**遮成 `"***"` 以保 JSON 合法；标量沿用原引号；`_`/`-` 互通（HTTP 头用连字符、SDK 参数用下划线）；左边界只排除字母数字（`refresh_token=` 要命中，`design=1` 不许命中）；键名按长度倒序匹配；`:` 形态的裸值必须停在下一个 `key=` 之前。
- **通道统一**：`JsonlLogSink.write()` 在**每一行落盘前**对 `message` 与 `data` 都过统一入口；`stderr` 走 `_ScrubbingFormatter`（**先 `format()` 再 scrub**）。
- **边界写进契约**（§7.6.2 的"已知不覆盖 5 条"）：URL 编码键名、跨行续行、非 `str` 键、自引用结构、无 `key=` 前缀的散文。不做"看起来很全"的承诺。
- **通道级测试 + 反向验证进 CI**（§10.3）：断言以"落盘文件里查不到明文"为准，并且要求"删掉 `write()` 里的 scrub 后必须变红"。

**影响（可验证）**

- `pytest backend/tests/platform/test_platform_redaction_channel.py backend/tests/platform/test_platform_redaction_scrub.py` → **31 passed**（2026-10-24 实测，`exit code 0`）。
- 通道级三条：`test_sink_write_scrubs_message`（`message` 带 `access_token=…` → 文件里查不到）、`test_sink_write_scrubs_data_values`、`test_sink_write_scrubs_dict_key`（键名层仍然活着，值级层不能取代它）。
- **反向验证**（判断"测试到底在测什么"的唯一办法）：删掉 `logging.py` 里 `"message": _scrub_text(message, self._scrub_patterns)` → 第一条**必须变红**；把 `"data": redact_any(dict(data or {}), self._redact_keys)` 换成 `dict(data or {})` → 后两条**必须变红**。仍然全绿 = 测试测错东西了。
- `test_platform_redaction_scrub.py` 的误伤回归：`design=1`、`sign_method=hmac-sha256`、`specification: 8`、`token_type=Bearer` 必须**原样返回**——遮错了比漏遮更难发现（正常字段变成 `***`，排障时无从下手）。
- 契约侧自查：`grep -n "已脱敏\|必须脱敏" docs/CONTRACT.md docs/DECISIONS.md` 命中的**每一处**都要能指到 §7.6.2 的某一层或某条"已知不覆盖"；指不到的就是一条新的悬空声明。
- 代码侧自查：`grep -n "_scrub_text\|redact_any" backend/app/observability/logging.py` → `write()` 里两个入口各一处（`message` 走 `_scrub_text`，`data` 走 `redact_any`），`_ScrubbingFormatter.format` 一处。

**为什么这样对生产更好**

凭据泄露是**不可逆**的：日志 append-only、被采集、进备份、进 git 历史、进别人的截图。事后擦除等于已经泄露，所以唯一有效的位置是**写入前**，而且必须落在**每一行都必经的那个函数**里——不能靠每个调用点各自记得（"记得"不是机制）。
两层不是"多加一道保险"，而是**两个不同的作用域**：键名层管结构化数据，值级层管自由文本，任何一层单独存在都有一整类输入 100% 漏遮。判断标准很硬：把错误原文写成 dict（`{"message": "…"}`）和写成裸字符串（`message="…"`）必须都被遮住——**一个正确性取决于"调用方恰好用哪种写法"的脱敏，不算脱敏**。
测试必须钉在通道上，因为这是唯一能证伪"我们真的脱敏了"的断言：测被调函数只证明"函数会遮"，证明不了"落盘的东西被遮"，而两者之间那一行（`write()` 里的调用）恰恰是事故现场。最后，**承认边界比承诺全覆盖更安全**：写清哪 5 类不覆盖，后来者才知道该躲什么（别把凭据拼进散文、别打印多行 header）；而"已脱敏"这句话一旦被相信，就再没人会去躲。

**被否方案**

| 备选 | 否决理由 |
|---|---|
| ① **只做键名层**（保持现状，把 5 处声明改成"只遮 dict 键名"） | 它把结论改成"我们不遮 `message`"——但 `message` 恰恰是上游错误原文的落点，也就是**唯一真的会泄露的那条通道**。等于用文档免责替代修复；而且 `data.error.message` 是 §7.6.1 冻结的字段，声明"这个字段不脱敏"等于契约自己承认会落凭据。 |
| ② **只做值级层**（删掉键名层，统一用文本正则） | `data={"access_token": SECRET}` 是 dict 键，正则只认文本里的 `=`/`:`。要让它命中就得先 `json.dumps` 再遮再 `loads`——为了少一层，把"结构化数据"变成"字符串再解析回来"，既多一次序列化/反序列化，又会因 `sort_keys`/编码差异改变下游看到的值（`/api/logs` 与 §12 的 `LogItem` 都依赖原样透传）。 |
| ③ **在 `record` 层脱敏 `stderr`**（处理 `LogRecord` 的 `msg`/`args`） | `%(message)s` 的最终文本只有 `format()` 之后才存在：`record.getMessage()` 只是第一次插值，formatter 之后还要拼 `asctime`/`levelname`/`name`，并把 `exc_info` 的 traceback 追加进来（traceback 里同样会有请求参数）。在 record 层遮的是"还没成为最终文本的东西"，容器/journald 收到的仍是完整明文。唯一正确的位置是 `format()` 之后。 |
| ④ **事故修完就收工，测试继续只测函数** | 门禁与泄露路径错位：最典型的回归（有人把 `write()` 里的那行删掉/重构掉）不会被任何用例发现——实测那 15 条函数级用例对它全绿。测试要么覆盖通道，要么等于没测。 |
| ⑤ **把 `redact` 直接改造成两层、名字保持不变**（不引入 `redact_any`） | 旧名会让"对 `str` 调它等于没调"这个坑继续被同一个名字掩盖：名字没变、语义变了，读到 `redact(str)` 的人无法判断这是修复前还是修复后。别名保留兼容 + 新入口用新名，是不制造"同名不同义"的最省做法。 |

---

## ADR-021 成本口径分两级：先店铺级加权，明确不做"猜 SKU"

**状态**：已决策（v1.5.0，CONTRACT §3.3 / §5.3 / §7.5 / §8.3 / §8.3.3 / §8.5 / §13）

**原方案（v1.4.0 及以前的契约）**：ADR-018 已把 SKU 维度与成本表写进契约，但落地形态是**一步到位到 SKU 级**——`MetricsSnapshot.skus` + 每个 SKU 一条 `SkuSnapshot.gross_margin` + 两轮求值 + SKU 级规则 + SKU 级动作 target。而 v1.4.0 的实际状态是"已入册、代码未实现"：`grep -rn "gross_margin" backend/` 为 **0**，`backend/config/sku_costs.yaml` **不存在**。同时 §8.5 把"成本表的键必须能在 `raw.products` 里对上"写成**启动期 `ConfigError`**。

**问题**

1. **"SKU 级一步到位"把首版能力锁死在一个大改上**。SKU 级要同时具备：`skus` 派生、两轮求值、`Anomaly.sku_id`、`anomaly_id` 含 sku、SKU 级动作 target 解析。任一环没落地，`gross_margin` **一个数都出不来**。而"老板担心有货在亏本卖"这件事，首版真正需要的只是一个**诚实的店铺级信号**：有成本覆盖的那部分成交额，整体是不是在亏。
2. **店铺级信号有天然的误读风险，必须显式建模**。只对有成本的 SKU 加权时，`gross_margin = -0.5` 可能只代表 **5%** 的成交额。**不把覆盖率一起给出，这个数会被读成"全店在亏"**——一个只覆盖 5% 的数被当成全店结论去动价，就是误伤。
3. **店铺级结论指不出具体 SKU（这条直接决定了动作面）**。`price_update` / `stock_update` / `coupon_create` 都要求**确定的 `target`**；店铺级异常只能靠 `target_from` 挑（`top_gmv_sku` / `lowest_stock_sku`）——那是**猜**。而猜错的代价**不对称**：给 GMV 最高的好货降价或下架，等于用一次误报去伤害店里最赚钱的商品，且**不可回滚**（价格改了、库存清零了，已成交订单收不回来）。
4. **§8.5 的启动期校验物理上做不到**。启动期（`app.main` lifespan）根本没有 `raw`——商品列表来自 `collect` 的一次上游调用。要把静态交叉校验放进启动流程，等于让**进程启动依赖抖店可用性**。一条永远无法执行的条款，最后一定变成"没人用的条款"（§8.3.3 的同一个教训）。
5. **`gross_margin` 这个名字已经被 §8.3.3 的 SKU 名字集合占着**。店铺级要用同名，就必须**现在**处理"同名不同义"，否则规则作者会把为全店调的阈值套到单品上——那不是风格问题，是**让阈值悄悄改变含义**的问题。

**决定**（CONTRACT §3.3 / §5.3 / §7.5 / §8.3 / §8.3.3 / §8.5）

- **成本口径分两级，先做店铺级**：`MetricsSnapshot` 加 `gross_margin` / `cost_coverage_ratio` 两个**店铺级**字段（`float 或 None`），公式只对**有成本的 SKU** 加权：
  - `covered_revenue = Σ by_sku[s].gmv_cents`（`s ∈ cost_table`）、`covered_cost = Σ cost_table[s] × by_sku[s].units`；
  - `gross_margin = (covered_revenue − covered_cost) / covered_revenue`；
  - `cost_coverage_ratio = covered_revenue / gmv_cents`，**不裁剪**（`> 1` 用于暴露上游汇总与明细对不上）。
- **新增数据面 `order_summary["by_sku"]`**（`collect` 归集，`{sku_id: {gmv_cents, units}}`）：只统计**已支付**订单行；无 `sku_id` 的已支付行**只进 GMV 分母**（计为未覆盖，绝不摊到别的 SKU 上）；**退款单仍计入**成交额与件数；件数缺失按 **1 件**（方向偏保守：只会漏报"亏本"，不会把好货判成亏本）；件数非 `int` / 负数 → `ValueError`。
- **`None` 严格不是 `0`**：没有 `by_sku` / 一个有成本的 SKU 都没有 / 分母为 0 → `None`；`None` 参与数值比较为 `False` → 规则**不触发**、**不进 skipped**。
- **`loss_making` 只告警、不给 `actions`**（`severity=critical`、`metric=gross_margin`、条件含 `cost_coverage_ratio >= 0.6`）。理由见问题 3；等 SKU 轮次落地后，这条规则应改为 **SKU 级**并对亏损 SKU 出 `price_update`（**提价**，不是降价）或停售。
- **命名裁定**：`gross_margin` 归**店铺级**；**SKU 轮次落地时 SKU 侧必须叫 `sku_gross_margin`**。两集合互斥断言一旦触发，**必须报明确错误并指向 §8.3.3 本条**——不允许静默失败、不允许"取其一"式自动改名。
- **§8.5 的启动期校验改写为运行期审计**：`compute_metrics` 节点在**本次商品列表非空**时记 `sku_costs.unmatched`（**只记不阻断**，一条过期配置不该让当天所有指标都算不出来）；商品列表为空时**不报**（否则每一条成本都会"看起来对不上"，一串假警报把真问题淹掉）。原"启动期 `ConfigError`"条文**删除**。
- **诚实登记缺口（不许读成"已完成"）**：①**前端 TS 已同步**（v1.5.0 落地后结清，不再是缺口）——`types.ts` 加了两个可选字段、§12 逐字块同一次提交改完、看板改为**零计算读后端字段**，契约外字段 `cost_cents` 与那套 `(GMV−退款−成本)/(GMV−退款)` 公式**已删除**，并加了"只给 `cost_cents` 时必须算不出数"的反向断言锁死回潮；②**alert-only 的 anomaly 当时不出现在 `/api/alerts`**（该端点读 `approval_request` 表，只有产生审批行的异常才可见），因此运营**从审批台看不到** `loss_making`。**这一条后来按根因修了**：审计事件（`anomaly.detected` / `metrics.computed` / `sku_costs.unmatched`）现在**同时落 JSONL sink**，`/api/logs?event=anomaly.` 与 `?level=error` 都能查到；但"面向运营的独立告警出口"仍未做，仍是设计债。

**影响（可验证）**

- `pytest backend/tests/platform/test_platform_gross_margin.py` → **34 passed**（2026-10-24 实测，`exit code 0`）。关键几条：`test_all_skus_costed_margin_matches_hand_calculation`（全店都有成本 → 与手算一致）、`test_partial_cost_only_uses_costed_skus_and_exposes_coverage`（只对部分 SKU 加权且覆盖率可见）、`test_no_cost_at_all_is_none_and_never_triggers_loss_making`（一个成本都没有 → `None` 且规则不触发）、`test_low_coverage_blocks_rule_even_with_very_negative_margin`（覆盖率低时亏得再狠也不报）、`test_coverage_is_not_clamped_so_data_inconsistency_is_visible`（`> 1` 如实给出）、`test_zero_denominators_return_none`、`test_revenue_without_units_is_rejected_not_silently_discounted`。
- 成本表边界：`test_missing_cost_file_is_not_an_error_but_is_logged`（缺失 → warn + 空表，**不是** `ConfigError`）、`test_bad_cost_file_raises_config_error`（参数化：重复键 / 空值 / 0 / 负数 / float / 字符串 / bool）、`test_non_utf8_cost_file_is_a_config_error`。
- 数据面：`test_aggregate_orders_groups_revenue_and_units_by_sku`、`test_order_units_defaults_to_one_and_rejects_non_int`、`test_by_sku_flows_from_collect_aggregation_into_metrics`、`test_unmatched_cost_entries_are_reported_but_do_not_block`。
- 规则形状：`test_loss_making_rule_shape_is_registered`（`severity=critical`、`metric=gross_margin`、**`actions` 为空**）、`test_repo_rules_yaml_still_loads`。
- 机械可查：`backend/config/rules.yaml` 的 `loss_making` 条目**下面没有 `actions:`**；`grep -rn "sku_gross_margin" backend/app` 在 SKU 轮次落地前为 **0**（名字已冻结、实现未开始）；`grep -rn '__tablename__' backend/app --include=*.py` 仍 **4 行**（成本不进 DB）。
- 反面对照（最有用的一条）：去掉 `cost_coverage_ratio`、只看 `gross_margin`，则"5% 的成交额在亏 50%"与"全店在亏 50%"在数据上**完全无法区分**——这就是覆盖率字段存在的全部理由。

**为什么这样对生产更好**

告警的价值只取决于两件事：**粒度对不对**、**结论覆盖多大范围**。ADR-018 解决的是第一件（SKU 级事实不能压成店铺级均值），但它的落地形态把首版能力锁在"全部就绪"上；本 ADR 解决的是第二件，并且用**最小可交付的正确子集**解锁首版：店铺级加权 + 覆盖率，能诚实地回答"有成本覆盖的那部分成交整体是不是在亏"，同时**明确拒绝**在没有 SKU 归属的情况下动手。这个"能说不能做"的组合看起来像缺功能，其实是**把不可逆的动作挡在信息不足之外**：改价、下架、清库存都是不可回滚的物理动作，而"是哪件货在亏"在店铺级口径里**根本不存在**——信息不存在时，正确的行为是**不做**，不是**猜一个最像的**（猜错的代价由店里最赚钱的商品承担）。

本 ADR 还固化两条更一般的取向。第一，**结论必须自带作用域**：任何"只覆盖一部分样本"的指标，都要有一个配套的覆盖度字段，否则它一定会被当成全域结论（`gross_margin` + `cost_coverage_ratio` 是这条的第一个实例）。第二，**门禁必须建在数据存在的地方**：§8.5 原条文要求启动期做一次静态交叉校验，但那一刻数据不存在——于是它从"门禁"退化成"信仰条款"；改成运行期审计后，同一个检查第一次真正会执行，而且**只记不阻断**，因为它拦的是"漏报"（过期成本让覆盖率变小），不是"误报"（把好货判成亏本），对漏报的正确处置是**让它可见**，不是**让流水线停下**。

**被否方案**

| 备选 | 否决理由 |
|---|---|
| ① **店铺级触发后拿 `top_gmv_sku` 动手**（`price_update` 降价 / `stock_update` 清库存） | 店铺级加权毛利率**指不出是哪件货在亏**。`top_gmv_sku` 是"卖得最多"，与"在亏"**没有任何逻辑关系**——最可能的结果是给店里最赚钱的商品降价或下架。而且这个错误**不可回滚**：价格与库存是真实世界的状态，误伤一次就写进历史。规则宁可只告警，也不许猜目标。 |
| ② **成本缺失按 0 处理**（"没配成本就当 0 成本"） | 两个方向都错，且反方向是灾难：当 0 成本 → 毛利率 = 1.0（看起来暴利，问题被永久藏起来）；反过来若"缺成本时把 margin 当 0 参与比较" → `0 < 阈值` **直接判亏本**，Agent 去改价/下架**没配成本的正常商品**。缺失的唯一正确表达是 `None`，且 `None` 必须让规则不触发（§8.3.3）。这是本仓反复强调的"最贵的一次误报"。 |
| ③ **覆盖率低于阈值也触发**（只看 `gross_margin < 0`，不加 `cost_coverage_ratio >= 0.6`） | 少数品的毛利率代表不了全店。覆盖率 10% 时一个 `-0.5` 只说明"这一小撮在亏"，拿它当全店结论就是误伤；而覆盖率 0（一个有成本的 SKU 都没卖）时 `gross_margin` 是 `None`，条件本来就该拦住。两个字段必须成对出现，缺一个都不能下结论。 |
| ④ **把 `loss_making` 做成直接下架**（`stock_update: {stock: 0}`） | 这是**钱的风险最大、可回滚性最差**的一类动作：下架后流量与权重都要重新积累，误伤一次的代价远超一次漏报。而且它建立在同一个信息缺口上——店铺级结论不知道是哪件货，下架谁都是猜。真正需要下架时，应该由**SKU 级**规则指名道姓地做，并走审批。 |
| ⑤ **覆盖率裁剪到 `[0, 1]`**（`min(covered_revenue / gmv_cents, 1.0)`） | 覆盖率 `> 1` 是**数据不一致的报警器**（上游汇总与订单明细对不上：分母少算了）。裁剪会让这种不一致彻底消失，只留下一个"看起来 100% 覆盖"的假象——而它恰恰会让 `gross_margin` 被当成全店结论使用，等于把本 ADR 要防的误读亲手做实。 |
| ⑥ **保留 §8.5 的"启动期 `ConfigError`"，让实现者自己想办法** | 数据在那一刻不存在，唯一的"实现方式"是把上游调用搬进 lifespan，即**让进程启动依赖抖店可用性**（上游抖动 → 服务起不来）。更糟的是它把"漏报"当成"误报"处理：一条过期成本配置会拦掉**当天所有指标**，而它真实的影响只是少了一个 SKU 的毛利率。改成运行期审计 + 只记不阻断，同一个检查第一次真正执行。 |
| ⑦ **SKU 侧继续用 `gross_margin` 这个名字**（两集合各有一份，靠"上下文"区分） | 同名不同义会让规则作者**无意中把店铺级阈值用到单品上**：一条为全店调的 `gross_margin < 0.0` 套到单品上会把"薄利"判成"亏本"，而误报会驱动真实动作。更隐蔽的是在 SKU 轮里两个名字**同时存在**（店铺级 15 ∪ SKU 级 10 = 25 键），写 `gross_margin` 的 SKU 级规则会对所有 SKU 得到**同一个**店铺级结果——作者以为在逐件判断，实际在重复判断全店。 |

---

## 附 A：被否掉的备选方案（记录理由，避免重复讨论）

| 备选 | 否决理由 |
|---|---|
| 用 `requests`（同步）替代 httpx | 无法注入 `MockTransport` 做确定性离线测试；FastAPI 异步路径里需要线程池桥接；无原生 retry/limits 语义 |
| 用 `tenacity` 做重试 | 重试决策依赖我们自己的 `mutating` + 分类规则，tenacity 的装饰器模型反而要把上下文透传出去；且多一个依赖换不到能力 |
| Celery/APScheduler 做效果调度 | 引入 broker（Redis）+ 另一个进程，为了 4 个窗口的定时任务；SQLite/PG 表 + 60s 轮询足够，且**调度状态在库里**可审计、可重放 |
| 用 `pandas` 算指标 | 数据量是"一天几百行"，pandas 带来 100MB 依赖与 dtype 陷阱（金额 float64）；纯函数 + int 分更可控 |
| 前端用 Next.js / SSR | 这是一个内部审批台，用户 < 20 人；Vite SPA + 静态托管最省事，SSR 只增加构建与部署面 |
| 用 `eval` + `__builtins__={}` 跑规则 | 见 ADR-014，逃逸面不可控 |
| 用 PG 的 `ON CONFLICT` 做幂等 | 见 ADR-012，方言绑定；且 SQLite 分支仍要另一套实现 |
| 让 `track_effects` 用 `Send` 并行等窗口 | 仍然是"图里等待三天"，只是并发等待，问题不变（ADR-003） |
| 审批状态只存 DB、不存 checkpoint | 那样 resume 时无法重放图（LangGraph 需要自己的 checkpoint 才能从 `interrupt` 处继续），等于手写状态机 |
| 节点 trace 也写 `agent_traces`（一行一节点或一行一 JSON 数组） | 见 ADR-015：高频 append 撞 `UNIQUE(shop_id, run_id)` 语义，写放大 + 索引 churn，且查询形态是 tail 不是关系查询 |
| 节点 trace 只写 JSONL、`step_count` 由各节点自己 `+1` | 两个写入点 = 两份真相，计数迟早对不上；ADR-015 要求收敛到 `traced()` 单点 + 可断言不变量 |
| 靠"有没有异常抛出"判失败（`error` 非空即失败，不设 `outcome`） | 见 ADR-016：HITL 挂起靠 `GraphInterrupt` 抛出，正常审批会被记成红色 error，告警永久为红 = 没有告警 |
| 用 `level` 单独表达故障（`outcome` 只做展示） | 见 ADR-016：`level` 是**派生**值（`outcome`→`level` 的映射是硬规则），反过来让 `level` 承担语义就会出现两处真相；且旧日志里两者必然不一致 |
| 只捕 `GraphInterrupt`，不捕基类 `GraphBubbleUp` | 见 ADR-016：`GraphDrained` / `ParentCommand` 同为控制流，只捕子类会在框架新增控制流异常时静默退化成"当失败" |
| 给挂起单独开一个 `event="node.interrupt"` 事件名（与 `node.trace` 并列） | 会破坏 `count(node.trace) == step_count` 不变量：挂起那次执行要么没 trace、要么被数两次；ADR-016 选择在同一事件里用 `outcome` 区分，保计数不变 |
| 把装配层与仓储层留在同一个文件（`adapters.py` 既装配又做 `agent_traces`/`action_log` 仓储） | 见 ADR-017：仓储因此出现两个 `+= 1` 写入点（撞 §4.7），且测试替换装配会把仓储写入带进测试路径；两层的变更轴不同，合成一个文件后每次改动都同时施加在两个轴上 |
| 只做店铺级指标，SKU 明细"以后再说" | 见 ADR-018：店铺级均值是加性的，一个爆款能永久掩盖任意多个亏损 SKU，"亏本"告警在数学上不可能触发；且 §8.3.3 已有的四个 SKU 级名字会变成无法满足的条款 |
| 成本缺失按 0 参与计算（"没配成本就当 0"） | 见 ADR-018：`gross_margin` 会被算成 0 或 1.0，两种都错——其中一种直接判亏本并触发改价/下架，是误报里最贵的一种。缺失必须是 `None`，规则不触发 |
| 用店铺级规则 + 阈值调优来抓亏损 SKU | 见 ADR-018：聚合层级错了，调参只是在噪声里找局部最优——SKU A 亏 20%、店铺毛利率 17.1%，任何阈值都不能同时满足"抓 A"和"不误报 B/C" |
| 让 `Anomaly` 进 `/api/alerts` 以便前端看 SKU | 见 ADR-018 §决定：`/api/alerts` 返回 `PendingApproval[]`，SKU 信息已由 `target`（= sku_id）与 `evidence.sku_id` 承载；把内部 `Anomaly` 抬进 HTTP 面会多一套 schema，且它没有稳定排序/分页语义 |
| 成本进 DB（第 5 张 `sku_cost` 表） | 见 ADR-018/§8.5：成本是商家自维护的**配置**（随部署走、要能 code review、几乎不变），不是运行时业务数据；加表要动 §4.7 断言 B′、动迁移、动 §6，收益为零 |
| 装配层不写进目录树 / 归属表，靠"大家都知道它在" | 见 ADR-017：没有归属就没有"只有一份"的约束，`AgentRuntime` 已在 `adapters.py`/`nodes.py` 各长出一份，两个单例让 lifespan 注入失效而 e2e 照样跑通 |
| 用 `grep -rn "step_count" backend/app` 只命中两个文件来证单写点 | 见 §4.7 断言取向：与「`reconcile_step_count` 在 `finalize` 前调用、`finalize` 在 `nodes.py`」互斥，字面 grep 必然命中 import；这种断言过不了，且唯一"过"的修法是挪走正确逻辑。改为 A/B′/C 三条按"定义/写入"形态匹配的断言 |
| 只让 `collect` 改抛异常、在图里加一个捕获节点把失败转回 state | 见 ADR-019：①LangGraph 没有"异常 → 边"的原生机制，得多写一个契约里没有的节点；②`compute_metrics` / `detect_anomalies` / `propose_actions` 是同一句"写 failed 后正常返回"，缺陷会在别处复制；③改动面从 1 个包装器扩散到 4 个节点 + 1 个新节点，还多一条"新节点必须记得抛异常"的隐性约定 |
| 用"返回值里带 `errors` / `status=failed`"判这次执行失败 | 见 ADR-019：`finalize` 在已失败的 run 上收尾时返回值也带 `status=failed`，每个失败 run 都会长出第二盏红灯，失败被重复计数；判据必须是**状态迁移**（非 failed → failed），而不是"结果取值里有没有失败字样" |
| 只做键名层脱敏（现状），把"已脱敏"的声明保留下来 | 见 ADR-020：`redact()` 对 `str` 是 no-op，而 `message` 与 `data.error.message` 恰恰是上游错误原文的落点——那条通道 100% 漏遮，事故就是这么发生的（`access_token=SECRET-abc123` 原样进 JSONL） |
| 只做值级层（文本正则），键名层交给它 | 见 ADR-020：`data={"access_token": SECRET}` 是 dict 键，正则只认文本里的 `=`/`:`；要么漏遮，要么先 `json.dumps` 再遮再 `loads`，改变下游看到的值的形态 |
| 在 `record` 层脱敏 `stderr`（处理 `LogRecord.msg/args`） | 见 ADR-020：`%(message)s` 的最终文本只有 `format()` 之后才存在（还要拼 `asctime`/`levelname` 与 traceback），在 record 层遮的是"还没成为最终文本的东西"，journald 收的仍是明文 |
| 脱敏测试只测 `redact()`/`redact_any()`，不测通道 | 见 ADR-020：把 `write()` 里那行 scrub 删掉，15 条函数级用例照样全绿——门禁与泄露路径错位，等于没测 |
| 店铺级毛利率触发后拿 `top_gmv_sku` 去降价/清库存 | 见 ADR-021：店铺级加权毛利率指不出是哪件货在亏，`top_gmv_sku` 是"卖得最多"与"在亏"无逻辑关系；误伤店里最赚钱的商品且不可回滚 |
| 成本缺失按 0 处理 | 见 ADR-021：当 0 成本 → 毛利率 1.0（问题被藏）；反方向 → `0 < 阈值` 直接判亏本，Agent 去改价下架**没配成本的正常商品** |
| 覆盖率低于阈值也触发（只看 `gross_margin < 0`） | 见 ADR-021：少数品的毛利率代表不了全店，覆盖率 10% 的 `-0.5` 被当成全店结论就是误伤 |
| 把 `loss_making` 做成直接下架 | 见 ADR-021：钱的风险最大且不可回滚（下架后流量与权重要重新积累），且仍建立在"不知道是哪件货"的同一个缺口上 |
| 覆盖率裁剪到 `[0, 1]` | 见 ADR-021：`> 1` 是上游汇总与明细对不上的报警器，裁剪会把它抹成"看起来 100% 覆盖"的假象 |
| 保留 §8.5 的启动期 `ConfigError` | 见 ADR-021：启动期没有 `raw`，唯一"实现"是把上游调用搬进 lifespan（进程启动依赖抖店可用性）；且它把漏报当误报处理，一条过期配置拦掉全天指标 |
| SKU 侧继续沿用 `gross_margin` 这个名字 | 见 ADR-021：SKU 轮里两个名字同时存在（27 键），写 `gross_margin` 的 SKU 级规则会对所有 SKU 得到同一个店铺级结果，作者以为在逐件判断 |

## 附 B：待核实清单（汇总，别假装已解决）

| # | 事项 | 当前实现选择 | 风险等级 | 核实方式 |
|---|---|---|---|---|
| 1 | 抖店签名串在真实 app_key 下被接受 | 按官方 SDK 实现 + 黄金向量 | 中 | sandbox 调 `shop.getShopInfo` 期望 `err_no=0` |
| 2 | `timestamp` 是否按服务器时区要求（本地 vs UTC） | 用本机本地时间格式化（与 SDK 一致） | 中 | 同上；若报签名错，先改 UTC 再试 |
| 3 | `AsyncPostgresSaver.from_conn_string` 是否存在 | 主路径；备用 `AsyncConnectionPool` | 低 | 安装 `langgraph-checkpoint-postgres==3.2.0` 后 `python -c "import inspect"` |
| 4 | 各接口真实 QPS 限额 | 默认 8 QPS / burst 16 | 低 | 开放平台后台查看应用限额 |
| 5 | 抖店接口返回 `err_no` 还是 `code` | 优先 `err_no`，回退 `code` | 低 | sandbox 抓一次真实响应 |
| 6 | `docker` daemon 在本机是否可用 | compose 文件按 v2 语法写 | 低 | `docker info`（`docs/ENV.md` §7） |
| 7 | 摘机内存余量是否够跑 pip+npm+test 并发 | 已记录：串行执行 | 中 | `docs/ENV.md` §6 |
