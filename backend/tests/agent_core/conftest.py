"""agent_core 测试的公共夹具：临时库 + 临时日志汇 + 可编排的假抖店 client。

三条原则（决定了这些夹具的样子）：

1. **零网络、零凭据**：所有上游调用都打到 :class:`FakeDoudianClient`，由用例编排返回值和异常。
   不 monkeypatch `httpx`（那会和 §10.1 的"注入 transport 优先"打架）。
2. **零共享状态**：每个用例一个 SQLite 文件、一个 checkpoint 文件、一个 JSONL 文件。
   用例之间不靠"顺序"通过，因此可以单独跑、可以乱序跑。
3. **真实边界**：DB 走真 SQLAlchemy 引擎、checkpoint 走 `AsyncSqliteSaver`、规则走真 YAML
   解析与真 AST 求值。只替换"外部世界"（抖店、通知、时钟），不替换"被测逻辑"。
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 由 pytest 的 pythonpath 兜住
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agent import adapters  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.db import create_engine_from_settings, init_db, reset_engine  # noqa: E402
from app.observability.logging import JsonlLogSink, get_logger  # noqa: E402
from app.services.actions import action_id_for  # noqa: E402
from app.services.errors import (  # noqa: E402
    DoudianBusinessError,
    DoudianError,
    DoudianUpstreamError,
)
from app.services.notifier import DryRunNotifier  # noqa: E402
from app.services.rules import load_rules  # noqa: E402

#: 固定时钟（UTC）：业务日期 = 2026-10-23（Asia/Shanghai 20:00）
FIXED_NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
TODAY = "2026-10-23"
SHOP_ID = "12345678"


@dataclass
class FakeResponse:
    """`DoudianResponse` 的最小等价物（节点只读 `.data` / `.request_id` / `.ok`）。"""

    data: Any = None
    request_id: str | None = "req-0001"
    ok: bool = True


@dataclass
class FakeDoudianClient:
    """可编排的抖店 client：记录每次调用、按脚本抛错、可断言"实际改了什么"。"""

    shop: dict[str, Any] = field(
        default_factory=lambda: {"shop_id": SHOP_ID, "shop_name": "示例旗舰店"}
    )
    products: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "product_id": "9001",
                "sku_id": "3400000000000000002",
                "title": "示例商品",
                "price": 9900,
                "stock": 20,
                "gmv_cents": 900_000,
                "refund_order_count": 0,
                "is_on_sale": True,
            }
        ]
    )
    orders: list[dict[str, Any]] = field(default_factory=list)
    #: 上游 `order.searchList` 响应里的 `data.summary`（抖店真实接口不返回 uv，这里是夹具约定：
    #: 传了就用作"这段汇总"的声明，`collect` 按作用域决定能不能当当日 uv，见 nodes._day_uv）
    order_summary: dict[str, Any] | None = None
    #: method 名 → 依次抛出的异常队列（用完后正常返回）
    failures: dict[str, list[BaseException]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    price_updates: list[dict[str, Any]] = field(default_factory=list)
    stock_updates: list[dict[str, Any]] = field(default_factory=list)

    # --- 编排 ---
    def fail(self, method: str, exc: BaseException, times: int = 1) -> None:
        self.failures.setdefault(method, []).extend([exc] * times)

    def _maybe_raise(self, method: str) -> None:
        queue = self.failures.get(method)
        if queue:
            raise queue.pop(0)

    # --- 上游接口（与 DoudianClient 同签名）---
    async def get_shop_info(self) -> FakeResponse:
        self.calls.append("shop.getShopInfo")
        self._maybe_raise("shop.getShopInfo")
        return FakeResponse(data=dict(self.shop))

    async def list_products(self, *, page: int = 0, size: int = 50) -> FakeResponse:
        self.calls.append("product.listV2")
        self._maybe_raise("product.listV2")
        return FakeResponse(data={"products": [dict(item) for item in self.products]})

    async def search_orders(
        self, *, start_time: str, end_time: str, page: int = 0, size: int = 50
    ) -> FakeResponse:
        self.calls.append("order.searchList")
        self._maybe_raise("order.searchList")
        data: dict[str, Any] = {"shop_order_list": [dict(item) for item in self.orders]}
        if self.order_summary is not None:
            data["summary"] = dict(self.order_summary)
        return FakeResponse(data=data)

    async def edit_price(self, *, product_id: str, sku_id: str, price_cents: int) -> FakeResponse:
        self.calls.append("product.editPrice")
        self._maybe_raise("product.editPrice")
        self.price_updates.append(
            {"product_id": product_id, "sku_id": sku_id, "price_cents": price_cents}
        )
        return FakeResponse(data={"updated": True}, request_id="req-edit-1")

    async def update_stock(self, *, product_id: str, sku_id: str, stock: int) -> FakeResponse:
        self.calls.append("product.editStock")
        self._maybe_raise("product.editStock")
        self.stock_updates.append(
            {"product_id": product_id, "sku_id": sku_id, "stock": stock}
        )
        return FakeResponse(data={"updated": True}, request_id="req-stock-1")

    async def aclose(self) -> None:  # pragma: no cover - 假 client 无需释放
        return None


# --------------------------------------------------------------------------------------
# 订单数据的构造工具
# --------------------------------------------------------------------------------------


def order(
    *,
    order_id: str,
    create_time: str,
    pay_amount_cents: int,
    refund_amount_cents: int = 0,
    refund_status: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "order_id": order_id,
        "create_time": create_time,
        "pay_time": create_time if pay_amount_cents > 0 else None,
        "pay_amount": pay_amount_cents,
    }
    if refund_amount_cents or refund_status:
        item["refund_amount"] = refund_amount_cents
        item["refund_status"] = refund_status
    return item


def history_then_today(
    *, history_gmv: int = 1_000_000, today_gmv: int = 100_000, days: int = 7
) -> list[dict[str, Any]]:
    """近 7 天 + 今天各一单：历史稳定、今天可低可高（决定规则是否触发）。"""
    rows: list[dict[str, Any]] = []
    base_day = datetime(2026, 10, 16, tzinfo=UTC)
    for offset in range(days):
        day = (base_day.replace(day=16 + offset)).strftime("%Y-%m-%d")
        rows.append(
            order(order_id=f"h{offset}", create_time=f"{day} 12:00:00", pay_amount_cents=history_gmv)
        )
    rows.append(
        order(order_id="today", create_time=f"{TODAY} 12:00:00", pay_amount_cents=today_gmv)
    )
    return rows


# --------------------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """把库/检查点/日志全部指到 tmp 的 Settings（并让 get_settings() 返回它）。

    直接 `dataclasses.replace` 而不是重写一份 yaml：测的是**同一套**配置解析结果，
    只有"落在哪个文件"这几个字段被替换——那些正是与产品行为无关的部署细节。
    """
    import app.config as config_module

    base = load_settings()
    resolved = dataclasses.replace(
        base,
        database=dataclasses.replace(base.database, url=f"sqlite+pysqlite:///{tmp_path}/doudian.db"),
        checkpoint=dataclasses.replace(base.checkpoint, type="sqlite", path=str(tmp_path / "checkpoint.db")),
        observability=dataclasses.replace(base.observability, log_file=str(tmp_path / "app.jsonl")),
        # 重试预算压到最短：测的是"重试了几次、对哪类错误重试"，不是真的等 3 秒退避。
        doudian=dataclasses.replace(
            base.doudian,
            rate_limit=dataclasses.replace(
                base.doudian.rate_limit,
                max_attempts=3,
                backoff_base_s=0.01,
                backoff_max_s=0.02,
            ),
        ),
    )
    monkeypatch.setattr(config_module, "_settings", resolved, raising=False)
    yield resolved
    config_module.reset_settings_cache()


@pytest.fixture()
def db(settings: Settings) -> Iterator[None]:
    """建好 tmp SQLite 的四张表，用完丢引擎。"""
    engine = create_engine_from_settings(settings)
    init_db(engine)
    yield
    reset_engine()


@pytest.fixture()
def sink(settings: Settings) -> Iterator[JsonlLogSink]:
    """装配进程级 JSONL 汇（节点明细的事实源）。"""
    resolved = JsonlLogSink(
        settings.observability.log_file,
        redact_keys=settings.observability.redact_keys,
    )
    adapters.set_log_sink(resolved)
    yield resolved
    adapters.set_log_sink(None)
    resolved.close()


@pytest.fixture()
def fake_client() -> FakeDoudianClient:
    return FakeDoudianClient(orders=history_then_today())


@pytest.fixture()
def rules_path(tmp_path: Path) -> Path:
    """一条确定性的规则（当天 GMV 低于近 7 日均值 70% → 降价 3%）。

    行为用例用这份**临时**规则而不是仓里的 `config/rules.yaml`：仓里那份会随运营调参变化，
    测试不该因为"多了一条规则"而变红。`config/rules.yaml` 另有一个"能被加载"的用例守着。
    """
    path = tmp_path / "rules.yaml"
    path.write_text(
        """
version: "1"
rules:
  - id: gmv_drop_day
    enabled: true
    severity: warn
    metric: gmv_cents
    when: "gmv_cents < gmv_7d_avg_cents * 0.7"
    window: day
    description: "当日 GMV 低于近 7 日均值的 70%"
    actions:
      - type: price_update
        target_from: top_gmv_sku
        params: {price_drop_pct: 3}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def runtime(settings: Settings, sink: JsonlLogSink, db: None, fake_client: FakeDoudianClient,
            rules_path: Path) -> Iterator[adapters.AgentRuntime]:
    """进程级运行时：假 client + dry-run 通知 + 临时规则集。

    依赖 `db`／`sink` 是刻意的：任何用到运行时的用例都必然要落库与写 JSONL，让夹具串起来
    比在 20 个用例里各写一遍 `db` 参数可靠（漏写一次就是 "session factory 未初始化"）。
    """
    resolved = adapters.AgentRuntime(
        settings=settings,
        client=fake_client,
        notifier=DryRunNotifier(logger=get_logger("tests.agent_core"), sink=sink),
        rules=load_rules(rules_path),
        clock=lambda: FIXED_NOW,
    )
    adapters.set_runtime(resolved)
    yield resolved
    adapters.reset_runtime()


@pytest.fixture()
def repo_rules() -> Any:
    """仓里真实的 `config/rules.yaml`（只用于"能被加载"的断言）。"""
    return load_rules()


# --------------------------------------------------------------------------------------
# 断言用的小工具
# --------------------------------------------------------------------------------------


def node_events(sink: JsonlLogSink, run_id: str) -> list[dict[str, Any]]:
    """某 run 的全部节点级事件（按 seq 升序）。"""
    rows = sink.read(run_id=run_id, event_prefix="node.trace", limit=500)
    return sorted(rows, key=lambda item: item["seq"])


def select_scalar(sql: str, **params: Any) -> Any:
    """用例里做"库里到底有几行"这类断言（绕开 ORM，直接看事实）。"""
    from sqlalchemy import text

    from app.db import run_db

    def _fn(session: Any) -> Any:
        return session.execute(text(sql), params).scalar()

    return run_db(_fn)


def approval_rows(thread_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db import run_db
    from app.models import ApprovalRequest

    def _fn(session: Any) -> list[dict[str, Any]]:
        rows = session.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.thread_id == thread_id)
            .order_by(ApprovalRequest.id)
        ).scalars()
        return [
            {
                "action_id": row.action_id,
                "status": row.status,
                "verdict": row.verdict,
                "interrupt_id": row.interrupt_id,
                "params": dict(row.params or {}),
                "payload_digest": row.payload_digest,
            }
            for row in rows
        ]

    return run_db(_fn)


def action_log_rows(run_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db import run_db
    from app.models import ActionLog

    def _fn(session: Any) -> list[dict[str, Any]]:
        rows = session.execute(
            select(ActionLog).where(ActionLog.run_id == run_id).order_by(ActionLog.id)
        ).scalars()
        return [
            {
                "action_log_id": row.action_log_id,
                "idempotency_key": row.idempotency_key,
                "status": row.status,
                "attempt": row.attempt,
                "error_category": row.error_category,
            }
            for row in rows
        ]

    return run_db(_fn)


def effect_rows(run_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db import run_db
    from app.models import EffectSnapshot

    def _fn(session: Any) -> list[dict[str, Any]]:
        rows = session.execute(
            select(EffectSnapshot)
            .where(EffectSnapshot.run_id == run_id)
            .order_by(EffectSnapshot.offset_days)
        ).scalars()
        return [
            {
                "snapshot_id": row.snapshot_id,
                "offset_days": int(row.offset_days),
                "status": row.status,
                "due_at": row.due_at,
            }
            for row in rows
        ]

    return run_db(_fn)


async def trace_row(shop_id: str, run_id: str) -> dict[str, Any]:
    """`agent_traces` 的 run 摘要行（用例里 `await` 它，用 `(await trace_row(...))["k"]`）。

    为什么是 async 而不是在内部 `asyncio.run(...)`：调用点全在 **async 用例内部**，那时事件
    循环已经在跑，`asyncio.run()` 会直接抛 `RuntimeError: asyncio.run() cannot be called from
    a running event loop`，并且留下 `coroutine ... was never awaited` 警告——helper 的缺陷会
    伪装成"被测逻辑出错"。不引入 `nest_asyncio` 之类的补丁：那只是把一个测试侧的调用方式
    错误藏起来，掩盖了"async 用例只能 await，不能另起 event loop"这一事实。
    """
    from app.agent.tracing import get_run_trace

    return await get_run_trace(shop_id=shop_id, run_id=run_id) or {}


def expected_action_id(run_id: str, action_type: str, target: str) -> str:
    return action_id_for(run_id=run_id, action_type=action_type, target=target)


def decisions_for(action_ids: Sequence[str], *, verdict: str = "approve") -> list[dict[str, Any]]:
    return [
        {
            "action_id": action_id,
            "verdict": verdict,
            "edited_params": None,
            "operator": "ops_zhang",
            "reason": None,
            "decided_at": "2026-10-23T12:05:00Z",
        }
        for action_id in action_ids
    ]


__all__ = [
    "FIXED_NOW",
    "SHOP_ID",
    "TODAY",
    "DoudianBusinessError",
    "DoudianUpstreamError",
    "DoudianError",
    "FakeDoudianClient",
    "FakeResponse",
    "action_log_rows",
    "approval_rows",
    "decisions_for",
    "effect_rows",
    "expected_action_id",
    "history_then_today",
    "node_events",
    "order",
    "select_scalar",
    "trace_row",
]
