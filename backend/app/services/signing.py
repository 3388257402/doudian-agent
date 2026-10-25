"""抖店开放平台签名（CONTRACT §5.2，纯函数）。

算法按官方 SDK `doudian==0.6` 的 `core.py::_sign/_hash_hmac` 核实后重写（DECISIONS ADR-007）：

    canonical = "app_key" + app_key + "method" + method + "param_json" + param_json
              + "timestamp" + timestamp + "v" + str(version)
    message   = app_secret + canonical + app_secret
    sign      = HMAC-SHA256(key=app_secret, msg=message).hexdigest()      # 小写 hex

签名材料的四个来源（写测试向量时按这个顺序拼）：

| 片段 | 从哪来 | 谁保证它"只出现一次/不重复生成" |
| --- | --- | --- |
| `app_key` / `app_secret` | 环境变量（`DOUDIAN_APP_KEY` / `DOUDIAN_APP_SECRET`） | `create_doudian_client`（缺就启动期失败） |
| `method` | 调用方传的点号方法名 | `_METHOD_PATTERN` 先校验 |
| `param_json` | `normalize_param_json(params)` | `_send` 只算一次，签名与 body 共用 |
| `timestamp` | `format_timestamp(datetime.now(DOUDIAN_TZ))` | `_send` 每次尝试重新生成（ADR-001） |

三条不许偏离的约束：

1. **timestamp 只生成一次**（ADR-001）。`call()` 里生成一次，同一个字符串同时进签名与 URL
   查询串；重试时**重新**生成（时间戳过期会让签名失效，§9.2）。
2. 签名**只覆盖** app_key / method / param_json / timestamp / v 五个字段。
   access_token / sign_method / sign 都不参与签名，且都在 query 上，不塞 body。
3. `sign_method != "hmac-sha256"` → 抛错，**不猜**别的版本。

"只生成一次"与"重试时重新生成"看起来矛盾，其实说的是两个不同的作用域：**一次尝试内**只能
有一个 timestamp（签名与 URL 里的必须字面相同），**跨尝试**必须重新生成（旧的可能已过期）。
ADR-001 记的是前者那类 bug——签名用一个值、URL 用另一个值，服务端算出来的摘要必然不同。

两个函数**不要混用**：`sign`（HMAC-SHA256、对称密钥）只用于 API 调用；
`verify_callback_event_sign`（MD5）**只用于**消息推送回调，算法是抖店协议规定的，不是自选。
把它们合成一个"通用签名函数"会得到一个两处都不对的实现。

本模块**不**负责的三件事（都在调用方）：

- **密钥/token 的来源与生命周期**：从哪个环境变量读、什么时候刷新，是 `doudian_client`
  与装配层的事；本模块只接收字符串。
- **时间戳的取值**（生成本机时间还是注入时间）：`format_timestamp` 只格式化传进来的
  `datetime`，时区由调用方保证（`doudian_client.DOUDIAN_TZ`）。
- **验签的时间窗与重放**：只验摘要是否一致，不验"这条回调是不是刚发生的"。

纯函数是刻意的：没有 I/O、没有全局状态，所以签名可以用固定向量单测（改一位就红），
也能在任意进程里复现同一份摘要——这是"签名错"这类事故能被快速定位的前提。
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

# 导出面：`doudian_client` 用 `SIGN_METHOD_HMAC_SHA256` / `sign` / `format_timestamp` /
# `normalize_param_json`；`canonical_string` 只给测试（拼出期望串来断言，不必整条调 sign）；
# `verify_callback_event_sign` 目前没有生产调用方（本仓还没接消息推送），保留是因为它是
# §5.2 末段的一部分、且已有单测覆盖 —— 别因为"没人调"就删掉。
# `SIGNED_FIELD_ORDER` **不在** `__all__` 里：它只是文档常量，外部拿它去跑循环拼接就会造出
# 第二份签名实现。

SIGN_METHOD_HMAC_SHA256 = "hmac-sha256"
#: 时间戳格式（北京时间、无时区后缀）。三个细节都不能改：没有 `T`、没有毫秒、没有 `+08:00`。
#: 改任何一个，服务端都会把请求判成"签名过期"或"参数错误"，而不是"格式不对"——排障时会往
#: 密钥方向查很久。
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 参与签名的字段顺序（冻结，改动即改变摘要）。
#:
#: 这个元组目前**只作文档**用（`canonical_string` 里的拼接是手写的、与它同序）：它存在的意义
#: 是把"哪五个字段参与签名"写成可被 review 的常量，而不是散落在拼接表达式里。真正的约束点
#: 在 `canonical_string`——改那里时请同步这里，否则常量会变成一句谎话。
#: 不在其中的三样：`access_token` / `sign_method` / `sign`（都在 query 上，不参与摘要）。
SIGNED_FIELD_ORDER: tuple[str, ...] = ("app_key", "method", "param_json", "timestamp", "v")


# 签名链路的检查清单（按"最容易错"排序，排障时从上往下排查）：
#   1. `app_secret` 首尾各夹一次（`message`），只用一次 = 全量签名错误；
#   2. `param_json` 与 body 是**同一个字符串**（`ensure_ascii`/`sort_keys`/分隔符三处任一不同
#      都会得到不同摘要，而现象是"偶尔错"——只有含中文/多键的值才会暴露）；
#   3. `timestamp` 用的是东八区墙上时间，且**签名与 URL 同一个值**；
#   4. `v` 是 `"2"`（字符串形态），签名串里不会出现 `v=2` 这种带等号的写法；
#   5. 结果是小写 hex、64 位（服务端按字符串比对，大写会被判错）。
# 以上五条都在单测里各有一条断言。改这个文件时先跑
# `pytest backend/tests/test_platform_signing.py`，它比通读代码快。
def normalize_param_json(params: Mapping[str, Any] | None) -> str:
    """把参数归一化成签名用的 `param_json` 原文。

    `sort_keys=True` + 紧凑分隔符 + `ensure_ascii=False`：同一份参数在任何进程、
    任何 locale 下都得到同一个字符串（否则签名不可复现）。

    保证：`None` 与 `{}` 都得到 `"{}"`（签名与 body 用同一个字符串，不能一个空串一个 "{}"）；
    非 ASCII 字符（中文商品名/原因）以 UTF-8 原文参与签名，不做 `U+XXXX` 形式的转义。
    不保证：与服务端"文本层面"完全一致——本函数只能保证**我们发出去的签名与 body 是同一份
    字节**；服务端如何反序列化再算摘要不在可验证范围内，那要靠现网联调。
    不保证：参数里值的**类型**稳定——`{"a": 1}` 与 `{"a": "1"}` 得到不同的串，也应当不同
    （签名覆盖的是"实际发出去的内容"，不是语义）。
    坑：① 改成 `ensure_ascii=True`（默认）会让本仓所有含中文的调用签名对不上，而且现象是
    "偶发签名错误"（只有中文参数才炸）；② 一旦调用点分别 dumps 两次，只要两处有一条参数
    不同就是签名错，所以 `doudian_client._send` 把结果复用给签名和 body 两处。

    例子（键序无关、无空格）：`{"b": 1, "a": "中"}` → `{"a":"中","b":1}`。
    `sort_keys` 是**递归**作用于所有嵌套 dict 的，所以嵌套参数的顺序也被固定——这正是它能
    当签名材料的前提（只排顶层的话，`{"x": {"b":1,"a":2}}` 两种写法会得到两个摘要）。
    """
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        # 类型错误用 TypeError（不是 ValueError）：这是"传参写错了代码"，不是"配置坏了"。
        raise TypeError(f"params 必须是 Mapping，收到 {type(params).__name__}")
    return json.dumps(dict(params), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# `canonical_string` 单独暴露（而不是藏在 `sign` 里）的两个理由：
#   1. 测试要能拼出"期望串"再断言，否则签名错了只能看到一个 64 位 hex，无法定位是哪一段错；
#   2. 它是**不含密钥**的那一半，排障时可以原样打进日志（`sign` 的输出也不能打——它是
#      "能用一次的凭据"，泄了等于把这次请求交给别人重放）。
def canonical_string(
    app_key: str,
    method: str,
    param_json: str,
    timestamp: str,
    version: int | str,
) -> str:
    """拼规范串（不含密钥，可安全进日志/测试断言）。

    保证：字段顺序由 `SIGNED_FIELD_ORDER` 的语义冻结（app_key/method/param_json/timestamp/v），
    没有分隔符——官方算法就是"键名+值"直接拼接，加任何分隔符都会改变摘要。
    不保证：不含敏感信息。`app_key` 会出现在里面（它本来就在 query 上），但 `app_secret`、
    `access_token`、`sign` 都不在，所以这条串可以进日志。
    坑：`str(version)` 让 int/str 两种入参得到同一个串（`2` 与 `"2"` ⇒ `"v2"`）；别改成
    f-string 之外的类型转换（`3.0` 会变成 `"2.0"` 这种形状）。
    """
    return (
        "app_key" + app_key
        + "method" + method
        + "param_json" + param_json
        + "timestamp" + timestamp
        + "v" + str(version)
    )


# 参数全是 keyword-only（第一个 `app_secret` 除外）：五个字符串参数的顺序错了**不会报错**，
# 只会算出一个合法但错误的签名，而现象是"服务端说签名不对"。强制具名让"传错顺序"变成
# TypeError，而不是一次线上排障。
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
    """返回 64 位小写 hex 签名。

    保证：① 只实现 `hmac-sha256`（`SIGN_METHOD_HMAC_SHA256`），别的算法**抛错**而不是猜；
    ② `app_secret` 为空立刻抛（空密钥算出来的签名"看起来"是合法 hex，服务端只会回一个
    含糊的签名错误）；③ 结果是 `hmac.new(...).hexdigest()`，恒为 64 位小写。
    不保证：时间戳有效性。签名只覆盖字符串本身，`timestamp` 是否过期由服务端判（所以
    `doudian_client` 每次尝试都重新生成）。
    坑：`message = app_secret + canonical + app_secret`（首尾都夹密钥）是官方 SDK 的形状，
    不是笔误，也不是"HMAC 需要"——改成只用 `canonical` 当消息会得到一个**同样合法**但服务端
    不认的签名，现象是"全量 401/签名错误"。
    """
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
    """`%Y-%m-%d %H:%M:%S`（抖店要求本地时间格式，不带时区后缀）。

    保证：`dt` 的**墙上时间**被原样格式化（不做时区换算）——所以调用方必须传入已经处于
    `Asia/Shanghai` 的 datetime（`doudian_client` 用 `DOUDIAN_TZ` 生成）。
    不保证：传 UTC datetime 会被拒绝。它不会：会静默得到一个差 8 小时的时间戳，服务端判
    "签名过期"。这是本函数唯一的坑，且只在"容器时区不是东八区"时才暴露。
    坑：格式里没有 `T`、没有毫秒、没有时区后缀，三个都不能加（官方协议照抄）。
    """
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

    保证：比较是常数时间的（`compare_digest`），不会因为前缀匹配长度泄露信息；`received_sign`
    先转小写再比，所以服务端给大写 hex 也能过。
    不保证：**时效与重放**。本函数只回答"这个签名对不对"，不回答"这条回调是不是刚发生的、
    之前有没有收过"。重放防护要靠调用方按事件 id 去重（如果上游给了 id）。
    坑：`body_text` 必须是**原始**请求体（未重排/未重新序列化的字节解码结果）。用
    `json.loads` 再 `dumps` 出来的串几乎一定验不过，因为键序/空格/转义都已变化。

    一个能直接跑的例子（测试里就是这么构造的）：`app_key="ak"`、`body_text='{"a":1}'`、
    `app_secret="sec"` → `expected = md5("ak" + '{"a":1}' + "sec")`。
    注意拼接顺序是 **app_key + body + app_secret**，与 API 签名那套（首尾夹密钥做 HMAC）
    完全不同——两个协议用两套算法，别把参数顺序"统一"成同一个样子。
    """
    expected = hashlib.md5(  # noqa: S324 - 抖店回调协议规定 md5，非自选
        (app_key + body_text + app_secret).encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(expected, (received_sign or "").lower())
