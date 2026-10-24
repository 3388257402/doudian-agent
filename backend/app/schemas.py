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
    interrupt_id: str
    action_type: ActionTypeLiteral
    target: str
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
    pending_approvals: list[AlertItem]
    effect_report: EffectReport | None = None


# --------------------------------------------------------------------------------------
# §7.3 GET /api/alerts
# --------------------------------------------------------------------------------------
class AlertList(BaseModel):
    """`GET /api/alerts` 响应。`next_cursor` = 最后一条的 `id`（字符串形式的整数），否则 `null`。"""

    model_config = _forbid_extra()

    items: list[AlertItem]
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
        if self.verdict == "edit":
            if not self.edited_params:
                raise ValueError("verdict=edit 必须带非空的 edited_params")
        elif self.edited_params is not None:
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
    refund_rate: float | None = None
    uv: int | None = None
    cvr: float | None = None
    gmv_7d_avg_cents: int | None = None
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
    run_id: str | None = None
    trace_id: str | None = None
    shop_id: str | None = None
    node: str | None = None
    message: str
    data: dict[str, Any]


class LogList(BaseModel):
    """`GET /api/logs` 响应。`seq DESC`；`next_cursor` = 最后一条 `seq` 的字符串形式。"""

    model_config = _forbid_extra()

    items: list[LogItem]
    next_cursor: str | None = None


# --------------------------------------------------------------------------------------
# §7.1 错误包络
# --------------------------------------------------------------------------------------
class ErrorDetail(BaseModel):
    """`error` 对象（§7.1）。`message` 对 500 固定为"内部错误"，细节只进日志与 `trace_id`。"""

    model_config = _forbid_extra()

    code: ApiErrorCode
    message: str
    details: dict[str, Any] | None = None
    trace_id: str


class ErrorEnvelope(BaseModel):
    """所有 4xx/5xx 的统一包络（§7.1）。前端只按 `error.code` 分支，不按 HTTP 状态猜。"""

    model_config = _forbid_extra()

    error: ErrorDetail


# --------------------------------------------------------------------------------------
# §7.7 GET /api/healthz
# --------------------------------------------------------------------------------------
class HealthzResponse(BaseModel):
    """健康检查（`200`）：任一依赖不可用就走 `HealthzDegraded` + `503`。"""

    model_config = _forbid_extra()

    status: Literal["ok"]
    version: str
    db: Literal["ok"]
    checkpointer: Literal["ok"]
    time: str


class HealthzDegraded(BaseModel):
    """`503` 的降级体（§7.7 逐字：`status` / `db` / `detail`）。"""

    model_config = _forbid_extra()

    status: Literal["degraded"]
    db: Literal["ok", "error"]
    detail: str
