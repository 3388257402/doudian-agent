"""通知（CONTRACT §5.6）：dry-run 落日志、失败不阻断但必须留类型。

判据：
1. `payload_digest` 与 §4.5 的公式必须能**独立复算**出来（不是照抄实现自己的函数输出）。
2. 通知失败**绝不**抛异常打断审批流程，但 `detail`/日志里必须带 `type(exc).__name__`
   —— 否则"网络超时"和"卡片格式非法"会混成同一句话。
3. `notifier.mode=feishu` 但凭据缺失 → 启动期 `ConfigError`，不静默降级成 dry_run。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.config import build_settings
from app.errors import ConfigError
from app.observability.logging import JsonlLogSink
from app.services.notifier import (
    DryRunNotifier,
    FeishuWebhookNotifier,
    NotificationResult,
    build_notifier,
    payload_digest,
)

APPROVAL = {
    "run_id": "run_20261023_abc",
    "shop_id": "12345678",
    "action_id": "act_0001",
    "action_type": "price_update",
    "target": "sku:3400000000000000002",
    "params": {"price_cents": 9600},
    "risk": "medium",
    "reason": "GMV 低于近 7 日均值 70%",
}


def _sink(tmp_path: Path) -> JsonlLogSink:
    return JsonlLogSink(tmp_path / "logs" / "app.jsonl")


def _independent_digest(approval: dict) -> str:
    """§4.5：sha256(canonical_json({shop_id, action_type, target, params}))。"""
    canonical = json.dumps(
        {key: approval[key] for key in ("shop_id", "action_type", "target", "params")},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_payload_digest_matches_independent_recomputation() -> None:
    digest = payload_digest(APPROVAL)
    assert digest == _independent_digest(APPROVAL)
    assert len(digest) == 64
    # 键序无关、内容敏感
    reordered = {key: APPROVAL[key] for key in reversed(list(APPROVAL))}
    assert payload_digest(reordered) == digest
    assert payload_digest({**APPROVAL, "params": {"price_cents": 9601}}) != digest


def test_payload_digest_refuses_malformed_approval() -> None:
    broken = dict(APPROVAL)
    del broken["params"]
    with pytest.raises(KeyError) as excinfo:
        payload_digest(broken)
    assert "params" in str(excinfo.value)


async def test_dry_run_writes_jsonl_and_reports_not_delivered(tmp_path: Path) -> None:
    import logging

    sink = _sink(tmp_path)
    notifier = DryRunNotifier(logger=logging.getLogger("tests.notifier"), sink=sink)

    result = await notifier.send_approval_request(APPROVAL)

    assert isinstance(result, NotificationResult)
    assert result.delivered is False, "dry-run 没发出去，不许谎报 True"
    assert result.mode == "dry_run"
    assert result.channel == "dry_run"
    assert result.payload_digest == _independent_digest(APPROVAL)

    rows = sink.read(event_prefix="notifier.dry_run")
    assert len(rows) == 1
    row = rows[0]
    assert row["data"]["payload_digest"] == result.payload_digest
    assert row["data"]["action_id"] == "act_0001"
    assert row["data"]["target"] == "sku:3400000000000000002"
    assert row["run_id"] == "run_20261023_abc"
    assert row["shop_id"] == "12345678"


async def test_dry_run_does_not_block_when_sink_write_fails(tmp_path: Path) -> None:
    """落库/落盘失败 → 仍然是"通知失败"，不抛、不阻断审批，但要留下异常类型。"""
    import logging

    class BrokenSink:
        def write(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("disk full")

    logger = logging.getLogger("tests.notifier.broken")
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.handlers.clear()
    logger.addHandler(_Handler())
    logger.setLevel(logging.DEBUG)

    notifier = DryRunNotifier(logger=logger, sink=BrokenSink())  # type: ignore[arg-type]
    result = await notifier.send_approval_request(APPROVAL)

    assert result.delivered is False
    assert result.mode == "dry_run"
    assert "OSError" in result.detail, "必须记录异常类型，不能只说 failed"
    assert any("OSError" in str(getattr(record, "data", "")) for record in records)


async def test_feishu_success_path_posts_interactive_card() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"code": 0, "msg": "success"}, request=request)

    import logging

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.test"
    )
    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.test/hook/abc",
        http_client=client,
        logger=logging.getLogger("tests.notifier.feishu"),
    )
    result = await notifier.send_approval_request(APPROVAL)
    await client.aclose()

    assert result.delivered is True
    assert result.mode == "feishu"
    assert captured["url"].endswith("/hook/abc")
    assert captured["body"]["msg_type"] == "interactive"
    assert captured["body"]["card"]["header"]["title"]["tag"] == "plain_text"
    assert result.payload_digest == _independent_digest(APPROVAL)


@pytest.mark.parametrize(
    ("status", "body", "error_marker"),
    [
        (500, {"code": 0}, "HttpStatusError"),
        (200, {"code": 19001, "msg": "param invalid"}, "FeishuApiError"),
    ],
)
async def test_feishu_failure_is_reported_not_raised(
    status: int, body: dict, error_marker: str
) -> None:
    import logging

    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(f"tests.notifier.feishu_fail_{status}")
    logger.handlers.clear()
    logger.addHandler(_Handler())
    logger.setLevel(logging.DEBUG)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code=status, json=body, request=request)
        ),
        base_url="https://open.feishu.test",
    )
    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.test/hook/abc",
        http_client=client,
        logger=logger,
    )
    result = await notifier.send_approval_request(APPROVAL)
    await client.aclose()

    assert result.delivered is False
    assert error_marker in result.detail or error_marker in str([r.getMessage() for r in records])
    assert any(
        error_marker in str(getattr(record, "data", {}).get("error_type", ""))
        for record in records
    ), "失败日志必须带 error_type，不能只写 failed"


async def test_feishu_network_exception_is_not_raised_and_keeps_exception_type() -> None:
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("feishu timeout", request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.test"
    )
    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.test/hook/abc",
        http_client=client,
        logger=logging.getLogger("tests.notifier.feishu_timeout"),
    )
    result = await notifier.send_approval_request(APPROVAL)
    await client.aclose()
    assert result.delivered is False
    assert "ConnectTimeout" in result.detail


def test_build_notifier_selects_by_mode(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    client = httpx.AsyncClient(base_url="https://open.feishu.test")

    dry = build_notifier(build_settings({}), http_client=client, sink=sink)
    assert isinstance(dry, DryRunNotifier)
    assert dry.mode == "dry_run"


def test_build_notifier_feishu_without_webhook_is_startup_error(
    monkeypatch, tmp_path: Path
) -> None:
    """静默降级成 dry_run 会让"以为有告警、其实没有"活到生产。

    为什么用 `tmp_path` 而不是仓库内固定文件名：Windows 上 `close()` 之后文件句柄
    未必立刻释放，紧接着 `unlink()` 会偶发 `[WinError 32] 另一个程序正在使用此文件`——
    单独跑绿、全量跑红，这类"顺序相关的假红"最消耗人。`tmp_path` 每个用例一个目录，
    既不共享路径也不需要手动清理（评审实测：全量跑时这条曾单个失败）。
    """
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    settings = build_settings({"notifier": {"mode": "feishu"}})
    sink = JsonlLogSink(tmp_path / "feishu.jsonl")
    try:
        with pytest.raises(ConfigError) as excinfo:
            build_notifier(
                settings,
                http_client=httpx.AsyncClient(base_url="https://open.feishu.test"),
                sink=sink,
            )
        assert "FEISHU_WEBHOOK_URL" in str(excinfo.value)
    finally:
        sink.close()


def test_build_notifier_feishu_with_webhook_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.test/hook/abc")
    settings = build_settings({"notifier": {"mode": "feishu"}})
    sink = JsonlLogSink(tmp_path / "feishu2.jsonl")
    try:
        notifier = build_notifier(
            settings,
            http_client=httpx.AsyncClient(base_url="https://open.feishu.test"),
            sink=sink,
        )
        assert isinstance(notifier, FeishuWebhookNotifier)
        assert notifier.mode == "feishu"
    finally:
        sink.close()


# --------------------------------------------------------------------------------------
# F4：一次通知 = 一条事件（不许 sink 与 logger 各写一条）
# --------------------------------------------------------------------------------------
async def test_dry_run_emits_exactly_one_event(tmp_path: Path) -> None:
    """reviewer F4：一次通知写两条日志会让前端"一行=一次事件"的计数断言全部失真。"""
    import logging

    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("tests.notifier.one_event")
    logger.handlers.clear()
    logger.addHandler(_Handler())
    logger.setLevel(logging.DEBUG)

    sink = _sink(tmp_path)
    notifier = DryRunNotifier(logger=logger, sink=sink)
    await notifier.send_approval_request(APPROVAL)

    rows = sink.read(event_prefix="notifier.")
    assert len(rows) == 1, f"一次通知只许一条事件，实际 {[r['event'] for r in rows]}"
    assert rows[0]["event"] == "notifier.dry_run"
    # 同一个事件不许再从 logger 走一遍（JsonlHandler 会把 logger 写进同一个 sink）
    assert not [r for r in records if getattr(r, "event", None) == "notifier.dry_run"]


async def test_dry_run_failure_emits_exactly_one_event(tmp_path: Path) -> None:
    import logging

    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    class _BrokenSink:
        def write(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("disk full")

    logger = logging.getLogger("tests.notifier.one_event_fail")
    logger.handlers.clear()
    logger.addHandler(_Handler())
    logger.setLevel(logging.DEBUG)

    notifier = DryRunNotifier(logger=logger, sink=_BrokenSink())  # type: ignore[arg-type]
    result = await notifier.send_approval_request(APPROVAL)

    assert result.delivered is False
    events = [getattr(record, "event", None) for record in records]
    assert events.count("notifier.dry_run_failed") == 1
    assert "notifier.dry_run" not in events, "失败时不许再补一条『成功落库』事件"


# --------------------------------------------------------------------------------------
# F3：声明的渲染方式必须与实现一致（用户可控字段不可按 markdown 渲染）
# --------------------------------------------------------------------------------------
async def test_card_uses_plain_text_only_so_user_content_cannot_be_rendered() -> None:
    """`target`/`params`/`reason` 都来自提议内容；整卡 plain_text 是唯一自证安全的写法。"""
    import logging

    hostile = dict(APPROVAL)
    hostile["target"] = "sku:1 **[点我](https://evil.example/steal)**"
    hostile["reason"] = "[立即审批](https://evil.example/approve) —— <at id=all></at>"

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"code": 0}, request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.test"
    )
    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.test/hook/abc",
        http_client=client,
        logger=logging.getLogger("tests.notifier.card"),
    )
    await notifier.send_approval_request(hostile)
    await client.aclose()

    card = captured["body"]["card"]
    tags = _collect_tags(card)
    assert "lark_md" not in tags, f"卡片里出现了 markdown 渲染：{tags}"
    assert tags and tags <= {"plain_text", "div", "note", "fields"}, tags

    # 恶意文本必须原样留在 plain_text 里（不被当成链接/被截断），即"渲染成字面量"
    text = json.dumps(card, ensure_ascii=False)
    assert "evil.example/steal" in text
    assert "evil.example/approve" in text


def _collect_tags(node: object) -> set[str]:
    tags: set[str] = set()
    if isinstance(node, dict):
        tag = node.get("tag")
        if isinstance(tag, str):
            tags.add(tag)
        for value in node.values():
            tags |= _collect_tags(value)
    elif isinstance(node, list):
        for item in node:
            tags |= _collect_tags(item)
    return tags
