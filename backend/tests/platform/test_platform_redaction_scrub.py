"""值级脱敏（`redact_any`）的对抗测试。

针对 REVIEW-core F1：`redact()` 曾经**只对 dict 键名生效**，对自由文本是 no-op，
于是 `access_token=SECRET-abc123` 会原样落进 JSONL——而契约与 DECISIONS 有 5 处声称已脱敏。
声明与实现反向不一致，比不声明更糟。

这些断言一律问"**泄露串是否还出现**"，而不是"函数有没有被调用"：
后者在 no-op 实现下同样是绿的，正是这次事故的成因。
"""

from __future__ import annotations

import json

import pytest

from app.observability.logging import redact, redact_any

SECRET = "SECRET-abc123"


@pytest.mark.parametrize(
    "text",
    [
        f"access_token={SECRET}",                       # query string 形态
        f"app_secret={SECRET}&shop=1",                  # 多参数，值止于 &
        f"Authorization: Bearer {SECRET}",              # HTTP 头形态（值含空格）
        f'{{"access_token": "{SECRET}"}}',              # JSON 形态
        f"'sign': '{SECRET}'",                          # 单引号形态
        f"请求失败 token={SECRET}, code=401",            # 中文文本里夹带
    ],
)
def test_value_level_scrub_removes_secret(text: str) -> None:
    out = redact(text)
    assert isinstance(out, str)
    assert SECRET not in out, f"未遮住：{out!r}"
    assert "***" in out, f"该遮的位置没有掩码：{out!r}"


def test_json_form_stays_valid_json() -> None:
    """遮完必须仍是合法 JSON：`_mask_value` 保留引号就是为了这个。

    如果只替换值不保留引号，`{"access_token": ***}` 会让下游 json.loads 直接崩，
    日志消费方（`/api/logs`、前端）会被一条日志搞挂。
    """
    payload = {"access_token": SECRET, "shop_id": "12345678"}
    out = redact_any(json.dumps(payload))
    assert SECRET not in out
    parsed = json.loads(out)
    assert parsed["access_token"] == "***"
    assert parsed["shop_id"] == "12345678"


def test_dict_key_is_still_masked() -> None:
    """键名脱敏不能被值级 scrub 取代（两层都要在）。"""
    assert redact({"access_token": SECRET}) == {"access_token": "***"}
    assert redact_any({"nested": {"app_secret": SECRET}}) == {"nested": {"app_secret": "***"}}


def test_password_as_dict_key_is_masked() -> None:
    """`password` 必须同时挡在**键名层**（v1.5.0 补）。

    它原先只在文本层（`DEFAULT_SCRUB_KEYS`），而文本层只认 `key=value` 形态——
    于是 `data={"password": "SECRET"}` 这种结构化值两层都碰不到，明文落盘。
    这条用例就是审查复现那条泄露的回归锁。
    """
    assert redact({"password": SECRET}) == {"password": "***"}
    assert SECRET not in json.dumps(redact_any({"outer": {"password": SECRET}}))


def test_sink_unions_configured_keys_with_defaults(tmp_path: Path) -> None:
    """**生产路径**也必须遮 `password`：配置传的键集合是**并集**，不是替换。

    这是那条泄露真正的根因（独立审查实测）：`JsonlLogSink.__init__` 原来写的是
    `tuple(redact_keys)`，而所有真实装配点都传 `settings.observability.redact_keys`
    （settings.yaml 里只有 5 个键，没有 `password`）。所以"把 password 加进
    DEFAULT_REDACT_KEYS"在生产路径上**完全不生效**——只改常量表是不够的。

    这条用例用 settings 那样的键集合构造 sink，断言默认键仍然生效：
    它会在"有人把并集改回替换"时立刻变红。
    """
    from app.observability.logging import JsonlLogSink

    sink = JsonlLogSink(
        tmp_path / "app.jsonl",
        # 故意只传 settings.yaml 里那 5 个键（不含 password）
        redact_keys=("app_secret", "access_token", "sign", "authorization", "cookie"),
    )
    try:
        sink.write("x", message="ok", data={"password": SECRET, "access_token": SECRET})
    finally:
        sink.close()

    text = (tmp_path / "app.jsonl").read_text(encoding="utf-8")
    assert SECRET not in text, f"默认键被配置覆盖掉了: {text}"
    assert text.count('"***"') >= 2


def test_base64_padding_is_consumed() -> None:
    """`=` 形态的值里可以含 `=`：Base64 填充不能被当成"值的结束"。

    否则 `access_token=AbC==` 会被遮成 `access_token=***==`，
    残留的 `==` 结合前后文仍可能泄露长度信息，更重要的是说明值边界判断错了。
    """
    token = "AbC123=="
    out = redact(f"access_token={token}")
    assert token not in out
    assert out.endswith("***"), f"值边界没吃干净：{out!r}"


def test_idempotent() -> None:
    """已遮过的再遮一次仍是 `***`——日志会被二次处理（例如审计事件复制 message）。"""
    once = redact(f"access_token={SECRET}")
    twice = redact(once)
    assert once == twice


@pytest.mark.parametrize(
    "text",
    [
        "design=1",              # 左边界：不能把 design 当成 sign
        "shop_id=12345678",      # 非敏感键不动
        "reason=库存不足",        # 中文值不动
        "",
    ],
)
def test_non_sensitive_text_is_untouched(text: str) -> None:
    """误伤和漏遮一样糟：把正常字段遮掉会让排障失去唯一线索。"""
    assert redact(text) == text


def test_secret_in_nested_list_is_scrubbed() -> None:
    """容器递归：错误摘要常以 list[dict] 形式出现在 data 里。"""
    value = {"errors": [{"message": f"app_secret={SECRET}"}, {"note": "ok"}]}
    out = redact_any(value)
    assert SECRET not in json.dumps(out, ensure_ascii=False)
    assert out["errors"][1]["note"] == "ok"
