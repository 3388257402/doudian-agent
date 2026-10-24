"""抖店开放平台签名（CONTRACT §5.2，纯函数）。

算法按官方 SDK `doudian==0.6` 的 `core.py::_sign/_hash_hmac` 核实后重写（DECISIONS ADR-007）：

    canonical = "app_key" + app_key + "method" + method + "param_json" + param_json
              + "timestamp" + timestamp + "v" + str(version)
    message   = app_secret + canonical + app_secret
    sign      = HMAC-SHA256(key=app_secret, msg=message).hexdigest()      # 小写 hex

三条不许偏离的约束：

1. **timestamp 只生成一次**（ADR-001）。`call()` 里生成一次，同一个字符串同时进签名与 URL
   查询串；重试时**重新**生成（时间戳过期会让签名失效，§9.2）。
2. 签名**只覆盖** app_key / method / param_json / timestamp / v 五个字段。
   access_token / sign_method / sign 都不参与签名，且都在 query 上，不塞 body。
3. `sign_method != "hmac-sha256"` → 抛错，**不猜**别的版本。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.services.errors import DoudianSignatureError

__all__ = [
    "SIGN_METHOD_HMAC_SHA256",
    "TIMESTAMP_FORMAT",
    "canonical_string",
    "sign",
    "format_timestamp",
    "normalize_param_json",
    "verify_callback_event_sign",
]

SIGN_METHOD_HMAC_SHA256 = "hmac-sha256"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 参与签名的字段顺序（冻结，改动即改变摘要）。
SIGNED_FIELD_ORDER: tuple[str, ...] = ("app_key", "method", "param_json", "timestamp", "v")


def normalize_param_json(params: Mapping[str, Any] | None) -> str:
    """把参数归一化成签名用的 `param_json` 原文。

    `sort_keys=True` + 紧凑分隔符 + `ensure_ascii=False`：同一份参数在任何进程、
    任何 locale 下都得到同一个字符串（否则签名不可复现）。
    """
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise TypeError(f"params 必须是 Mapping，收到 {type(params).__name__}")
    return json.dumps(dict(params), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical_string(
    app_key: str,
    method: str,
    param_json: str,
    timestamp: str,
    version: int | str,
) -> str:
    """拼规范串（不含密钥，可安全进日志/测试断言）。"""
    return (
        "app_key" + app_key
        + "method" + method
        + "param_json" + param_json
        + "timestamp" + timestamp
        + "v" + str(version)
    )


def sign(
    app_secret: str,
    *,
    app_key: str,
    method: str,
    param_json: str,
    timestamp: str,
    version: int | str,
    sign_method: str = SIGN_METHOD_HMAC_SHA256,
) -> str:
    """返回 64 位小写 hex 签名。"""
    if sign_method != SIGN_METHOD_HMAC_SHA256:
        raise DoudianSignatureError(
            f"unsupported sign_method: {sign_method!r}（只实现 {SIGN_METHOD_HMAC_SHA256}）",
        )
    if not app_secret:
        raise DoudianSignatureError("app_secret 缺失，无法签名（检查 DOUDIAN_APP_SECRET）")
    canonical = canonical_string(app_key, method, param_json, timestamp, version)
    message = app_secret + canonical + app_secret
    return hmac.new(
        app_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def format_timestamp(dt: datetime) -> str:
    """`%Y-%m-%d %H:%M:%S`（抖店要求本地时间格式，不带时区后缀）。"""
    return dt.strftime(TIMESTAMP_FORMAT)


def verify_callback_event_sign(
    app_key: str,
    body_text: str,
    app_secret: str,
    *,
    received_sign: str,
) -> bool:
    """消息推送回调验签：`md5(app_key + body_text + app_secret)`（§5.2 末段）。

    只用于回调，**不用于** API 调用。用 `compare_digest` 做常数时间比较。
    """
    expected = hashlib.md5(  # noqa: S324 - 抖店回调协议规定 md5，非自选
        (app_key + body_text + app_secret).encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(expected, (received_sign or "").lower())
