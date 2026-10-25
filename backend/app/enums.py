"""枚举 —— 逐字对应 docs/CONTRACT.md §2（唯一事实源，不得增删改值）。

枚举序列化后就是这里的字符串字面量，DB 里存的也是它们（`String` 列，不建 PG enum）。
因此**不要**把这些枚举改成 `IntEnum` 或加别名：前端 `types.ts`（契约 §12）与 DB 里已有的
历史行都按字面量对齐，改一个字面量等于一次数据迁移。
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """一次 run 的状态（§4.2 迁移表）：`running → awaiting_approval → executing → done/failed`。

    保证：DB 里存的、日志里写的、响应里返回的都是**这些字面量**（不是整数）。
    不保证：不保证任意两个状态之间都能直接跳（合法迁移只看 §4.2；枚举本身不校验顺序）。
    调用方坑：`cancelled` 目前没有写入点（留给运维手工干预），别把它当成"失败"的同义词。
    """

    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    # 手工干预/运维取消用的终态；当前没有业务代码写入它，但 handler/前端仍要认得。
    CANCELLED = "cancelled"


class Severity(StrEnum):
    """异常严重度（§5.4）。它决定日志的 `level`（critical→error、warn→warn、info→info）。"""

    INFO = "info"
    WARN = "warn"
    # critical 必须映射到日志的 error 级：`loss_making` 这类"只告警不给动作"的规则
    # 没有别的出口，落成 info 就等于在所有人类入口上消失。
    CRITICAL = "critical"


class Risk(StrEnum):
    """动作风险等级（§5.5）：审批台按它分色，`agent.auto_approve_risk` 按它决定能不能自动批。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionType(StrEnum):
    """白名单动作类型（§5.5）。`coupon_create` 目前**没有**上游方法（见 `nodes._dispatch`）。"""

    PRICE_UPDATE = "price_update"
    STOCK_UPDATE = "stock_update"
    # 契约里有，但 `DoudianClient` 没封装对应的上游方法：执行时会显式失败（不是静默成功）。
    COUPON_CREATE = "coupon_create"


class Verdict(StrEnum):
    """人工裁决（§4.4）：`edit` 必须带非空 `edited_params`，其它必须为 `null`。"""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class ApprovalStatus(StrEnum):
    """`approval_request.status`（§5.8）：`expired` 由超时任务写，不是用户动作。"""

    PENDING = "pending"
    # `approved` / `rejected` 由 `mark_decided` 写；`expired` 只由超时清扫写。
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ActionLogStatus(StrEnum):
    """`action_log.status`（§6.2 五态）：`unknown` 是"说不清"，不是"失败"。"""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"  # 上游结果不确定，待对账
    # 幂等键挡住的那一次：上游**没有**被调用，所以它不是 failed，也不是 succeeded。
    SKIPPED_DUPLICATE = "skipped_duplicate"


class ErrorCategory(StrEnum):
    """错误分类（§5.1.2 / §9.1）：`retryable` 由类别查表得出，不手写。

    `ambiguous`（mutating 调用遇到超时/5xx）**不可重试**：上游结果不确定，重发等于赌
    "上一次没生效"（ADR-011）。
    """

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER_5XX = "server_5xx"
    RATE_LIMITED = "rate_limited"
    # 以上四类是 §9.1 允许重试的全部（见 `RETRYABLE_CATEGORIES`）。
    CLIENT_4XX = "client_4xx"
    SIGNATURE = "signature"
    AUTH = "auth"
    BUSINESS = "business"
    # mutating 调用的超时/连接/5xx：上游可能已经生效，重发等于赌（ADR-011）。
    AMBIGUOUS = "ambiguous"
    CONFIG = "config"
    UNKNOWN = "unknown"


class Attribution(StrEnum):
    """效果归因强度（§5.9）：本服务只有 `single_arm_before_after`（没有 A/B 分流能力）。"""

    SINGLE_ARM_BEFORE_AFTER = "single_arm_before_after"
    # 本服务没有分流能力：`ab_test` 只是契约里的取值，产生不了这个归因。
    AB_TEST = "ab_test"
    NONE = "none"


class ApiErrorCode(StrEnum):
    """§7.1 的 9 个错误码（冻结）：前端只按它分支，不按 HTTP 状态猜。"""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    THREAD_NOT_FOUND = "THREAD_NOT_FOUND"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    # 409 三兄弟：都表示"请求合法但当前状态不允许"，区别只在**更具体的那一条**。
    DUPLICATE_DECISION = "DUPLICATE_DECISION"
    RUN_IN_PROGRESS = "RUN_IN_PROGRESS"
    RUN_STATE_CONFLICT = "RUN_STATE_CONFLICT"
    # 502 只给上游族（`DoudianError`）；本地 bug 一律 INTERNAL_ERROR(500)。
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# §9.1 的 `retryable` 单一事实源：任何实现都**不得**在 except 分支里手写 retryable=True，
# 必须用下面这张表按类别查（§5.1.2 末段的要求）。
#
# 只放四类：timeout / connection / server_5xx / rate_limited。它们的共同点是
# **"这次没成，但重发一次可能成"**；business/client_4xx/auth/signature 重发一万次也一样。
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.TIMEOUT,
        ErrorCategory.CONNECTION,
        ErrorCategory.SERVER_5XX,
        ErrorCategory.RATE_LIMITED,
    }
)


def is_retryable_category(category: ErrorCategory | str) -> bool:
    """§9.1：只有 timeout / connection / server_5xx / rate_limited 可重试。

    注意 `ambiguous` **不在**可重试集合里：mutating 调用遇到 timeout/connection/5xx 时
    上游结果不确定，重试等于赌"上一次没生效"（ADR-011 明确禁止）。

    保证：类别非法（不在 `ErrorCategory` 里）会抛 `ValueError`——静默返回 False 会让
        "分类写错了"表现成"这个错误不该重试"，两者后果完全不同。
    不保证：不保证"可重试"就一定能成功（它只是"值得再试一次"）。
    调用方坑：入参允许 str（枚举的值），内部会 `ErrorCategory(...)` 收敛；
        不要用它判断"要不要告警"——那是 severity 的事。
    """
    return ErrorCategory(str(category)) in RETRYABLE_CATEGORIES


__all__ = [
    "RETRYABLE_CATEGORIES",
    "ActionLogStatus",
    "ActionType",
    "ApiErrorCode",
    "ApprovalStatus",
    "Attribution",
    "ErrorCategory",
    "Risk",
    "RunStatus",
    "Severity",
    "Verdict",
    "is_retryable_category",
]
