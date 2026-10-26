"""LLM 旁路：`explain` 节点唯一的"外部世界"依赖（协议 + OpenAI 兼容实现）。

## 这个模块**不是**决策组件（先说边界，因为它决定了下面每一处设计）

项目里第一次有"模型在场"，但它**只产出两样给人看的文字**：

- `explanations`：某个异常到底意味着什么（一句中文人话）；
- `suggestions`：一句建议草稿（**不是** `ActionProposal`）。

动作**永远**只由规则（`services/actions.py`）产出。本模块没有任何通往 `pending_actions` /
`approval_request` / `action_log` 的路径，也不写 `state`——它只返回一个不可变结果值，由调用方
（`nodes.explain_node`）决定怎么落进 state 与审计。把"模型说了什么"和"系统要做什么"焊在同一个
返回类型上，迟早会有人从模型输出里读出一个动作；这里从类型上就不给这个机会。

## 配置只走环境变量（不进 `config/settings.yaml`）

`LLM_API_KEY` / `LLM_BASE_URL`（默认 `https://api.deepseek.com`）/ `LLM_MODEL`
（默认 `deepseek-chat`）/ `LLM_TIMEOUT_S`（默认 5 秒）。

为什么不开一个 `settings.yaml` 的 `llm:` 段：`config.build_settings` 的段/键白名单是**冻结**的
（§8.1「未知字段一律 ConfigError」），加一段要走契约变更流程；而这条旁路的第一原则是"没配就
当它不存在"，它的开关天然是"有没有那把 key"。所以配置读取集中在本模块一处，且**只在装配层
（`app.main` 的 lifespan）调用一次**。

**坏值不许静默**：`LLM_TIMEOUT_S=abc` / 负数 / `LLM_BASE_URL` 不是 http(s) —— 一律抛
`ConfigError`（启动期失败）。理由与 `config.py` 的同名校验完全一致：悄悄退回默认值等于
"配置没生效但没人知道"，而这条旁路失效的表现是"解释一直是空的"，没人会因此怀疑配置。
注意"空字符串"不在此列：`LLM_*=` 是"没配"（与 `config.build_settings` 对 `FOO=` 的处理一致）。

## 失败一律降级，绝不重试、绝不阻塞

`explain` 是旁路：它失败时这一轮少两句人话，别的什么都不变。所以本模块**只返回**降级结果，
不抛业务异常、不重试。四个 `reason` 是闭合枚举（调用方直接写进审计，不做二次翻译）：

| reason | 判据 |
|---|---|
| `timeout` | 墙钟超时（`asyncio.timeout`）或 httpx 的任一超时 |
| `http_error` | 连不上 / 非 2xx / TLS 等 `httpx.HTTPError` |
| `bad_json` | 拿不到"一段 JSON 文本"（信封形状不对，或 content 不是合法 JSON） |
| `schema_mismatch` | 拿到了 JSON，但形状不是约定的那两个 `{anomaly_id: str}` |

**为什么 `bad_json` 与 `schema_mismatch` 要分开**：前者是"模型/网关坏了"，改 prompt 没用；
后者是"prompt 被模型理解歪了"，要改的是 prompt。合成一个 reason 就等于放弃这个判断。

**为什么超时要两套**：httpx 的 `read` 超时是**每个读块**的预算，一个每次吐一个字节的响应能把
它拖成任意长。旁路组件必须有墙钟上界，所以外面再套一层 `asyncio.timeout`。两个都留着：
httpx 那层让连接失败尽快返回，`asyncio.timeout` 那层才是"5 秒就是 5 秒"的保证。

**未知异常不接**：只有上面四类被折成降级。`AttributeError` / `TypeError` 这类是**代码 bug**，
让它们照常抛出去（`traced()` 会记成 `outcome=error`），而不是伪装成"上游今天不太好"。这与
`nodes._business_date_of` 里「代码 bug 不许被降级成数据脏」是同一条纪律。

## 脱敏的责任划分

**调用方（节点）负责脱敏输入**，因为只有它知道 `observability.redact_keys` 与哪些字段可能带
上游原文；本模块只负责把 `request.payload` 原样当成 user message 发出去，**不再猜**哪些键敏感
（两处各猜一次必然走散）。模型输出由调用方在落盘前再脱一次（模型可能把我们发的原文回吐出来）。
本模块从不记录 `api_key`，也不把上游响应体写进任何日志。

## 与进程生命周期的关系

一个进程一个 `httpx.AsyncClient`（连接池），由装配层持有、关停时 `aclose()`。不要在每个节点里
新建——那正是 ADR-004 对抖店客户端禁止的做法，理由在这里同样成立（每轮 run 新建一个连接池，
旁路就变成了资源放大器）。注入 `http_client` 时**所有权在调用方**，本类不会关闭它（测试用假
transport 就是这么用的）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.errors import ConfigError

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_S",
    "DEGRADE_BAD_JSON",
    "DEGRADE_HTTP_ERROR",
    "DEGRADE_REASONS",
    "DEGRADE_SCHEMA_MISMATCH",
    "DEGRADE_TIMEOUT",
    "ExplainRequest",
    "ExplainResult",
    "LlmClient",
    "LlmConfig",
    "OpenAiCompatibleLlmClient",
    "SYSTEM_PROMPT",
    "build_llm_client",
    "config_from_env",
]

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

#: 默认网关（DeepSeek 的 OpenAI 兼容端点）。换供应商只改 `LLM_BASE_URL`，代码不动。
DEFAULT_BASE_URL: Final[str] = "https://api.deepseek.com"
DEFAULT_MODEL: Final[str] = "deepseek-chat"
#: 默认墙钟预算（秒）。比 `app.request_timeout_s`（30s）短得多：旁路没有资格吃掉整个请求预算。
DEFAULT_TIMEOUT_S: Final[float] = 5.0

DEGRADE_TIMEOUT: Final[str] = "timeout"
DEGRADE_HTTP_ERROR: Final[str] = "http_error"
DEGRADE_BAD_JSON: Final[str] = "bad_json"
DEGRADE_SCHEMA_MISMATCH: Final[str] = "schema_mismatch"

#: 降级原因的**闭合**枚举：调用方（审计、测试、未来的告警规则）按它分支，不要新增第五个而不改这里。
DEGRADE_REASONS: Final[frozenset[str]] = frozenset(
    {DEGRADE_TIMEOUT, DEGRADE_HTTP_ERROR, DEGRADE_BAD_JSON, DEGRADE_SCHEMA_MISMATCH}
)

#: 模型要遵守的协议。**只有这么两份输出**，且明确写了"你不做决策"——prompt 是唯一能约束模型
#: 输出形状的手段（我们不解析自然语言，也不给模型任何动作模板）。
SYSTEM_PROMPT: Final[str] = (
    "你是电商运营助理。输入是一份 JSON：一家店铺当天的指标，以及一组由**规则**检测出的异常"
    "（每条都不属于任何动作提议）。\n"
    "你只做两件事，每条都用中文、只说一句人话：\n"
    "1. explanations：这条异常意味着什么；\n"
    "2. suggestions：一句给人工参考的建议草稿。\n"
    "你不做决策：任何改价/改库存都不会由你执行，你的文字也不会被当成动作。"
    "不要假装能执行，也不要给出参数表。\n"
    "只返回 JSON，不要代码围栏、不要解释性文字，形状严格如下：\n"
    '{"explanations": {"<anomaly_id>": "..."}, "suggestions": {"<anomaly_id>": "..."}}\n'
    "键必须来自输入里出现过的 anomaly_id；不认识或不存在的 id 不要自己编。"
)

#: 代码围栏：模型偶尔会把 JSON 包在 ```json ... ``` 里。去掉它而不是判成 bad_json ——
#: 这是"同样的内容换了个包装"，判死会让一条本来可用的解释白白丢掉。
_FENCE_RE = re.compile(r"^\s*```[A-Za-z]*\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


# --------------------------------------------------------------------------------------
# 配置（只在装配层读一次环境变量）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LlmConfig:
    """LLM 旁路的静态配置（`frozen`：启动期定下来的事实，运行期改它需要重启）。

    保证：`base_url` 已去掉尾部斜杠且是 http(s)；`timeout_s > 0`；`model` 非空。
    不保证：`api_key` 一定可用——空串表示"没配"（`enabled=False`，装配层装 `None`），
        配了也可能没有权限（那会在第一次调用时表现为 `http_error` 降级）。
    调用方坑：`api_key` 出现在这里**不代表可以打日志**。本类没有 "to_dict"，
        就是为了不给"顺手把配置整份写进日志"留口子。
    """

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_s: float = DEFAULT_TIMEOUT_S

    @property
    def enabled(self) -> bool:
        """有没有 key（= 这条旁路是否在场）。

        保证：只判"非空"，不判有效性——有效性只有真正调用一次才知道。
        调用方坑：`False` 时装配层必须装 `None` 而不是"装一个每次 401 的客户端"，
            否则每轮 run 都会多一条 `llm.degraded(http_error)`，看起来像持续故障。
        """
        return bool(self.api_key)


def _parse_timeout(raw: Any) -> float:
    """`LLM_TIMEOUT_S` → 正 float；缺省/空串 → 默认值；**有值但坏** → `ConfigError`。

    为什么不"尽力而为"地退回默认值：这条旁路失效的表现是"解释一直是空的"，
    没有任何人会因此去怀疑"我把 5s 写成了 5s "——静默退回默认值等于让那个 typo 永久生效。
    """
    if raw is None or str(raw).strip() == "":
        return DEFAULT_TIMEOUT_S
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise ConfigError(f"LLM_TIMEOUT_S 必须是数字（秒），收到 {raw!r}") from exc
    if value <= 0:
        # 0 或负数会让 httpx 立刻超时：那不是"关掉超时"，是"永远降级"。
        raise ConfigError(f"LLM_TIMEOUT_S 必须是正数（秒），收到 {raw!r}")
    return value


def config_from_env(env: Mapping[str, str] | None = None) -> LlmConfig:
    """从环境变量组装 `LlmConfig`（纯函数：`env=None` 时读真实 `os.environ`）。

    保证：返回的配置自洽（URL 合法、超时为正），否则抛 `ConfigError`（启动期失败）。
    不保证：不校验 `api_key` 是否有效（见 `LlmConfig.enabled`；那只有真调一次才知道）。
    调用方坑：测试务必显式传 `env={}`，否则结果会随开发机的环境变量漂移——这与
        `config.build_settings` 里同名参数的理由一样。
    """
    source: Mapping[str, str] = os.environ if env is None else env
    api_key = str(source.get("LLM_API_KEY") or "").strip()
    # 空串按"没配"处理：容器编排里 `LLM_BASE_URL=` 极常见，不该把地址覆盖成空。
    base_url = str(source.get("LLM_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    model = str(source.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL
    if not base_url.startswith(("http://", "https://")):
        # 漏协议（`api.deepseek.com`）在 httpx 里要等到第一次请求才炸，那时进程早已"启动成功"。
        raise ConfigError(f"LLM_BASE_URL 必须是 http(s) URL，收到 {base_url!r}")
    return LlmConfig(
        api_key=api_key,
        # 去尾斜杠：`base_url + "/chat/completions"` 出现双斜杠时，某些网关会把它当成不同路径。
        base_url=base_url.rstrip("/"),
        model=model,
        timeout_s=_parse_timeout(source.get("LLM_TIMEOUT_S")),
    )


# --------------------------------------------------------------------------------------
# 请求 / 结果（都是不可变值：调用方拿到之后没有任何办法把它变成"动作"）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ExplainRequest:
    """一次解释请求。

    保证：`anomaly_ids` 是**本次输入的** id 全集（模型编出来的 id 靠它挡掉）；
        `payload` 已由调用方脱敏（本模块不再猜哪些键敏感）。
    不保证：不保证 `payload` 里的字段名与 `anomaly_ids` 一一对应——那只是给模型看的摘要。
    调用方坑：`payload` 会被 `json.dumps` 后原样发给模型，所以**只能**放 JSON 原生类型。
    """

    anomaly_ids: tuple[str, ...]
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExplainResult:
    """一次解释的结果（成功或降级，二选一）。

    保证：`ok=True` 时 `reason is None`；`ok=False` 时 `reason in DEGRADE_REASONS`，
        且 `explanations` / `suggestions` 都是空 dict（降级不产出半份解释）。
    不保证：`ok=True` 不保证每条异常都有解释——模型可以只解释一部分。
    调用方坑：`dropped` 只在 `ok=True` 时有意义（模型给出了本次输入之外的 id，或给了空串）；
        它**不是**"失败条数"，失败走 `reason`。
    """

    ok: bool
    explanations: Mapping[str, str]
    suggestions: Mapping[str, str]
    dropped: int = 0
    reason: str | None = None
    detail: str | None = None

    @classmethod
    def success(
        cls,
        *,
        explanations: Mapping[str, str],
        suggestions: Mapping[str, str],
        dropped: int = 0,
    ) -> ExplainResult:
        """成功结果的唯一构造点（避免有人手搓一个 `ok=True, reason="timeout"`）。"""
        return cls(
            ok=True,
            explanations=dict(explanations),
            suggestions=dict(suggestions),
            dropped=int(dropped),
        )

    @classmethod
    def degraded(cls, reason: str, detail: str = "") -> ExplainResult:
        """降级结果的唯一构造点。

        为什么把 `reason` 收在这里校验：它是审计里的**枚举**值，拼错一个字母会让"为什么没解释"
        在日志里变成一句无法分组的新字符串，而没有任何东西会报错。
        """
        if reason not in DEGRADE_REASONS:  # pragma: no cover - 内部调用点受限
            raise ValueError(f"非法降级原因: {reason!r}（取值域见 DEGRADE_REASONS）")
        return cls(ok=False, explanations={}, suggestions={}, dropped=0, reason=reason,
                   detail=str(detail)[:120])


# --------------------------------------------------------------------------------------
# 协议
# --------------------------------------------------------------------------------------
@runtime_checkable
class LlmClient(Protocol):
    """解释器协议：`explain` 是唯一的业务方法，`aclose` 只服务进程关停。

    保证：实现方**不得**抛业务异常（四类失败必须折成 `ExplainResult.degraded`）；
        `explain` 不得有副作用（不写库、不写 state、不发通知）。
    不保证：不保证 `explain` 一定返回解释——降级是正常返回值，不是异常。
    调用方坑：`aclose` 必须幂等（关停路径可能被调用两次：一次正常关、一次启动失败收尾）。
    """

    async def explain(self, request: ExplainRequest) -> ExplainResult:  # pragma: no cover
        ...

    async def aclose(self) -> None:  # pragma: no cover
        ...


# --------------------------------------------------------------------------------------
# 模型输出的 schema（pydantic）
# --------------------------------------------------------------------------------------
class _Explanations(BaseModel):
    """模型必须返回的 JSON 形状：`{"explanations": {...}, "suggestions": {...}}`。

    三条硬约束，缺一条就等于"模型慢慢改坏了协议而没人知道"：

    1. `extra="forbid"`：多一个顶层键就是形状漂移（模型很爱加 `"note"` / `"reason"`），
       静默忽略它会让"它开始输出别的东西了"永远不可见；
    2. 值必须是 `str`：`{"x": 123}` 不是我们的契约，数字/列表混进来会让审计与前端各猜一次；
    3. 两个键**都必须存在**：没给 `suggestions` 也算形状漂移——契约就是两份输出，
       缺一份时给 `{}` 会让"模型只学会了一半"这件事变成一个静默的默认值。

    注意"id 必须是本次输入的子集"**不在**这里校验：那是业务判断（要对照 `request.anomaly_ids`
    才知道），而且它的处置方式是"丢掉那一条"而不是"整份降级"，所以放在 `_keep_known_ids`。
    """

    model_config = ConfigDict(extra="forbid")

    explanations: dict[str, str]
    suggestions: dict[str, str]


def _keep_known_ids(mapping: Mapping[str, str], allowed: frozenset[str]) -> tuple[dict[str, str], int]:
    """只保留 `allowed` 里的 id，并丢掉空白值；返回 `(保留的, 丢掉的条数)`。

    为什么要丢而不是整份降级：模型编一个 id（`"anomaly_1"`）时，其余几条往往是好用的。
    整份丢 = 为了一条编造的 id 让另外五条真解释一起消失。

    为什么要丢空白值：`{"x": "   "}` 通过了 schema（它确实是 str），但它落到 state 里就是
    "这条异常有解释"的假象。空白 = 没解释，与"没给这一条"同义。

    调用方坑：丢掉的条数会被调用方报成 `schema_mismatch` 降级（这是**形状漂移**的信号，
    不是"模型今天话少"）。
    """
    kept: dict[str, str] = {}
    dropped = 0
    for key, value in mapping.items():
        text = value.strip()
        if key not in allowed or not text:
            dropped += 1
            continue
        kept[key] = text
    return kept, dropped


def _strip_fence(text: str) -> str:
    """去掉模型可能加的 ```json ... ``` 包裹（不是"尽力解析"，只处理这一种确定的包装）。"""
    match = _FENCE_RE.match(text)
    body = match.group("body") if match else text
    return body.strip()


def _content_of(raw_text: str) -> str | None:
    """从 chat/completions 响应里取出 `choices[0].message.content`；取不到 → `None`。

    只认这一条路径：别的形状（`choices` 为空、`message` 是字符串、`content` 是列表）
    都属于"网关/供应商改了协议"，由调用点折成 `bad_json`——不猜、不兼容，猜出来的东西
    会被当成模型的回答写进 state。
    """
    try:
        envelope = json.loads(raw_text)
    except ValueError:
        return None
    if not isinstance(envelope, Mapping):
        return None
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _schema_detail(exc: ValidationError) -> str:
    """把 pydantic 的第一个错误折成一句短 detail（`type@loc`），不含模型原文。

    为什么只给类型与位置：`detail` 会进 JSONL 审计，模型的原文可能带任意内容（含我们发过去的
    摘要）。只留 `missing@suggestions` / `extra_forbidden@note` 这种形状信息，既够定位又不搬内容。
    """
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic 至少给一条
        return "validation_error"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc") or ()) or "<root>"
    return f"{first.get('type') or 'invalid'}@{location}"


# --------------------------------------------------------------------------------------
# OpenAI 兼容实现
# --------------------------------------------------------------------------------------
class OpenAiCompatibleLlmClient:
    """`/chat/completions` 的最小实现（DeepSeek / OpenAI / 任何兼容网关通用）。

    保证：四类失败都折成 `ExplainResult.degraded`（**不抛**），且整次调用有墙钟上界；
        返回的 `explanations` / `suggestions` 的 id 一定是 `request.anomaly_ids` 的子集。
    不保证：不保证模型遵守 prompt（它可能答非所问），也不保证解释质量——本类只做形状校验。
    调用方坑：`http_client` 注入时**所有权在调用方**（本类不会 `aclose` 它）；生产路径不注入，
        于是本类自己持有连接池，必须由装配层在关停时调用 `aclose()`。
    """

    def __init__(
        self, config: LlmConfig, *, http_client: httpx.AsyncClient | None = None
    ) -> None:
        if not config.enabled:
            # 构造一个没 key 的客户端 = 每轮 run 都稳定 401，然后被折成 `http_error` 降级。
            # 那看起来像"上游持续故障"，实际是装配错误；在构造点炸掉才查得出来。
            raise ConfigError(
                "LLM_API_KEY 为空时不应构造 OpenAI 兼容客户端：装配层必须装 None（explain 会跳过）"
            )
        self._config = config
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            # httpx 的四段超时全部压到同一个预算：旁路没有"连接可以慢慢来"的理由。
            timeout=httpx.Timeout(config.timeout_s)
        )

    @property
    def config(self) -> LlmConfig:
        """当前配置（**不含**任何可打印的凭据处理逻辑，调用方不要拿它去打日志）。"""
        return self._config

    @property
    def endpoint(self) -> str:
        """完整的 `/chat/completions` 地址。"""
        return f"{self._config.base_url}/chat/completions"

    async def aclose(self) -> None:
        """关掉自己持有的连接池（幂等；注入的 `http_client` 一律不动）。"""
        if self._owns_http:
            await self._http.aclose()

    async def explain(self, request: ExplainRequest) -> ExplainResult:
        """发一次解释请求；任何失败都折成降级结果（见模块 docstring 的四类 reason）。

        保证：返回值的 id 集合 ⊆ `request.anomaly_ids`；本方法不抛业务异常、不重试。
        不保证：不保证成功——降级是正常返回值。
        调用方坑：`payload` 必须是已脱敏的 JSON 原生结构（`json.dumps` 会在调用点抛 `TypeError`，
            那属于调用方 bug，不被折成降级）。
        """
        body = {
            "model": self._config.model,
            # 温度 0：同一份输入要得到同一套说法。解释是给人看的旁注，"有创意"在这里毫无价值，
            # 反而会让排障时无法判断"两轮说法不同"是数据变了还是模型抖了。
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    # 键排序 + 不转义中文：同一份 payload 永远得到同一段文本（可复现、可 diff）。
                    "content": json.dumps(
                        dict(request.payload), ensure_ascii=False, sort_keys=True
                    ),
                },
            ],
        }
        try:
            # 墙钟上界（见模块 docstring：httpx 的 read 超时是每块预算，挡不住慢速滴答）。
            async with asyncio.timeout(self._config.timeout_s):
                response = await self._http.post(
                    self.endpoint,
                    json=body,
                    headers={
                        # 每次现拼：凭据不落在任何长期存活的对象上（也不进 repr/dataclass）。
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                raw_text = response.text
        except (TimeoutError, httpx.TimeoutException) as exc:
            # `TimeoutError` 是 `asyncio.timeout` 的出口（3.11+ 与 asyncio.TimeoutError 同一个类）；
            # httpx.TimeoutException 覆盖 connect/read/write/pool 四段。
            return ExplainResult.degraded(DEGRADE_TIMEOUT, type(exc).__name__)
        except httpx.HTTPError as exc:
            # 含 `HTTPStatusError`（非 2xx）、`ConnectError`、`TransportError` 等。
            # detail 只给"哪一类 + 状态码"，**不给响应体**：响应体是外部原文，进日志等于把它带进
            # 我们自己唯一的可信通道。
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = type(exc).__name__ + (f":{status}" if status is not None else "")
            return ExplainResult.degraded(DEGRADE_HTTP_ERROR, detail)

        content = _content_of(raw_text)
        if content is None:
            # 200 但不是 chat/completions 的形状：网关改了协议 / 返回了别的东西。
            return ExplainResult.degraded(DEGRADE_BAD_JSON, "no_message_content")
        try:
            parsed = json.loads(_strip_fence(content))
        except ValueError as exc:
            return ExplainResult.degraded(DEGRADE_BAD_JSON, type(exc).__name__)
        try:
            # `model_validate` 而不是 `parse_raw`：传 dict，不再做一次字符串解析。
            payload = _Explanations.model_validate(parsed)
        except ValidationError as exc:
            return ExplainResult.degraded(DEGRADE_SCHEMA_MISMATCH, _schema_detail(exc))

        allowed = frozenset(request.anomaly_ids)
        explanations, dropped_explanations = _keep_known_ids(payload.explanations, allowed)
        suggestions, dropped_suggestions = _keep_known_ids(payload.suggestions, allowed)
        return ExplainResult.success(
            explanations=explanations,
            suggestions=suggestions,
            dropped=dropped_explanations + dropped_suggestions,
        )


# --------------------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------------------
def build_llm_client(config: LlmConfig) -> LlmClient | None:
    """按配置装配一个客户端；**没配 key 就返回 `None`**（装配层据此让 `explain` 跳过）。

    为什么"没 key"返回 `None` 而不是"返回一个会降级的客户端"：这两种状态在审计里长得完全
    一样（`llm.degraded(http_error)`），但含义天差地别——一个是"没配"，一个是"配了但坏了"。
    用一个 `None` 把"没配"固定在装配层，`explain` 才能写出正确的 `llm.skipped`。

    保证：返回 `None` 或一个 `LlmClient`；`config` 非法（URL/超时）在 `config_from_env` 就更早抛。
    不保证：不保证客户端可用（没有网络也会构造成功——第一次调用才降级）。
    调用方坑：返回的客户端持有连接池，装配层必须在关停时 `aclose()`；**不要**在多处各建一个。
    """
    if not config.enabled:
        return None
    return OpenAiCompatibleLlmClient(config)
