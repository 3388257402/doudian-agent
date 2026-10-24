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

__all__ = [
    "NotificationResult",
    "Notifier",
    "DryRunNotifier",
    "FeishuWebhookNotifier",
    "build_notifier",
    "payload_digest",
]

_DIGEST_KEYS = ("shop_id", "action_type", "target", "params")


def payload_digest(approval: Mapping[str, Any]) -> str:
    """`sha256(canonical_json({shop_id, action_type, target, params}))`（§4.5，64 hex）。"""
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
    delivered: bool
    mode: str
    channel: str
    detail: str
    payload_digest: str


class Notifier(Protocol):
    mode: str

    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult: ...


class DryRunNotifier:
    """本地 dry-run：只写 trace，不外发。零凭据、零网络。"""

    mode = "dry_run"

    def __init__(self, *, logger: logging.Logger, sink: JsonlLogSink) -> None:
        self._logger = logger
        self._sink = sink

    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult:
        digest = payload_digest(approval)
        try:
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


class FeishuWebhookNotifier:
    """飞书群机器人 webhook（interactive card）。失败只降级为 `delivered=False`。"""

    mode = "feishu"

    def __init__(
        self,
        *,
        webhook_url: str,
        http_client: httpx.AsyncClient,
        logger: logging.Logger,
    ) -> None:
        if not webhook_url:
            raise ValueError("webhook_url 不能为空")
        self._webhook_url = webhook_url
        self._http = http_client
        self._logger = logger

    async def send_approval_request(self, approval: Mapping[str, Any]) -> NotificationResult:
        digest = payload_digest(approval)
        payload = {"msg_type": "interactive", "card": _build_card(approval, digest)}
        try:
            response = await self._http.post(self._webhook_url, json=payload)
        except Exception as exc:  # noqa: BLE001 - 通知失败绝不阻断审批
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
            return self._failed(
                approval,
                digest,
                detail=f"HTTP {response.status_code}",
                error_type="HttpStatusError",
                error_message=f"status={response.status_code}",
            )
        code = _feishu_code(body)
        if code not in (None, 0):
            return self._failed(
                approval,
                digest,
                detail=f"飞书返回 code={code} msg={_feishu_message(body)}",
                error_type="FeishuApiError",
                error_message=f"code={code}",
            )
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
    """
    action_type = str(approval.get("action_type") or "")
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
        elements.append(
            {"tag": "div", "text": {"tag": "plain_text", "content": f"原因：{reason}"}}
        )
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"payload_digest: {digest}"}],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"抖店审批请求 · {action_type}"},
        },
        "elements": elements,
    }


def _feishu_code(body: Any) -> int | None:
    if not isinstance(body, Mapping):
        return None
    for key in ("code", "StatusCode", "status_code"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _feishu_message(body: Any) -> str:
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
