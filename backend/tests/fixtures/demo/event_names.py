"""事件名的"生产者集合"提取器 + 两处复用（export / check）。

为什么需要它：演示数据（`frontend/mock/logs.json`、本目录导出的 `logs_export.jsonl`）如果
手写事件名，就一定会和实现漂——改一次代码漂一次，而"漂了"只有下次人工比对才被发现。
这里把"backend 真的会产出哪些事件名"变成**可执行的事实**：

    python check_event_names.py <文件...>

判定"一个事件名有生产者"，只看下面四类**字面量**（都在 `backend/app/**/*.py` 里）：

1. 关键字参数：`event="collect.done"` / `sink.write(..., event="node.trace", ...)`
2. 字典字面量：`{"event": "api.error", ...}`
3. 常量：`NODE_TRACE_EVENT = "node.trace"`
4. 位置参数写法：`self._log("doudian.call", ...)`（`_log` / `write` / `log` 的首参）
5. logger 名：`getLogger("app.main")` —— 未带 `event` 字段的记录会**用 logger 名当事件名**
   （`JsonlLogSink` 的既有行为），所以 logger 名也是生产者。

**方向是保守的**：宁可多认生产者（例如只出现在 docstring 示例里的名字），也不漏认——
多认只会让漂移少被发现一点，漏认会让"检查器本身说谎"。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "BACKEND_APP_DIR",
    "EVENT_NAME_RE",
    "extract_events_from_file",
    "producer_names",
    "CREDENTIAL_PATTERNS",
    "scan_credentials",
]

#: 本文件在 `backend/tests/fixtures/demo/`；上溯 3 层就是 `backend/`
BACKEND_APP_DIR = Path(__file__).resolve().parents[3] / "app"

#: 事件名形状：小写点分（`node.trace` / `api.error` / `doudian.call`）
EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")

#: 生产者的四种写法（见模块 docstring）
_PRODUCER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # (1) event="x.y"（前面必须是分隔符，避开中文全角括号里的文档示例）
    re.compile(r"(?:^|[\s(,{])event\s*=\s*\"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\""),
    # (2) {"event": "x.y"}
    re.compile(r"\"event\"\s*:\s*\"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\""),
    # (3) CONST = "x.y"（模块级大写常量）
    re.compile(
        r"^[A-Z][A-Z0-9_]*\s*=\s*\"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\"", re.MULTILINE
    ),
    # (4) self._log("x.y", ...) / sink.write("x.y", ...)
    re.compile(r"(?:_log|write|log)\(\s*\"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\""),
    # (5) logger 名：getLogger("app.main") —— 未带 event 字段时就是事件名
    re.compile(r"getLogger\(\s*\"([a-z][a-z0-9_.]+)\"\s*\)"),
)

#: "看起来像凭据"的串。用来把"把泄露样本带进仓库"这件事变成会失败的命令。
#: 注意：这不是"脱敏器"，是**出口闸门**——命中就说明导出的东西不该进仓库。
CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("access_token 明文", re.compile(r"access_token=(?!\*\*\*)[A-Za-z0-9._-]{4,}")),
    ("sign 明文", re.compile(r"[?&]sign=(?!\*\*\*)[A-Fa-f0-9]{8,}")),
    ("app_secret 明文", re.compile(r"app_secret=(?!\*\*\*)[^\s&\"']{4,}")),
    ("Bearer 令牌", re.compile(r"Bearer\s+(?!\*\*\*)[A-Za-z0-9._-]{8,}")),
    ("飞书 webhook", re.compile(r"open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]{8,}")),
)


def producer_names(app_dir: Path | None = None) -> set[str]:
    """扫一遍 `backend/app/**/*.py`，返回"真的会产出这些事件名"的集合。"""
    base = app_dir or BACKEND_APP_DIR
    names: set[str] = set()
    for path in sorted(base.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in _PRODUCER_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group(1)
                if EVENT_NAME_RE.match(candidate) or pattern is _PRODUCER_PATTERNS[4]:
                    names.add(candidate)
    return names


def extract_events_from_file(path: Path) -> list[str]:
    """从一个 JSON 文件里取出所有 `event` 值（支持 JSONL / JSON 数组 / 单个对象 / 嵌套）。"""
    text = path.read_text(encoding="utf-8")
    events: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            value = node.get("event")
            if isinstance(value, str):
                events.append(value)
            for nested in node.values():
                _walk(nested)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    try:
        _walk(json.loads(text))
        return events
    except json.JSONDecodeError:
        pass
    # JSONL：逐行解析，坏行直接报出来（不静默跳过）
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            _walk(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{number} 不是合法 JSON：{exc}") from exc
    return events


def scan_credentials(text: str) -> list[str]:
    """返回命中的"像凭据"的说明（空列表 = 干净）。"""
    hits: list[str] = []
    for label, pattern in CREDENTIAL_PATTERNS:
        if pattern.search(text):
            hits.append(label)
    return hits


def unknown_events(found: Iterable[str], producers: set[str]) -> list[str]:
    """`found` 里没有生产者的那些（排序去重）。"""
    return sorted({name for name in found if name not in producers})
