"""REST 契约的 pydantic v2 模型 —— 逐字对应 docs/CONTRACT.md §7（唯一事实源）。

三条硬规则（违反即集成失败）：

1. **字段名逐字一致**（snake_case，不做 camelCase 转换）：§12 的 TS 类型是同一份形状，
   改一个键名等于同时改前端、日志、验收断言三处。
2. **金额一律整数分**，字段名必须 `*_cents` 且类型是 `int`。出现 `float` 或 `*_yuan`
   就是资损级歧义（9900 分 vs 99.00 元），在边界炸掉，不做静默换算。
3. **请求体 `extra="forbid"`**：未知键 = 400。静默忽略 typo（`edited_param` 少个 `s`）会让
   "以为改了参数、其实按原参数执行"，这不属于宽容，属于把错误藏起来。
   响应侧靠 FastAPI 的 `response_model` 只输出声明字段（§7：未知字段禁止透传）。

校验分工（同一条规则只有一处实现）：

- **形状/类型/枚举/必填** → 本文件（pydantic），由 FastAPI 转成 400 `VALIDATION_ERROR`；
- **与待审批集合的一致性**（多/少/重复到具体 action_id）→ `app/agent/nodes.py` 的
  `validate_resume_payload`（§4.4，权威在校验层，不在这里重写一遍）；
- 本文件只额外拦"重复 action_id"这一条**纯请求体内部**的错（不需要查库就能判定）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ActionTypeLiteral",
    "AlertItem",
    "AlertList",
    "ApiErrorCode",
    "AppliedDecision",
    "ApproveRequest",
    "ApproveResponse",
    "AttributionLiteral",
    "Decision",
    "EffectReport",
    "EffectWindow",
    "ErrorDetail",
    "ErrorEnvelope",
    "HealthzDegraded",
    "HealthzResponse",
    "LogItem",
    "LogLevel",
    "LogList",
    "MetricPoint",
    "MetricsResponse",
    "OPERATOR_PATTERN",
    "RUN_ID_PATTERN",
    "RUN_TRIGGERS",
    "RiskLiteral",
    "RunRequest",
    "RunResponse",
    "RunStatusLiteral",
    "SHOP_ID_PATTERN",
    "ALERT_STATUSES",
    "ApprovalStatusLiteral",
    "VerdictLiteral",
]

# --------------------------------------------------------------------------------------
# 字面量（§7.1 错误码、§2 枚举、§7 各响应字段取值域）
# --------------------------------------------------------------------------------------

#: §7.2：`shop_id` / `run_id` 的字符集与长度
#: 与 `state._RUN_ID_RE` 同形：入口层先用它挡住"带冒号/换行"的 id，
#: 否则 `thread_id = f"{shop_id}:{run_id}"` 会变得不可反解。
SHOP_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

#: §7.4：`operator` 的字符集与长度
OPERATOR_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"

#: §7.2：`trigger` 取值域
RUN_TRIGGERS = ("manual", "scheduled", "retry")

#: §7.3：`status` 过滤取值域（`all` = 不过滤）
ALERT_STATUSES = ("pending", "approved", "rejected", "expired", "all")

RunStatusLiteral = Literal[
    "running", "awaiting_approval", "executing", "done", "failed", "cancelled"
]
ApprovalStatusLiteral = Literal["pending", "approved", "rejected", "expired"]
AlertStatusFilter = Literal["pending", "approved", "rejected", "expired", "all"]
ActionTypeLiteral = Literal["price_update", "stock_update", "coupon_create"]
VerdictLiteral = Literal["approve", "reject", "edit"]
RiskLiteral = Literal["low", "medium", "high"]
AttributionLiteral = Literal["single_arm_before_after", "ab_test", "none"]
EffectWindowStatusLiteral = Literal["scheduled", "evaluated", "missed"]
LogLevel = Literal["debug", "info", "warn", "error"]
RunTriggerLiteral = Literal["manual", "scheduled", "retry"]

#: `applied[].status` = `ActionLogStatus` ∪ `{"rejected"}`（§7.4）
AppliedStatusLiteral = Literal[
    "pending", "succeeded", "failed", "unknown", "skipped_duplicate", "rejected"
]

#: §7.1 的 9 个错误码（冻结）
#: 与 `app/enums.py::ApiErrorCode` 必须逐字一致：这里是 pydantic 侧的 Literal（做校验），
#: 那边是业务侧的 StrEnum（做构造）。两处不一致的后果是"服务能抛的码模型不认"→ 500。
ApiErrorCode = Literal[
    "VALIDATION_ERROR",
    "THREAD_NOT_FOUND",
    "APPROVAL_NOT_FOUND",
    "RESOURCE_NOT_FOUND",
    "DUPLICATE_DECISION",
    "RUN_IN_PROGRESS",
    "RUN_STATE_CONFLICT",
    "UPSTREAM_ERROR",
    "INTERNAL_ERROR",
]


def _forbid_extra() -> ConfigDict:
    """请求模型的统一 `model_config`：未知键 → 400（§7 硬规则 3）。

    保证：返回**新** `ConfigDict`（pydantic 会拷走它，共享同一个对象也不会串味）。
    不保证：不禁止空值/不做额外校验——"未知键"是这里唯一的一条规矩。
    调用方坑：所有**请求**模型都必须用它；响应模型也用它来禁止透传未声明字段
        （§7：未知字段不许出现在响应里）。
    """
    return ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------
# §7.2 POST /api/agent/run
# --------------------------------------------------------------------------------------
class RunRequest(BaseModel):
    """`POST /api/agent/run` 请求体。

    `dry_run` 缺省（`null`）时取 `settings.doudian.dry_run`（§7.2）。上游模式是**进程级**的
    （`AgentRuntime.client` 是单例，ADR-004），所以路由层只接受"与进程配置一致"的显式值；
    想切换请改 `config/settings.yaml` 并重启（见 `app/api/routes_run.py`）。
    """

    model_config = _forbid_extra()

    shop_id: str = Field(pattern=SHOP_ID_PATTERN, description="店铺 ID，`^[A-Za-z0-9_-]{1,64}$`")
    run_id: str | None = Field(
        default=None,
        pattern=RUN_ID_PATTERN,
        description="`null` → 服务端生成 `run_{YYYYMMDDTHHMMSSZ}_{8hex}`",
    )
    # `trigger` 只进审计与日志，不改变执行路径（三种值走的是同一张图）。
    trigger: RunTriggerLiteral = "manual"
    dry_run: bool | None = Field(
        default=None, description="`null` → 取 settings.doudian.dry_run"
    )


class EffectWindow(BaseModel):
    """效果窗口（§7.4 `effect_report.windows[]`、§3.3 `EffectWindow`）。

    `offset_days` 是 **int 天数**（不是时间戳）：T+0/1/3/7 是离散枚举，用整数才能做 `UNIQUE` 与
    排序；`due_at` 是**绝对时刻**而不是"过几天"——图内不等待（ADR-003），窗口靠 `due_at <= now`
    被调度器捞，所以两者都必须落盘、不能只在内存里加。
    `status` 是枚举而不是 bool：`scheduled/evaluated/missed` 三态里，`missed`（"确实没看成"）
    与 `scheduled`（"还没到时间"）是不同事实，压成 bool 就会把"漏采"画成"待采"。
    """

    model_config = _forbid_extra()

    offset_days: int
    due_at: str
    snapshot_id: str
    status: EffectWindowStatusLiteral


class EffectReport(BaseModel):
    """效果报告（§5.9）。图内只登记窗口，不给因果结论。

    `conclusion` 是 `str` 而不是枚举：它是**人读文案**，由 `build_effect_report` 按"是否有对照组"
    生成；契约 §5.9.3 约束的是"文案里不许出现因果断言词"，那是内容校验，压成枚举反而写不出句子。
    `caveats` 是 `list[str]` 而不是单个 `str`：无对照组时必须同时挂两条（季节性/归因强度），
    一条字符串会让调用方自己去拆分隔符。
    """

    model_config = _forbid_extra()

    run_id: str
    generated_at: str
    attribution: AttributionLiteral
    windows: list[EffectWindow]
    conclusion: str
    caveats: list[str]


class AlertItem(BaseModel):
    """一条审批提议（§7.3 `items[]`，与 §12 `PendingApproval` 同形）。

    身份就是 `action_id`：契约里**没有** `approval_id`（§6.1 `approval_request.action_id` UNIQUE）。
    `interrupt_id` 是 `str` 且**允许空串**：行的落库发生在 `interrupt()` 之前（§5.8 时序），
    `interrupt_id` 只能事后回填；空串表示"这条还没关联到具体挂起点"，前端点审批会拿到 404
    （所以 `/api/alerts` 只在 run 挂起并回填之后才应该显示它）。
    `params`/`evidence` 是 `dict[str, Any]` 而不是强类型：它们是**动作类型相关**的自由字段
    （改价是 `price_cents`、清库存是 `stock`），§7 只冻结了外层键名；强类型会逼模型层认得每种动作。
    `reason` 是 `str` 必填（可以为空串）：审批人要能看到"为什么提这个"；`null` 会逼前端做空态分支。
    `risk`/`status` 是枚举：前端按它们分色/过滤，自由字符串会让"high" 和 "HIGH" 变成两回事。
    """

    model_config = _forbid_extra()

    action_id: str
    run_id: str
    thread_id: str
    shop_id: str
    trace_id: str
    # 允许空串：行的落库在 `interrupt()` 之前，id 只能事后回填（空串 = 点审批会 404）。
    interrupt_id: str
    action_type: ActionTypeLiteral
    target: str
    # 动作类型相关的自由字段（改价是 price_cents、清库存是 stock）：§7 只冻结外层键名。
    params: dict[str, Any]
    reason: str
    evidence: dict[str, Any]
    risk: RiskLiteral
    status: ApprovalStatusLiteral
    created_at: str


class RunResponse(BaseModel):
    """`POST /api/agent/run` 响应（§7.2，`202`；已终态的同一 run 回放时为 `200`）。

    `status` 是枚举而不是 bool（`ok`/`failed`）：一次 run 有 `awaiting_approval` 这个**中间态**，
    压成 bool 会让"等人审批"和"跑完了"无法区分（前端会去错误地轮询结果）。
    `pending_approvals` 必须始终是**数组**（不是 `null`）：空数组 = "这次没有待办"，`null` = "不知道"，
    两者语义不同，前端据此决定"显示空态"还是"显示加载中"。
    `effect_report` 是 `EffectReport | None`：`null` = 本轮没执行过动作（没有窗口可登记），
    不是"执行了但没效果"——后者是 `windows[].status` 的事。
    """

    model_config = _forbid_extra()

    run_id: str
    thread_id: str
    trace_id: str
    status: RunStatusLiteral
    # 恒为数组（不是 null）：空数组 = "这次没有待办"，null = "不知道"，两者语义不同。
    pending_approvals: list[AlertItem]
    # `null` = 本轮没执行过动作（没有窗口可登记），不是"执行了但没效果"。
    effect_report: EffectReport | None = None


# --------------------------------------------------------------------------------------
# §7.3 GET /api/alerts
# --------------------------------------------------------------------------------------
class AlertList(BaseModel):
    """`GET /api/alerts` 响应。`next_cursor` = 最后一条的 `id`（字符串形式的整数），否则 `null`。

    保证：`items` 一定是列表（空列表 = 没有待办）；`next_cursor` 为 `null` 表示"没有下一页"。
    不保证：`next_cursor` 是不透明游标（字符串），不要自己加减——它只是行 id 的字符串形式。
    调用方坑：别用 `len(items) == limit` 判断"还有下一页"（最后一页刚好整页时也为 `null`）。
    """

    model_config = _forbid_extra()

    items: list[AlertItem]
    # 默认 `None`（不是空串）：`null` 与 "" 在"还有没有下一页"这个问题上含义不同。
    next_cursor: str | None = None


# --------------------------------------------------------------------------------------
# §7.4 POST /api/approve
# --------------------------------------------------------------------------------------
class Decision(BaseModel):
    """单条裁决。`edited_params` 必填（可 `null`）：`edit` 必须非空，其他必须 `null`（§4.4 规则 5）。"""

    model_config = _forbid_extra()

    action_id: str = Field(min_length=1, max_length=160)
    verdict: VerdictLiteral
    # 必填且可 null：用「无默认值 + Optional」表达，缺键 → 400（不做"缺键即 null"的宽容）
    edited_params: dict[str, Any] | None
    operator: str | None = Field(default=None, pattern=OPERATOR_PATTERN)
    reason: str | None = None

    @model_validator(mode="after")
    def _check_edited_params(self) -> Decision:
        """`edit` 必须带非空 `edited_params`，其它 verdict 必须为 `null`（§4.4 规则 5）。

        保证：两条互补的规矩都在这里（多了不行、少了也不行），错误以 pydantic 的
            `ValueError` 形式抛 → FastAPI 转成 400 `VALIDATION_ERROR`。
        不保证：不校验参数**内容**是否符合作动作 schema——那要 `action_type`，
            请求体里没有（在 `nodes.validate_resume_payload` 里查）。
        调用方坑：`mode="after"` 意味着拿到的已经是解析后的值（`verdict` 是 Literal 校验过的）。
        """
        if self.verdict == "edit":
            if not self.edited_params:
                raise ValueError("verdict=edit 必须带非空的 edited_params")
        elif self.edited_params is not None:
            # 不做"多给就忽略"：那会让"以为改了、其实按原参数执行"成为可能。
            raise ValueError(f"verdict={self.verdict} 时 edited_params 必须为 null")
        return self


class ApproveRequest(BaseModel):
    """`POST /api/approve` 请求体（§7.4）。

    `interrupt_id` 是"这是我看到的那个挂起点"的凭据：`resume_agent` 会拿它和当前挂起的
    interrupt id 比对，不一致 → 404 `APPROVAL_NOT_FOUND`（旧审批页面不能对新挂起点生效）。
    """

    model_config = _forbid_extra()

    thread_id: str = Field(min_length=1, max_length=160)
    interrupt_id: str = Field(min_length=1, max_length=64)
    operator: str = Field(pattern=OPERATOR_PATTERN)
    decisions: list[Decision] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_action_ids(self) -> ApproveRequest:
        """请求体内部就能判定的"重复 action_id"必须当场 400。

        保证：同一条动作不会被批两次（重复会让 `applied[]` 出现两条指向同一 action 的裁决）。
        不保证：不校验"集合与挂起的 pending_actions 一致"——那要查库/读 state，
            在 `nodes.validate_resume_payload`（§4.4 规则 2，权威在校验层）。
        调用方坑：这是"纯请求体"校验的一道**额外**保险，不是替代品：两处都要过。
        """
        ids = [decision.action_id for decision in self.decisions]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"decisions 里出现重复 action_id: {duplicates}")
        return self


class AppliedDecision(BaseModel):
    """`applied[]` 元素。`reject` 不写 `action_log`：`status="rejected"` 且 `action_log_id=null`。"""

    model_config = _forbid_extra()

    action_id: str
    verdict: VerdictLiteral
    # 普通字符串：取值域是 ActionLogStatus ∪ {"rejected"}（§7.4，不能声明成 ActionLogStatus）
    status: AppliedStatusLiteral
    action_log_id: str | None = None
    error: str | None = None


class ApproveResponse(BaseModel):
    """`POST /api/approve` 响应（§7.4，`200`）。

    `applied[]` 与请求里的 `decisions` **不是**一一对应的同义词：`reject` 不写 `action_log`，
    其 `status` 固定 `"rejected"`、`action_log_id=null`；所以 `applied[].status` 不能声明成
    `ActionLogStatus` 枚举（§7.4 明确要求普通字符串 + `rejected` 这个额外值）。
    `status` 是恢复后的 **run** 状态（可能是 `done`、也可能是 `awaiting_approval`）：
    别把"200"读成"已经跑完了"。
    `effect_report` 为 `null` 表示"这一轮没有成功执行的动作，没有窗口可登记"，
    与 `windows=[]` 的"登记了 0 个窗口"是两件事。
    """

    model_config = _forbid_extra()

    thread_id: str
    run_id: str
    trace_id: str
    # 恢复后的 **run** 状态：可能是 done、也可能是 awaiting_approval（上游还有下一轮）。
    status: RunStatusLiteral
    applied: list[AppliedDecision]
    pending_approvals: list[AlertItem]
    effect_report: EffectReport | None = None


# --------------------------------------------------------------------------------------
# §7.5 GET /api/metrics
# --------------------------------------------------------------------------------------
class MetricPoint(BaseModel):
    """一天的店铺级指标（§7.5 / §5.3）。

    `refund_rate` / `cvr` / `uv` / `gmv_7d_avg_cents` 可为 `null`（"算不出来"），
    绝不填 `0`（`0` 是"等于零"，是另一个业务事实）。逐个说清：
        - `refund_rate`：分母 `paid_order_count == 0` → `null`。填 0 会读成"退款率 0%，很健康"，
          而事实是"今天没有支付订单，退款率无从谈起"；
        - `cvr`：`uv` 缺失或为 0 → `null`。填 0 会读成"转化率为 0"，把"没有访客数据"变成
          一个假的运营结论；
        - `uv`：`null` = 上游没给或无法确认作用域（§5.3）。**不摊派**（8 天窗口 ÷ 8 会把 cvr 摊薄 8 倍）；
        - `gmv_7d_avg_cents`：历史不足 3 天 → `null`。用 1 天算"7 日均值"是假精确。
    其余 `*_cents` 是 `int`（整数分）：`float` 会让 9900 与 99.00 在两处代码里都"看起来对"。
    `aov_cents` 例外地允许 0（分母 0 → 0）：客单价 0 是业务上真实存在的值，不是"算不出来"。

    成本口径（v1.5.0，纯增量，默认 `null`）：
        - `gross_margin`：**只对有成本的 SKU 加权**的毛利率。`null` = 没配成本 / 没有按 SKU 归集
          / 有成本的成交额为 0；它**不是** 0（0 会被读成"不赚不亏"，而事实是"不知道"）；
        - `cost_coverage_ratio`：有成本的 SKU 的成交额占全店 GMV 的比例。看板必须拿它和
          `gross_margin` 成对展示：覆盖率 10% 的 -50% 不代表"全店在亏"。
        老 checkpoint 里没有这两个键 → 默认 `null`（不补 0），老消费者读到 `null` 的行为与
        "没有这个字段"一致。
    """

    model_config = _forbid_extra()

    date: str
    gmv_cents: int
    order_count: int
    paid_order_count: int
    refund_order_count: int
    refund_amount_cents: int
    aov_cents: int
    # 四个"可能算不出来"的比率/均值：`None` 与 `0` 是两个业务事实（见类 docstring）。
    refund_rate: float | None = None
    uv: int | None = None
    cvr: float | None = None
    gmv_7d_avg_cents: int | None = None
    # 成本口径（v1.5.0 增量）：老 checkpoint 里没有这两个键 → 默认 `None`（不补 0）。
    gross_margin: float | None = None
    cost_coverage_ratio: float | None = None
    computed_at: str


class MetricsUnit(BaseModel):
    """单位声明（§7.5）：金额=分、日期=Asia/Shanghai、比率=0..1。前端据此格式化，不猜。

    为什么把单位放进**响应体**而不是只写在文档里：金额是分还是元、比率是 0..1 还是 0..100，
    猜错一次的后果是看板差 100 倍而没人发现。三个字段都给默认值，是因为它们描述的是本服务的
    固定约定（不是每次请求都不同的配置），默认值本身就是事实。
    """

    model_config = _forbid_extra()

    # 三个默认值本身就是事实（本服务的固定约定），所以不需要每次请求都传。
    money: Literal["cent"] = "cent"
    date: str = "Asia/Shanghai"
    ratio: str = "0..1"

class MetricsResponse(BaseModel):
    """`GET /api/metrics` 响应（§7.5）。`points` 按 `date` 升序，缺的日期**不补零**。

    `granularity` 是单值 Literal 而不是 `str`：现在只实现 `day`，把"仅支持 day"写进类型，
    比在文档里承诺更早暴露"想要周聚合"的调用方（它会拿到 400 而不是一份假的周数据）。
    `points` 空数组**不会**出现：没有数据是 404（§7.5），所以"200 + 空数组"永远意味着 bug。
    """

    model_config = _forbid_extra()

    shop_id: str
    granularity: Literal["day"]
    unit: MetricsUnit = MetricsUnit()
    points: list[MetricPoint]


# --------------------------------------------------------------------------------------
# §7.6 GET /api/logs
# --------------------------------------------------------------------------------------
class LogItem(BaseModel):
    """一行 JSONL 日志（§7.6）。`data` 里的敏感键在**写入前**已脱敏（`***`）。"""

    model_config = _forbid_extra()

    seq: int
    ts: str
    level: LogLevel
    event: str
    # 四个身份字段可为 None：不是每条事件都带全（例如启动横幅没有 run_id）。
    run_id: str | None = None
    trace_id: str | None = None
    shop_id: str | None = None
    node: str | None = None
    message: str
    data: dict[str, Any]


class LogList(BaseModel):
    """`GET /api/logs` 响应。`seq DESC`；`next_cursor` = 最后一条 `seq` 的字符串形式。

    保证：`items` 一定是列表（空列表 = 该过滤条件下没有事件）。
    不保证：不保证 `seq` 连续——它是"写入序号"，进程重启会从文件末尾续，跳号是正常的。
    调用方坑：翻页用 `before_seq`（严格小于），不要用 `seq - 1` 推算下一页起点。
    """

    model_config = _forbid_extra()

    items: list[LogItem]
    # 同上：`null` 表示没有下一页，不是"游标是空串"。
    next_cursor: str | None = None


# --------------------------------------------------------------------------------------
# §7.1 错误包络
# --------------------------------------------------------------------------------------
class ErrorDetail(BaseModel):
    """`error` 对象（§7.1）。`message` 对 500 固定为"内部错误"，细节只进日志与 `trace_id`。"""

    model_config = _forbid_extra()

    # `code` 用 Literal 而不是 str：9 个码是冻结的，写错一个就应该是 500（模型校验失败），
    # 而不是让前端收到一个它不认识的码。
    code: ApiErrorCode
    message: str
    # 可空但键必须出现（§7.1）：前端按"有没有这个键"写逻辑时不该看到两种形状。
    details: dict[str, Any] | None = None
    trace_id: str


class ErrorEnvelope(BaseModel):
    """所有 4xx/5xx 的统一包络（§7.1）。前端只按 `error.code` 分支，不按 HTTP 状态猜。"""

    model_config = _forbid_extra()

    # 只有 `error` 一个键：把 `trace_id` 放在这里（而不是顶层）是 §7.1 的冻结形状，
    # 顶层再加一个字段就等于改了契约。
    error: ErrorDetail


# --------------------------------------------------------------------------------------
# §7.7 GET /api/healthz
# --------------------------------------------------------------------------------------
class HealthzResponse(BaseModel):
    """健康检查（`200`）：任一依赖不可用就走 `HealthzDegraded` + `503`。"""

    model_config = _forbid_extra()

    # `status` 是 `Literal["ok"]`：200 的响应里它不可能是别的值——多一个状态就等于多一条
    # 前端分支，所以"不健康"直接走另一个模型（`HealthzDegraded` + 503）。
    status: Literal["ok"]
    version: str
    db: Literal["ok"]
    checkpointer: Literal["ok"]
    time: str


class HealthzDegraded(BaseModel):
    """`503` 的降级体（§7.7 逐字：`status` / `db` / `detail`）。

    注意它**没有** `version`/`checkpointer`/`time`：503 的形状与 200 **不同**（§7.7 就这么写）。
    前端用一个解析器强解两种形状会被 503 打成畸形——这是有意的取舍，不是遗漏。
    """

    model_config = _forbid_extra()

    status: Literal["degraded"]
    db: Literal["ok", "error"]
    detail: str
