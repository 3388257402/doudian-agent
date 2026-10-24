"""枚举 —— 逐字对应 docs/CONTRACT.md §2（唯一事实源，不得增删改值）。

枚举序列化后就是这里的字符串字面量，DB 里存的也是它们（`String` 列，不建 PG enum）。
因此**不要**把这些枚举改成 `IntEnum` 或加别名：前端 `types.ts`（契约 §12）与 DB 里已有的
历史行都按字面量对齐，改一个字面量等于一次数据迁移。
"""

from __future__ import annotations

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
    UNKNOWN = "unknown"  # 上游结果不确定，待对账
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


# §9.1 的 `retryable` 单一事实源：任何实现都**不得**在 except 分支里手写 retryable=True，
# 必须用下面这张表按类别查（§5.1.2 末段的要求）。
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
