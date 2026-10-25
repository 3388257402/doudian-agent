"""审批通知（CONTRACT §5.6）。

三条硬约束：

1. **通知不是主通道**。审批台（REST + 前端）才是。通知失败**不得**阻断审批流程，
   所以 `send_approval_request` 永不抛异常。
2. **不吞异常类型**：失败必须把 `type(exc).__name__` 记进 trace 与 `detail`。
   只写一句 "failed" 等于把"网络超时"和"卡片格式非法"混成同一件事，事后无法定位。
3. **无凭据可跑**：`DryRunNotifier` 只写一条 JSONL（含 `payload_digest`），
   `delivered=False`。把"没发出去"如实报成 False，而不是假装成功。

`payload_digest` 与 `approval_request.payload_digest` 用**同一个公式**（§4.5），
所以"通知里说的那条提议"和"库里那条审批行"可以用摘要对齐。

dry-run 与真实发送为什么**走同一套调用形状**（同一个 `Notifier` 协议、同一个
`send_approval_request(approval)` 入参、同一个 `NotificationResult` 返回）：
调用点只有一处、断言只有一套，`mode=dry_run` 换的只是"实现"，不是"路径"。如果 dry-run 走
一个 `if dry_run: log(...); return` 的旁路，那么摘要计算、必填字段检查、失败分支就永远只在
生产路径上被执行——而那些正是最容易随重构坏掉的部分。

失败**吞在哪一层**：吞在 `DryRunNotifier.send_approval_request` / `FeishuWebhookNotifier` 的
`try`（以及 `_failed` 里），**不外抛**，只返回 `delivered=False` + `detail=<异常类型>: <消息>`
+ 一条 `notifier.*_failed` 的 warn 日志。所以：

- 谁负责告警：`detail`（进 `RunResult`/trace）与那条 warn 日志，两者都带 `error_type`；
- 谁**不**负责：本模块不重试、不排队、不落库。审批台是主通道，通知只是"顺手提醒"；
- 代价（必须承认）：`delivered=False` 是一个**返回值而不是异常**，调用方如果忘了看它，
  通知失败就是静默的。所以本模块把 `detail` 写得足够可诊断，而"要不要因为通知失败报警"
  这个决定留在节点层。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from app.observability.logging import JsonlLogSink, get_logger

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.config import Settings

# --------------------------------------------------------------------------------------
# 三个"必须同时成立"的硬约束（改任一实现前逐条核对；违反其中一条都会让事故活到生产）
# --------------------------------------------------------------------------------------
# 1. **不抛**：`send_approval_request` 的任何路径都只能返回 `NotificationResult`。
#    调用点虽然有 try/except 兜底，但那是第二道防线——本模块自己抛异常意味着"通知失败"
#    会以异常形式出现在 trace 里，掩盖掉真正的原因（异常类型来自我们的代码，不是上游）。
# 2. **不谎报**：dry-run 恒 `delivered=False`；真实发送只有"HTTP 2xx 且 code 为 0/缺省"才
#    `delivered=True`。宁可报"没发出去"，也不要让一次失败看起来像成功。
# 3. **不吞类型**：`detail` 与日志里必须带 `type(exc).__name__` / 上游 code。一句 "failed"
#    会把"DNS 挂了""webhook 被删了""卡片格式非法"混成同一件事，事后无法定位。
#
# 附带的设计后果：本模块**没有**重试、队列、落库。要补必须在节点层做（并且明确它是否会
# 影响 run 状态），不要在这里加——那会让"通知"变成第二个有状态的子系统。
# --------------------------------------------------------------------------------------
__all__ = [
    "NotificationResult",
    "Notifier",
    "DryRunNotifier",
    "FeishuWebhookNotifier",
    "build_notifier",
    "payload_digest",
]

# --------------------------------------------------------------------------------------
# 谁在什么时候调用本模块（读代码时先建立这个地图）
# --------------------------------------------------------------------------------------
#   `nodes.approval_gate` 落完审批行（`approvals.upsert_proposals`）之后、`interrupt()`
#   **之前**调一次 → `notifier.send_approval_request(approval)`
#     → 节点自己还包了一层 `try/except`（显式放行 `GraphBubbleUp`），把异常文本记成
#       `notification_error`；**不**看 `delivered`、**不**因此改变 run 状态
#
# 所以有三条不变量，改动前先确认它们还成立：
# 1. 通知失败不能让 run 变成 failed —— 节点已经兜住了异常，"本模块不抛"是双保险；
# 2. 通知不是"审批台出现这条待办"的前提 —— 审批行在调用之前就已经落库了；
# 3. `delivered=False` **只有**通过返回值/trace 才能被看见（日志里还有一条 warn）。
# 一旦某天有人在上层加 `if not result.delivered: raise`，就等于让"飞书挂了"变成
# "审批不可用"——那与本模块的整个设计前提相反。
#
# 已知代价（不是 bug，别去"修"）：`interrupt()` 恢复会**从节点开头重放** `approval_gate`，
# 而审批行的 upsert 是 insert-if-absent（不会重复），通知却是**无条件再发一次**。所以群里
# 可能看到同一条待办的两张卡。要消掉它得给通知加"已通知过"的持久化标记——那会引入状态，
# 而当前取舍是"宁可重复提醒，也不要漏提醒"。
# --------------------------------------------------------------------------------------
_DIGEST_KEYS = ("shop_id", "action_type", "target", "params")


# 这里有一份**自己的** `payload_digest`，而不是 import `actions.payload_digest`：
#   - 两者的入参形状不同（这里是审批行的 Mapping/`PendingApproval`，那边是四个具名参数）；
#   - 公式必须**逐字节相同**（§4.5），所以两边都写了完整的键集合与 json 参数；
#   - 不 import 是为了不让"通知"这条旁路依赖 services 里的动作模块（通知不该因为 actions
#     改动而被迫重测）。代价是"改公式要改两处"——所以文件开头的 docstring 把它写成硬约束。
def payload_digest(approval: Mapping[str, Any]) -> str:
    """`sha256(canonical_json({shop_id, action_type, target, params}))`（§4.5，64 hex）。

    保证：与 `actions.payload_digest` **逐字节相同**（同一组键、同样的
    `sort_keys`/`ensure_ascii=False`/紧凑分隔符），所以通知卡片上的 digest 可以直接和
    `approval_request.payload_digest` 比对。改这里必须同步改那边，否则审批台会出现
    "两个摘要对不上"的假不一致。
    不保证：入参里多余字段会被忽略——`_DIGEST_KEYS` 之外的键**不参与**摘要（这正是
    "证据字段不改变身份"的实现方式）。
    坑：缺键抛 `KeyError`（而不是把缺席当 null 参与哈希）：把 `target` 缺失哈希成 null，
    会让两条不同的提议撞同一个摘要，那比报错危险得多。
    """
    missing = [key for key in _DIGEST_KEYS if key not in approval]
    if missing:
        raise KeyError(f"approval 缺少字段 {missing}，无法计算 payload_digest")
    canonical = json.dumps(
        {key: approval[key] for key in _DIGEST_KEYS},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NotificationResult:
    """一次通知尝试的结果（**不是**审批状态，别拿它推进流程）。

    `delivered` 是"外发成功没有"，`mode`/`channel` 是"走的哪条实现"，`detail` 是给人看的原因
    （失败时以 `异常类型: 消息` 开头），`payload_digest` 用来和审批行对齐。
    `delivered=False` 恒表示"没发出去"，包括 dry-run 的"按设计不发"与真失败的"没发成"——
    两者靠 `mode` 区分（`dry_run` vs `feishu`），不要只看 `delivered`。

    `mode` 与 `channel` 现在是同一个值，为什么还留两个：`mode` 是**配置选出来的实现**
    （`settings.notifier.mode`），`channel` 是**这次投递实际走的通道名**——将来一个实现发多个
    通道（例如 feishu + 站内信）时，`mode` 不变而 `channel` 变，消费方按 `channel` 统计才对。
    """

    delivered: bool
    mode: str
    channel: str
    detail: str
    payload_digest: str


class Notifier(Protocol):
    """通知器协议：`mode` 是给人/配置看的路由标签，`send_approval_request` 是唯一入口。

    保证：`send_approval_request` **永不抛异常**（实现方必须自己吞），返回值恒为
    `NotificationResult`。
    坑：新增实现时必须保持"不抛"这条 —— 调用点在节点里没有 try，一抛就会把整轮 run 打成
    failed，而通知失败本来不该影响审批。
    """

    mode: str

    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult: ...


# dry-run 也写 `payload_digest`，不是"顺手"：它让 dry-run 的 JSONL 能和审批行、以及真实发送
# 的卡片摘要三处对齐。否则一旦有人问"那次 dry-run 跑的是哪条提议"，只能靠 action_id 猜
# （而 action_id 在参数被 edit 之后就不唯一了）。
class DryRunNotifier:
    """本地 dry-run：只写 trace，不外发。零凭据、零网络。

    保证：① 每次调用都写一条 `notifier.dry_run` JSONL（含 `payload_digest`），所以
    "零凭据跑一遍"就能看到"如果有 webhook，会发出什么"；② 永不外发、永不抛。
    不保证：`delivered=True` —— 它恒为 False（"没发出去"就如实报 False，不假装成功）。
    坑：dry-run 与真实发送的**唯一**差别是"写 JSONL"vs"POST webhook"。所以别在这条分支里
    加"生产才需要"的字段校验——那些校验应该对两条路径都生效。
    """

    # `mode` 是**类属性**（不是实例属性）：配置/日志/测试都按"实现类"判断模式，不需要先
    # 建实例；`NotificationResult.mode` 再从它取值，两处永远一致。
    mode = "dry_run"

    def __init__(self, *, logger: logging.Logger, sink: JsonlLogSink) -> None:
        """注入 logger 与 sink（不自己建）：测试可以把 sink 指到 tmp 目录、把 logger 指到
        caplog，而不需要动全局单例。参数是 keyword-only 的，避免两个位置参数传反。
        """
        self._logger = logger
        self._sink = sink

    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult:
        """写一条 dry-run 事件；**落盘失败也不外抛**（只降级为 `delivered=False`）。

        保证：无论 sink 是否成功，都返回一个 `NotificationResult`（`detail` 里带失败原因）。
        不保证：sink 一定落盘 —— 磁盘满/权限问题时 `delivered=False` + warn 日志，审批照常。
        坑：写盘异常被吞在这里（`noqa: BLE001`），所以"dry-run 从不报错"不能当作"通知链路
        正常"的证据；要确认链路，看 JSONL 里有没有 `notifier.dry_run` 行。
        """
        digest = payload_digest(approval)
        try:
            # `write` 的参数是**白名单字段**（action_id/type/target/risk/digest）：故意不带
            # `params`/`reason`，它们是用户可控文本，dry-run 的 trace 里没必要留全量副本。
            self._sink.write(
                "notifier.dry_run",
                message="dry-run：审批通知未外发（审批台是主通道）",
                data={
                    "action_id": approval.get("action_id"),
                    "action_type": approval.get("action_type"),
                    "target": approval.get("target"),
                    "risk": approval.get("risk"),
                    "payload_digest": digest,
                },
                run_id=approval.get("run_id"),
                shop_id=approval.get("shop_id"),
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败绝不阻断审批流程
            # 落盘失败（磁盘满/权限）也是"通知没发出去"，不抛、不谎报成功，
            # 但必须把异常类型记下来，否则"磁盘满"和"卡片非法"看起来一样。
            detail = f"{type(exc).__name__}: {exc}"
            self._logger.warning(
                "dry-run 通知落 trace 失败（不阻断审批）: %s",
                detail,
                # 日志里带 `error_type` 与 `error_message` 两个字段（而不是只有一段字符串）：
                # 日志页要能按错误类型聚合，字符串无法聚合。
                extra={
                    "event": "notifier.dry_run_failed",
                    "run_id": approval.get("run_id"),
                    "shop_id": approval.get("shop_id"),
                    "data": {
                        "action_id": approval.get("action_id"),
                        "payload_digest": digest,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                },
            )
            return NotificationResult(
                delivered=False,
                mode=self.mode,
                channel="dry_run",
                detail=detail,
                payload_digest=digest,
            )
        # 一次通知 = 一条事件（`notifier.dry_run`，由 sink 写）。这里**不再**补一条
        # `logging.info`：当 JsonlHandler 接到同一个 sink 时，那会让一次通知产生两条行，
        # 前端"一行 = 一次事件"的计数断言全都要跟着打补丁（reviewer F4）。
        return NotificationResult(
            delivered=False,
            mode=self.mode,
            channel="dry_run",
            detail="dry-run 模式：通知未外发（审批台是主通道）",
            payload_digest=digest,
        )


# 卡片里**不放** `evidence`（指标快照 + 目标快照），原因有两个：① 飞书卡片有长度限制，
# 证据里嵌套 dict 很容易超；② 证据是"决策依据"，审批台上要能展开看，群消息里刷屏反而
# 让人不去点审批台。所以卡片只给"是什么/对谁/多危险/哪次 run"，证据留在库里。
class FeishuWebhookNotifier:
    """飞书群机器人 webhook（interactive card）。失败只降级为 `delivered=False`。

    保证：① 成功判定要**两个条件**同时成立——HTTP 2xx **且** body `code` 为 0 或缺省；
    ② 任何失败路径都返回 `NotificationResult(delivered=False, detail="<类型>: <原文>")` 且写
    一条 `notifier.feishu_failed`，绝不外抛。
    不保证：消息真的"被人看到"（群机器人可能被移出群、用户免打扰），也不重试。
    坑：① `http_client` 是**外部注入**的（lifespan 里那个共享 AsyncClient），本类不负责关闭，
    也不该在失败时 `aclose()` 它；② webhook 里的 token 属于凭据，所以 URL **不**进日志
    （日志只打 action_id/digest/错误类型）；③ 飞书对同一机器人有频率限制，超限时**可能仍返回
    HTTP 200 + 非 0 code**（这正是下面"双条件判定"存在的原因），所以"状态码 2xx"不等于"发出去了"。
    """

    mode = "feishu"

    def __init__(
        self,
        *,
        webhook_url: str,
        http_client: httpx.AsyncClient,
        logger: logging.Logger,
    ) -> None:
        """注入 webhook URL 与**共享**的 `httpx.AsyncClient`。

        `http_client` 不归本类所有（关闭责任在装配方）：为一条 webhook 单独建 client 会让
        每次通知都重新握手，而通知本来就不该有自己的连接池。
        """
        if not webhook_url:
            # 构造期就炸：空 URL 会在第一次 post 时抛一个 httpx 的 InvalidURL，
            # 那时报错信息里只有 URL 为空，看不出是"配置没读到"还是"代码传错"。
            raise ValueError("webhook_url 不能为空")
        self._webhook_url = webhook_url
        self._http = http_client
        self._logger = logger

    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult:
        """发一张审批卡片；失败返回 `delivered=False`（不抛）。

        保证：`payload_digest` 在**发之前**算好并写进卡片 note——所以卡片本身就能回答"它说的
        是哪条审批行"，即使发送失败也能从返回值里拿到同一个摘要。
        不保证：卡片送达。网络错误/HTTP 非 2xx/业务 code 非 0 都只降级。
        坑：`response.json()` 的解析失败被容忍（`body=None`），随后仍按 status 判定——飞书
        偶尔返回非 JSON 的错误页，不能因为"解析不了"就把它当成成功。
        """
        digest = payload_digest(approval)
        payload = {"msg_type": "interactive", "card": _build_card(approval, digest)}
        try:
            response = await self._http.post(self._webhook_url, json=payload)
        except Exception as exc:  # noqa: BLE001 - 通知失败绝不阻断审批
            # 网络层（超时/DNS/连接重置）与配置层（URL 非法）都会落到这里；用
            # `type(exc).__name__` 区分它们，而不是统一写 "failed"。
            return self._failed(
                approval,
                digest,
                detail=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code < 200 or response.status_code >= 300:
            # 3xx 也算失败：AsyncClient 默认不跟随重定向，而 webhook 的重定向几乎总是
            # "URL 配错了"，跟着走只会把内容发到一个未知地址。
            return self._failed(
                approval,
                digest,
                detail=f"HTTP {response.status_code}",
                error_type="HttpStatusError",
                error_message=f"status={response.status_code}",
            )
        code = _feishu_code(body)
        if code not in (None, 0):
            # `None`（body 里没有 code 字段）被当成成功：老版 webhook 的 200 响应就是
            # `{"code":0,...}`，个别网关会省略；只有**明确非 0** 才算失败，避免把"没有 code
            # 字段"误判成失败从而产生一堆假告警。
            return self._failed(
                approval,
                digest,
                detail=f"飞书返回 code={code} msg={_feishu_message(body)}",
                error_type="FeishuApiError",
                error_message=f"code={code}",
            )
        # 成功才写 info 日志：失败路径由 `_failed` 的 warn 负责，两条路径各写一条，不重复。
        self._logger.info(
            "飞书审批通知已发送: %s",
            approval.get("action_id"),
            extra={
                "event": "notifier.feishu_sent",
                "data": {"action_id": approval.get("action_id"), "payload_digest": digest},
            },
        )
        return NotificationResult(
            delivered=True,
            mode=self.mode,
            channel="feishu",
            detail="已发送",
            payload_digest=digest,
        )

    def _failed(
        self,
        approval: Mapping[str, Any],
        digest: str,
        *,
        detail: str,
        error_type: str,
        error_message: str,
    ) -> NotificationResult:
        """统一的失败出口：写一条 warn 日志 + 返回 `delivered=False`。

        保证：所有失败分支都走这里，所以日志事件名、字段集合、`detail` 的前缀格式完全一致
        ——日志页按 `notifier.feishu_failed` 过滤就能拿到全部失败，不需要记三种事件名。
        不保证：调用方一定会看 `delivered`。这是本模块最脆的一环：失败不抛异常，所以"通知
        没发出去"只能靠调用方读返回值（或靠人看日志）。
        坑：`detail` 以 `error_type` 开头是**契约**（`f"{error_type}: {detail}"`），别去掉——
        调用方/前端按这个前缀区分失败类型。
        """
        self._logger.warning(
            "飞书审批通知失败（不阻断审批）: %s",
            detail,
            extra={
                "event": "notifier.feishu_failed",
                "run_id": approval.get("run_id"),
                "shop_id": approval.get("shop_id"),
                "data": {
                    "action_id": approval.get("action_id"),
                    "payload_digest": digest,
                    "error_type": error_type,
                    "error_message": error_message,
                },
            },
        )
        return NotificationResult(
            delivered=False,
            mode=self.mode,
            channel="feishu",
            # `detail` 里带异常类型：调用方（节点/日志）不需要再翻日志才能知道是哪种失败。
            detail=f"{error_type}: {detail}",
            payload_digest=digest,
        )


def _build_card(approval: Mapping[str, Any], digest: str) -> dict[str, Any]:
    """审批卡片。**声明与实现一致**：卡片里每一处文本都是 `plain_text`。

    为什么连标签（`action_type` 这种我们自己的常量）也走 `plain_text`：`fields` 里
    label 与 value 拼在同一个 text 对象里，只要有一格是 `lark_md`，用户可控的
    `target`/`params`/`reason` 就会按 markdown 渲染（`[点我](http://evil)`、
    `**` 截断等），卡片语义可被提议内容改变。整卡 `plain_text` 是唯一不需要
    "论证某几个字段恰好不可注入"就能成立的写法（reviewer F3）。

    另外两条也是刻意的：① 卡片里**没有**审批按钮——交互回调需要一套验签 + 幂等端点，
    而审批台（REST）才是主通道，加按钮等于开第二条写路径；② 卡片里的字段是**挑选过**的
    （不含 `evidence`），证据留在库里给审批台展开，卡片只承载"要不要点进去看"的信息。
    """
    action_type = str(approval.get("action_type") or "")
    # 字段顺序 = 人看卡片时的阅读顺序（是什么动作 → 对谁 → 多危险 → 哪次 run/哪个店 → 参数）。
    # `params` 用 `json.dumps(sort_keys=True)` 而不是 `repr`：dict 的 repr 在不同 Python 版本
    # 上引号/顺序都可能变，而排序后的 JSON 是稳定的、且能直接贴进工单。
    fields = [
        ("action_type", action_type),
        ("target", str(approval.get("target") or "")),
        ("risk", str(approval.get("risk") or "")),
        ("run_id", str(approval.get("run_id") or "")),
        ("shop_id", str(approval.get("shop_id") or "")),
        ("params", json.dumps(approval.get("params") or {}, ensure_ascii=False, sort_keys=True)),
    ]
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {"tag": "plain_text", "content": f"{name}: {value}"},
                }
                for name, value in fields
            ],
        }
    ]
    reason = approval.get("reason")
    if reason:
        # `if reason:` 而不是总加一个空 div：空"原因"占位会让卡片看起来像少了一行。
        # 这条是**唯一**一段自由文本进卡片：`reason` 里带规则的 `description` 与指标值，
        # 也就是运营要看的那句"为什么弹了这张卡"。它同样走 `plain_text`（见 docstring）。
        elements.append(
            {"tag": "div", "text": {"tag": "plain_text", "content": f"原因：{reason}"}}
        )
    elements.append(
        {
            "tag": "note",
            # digest 放在 note 里（字号小、置底），是"给人对账用"的信息，不是给人读的主内容。
            # 它的价值场景：群里两个人同时看到两张卡，凭 action_id 分不清"是不是同一条"，
            # 而 digest 会把"参数被改过"的两条区分开。
            "elements": [{"tag": "plain_text", "content": f"payload_digest: {digest}"}],
        }
    )
    return {
        # wide_screen_mode：6 个字段两列排布，窄屏会被挤成一行一个字。
        "config": {"wide_screen_mode": True},
        "header": {
            # 颜色固定是 `orange`（"待处理"），**不**按 risk 变色：risk 已经有自己的文本字段，
            # 再维护一套 risk→颜色的映射，等于把同一份判断放在两处（卡片和前端）各自演化。
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"抖店审批请求 · {action_type}"},
        },
        "elements": elements,
    }


def _feishu_code(body: Any) -> int | None:
    """从 webhook 响应体里取业务返回码；取不到/不是 int 返回 `None`（= 不知道，按成功处理）。

    三个键名都要试（`code` / `StatusCode` / `status_code`）：飞书 webhook 的不同版本/网关
    用不同的键，只认一个会把"成功"读成"没有 code"→ 已按成功处理（无害），但把失败读成
    "没有 code"（有害）。所以宁可多试。
    坑：`isinstance(value, int) and not isinstance(value, bool)` —— `True` 是 int，不排除
    会把 `{"code": true}` 读成 1 然后当成业务错误。
    """
    if not isinstance(body, Mapping):
        return None
    for key in ("code", "StatusCode", "status_code"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _feishu_message(body: Any) -> str:
    """取 webhook 响应里的可读消息（`msg` / `message` / `StatusMessage`），取不到返回空串。

    只用于让失败原因更可读（拼进 `detail`），**不**参与成功/失败判定——判定只看
    `_feishu_code` 与 HTTP 状态码。
    """
    if not isinstance(body, Mapping):
        return ""
    for key in ("msg", "message", "StatusMessage"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return ""


def build_notifier(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient,
    sink: JsonlLogSink,
) -> Notifier:
    """按 `settings.notifier.mode` 选择实现。

    `mode=feishu` 但 webhook 环境变量缺失 → 启动期 `ConfigError`：
    静默降级成 dry_run 会让"以为有告警、其实没有"这种事故活到生产。

    保证：返回的对象满足 `Notifier` 协议（`mode` + 不抛的 `send_approval_request`），调用方
    只依赖协议、不依赖具体类。
    不保证：webhook 本身可用（URL 有效但群被解散/机器人被移出，只有真发一次才知道）。
    坑：① `mode` 是**白名单**判定，未知值走最后的 `ConfigError`（不是默认 dry_run）——
    配置写错 `feishuu` 必须启动失败，否则整条告警链路静默失效；
    ② `http_client` 与 `sink` 都由调用方注入且**不归本函数所有**，这里不创建也不关闭它们；
    ③ `logger` 在这里现取（`get_logger`），测试要断言日志时得从同一个 logger 名字上抓。
    """
    from app.errors import ConfigError  # 延迟 import：services 不依赖 app 层模块级导入

    logger = get_logger("app.services.notifier")
    mode = settings.notifier.mode
    if mode == "dry_run":
        return DryRunNotifier(logger=logger, sink=sink)
    if mode == "feishu":
        env_name = settings.notifier.feishu_webhook_env
        webhook_url = os.environ.get(env_name, "")
        if not webhook_url:
            # 空串与"没设置"同义：`os.environ.get` 的默认值就是空串，所以这里不判 None。
            raise ConfigError(
                f"notifier.mode=feishu 但环境变量 {env_name} 未设置"
                "（不静默降级成 dry_run）"
            )
        return FeishuWebhookNotifier(
            webhook_url=webhook_url,
            http_client=http_client,
            logger=logger,
        )
    raise ConfigError(f"未知 notifier.mode: {mode!r}")
