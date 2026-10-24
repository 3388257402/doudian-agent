"""**泄露通道**级测试：走真实的 `JsonlLogSink.write`，而不是只测 `redact()`。

为什么单独一个文件：review 指出前一组 15 条用例全部只调 `redact()/redact_any()`，
而真实泄露通道是 `JsonlLogSink.write()` 里的 `_scrub_text`。把那一行整段删掉，
前一组照样全绿——那正是这次事故的同一个失效模式（测被调函数，不测通道）。

本文件的断言一律以"**落盘后的文件里查不到那个串**"为准。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.observability.logging import JsonlLogSink, redact, redact_any

SECRET = "SECRET-abc123"


@pytest.fixture()
def sink(tmp_path: Path) -> JsonlLogSink:
    created = JsonlLogSink(tmp_path / "app.jsonl")
    yield created
    created.close()


def test_sink_write_scrubs_message(sink: JsonlLogSink) -> None:
    """`message` 是 F1 的实际泄露路径：上游错误原文写在这里，键名脱敏碰不到它。"""
    sink.write("upstream.error", level="error", message=f"抖店返回 access_token={SECRET}")
    assert SECRET not in sink.path.read_text(encoding="utf-8")


def test_sink_write_scrubs_data_values(sink: JsonlLogSink) -> None:
    sink.write("node.trace", message="ok", data={"detail": f"app_secret={SECRET}"})
    assert SECRET not in sink.path.read_text(encoding="utf-8")


def test_sink_write_scrubs_dict_key(sink: JsonlLogSink) -> None:
    """键名层也必须活着：值级 scrub 不能取代它。"""
    sink.write("x", message="ok", data={"access_token": SECRET})
    text = sink.path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert json.loads(text.strip())["data"]["access_token"] == "***"


@pytest.mark.parametrize(
    "text",
    [
        f"refresh_token={SECRET}",       # 复合键：最常见的真实写法
        f"client_secret={SECRET}",
        f"id_token={SECRET}",
        f"X-Auth-Token: {SECRET}",       # 带连字符的头
        f"x-dd-app-key={SECRET}",
    ],
)
def test_compound_and_hyphenated_keys_are_scrubbed(text: str) -> None:
    """左边界只排除字母数字：`_`/`-` 必须允许，否则这几类 100% 漏遮。"""
    out = redact(text)
    assert SECRET not in out, f"漏遮：{out!r}"


@pytest.mark.parametrize(
    "text",
    [
        "sign_method=hmac-sha256",
        "token_type=Bearer",
        "specification: 8",
        "design=1",
    ],
)
def test_lookalike_keys_are_not_mangled(text: str) -> None:
    """误伤回归：键名后缀像敏感词、但键本身不是（`sign_method` 不能遮 `hmac-sha256`）。"""
    assert redact(text) == text


def test_structured_value_keeps_json_valid() -> None:
    """对象/数组值必须整体遮成字符串：`{"token": ***}` 是非法 JSON，会让 /api/logs 下游崩。"""
    payload = {"token": {"v": 1}, "ok": 1}
    out = redact_any(json.dumps(payload))
    assert SECRET not in out
    parsed = json.loads(out)
    assert parsed["token"] == "***"
    assert parsed["ok"] == 1


def test_array_value_keeps_json_valid() -> None:
    parsed = json.loads(redact_any('{"secret": [1, 2, 3]}'))
    assert parsed["secret"] == "***"


def test_colon_value_does_not_swallow_following_keys() -> None:
    """`sign: missing shop_id=1` 只能遮 `missing`，不能把整行剩余内容吃掉。

    没有这条上界时，单行 100KB 日志里出现一个 `token:` 就足以让排障线索全灭。
    """
    out = redact("sign: missing shop_id=12345 order=678")
    assert "shop_id=12345" in out and "order=678" in out
    assert "missing" not in out


def test_header_value_with_space_is_still_fully_masked() -> None:
    """反过来：`Bearer xxx` 这种没有后续 `key=` 的值必须整段吞掉（只遮 Bearer 等于没遮）。"""
    out = redact(f"Authorization: Bearer {SECRET}")
    assert SECRET not in out
    assert "Bearer" not in out, f"半遮：{out!r}"
