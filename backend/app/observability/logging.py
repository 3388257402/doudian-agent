"""结构化日志汇（CONTRACT §7.6 / §0.1）。

一个进程一个 `JsonlLogSink`，JSONL 追加写。行结构 = `/api/logs` 的 `items[]` 元素：

    {"seq":1287,"ts":"2026-10-23T14:26:02.113Z","level":"info","event":"action.executed",
     "run_id":"run_x","trace_id":"7c1f...","shop_id":"12345678","node":"execute",
     "message":"price updated","data":{...}}

为什么是文件而不是表：节点事件高频 append-only、查询形态是"某 run 最近 N 条"、不 join
不聚合（DECISIONS ADR-015）。`seq` 由写入端分配，进程启动时从文件末尾续上
（读最后一行 `seq` + 1），保证跨进程重启单调递增——前端用 `before_seq` 做 keyset 分页，
`seq` 重复会让分页丢行。

脱敏在**写入前**完成（事后擦除等于已经泄露），且必须是**两层**（v1.3.3）：

1. **键名层**：`data` 里名字命中 `redact_keys`（默认 app_secret/access_token/sign/authorization/cookie）的值递归替换成 `"***"`；
2. **值级层（v1.3.3，ADR-020）**：任意**字符串**里的 `access_token=xxx` / `token: xxx` / `"access_token":"xxx"`
   三种形态，把敏感键后面的值替换成 `***`。只做第一层是不够的——上游错误原文是 `str`
   （`DoudianUpstreamError("... access_token=SECRET")`），按键名的脱敏对 `str` 是 no-op，
   于是"已脱敏"只对 dict 成立、对真正会泄露的那条通道不成立。

两层共用唯一入口 :func:`redact_any`，由 `JsonlLogSink.write` 在**每一行落盘前**对 `message` 与 `data`
统一调用——这样"新增一个日志写入点"不会顺便新增一个泄露点。
"""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

__all__ = [
    "DEFAULT_REDACT_KEYS",
    "DEFAULT_SCRUB_KEYS",
    "LogEvent",
    "JsonlLogSink",
    "JsonlHandler",
    "configure_logging",
    "get_logger",
    "get_trace_id",
    "new_trace_id",
    "redact",
    "redact_any",
    "reset_trace_id",
    "set_trace_id",
    "utcnow_iso",
]

#: 默认脱敏键（dict **键名**，大小写不敏感，见 `redact_any`）。
#:
#: `password` 必须在这一组里（v1.5.0 补）：它原先只在 `DEFAULT_SCRUB_KEYS`（文本层），
#: 而文本层只认 `key=value` 形态——于是 `data={"password": "SECRET"}` 这种**结构化**值
#: 两层都碰不到，明文落盘。这是审查实测出来的（构造一条带 password 的审计事件即可复现）。
#: 教训：一个名字该放哪一层，看的是"它出现在什么**位置**"，不是"它敏不敏感"。
DEFAULT_REDACT_KEYS: tuple[str, ...] = (
    "app_secret",
    "access_token",
    "sign",
    "authorization",
    "cookie",
    "password",
)

#: 默认 scrub 键（自由**文本里**出现在 `key=value` 里的键名，大小写不敏感，见 `redact_any`）。
#:
#: 与 `DEFAULT_REDACT_KEYS` 的区别是**位置**不是名字：那一组挡的是"dict 的键名"，这一组挡的是
#: "一整段字符串里的 `key=value`"（上游错误原文、query string、HTTP 头）。两组取**并集**用：
#: `token` / `password` / `app_key` 这些几乎只以文本形态出现，而 `cookie` 只出现在头的键名里。
DEFAULT_SCRUB_KEYS: tuple[str, ...] = (
    "access_token",
    "app_secret",
    "app_key",
    "token",
    "secret",
    "password",
    "sign",
    "authorization",
    "cookie",
)

#: 契约 §7.6 的 level 取值域。
LEVEL_NAMES: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

_REDACTED = "***"

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    """32 位小写 hex（§0 ID 规则）。"""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> Token[str | None]:
    """设置当前上下文 trace_id，返回 token 供 `reset_trace_id` 还原。"""
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _trace_id_var.reset(token)


def utcnow_iso(dt: datetime | None = None) -> str:
    """UTC ISO-8601，毫秒精度、以 `Z` 结尾（§0 时间规则）。"""
    value = dt if dt is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


#: 值级 scrub 的两条形态。**为什么值边界两种形态不同**：
#:
#: - `=`（query string / 我们自己的日志）：值止于 `&`、空白、引号、逗号、分号、右括号。
#:   Base64 的填充 `==` **不是**分隔符，所以值里可以含 `=`（`access_token=AbC==` 要整段吞掉）。
#: - `:`（HTTP 头 / JSON）：值**允许含空格**——`Authorization: Bearer xxx` 若不吞掉 "Bearer xxx"，
#:   只遮住 "Bearer" 等于把令牌原样留在后面。止于换行、引号、逗号、分号、右括号。
#:
#: 两种形态都优先匹配**带引号**的值，这样 JSON 文本被替换后仍然是合法 JSON（`"***"` 保留引号）。
#: 值的**五种**形态：双引号标量 / 单引号标量 / 单层对象 / 单层数组 / 裸值。
#:
#: 单引号那段不是可有可无：Python `dict` 的 repr（`{'sign': 'x'}`）和日志里手拼的
#: `key='value'` 都是单引号，漏掉它等于对这两类最常见的日志完全不脱敏。
#:
#: 对象/数组各占一支：只遮"标量"会把 `{"token": {"v": 1}}` 遮成 `{"token": ***"v": 1}}`
#: （非法 JSON，下游 `json.loads` 直接崩）。整段遮掉、由 `_mask_value` 输出 `"***"` 才合法。
#:
#: `:` 形态的裸值**必须**带惰性 + 前瞻上界：`[^\n...]+?` 配
#: `(?=\s+\w+\s*[:=]|$)` 让它在"下一个 key= 之前"就停住。
#: 没有上界时，`sign: missing shop_id=1 order=2` 会把整行剩余内容吞成一条 `***`，排障线索归零
#: （100KB 单行日志里只要前部出现一个 `token:` 就全灭）。同时它仍能整段吞掉
#: `Authorization: Bearer xxx`——那种写法后面没有 `key=`，只能落到 `$`。
_SCRUB_VALUE_EQ = (
    r"\"[^\"\n]*\""
    r"|'[^'\n]*'"
    r"|\{[^\n{}]*\}"
    r"|\[[^\n\[\]]*\]"
    r"|[^&\s,;\"'{}\[\]]+"
)
_SCRUB_VALUE_COLON = (
    r"\"[^\"\n]*\""
    r"|'[^'\n]*'"
    r"|\{[^\n{}]*\}"
    r"|\[[^\n\[\]]*\]"
    r"|[^\n\"'{};,\]]+?(?=\s+[A-Za-z_][\w.-]*\s*[:=]|$)"
)


@functools.lru_cache(maxsize=32)
def _scrub_patterns(keys: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """把敏感键集合编译成两条正则（`=` 形态、`:` 形态）。结果缓存：它会在每个节点事件上被调用。

    排序按长度倒序：`access_token` 必须排在 `token` 前面，否则交替会先命中 `token`
    从而只遮住 `_token=...` 这一段（正则交替是"先匹配先赢"）。
    `(?<![\\w-])` 是左边界：没有它，`design=1` 会被当成 `sign=1` 遮掉——误伤比漏遮少见但一样糟。
    """
    ordered = sorted({key for key in keys if key}, key=len, reverse=True)
    if not ordered:
        return ()
    alternation = "|".join(
        # 键名里的 `_` 允许写成 `-`：HTTP 头用连字符（`X-App-Key`），
        # 而 SDK 参数用下划线（`app_key`）。只认一种，另一类就整类漏遮。
        re.escape(key).replace("_", "[-_]")
        for key in ordered
    )
    # **`(?:...)` 不是可选的**：`|` 的优先级最低，直接写
    #   `前缀 A|B|C 后缀` 会让前缀只绑定 A、后缀只绑定 C，
    # 中间那些备选（`access_token` 等）就成了"光秃秃的键名"也能匹配——
    # 于是 `match.group("value")` 为 None，`_mask_value` 在 `_REDACTED` 分支炸掉；
    # 同时 `sign` 这一支失去左边界，`design=1` 会被遮成 `de***`（误伤正常字段）。
    # 这两件事都是实测踩出来的（`test_platform_redaction_scrub.py` 一跑全红）。
    # 左边界只排除**字母数字**（`(?<![A-Za-z0-9])`），不排除 `_` 与 `-`：
    # `refresh_token` / `client_secret` / `X-Auth-Token` 是真实日志里最常见的写法，
    # 把 `_`、`-` 也算作"词的一部分"会让它们 100% 漏遮（review 实测）。
    # 想挡的是 `design=1` 这类误伤——它靠"字母数字"这一条就挡住了，
    # 不需要连 `_`/`-` 一起禁；而 `sign_method=x`、`token_type=Bearer`
    # 因为键名后紧跟的不是 `[:=]`，本来就不会命中。
    return (
        re.compile(
            rf"(?<![A-Za-z0-9])[\"']?(?:{alternation})[\"']?\s*=\s*(?P<value>{_SCRUB_VALUE_EQ})",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?<![A-Za-z0-9])[\"']?(?:{alternation})[\"']?\s*:\s*(?P<value>{_SCRUB_VALUE_COLON})",
            re.IGNORECASE,
        ),
    )


def _scrub_keys(redact_keys: Iterable[str]) -> tuple[str, ...]:
    """值级 scrub 实际使用的键集合：`DEFAULT_SCRUB_KEYS` ∪ 配置的 `redact_keys`（并集，只增不减）。"""
    return tuple(sorted({*DEFAULT_SCRUB_KEYS, *redact_keys}))


def _scrub_text(text: str, patterns: Sequence[re.Pattern[str]]) -> str:
    """对一段自由文本做值级 scrub。已遮过的 `***` 再遮一次仍是 `***`（幂等）。"""
    for pattern in patterns:
        text = pattern.sub(_mask_value, text)
    return text


def _mask_value(match: re.Match[str]) -> str:
    """把命中的值换成 `***`；值原本被引号包着就**沿用同一个引号字符**。

    为什么要保留引号：被替换的往往是 JSON 或 Python repr 的一段文本，
    统一改成双引号会把 `{'sign': ...}` 变成 `{'sign': "***"}`（合法但风格突变），
    而丢掉引号会让 JSON 直接非法、下游 `json.loads` 炸掉整条日志。

    对象/数组值（`{...}` / `[...]`）一律改写成 `"***"`：它们是**结构化值**，
    用裸 `***` 替换会让 `{"token": ***}` 变成非法 JSON。改写成字符串既合法，
    又不会让人误以为"原值是个字符串"——`***` 本来就表示"这里被遮了"。
    """
    value = match.group("value")
    if value[:1] in {"{", "["}:
        masked = f'"{_REDACTED}"'
    else:
        quote = value[0] if value[:1] in {'"', "'"} else ""
        masked = f"{quote}{_REDACTED}{quote}" if quote else _REDACTED
    return match.string[match.start(): match.start("value")] + masked


def redact_any(
    value: Any,
    redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS,
    *,
    scrub_keys: Iterable[str] | None = None,
) -> Any:
    """**统一脱敏入口**：dict 键名脱敏 + 自由文本值级 scrub，同一次递归里两层都做。

    - `redact_keys`：dict **键名**命中就整值替换（`{"access_token": ...}` → `{"access_token": "***"}`）；
    - `scrub_keys`：文本里 `key=value` / `key: value` / `"key":"value"` 的**值**替换（缺省 = 上面两组并集）。

    返回新对象（不改入参）。非 str / 非容器原样返回——脱敏不负责改类型，也不要试图序列化。
    """
    # `redact_keys` 可能是生成器：下面要用它两次（键名层 + 值级层），
    # 不物化就会在第二次遍历时静默变空——那会让值级层退化成只剩默认键，
    # 而调用方以为自己传的键都生效了。
    keys = tuple(redact_keys)
    lowered = frozenset(key.lower() for key in keys)
    # `scrub_keys` 是**追加**语义，不是替换：显式传参曾经会把默认键集整段替换掉，
    # 于是 `redact_any(x, scrub_keys=("token",))` 之后 `password=` 反而不遮了——
    # 一个"越指定越不安全"的默认，必须堵死。
    if scrub_keys is None:
        effective_scrub = _scrub_keys(keys)
    else:
        effective_scrub = _scrub_keys((*scrub_keys, *keys))
    return _redact_inner(value, lowered, _scrub_patterns(effective_scrub))


def redact(value: Any, redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> Any:
    """`redact_any` 的旧名字（保留兼容）。

    v1.3.3 起它**不再只是键名脱敏**：`redact("access_token=SECRET")` 也会遮。改名不是为了
    换语义，而是让"对字符串调它等于没调"这个坑不能再被同一个名字掩盖（REVIEW-core F1）。
    """
    return redact_any(value, redact_keys)


def _redact_inner(
    value: Any, lowered: frozenset[str], patterns: Sequence[re.Pattern[str]]
) -> Any:
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in lowered:
                out[key] = _REDACTED
            else:
                out[key] = _redact_inner(item, lowered, patterns)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_inner(item, lowered, patterns) for item in value]
    if isinstance(value, str):
        return _scrub_text(value, patterns) if patterns else value
    return value


class LogEvent(TypedDict):
    """一行 JSONL 的完整结构（键永远齐全，缺的写 `null`，不省略键——§0 空值规则）。"""

    seq: int
    ts: str
    level: str
    event: str
    run_id: str | None
    trace_id: str | None
    shop_id: str | None
    node: str | None
    message: str
    data: dict[str, Any]


class JsonlLogSink:
    """线程安全的 JSONL 追加写 + 读取（`/api/logs` 的数据源）。"""

    def __init__(
        self,
        path: str | Path,
        *,
        redact_keys: Sequence[str] = DEFAULT_REDACT_KEYS,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # **并集，不是替换**：与文本层 `_scrub_keys()` 保持同一语义（"默认键只增不减"）。
        # 这里原先写的是 `tuple(redact_keys)`，而所有真实装配点传的都是 settings 里的键集合
        # （`adapters.refresh_log_sink` / `main` lifespan / 测试 conftest）——于是**新增一个默认键
        # 在生产路径上完全不生效**：把 `password` 加进 `DEFAULT_REDACT_KEYS` 之后，
        # `data={"password": "SECRET"}` 依旧明文落盘（独立审查实测）。
        # 教训：同一个类里两个"脱敏键集合"不能一个取并集、一个取替换——
        # 配置只能往默认集上**加**，不能悄悄把它减掉，否则默认值就是摆设。
        self._redact_keys = tuple({*DEFAULT_REDACT_KEYS, *redact_keys})
        self._scrub_patterns = _scrub_patterns(_scrub_keys(self._redact_keys))
        self._lock = threading.Lock()
        self._seq = self._read_last_seq()
        self._fh = self._path.open("a", encoding="utf-8")

    # --- 元信息 ---
    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_seq(self) -> int:
        return self._seq

    # --- 写 ---
    def write(
        self,
        event: str,
        *,
        level: str = "info",
        message: str = "",
        data: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        shop_id: str | None = None,
        node: str | None = None,
        ts: str | None = None,
    ) -> LogEvent:
        """追加一行并返回写入的完整行（含分配到的 `seq`）。

        `trace_id` 缺省取当前 contextvar。**落盘前统一脱敏**：`message` 与 `data` 都过
        :func:`redact_any`（含值级 scrub）。`message` 曾经是漏的——它不叫 `access_token`，
        所以按键名的脱敏永远碰不到它，而上游错误原文恰恰写在那里（REVIEW-core F1）。
        """
        if level not in {"debug", "info", "warn", "error"}:
            raise ValueError(f"非法 level: {level!r}（取值域 debug/info/warn/error，§7.6）")
        with self._lock:
            self._seq += 1
            line: LogEvent = {
                "seq": self._seq,
                "ts": ts or utcnow_iso(),
                "level": level,
                "event": event,
                "run_id": run_id,
                "trace_id": trace_id if trace_id is not None else get_trace_id(),
                "shop_id": shop_id,
                "node": node,
                "message": _scrub_text(message, self._scrub_patterns),
                "data": redact_any(dict(data or {}), self._redact_keys),
            }
            self._fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._fh.flush()
        return line

    # --- 读 ---
    def read(
        self,
        *,
        run_id: str | None = None,
        level: str | None = None,
        event_prefix: str | None = None,
        limit: int = 100,
        before_seq: int | None = None,
    ) -> list[LogEvent]:
        """按 `seq DESC` 返回最近若干行（`/api/logs` 语义）。

        `event_prefix` 是前缀匹配（`action.` 命中 `action.executed`）。
        """
        if limit < 1:
            raise ValueError("limit 必须 >= 1")
        if level is not None and level not in {"debug", "info", "warn", "error"}:
            raise ValueError(f"非法 level: {level!r}")
        matched: list[LogEvent] = []
        for line in self._iter_lines():
            if run_id is not None and line.get("run_id") != run_id:
                continue
            if level is not None and line.get("level") != level:
                continue
            if event_prefix is not None and not str(line.get("event", "")).startswith(event_prefix):
                continue
            seq = line.get("seq")
            if before_seq is not None and (not isinstance(seq, int) or seq >= before_seq):
                continue
            matched.append(line)  # type: ignore[arg-type]
        matched.sort(key=lambda item: item.get("seq", 0), reverse=True)
        return matched[:limit]

    def count(self, *, run_id: str | None = None, event: str | None = None) -> int:
        """统计满足条件的行数（`tracing.reconcile_step_count` 用它校准 `step_count`）。"""
        total = 0
        for line in self._iter_lines():
            if run_id is not None and line.get("run_id") != run_id:
                continue
            if event is not None and line.get("event") != event:
                continue
            total += 1
        return total

    # --- 生命周期 ---
    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> JsonlLogSink:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 内部 ---
    def _iter_lines(self) -> Iterable[dict[str, Any]]:
        # 全量扫描：日志文件是 dev/面试规模（几万行）。生产该换 SQLite/ES，
        # 这里不提前优化，但要保证语义正确（排序稳定、坏行跳过而不是整页 500）。
        with self._lock:
            self._fh.flush()
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        yield parsed
        except FileNotFoundError:
            return

    def _read_last_seq(self) -> int:
        last = 0
        if not self._path.exists():
            return last
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                seq = parsed.get("seq") if isinstance(parsed, dict) else None
                if isinstance(seq, int):
                    last = max(last, seq)
        return last


class JsonlHandler(logging.Handler):
    """把 stdlib `logging` 记录写进 `JsonlLogSink`。

    `extra={"event": ..., "data": {...}, "run_id": ..., "node": ...}` 会映射到对应列，
    其余 `extra` 键并入 `data`（不丢信息，且统一走脱敏）。
    """

    _RESERVED = frozenset(
        {"event", "data", "run_id", "trace_id", "shop_id", "node", "message"}
    )

    def __init__(self, sink: JsonlLogSink) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra_data: dict[str, Any] = {}
            for key, value in record.__dict__.items():
                if key in self._RESERVED or key.startswith("_") or key in _LOGRECORD_ATTRS:
                    continue
                extra_data[key] = value
            declared = getattr(record, "data", None)
            if isinstance(declared, Mapping):
                extra_data.update(declared)
            self._sink.write(
                getattr(record, "event", record.name),
                level=LEVEL_NAMES.get(record.levelno, "info"),
                message=record.getMessage(),
                data=extra_data,
                run_id=getattr(record, "run_id", None),
                trace_id=getattr(record, "trace_id", None),
                shop_id=getattr(record, "shop_id", None),
                node=getattr(record, "node", None),
            )
        except Exception:  # noqa: BLE001 - logging 内部不允许把异常抛回业务
            self.handleError(record)


#: `logging.LogRecord` 自带属性，不能当成 `extra` 抄进 data。
_LOGRECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
        "message", "asctime",
    }
)


def get_logger(name: str) -> logging.Logger:
    """取模块 logger（真正的输出目的地由 `configure_logging` 决定）。"""
    return logging.getLogger(name)


class _ScrubbingFormatter(logging.Formatter):
    """stderr 用：**先格式化，再 scrub**。

    为什么不能在 `record` 层脱敏：`%(message)s` 的最终文本只有 format 之后才存在，
    而容器/journald 收到的正是这段文本。JSONL 遮了、stderr 没遮，
    等于同一份凭据换了个通道明码落地——这是 review 抓出来的真实缺口。
    """

    def __init__(self, fmt: str, patterns: Sequence[re.Pattern[str]]) -> None:
        super().__init__(fmt)
        self._patterns = tuple(patterns)

    def format(self, record: logging.LogRecord) -> str:
        return _scrub_text(super().format(record), self._patterns)


def configure_logging(
    *,
    sink: JsonlLogSink,
    level: str = "INFO",
    also_stderr: bool = True,
) -> None:
    """装配 root logger：JSONL 汇（唯一事实源）+ 可选 stderr 人类可读流。

    重复调用是幂等的（先摘掉本函数装过的 handler），避免 uvicorn `--reload` 下日志翻倍。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        if getattr(handler, "_doudian_agent", False):
            root.removeHandler(handler)
            handler.close()
    jsonl = JsonlHandler(sink)
    jsonl._doudian_agent = True  # type: ignore[attr-defined]
    root.addHandler(jsonl)
    if also_stderr:
        stream = logging.StreamHandler()
        # stderr 也要脱敏：容器/journald 收集的是**格式化后的文本**，
        # 只遮 JSONL 等于同一份凭据在另一个通道明码落地（review 实测出这条）。
        stream.setFormatter(
            _ScrubbingFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                _scrub_patterns(_scrub_keys(DEFAULT_REDACT_KEYS)),
            )
        )
        stream._doudian_agent = True  # type: ignore[attr-defined]
        root.addHandler(stream)
