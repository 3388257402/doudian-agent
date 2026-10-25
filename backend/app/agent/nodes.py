"""8 个图节点（CONTRACT §4.3 逐字签名）+ 节点运行期依赖。

## 节点怎么拿到 client / session / notifier

契约把节点签名冻结成 `(state, config)` —— 节点不能多收一个 `client` 或 `session` 参数。
所以依赖走两处，且只有这两处：

1. `config["configurable"]["runtime"] / ["client"] / ["now"]`：测试与上层显式注入；
2. 进程级单例 `AgentRuntime`（`app.main` lifespan 里 `set_runtime()`）。

散在 8 个节点里各自 `create_doudian_client()` 是不行的：ADR-004 要求一个进程一个
`httpx.AsyncClient`，限流桶与并发信号量都挂在那个实例上。

## 重放与幂等（ADR-010）

`interrupt()` 恢复会**从节点开头重放整个 `approval_gate`**。因此：

- `approval_request` 的 upsert 是 insert-if-absent（绝不回写 `status`）；
- 通知失败不阻断（重放会再通知一次，这是被接受的代价）；
- `action_log` 的唯一键是 `idempotency_key`（UNIQUE），重放/重试第二次是
  `skipped_duplicate` 而不是真的再改一次价。

## 审计事件有**两个**出口（v1.5.1 的根因修复）

`state.audit` 是 checkpoint 里的审计留痕（可复现、可回放），但**只有读 checkpoint 的人看得见**；
`/api/logs` 只读 JSONL 汇。于是"只写 audit、不写 sink"的事件在系统里**没有任何人类出口**：
`loss_making` 是最贵的一例——它只告警、不给 `approval_request`（审批台看不见），落在
`state.audit` 里（日志页看不见），一次"这批货在亏本卖"的判定就这样消失了。

因此 `metrics.computed` / `anomaly.detected` / `sku_costs.unmatched`（以及采集层的降级留痕与
`collect.done` / `collect.failed` 两个结果事件）**同时**写两处：`state.audit`（原样，不动）+
JSONL 汇（走 `app.observability.logging` 的 sink 与统一脱敏）。这不是往 `node.trace` 里加键
——`data` 的键集合（§7.6.1）一个字都没改，是**新事件**。

只桥接上面这几个事件（含采集降级与 `collect.done` / `collect.failed`）而不是全部 audit 事件：
其余事件（`action.proposed` / `approval.decided` / `action.executed` / `effect.registered` /
`run.finished`）本来就各有**表级出口**（`approval_request` / `action_log` / `effect_snapshot` /
`agent_traces`），再抄一份进 JSONL 只会让日志翻倍而不增加任何可查性。判据是"有没有别的出口"，
不是"是不是审计"。采集的两个**结果**事件属于例外里必须补的那一类：它们**没有任何表级出口**，
而"这一轮采集到底成功了没有、为什么失败"恰恰是日志页最该回答的问题。

## 副作用与状态

节点返回的是"该字段的完整目标值"（replace 语义，§3.1），不返回增量。
不做任何 `except Exception: pass`：异常要么被记成 `errors` 并让流程降级到 `finalize`，
要么原样抛给 LangGraph 的重试策略 / `run_agent`。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from sqlalchemy import select

from app.agent.adapters import (
    require_log_sink,
    resolve_client,
    resolve_now,
    resolve_runtime,
)
from app.agent.state import (
    ActionProposal,
    AgentError,
    AgentState,
    Anomaly,
    AuditEntry,
    Decision,
    ExecutionResult,
    MetricsSnapshot,
    RawSnapshot,
    append_audits,
    append_errors,
    business_date,
    make_agent_error,
    make_audit_entry,
    utc_iso,
    utcnow,
)
from app.agent.tracing import calibrate_run_steps, digest_of, traced, update_run_trace
from app.db import arun_db, session_scope
from app.enums import ActionLogStatus, ActionType, Attribution, ErrorCategory, RunStatus, Verdict
from app.errors import ApprovalValidationError, ConfigError, ParamValidationError
from app.models import ActionLog
from app.services.errors import (
    DoudianAmbiguousOutcomeError,
    DoudianAuthError,
    DoudianBusinessError,
    DoudianError,
)

__all__ = [
    "HISTORY_WINDOW_DAYS",
    "approval_gate",
    "collect",
    "compute_metrics_node",
    "detect_anomalies_node",
    "execute",
    "finalize",
    "insert_action_log",
    "is_retryable_doudian_error",
    "load_action_log",
    "propose_actions_node",
    "record_effect",
    "route_after_collect",
    "route_after_propose_actions",
    "update_action_log",
    "validate_resume_payload",
]

#: 近 7 日均值要 7 天历史，再加今天 = 8 个业务日（§5.3 的 `gmv_7d_avg_cents`）
#: `collect` 的查询窗口就是 `[today-7, today]`，由这个常量推出（不要另写一个 8）。
HISTORY_WINDOW_DAYS = 7

#: 本模块的 stdlib logger。**只给旁路失败用**（目前是 `_log_audit` 写盘失败），
#: 业务结论一律走节点返回值里的 `errors` / `audit`，不走 logging。
#: 理由：节点是图的一部分，它的结论必须随 state 进 checkpoint（可回放）；
#: logging 只是"旁路失败"的最后一道可见性。
logger = logging.getLogger(__name__)

#: 业务日期所属时区（§0：`date` 字段是 `Asia/Shanghai` 的 `YYYY-MM-DD`）
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

#: `action_log` 的终态里"已经成功过、不该再执行"的那两个
#: `succeeded` 是"真的改过了"；`skipped_duplicate` 是"上一次执行时幂等键挡住的"。
#: 两者都意味着"上游已经处于目标状态"，重发只会让审计里多一条无意义记录。
_NO_REEXECUTE_STATUSES = frozenset(
    {ActionLogStatus.SUCCEEDED.value, ActionLogStatus.SKIPPED_DUPLICATE.value}
)

#: 售后/退款状态词（归一化用；上游是字符串枚举，这里只做"是否涉及退款"的判断）
#: 中英混排是刻意的：抖店不同接口给过英文枚举，也给过中文文案。比较时会统一 upper。
_REFUND_STATUSES = frozenset(
    {
        "REFUND",
        "REFUNDED",
        "REFUNDING",
        "AFTER_SALE",
        "APPLY_REFUND",
        "已退款",
        "退款中",
        "售后中",
    }
)


# ======================================================================================
# 通用小工具
# ======================================================================================


def _aborted(state: Mapping[str, Any]) -> bool:
    """前面的节点已经把 run 判死：后续节点不再产生任何副作用。

    保证：只看 `status == failed` 一个判据（唯一"已判死"的信号）。
    不保证：不看 `errors` 是否为空——有错误不等于判死（非致命错误只进 `errors`）。
    调用方坑：每个节点开头都调它并 `return {}`：**空 dict 是"什么都不改"**，
        不是"把字段清空"（replace 语义下，返回 `{}` 等于零改动）。
    """
    return str(state.get("status") or "") == RunStatus.FAILED.value


def _audit(state: Mapping[str, Any], *, node: str, event: str, data: dict[str, Any] | None = None,
           at: str | None = None) -> list[AuditEntry]:
    """往 `state.audit` 里**追加**一条审计事件，返回完整新列表（不是增量）。

    保证：返回的是"旧列表 + 这一条"的完整列表，且按 `event_id` 去重（`append_audits` 的语义），
        因此节点被重放时不会出现两条同 id 的记录。
    不保证：**不写 JSONL**。本函数只服务 checkpoint 里的审计；要在日志页上看得见，
        调用点必须再配一次 :func:`_log_audit`（"双出口"是调用点的义务，不是本函数的）。
    调用方坑：返回值必须赋给节点的 `"audit"` 键——直接调用而丢掉返回值等于什么都没记；
        且多次累积时要拿上一次的返回值继续传（`append_audits({"audit": audits}, ...)`）。
    """
    return append_audits(
        {"audit": list(state.get("audit") or [])},  # type: ignore[arg-type]
        [make_audit_entry(node=node, event=event, data=data or {}, at=at)],
    )


#: 异常 `severity` → JSONL `level`（§7.6 的取值域：critical→error、warn→warn、info→info）。
#:
#: `critical` 必须落 `error`：`/api/logs?run_id=…&level=error` 是运营找"今天哪里红了"的入口，
#: 若 `loss_making`（critical）写成 info，日志页上同一条事实就会同时在"红"和"绿"两种口径下
#: 自相矛盾——而它恰恰是唯一没有任何别的出口的那条告警。
#: 这张表是"severity 决定 level"的唯一实现（不要在各调用点再写一次映射）。
_SEVERITY_LEVEL: dict[str, str] = {
    "critical": "error",
    "warn": "warn",
    "info": "info",
}


def _log_audit(
    state: Mapping[str, Any],
    *,
    node: str,
    event: str,
    message: str,
    data: Mapping[str, Any] | None = None,
    level: str = "info",
) -> None:
    """把一条审计事实写进 JSONL 汇（**只写汇**；`state.audit` 由调用点自己 append）。

    为什么必须有这一条路（根因，不是措辞修正）：`/api/logs` 只读 JSONL 汇，而审计事件过去
    只进 `state.audit`（checkpoint）。于是"指标算出来了 / 有异常了 / 成本表有条目对不上"这些
    事实在**所有人类入口上都不存在**，`loss_making` 因此变成一条发出去也看不见的告警。

    **为什么这里不能顺带写 `state.audit`**：传进来的 `state` 是 `Mapping`（不可变视图），
    本函数改不动它；审计条目要随节点返回值一起进 state。所以"双出口"是**每个调用点各写一次**
    保证的（`append_audits(...)` + `_log_audit(...)` 成对出现），不是由这个函数保证的。
    早先这里写"同时写进"是错的——审查实测发现调用后 `state["audit"]` 仍是 `[]` 才暴露，
    属于"注释声称的契约"与"代码真实的契约"不一致，与本项目别处的几次事故同一类。

    脱敏不用自己做：`JsonlLogSink.write` 在落盘前对 `message` 与 `data` 统一走
    `redact_any`（键名层 + 值级 scrub），这正是"复用 sink"而不是"new 一个 logger"的理由。

    重放与重复：节点被重试/重放时这会多写一条（与 `node.trace` 的行为一致——一次真实执行
    一条）。审计去重靠 `state.audit` 的 `event_id`，日志是事件流，不做去重。

    ## 写盘失败：旁路失败不许改变业务结论（v1.5.1）

    JSONL 是**旁路**：它的人类可读副本 + `/api/logs` 的查询入口。磁盘满 / 句柄失效（`OSError`）
    时若让异常逃出本函数，它会从节点一路逃到 `POST /api/agent/run` → **整轮 500**；而
    `state.audit` 那一份也会一起丢（调用点先 `append_audits(...)` 再调本函数，异常在
    `return` 之前炸掉，节点的返回值根本构造不出来）。

    所以这里**必须**接住写盘异常，但**不许静默**：用 stdlib `logging` 打一条 warning，
    带上 event 名与异常类型。为什么这不是"用 try/except 掩盖问题"——掩盖的判据是
    "缺陷是否因此不可见"，而不是"有没有 except"：

    - 被接住的是**不影响业务结论的旁路失败**（少一行日志，指标/审批/执行全都不变）；
    - 缺陷本身（磁盘满、文件句柄失效）由那条 warning 暴露，运维照样看得见；
    - 而 `state.audit` 的那一份仍然随节点返回值进 checkpoint——审计**没有**被这条 except 吞掉。

    反面做法（不接）等于让"日志写不进去"升级成"当天指标算不出来"，那才是把旁路当主路。
    """
    try:
        sink = require_log_sink()
        # 身份字段只取三个且统一转成 str：`None` 在日志里保留 None（区分"没有这个值"
        # 与"值是字符串 'None'"），这在按 run_id 过滤时很关键。
        identity = {
            key: (None if state.get(key) is None else str(state.get(key)))
            for key in ("run_id", "trace_id", "shop_id")
        }
        sink.write(
            event,
            level=level,
            message=message,
            data=dict(data or {}),
            node=node,
            **identity,
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring：旁路失败不许打断业务主路径
        # 注意日志内容里必须同时有 event 与异常类型：只写"写日志失败"等于把缺陷本身也藏起来。
        logger.warning(
            "JSONL 审计写入失败（event=%s，异常类型=%s）：%s；state.audit 仍然保留",
            event,
            type(exc).__name__,
            exc,
        )


#: 降级/拒绝审计里**最多保留**的现场条数（见 :func:`_degradation_summary`）
#: 5 条足够看出"上游把哪个字段写坏了"的形态，又不会把审计/日志刷成几百行。
_DEGRADATION_SAMPLE_LIMIT = 5


def _degradation_summary(kind: str, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把采集层的降级现场汇总成一条可排障的审计 `data`（按原因计数 + 前 5 条现场）。

    为什么只留前 5 条：一次上游改字段可能让几百条订单行同时降级，整块塞进审计/日志会让
    "哪几种写法出现了"这个真正的问题被淹掉；而前 5 条已经足够看出形态（`missing` / `non_positive`）。

    为什么必须显式给出 `sample_limit` / `samples_truncated`（v1.5.1）：`count` 是**全量**，
    `samples` 只有前 5 条。只写 `samples` 会让读者以为"一共就这几条"，把"400 行被拒"读成
    "1 行被拒"——恰好是这类审计最需要说清的规模。`samples_truncated` 明确"还有更多没贴"。

    保证：`count` 是全量条数；`reasons` 的计数之和 == `count`（缺失原因归 `unknown`）。
    不保证：不保证样本代表全貌——它只是**前 5 条**（按上游返回顺序，不按严重程度排序）。
    调用方坑：`kind` 是同一 event 名下多种 schema 的判别键（`units` / `row_rejected` /
        `date_fallback`），消费方按它分支，不要靠"有没有某个字段"去猜形状。
    """
    counts: dict[str, int] = {}
    for item in items:
        # `reason` 缺失时归到 `unknown` 而不是丢掉：分类的计数之和必须等于 `count`，
        # 否则"按原因计数"本身就成了一个对不上的账。
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return {
        "kind": kind,
        # `count` 是全量（不是样本数）：这两者的区别是本函数存在的理由，见 docstring。
        "count": len(items),
        "reasons": counts,
        # 样本上限与"是否被截断"和样本本身一起返回：消费方不必知道 `[:5]` 这个魔法数。
        "sample_limit": _DEGRADATION_SAMPLE_LIMIT,
        "samples_truncated": len(items) > _DEGRADATION_SAMPLE_LIMIT,
        "samples": [dict(item) for item in items[:_DEGRADATION_SAMPLE_LIMIT]],
    }


def _errors(state: Mapping[str, Any], errors: Sequence[AgentError]) -> list[AgentError]:
    """把新错误并进 `state.errors`，返回完整新列表（按 (node, category, message) 去重）。

    保证：返回"旧错误 + 新错误"的完整列表，重放不会让同一条错误翻倍。
    不保证：不落库、不进 JSONL，也不改变 `status`——"有错误"与"这一轮死了"是两件事，
        判死由调用方显式写 `status=failed`（`traced()` 再据此记 `outcome=error`）。
    调用方坑：必须先算好完整列表再赋值给 `"errors"`；`errors` 是 replace 语义（§3.1），
        直接 `state["errors"].append(...)` 会被 LangGraph 丢弃。
    """
    return append_errors({"errors": list(state.get("errors") or [])}, list(errors))  # type: ignore[arg-type]


def _shift_business_date(day: str, offset_days: int) -> str:
    """业务日期字符串 `YYYY-MM-DD` 平移 `offset_days` 天（返回同形状字符串）。

    保证：纯日历运算（`date + timedelta`），跨月/跨年/闰日都正确。
    不保证：不处理时区与夏令时——入参是**已经折算好**的 Asia/Shanghai 业务日期，
        不要拿它去平移 UTC 时间戳。
    调用方坑：非法日期串会抛 `ValueError`（`date.fromisoformat`），本函数不兜底；
        `collect` 的入参来自 `business_date(now)`，形状由那里保证。
    """
    base = date.fromisoformat(day)
    return (base + timedelta(days=offset_days)).isoformat()


class _BadBusinessDate(ValueError):
    """**行级**业务日期解析不出来（`strict` 模式专用，v1.5.1）。

    `_business_date_of` 的非 strict 行为是"解析不出来就回退 `fallback`"，那是给窗口下界、
    `summary` 作用域这类**非行级**调用点用的（它们回退到今天是有界、可解释的）。订单行不行：
    把一行的垃圾 `create_time` 回退成"今天"，等于凭空造出一笔当日成交——实测
    `create_time="2026-13-45 99:99:99"` 会得到 `gmv=555 / orders=1`，与"猜不出含义就整行拒绝"
    正好相反。所以行级路径用 `strict=True`，由 :func:`_row_business_date`（它按
    `create_time` → `pay_time` 逐字段尝试）汇总成 `_RowRejected(reason="bad_create_time")`；
    越界时间戳（Windows 上 `fromtimestamp` 抛
    `OSError`、其他平台可能是 `ValueError`/`OverflowError`）也在这里被统一成该异常，
    不再从节点逃出去变成 `POST /api/agent/run` 的 500。
    """


def _business_date_of(value: Any, *, fallback: str, strict: bool = False) -> str:
    """把上游的时间（带时区 ISO / 无时区北京时间 / 秒/毫秒时间戳）折成业务日期。

    无时区的时间戳按 **Asia/Shanghai** 解释：抖店开放平台的 `create_time` 字段就是北京时间，
    把它当 UTC 会让"昨晚 23:30 的单"落到错误的一天，指标与规则一起错。

    ## `strict` 只给**行级**路径用（v1.5.1）

    `strict=False`（默认，既有行为**一字不改**）：解析不出来 / 类型不认识 → 返回 `fallback`。
    `strict=True`：除了 `None`（"没给日期"，不是"给了垃圾"）仍然回退 `fallback` 之外，
    解析失败、时间戳越界、类型错误都会抛 :class:`_BadBusinessDate`——调用方（订单行解析）
    据此整行拒绝，而不是把这行悄悄算进今天。

    为什么时间戳要接三种异常：`datetime.fromtimestamp` 的越界表现**跨平台不一致**——
    Windows 上是 `OSError(22, Invalid argument)`，Linux/CPython 其他路径上可能是
    `ValueError` 或 `OverflowError`。只接 `ValueError` 就会"CI 绿、线上红"。

    ## 这里**不接** `TypeError`（v1.5.2，与 `_best_effort_units` 的口径无关）

    参数在每一支里都已先按类型收窄（`datetime` / `int|float` / `str`），能抛
    `TypeError` 的只剩我们自己的代码（例如改了 `_BUSINESS_TZ` 的类型、或传了个
    `datetime` 子类把方法写坏）。把它和"上游给了坏日期"一起折成 :class:`_BadBusinessDate`，
    在 `strict=True` 的行级路径上就会变成"这一行脏"的整行拒绝——正是本仓明令禁止的
    "代码 bug 被降级成数据脏"。所以 `TypeError` 必须照常传播；`_best_effort_units`
    是**另一条旁路**（只喂审计样本、不影响业务结论），那里保留 `TypeError` 是有意的，
    两处口径不同但都各自自洽。
    """

    def _bad(detail: str) -> str:
        # 两种口径的唯一分叉点：strict 抛（行级路径整行拒绝），非 strict 回退 `fallback`
        # （窗口下界/summary 作用域这类"回退到今天是有界可解释的"调用点）。
        if strict:
            raise _BadBusinessDate(detail)
        return fallback

    if isinstance(value, datetime):
        try:
            # 无 tzinfo 的 datetime 按北京时间补：抖店给的 `create_time` 就是北京时间，
            # 当 UTC 解释会让"昨晚 23:30 的单"落到错误的一天。
            resolved = value if value.tzinfo else value.replace(tzinfo=_BUSINESS_TZ)
            return resolved.astimezone(_BUSINESS_TZ).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError) as exc:
            return _bad(f"无法把 datetime 折算成业务日期：{exc}")
    if isinstance(value, bool):
        # bool 是 int 的子类：不先拦下来，`create_time=True` 会被当成 1970-01-01 01:33:20+08:00。
        return _bad(f"时间不能是 bool，收到 {value!r}")
    if value is None:
        # "没给日期"不是"给了垃圾"：strict 模式下也回退（回退是有界的），只有垃圾才抛。
        return fallback
    if isinstance(value, (int, float)):
        try:
            # 毫秒时间戳的量级判别：> 1e11 视为毫秒（约等于公元 5138 年的秒级时间戳），
            # 抖店两种都给过，按量级分流比按字段名猜更稳。
            seconds = float(value) / (1000.0 if float(value) > 1e11 else 1.0)
            return datetime.fromtimestamp(seconds, UTC).astimezone(_BUSINESS_TZ).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError) as exc:
            # 包含 NaN/inf（ValueError）与超范围整数（OSError/OverflowError，平台相关）。
            return _bad(f"时间戳 {value!r} 超出可表示范围：{exc}")
    if not isinstance(value, str):
        return _bad(f"时间必须是字符串/数字/datetime，收到 {type(value).__name__}")
    text = value.strip()
    if not text:
        return _bad("时间文本为空（只有空白字符）")
    parsed: datetime | None = None
    try:
        # `Z` 要换成 `+00:00`：老版本 CPython 的 `fromisoformat` 不认 `Z`。
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # 三种常见写法兜底（抖店不同接口给过 `YYYY-MM-DD HH:MM:SS` / 无秒 / 纯日期）。
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return _bad(f"无法解析时间文本 {text!r}")
    try:
        resolved = parsed if parsed.tzinfo else parsed.replace(tzinfo=_BUSINESS_TZ)
        return resolved.astimezone(_BUSINESS_TZ).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError) as exc:
        return _bad(f"无法折算时间文本 {text!r} 的业务日期：{exc}")


def _money(value: Any, *, where: str) -> int:
    """金额归一化：`*_cents`/`*_yuan` 之外的裸字段按**整数分**读（抖店开放平台约定）。

    - `*_yuan` → `round(x*100)`；
    - 裸字段（`price` / `pay_amount` / `refund_amount`）→ 必须是 int（分）；
    - `float` / `str` / `bool` → `ValueError`：在边界炸，不做"元还是分"的静默猜测。
      （一次猜错就是资损级歧义，见 DECISIONS 附 A「金额一律整数分」。）
    """
    if isinstance(value, bool):
        # bool 是 int 的子类：不先拦，`pay_amount=True` 会被当成 1 分静默计入 GMV。
        raise ValueError(f"{where}: 金额不允许 bool")
    if isinstance(value, int):
        return value
    # float/str（"199.00"）一律炸：那正是"元还是分"猜错一次就资损的那类歧义，
    # 由调用方（`_read_order_row`）整行拒绝，而不是在这里猜单位。
    raise ValueError(f"{where}: 金额必须是整数分 int，收到 {value!r}")


def _first_int(source: Mapping[str, Any], names: Sequence[str], *, where: str) -> int | None:
    """按候选字段名依次取值，命中第一个**非 None** 的走 :func:`_money` 归一化。

    保证：多字段兼容（`pay_amount_cents` / `pay_amount` 这类新旧写法）只在这里定义一次，
        下游拿到的永远是"整数分或 None"。
    不保证：不保证值合法——非 int 会由 `_money` 抛 `ValueError`（带 `where`），
        所以"金额形状错"与"没有金额"是两条不同的路径，不要混着兜。
    调用方坑：`None` 表示"这些字段一个都没有"，**不是 0**；把 None 当 0 会让
        "上游少给金额"变成"这单 0 元"。`where` 必须给得足够具体（含字段路径），
        它会被拼进异常消息，是排障时唯一的定位信息。
    """
    for name in names:
        # `name in source and source[name] is not None`：显式排除 None 而不是用 `or`，
        # 否则合法的 `0`（0 元单）会被当成"没有这个字段"。
        if name in source and source[name] is not None:
            return _money(source[name], where=f"{where}.{name}")
    return None


#: 订单行里"买了几件"的字段名（抖店不同接口/不同版本的写法；命中第一个即可）
#: 顺序即优先级：`product_count` 是最规范的那个，`quantity` 只在少数接口出现。
_ORDER_UNIT_KEYS: tuple[str, ...] = ("product_count", "goods_count", "quantity")


def _order_units(
    item: Mapping[str, Any],
    *,
    where: str,
    order_id: str | None = None,
    degraded: list[dict[str, Any]] | None = None,
) -> int:
    """一条订单行卖出的**件数**（成本摊分靠它：成本额 = 单价成本 × 件数）。

    ## 缺件数 / 件数非正 → 按 1 件，但**降级必须可见**

    上游没给件数（`missing`）或给了非正数（`non_positive`，如 `product_count: 0`）时按 1 件计，
    并把现场追加进 `degraded`（调用方 `_aggregate_orders` → `collect` 据此记一条 warn 级审计）。
    为什么容错而不是抛异常（v1.5.1 的根因修复）：`product_count: 0` 这样的脏数据若直接抛，
    异常会从 `collect` 逃出节点 → 图把 run 标 `failed` 并 re-raise 到 `POST /api/agent/run`
    → **整轮 500**，一条脏数据打死当天所有指标（毛利率等成本口径还会因此永远算不出来）。

    ## 这个假设的方向是**偏乐观**，不是"偏保守"（v1.5.1 更正了写反的注释）

    件数偏小 → 成本额偏小 → 毛利率偏高 → 更**不容易**报"亏本" → 后果是**漏报**（把亏本看成暴利）。

    算例（真实数字，来自 demo 夹具 `config/sku_costs.yaml`）：真丝衬衫成本 **28000 分/件**；
    一行 `pay_amount=199000`（10 件 × 199 元）但**没有** `product_count` → 按 1 件算：

        按 1 件（错）：毛利率 = (199000 - 28000×1)  / 199000 = **+0.8593**
        按 10 件（对）：毛利率 = (199000 - 28000×10) / 199000 = **-0.4070**
        偏差 = +0.8593 - (-0.4070) = **+126.6 个百分点**（方向：亏本 → 暴利）

    ## 什么能降级、什么不能

    - **能降级**：取值越界（`0` / 负数）——含义明确（"这件货没记件数/写错了"），按 1 件
      是有界、可解释的降级；
    - **不能降级成"1 件"**：类型不对（float / 字符串 / bool）→ 仍然 `ValueError`。那是
      "上游改了字段类型"，静默取 1 会把"每单都只买一件"永久藏起来。
    - 但**不是**"继续打死整轮"（v1.5.1）：这个 `ValueError` 由 :func:`_read_order_row` 接住，
      该行**整行拒绝**、记一条 `collect.row_rejected` 审计，其他行照常算——这是"逐行拒绝"
      与"按 1 件降级"两种降级的分界线：**能猜出含义的按 1 件；猜不出含义的整行不参与指标。**

    ## 样本 schema（v1.5.1：`missing` 与 `non_positive` 统一，且都带 `order_id`）

    只给页内下标 `order[3]` 的话，跨 run / 重试就没法定位到底是哪一单（下标会变）；
    所以样本统一是 `{order_id, where, keys, key, value, reason}` 六个键：`missing` 的
    `key` / `value` 为 `None`，`non_positive` 带上命中字段与非法值，`keys` 是候选字段清单。
    """
    for key in _ORDER_UNIT_KEYS:
        if key not in item or item[key] is None:
            # 键不存在/值为 None = 上游没给件数 → 继续试下一个候选字段。
            continue
        value = item[key]
        if isinstance(value, bool) or not isinstance(value, int):
            # 与同文件的 `_money` 一致：数据形状错误统一用 ValueError（调用方只接一种）。
            # 类型错**不**降级成 1 件：那是"上游改了字段类型"，静默取 1 会把
            # "每单都只买一件"永久藏起来。
            raise ValueError(  # noqa: TRY004
                f"{where}.{key}（件数）必须是整数，收到 {value!r}"
            )
        if value <= 0:
            if degraded is not None:
                # 0/负数 = 含义明确（"这货没记件数/写错了"），按 1 件是有界降级，
                # 但必须把现场留进样本，否则毛利率会静默偏乐观（见 docstring 算例）。
                degraded.append(
                    {
                        "order_id": order_id,
                        "where": where,
                        "keys": list(_ORDER_UNIT_KEYS),
                        "key": key,
                        "value": value,
                        "reason": "non_positive",
                    }
                )
            return 1
        return value
    if degraded is not None:
        # 三个候选字段都没有 = `missing`（与 `non_positive` 分开计原因，排障方向不同）。
        degraded.append(
            {
                "order_id": order_id,
                "where": where,
                "keys": list(_ORDER_UNIT_KEYS),
                "key": None,
                "value": None,
                "reason": "missing",
            }
        )
    return 1


def _as_list_raw(payload: Any, names: Sequence[str]) -> list[Any]:
    """从上游 `data` 里取出条目列表的**原始元素**（不过滤元素形状）。

    为什么订单侧必须拿原始元素（v1.5.1）：`{"shop_order_list": ["x", 1, None]}` 里的三个元素
    是"上游确实给了、但一个都不是对象"的行。旧实现由 :func:`_as_list` 直接丢掉它们，于是
    聚合结果是 `gmv=0 / orders=0`，只有一条 `collect.done`——"看到了但没解析出来"被伪装成
    "今天没卖东西"。订单侧要把它们按 `row_rejected(reason="not_an_object")` 计入拒绝率，
    因此先取原始元素、再在 :func:`_aggregate_orders` 里逐行分类。
    """
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, Mapping):
        for name in names:
            value = payload.get(name)
            # 只认第一个**是 list** 的候选字段：值为 None/别的类型时继续往后找，
            # 让"哪个字段算订单列表"这件事也只有一处定义（`_ORDER_LIST_KEYS`）。
            if isinstance(value, list):
                return list(value)
    return []


def _as_list(payload: Any, names: Sequence[str]) -> list[Mapping[str, Any]]:
    """从上游 `data` 里取出**对象**条目列表（`{"<name>": [...]}` 或裸列表）。

    商品列表这类"非对象元素本来就没有意义、也不参与任何指标"的调用点用它：过滤掉非 Mapping
    不会伪造出业务结论。订单列表请用 :func:`_as_list_raw`——那里的非对象元素必须计入
    `row_rejected`，不能再被静默丢弃。
    """
    return [item for item in _as_list_raw(payload, names) if isinstance(item, Mapping)]


# ======================================================================================
# action_log 仓储（执行幂等的唯一来源，§6.2；本模块是它的唯一写入方）
# ======================================================================================


async def load_action_log(idempotency_key: str) -> dict[str, Any] | None:
    """按 `idempotency_key` 取执行记录。

    保证：返回 §6.2 那一行的纯 dict（含 `status` / `attempt` / `request_params` / `response`）；
        不存在时给 `None`（这是**正常**返回值，`execute` 靠它区分首跑与重放）。
    不保证：不保证 `response` 是完整上游响应——成功时只存 `{ok, request_id, data_digest}`
        这一份最小摘要（见 `execute` 的落库处），拿它当"上游返回了什么"会失望。
    调用方坑：`status` 是字符串而不是 `ActionLogStatus`；比较时用 `.value` 或字符串，
        不要拿枚举本身做 `==`（StrEnum 下多数情况能过，但混进普通 str 会假不等）。
    """

    def _fn(session: Any) -> dict[str, Any] | None:
        row = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "action_log_id": row.action_log_id,
            "idempotency_key": row.idempotency_key,
            "action_id": row.action_id,
            "run_id": row.run_id,
            "shop_id": row.shop_id,
            "action_type": row.action_type,
            "target": row.target,
            "request_params": dict(row.request_params or {}),
            # JSON 列在 SQLite 上可能读回 str（历史行/手写 SQL）：只有 Mapping 才折成 dict，
            # 其余给 None，避免把一段 JSON 文本当字典用。
            "response": dict(row.response) if isinstance(row.response, Mapping) else None,
            "status": row.status,
            "attempt": int(row.attempt or 1),
            "upstream_request_id": row.upstream_request_id,
            "error_category": row.error_category,
            "error_message": row.error_message,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "finished_at": row.finished_at,
        }

    return await arun_db(_fn)


async def insert_action_log(
    *,
    idempotency_key: str,
    action_id: str | None,
    run_id: str,
    shop_id: str,
    action_type: str,
    target: str,
    request_params: Mapping[str, Any],
    status: ActionLogStatus | str = ActionLogStatus.PENDING,
    attempt: int = 1,
    now: datetime | None = None,
) -> bool:
    """insert-if-absent（`UNIQUE(idempotency_key)`）。返回 True = 本次插入成功。

    并发撞 `IntegrityError` 时**重查并跳过**（§6.3 第 6 条：不用 `INSERT OR REPLACE` /
    `ON CONFLICT`，否则会把别人写的结果覆盖掉）。

    保证：`idempotency_key` 至多一行；返回 `True` 严格等于"这一行是我插的"。
    不保证：不保证拿到 `False` 就一定是别人插的——也可能是上一次重放插的（两种都该 skip）。
    调用方坑：插入后 `status` 是 `pending`，**必须**在真正下发上游之后调 `update_action_log`
        写终态；否则崩溃窗口里那一行会永远 pending，后续执行被当成"结果不确定"（ADR-011）。
    """
    from sqlalchemy.exc import IntegrityError

    stamp = now or utcnow()

    def _fn(session: Any) -> bool:
        existing = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return False
        session.add(
            ActionLog(
                # `action_log_id` 直接等于幂等键：§6.1 只有一套 ID，另一套迟早会走散。
                action_log_id=idempotency_key,
                idempotency_key=idempotency_key,
                action_id=action_id,
                run_id=run_id,
                shop_id=shop_id,
                action_type=action_type,
                target=target,
                request_params=dict(request_params),
                # response 此刻必然为空：还没来得及调上游。
                response=None,
                status=str(status),
                attempt=attempt,
                upstream_request_id=None,
                error_category=None,
                error_message=None,
                created_at=stamp,
                updated_at=stamp,
                finished_at=None,
            )
        )
        try:
            # 立即 flush 把唯一约束冲突暴露在这里（而不是等事务提交时抛在一个无关的位置）。
            session.flush()
        except IntegrityError:
            # 并发窗口：别人先插了。重查一次；查到 = 本次算跳过（`again is None` 为 False）。
            session.rollback()
            again = session.execute(
                select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            return again is None  # 重查有行 = 别人先插了，本次算跳过
        return True

    return bool(await arun_db(_fn))


async def update_action_log(
    idempotency_key: str,
    *,
    status: ActionLogStatus | str | None = None,
    response: Mapping[str, Any] | None = None,
    upstream_request_id: str | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
    attempt: int | None = None,
    now: datetime | None = None,
) -> None:
    """更新执行记录；写入终态时补 `finished_at`。

    保证：`status` 落在四个终态之一时同一个事务里补 `finished_at`（"这个动作什么时候结束的"
        只有一个来源，不靠调用方另写一次）。
    不保证：不保证行存在——不存在直接抛 `LookupError`（静默跳过等于把"幂等键丢了"藏起来）。
        也不做状态机校验：允许 `succeeded → failed` 这类回退（重试确实会这么发生）。
    调用方坑：所有参数都是"给了才写"，`None` 表示"这次不改这一列"，**不是**"把它清空"；
        想把 `error_message` 清掉，这里没有口子（也不要为了它加一个）。
    """
    stamp = now or utcnow()
    # 四个终态：只有落在这里才写 finished_at。`pending` 不是终态（还没下发）。
    terminal = {
        ActionLogStatus.SUCCEEDED.value,
        ActionLogStatus.FAILED.value,
        ActionLogStatus.UNKNOWN.value,
        ActionLogStatus.SKIPPED_DUPLICATE.value,
    }

    def _fn(session: Any) -> None:
        row = session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if row is None:
            # 没有行就没有"执行"可言：静默什么都不做会让"幂等键丢了"永远不被发现。
            raise LookupError(f"action_log 缺少 idempotency_key={idempotency_key}")
        if status is not None:
            row.status = str(status)
            if str(status) in terminal:
                row.finished_at = stamp
        if response is not None:
            # 整块覆盖（不是合并）：上游响应是"这一次"的快照，合并两份会造出一个
            # 从没存在过的响应体。
            row.response = dict(response)
        if upstream_request_id is not None:
            row.upstream_request_id = upstream_request_id
        if error_category is not None:
            row.error_category = error_category
        if error_message is not None:
            row.error_message = error_message
        if attempt is not None:
            row.attempt = attempt
        row.updated_at = stamp

    await arun_db(_fn)


# ======================================================================================
# 1) collect
# ======================================================================================


def is_retryable_doudian_error(exc: BaseException) -> bool:
    """节点级重试的判据（§9.2）：只有抖店异常族里 `retryable=True` 的类别才重试。

    这条判据同时是 `RetryPolicy.retry_on`：`GraphBubbleUp`（HITL 挂起）、
    `ApprovalValidationError`（本地校验错）、`business`/`client_4xx`/`auth`/`signature`
    一律**不重试**——重试一个"参数错了"或"token 没有权限"只会把同一个错误重复四遍。

    保证：非抖店异常恒为 `False`（本地 bug 不会被重试策略掩盖成"上游抖动"）。
    不保证：不保证 `retryable=True` 就一定该重发——`ambiguous`（mutating 调用的超时/5xx）
        不在可重试类别里是**上游族自己**的语义（ADR-011），本函数只转发不判断。
    调用方坑：它是 `retry_on` 回调，会被 LangGraph 对**每个**异常调用一次（含 `GraphInterrupt`），
        所以必须写成纯函数、不能有副作用，也不能在这里打日志（重试 N 次就会写 N 条）。
    """
    return isinstance(exc, DoudianError) and bool(exc.retryable)


def _note_call(source: dict[str, Any], method: str, response: Any) -> None:
    """就地记录"这次调了哪个上游方法 + 上游给的 request_id"（写进 `RawSnapshot.source`）。

    保证：`method` 一定会被 append（调用过就是调用过）；`request_id` 只在非空时记。
    不保证：不保证成功/失败——失败路径不调用本函数（那时 `response` 根本不存在），
        所以 `source.methods` 是"成功调用过的方法"，不是"尝试过的方法"。
    调用方坑：**就地修改** `source`（无返回值）；这与"节点返回完整目标值"的 replace 语义
        不冲突，因为 `source` 是节点自己新建的局部对象，最后整个塞进 `RawSnapshot`。
    """
    source["methods"].append(method)
    request_id = getattr(response, "request_id", None)
    if request_id:
        # 转 str：上游可能给 int，`source` 要能无歧义地序列化进 checkpoint。
        source["request_ids"].append(str(request_id))


def _normalize_shop(payload: Any) -> dict[str, Any]:
    """店铺信息 → `RawSnapshot.shop`（`shop_id`/`shop_name` 归一化，其余字段原样保留）。

    保证：两个关键字段（`shop_id` / `shop_name`）一定存在，且要么是 str 要么是 `None`；
        上游的 `id` / `name` 变体会被折到标准名上。
    不保证：不保证形状是 §5.5 的店铺契约——其余字段原样透传，本函数只做改名不做校验。
    调用方坑：非 Mapping 输入返回 `{}`（**不是**抛错）：店铺画像是非必需数据，
        缺了只该让 `partial=True`，不该打死整轮采集。
    """
    if not isinstance(payload, Mapping):
        return {}
    shop = dict(payload)
    # 两个变体都认（`id`/`name` 是抖店不同接口的写法），标准名优先：
    # 用 `or` 而不是 `get(k, default)`，因为上游会把"没这个字段"写成 `null` 而不是缺键。
    shop_id = shop.get("shop_id") or shop.get("id")
    shop["shop_id"] = str(shop_id) if shop_id is not None else None
    name = shop.get("shop_name") or shop.get("name")
    shop["shop_name"] = str(name) if name is not None else None
    return shop


def _normalize_product(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """一个商品/规格 → §5.5 `PRODUCT_FIELDS` 形状；金额一律整数分。

    保证：返回的 dict 键集合固定（§5.5 的八个字段都在），金额是 int 分。
    不保证：不保证这个商品有效——`sku_id`/`product_id` 都没有时返回 `None`（调用方跳过它），
        因为"没有身份的条目"无法归属到任何 SKU，留着只会污染成本交叉校验。
    调用方坑：`stock` 非法（bool/负数/非 int）会抛 `ValueError`，由 `collect` 收成
        `errors` 并降级为 `partial`；本函数不返回空值来"吞掉"坏数据。
    """
    sku_id = item.get("sku_id")
    product_id = item.get("product_id") or item.get("id")
    if sku_id is None and product_id is None:
        # 两个身份都没有：这条没法归属，直接判"不是商品"（`collect` 会跳过）。
        return None
    # `where` 会进异常消息：带上能识别这个条目的 id，排障时不必再猜是哪一条。
    where = f"product[{sku_id or product_id}]"
    price_cents = _first_int(item, ("price_cents", "price"), where=where)
    if price_cents is None:
        # 唯一允许的"元→分"换算点：字段名自带 `_yuan` 才是元，裸字段一律按分读。
        price_yuan = item.get("price_yuan")
        if price_yuan is not None:
            price_cents = int(round(float(price_yuan) * 100))
    stock = item.get("stock")
    if stock is not None and (
        isinstance(stock, bool) or not isinstance(stock, int) or stock < 0
    ):
        # 库存为负/非整数没有合理语义：炸出来，别静默写成 0（0 = "卖光了"，是另一个结论）。
        raise ValueError(f"{where}.stock 必须是非负 int，收到 {stock!r}")
    return {
        "product_id": str(product_id) if product_id is not None else None,
        "sku_id": str(sku_id) if sku_id is not None else None,
        "title": str(item.get("title") or item.get("name") or ""),
        "price_cents": price_cents,
        # 缺失的库存按 0：这是展示字段，不参与任何告警判定。
        "stock": int(stock) if stock is not None else 0,
        "gmv_cents": _first_int(item, ("gmv_cents", "sales_gmv_cents"), where=where) or 0,
        "refund_order_count": int(item.get("refund_order_count") or 0),
        "is_on_sale": bool(item.get("is_on_sale", item.get("on_sale", True))),
    }


def _is_refunded(item: Mapping[str, Any], *, refund_amount: int) -> bool:
    """这一行是否涉及退款：状态词命中，或退款额 > 0。

    退款额由调用方（:func:`_read_order_row`）**先**校验成非负整数分再传进来：这样
    `refund_amount: "1.00"` 这种形状错会变成"整行拒绝"的一条审计，而不是在这里再解析一次
    （同一字段解析两遍，两处校验迟早会分叉）。
    """
    status = str(item.get("refund_status") or "").strip().upper()
    if status and status in {word.upper() for word in _REFUND_STATUSES}:
        # 集合每次重建（不是模块级常量）：`_REFUND_STATUSES` 里既有大写也有中文，
        # 归一化必须在**比较时**做，预先把常量 upper 一遍会破坏中文项的可读性。
        return True
    # 状态词没命中也不算"没退款"：金额 > 0 就是退款（状态字段上游经常不填）。
    return refund_amount > 0


#: 上游 `summary` 里可能声明"这段汇总覆盖哪一天"的字段（v1.3.2）
#: 两者都试（顺序固定）：上游不同版本给不同的键，只认一个会让 uv 作用域永远判不出来。
_UV_SCOPE_DATE_KEYS: tuple[str, ...] = ("date", "business_date")


def _summary_uv(summary: Mapping[str, Any]) -> int | None:
    """`summary.uv` → 非负 int；缺键 → `None`；有值但不合法 → :class:`_BatchContractError`。

    `uv` 是**可缺**字段（§5.3：缺 → `uv=None` / `cvr=None`），"没有"不是错；
    但"有值却不是非负整数"必须当场炸：静默丢掉会让"上游改了形状"永远表现成
    "这家店没有访客数据"——把一次数据结构变更伪装成正常的无数据，正是 §5.3 要防的那类错。

    ## 为什么抛的是**整批**契约错而不是裸 `ValueError`（v1.5.1）

    裸 `ValueError` 会逃出 `_aggregate_orders` → `collect` → 图把 run 标 `failed` 后 re-raise
    → `POST /api/agent/run` **500**。这与 F1b 已经确立的分级自相矛盾：整批形状错（订单容器、
    订单列表字段）走"记错 + `status=failed` + 不 500"，`summary.uv` 却把同一类事故升级成栈。
    改成 `_BatchContractError` 后走**同一条路径**：错误照样进 `errors` 与 `status=failed`，
    可观测性一点没少，API 侧不再 500。
    """
    value = summary.get("uv")
    if value is None:
        # "没有"不是错：`uv` 是可缺字段（§5.3），缺了就让 uv/cvr 一起是 None。
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        # 有值但形状不对必须炸：静默丢掉会让"上游改了形状"永远表现成"这家店没有访客数据"。
        raise _BatchContractError(f"order_summary.summary.uv 必须是非负整数，收到 {value!r}")
    return value


def _declared_uv_scope(summary: Mapping[str, Any]) -> str | None:
    """上游用 `date` / `window.end` 明确声明的作用域（业务日期）；没声明 → `None`。

    保证：只返回"上游自己声明过"的日期；`fallback=""` 让非 strict 路径把"解析不出"
        折成空串，于是 `if declared:` 就是"上游声明了没有"。
    不保证：不保证声明的日期是对的——它只被用来和 `window_end` 比较相等，
        不参与任何"今天"的推算（推算是 `business_date(now)` 的事）。
    调用方坑：三个来源按**固定顺序**试（`date` → `business_date` → `window.end`），
        第一个能解析的就胜出；顺序变了会让"上游同时给了两个不一致的日期"时结果悄悄改变。
    """
    for key in _UV_SCOPE_DATE_KEYS:
        # 非 strict：作用域声明解析不出来不算脏数据，只是"没声明"（空串走 falsy 分支）。
        declared = _business_date_of(summary.get(key), fallback="")
        if declared:
            return declared
    window = summary.get("window")
    if isinstance(window, Mapping):
        # `window.end` 是第二优先的来源：`date` 缺省时它同样能说明汇总覆盖到哪天。
        declared = _business_date_of(window.get("end"), fallback="")
        if declared:
            return declared
    return None


def _day_uv(payload: Any, *, window_end: str, order_days: set[str]) -> int | None:
    """从上游 `summary` 取出**当日**访客数；无法确认作用域就 `None`（不摊派、不补零）。

    为什么需要这层确认（v1.3.2）：`collect` 一次查近 8 个业务日（§5.3 的历史均值需要），
    而上游 `summary` 是**整段查询窗**的汇总。把 8 天的 uv 当成今天的 uv，`cvr` 会被摊薄成
    约 1/8，"访客转化率掉了"这类规则就永远不响——**静默算错指标比缺数据更糟**。
    因此只在能确认是"当天"时才认：

    ① `summary.date` / `summary.window.end` 声明的作用域**等于** `window_end`；
    ② 本次返回的订单**全部落在** `window_end`（单日响应，`summary` 不可能是别的日子）。

    两者都不成立 → `None`：`compute_metrics` 随之给出 `uv=None` / `cvr=None`（§5.3）。
    """
    summary = payload.get("summary") if isinstance(payload, Mapping) else None
    if not isinstance(summary, Mapping):
        return None
    uv = _summary_uv(summary)
    if uv is None:
        return None
    declared = _declared_uv_scope(summary)
    if declared is not None:
        # ① 上游自己声明了作用域：只认"声明 == 窗口末"的那一种，其余一律 None。
        return uv if declared == window_end else None
    if order_days and order_days <= {window_end}:
        # ② 没声明，但本次返回的订单**全部**落在 window_end（子集判定）：
        #    单日响应 ⇒ summary 不可能是别的日子的汇总。
        return uv
    # ③ 两个依据都没有：宁可 None，也不按"总数 ÷ 天数"摊派（会把 cvr 摊薄 8 倍）。
    return None


class _BatchContractError(ValueError):
    """**整批**订单响应不满足契约（不是"某一行脏"）。

    与 :class:`_RowRejected` 的分工就是本仓"降级 vs 硬失败"的分界线：

    - 一行字段类型不对 → `_RowRejected`：整行跳过、留痕、其他行照算；
    - `data` 不是映射/列表、订单列表字段不是 list、一个已知列表字段都没有 →
      本异常：`collect` 记一条错误并把 run 标 `failed`（走 `finalize`，不是 500）。

    继承 `ValueError`：这与 `_money` / `_summary_uv` / `_as_list` 一侧的既有契约一致
    （所有"数据形状不对"都用 ValueError 家族），只是多了一个可精确捕获的子类。
    """


class _RowRejected(ValueError):
    """**一行**订单数据不合法（类型不对 / 负金额 / 件数不是整数）。

    只在 :func:`_read_order_row` 里抛，由 :func:`_aggregate_orders` 逐行捕获 → 跳过该行 +
    记一条 `collect.row_rejected` 审计。`reason` 是稳定分类（审计要按原因计数），
    `str(exc)` 保留原始异常原文（含字段路径），两者都要进样本。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        """`reason` 是**稳定分类**（审计按它计数），`str(exc)` 保留含字段路径的原文。

        保证：`reason` 一定是调用方写死的字面量之一（`no_known_fields` / `bad_create_time` /
            `bad_pay_amount` / `bad_units` / `bad_refund_amount` / `bad_sku_id`）。
        不保证：不保证 `message` 稳定——它会被拼进审计样本，只给人读，不要用它对账。
        调用方坑：关键字参数是必须的（`reason=`），位置传参会静默错位成 `message` 的一部分。
        """
        self.reason = reason
        super().__init__(message)


class _AllRowsRejected(_BatchContractError):
    """**看到了订单行，但一行都没被接受**（v1.5.1）：按整批不可用处理。

    这是 P1 那一档缺口的修复：逐行拒绝让"整批契约坏"（F1b）不再 500 了，但退化成了
    "每一行都坏 → 静默 0 单 0 元"。实测两行 `pay_amount` 都是字符串时，结果是
    `status=None / partial=False / gmv=0 / orders=0 / anomalies=[] / errors=[]`——零告警的
    "生意归零"，恰好是 F1b 要避免的那个状态，只是从"整批"降级成"逐行"。

    判据是 `rows_seen > 0 且 rows_accepted == 0`（也覆盖"全部元素不是对象"与"全部落在业务
    窗口外"——两者同样不能作为"今天没卖东西"的证据）。它继承 :class:`_BatchContractError`，
    因此 `collect` 用同一个 `except` 接住：记明确错误 + `status=failed` + **不 500**。
    """

    def __init__(
        self,
        message: str,
        *,
        rows_seen: int,
        rows_accepted: int,
        reasons: Mapping[str, int],
        rows_out_of_window: int,
        samples: Sequence[Mapping[str, Any]],
    ) -> None:
        # 这些字段让 `collect` 不必解析异常字符串就能把拒绝率写进 `collect.failed` 的审计。
        # 四个桶（seen / accepted / rejected / out_of_window）必须一起带出来：
        # 只给 seen/accepted 的话，账上永远差一个"窗口外"的桶（见 `_check_row_accounting`）。
        self.rows_seen = rows_seen
        self.rows_accepted = rows_accepted
        self.reasons = dict(reasons)
        self.rows_out_of_window = rows_out_of_window
        # 拷一份样本（不是引用）：异常会被 re-raise 上抛，引用会让调用方与本地列表共享同一个
        # 可变对象，后面任何 append 都会同时改变审计内容。
        self.row_rejected = [dict(item) for item in samples]
        super().__init__(message)


#: 上游订单列表字段名（与 `_as_list` 的调用点保持一致；这里用常量是为了让"哪个字段算契约"
#: 只有一处定义，避免校验和取值各自挑一份名单而悄悄分叉）。
#: 顺序即优先级；`_require_orders_container` 与 `_as_list_raw` 共用同一份名单。
_ORDER_LIST_KEYS: tuple[str, ...] = ("shop_order_list", "orders", "order_list")


def _require_orders_container(payload: Any) -> None:
    """整批契约校验：`data` 必须是订单列表（裸 list，或带列表字段的映射），否则抛错。

    为什么必须校验（v1.5.1 的根因）：`_as_list` 对"形状不认识"的输入返回 `[]`，于是
    `data=None` / `{"oops": 1}` 会被静默聚合成"今天 0 单 0 元"。那不是"缺数据"，而是
    **把一次上游改结构伪装成生意归零**——所有基于 "gmv 掉了" 的规则都会静默失效，
    而没有任何一处会红（§5.3 的"缺数据不许补零"同一类事故）。

    空列表 `[]` 是**合法**的（今天确实没有订单）：判据是"形状对不对"，不是"有没有内容"。
    `字段存在但为 None` 也按空列表处理——上游对空结果返回 `null` 是常见写法，为此把整轮
    判死属于误伤；`字段存在但不是 list`（例如变成了 `"oops"`）才是真形状错。
    """
    if isinstance(payload, list):
        # 裸列表是合法的订单响应（某些接口直接给数组）。
        return
    if not isinstance(payload, Mapping):
        # None / 字符串 / 数字：不是"今天没单"，是形状不认识 → 整批不可用。
        raise _BatchContractError(
            f"订单响应 data 必须是映射或列表，收到 {type(payload).__name__}"
        )
    for name in _ORDER_LIST_KEYS:
        if name not in payload:
            continue
        value = payload[name]
        if value is None or isinstance(value, list):
            # `null` 按空列表处理（上游对空结果返回 null 是常见写法，为此判死属于误伤）。
            return
        raise _BatchContractError(
            f"data.{name} 必须是 list（空结果请给 [] 或 null），收到 {type(value).__name__}"
        )
    # 一个已知列表字段都没有：与行级 `no_known_fields` 同源的判断，只是发生在批层。
    raise _BatchContractError(
        f"缺少订单列表字段（期望 {'/'.join(_ORDER_LIST_KEYS)} 之一），"
        f"实际键：{sorted(str(key) for key in payload)}"
    )


def _order_id_of(item: Mapping[str, Any]) -> str | None:
    """订单行的 `order_id`（只用于审计定位）：缺失/空值 → `None`，从不抛异常。

    `order_id` 脏（例如是 dict）也不能因此拒绝整行：它是一个**辅助身份**字段，不参与任何
    指标计算；为了它丢掉一行真实成交额反而是新的静默少算。
    """
    raw = item.get("order_id")
    if raw is None or isinstance(raw, bool):
        # bool 也拒：`order_id=True` 会被 str() 成 "True"，那是噪声不是身份。
        return None
    text = str(raw).strip()
    return text or None


def _best_effort_units(item: Mapping[str, Any], *, where: str) -> int | None:
    """0 元单的件数——**只喂审计样本**（它不进 `by_sku`），读不到就 `None`。

    为什么要单独一条路：`_order_units` 对类型错会抛 `ValueError`，而 0 元单本来就不参与
    成本口径，若因为"样本想多带一个字段"把整行拒掉，等于让旁路信息反过来改变业务结论。


    ## 这里**故意**保留 `TypeError`（不是漏改）

    本仓的硬边界是"我们自己的代码 bug 不许被降级成数据脏"（见 :func:`_read_order_row`
    的 `except ValueError` 与 `_aggregate_orders` 里只接 `ValueError` 的那一段）。那条边界
    针对的是**业务结论**：一行的 `pay_amount`/件数解析出错若被写成"脏行"，指标会静默少算。

    这个函数不产生任何业务结论——它的返回值只进 `collect.zero_revenue_paid` 的审计样本
    （`units` 字段，让人事后估算"漏了多少成本"）。所以这里的取舍相反：

    - 收窄成只接 `ValueError` → 一个 `TypeError`（只可能来自我们自己）会从"想给样本多带
      一个字段"这条旁路逃出去，**整轮采集直接失败**，当天所有指标一起没有；
    - 保留 `TypeError` → 最坏情况是"这条样本的 `units` 是 `null`"，指标一个数都不变。

    即：**旁路样本的完整性不许反过来决定业务结论的成败**。这是唯一一处故意保留
    `TypeError` 的地方；`_read_order_row` / `_aggregate_orders` / `_business_date_of`
    里的 `except` 都已收窄到数据侧真正会抛的异常（`ValueError` / `OSError` /
    `OverflowError`），两处口径不同是有意的，不是忘了改。
    """
    try:
        return _order_units(item, where=where)
    except (ValueError, TypeError):
        # 两种都接（与 `_read_order_row` 只接 ValueError 相反）：样本读不到就给 None，
        # 指标一个数都不变；反过来让异常逃出去会让整轮采集失败。
        return None


#: 一行订单的**业务日期**候选字段（按此顺序逐字段尝试；抖店不同接口给其中一个，也可能两个都给）
#: 顺序即优先级：`create_time` 是"下单时刻"，`pay_time` 是"支付时刻"，
#: 前者更接近业务语义（跨零点支付归属下单日），所以它先试。
_ORDER_DATE_KEYS: tuple[str, ...] = ("create_time", "pay_time")

#: :func:`_read_order_row` **实际读取**的订单字段名（行级契约的"已知字段"名单）。
#:
#: 名单必须与 `_read_order_row` 的读取点**同源**，否则"一行什么都没有"的判据会随时间腐烂：
#: 加了一个读取字段却忘了加进名单，就会把"只带这个字段的真实订单"判成 `no_known_fields`。
#: 因此这里直接展开 `_ORDER_DATE_KEYS` / `_ORDER_UNIT_KEYS`，而不是再抄一份字面量。
_KNOWN_ORDER_ROW_KEYS: frozenset[str] = frozenset(
    {
        "order_id",  # `_order_id_of`
        "sku_id",
        "refund_status",  # `_is_refunded`
        "pay_amount_cents",
        "pay_amount",  # `_first_int`
        "refund_amount_cents",
        "refund_amount",  # `_first_int`
        *_ORDER_DATE_KEYS,  # `create_time` / `pay_time`
        *_ORDER_UNIT_KEYS,  # `product_count` / `goods_count` / `quantity`
    }
)


def _row_business_date(
    item: Mapping[str, Any],
    *,
    where: str,
    window_end: str,
    order_id: str | None = None,
    fallbacks: list[dict[str, Any]] | None = None,
) -> str:
    """一行的业务日期：`create_time` → `pay_time` **逐字段尝试**，全都不是日期才整行拒绝。

    ## 三个必须分开的情况

    1. **键不存在 / 值是 `None`**：真·缺日期 → 回退 `window_end`，不拒绝整行
       （"上游这条链路没给我们日期"是缺数据，不是脏数据）；
    2. **键存在但值是 falsy**（`False` / `0` / `""`）：**不是**"没给日期"，是"给了一个不可能是
       日期的值"。旧实现 `item.get("create_time") or item.get("pay_time")` 在 strict 判定
       **之前**就把 falsy 折成了 `None`，而 strict 只对"给了垃圾"生效、对 `None` 仍回退当日
       → 实测 `create_time=False` / `0` / `""` 都变成"今天成交 + GMV 全额计入"，即上一轮声称
       修掉的那个现象。falsy 不该走回退这条路，也不该当成"缺键"，按**该字段不可用**处理；
    3. **键存在、值不是 falsy、但解析不出来**（`"0000-00-00 00:00:00"` / 越界时间戳 / 乱码）：
       这个字段不可用，**试下一个**字段；两个都不可用才 `_RowRejected(bad_create_time)`。

    ## 为什么第 3 条要逐字段尝试，而不是"第一个字段坏就整行拒"

    实测 `create_time="0000-00-00 00:00:00"` 配一个合法的 `pay_time` 时，旧实现整行被丢：
    GMV 少算，可这一行其实有可用日期。日期在这两个字段上是**同义**的（都表示这单哪天成交），
    一个不可用不等于另一个也不可用；全都不可用才真的"猜不出这单是哪天的"。

    ## 靠后续字段才成功时**必须留痕**（v1.5.2）

    "第一个候选字段不可用、靠 `pay_time` 才解析成功"的行是**被接受**的（不是脏行），但它
    说明上游的 `create_time` 正在系统性坏掉。旧实现只在**全都不可用**时留痕（整行拒绝），
    于是 `create_time` 全写成 `False` 而 `pay_time` 始终合法的场景**永远查不出来**——同一
    文件里 `_order_units` 的规矩是"降级必须可见"，日期这条降级却没有任何出口。

    所以这里把跳过现场追加进 `fallbacks`（调用方 `_aggregate_orders` 只在**行最终被接受**后
    合并，避免给一个后面又被拒/落在窗口外的行留下"日期降级"的假痕迹），由 `collect` 写成
    一条 `collect.date_fallback`（warn，**不计入 `row_rejected`**：行是被接受的）。
    样本只带 `order_id` / `where` / `reason` / `used_key` / `skipped_keys`，不重复异常原文
    （那一行照样在 `collect.done` 里被计入指标，排障要的是"哪个字段坏了"）。

    只有"**字段存在但给不出日期**"才算降级：`create_time` 键根本不存在、直接只给 `pay_time`
    是上游不同接口的正常形态（见 `_ORDER_DATE_KEYS` 的注释），不告警——否则每次正常采集
    都会 warn，把真正的系统性坏字段淹掉。

    失败时抛 :class:`_RowRejected`（`reason="bad_create_time"`），`error` 带上每个尝试过的字段
    与原因——"上游到底把哪个字段改坏了"不必再猜。
    """
    skipped: list[tuple[str, str, str]] = []
    for key in _ORDER_DATE_KEYS:
        if key not in item or item[key] is None:
            # 真·缺日期（键不存在或 None）：不是脏数据，也不记降级样本。
            continue
        value = item[key]
        if not value:
            # falsy ≠ 缺日期：`0` 是 1970-01-01、`""` 是空文本、`False` 是 bool——没有一个能
            # 表示"这单哪天成交"。折成 None 就会回退成今天，凭空造出一笔当日成交。
            skipped.append((key, "falsy_value", f"{where}.{key}={value!r}（falsy 值，不是日期）"))
            continue
        try:
            day = _business_date_of(value, fallback=window_end, strict=True)
        except _BadBusinessDate as exc:
            # 这个字段不可用 → 记下现场，继续试下一个候选字段（`pay_time`）。
            skipped.append((key, "unparseable", f"{where}.{key}：{exc}"))
            continue
        if skipped and fallbacks is not None:
            # 行最终是否被接受由调用方决定（可能后面金额/件数坏掉，也可能落在窗口外）：
            # 这里只把现场暂存到**这一行自己的** staging 列表，不直接算进全量审计。
            fallbacks.append(
                {
                    "order_id": order_id,
                    "where": where,
                    # 只取第一条跳过原因当分类：`reason` 是给人分组的稳定值，
                    # 完整的逐字段明细在 `skipped_keys` 里，两者各司其职。
                    "reason": skipped[0][1],
                    "used_key": key,
                    "skipped_keys": [skipped_key for skipped_key, _, _ in skipped],
                }
            )
        return day
    if skipped:
        # 有候选字段、但一个都给不出日期：这才是脏数据，整行拒绝（不回退今天）。
        raise _RowRejected(
            "业务日期不合法（"
            + "；".join(detail for _, _, detail in skipped)
            + f"）：{'/'.join(_ORDER_DATE_KEYS)} 都不是可用的日期，整行拒绝（不回退今天）",
            reason="bad_create_time",
        )
    # 两个候选字段都不存在：真的没给日期，回退窗口末（有界、可解释的降级）。
    return window_end


def _read_order_row(
    item: Mapping[str, Any],
    *,
    where: str,
    window_start: str,
    window_end: str,
    degraded: list[dict[str, Any]],
    date_fallbacks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """把一行订单**校验并解析**成一个待提交的值；脏行抛 :class:`_RowRejected`。

    ## 为什么"先解析成值、再提交到桶"，而不是就地累加

    旧实现在同一遍里既解析又累加：`order_count` 先 +1，随后 `product_count: "3"` 才炸。
    即使调用方接住异常，桶里也已经留下**半行数据**（订单数 +1、金额没加），而且异常当时
    直接逃到 API 层变成 500。行级拒绝必须是**原子的**：要么整行进指标，要么整行不进。

    ## 校验顺序（顺序本身是契约）

    0. **"这一行到底是不是一行订单"**：一个已知订单字段都没有（`{}` / `{"foo": 1}`）→
       `_RowRejected(reason="no_known_fields")`。必须在**日期之前**判：这种行没有
       `create_time`/`pay_time`，走到日期那一步会回退成"今天"，于是被当成一笔 0 元当日订单
       计入 `order_count`——实测 `{"shop_order_list": [{}, {}, {}]}` 得到
       `order_count=3 / gmv=0 / rows_rejected=0 / partial=False` 且**零告警**，正是批层
       "一个已知字段都没有"判据（`_require_orders_container`）在行层的缺失。
    1. 业务日期 → 窗口外的行直接 `None`（既不算错、也不进指标；上游会多给窗口外的单）。
       窗口判定**先于**金额解析：窗口外的行本来就不参与任何指标，把它算成"脏行"只会让
       审计里的拒绝计数虚高。日期的字段级细节（falsy / 逐字段尝试 / 全都不可用才拒绝）
       见 :func:`_row_business_date`；"靠后续字段才解析成功"的降级现场写进
       `date_fallbacks`（staging，**只有这张行最终被接受时**才由调用方合并）。
       注意顺序：`date_fallbacks` 在窗口判定**之前**产生，而窗口外的行 `return None`——
       调用方按 `row is not None` 决定是否合并，所以窗口外行不会留下日期降级痕迹。
    2. 支付金额：必须是整数分、且**非负**（负数不是"退款/冲正"的表达方式，退款在
       `refund_amount` 里；负金额只会让 GMV 凭空变小）。
    3. 件数（仅对"已支付 + 有 sku + 成交额 > 0"的行）：缺失/非正按 1 件（降级），
       类型错整行拒绝（猜不出含义，不许按 1 件猜）。
    4. 退款金额：整数分、非负；再结合 `refund_status` 判定是否退款。

    返回的 dict 是**纯值**，累加只发生在 :func:`_aggregate_orders` 的提交段：
    `{order_id, day, paid, revenue, sku_id, units, zero_revenue, refunded, refund_amount,
    best_effort_units}`。

    ## 异常口径

    本函数只把**数据形状**错转成 :class:`_RowRejected`（它继承 `ValueError`），并且只接
    `ValueError`：`TypeError` 之类只可能来自我们自己代码的 bug，必须照常传播出去，不许被
    写成"这一行脏、跳过"。见 `_aggregate_orders` 里那段 `except ValueError` 的注释。
    """
    if not _KNOWN_ORDER_ROW_KEYS.intersection(item):
        # "一个已知字段都没有" ＝ 这不是一行订单，而不是"一笔 0 元订单"。行层的判据必须与批层
        # 同源（`_require_orders_container` 判的是"一个已知**列表**字段都没有"），否则
        # `[{}, {}, {}]` 会被静默聚合成"3 单 0 元"——零告警的生意归零。
        # 顺序上必须在日期解析**之前**：这种行没有 create_time/pay_time，走到日期那一步会
        # 回退成"今天"，于是被当成一笔 0 元当日订单计入 order_count。
        known = "、".join(sorted(_KNOWN_ORDER_ROW_KEYS))
        raise _RowRejected(
            f"{where} 没有任何一个已知订单字段（实际键：{sorted(str(key) for key in item)}；"
            f"已知字段：{known}）：这不是一行订单数据，不能当成一笔 0 元订单计入 order_count",
            reason="no_known_fields",
        )
    order_id = _order_id_of(item)
    day = _row_business_date(
        item, where=where, window_end=window_end, order_id=order_id, fallbacks=date_fallbacks
    )
    if not (window_start <= day <= window_end):
        # 窗口外：既不算脏、也不进指标（`None` 让调用方只做 `rows_out_of_window += 1`）。
        # 窗口判定**先于**金额解析：窗口外的行本来不参与任何指标，把它算成脏行会让
        # 审计里的拒绝计数虚高。
        return None

    try:
        paid_amount = _first_int(item, ("pay_amount_cents", "pay_amount"), where=where)
    except ValueError as exc:
        # 形状错 → 整行拒绝（猜不出含义，不许按 0 元处理）。
        raise _RowRejected(f"支付金额不合法：{exc}", reason="bad_pay_amount") from exc
    if paid_amount is not None and paid_amount < 0:
        # 负数不是"退款/冲正"的表达方式（退款在 `refund_amount` 里），只会让 GMV 凭空变小。
        raise _RowRejected(
            f"{where}.pay_amount 不允许为负，收到 {paid_amount!r}", reason="bad_pay_amount"
        )

    # "已支付"的判据是**数据**而不是状态词表：`pay_amount > 0` 或存在 `pay_time`。
    # 依赖状态字符串会让上游改一次枚举就静默把 GMV 算成 0。
    paid = bool(paid_amount and paid_amount > 0) or item.get("pay_time") is not None
    row: dict[str, Any] = {
        "order_id": order_id,
        "day": day,
        "paid": paid,
        "revenue": int(paid_amount or 0),
        "sku_id": None,
        "units": 0,
        "zero_revenue": False,
        "refunded": False,
        "refund_amount": 0,
        "best_effort_units": None,
    }
    if not paid:
        # 未支付行不参与金额/件数/退款三件事：不去解析它们的 `refund_amount`（旧行为一致）。
        return row

    sku_id = item.get("sku_id")
    if sku_id is not None:
        # 只接受 str / int（v1.5.1）：`str({"a": 1})` 会得到 "{'a': 1}" 这种字符串并被当成
        # 一个真实 SKU 名写进 `by_sku`——成本表永远匹配不上它，`cost_coverage_ratio` 被
        # 拉低而人不明所以。dict/list/bool/float 一律整行拒绝（猜不出它想指哪个 SKU）。
        if isinstance(sku_id, bool) or not isinstance(sku_id, (str, int)):
            raise _RowRejected(
                f"{where}.sku_id 必须是字符串或整数，收到 {type(sku_id).__name__}",
                reason="bad_sku_id",
            )
        sku_text = str(sku_id).strip()
        if sku_text:
            row["sku_id"] = sku_text
            if row["revenue"] <= 0:
                # 0 元单不进成本口径（成本会被记、成交额是 0 → 毛利率被压负 → 误报亏本）；
                # 件数只用来给审计样本估算"漏了多少成本"。
                row["zero_revenue"] = True
                row["best_effort_units"] = _best_effort_units(item, where=where)
            else:
                try:
                    row["units"] = _order_units(
                        item, where=where, order_id=order_id, degraded=degraded
                    )
                except ValueError as exc:
                    raise _RowRejected(f"件数不合法：{exc}", reason="bad_units") from exc

    try:
        refund_amount = _first_int(item, ("refund_amount_cents", "refund_amount"), where=where)
    except ValueError as exc:
        raise _RowRejected(f"退款金额不合法：{exc}", reason="bad_refund_amount") from exc
    if refund_amount is not None and refund_amount < 0:
        raise _RowRejected(
            f"{where}.refund_amount 不允许为负，收到 {refund_amount!r}",
            reason="bad_refund_amount",
        )
    row["refund_amount"] = int(refund_amount or 0)
    # 退款判定放在最后：`_is_refunded` 需要**已校验**的退款额（它不再自己解析一遍）。
    row["refunded"] = _is_refunded(item, refund_amount=row["refund_amount"])
    return row


def _check_row_accounting(
    *,
    rows_seen: int,
    rows_accepted: int,
    rows_rejected: int,
    rows_out_of_window: int,
    where: str,
) -> None:
    """行级账目恒等式（v1.5.2）：`rows_seen == rows_accepted + rows_rejected + rows_out_of_window`。

    为什么必须显式断言：`rows_seen - rows_accepted` 曾经被读成"被拒行数"，实际它同时包含
    **窗口外**的行（上游多给的、时区分页边界产生的），而 `rows_out_of_window` 不在任何事件的
    `data` 里 → 账上差一个查不到的桶（实测 `100 - 93 = 7`，其中只有 3 行是脏行，另 4 行在
    窗口外）。现在四者都进 `collect.done` / `collect.failed` / `collect.row_rejected`，这条
    恒等式是它们能对账的唯一依据。

    用 `raise AssertionError` 而不是 `assert`：`python -O` 会把语句级 `assert` 整条删掉，
    而那正是"账永远对不上、还没有任何入口发现"的场景。异常消息带上四个数，出了问题能直接
    看出是哪一桶被漏算。
    """
    total = rows_accepted + rows_rejected + rows_out_of_window
    if rows_seen != total:
        # 用 `raise` 而不是 `assert`：`python -O` 会把语句级 assert 整条删掉，
        # 而那正是"账永远对不上、还没有任何入口发现"的场景。
        raise AssertionError(
            f"{where}：行级账目不守恒——rows_seen={rows_seen}，"
            f"但 rows_accepted({rows_accepted}) + rows_rejected({rows_rejected}) + "
            f"rows_out_of_window({rows_out_of_window}) = {total}。"
            "上游给的每一行必须落进这三个桶之一（被接受 / 被拒 / 窗口外），"
            "否则 rows_seen - rows_accepted 就会包含一个查不到的桶。"
        )


def _aggregate_orders(
    payload: Any,
    *,
    window_start: str,
    window_end: str,
    cost_table: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """订单列表 → 按业务日聚合（`gmv_cents` / `order_count` / `paid_order_count` /
    `refund_order_count` / `refund_amount_cents` / `uv`），并保留 `by_date` 供历史指标使用。

    "已支付"的判据是**数据**而不是状态词表：`pay_amount > 0` 或存在 `pay_time`。
    依赖状态字符串会让上游改一次枚举就静默把 GMV 算成 0。

    `uv` 不在订单明细里，只可能来自上游 `summary`；由 :func:`_day_uv` 判定它是否可用于
    **当天**（拿不到就 `None`，绝不按"窗口总数 / 天数"摊派，也不补零）。

    `by_sku`（v1.5.0）是成本口径的唯一数据源：`sku_id -> {"gmv_cents", "units"}`，只统计
    **已支付**的订单行。没有这一步，"某件货在亏本卖"就无法计算——成本表只知道单价成本，
    不知道这件货今天卖了多少钱、卖了几件（成本额 = 单价成本 × 件数）。
    明细里没有 `sku_id` 的订单行**不放进 `by_sku`**：它无法归属到任何 SKU，于是只进全店
    GMV 的不覆盖部分（`cost_coverage_ratio` 的分子不收它）——这正是"覆盖率"要表达的事，
    不许为了凑满覆盖率把它摊到别的 SKU 上。

    退款单仍计入成交额与件数（与 `gmv_cents` 的口径一致：GMV 含已退款支付额）。
    退款把成本冲回来（退货入库）是另一个口径，需要单独的净额指标，不在本函数里偷偷做掉。

    ## 两条**降级**事实（v1.5.1）：只留痕、不抛异常，由 `collect` 记 warn 审计

    `units_degraded`：件数缺失/非正按 1 件（见 :func:`_order_units` 的算例：方向偏乐观、
    会**漏报**亏本）。采集层容错是为了"一条脏数据不打死当天所有指标"。

    `zero_revenue_paid`：**有 `pay_time` 但成交额为 0** 的订单行。

    - `paid` 的判据是"`pay_amount > 0` 或存在 `pay_time`"（不看状态词表：上游改一次枚举就会
      静默把 GMV 算成 0）。但"有 `pay_time`"只说明它被支付过，**不说明 `pay_amount` 拿到了**：
      上游漏返回金额时 `revenue` 就是 0；也可能是**真实的 0 元赠品单**——两者从这里区分不了。
    - 若这一行仍然进 `by_sku`（`units += 1`），成本口径会记一条**真实的成本**、却记 0 元成交：
      `covered_revenue` 不变、`covered_cost` 变大 → 毛利率被莫名压负。算例：原本
      `covered_revenue=120000`、`covered_cost=60000`（毛利率 **+0.5**）；混进一行
      `units=1, revenue=0`、单价成本 80000 分的"0 元单"后 `covered_cost=140000`，
      毛利率变成 (120000-140000)/120000 = **-0.1667**，足以**误触发** `loss_making`
      （critical，只告警不给动作，但它会进日志/告警台，让人去追一件根本没亏的货）。
    - 所以 `revenue <= 0` 的已支付行**不进成本口径**（不进 `by_sku`），但要留痕：它仍计入
      `order_count` / `paid_order_count`（GMV 口径不变，那是另一件事），只是无法参与"金额/件数"
      这条比率。这也是**偏乐观**的方向（少算成本 → 少报亏本）——与本仓"宁可漏报，不可误报"
      的分级一致，且这条漏报是可查的（log 里有 `collect.zero_revenue_paid`）。
    - 样本带 `units` / `unit_cost_cents`（v1.5.1，`cost_table` 给了才有）：只有件数没有单价时
      "漏了多少钱"仍然算不出来，人只能看到"有 N 行没进成本口径"却无法排序该先修哪一单。
    - `revenue <= 0` 也可能来自**负金额**——那种行不进这里，而是整行拒绝（见下面第 3 条）：

    `date_fallback`（v1.5.2）：行被**接受**，但第一个候选日期字段（`create_time`）存在却给不出
    日期，靠 `pay_time` 才解析成功。它不是脏行（行进了指标，所以**不**进 `row_rejected`），
    但"上游把 `create_time` 写坏了"这件事必须可查——否则下一次所有行都退回 `pay_time`，
    业务日期口径已经悄悄换了而没有人知道。现场只合并**被接受行**的暂存（窗口外/随后被拒的
    行不留痕迹），由 `collect` 记一条 `collect.date_fallback`（warn）并置 `source.partial=True`。

    ## 第三条：**行级脏数据整行拒绝**（v1.5.1；这是"脏数据不许打死整轮"的根治）

    分界线（也是本仓对"降级 vs 硬失败"的判据）：

    - **一行的数据脏**（字段类型不对 / 负数金额 / 件数不是整数）→ :func:`_read_order_row` 抛
      :class:`_RowRejected`，本函数**整行跳过**并写进 `row_rejected`，其他行照常算。
      这行**不进 GMV / order_count / by_sku 中的任何一个**（原子提交：先校验成值、再累加），
      因此不存在"计数 +1 了但金额没加"的半行状态。
    - **整批的契约坏了**（`data` 不是映射/列表、订单列表字段不是 list、一个已知字段都没有、
      `summary.uv` 形状不对）→ :func:`_require_orders_container` / :func:`_summary_uv` 抛
      :class:`_BatchContractError`，由 `collect` 记错并把 run 标 `failed`。绝不降级成
      "今天 0 单 0 元"：那会把一次上游改结构伪装成"生意归零"，所有规则都会因为指标为 0 而静默失效。
    - **看到了行、但一行都没被接受**（`rows_seen > 0 and rows_accepted == 0`，v1.5.1）→ 同样抛
      :class:`_AllRowsRejected`（`_BatchContractError` 的子类），走**同一条** failed 路径。
      逐行拒绝若不做这道阈值，"每一行都坏"就会退化成静默的 0 单 0 元——只是把整批契约坏
      降级成逐行契约坏，F1b 要避免的状态一点没变。

    ## 拒绝率必须可观测（v1.5.1）

    `rows_seen`（订单列表里的**全部元素**，含非对象元素与窗口外的行）与 `rows_accepted`
    （真正进入任何指标的行）随聚合结果一起返回，`collect.done` 落进审计：只看 `gmv=0` 分不清
    "今天没卖东西"和"上游给的行全被拒了"，而这两个状态的处置完全不同。

    **行级账目必须守恒（v1.5.2）**：
    `rows_seen == rows_accepted + rows_rejected + rows_out_of_window`。
    `rows_out_of_window` 是业务日期落在窗口外、被 :func:`_read_order_row` 返回 `None` 的行
    （不是脏数据，也不进指标）。以前它只在本函数内部计数、**不进任何事件的 `data`**，于是
    `rows_seen - rows_accepted` 会被读成"被拒行数"，而它其实 = 被拒 + 窗口外——账上差一个
    查不到的桶。现在四者一起返回、由 :func:`_check_row_accounting` 断言，`collect` 再把
    `rows_out_of_window` 写进三个出口（`collect.done` / `collect.failed` /
    `collect.row_rejected`）。

    `row_rejected` 的样本也是 `{order_id, where, reason, error}`：`reason` 是稳定分类
    （`no_known_fields` / `bad_create_time` / `bad_pay_amount` / `bad_units` /
    `bad_refund_amount` / `bad_sku_id` / `not_an_object` / `unclassified`），`error` 保留原始
    异常原文（含字段路径），这样"上游改了哪个字段"不用猜。"不是对象"的元素（`["x", 1, None]`）
    也算一行：它不是"没给数据"，是"给了但没解析出来"，不能像 :func:`_as_list` 那样被静默丢掉。
    `no_known_fields` 是同一件事在**对象内部**的形态（`{}` / `{"foo": 1}`）：行是对象，但一个
    订单字段都没有，同样不能当成"一笔 0 元订单"（见 :func:`_read_order_row` 第 0 条）。

    `cost_table` 只用于**审计样本**里的 `unit_cost_cents`（让事后能算出 0 元单漏了多少成本），
    不参与本函数的任何累计口径——所以它缺省可以是 `None`。
    """
    _require_orders_container(payload)
    # 取**原始元素**而不是 `_as_list`（后者会丢掉非对象元素）：那些元素也是"看到了但没解析
    # 出来"的行，必须计入拒绝率（v1.5.1）。
    raw_rows = _as_list_raw(payload, _ORDER_LIST_KEYS)
    # 按业务日期分桶：`by_date` 既是当日指标的来源，也是近 7 日历史的来源（一次采集算完 8 天）。
    buckets: dict[str, dict[str, Any]] = {}
    #: 降级留痕（调用方负责变成审计条目；本函数是纯聚合，不碰日志/DB）
    units_degraded: list[dict[str, Any]] = []
    zero_revenue_paid: list[dict[str, Any]] = []
    #: 被整行拒绝的行（脏数据）——这些行**没有**进入任何指标，审计里必须说清楚
    #: 每条样本是 `{order_id, where, reason, error}`，`reason` 是稳定分类。
    row_rejected: list[dict[str, Any]] = []
    #: 被**接受**、但第一个候选日期字段不可用（靠 `pay_time` 才解析成功）的行（v1.5.2）。
    #: 它们不是脏行（行进了指标），所以单独一条 `collect.date_fallback`，不进 `row_rejected`。
    date_fallbacks: list[dict[str, Any]] = []
    #: 拒绝率的分母/分子：分母是列表里的全部元素（含非对象元素与窗口外的行），
    #: 分子是真正进入任何指标的行。两者一起进 `collect.done`，否则"gmv=0"没法归因。
    rows_seen = len(raw_rows)
    rows_accepted = 0
    #: 解析成功、但业务日期不在窗口内的行（不是脏数据，也不是"今天没卖东西"的证据）
    rows_out_of_window = 0

    def _bucket(day: str) -> dict[str, Any]:
        # `setdefault` 而不是"先 get 再 add"：桶的字段清单只在这里定义一次，
        # 新增一个指标列时不会出现"某条分支漏初始化"的半行字典。
        # 注意 `uv` 初始是 None（不是 0）："没有访客数据"与"零访客"是两件事。
        return buckets.setdefault(
            day,
            {
                "gmv_cents": 0,
                "order_count": 0,
                "paid_order_count": 0,
                "refund_order_count": 0,
                "refund_amount_cents": 0,
                "uv": None,
                "by_sku": {},
            },
        )

    for index, item in enumerate(raw_rows):
        # 页内下标只用来**定位**（`order[3]`），不参与任何指标：跨 run 重试时它会变，
        # 所以真正的定位键是 `order_id`（见下面各样本）。
        where = f"order[{index}]"
        if not isinstance(item, Mapping):
            # 元素形状错（字符串/数字/null）：也算"一行被拒"，不许像旧 `_as_list` 那样静默丢弃。
            row_rejected.append(
                {
                    "order_id": None,
                    "where": where,
                    "reason": "not_an_object",
                    "error": f"订单行必须是对象/映射，收到 {type(item).__name__}",
                }
            )
            continue
        order_id = _order_id_of(item)
        #: 这一行自己的降级留痕。**行被接受才合并到 `units_degraded`**：先合并再校验的话，
        #: 一个后面又被拒绝的行会在审计里留下"缺失件数"的痕迹，可它根本没进任何指标。
        #: 同理，`staged_date_fallbacks` 也只有这一行被接受时才合并。
        staged_degraded: list[dict[str, Any]] = []
        #: 同理，日期降级（靠 `pay_time` 才解析成功）也只在这一行**被接受**时合并：
        #: 行在窗口外或随后金额/件数坏掉时，它没进任何指标，"create_time 坏了"这件事
        #: 对指标没有影响，不该在审计里留一条会误导归因的痕迹。
        #: 这也是"暂存 + 提交点"这套写法的全部目的：留痕与"是否进指标"必须同时发生。
        staged_date_fallbacks: list[dict[str, Any]] = []
        try:
            row = _read_order_row(
                item,
                where=where,
                window_start=window_start,
                window_end=window_end,
                degraded=staged_degraded,
                date_fallbacks=staged_date_fallbacks,
            )
        except _RowRejected as exc:
            # 行级拒绝（v1.5.1）：这一行整行不进任何指标，但**必须留痕**，否则就是静默少算。
            # `reason` 是稳定分类（`bad_pay_amount` 等），`error` 保留含字段路径的原文。
            row_rejected.append(
                {"order_id": order_id, "where": where, "reason": exc.reason, "error": str(exc)}
            )
            continue
        except ValueError as exc:
            # 没有显式分级的**数据**解析错也走同一条降级（带异常类型名，避免"看不出是什么错"）。
            # 只接 ValueError（v1.5.1）：本文件数据侧（`_money` / `_order_units` / `_is_refunded`）
            # 只抛 ValueError；接住 TypeError 等于把我们自己代码的 bug 降级成"数据脏"。
            # `reason="unclassified"` 是"兜住了但没分类"的显式标记，不是"没问题"。
            row_rejected.append(
                {
                    "order_id": order_id,
                    "where": where,
                    "reason": "unclassified",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if row is None:
            # 业务窗口外的行（上游时区/分页边界多给的）：既不是脏数据、也不进指标。
            rows_out_of_window += 1
            continue
        # **原子提交点**：到这里这一行才被算作"被接受"，staged 留痕也才合并进全量审计。
        # 在那之前任何一步抛错都会走上面的 `continue`，桶里不会留下半行数据。
        rows_accepted += 1
        units_degraded.extend(staged_degraded)
        date_fallbacks.extend(staged_date_fallbacks)
        bucket = _bucket(row["day"])
        bucket["order_count"] += 1
        if not row["paid"]:
            # 未支付行只进 `order_count`：GMV / 件数 / 退款三件事都与"付没付"有关。
            continue
        bucket["paid_order_count"] += 1
        # GMV 口径含已退款支付额（与 §5.3 一致）：退款另立一个 `refund_*` 指标，
        # 不在这里做净额抵消——抵消需要一个"退货入库"的会计口径，本服务没有。
        bucket["gmv_cents"] += row["revenue"]
        if row["sku_id"] is not None:
            if row["zero_revenue"]:
                # 有支付时间却没有成交额：**不进成本口径**（见 docstring 的 -0.1667 算例）。
                # 上面已经把它计入 paid_order_count / gmv_cents（GMV 口径是另一件事）。
                # 样本带件数与单价成本：只知道"有 N 行没进成本口径"是算不出漏了多少钱的。
                zero_revenue_paid.append(
                    {
                        "order_id": row["order_id"],
                        "where": where,
                        "sku_id": row["sku_id"],
                        "units": row["best_effort_units"],
                        "unit_cost_cents": (
                            None if cost_table is None else cost_table.get(row["sku_id"])
                        ),
                    }
                )
            else:
                # 件数在**解析阶段**已经读好（见 `_read_order_row`）：件数缺失/类型错的边界在
                # 采集侧，到了 compute_metrics 就只有"金额/件数"这种已经归集好的干净数据。
                entry = bucket["by_sku"].setdefault(
                    row["sku_id"], {"gmv_cents": 0, "units": 0}
                )
                entry["gmv_cents"] += row["revenue"]
                entry["units"] += row["units"]
        if row["refunded"]:
            bucket["refund_order_count"] += 1
            bucket["refund_amount_cents"] += row["refund_amount"]

    # v1.5.1：看到了行，但**一行都没被接受** → 与整批契约坏走同一条路径（failed，不 500）。
    # 不做这道阈值，"每一行都坏"就会退化成静默的 0 单 0 元——那是 F1b 要避免的状态，
    # 只是从"整批契约坏"降级成了"逐行契约坏"。全部落在窗口外同样不算"今天没卖东西"。
    # 判据只看两个数（seen/accepted）：它不关心"为什么没接受"，原因在 `reject_reasons` 里。
    if rows_seen > 0 and rows_accepted == 0:
        reject_reasons: dict[str, int] = {}
        for sample in row_rejected:
            # 与 `_degradation_summary` 同一个口径：按 `reason` 计数，缺失归 unknown。
            reason = str(sample.get("reason") or "unknown")
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
        _check_row_accounting(
            rows_seen=rows_seen,
            rows_accepted=rows_accepted,
            rows_rejected=len(row_rejected),
            rows_out_of_window=rows_out_of_window,
            where="_aggregate_orders 全行被拒分支",
        )
        detail = (
            f"全部 {rows_seen} 行都在业务窗口之外，没有任何一行进入指标"
            if rows_out_of_window == rows_seen
            else f"rows_seen={rows_seen}，rows_accepted=0，按原因：{reject_reasons}"
        )
        raise _AllRowsRejected(
            f"订单行全部不可用（{detail}）：一行都没解析出来 ≠ 今天没卖东西，"
            "按整批不可用处理，不降级成 0 单 0 元",
            rows_seen=rows_seen,
            rows_accepted=rows_accepted,
            reasons=reject_reasons,
            rows_out_of_window=rows_out_of_window,
            samples=row_rejected,
        )

    order_days = set(buckets)  # 先记下"有订单的那些天"，再补 end 桶（否则 scope 判定会被自己污染）
    # 上面只建了"有订单的桶"；今天没单也必须有一个桶（`gmv=0` 是**事实**，不是缺数据），
    # 否则当日指标会整个缺失，`_day_uv` 的"全部订单都在今天"判定也会被空集误伤。
    today = buckets.get(window_end) or _bucket(window_end)
    day_uv = _day_uv(payload, window_end=window_end, order_days=order_days)
    if day_uv is not None:
        # 只有能确认"就是今天"时才写：`_day_uv` 返回 None 时保持桶里的 None（不补零）。
        today["uv"] = day_uv
    # 降级/拒绝留痕与聚合结果**分开返回**：调用方（`collect`）要把它们从 `order_summary` 里取走
    # 变成审计条目，而 `MetricsSnapshot` 的输入不该多出与指标无关的键。
    _check_row_accounting(
        rows_seen=rows_seen,
        rows_accepted=rows_accepted,
        rows_rejected=len(row_rejected),
        rows_out_of_window=rows_out_of_window,
        where="_aggregate_orders 返回前",
    )
    return {
        # `sorted(buckets)`：字典序恰好等于日期的时序（ISO 日期），历史均值的顺序由此保证。
        "by_date": {day: buckets[day] for day in sorted(buckets)},
        "units_degraded": units_degraded,
        "zero_revenue_paid": zero_revenue_paid,
        "row_rejected": row_rejected,
        # 被接受、但靠后续字段才拿到日期的行（v1.5.2）：单独一条 warn，不进 row_rejected。
        "date_fallback": date_fallbacks,
        # 拒绝率（v1.5.1）：`collect.done` 的审计靠这两个数把"0 单 0 元"归因清楚。
        "rows_seen": rows_seen,
        "rows_accepted": rows_accepted,
        # 窗口外的行数（v1.5.2）：`rows_seen - rows_accepted` 里的另一个桶，必须和
        # rows_accepted / rows_rejected 一起出现，否则"对账"永远差一截。
        "rows_out_of_window": rows_out_of_window,
        **today,
    }


def _audit_cost_table() -> dict[str, int] | None:
    """给**审计样本**用的成本表（`sku_id -> 整数分`）；读不到 → `None`。

    为什么 `collect` 要碰成本表：`collect.zero_revenue_paid` 的样本只有"这行没进成本口径"，
    没有单价就算不出"漏了多少成本"（事后只能看到 N 行，排不出该先修哪一单）。所以样本里补
    `units × unit_cost_cents`。但这一份**只进审计**，不参与任何指标累计。

    为什么 `ConfigError` 在这里降级成 `None`：成本表坏掉的**硬失败归 `compute_metrics`**
    （那里记 CONFIG 错并让 run 失败，`test_compute_metrics_node_fails_loudly…` 钉住了）。
    若这里也抛，一个配置事故会在采集阶段就把整轮 500，连"当天成交额是多少"都拿不到——
    那正是本文件反复要避免的"配置错误伪装成采集事故"。所以：**读不到就没有单价，不编一个 0**。
    """
    try:
        from app.services.costs import get_sku_costs

        # 懒 import：`costs` 会反过来读配置，模块级 import 会与 config 形成环。
        return get_sku_costs()
    except ConfigError:
        # 只接 ConfigError：那是"这家店没配成本/文件坏了"的已知状态。
        # 其它异常照常传播（我们自己的 bug 不许被降级成"没有成本"）。
        return None


#: `collect.failed` 的 `reason` 取值域（稳定分类，写进 JSONL 的 `data.reason` 与 `message`）。
#:
#: 为什么必须是**三个**互斥值而不是一句自然语言：`collect` 的失败在日志页上曾经只剩一条
#: `node.trace` 的 `outcome=error`，而"上游接口挂了"与"这批订单全被拒"在那一行里长得一模一样
#: ——运营据此要么去重启服务，要么去查数据，方向完全相反。分类是给人**分流**用的。
_COLLECT_FAIL_BATCH_CONTRACT = "batch_contract"
_COLLECT_FAIL_ALL_ROWS_REJECTED = "all_rows_rejected"
_COLLECT_FAIL_ORDERS_UNAVAILABLE = "orders_unavailable"


def _collect_failure_message(
    reason: str, *, rows_seen: int, rows_rejected: int, reject_reasons: Mapping[str, int]
) -> str:
    """`collect.failed` 的人读文案：`reason=...` 原样出现在句子里，日志页与 grep 一眼可辨。

    `data.reason` 是给机器/筛选用的事实，`message` 是给人看的一句话；两者必须表达同一个分类，
    否则"红行文案说接口挂了、data 却写 all_rows_rejected"就会变成新的自相矛盾。
    """
    if reason == _COLLECT_FAIL_ALL_ROWS_REJECTED:
        # 行都在、全被拒：文案必须带上"给了多少行/拒了多少行/按什么原因"，
        # 否则读者只能看到"失败"，无法判断是上游改结构还是真的没成交。
        return (
            f"采集失败（reason={reason}）：上游给了 {rows_seen} 行订单，"
            f"但一行都没通过校验（整行被拒 {rows_rejected} 行，按原因：{dict(reject_reasons)}），"
            "本轮没有产出任何指标"
        )
    if reason == _COLLECT_FAIL_BATCH_CONTRACT:
        # 整批契约坏：文案里逐个点名三个可能的位置（data / 列表字段 / summary），
        # 因为这三处的修法完全不同。
        return (
            f"采集失败（reason={reason}）：订单响应不满足契约"
            "（data / 订单列表字段 / summary 形状不对），整批不可用，本轮没有产出任何指标"
        )
    return (
        f"采集失败（reason={reason}）：订单列表接口没取到数据"
        "（上游错误已记在 errors 里），本轮没有订单数据，也没有任何指标"
    )


@traced("collect")
async def collect(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """采集：店铺信息 + 商品列表 + 近 8 个业务日的订单聚合（§3.3 `RawSnapshot`）。

    失败分级（§4.2 的 `collect --(fail)--> finalize`）：

    - **可重试**错误（timeout/connection/5xx/限流）→ 原样抛出，交给节点级 `RetryPolicy`
      按 §9.2 的预算重试；重试用尽才由 `run_agent` 兜住。
    - **不可重试**错误（4xx / business / signature / auth）→ 记 `errors`；其中订单（指标的唯一
      来源）缺失即判定 `failed` 走 `finalize`，其余降级为 `partial=True` 继续。

    ## 脏数据的容错与留痕（v1.5.1）

    订单明细里"件数缺失/非正"与"有 `pay_time` 却成交额为 0"是**真实会发生的脏数据**。前者
    按 1 件、后者不进成本口径（细节见 :func:`_order_units` / :func:`_aggregate_orders`），
    **都不抛异常**：抛出去会从 `collect` 逃出节点 → 图把 run 标 `failed` 并 re-raise
    → `POST /api/agent/run` 500，一条脏数据打死当天所有指标。

    代价是"这两个选择都会让毛利率偏乐观（漏报亏本）"，所以每次降级都写**两处**留痕：
    `state.audit`（`collect.units_degraded` / `collect.zero_revenue_paid`）与 JSONL——
    后者是它们唯一的人类出口（没有 approval_request、也没有 action_log 行）。

    ## 四种数据问题的分级（三批改动都并入契约 v1.5.1，因此这里只讲语义、不再分批次号）

    上一版只处理了"件数缺失/非正"和"0 元单"两种**能猜出含义**的降级，而
    `product_count: "3"` / `pay_amount: "199.00"` 这类**猜不出含义**的形状错仍然从 `collect`
    逃出 → 整轮 500。现在四档分明（判据写在 :func:`_aggregate_orders` 的 docstring）：

    1. **能猜出含义 → 有界降级**（件数按 1 件、0 元单不进成本口径、日期靠后续字段才解析成功
       也按解析到的日期计入但必须留痕），照常算指标；
    2. **一行的数据脏、猜不出含义 → 整行拒绝**：跳过该行并记 `collect.row_rejected`
       （warn，带 `count` + 最多 5 条样本 + 按原因计数），其余行照常算。"这一行一个已知订单
       字段都没有"（`{}` / `{"foo": 1}`）也在这一档，原因写作 `no_known_fields`——它**不是**
       "一笔 0 元订单"，见 :func:`_read_order_row` 第 0 条；
    3. **整批契约坏了 → 硬失败**（不 500）：`data` 不是映射/列表、订单列表字段不是 list、
       一个已知列表字段都没有、`summary.uv` 形状不对 → 记一条错误 + `failed` 走 `finalize`，
       绝不静默给出"0 单 0 元"；
    4. **看到了行但一行都没被接受 → 同样硬失败**（v1.5.1）：`rows_seen > 0 且
       rows_accepted == 0`（全部被拒 / 全部不是对象 / 全部在窗口外）→ :class:`_AllRowsRejected`，
       与第 3 档**同一条** `_BatchContractError` 路径。没有这一档，"每一行都坏"就会退化成
       零告警的 0 单 0 元——F1b 要避免的状态只是从"整批"挪到了"逐行"。

    只要出现任一降级/拒绝（`units_degraded` / `zero_revenue_paid` / `row_rejected` /
    `date_fallback`），`source["partial"]` 就必须是 True：这次采集不是完整的一份，raw 快照里要能看出来
    （v1.5.1 修掉了"行级拒绝后 partial 仍为 False"；v1.5.2 补上"日期降级也是降级"）。

    ## 两个出口都要有"这一轮采集怎么了"（成功与失败各一条）

    `collect.done`（info）与 `collect.failed`（error）**都**进 JSONL 汇，`data` 里都带
    `rows_seen` / `rows_accepted` / `rows_rejected` / `rows_out_of_window`（+ 失败时的
    `reason` / `reject_reasons` / `row_reject_samples`）。`collect.row_rejected` 也带同一组
    **分母/旁的桶**（v1.5.2）：否则日志页上的"7 行被拒"分不清是 7/7 还是 7/100，也拆不开
    "3 行脏 + 4 行窗口外"。三者必须满足行级账目恒等式
    `rows_seen == rows_accepted + rows_rejected + rows_out_of_window`（见
    :func:`_check_row_accounting`，`collect` 在每个出口写入前断言）。两个方向都要写：

    - 只写失败会让人以为"没红就是正常"：一次**正常**采集在前端/日志页上是沉默的，而沉默
      既可能是"跑完了没事"，也可能是"根本没跑"——这两件事在排障里完全不是一回事；
    - 只写成功则相反：整批契约坏与全行被拒又退回"只有一条 `node.trace` 的 `outcome=error`"，
      与"上游 503"无法区分（`reason` 就是为这条分叉而存在的）。

    审计是**旁路**：`_log_audit` 的写盘失败不会打断本节点（见该函数的 docstring）。
    """
    runtime = resolve_runtime(config)
    client = resolve_client(config, runtime)
    now = resolve_now(config)
    # 两次 `business_date(now)` 调用之间不能有 `resolve_now` 的重算：窗口必须基于**同一个**
    # "现在"，否则跨零点的采集会得到 start/end 不同源的窗口（一天的数据被算成两天）。
    today = business_date(now)
    # 窗口是"今天往前 7 天 + 今天"= 8 个业务日：`gmv_7d_avg_cents` 要 7 天历史（不含今天），
    # 加上今天自己才是 8 天。`HISTORY_WINDOW_DAYS` 就是那个 7。
    window_start = _shift_business_date(today, -HISTORY_WINDOW_DAYS)
    # `source` 是 `RawSnapshot.source`：方法清单 / request_id 清单 / 是否降级。
    # `partial` 一开始是 False，任何一处降级都必须把它置 True（"这份数据不完整"），
    # 它是 raw 快照里唯一能说明"今天的数据不全"的字段。
    source: dict[str, Any] = {"methods": [], "request_ids": [], "partial": False}
    errors: list[AgentError] = []

    def _record(exc: DoudianError, *, what: str) -> None:
        # `what` 是中文的"哪一段采集"，拼进 message 是为了让人在日志里直接看出
        # "店铺信息挂了"还是"订单挂了"——两者对指标的影响完全不同。
        errors.append(
            make_agent_error(
                node="collect",
                category=exc.category,
                message=f"{what} 采集失败：{exc}",
                retryable=bool(exc.retryable),
            )
        )

    # --- 店铺信息（非必需：缺了只是少了店铺画像） ---
    shop: dict[str, Any] = {}
    try:
        response = await client.get_shop_info()
    except DoudianError as exc:
        if exc.retryable:
            # 可重试错误原样抛出：交给节点级 RetryPolicy（§9.2）按预算重试，
            # 在这里 catch 掉等于把"上游抖了一下"变成"这家店没有画像"。
            raise
        _record(exc, what="店铺信息")
        source["partial"] = True
    else:
        # `else` 而不是 try 里继续写：只有真拿到响应才 `_note_call`，
        # 否则 `source.methods` 会列出"其实没成功"的方法名。
        _note_call(source, "shop.getShopInfo", response)
        shop = _normalize_shop(getattr(response, "data", None))

    # --- 商品列表（非必需：缺了只是这次不提议动作） ---
    products: list[dict[str, Any]] = []
    try:
        response = await client.list_products(page=0, size=100)
    except DoudianError as exc:
        if exc.retryable:
            raise
        _record(exc, what="商品列表")
        source["partial"] = True
    else:
        _note_call(source, "product.listV2", response)
        # 商品用 `_as_list`（丢非对象元素）：商品条目不参与任何金额/拒绝率指标，
        # 过滤掉不会伪造业务结论（订单列表则必须用 `_as_list_raw`）。
        for item in _as_list(getattr(response, "data", None), ("products", "items", "list")):
            normalized = _normalize_product(item)
            if normalized is not None:
                products.append(normalized)

    # --- 订单（必需：指标的唯一来源） ---
    # `None` 是本节点唯一的"失败信号"：它保持 None 就走下面的失败出口（status=failed）。
    # 注意这里不是 `{}`——空 dict 会被下游当成"今天 0 单"。
    order_summary: dict[str, Any] | None = None
    units_degraded: list[dict[str, Any]] = []
    zero_revenue_paid: list[dict[str, Any]] = []
    row_rejected: list[dict[str, Any]] = []
    #: 被接受、但靠后续日期字段才解析成功的行（v1.5.2）：行进了指标，所以**不**进 `row_rejected`。
    #: 它只写一条 warn 级 `collect.date_fallback`，并让 `source.partial=True`。
    date_fallbacks: list[dict[str, Any]] = []
    #: 拒绝率（v1.5.1）：`rows_seen` 是上游给的**行数**（含非对象元素/窗口外的行），
    #: `rows_accepted` 是真正进入指标的行数。两者进 `collect.done` / `collect.failed`，
    #: 否则"gmv=0"分不清"今天没卖东西"和"给的行全没解析出来"。
    rows_seen = 0
    rows_accepted = 0
    #: 窗口外行数（v1.5.2）：`rows_seen - rows_accepted` 的第二个桶。三个出口都必须带它，
    #: 否则"100 - 93 = 7"里那 4 行窗口外的数据在日志上无处可查。
    #: 四个桶（seen/accepted/rejected/out_of_window）一起满足
    #: `rows_seen == rows_accepted + rows_rejected + rows_out_of_window`。
    rows_out_of_window = 0
    reject_reasons: dict[str, int] = {}
    #: 失败分类（写进 `collect.failed` 的 `data.reason`）。**三档互斥**，默认是"接口没取到数据"：
    #: 订单请求本身失败时不会有任何异常对象可分类，只能靠这个标记说明"这不是数据脏"。
    failure_reason = _COLLECT_FAIL_ORDERS_UNAVAILABLE
    try:
        response = await client.search_orders(
            # 窗口按**业务日**拼成本地时间字符串（抖店要求 `YYYY-MM-DD HH:MM:SS`，北京时间），
            # 不要在这里做 UTC 转换：换一次就整体错一天。
            start_time=f"{window_start} 00:00:00", end_time=f"{today} 23:59:59", page=0, size=100
        )
    except DoudianError as exc:
        if exc.retryable:
            raise
        _record(exc, what="订单列表")
        source["partial"] = True
        # 非可重试的上游错误（4xx / business / signature / auth）→ `order_summary` 保持 None。
        # 分类留在上面的默认值上：它与"整批契约坏""全行被拒"是**不同**的处置方向。
    else:
        _note_call(source, "order.searchList", response)
        try:
            aggregate = _aggregate_orders(
                getattr(response, "data", None),
                window_start=window_start,
                window_end=today,
                # 成本表只用来给审计样本补 `unit_cost_cents`（事后量化"漏了多少成本"），
                # 读不到就 None——**成本表坏掉的硬失败归 compute_metrics**，这里不许抢先炸。
                cost_table=_audit_cost_table(),
            )
        except _BatchContractError as exc:
            # 整批不可用：`data`/列表字段/`summary.uv` 形状坏了，**或**（v1.5.1）看到了行但
            # 一行都没被接受（`_AllRowsRejected`）。两种都记一条具体错误，`order_summary`
            # 保持 None → 下面统一走"订单数据缺失"的硬失败分支（status=failed → finalize）。
            # 绝不继续用"0 单"把一次上游改结构/整批脏行伪装成"今天没卖东西"。
            #
            # 这里**不**捕获普通 `ValueError`：数据侧只抛 ValueError 的地方已经由
            # `_read_order_row` 转成行级拒绝；逃到这里的 ValueError 是我们自己代码的 bug，
            # 不许被降级成"数据脏"（v1.5.1 收窄，见 P2）。
            if isinstance(exc, _AllRowsRejected):
                failure_reason = _COLLECT_FAIL_ALL_ROWS_REJECTED
                message = f"订单行全部被拒绝，无法聚合成指标：{exc}"
                rows_seen = exc.rows_seen
                rows_accepted = exc.rows_accepted
                # 全部落在窗口外时（rows_out_of_window == rows_seen），失败审计也要能看出
                # "不是脏、是窗口外"，否则这条失败会被误读成"上游全是脏行"（v1.5.2）。
                rows_out_of_window = exc.rows_out_of_window
                reject_reasons = dict(exc.reasons)
                # 把拒绝样本接回来：`collect.failed` 的审计要能按原因计数（`rows_rejected`
                # 与 `reject_reasons` 都从这里来），否则失败分支只剩一句"不可用"。
                row_rejected = list(exc.row_rejected)
            else:
                failure_reason = _COLLECT_FAIL_BATCH_CONTRACT
                message = f"订单数据结构不合法，无法聚合：{exc}"
            errors.append(
                make_agent_error(
                    node="collect",
                    category=ErrorCategory.UNKNOWN,
                    message=message,
                    retryable=False,
                )
            )
            source["partial"] = True
        else:
            # 成功分支：把"降级留痕"和"指标"拆开取走——`order_summary` 只留指标相关的键，
            # `MetricsSnapshot` 不该混进 `rows_seen` 这类审计字段。
            by_date = aggregate.pop("by_date")
            units_degraded = aggregate.pop("units_degraded")
            zero_revenue_paid = aggregate.pop("zero_revenue_paid")
            row_rejected = aggregate.pop("row_rejected")
            date_fallbacks = aggregate.pop("date_fallback")
            rows_seen = int(aggregate.pop("rows_seen"))
            rows_accepted = int(aggregate.pop("rows_accepted"))
            rows_out_of_window = int(aggregate.pop("rows_out_of_window"))
            # `by_date` 再塞回去：`RawSnapshot.order_summary` 需要它来做历史均值。
            order_summary = {**aggregate, "by_date": by_date}
            # 只要有行被降级/拒绝，这次采集就**不是完整的一份**：`source.partial` 必须说出来。
            # 旧实现在行级拒绝后仍然是 partial=False（审查实测），于是"今天的数据不全"这件事
            # 在 raw 快照里不可见，只有一条 warn 审计——两个出口的口径互相矛盾。
            # v1.5.2：日期降级（靠 pay_time 才解析成功）同样是"这份数据不是原样的"，也要置位。
            if units_degraded or zero_revenue_paid or row_rejected or date_fallbacks:
                source["partial"] = True

    if order_summary is None:
        # 失败出口也必须对账（v1.5.2）：`batch_contract` / `orders_unavailable` 两个分支
        # 根本没读到行（四个数都是 0），恒等式照样成立；`all_rows_rejected` 分支带着
        # `rows_out_of_window` 回来，不校验就可能写出一个"被拒 + 窗口外 ≠ 看到"的审计。
        _check_row_accounting(
            rows_seen=rows_seen,
            rows_accepted=rows_accepted,
            rows_rejected=len(row_rejected),
            rows_out_of_window=rows_out_of_window,
            where="collect.failed 写入前",
        )
        # 这一条与上面 `_record` 的"具体哪一段失败"并列：它说明的是"缺的是必需数据"，
        # 而不是"某次调用失败了"——两者都保留，才能既知道原因又知道后果。
        errors.append(
            make_agent_error(
                node="collect",
                category=ErrorCategory.UNKNOWN,
                message="订单数据缺失，无法计算指标（这是指标的唯一来源）",
                retryable=False,
            )
        )
        # 拒绝样本与 `collect.row_rejected` 共用同一个上限与前缀语义：`rows_rejected` 是全量，
        # `row_reject_samples` 只是现场前几条。不复用 `_degradation_summary` 的输出结构是因为
        # 这里的 `data` 以"这一轮采集的整体结果"为主，样本只是它的一个子字段。
        reject_samples = [dict(item) for item in row_rejected[:_DEGRADATION_SAMPLE_LIMIT]]
        failed_data: dict[str, Any] = {
            "window": {"start": window_start, "end": today},
            # `source` 整块带出（含 `partial` 与调过的方法）：失败现场也要能看出"打到哪一步"。
            "source": source,
            # 失败原因必须**一眼可辨**：`batch_contract`（上游改了结构/形状坏）与
            # `all_rows_rejected`（行都在、全被拒）与 `orders_unavailable`（接口没取到数）
            # 是三个不同的处置方向，只靠一条 `node.trace(outcome="error")` 分不开。
            "reason": failure_reason,
            # 失败也必须带拒绝率：`gmv=0` 与"一行都没解析出来"在 trace 上是同一行摘要，
            # 只有这几个数能把"生意归零"和"数据不可用"分开。
            # v1.5.2：`rows_out_of_window` 是 `rows_seen - rows_accepted` 的第二个桶，
            # 少了它，失败审计里的"7 行没进指标"仍然无法拆成"3 行脏 + 4 行窗口外"。
            "rows_seen": rows_seen,
            "rows_accepted": rows_accepted,
            "rows_rejected": len(row_rejected),
            "rows_out_of_window": rows_out_of_window,
            "reject_reasons": dict(reject_reasons),
            "row_reject_samples": reject_samples,
            "row_reject_samples_truncated": len(row_rejected) > len(reject_samples),
        }
        # 双出口：`state.audit`（checkpoint，可回放）+ JSONL 汇（`/api/logs` 的唯一数据源）。
        # 只写前者 = 整批失败在日志页上不存在，运营只能看见一条和"上游 503"同形的红行。
        _log_audit(
            state,
            node="collect",
            event="collect.failed",
            level="error",
            message=_collect_failure_message(
                failure_reason,
                rows_seen=rows_seen,
                rows_rejected=len(row_rejected),
                reject_reasons=reject_reasons,
            ),
            data=failed_data,
        )
        return {
            # `status=failed` 是这一轮的唯一"判死"信号：`traced()` 据此记 outcome=error，
            # 图层的 `route_after_collect` 据此直奔 finalize（`raw` 也没有，两条判据一致）。
            "errors": _errors(state, errors),
            "audit": _audit(
                state,
                node="collect",
                event="collect.failed",
                # `state.audit` 这一份用同一个 `failed_data`：两个出口写**同一份事实**，
                # 差异只在于"谁能看见"（checkpoint 读者 vs 日志页读者）。
                data=failed_data,
            ),
            "status": RunStatus.FAILED.value,
        }

    raw = RawSnapshot(
        # `collected_at` 用节点开头取到的那个 `now`（不是新的 utcnow）：整份快照的时间基准
        # 只有一个，否则 `input_digest` 会每次都不一样。
        collected_at=utc_iso(now),
        window={"start": window_start, "end": today},
        shop=shop,
        products=products,
        order_summary=order_summary,
        source=source,
    )
    _check_row_accounting(
        rows_seen=rows_seen,
        rows_accepted=rows_accepted,
        rows_rejected=len(row_rejected),
        rows_out_of_window=rows_out_of_window,
        where="collect.done 写入前",
    )
    done_data: dict[str, Any] = {
        "window": raw["window"],
        # 商品条数而不是商品列表本身：日志里不需要几百条 SKU 明细（那是 raw 快照的事）。
        "products": len(products),
        "methods": list(source["methods"]),
        "partial": bool(source["partial"]),
        # 拒绝率：分母=上游给的行数，分子=真正进入指标的行数。与
        # `rows_rejected`（整行被拒的条数）一起构成"这一轮采集到底发生了什么"的汇总。
        # v1.5.2 起再加 `rows_out_of_window`：`rows_seen - rows_accepted` 必须能被这两个桶
        # 完整解释（账目恒等式见 `_check_row_accounting`），否则读者会把窗口外的行算成脏行。
        "rows_seen": rows_seen,
        "rows_accepted": rows_accepted,
        # 整行被拒的条数在这里也出现一次：`collect.done` 是"这一轮采集到底发生了什么"的
        # 汇总入口，读者不必先去翻 `collect.row_rejected` 才知道今天有几行没进指标。
        "rows_rejected": len(row_rejected),
        "rows_out_of_window": rows_out_of_window,
    }
    audits = _audit(
        state,
        node="collect",
        event="collect.done",
        # 审计的 `at` 用 `collected_at`（evaluate 时刻）：这条事件描述的是"那次采集"，
        # 不是"写审计的这一刻"。
        data=done_data,
        at=raw["collected_at"],
    )
    # 成功也要落盘：只有失败落盘会让"正常"变成沉默，而"沉默"既可能是"跑完没事"，
    # 也可能是"这一轮根本没跑"——排障时这两件事的下一步完全不同。
    partial_text = "true" if source["partial"] else "false"
    _log_audit(
        state,
        node="collect",
        event="collect.done",
        level="info",
        message=(
            f"采集完成：上游给 {rows_seen} 行订单，{rows_accepted} 行进入指标，"
            f"{len(row_rejected)} 行整行被拒（partial={partial_text}）"
        ),
        data=done_data,
    )
    # 采集层的三次**容错**必须留痕（v1.5.2：units / zero_revenue_paid / date_fallback）。
    # 它们不产生 approval_request、不进 action_log，所以除了 `state.audit` 之外还必须写一条
    # JSONL：否则"这次为什么漏报亏本 / 为什么日期口径变了"在日志页上找不到任何线索。
    if units_degraded:
        data = _degradation_summary("units", units_degraded)
        # `state.audit` 与 JSONL 都写：这一条既不产生 approval_request 也不进 action_log，
        # 除了这两个出口之外没有任何人类可见的痕迹。
        # 顺序：先 append 进 `audits`（checkpoint 那份），再 `_log_audit` 写盘（旁路那份）。
        # 这样即使写盘失败，`state.audit` 已经带上了这条事实。
        audits = append_audits(
            {"audit": audits},  # type: ignore[arg-type]
            [make_audit_entry(node="collect", event="collect.units_degraded", data=data)],
        )
        _log_audit(
            state,
            node="collect",
            event="collect.units_degraded",
            level="warn",
            message=(
                f"有 {data['count']} 条订单行缺件数/件数非正，已按 1 件计入成本口径"
                "（方向偏乐观：会漏报亏本）"
            ),
            data=data,
        )
    if zero_revenue_paid:
        data = _degradation_summary("zero_revenue_paid", zero_revenue_paid)
        audits = append_audits(
            {"audit": audits},  # type: ignore[arg-type]
            [make_audit_entry(node="collect", event="collect.zero_revenue_paid", data=data)],
        )
        _log_audit(
            state,
            node="collect",
            event="collect.zero_revenue_paid",
            level="warn",
            message=(
                f"有 {data['count']} 条已支付订单行成交额为 0（可能缺 pay_amount，"
                "也可能真是 0 元单），未计入成本口径（避免虚增成本造成误报亏本）"
            ),
            data=data,
        )
    if row_rejected:
        # 行级拒绝（v1.5.1）：这些行**整行**没进指标。审计必须把这件事说出来，否则
        # "GMV 比昨天少了一截"就永远无法归因到"上游有几行脏数据被跳过了"。
        _check_row_accounting(
            rows_seen=rows_seen,
            rows_accepted=rows_accepted,
            rows_rejected=len(row_rejected),
            rows_out_of_window=rows_out_of_window,
            where="collect.row_rejected 写入前",
        )
        data = _degradation_summary("row_rejected", row_rejected)
        # 这个标记让消费方不必读文档就知道"这些行没进指标"——`count` 只是"有 N 行不合法"，
        # 真正要紧的是"它们被**排除**在指标之外了"。
        # 这个键只在这里出现（`units` / `date_fallback` 两类没有它：那两类会进指标）。
        data["excluded_from_metrics"] = True
        # **分母**必须跟着分子一起出现（v1.5.2）：只写 `count`（被拒条数）时，日志页上的
        # "7 行被拒"分不清是 7/7（整批脏）还是 7/100（个别脏）。`rows_seen` / `rows_accepted`
        # / `rows_out_of_window` 与 `collect.done` / `collect.failed` 同一个口径
        # （上游给的行数 vs 进入指标的行数 vs 窗口外的行数）。
        data["rows_seen"] = rows_seen
        data["rows_accepted"] = rows_accepted
        data["rows_out_of_window"] = rows_out_of_window
        # 三行都是"与 `collect.done` 同口径"的分母/旁桶：同一组数在三个出口里必须一致，
        # 否则日志页上"被拒 7 行"在读 `done` 和读 `row_rejected` 时会得到不同结论。
        # 文案必须自证"样本只是样本"（v1.5.1）：`count` 是全量，`samples` 只有前 N 条。
        # 只写"样本见 data.samples"会让读者把"看到 5 条"当成"一共就 5 条"。
        sample_note = f"仅前 {data['sample_limit']} 条样本" + (
            "（其余已截断）" if data["samples_truncated"] else ""
        )
        audits = append_audits(
            {"audit": audits},  # type: ignore[arg-type]
            [make_audit_entry(node="collect", event="collect.row_rejected", data=data)],
        )
        _log_audit(
            state,
            node="collect",
            event="collect.row_rejected",
            level="warn",
            message=(
                f"有 {data['count']} 条订单行数据不合法（按原因：{data['reasons']}），已整行跳过："
                "这些行没有进入 GMV / 订单数 / by_sku 中的任何一个指标"
                f"（不是静默少算：样本见 data.samples，{sample_note}）"
            ),
            data=data,
        )
    if date_fallbacks:
        # 日期降级（v1.5.2）：行是**被接受**的（进了指标），所以**不**写进 `row_rejected`。
        # 但没有这条留痕，"上游把 create_time 全写成 False、靠 pay_time 照常算"就永远查不出来：
        # 指标看起来一切正常，业务日期口径却已经悄悄从"下单日"换成了"支付日"。
        data = _degradation_summary("date_fallback", date_fallbacks)
        # 与 `row_rejected` 用同一套"样本只是样本"的措辞：`count` 是全量、`samples` 是前 N 条。
        sample_note = f"仅前 {data['sample_limit']} 条样本" + (
            "（其余已截断）" if data["samples_truncated"] else ""
        )
        audits = append_audits(
            {"audit": audits},  # type: ignore[arg-type]
            [make_audit_entry(node="collect", event="collect.date_fallback", data=data)],
        )
        _log_audit(
            state,
            node="collect",
            event="collect.date_fallback",
            level="warn",
            message=(
                f"有 {data['count']} 条订单行的 create_time 不可用、靠后续日期字段才解析成功"
                f"（按原因：{data['reasons']}）：已按实际解析到的日期计入指标，"
                "上游 create_time 可能正在系统性写坏（样本见 data.samples，"
                f"{sample_note}）"
            ),
            data=data,
        )
    return {
        "raw": raw,
        "errors": _errors(state, errors),
        "audit": audits,
    }


def route_after_collect(state: AgentState) -> str:
    """§4.2 唯一的"入口"条件边：采集硬失败就直奔 `finalize`。

    保证：只返回两个合法分支名（`"finalize"` / `"compute_metrics"`），与 `graph.build_graph`
        里那张映射表的键逐字一致；`status=failed` 优先于"有没有 raw"。
    不保证：不保证 `compute_metrics` 一定成功——它自己还会因为 raw 缺字段/成本表坏掉失败。
    调用方坑：返回值是**节点名**而不是布尔；两个判据（`_aborted` / `raw` 缺失）是有意重叠的
        （失败分支通常也没 raw），改成一个会漏掉"raw 在但 run 已判死"的那条路径。
    另外：本函数是**纯读**的（不改 state），所以重放安全——LangGraph 可能在任意时刻
        为决定路由而多次调用它。
    """
    if _aborted(state) or not state.get("raw"):
        # 两个判据都要留：`status=failed` 但 raw 在（采集部分成功后被判死）不能继续算指标；
        # 反过来 raw 缺失但状态还不是 failed（节点异常早退）也必须收口。
        return "finalize"
    return "compute_metrics"


# ======================================================================================
# 2) compute_metrics
# ======================================================================================


def _unmatched_cost_skus(
    products: Sequence[Mapping[str, Any]], cost_table: Mapping[str, int]
) -> list[str]:
    """成本表里有、这次商品列表里**没有**的 `sku_id`（§8.5 要求"不能静默忽略"）。

    为什么不在启动期做（契约原文写的是启动期 `ConfigError`）：启动期根本拿不到
    `raw.products`——商品列表来自一次上游调用。静态交叉校验在物理上不可能，能做的只有
    在第一次看到商品列表时把名单喊出来（调用方记 audit）。

    为什么商品列表为空时返回空名单：采集降级（`partial=True`）时没有商品列表，
    此时**每一条**成本都会"看起来对不上"，报出来是一串假警报，反而把真问题淹掉。
    """
    if not products:
        # 商品列表为空 = 这次采集降级了：此时**每一条**成本都会"看起来对不上"，
        # 报出来是一串假警报，反而把真问题（真的改过 sku_id）淹掉。
        return []
    known = {
        str(item.get("sku_id"))
        for item in products
        # 只收"有 sku_id 的对象条目"：没有 sku 的商品（纯 product 级）不参与 SKU 成本归属。
        if isinstance(item, Mapping) and item.get("sku_id")
    }
    # 差集排序输出：审计里给人看的名单必须稳定（同一次采集重跑得到同样的顺序）。
    return sorted(set(cost_table).difference(known))


@traced("compute_metrics")
async def compute_metrics_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """算出当日指标 + 近 7 日历史（含今天），供均值/环比使用（§5.3）。

    保证：成功时 `metrics` 是当日的 `MetricsSnapshot`，`history` 是按日期升序的序列
        （每条都带"它自己那一刻"的 7 日均值），最多 9 条（8 天窗口 + 今天）。
    不保证：不保证 `raw` 缺失时能算出东西——那是 `status=failed`（CONFIG 类错误），
        不是"今天 0 单"；也不保证毛利率有值（没配成本时 `gross_margin=None`，不是 0）。
    调用方坑：本节点是**成本表坏掉时的硬失败点**（`ConfigError` → failed）；`collect` 那边
        只把成本表当审计样本用。想改这个分工要先读 `_audit_cost_table` 的说明。
    """
    from app.services.metrics import compute_metrics

    if _aborted(state):
        # 前面的节点已经判死：不产生任何数字，`{}` 让 state 原样保留。
        return {}
    raw = state.get("raw")
    if not isinstance(raw, Mapping):
        return {
            "errors": _errors(
                state,
                [
                    make_agent_error(
                        node="compute_metrics",
                        category=ErrorCategory.CONFIG,
                        message="raw 缺失，无法计算指标",
                        retryable=False,
                    )
                ],
            ),
            "status": RunStatus.FAILED.value,
        }
    now = resolve_now(config)
    by_date = dict((raw.get("order_summary") or {}).get("by_date") or {})
    # 当日 = 采集窗口的 `end`（不是"现在"的日期）：跨零点重跑时，指标仍属于那一轮采集的
    # 业务日，否则同一次采集会算出"昨天"的数字。
    today = str(raw["window"]["end"])

    # 成本表（§8.5）：正常由 lifespan 先加载并校验；这里取缓存的同一份。若它坏了（有人在
    # lifespan 之后重置了缓存又改坏了文件），按"配置错误"记进 errors 并让 run 失败——
    # 绝不降级成"没有成本"继续跑：那会把一次配置事故伪装成"这家店没配成本"的正常状态。
    try:
        from app.services.costs import get_sku_costs

        cost_table = get_sku_costs()
    except ConfigError as exc:
        return {
            "errors": _errors(
                state,
                [
                    make_agent_error(
                        node="compute_metrics",
                        category=ErrorCategory.CONFIG,
                        message=f"成本表加载失败，无法计算毛利率：{exc}",
                        retryable=False,
                    )
                ],
            ),
            "status": RunStatus.FAILED.value,
        }

    unmatched_cost_skus = _unmatched_cost_skus(raw.get("products") or [], cost_table)

    history: list[MetricsSnapshot] = []
    for day in sorted(by_date):
        # 每一天都**重新构造**一份只有那一天的 RawSnapshot：历史均值必须按"当天当时能看到
        # 的数据"算，直接复用整段 raw 会让每一天都拿到 8 天合计。
        day_raw = RawSnapshot(
            collected_at=str(raw.get("collected_at") or ""),
            window={"start": day, "end": day},
            # 历史日的 shop/products 留空：金不参与指标，省一份大对象进内存。
            shop={},
            products=[],
            order_summary=dict(by_date[day]),
            source={"methods": [], "request_ids": [], "partial": False},
        )
        # 逐日累积：每一条历史都能带着"它自己那一刻"的 7 日均值，而不是全 None。
        history.append(
            compute_metrics(day_raw, history=list(history), now=now, cost_table=cost_table)
        )

    if history and history[-1]["date"] == today:
        # 最后一天正好是今天：直接用它当 metrics，避免把今天算两遍（重复 append）。
        metrics = history[-1]
    else:
        # 今天没有订单桶（上游没给今天的行）：仍然要为今天算一份 —— 全 0 是**事实**，
        # 而"没有今天的指标"会让看板缺一天。
        metrics = compute_metrics(raw, history=list(history), now=now, cost_table=cost_table)
        history.append(metrics)

    audit_data = {
        "date": metrics["date"],
        "gmv_cents": metrics["gmv_cents"],
        "history_days": len(history),
        "gmv_7d_avg_cents": metrics["gmv_7d_avg_cents"],
        # 成本口径的两个字段进审计：`None`（没配成本/没按 SKU 归集）与真实的
        # `0.0`（配了成本但一件没卖）在这里必须能被区分，否则事后无法解释
        # "为什么这次 loss_making 没响"。
        "gross_margin": metrics["gross_margin"],
        "cost_coverage_ratio": metrics["cost_coverage_ratio"],
    }
    audits = _audit(
        state,
        node="compute_metrics",
        event="metrics.computed",
        # 审计时间用指标自己的 `computed_at`：它与 `metrics.created_at` 是同一时刻，
        # 拿 `utcnow()` 会让两个出口的时间差几十毫秒。
        data=audit_data,
        at=str(metrics["computed_at"]),
    )
    # 同一个事实也写进 JSONL：`/api/logs?event=metrics.` 是"这一天到底算出了什么"的唯一
    # 人类入口（成本口径的两个字段在这里是 None 还是 0.0，决定了 loss_making 该不该响）。
    _log_audit(
        state,
        node="compute_metrics",
        event="metrics.computed",
        level="info",
        message=(
            f"指标已计算：{metrics['date']} gmv={metrics['gmv_cents']} 分 "
            f"gross_margin={metrics['gross_margin']} "
            f"cost_coverage_ratio={metrics['cost_coverage_ratio']}"
        ),
        # 与 `state.audit` 同一份 `data`：两个出口表达同一个事实，只是可见性不同。
        data=audit_data,
    )
    if unmatched_cost_skus:
        # §8.5 要求"成本表里对不上商品的条目不能静默忽略"。启动期没有 `raw.products`，
        # 静态交叉校验不可能，所以在这里（第一次能看到商品列表的地方）记一条可见的审计；
        # 只记不阻断：一条过期的成本配置不该让当天所有指标都算不出来。
        unmatched_data = {"sku_ids": unmatched_cost_skus, "count": len(unmatched_cost_skus)}
        audits = append_audits(
            {"audit": audits},  # type: ignore[arg-type]
            [
                make_audit_entry(
                    node="compute_metrics",
                    event="sku_costs.unmatched",
                    data=unmatched_data,
                )
            ],
        )
        _log_audit(
            state,
            node="compute_metrics",
            event="sku_costs.unmatched",
            level="warn",
            message=(
                f"成本表里有 {len(unmatched_cost_skus)} 个 SKU 不在本次商品列表中"
                "（这些条目的成本不会参与毛利计算，检查是否下架/改 sku_id 后没同步）"
            ),
            data=unmatched_data,
        )

    return {
        "metrics": metrics,
        # 只留最近 9 条：`history` 太长会一直涨 checkpoint 体积（每轮 run 一份），
        # 而均值最多只需要 8 天。
        "history": history[-9:],
        "audit": audits,
    }


# ======================================================================================
# 3) detect_anomalies
# ======================================================================================


@traced("detect_anomalies")
async def detect_anomalies_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """按 `rules.yaml` 逐条求值；跳过与截断都必须可见（§5.4）。

    保证：`anomalies` 是规则求值的结果（已按 `max_anomalies` 截断）；每条被跳过的规则、
        每一条被截断的异常都会进 `errors`——"规则没响"与"规则没求值"必须能区分。
    不保证：不保证告警一定给出动作（`loss_making` 只告警不给审批），也不保证求值成功
        （表达式写错 → 该条进 `skipped`，其余规则照常跑）。
    调用方坑：`metrics` 缺失是 CONFIG 硬失败（`status=failed`），不是"没有异常"；
        不要把它降级成空 `anomalies`，那会把"指标没算出来"画成"今天很健康"。
    """
    from app.services.anomalies import evaluate_rules

    if _aborted(state):
        return {}
    metrics = state.get("metrics")
    if not isinstance(metrics, Mapping):
        return {
            "errors": _errors(
                state,
                [
                    make_agent_error(
                        node="detect_anomalies",
                        category=ErrorCategory.CONFIG,
                        message="metrics 缺失，无法检测异常",
                        retryable=False,
                    )
                ],
            ),
            "status": RunStatus.FAILED.value,
        }
    runtime = resolve_runtime(config)
    now = resolve_now(config)
    evaluation = evaluate_rules(
        metrics=metrics,  # type: ignore[arg-type]
        # 历史只给 `history`（不含今天的 metrics）：环比类规则自己决定要不要把今天并进去，
        # 传"含今天的全部"会让"昨天 vs 今天"变成"今天 vs 今天"。
        history=state.get("history") or [],
        rules=runtime.rules,
        run_id=str(state["run_id"]),
        now=now,
        max_anomalies=int(runtime.settings.agent.max_anomalies),
    )
    # 跳过的规则也进 errors：一条写坏的表达式若只被静默跳过，规则等于悄悄失效了。
    errors = [
        make_agent_error(
            node="detect_anomalies",
            category=ErrorCategory.CONFIG,
            message=f"规则 {rule_id} 求值失败已跳过：{reason}",
            retryable=False,
        )
        for rule_id, reason in evaluation.skipped
    ]
    if evaluation.dropped:
        # 截断必须是**事故**而不是常态：超过 `max_anomalies` 说明规则命中过多，
        # 静默丢弃会让"告警太多"看起来像"告警刚好"。
        errors.append(
            make_agent_error(
                node="detect_anomalies",
                category=ErrorCategory.UNKNOWN,
                message=f"异常条数超过 max_anomalies={runtime.settings.agent.max_anomalies}，"
                f"丢弃 {evaluation.dropped} 条",
                retryable=False,
            )
        )
    anomalies: list[Anomaly] = evaluation.anomalies
    #: `kind` 是**同一 event 名下两种 schema 的判别键**（v1.5.1）：`anomaly.detected` 既写一条
    #: 汇总、又给每条异常写一条明细。过去消费方只能靠"data 里有没有 rule_id"来猜两种形状
    #: （明细没有 `count`、汇总没有 `rule_id`），猜错的代价是 `.get()` 满天飞 + 静默把汇总当明细。
    #: 这与同文件 `collect.*` 事件已有的风格一致（它们的 `data.kind` 是 `units` /
    #: `zero_revenue_paid` / `row_rejected`），所以这里补的不是新发明，而是把已有的约定补齐。
    summary = {
        "kind": "summary",
        "count": len(anomalies),
        "rule_ids": [item["rule_id"] for item in anomalies],
        "skipped": [rule_id for rule_id, _ in evaluation.skipped],
        "dropped": evaluation.dropped,
    }
    audits = _audit(
        state, node="detect_anomalies", event="anomaly.detected", data=summary
    )
    # 一条**汇总**行（命中几条、跳过/截断几条）：level 恒为 info——它是"这次检测跑了"的心跳，
    # 不是告警本身；真正的告警在下面每条异常一行、按 severity 定 level。
    _log_audit(
        state,
        node="detect_anomalies",
        event="anomaly.detected",
        level="info",
        message=(
            f"异常检测完成：命中 {len(anomalies)} 条，跳过 "
            f"{len(evaluation.skipped)} 条规则，截断 {evaluation.dropped} 条"
        ),
        data=summary,
    )
    for anomaly in anomalies:
        # 每条异常一条独立事件（v1.5.1 的根因修复）：只写汇总的话，`loss_making` 这类
        # "只告警、不产生 approval_request"的规则仍然没有出口——审批台看不到（没有待办），
        # 日志页也查不到（event 里只有 count/rule_ids，没有 observed）。
        _log_audit(
            state,
            node="detect_anomalies",
            event="anomaly.detected",
            # level 由 severity 查表派生（critical→error），不是写死 info：
            # 否则同一条事实会在"红"和"绿"两种口径下自相矛盾。
            level=_SEVERITY_LEVEL[str(anomaly["severity"])],
            message=str(anomaly["message"]),
            data={
                # 与上面那条汇总用同一个 `kind` 判别键（`"detail"` / `"summary"`）：
                # 消费方按 `kind` 取，不再靠"有没有 rule_id"猜这两种形状。
                "kind": "detail",
                "anomaly_id": anomaly["anomaly_id"],
                "rule_id": anomaly["rule_id"],
                "metric": anomaly["metric"],
                "observed": anomaly["observed"],
                "threshold": anomaly["threshold"],
                "comparator": anomaly["comparator"],
                "severity": str(anomaly["severity"]),
                "window": anomaly["window"],
                "detected_at": anomaly["detected_at"],
            },
        )
    return {
        "anomalies": anomalies,
        "errors": _errors(state, errors),
        "audit": audits,
    }


# ======================================================================================
# 4) propose_actions
# ======================================================================================


@traced("propose_actions")
async def propose_actions_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """异常 → 动作提议（白名单类型 + 参数已归一化 + `risk` 已判定，§5.5）。

    保证：`pending_actions` 里每一条都是**可审批**的提议（`action_id` 已生成、
        `params` 已按动作 schema 归一化、`risk` 已判定），且不含 `idempotency_key`
        （它由审批后的最终参数决定，见 `approval_gate`）。
    不保证：不保证有提议——没有异常时返回空列表，那不叫失败（状态仍是 running）。
    调用方坑：模板展开失败的条目进 `errors` 而不是让本节点失败；`pending_actions` 为空
        会让 `route_after_propose_actions` 直通 `finalize`（不进审批），这是有意的。
    """
    from app.services.actions import propose_actions_with_report

    if _aborted(state):
        return {}
    anomalies = list(state.get("anomalies") or [])
    if not anomalies:
        # 无异常 = 无提议：直接交给 finalize（不进审批，见 §4.2 的"无提议时直通"）。
        # 仍然写一条 count=0 的审计：否则"这轮跑过了、只是没事"在日志里完全不存在。
        return {
            "pending_actions": [],
            "audit": _audit(
                state, node="propose_actions", event="action.proposed", data={"count": 0}
            ),
        }
    metrics = state.get("metrics")
    raw = state.get("raw")
    if not isinstance(metrics, Mapping) or not isinstance(raw, Mapping):
        return {
            "errors": _errors(
                state,
                [
                    make_agent_error(
                        node="propose_actions",
                        category=ErrorCategory.CONFIG,
                        message="metrics/raw 缺失，无法提议动作",
                        retryable=False,
                    )
                ],
            ),
            "status": RunStatus.FAILED.value,
        }
    runtime = resolve_runtime(config)
    report = propose_actions_with_report(
        anomalies=anomalies,
        metrics=metrics,  # type: ignore[arg-type]
        # 商品列表用于把 "sku_id" 还原成上游要的 product_id/sku_id 对（`evidence.target_snapshot`），
        # 缺它时 `_target_ids` 会退回"target 兼任"的降级路径。
        products=raw.get("products") or [],
        rules=runtime.rules,
        run_id=str(state["run_id"]),
        now=resolve_now(config),
    )
    # 模板展开失败（规则里引用了不存在的参数、动作类型不在白名单）只记错不失败：
    # 少一条提议比整轮什么都不提议要好，但必须可见。
    errors = [
        make_agent_error(
            node="propose_actions",
            category=ErrorCategory.CONFIG,
            message=f"动作模板未展开：{reason}",
            retryable=False,
        )
        for reason in report.dropped
    ]
    proposals: list[ActionProposal] = report.proposals
    return {
        "pending_actions": proposals,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="propose_actions",
            event="action.proposed",
            data={
                "count": len(proposals),
                "action_ids": [item["action_id"] for item in proposals],
                "risks": [str(item["risk"]) for item in proposals],
                "dropped": len(report.dropped),
            },
        ),
    }


def route_after_propose_actions(state: AgentState) -> str:
    """无提议（无异常 / 规则没配动作）→ 不进入审批，直接 `finalize`。

    §4.2 的拓扑图在 `propose_actions` 与 `finalize` 之间画了这条"直通"边，`:514` 的
    "(等待审批 / 无提议时直通)" 就是它；本函数是它的实现。

    保证：只返回 `"finalize"` / `"approval_gate"` 两个分支名（映射表的键）。
    不保证：不保证"有提议就一定会挂起"——`approval_gate` 自己还有一道"待办为空就直通"的
        防御性判断（拓扑上走不到，但那条判断留着）。
    调用方坑：判据是 `pending_actions` **非空**而不是"有没有异常"：异常可能一个动作都
        没配模板（那时没有可审批的东西，直接收口）。
    """
    if _aborted(state) or not state.get("pending_actions"):
        return "finalize"
    return "approval_gate"


# ======================================================================================
# 5) approval_gate（interrupt 发生在这里，§4.4 逐字）
# ======================================================================================


def _validate_decisions_shape(payload: Any) -> list[Decision]:
    """恢复值的外层形状校验（类型/枚举/edited_params 规则），§4.4 规则 1/3/5。

    保证：返回的每条 `Decision` 都补齐了 `decided_at`（缺省用服务端时间）、`verdict` 是
        `Verdict` 枚举、`edited_params` 已按规则校验过（edit 非空、其它必须 null）。
    不保证：**不做**与 `pending_actions` 的一致性校验（多/少/重复 action_id）——那是
        :func:`validate_resume_payload` 的事，分工是"本函数只看请求体内部"。
    调用方坑：任何不合法都抛 `ApprovalValidationError`(400)，不要在外面吞成 500；
        `decided_at` 由服务端盖，客户端传的值只在"合法字符串"时保留。
    """
    if not isinstance(payload, Mapping):
        raise ApprovalValidationError(
            "恢复值必须是映射", details={"received_type": type(payload).__name__}
        )
    if payload.get("type") != "approval_decisions":
        # `type` 是"这是哪种恢复值"的判别键（图里可能挂起多种等待），写错就直接拒绝，
        # 不做"猜你想要哪种"。
        raise ApprovalValidationError(
            '恢复值 type 必须是 "approval_decisions"', details={"type": payload.get("type")}
        )
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list):
        raise ApprovalValidationError(
            "恢复值 decisions 必须是列表", details={"type": type(raw_decisions).__name__}
        )
    decisions: list[Decision] = []
    for index, item in enumerate(raw_decisions):
        where = f"decisions[{index}]"
        if not isinstance(item, Mapping):
            raise ApprovalValidationError(f"{where} 必须是映射")
        action_id = item.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            # 空串也拒：`action_id` 是审批的身份键，空串会让 `by_id` 查找静默命中错误条目。
            raise ApprovalValidationError(f"{where}.action_id 必须是非空字符串")
        verdict_raw = item.get("verdict")
        try:
            verdict = Verdict(str(verdict_raw))
        except ValueError as exc:
            # 用枚举构造而不是 `if in (...)`：取值域只有一个来源（`app/enums.py`）。
            raise ApprovalValidationError(
                f"{where}.verdict 非法：{verdict_raw!r}（允许 approve/reject/edit）"
            ) from exc
        edited = item.get("edited_params", None)
        if verdict is Verdict.EDIT:
            if not isinstance(edited, dict) or not edited:
                raise ApprovalValidationError(f"{where}.edited_params 在 verdict=edit 时必须是非空映射")
        elif edited is not None:
            # 反方向也严格：非 edit 带了 edited_params 就是"以为改了、其实没改"，
            # 不做"多给就忽略"的宽容（§4.4 明确要求 null）。
            raise ApprovalValidationError(
                f"{where}.edited_params 在 verdict={verdict.value} 时必须为 null（不做宽容处理）"
            )
        operator = item.get("operator")
        if operator is not None and not isinstance(operator, str):
            raise ApprovalValidationError(f"{where}.operator 必须是字符串或 null")
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ApprovalValidationError(f"{where}.reason 必须是字符串或 null")
        decided_at = item.get("decided_at")
        if decided_at is not None and not isinstance(decided_at, str):
            # 只校验类型不校验格式：真正的权威时间由服务端盖（下面 `or utc_iso()`），
            # 客户端给的值只在"是字符串"时被沿用。
            raise ApprovalValidationError(f"{where}.decided_at 必须是 ISO-8601 字符串或 null")
        decisions.append(
            Decision(
                action_id=action_id,
                verdict=verdict,
                # 拷贝一份：`Decision` 会进 checkpoint，直接引用请求体里的可变 dict
                # 会让后续任何修改悄悄改到已落库的裁决。
                edited_params=dict(edited) if edited is not None else None,
                operator=operator,
                reason=reason,
                decided_at=decided_at or utc_iso(),
            )
        )
    return decisions


def validate_resume_payload(payload: Any, *, expected: Sequence[ActionProposal]) -> dict[str, Any]:
    """恢复值严格校验（§4.4 校验规则 1/2/3/4/5）。任一不成立 → `ApprovalValidationError`(400)。

    规则 2 是"zip 静默截断"的根治点（ADR-005）：`set(decisions.action_id)` 必须与
    `set(pending_actions.action_id)` **完全相等** —— 不许多、不许少、不许重复；
    不做任何按位置配对。

    保证：返回值是 `{"type": "approval_decisions", "decisions": [...]}`（可以直接喂给
        `Command(resume=...)`），且集合与 `expected` 逐 id 相等。
    不保证：不保证这些 action_id 在库里仍是 pending（那要查库，见 `approvals.assert_all_pending`）。
    调用方坑：它是**第二道闸**，`app/api/routes_approve.py` 已经校验过一次；绕过 API 直接
        调 `resume_agent` 时这里是唯一的把关点，别以为可以省。
    """
    decisions = _validate_decisions_shape(payload)
    expected_ids = [str(item["action_id"]) for item in expected]
    got_ids = [decision["action_id"] for decision in decisions]
    # 重复 id 先查：`set` 相等会掩盖"同一 action 批两次"（集合看起来一样，裁决却有两份）。
    duplicates = sorted({item for item in got_ids if got_ids.count(item) > 1})
    if duplicates:
        raise ApprovalValidationError(
            "decisions 里出现重复 action_id", details={"duplicates": duplicates}
        )
    # 缺与多分开报：前端要能一眼看出是"漏了一条"还是"多了一条失效的"。
    missing = [item for item in expected_ids if item not in set(got_ids)]
    extra = [item for item in got_ids if item not in set(expected_ids)]
    if missing or extra:
        raise ApprovalValidationError(
            "decisions 与待审批动作集合不一致（既不许多也不许少）",
            details={"missing": missing, "extra": extra, "expected": len(expected_ids)},
        )
    # 规则 4：verdict=edit 的参数必须能过该 action_type 的 schema（在进图之前就拦）。
    from app.services.actions import validate_params

    by_id = {str(item["action_id"]): item for item in expected}
    for decision in decisions:
        if decision["verdict"] is not Verdict.EDIT:
            # approve/reject 不带参数，没有 schema 可校验（形状规则已在
            # `_validate_decisions_shape` 里保证 edited_params 必须为 None）。
            continue
        proposal = by_id[decision["action_id"]]
        try:
            # 用**该动作类型自己的** schema 校验（改价看 price_cents、改库存看 stock）：
            # 在进图之前拦住，比让 `execute` 在上游调用前才炸要好得多（那时审批已落库）。
            validate_params(str(proposal["action_type"]), decision["edited_params"] or {})
        except ParamValidationError as exc:
            raise ApprovalValidationError(
                f"{decision['action_id']} 的 edited_params 不符合参数 schema：{exc}",
                details={"action_id": decision["action_id"]},
            ) from exc
    # 原样回一个"恢复值"形状的对象：`Command(resume=...)` 要的就是这一份，而不是 Decision 列表。
    return {"type": "approval_decisions", "decisions": decisions}


#: 契约 §4.4 代码块里的私有名字（保持逐字可对照）
_validate_resume_payload = validate_resume_payload


@traced("approval_gate")
async def approval_gate(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """人工审批闸门：落库 → 通知 → `interrupt()` → 校验恢复值 → 落裁决（§4.4）。

    ## 五步的顺序本身就是契约

    1. `approval_request` insert-if-absent（**绝不**回写 status）——重放必须先看到库里那一行；
    2. 通知（失败不阻断）——审批台是主通道，通知只是加速；
    3. 写 `awaiting_approval` 到 `agent_traces`——必须在 `interrupt()` **之前**，因为
       `interrupt()` 抛异常会丢弃节点返回值，那次状态迁移只能在之前直接写库；
    4. `interrupt(payload)` 挂起（本节点内唯一一次）；
    5. 恢复后重放：校验恢复值 → 落裁决 → 算 `idempotency_key`（落在**最终参数**上）。

    保证：挂起时 `pending_approvals` 一定已经在库里（审批台读得到）；恢复后返回的
        `approved_actions[].idempotency_key` 一定对应"人最终批准的那份参数"。
    不保证：不保证通知送达（失败只记一条 error）；也不保证恢复值合法——非法直接抛
        `ApprovalValidationError`(400)，图不会被推进。
    调用方坑：本节点会被**重放**（`interrupt` 恢复从节点开头重来），所以第 1、2 步必须
        幂等/可重复；第 3 步写库也是幂等的（同一个 status 覆盖同一个 status）。
    """
    from app.services import approvals

    if _aborted(state):
        return {}
    pending: list[ActionProposal] = list(state.get("pending_actions") or [])
    if not pending:
        # 防御性直通：拓扑上不会走到这里（route_after_propose_actions 已经分流），
        # 但"节点自己不制造等待"是硬约束，不能靠调用方保证。
        return {}

    runtime = resolve_runtime(config)
    now = resolve_now(config)
    already = state.get("approval") or {}
    # 重放时复用**原来那次** `requested_at`：审批人看到的"这条待办是什么时候来的"不该因为
    # 进程重启而变（重放会重新执行到这里）。
    requested_at = already.get("requested_at") if isinstance(already, Mapping) else None
    payload = {
        "type": "approval_request",
        "run_id": state["run_id"],
        "thread_id": state["thread_id"],
        "shop_id": state["shop_id"],
        "trace_id": state["trace_id"],
        "requested_at": requested_at or utc_iso(now),
        "proposals": pending,
    }

    # 1) 幂等落库（insert-if-absent；interrupt_id 先写 ""，由 resume_agent 回填）
    with session_scope() as session:
        await approvals.upsert_proposals(
            session=session,
            run_id=str(state["run_id"]),
            shop_id=str(state["shop_id"]),
            thread_id=str(state["thread_id"]),
            proposals=pending,
        )

    # 2) 通知（失败不阻断：审批台是主通道，§5.6）
    notification_error: str | None = None
    try:
        await runtime.notifier.send_approval_request(payload)
    except GraphBubbleUp:
        # 控制流类异常不得被"通知失败"的兜底吞掉（§4.3 规则 2：节点体内 except Exception
        # 必须显式排除 GraphInterrupt/GraphBubbleUp，唯一观察点是 traced()）。
        raise
    except Exception as exc:  # noqa: BLE001 - 通知是旁路，任何失败都不得阻断审批
        notification_error = f"{type(exc).__name__}: {exc}"

    # 3) 状态先落 awaiting_approval：interrupt 会抛异常，节点返回值会被丢弃，
    #    所以"挂起中的状态"只能在 interrupt **之前**直接写库（§4.2 状态迁移表）。
    await update_run_trace(
        shop_id=str(state["shop_id"]),
        run_id=str(state["run_id"]),
        status=RunStatus.AWAITING_APPROVAL.value,
        now=now,
    )

    # 4) 挂起（同一个 state 上唯一一次 interrupt）
    from langgraph.types import interrupt

    # `interrupt()` 抛 `GraphInterrupt`：本行之后的代码只在**恢复后重放**时才会执行。
    # 上面 1)~3) 因此在恢复时会再跑一遍，这就是它们必须幂等的原因。
    answer = interrupt(payload)

    # 5) 恢复后重放：校验 + 落裁决
    validated = validate_resume_payload(answer, expected=pending)
    decisions: list[Decision] = list(validated["decisions"])
    with session_scope() as session:
        await approvals.mark_decided(
            session=session, thread_id=str(state["thread_id"]), decisions=decisions
        )

    from app.services.actions import idempotency_key

    by_id = {str(item["action_id"]): item for item in pending}
    approved: list[ActionProposal] = []
    rejected: list[ActionProposal] = []
    for decision in decisions:
        proposal = by_id[decision["action_id"]]
        if decision["verdict"] is Verdict.REJECT:
            # 拒绝：只进 `rejected_actions`，不产生 `idempotency_key`（它永远不会被执行）。
            rejected.append(proposal)
            continue
        params = (
            # `edit`：以人改过的参数为准；`approve`：用提议时的原参数。
            dict(decision["edited_params"] or {})
            if decision["verdict"] is Verdict.EDIT
            else dict(proposal["params"])
        )
        effective = ActionProposal(
            action_id=proposal["action_id"],
            action_type=proposal["action_type"],
            target=proposal["target"],
            params=params,
            # §4.5：键落在"参数已经定稿"之后（改价 9900→9600 必须是两个不同的键）。
            idempotency_key=idempotency_key(
                shop_id=str(state["shop_id"]),
                action_type=str(proposal["action_type"]),
                target=str(proposal["target"]),
                params=params,
            ),
            reason=proposal["reason"],
            evidence=proposal["evidence"],
            risk=proposal["risk"],
            created_at=proposal["created_at"],
        )
        approved.append(effective)

    approval_state = {
        # 节点拿不到自己的 interrupt id（它由 LangGraph 生成）→ 只能留 None，
        # 由 `resume_agent` / `run_agent` 的 `backfill_interrupt_id` 事后回填。
        "interrupt_id": None,  # 由 resume_agent 回填（节点拿不到自己的 interrupt id）
        "requested_at": payload["requested_at"],
        "decided_at": utc_iso(now),
        # 只记第一条裁决的 operator：§3.3 只有一个 `decided_by` 字段，
        # 多人分歧时完整名单在 `decisions` 里（这里不编一个"多人"的假值）。
        "decided_by": decisions[0]["operator"] if decisions else None,
        "decisions": decisions,
    }
    errors: list[AgentError] = []
    if notification_error:
        errors.append(
            make_agent_error(
                node="approval_gate",
                category=ErrorCategory.UNKNOWN,
                message=f"审批通知未送达（不阻断）：{notification_error}",
                retryable=False,
            )
        )
    return {
        "approved_actions": approved,
        "rejected_actions": rejected,
        "approval": approval_state,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="approval_gate",
            event="approval.decided",
            data={
                "approved": len(approved),
                "rejected": len(rejected),
                "verdicts": [str(item["verdict"]) for item in decisions],
            },
        ),
        "status": RunStatus.EXECUTING.value,
    }


# ======================================================================================
# 6) execute
# ======================================================================================


def _dispatch(client: Any, proposal: Mapping[str, Any]) -> Awaitable[Any]:
    """派发到抖店的具体方法（只白名单三类动作）。

    保证：只可能返回三种结局——一个 awaitable（真下发）、或抛 `DoudianBusinessError`（类型不支持/
        未知类型）。绝不"静默什么都不做然后成功"。
    不保证：不保证参数已校验（那是 `validate_params` 的事）；这里只按动作类型取字段。
    调用方坑：返回的是**未 await 的 awaitable**（`client.xxx(...)` 调用本身不执行请求），
        所以调用方必须 `await`；异常也可能是从 `await` 那一刻才抛出来的，try 要包住 await。
    """
    action_type = ActionType(str(proposal["action_type"]))
    params = dict(proposal["params"])
    product_id, sku_id = _target_ids(proposal)
    if action_type is ActionType.PRICE_UPDATE:
        return client.edit_price(
            product_id=product_id, sku_id=sku_id, price_cents=int(params["price_cents"])
        )
    if action_type is ActionType.STOCK_UPDATE:
        return client.update_stock(
            product_id=product_id, sku_id=sku_id, stock=int(params["stock"])
        )
    if action_type is ActionType.COUPON_CREATE:
        # 契约里有这个动作类型，但上游方法没封装：显式失败比"假装成功"好——
        # 后者会写一条 succeeded 的 action_log，让人以为优惠券发出去了。
        raise DoudianBusinessError(
            "coupon_create 没有对应的抖店上游方法（DoudianClient 只封装了 editPrice/editStock，"
            "见 CONTRACT §5.1），本次不执行",
            category=ErrorCategory.CONFIG,
            retryable=False,
        )
    raise DoudianBusinessError(
        f"未知动作类型 {action_type!r}", category=ErrorCategory.CONFIG, retryable=False
    )


def _target_ids(proposal: Mapping[str, Any]) -> tuple[str, str]:
    """`proposal` → (product_id, sku_id)。

    上游 `editPrice`/`editStock` 要 `product_id` + `sku_id` 两个；而 §3.3 的 `target` 只有一个。
    两者都来自 `propose_actions` 记录的 `evidence.target_snapshot`（商品列表里的真实条目），
    缺 product_id 时才退回"target 兼任"。

    保证：永远返回两个字符串（不返回 None），因此调用方可以直接传给上游方法。
    不保证：不保证两个 id 都真的是上游的 id —— 快照缺失时 `target` 兼任，那是一个**降级**：
        上游会因为"product_id 不存在"报 business 错误，而不是在这里被拦住。
    调用方坑：`str(None)` 会得到 "None" 这种脏值，所以每一侧都显式判 `is not None`；
        不要为了"少一个分支"改成 `or target`（`""` 也会被误当成缺失）。
    """
    snapshot = (proposal.get("evidence") or {}).get("target_snapshot") or {}
    target = str(proposal["target"])
    product_id = snapshot.get("product_id")
    sku_id = snapshot.get("sku_id")
    return (
        # 两侧各自判 None：`str(None)` == "None" 会被当成一个真实 id 发给上游。
        str(product_id) if product_id is not None else target,
        str(sku_id) if sku_id is not None else target,
    )


@traced("execute")
async def execute(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """执行审批通过的动作（幂等由 `action_log.idempotency_key` 保证，§6.2）。

    ## 幂等的三条分支（每一条都对应一种真实事故）

    - `status ∈ {succeeded, skipped_duplicate}`：已经成功过 → **直接跳过**，记
      `skipped_duplicate`（重放/重试的第二次走这里）。
    - `status ∈ {pending, unknown}`：下发过但结果不确定 → **不重发**（ADR-011），
      记 `unknown` 并交给对账；`pending` 还会被就地提升成 `unknown`（"说不清"必须显式）。
    - 没有行：首次执行 → 先插 `pending` 再下发，拿到结果后写终态。

    保证：同一份 `idempotency_key` 最多真正向上游成功下发一次；每个提案都会在
        `execution_results` 里留下一条记录（哪怕是 skip/unknown）。
    不保证：不保证"下发成功"与"数据库写成功"的原子性——崩溃窗口里会出现 `pending`，
        那正是对账（§9.3）存在的理由，不是本节点能消灭的。
    调用方坑：`DoudianAuthError` 会**re-raise 中止整轮**（其余不可重试错误只记 `failed`
        继续下一个动作）；可重试错误也是 re-raise，交给节点级 RetryPolicy 重放整个节点，
        靠上面的幂等分支保证不会重复改价。
    """
    if _aborted(state):
        return {}
    approved: list[ActionProposal] = list(state.get("approved_actions") or [])
    if not approved:
        # 没有批准的动作（全部被拒 / 无提议）：仍然要显式写 `executing`，
        # 因为 §4.2 的状态迁移表里 execute 之后就是 executing。
        return {
            "execution_results": [],
            "audit": _audit(
                state, node="execute", event="action.executed", data={"count": 0}
            ),
            "status": RunStatus.EXECUTING.value,
        }

    from app.services.actions import idempotency_key

    runtime = resolve_runtime(config)
    client = resolve_client(config, runtime)
    now = resolve_now(config)
    results: list[ExecutionResult] = []
    errors: list[AgentError] = []
    shop_id = str(state["shop_id"])

    for proposal in approved:
        params = dict(proposal["params"])
        # 幂等键在**审批后的最终参数**上重算一次（`approval_gate` 也算过）：
        # 改价 9900→9600 必须是两个不同的键，用提议时的旧参数算会把两次改动混成一次。
        key = idempotency_key(
            shop_id=shop_id,
            action_type=str(proposal["action_type"]),
            target=str(proposal["target"]),
            params=params,
        )
        previous = await load_action_log(key)
        if previous is not None and previous["status"] in _NO_REEXECUTE_STATUSES:
            # 已经成功过（或上次就是重复跳过）：记一条 skip，不碰上游。
            # `attempt` 取上一份的原值，让"这是第几次尝试"在审计里连续。
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.SKIPPED_DUPLICATE.value,
                    upstream_request_id=previous.get("upstream_request_id"),
                    error=None,
                    error_category=None,
                    attempt=int(previous.get("attempt") or 1),
                    executed_at=utc_iso(now),
                )
            )
            continue
        if previous is not None and previous["status"] in {
            ActionLogStatus.PENDING.value,
            ActionLogStatus.UNKNOWN.value,
        }:
            # 已经下发过但没拿到确定结果（崩溃窗口 / ambiguous）：**不能重发**（ADR-011），
            # 交给对账任务（§9.3）；把 pending 也并进 unknown，让"说不清"显式可见。
            if previous["status"] == ActionLogStatus.PENDING.value:
                # 就地提升 pending→unknown：pending 的字面意思是"还没下发"，而事实是
                # "已经下发但结果丢了"——两者对账时的处置完全不同，必须纠正过来再往下走。
                await update_action_log(key, status=ActionLogStatus.UNKNOWN, now=now)
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.UNKNOWN.value,
                    upstream_request_id=previous.get("upstream_request_id"),
                    error=previous.get("error_message")
                    or "上次执行结果不确定（pending/unknown），不自动重发，等待对账",
                    error_category=ErrorCategory.AMBIGUOUS.value,
                    attempt=int(previous.get("attempt") or 1),
                    executed_at=utc_iso(now),
                )
            )
            continue

        attempt = 1 if previous is None else int(previous.get("attempt") or 1) + 1
        if previous is None:
            # 先插 `pending` 再下发：这一行是"我要动上游了"的唯一凭证。
            # 顺序不能反过来——下发成功却没插行，等于把一个真实的改价变成不可审计。
            await insert_action_log(
                idempotency_key=key,
                action_id=str(proposal["action_id"]),
                run_id=str(state["run_id"]),
                shop_id=shop_id,
                action_type=str(proposal["action_type"]),
                target=str(proposal["target"]),
                request_params=params,
                status=ActionLogStatus.PENDING,
                attempt=attempt,
                now=now,
            )
        try:
            response = await _dispatch(client, proposal)
        except DoudianAmbiguousOutcomeError as exc:
            # mutating 调用遇到超时/连接/5xx：**不知道上不上游生效**。记 unknown 且不重试，
            # 这正是 ADR-011 的场景（抖店没有服务端幂等键，重发等于赌"上次没生效"）。
            await update_action_log(
                key,
                status=ActionLogStatus.UNKNOWN,
                error_category=str(exc.category),
                error_message=str(exc),
                attempt=attempt,
                now=now,
            )
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.UNKNOWN.value,
                    upstream_request_id=None,
                    error=str(exc),
                    error_category=str(exc.category),
                    attempt=attempt,
                    executed_at=utc_iso(now),
                )
            )
        except DoudianAuthError as exc:
            # §9.1：鉴权失败（已强制刷过 token）→ 停止整个 run。先落库再抛，证据不丢。
            # 继续尝试下一个动作没有意义：token 没有权限，后面每一个都会一样失败。
            await update_action_log(
                key,
                status=ActionLogStatus.FAILED,
                error_category=str(exc.category),
                error_message=str(exc),
                attempt=attempt,
                now=now,
            )
            raise
        except DoudianError as exc:
            # 其余抖店错误（business / 4xx / signature）：记 failed 并**继续下一个动作**——
            # 一条动作失败不该让同一批里其他动作也不执行（它们各自独立）。
            await update_action_log(
                key,
                status=ActionLogStatus.FAILED,
                error_category=str(exc.category),
                error_message=str(exc),
                attempt=attempt,
                now=now,
            )
            errors.append(
                make_agent_error(
                    node="execute",
                    category=exc.category,
                    message=f"动作 {proposal['action_id']} 执行失败：{exc}",
                    retryable=bool(exc.retryable),
                )
            )
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.FAILED.value,
                    upstream_request_id=None,
                    error=str(exc),
                    error_category=str(exc.category),
                    attempt=attempt,
                    executed_at=utc_iso(now),
                )
            )
            if exc.retryable:
                # 交给节点级 RetryPolicy（§9.2 的预算）：重放时已成功的动作会变成
                # skipped_duplicate（幂等键挡住），不会重复改价。
                raise
        else:
            request_id = getattr(response, "request_id", None)
            await update_action_log(
                # 成功路径：先落库再构造返回值（顺序反了会让"返回了但没记账"成为可能）。
                key,
                # 只留可审计的最小摘要：上游 data 可能很大且不可 JSON 序列化，整块塞库会让
                # 一次执行写进几百 KB 并且丢掉"能查"的价值。
                status=ActionLogStatus.SUCCEEDED,
                response={
                    # `ok` 默认 True：上游没给这个字段时按成功处理（异常路径上面已经拦过了）。
                    "ok": bool(getattr(response, "ok", True)),
                    "request_id": str(request_id) if request_id else None,
                    # data 只留 16 位摘要：能判断"两次返回是否同一份"，但拿不到内容（脱敏）。
                    "data_digest": digest_of(getattr(response, "data", None)),
                },
                upstream_request_id=str(request_id) if request_id else None,
                attempt=attempt,
                now=now,
            )
            results.append(
                ExecutionResult(
                    action_id=str(proposal["action_id"]),
                    idempotency_key=key,
                    status=ActionLogStatus.SUCCEEDED.value,
                    upstream_request_id=str(request_id) if request_id else None,
                    error=None,
                    error_category=None,
                    attempt=attempt,
                    executed_at=utc_iso(now),
                )
            )

    return {
        "execution_results": results,
        "errors": _errors(state, errors),
        "audit": _audit(
            state,
            node="execute",
            event="action.executed",
            data={
                "count": len(results),
                "statuses": [item["status"] for item in results],
                "keys": [item["idempotency_key"] for item in results],
            },
        ),
        "status": RunStatus.EXECUTING.value,
    }


# ======================================================================================
# 7) record_effect（图内**不等待** T+N，ADR-003）
# ======================================================================================


@traced("record_effect")
async def record_effect(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """只登记 `effect_snapshot`（`status="scheduled"`）然后立刻返回；采样交给调度器。

    保证：执行过的每个动作都按 `settings.effects.windows_days`（T+0/1/3/7）登记窗口，
        登记是幂等的（`UNIQUE(action_log_id, offset_days)`）；`effect_report` 只描述
        "登记了哪些窗口"，不含任何因果结论。
    不保证：不保证效果被采样到了——那是调度器在 `due_at` 之后做的事（ADR-003：图内不等）。
    调用方坑：`effect_report=None`（没执行动作）与 `windows=[]`（登记了 0 个）是两件事；
        报告里的 `attribution` 固定 `single_arm_before_after`，没有对照组时 `caveats` 会
        同时挂"季节性/归因强度"两条，别在前端把它渲染成因果结论。
    """
    from app.services.effects import build_effect_report, register_effect_snapshots

    if _aborted(state):
        return {}
    results = list(state.get("execution_results") or [])
    if not results:
        # 一个动作都没执行（全被拒 / 全失败）：没有窗口可登记，`effect_report` 保持 None。
        return {
            "effect_report": None,
            "audit": _audit(
                state, node="record_effect", event="effect.registered", data={"windows": 0}
            ),
        }
    runtime = resolve_runtime(config)
    now = resolve_now(config)
    # 窗口天数来自配置（§8.2），不写死：与调度器的采样节奏必须一起调，写死两处必然走散。
    windows_days = [int(item) for item in runtime.settings.effects.windows_days]
    with session_scope() as session:
        windows = await register_effect_snapshots(
            session=session,
            run_id=str(state["run_id"]),
            shop_id=str(state["shop_id"]),
            results=results,
            windows_days=windows_days,
            now=now,
        )
    report = build_effect_report(
        run_id=str(state["run_id"]),
        windows=windows,
        # 单臂前后对比：本服务没有 A/B 分流能力，把归因强度如实写进报告，
        # 而不是让读报告的人以为这是因果结论。
        attribution=Attribution.SINGLE_ARM_BEFORE_AFTER,
        now=now,
    )
    return {
        "effect_report": report,
        "audit": _audit(
            state,
            node="record_effect",
            event="effect.registered",
            data={
                "windows": len(windows),
                "offsets": [window["offset_days"] for window in windows],
                "statuses": sorted({window["status"] for window in windows}),
            },
            at=str(report["generated_at"]),
        ),
    }


# ======================================================================================
# 8) finalize
# ======================================================================================


@traced("finalize")
async def finalize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """收口：定终态 + 用 JSONL 校准步数（§4.7 不变量）+ 落 `finished_at`。

    保证：无论从哪条边进来（采集失败 / 无提议直通 / 执行完），终态只有 `done` / `failed`
        两种；返回前 `agent_traces` 的 `step_count` 与 JSONL 的 `node.trace` 行数已对齐。
    不保证：不保证 `finalize` 自己也被计入——它的那条 `node.trace` 由 `traced()` 包装器写，
        **晚于**本函数体内的校准（所以校准值会比最终行数少 1，这是有意的：校准只对账
        "已经发生的"）。也不保证没有 errors：有 errors 但没 `failed` 的 run 也是 `done`。
    调用方坑：本节点是**所有**分支的汇合点，因此它不能假设 `metrics`/`execution_results`
        存在；`error` 只取最后一条错误的 message（完整列表在 `state.errors`）。
    """
    from app.agent.adapters import require_log_sink

    now = resolve_now(config)
    # 终态判定只看 `status`：`_aborted` 的语义就是"前面的节点已经判死"。
    status = RunStatus.FAILED.value if _aborted(state) else RunStatus.DONE.value
    errors = list(state.get("errors") or [])
    sink = require_log_sink()
    with session_scope() as session:
        # 以 JSONL 为准校准（只增不减）：崩溃窗口里"日志写了、DB 没 +1"的情况靠它修回来。
        calibrated = await calibrate_run_steps(
            session=session, run_id=str(state["run_id"]), sink=sink
        )
    await update_run_trace(
        shop_id=str(state["shop_id"]),
        run_id=str(state["run_id"]),
        status=status,
        # `finished=True` 与 `status` 必须同一次写：分开写会让中间态（failed 但没 finished_at）
        # 被启动对账当成"非终态"再捞一遍。
        finished=True,
        # `error` 只在**失败**时写：成功的 run 留一条错误文本会让看板把它当成有问题的 run。
        error=errors[-1]["message"] if errors and status == RunStatus.FAILED.value else None,
        now=now,
    )
    return {
        "status": status,
        "audit": _audit(
            state,
            node="finalize",
            event="run.finished",
            data={
                "status": status,
                "steps": calibrated,
                "errors": len(errors),
                "executed": len(state.get("execution_results") or []),
            },
            at=utc_iso(now),
        ),
    }
