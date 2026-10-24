"""签名（CONTRACT §5.2）：三个黄金向量必须逐位相等。

这组用例的定位：**契约的不可回归锚点**。签名串里少一个字段、多一个空格、
`param_json` 的 key 顺序变了、大小写变了，都会让这里的断言变红——
而这些差异在真实环境里表现为"服务端说签名错"，很难排查。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.services.errors import DoudianSignatureError
from app.services.signing import (
    SIGN_METHOD_HMAC_SHA256,
    canonical_string,
    format_timestamp,
    normalize_param_json,
    sign,
    verify_callback_event_sign,
)
from platform_kit import load_signing_vectors

VECTORS = load_signing_vectors()


def test_three_golden_vectors_match_bit_for_bit() -> None:
    """§5.2 黄金向量：两个签名 + 一个回调 md5，逐一相等。"""
    app_key = VECTORS["app_key"]
    app_secret = VECTORS["app_secret"]
    timestamp = VECTORS["timestamp"]
    version = VECTORS["version"]

    for vector in VECTORS["vectors"]:
        param_json = normalize_param_json(vector["params"])
        assert param_json == vector["param_json"], (
            f"{vector['name']}: param_json 归一化结果与黄金向量不一致"
        )
        assert (
            sign(
                app_secret,
                app_key=app_key,
                method=vector["method"],
                param_json=param_json,
                timestamp=timestamp,
                version=version,
            )
            == vector["sign"]
        ), f"{vector['name']}: 签名与黄金向量不一致"

    callback = VECTORS["callback"]
    assert (
        hashlib.md5(  # noqa: S324 - 契约规定的回调算法
            (app_key + callback["body"] + app_secret).encode("utf-8")
        ).hexdigest()
        == callback["event_sign"]
    )


def test_callback_verify_helper_accepts_only_exact_sign() -> None:
    callback = VECTORS["callback"]
    assert verify_callback_event_sign(
        VECTORS["app_key"],
        callback["body"],
        VECTORS["app_secret"],
        received_sign=callback["event_sign"],
    )
    assert not verify_callback_event_sign(
        VECTORS["app_key"],
        callback["body"],
        VECTORS["app_secret"],
        received_sign=callback["event_sign"].upper() + "0",
    )
    assert not verify_callback_event_sign(
        VECTORS["app_key"],
        callback["body"] + " ",
        VECTORS["app_secret"],
        received_sign=callback["event_sign"],
    )


def test_canonical_string_layout_is_frozen() -> None:
    """规范串 = 五组 "名字+值" 直接拼接，无分隔符（§5.2 逐字）。"""
    assert (
        canonical_string("ak", "product.editPrice", '{"a":1}', "2024-01-01 00:00:00", 2)
        == 'app_keyakmethodproduct.editPriceparam_json{"a":1}timestamp2024-01-01 00:00:00v2'
    )
    # version 用 int 还是 str 必须得到同一个规范串（str(version) 归一）
    assert canonical_string("ak", "m", "{}", "ts", 2) == canonical_string("ak", "m", "{}", "ts", "2")


def test_normalize_param_json_is_key_order_and_locale_independent() -> None:
    first = normalize_param_json({"b": 2, "a": 1})
    second = normalize_param_json({"a": 1, "b": 2})
    assert first == second == '{"a":1,"b":2}'
    # 紧凑分隔符：没有逗号后的空格
    assert ", " not in first
    # ensure_ascii=False：中文不转义（否则签名串随实现变化）
    assert normalize_param_json({"title": "真丝衬衫"}) == '{"title":"真丝衬衫"}'
    # None（无参数）等价于空对象
    assert normalize_param_json(None) == "{}"


def test_sign_is_64_lowercase_hex() -> None:
    digest = sign(
        "secret",
        app_key="ak",
        method="shop.getShopInfo",
        param_json="{}",
        timestamp="2024-01-01 00:00:00",
        version=2,
    )
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_sign_covers_only_the_five_fields() -> None:
    """access_token / sign_method 不进签名：换它们不能改变摘要。"""
    base = dict(
        app_key="ak",
        method="shop.getShopInfo",
        param_json="{}",
        timestamp="2024-01-01 00:00:00",
        version=2,
    )
    assert sign("secret", **base) == sign("secret", **base, sign_method=SIGN_METHOD_HMAC_SHA256)


def test_unsupported_sign_method_is_rejected_not_guessed() -> None:
    with pytest.raises(DoudianSignatureError) as excinfo:
        sign(
            "secret",
            app_key="ak",
            method="m",
            param_json="{}",
            timestamp="ts",
            version=2,
            sign_method="md5",
        )
    assert "unsupported sign_method" in str(excinfo.value)
    assert excinfo.value.category.value == "signature"
    assert excinfo.value.retryable is False


def test_missing_app_secret_is_a_signature_error_not_a_silent_empty_key() -> None:
    with pytest.raises(DoudianSignatureError):
        sign("", app_key="ak", method="m", param_json="{}", timestamp="ts", version=2)


def test_format_timestamp_is_second_precision_without_timezone_suffix() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime(2026, 10, 23, 14, 25, 30, 113000, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert format_timestamp(dt) == "2026-10-23 14:25:30"


def test_vector_file_is_the_contract_text() -> None:
    """夹具必须是契约原文（任何人手改过这里都会被发现）。"""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fixtures" / "doudian" / "signing_vectors.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["app_key"] == "7000000000000000001"
    assert raw["app_secret"] == "test_secret_do_not_use"
    assert [v["name"] for v in raw["vectors"]] == ["edit_price", "empty_params"]
    assert raw["callback"]["event_sign"] == "35b25ee40affe3b74045b3c7fda228d3"
