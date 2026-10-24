#!/usr/bin/env python
"""demo 模式：**零凭据、零外网**跑通全链路（采集 → 指标 → 检测 → 审批 → 执行 → 回测）。

为什么这个脚本放在 `backend/tests/fixtures/demo/`：

- 它唯一的输入是 `backend/tests/fixtures/doudian/cassettes/` 里的离线夹具（`dry_run=true` 时
  `DoudianClient` 注入的就是这份目录，见 `app/services/doudian_client.py` 的
  `DEFAULT_CASSETTE_DIR`）。脚本与它回放的夹具放在一起，"删掉 demo = 删掉一个目录"。
- 它**不是**测试（pytest 只收 `test_*.py`），也**不改任何实现代码**：走的全是实现层已经暴露的
  公开入口——`create_app()` 的 lifespan（建表 / 日志汇 / 运行时 / 检查点）+ §7 的 5 个 HTTP 端点
  + `run_effect_evaluator_once()`。也就是说：demo 能跑通 = 别人 clone 下来能跑通同一条链。

时钟为什么固定在 2026-10-23：离线夹具里的订单都是那一天的业务日。演示用固定时钟，
换任何一天跑出来的数字都一样（可复现），而不是"今天恰好没数据于是 demo 空白"。

用法（在仓库根目录）：

    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\run_demo.py
    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\run_demo.py --verdict reject
    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\run_demo.py --scenario pending
    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\run_demo.py --scenario collect-failure

`--scenario` 只影响"这次演示跑到哪、以及上游给什么"，走的仍然是实现层的同一条链路
（`POST /api/agent/run` → 图 → `finalize`）：

- `full`（默认）：采集 → 指标 → 检测 → 审批（interrupt/resume）→ 执行 → 效果窗口；
- `pending`：只跑到审批挂起就停（"等人工裁决"这一态的真实事件流）；
- `collect-failure`：把订单接口的响应换成"上游把 `data` 结构改坏了"，于是 `collect` 按
  **整批契约坏**硬失败（`status=failed`，不 500），产生一条真实的 `collect.failed` 红行。
  它存在的理由：失败态的事件流过去在演示数据里是手写的，改一次事件名就漂一次。

退出码：0 = 本次场景跑通；2 = 链路在预期之外停住（贴在输出里的位置就是断点）。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# --- 先让 `import app.*` 可用（不依赖 CWD、不依赖 PYTHONPATH）---------------------------
HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[3]  # backend/
REPO_ROOT = HERE.parents[4]  # 仓库根
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent import adapters  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.enums import RunStatus  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import EffectSnapshot  # noqa: E402
from app.services.effects import run_effect_evaluator_once  # noqa: E402

__all__ = ["DEMO_NOW", "DEMO_SHOP_ID", "main"]

#: 演示用的"现在"：UTC 2026-10-23 12:00 = 上海 20:00 → 业务日期 2026-10-23（夹具的订单日）
DEMO_NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
DEMO_SHOP_ID = "12345678"
DEMO_OPERATOR = "demo_ops"

#: §7.2 的 `run_id` 字符集（`^[A-Za-z0-9_-]{1,64}$`）——在本地先拦，免得打到 API 才 400
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: demo 自己的运行目录（在 `data/` 下，已被 .gitignore 忽略；不污染开发库 `data/doudian.db`）
DEMO_DATA_DIR = REPO_ROOT / "data" / "demo"
CASSETTE_DIR = BACKEND_ROOT / "tests" / "fixtures" / "doudian" / "cassettes"


# --------------------------------------------------------------------------------------
# 输出小工具（不引第三方表格库：demo 要能在只有 requirements.txt 的环境里跑）
# --------------------------------------------------------------------------------------
def _rule(char: str = "=") -> None:
    print(char * 78)


def _step(index: int, total: int, title: str) -> None:
    print()
    _rule("-")
    print(f"[{index}/{total}] {title}")
    _rule("-")


def _kv(label: str, value: Any) -> None:
    print(f"  {label:<14}{value}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _fail(message: str) -> int:
    print()
    print(f"!! demo 中断：{message}")
    return 2


# --------------------------------------------------------------------------------------
# 配置：用真实 settings.yaml，只把"库 / 检查点 / 日志"改到 data/demo 下
# --------------------------------------------------------------------------------------
def _demo_settings(*, keep_data: bool = False) -> Any:
    base = load_settings()
    if not keep_data and DEMO_DATA_DIR.exists():
        # 为什么要清：动作的幂等键 = f(shop, action_type, target, params)（ADR-006），**不含 run_id**。
        # 第二次跑同一个演示时，执行节点会正确地把这次动作判成 `skipped_duplicate` 而不再登记
        # 效果窗口——那是对的，但演示要看的是完整链路，所以每次从干净的库开始。
        shutil.rmtree(DEMO_DATA_DIR, ignore_errors=True)
    DEMO_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DEMO_DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    return dataclasses.replace(
        base,
        database=dataclasses.replace(
            base.database,
            # 绝对路径：从哪个目录执行 demo 都落在同一个地方（SQLite 的相对路径是按 CWD 解析的）
            url=f"sqlite+pysqlite:///{(DEMO_DATA_DIR / 'doudian.db').as_posix()}",
        ),
        checkpoint=dataclasses.replace(
            base.checkpoint, type="sqlite", path=str(DEMO_DATA_DIR / "checkpoint.db")
        ),
        observability=dataclasses.replace(
            base.observability, log_file=str(DEMO_DATA_DIR / "logs" / "app.jsonl")
        ),
    )


def _break_order_response(client: Any) -> None:
    """把 `search_orders` 换成"上游把 `data` 结构改坏了"的响应（其余方法原样转发）。

    为什么用实例属性覆盖而不是新写一个 client：`collect` 走的是**同一条**真实路径
    （`_aggregate_orders` → `_require_orders_container` → `_BatchContractError` →
    `status=failed` → `finalize`），这里只替换"外部世界给回来的那份 JSON"，与离线 cassette
    回放的定位一致。换成 mock 掉节点/图，演示出来的 `collect.failed` 就不是真事件了。

    返回的 `DoudianResponse` 用真实数据类而不是 `SimpleNamespace`：`_note_call` 会读
    `request_id` / `ok`，任何一个字段缺失都会把"结构坏"演示成"属性错"。
    """
    from app.services.doudian_client import DoudianResponse

    async def _malformed(**_kwargs: Any) -> DoudianResponse:
        return DoudianResponse(
            ok=True,
            err_no=0,
            message="ok",
            data={"oops": 1},
            http_status=200,
            request_id="demo-req-malformed-batch",
            raw={"data": {"oops": 1}},
            attempts=1,
        )

    client.search_orders = _malformed  # type: ignore[method-assign]


async def _run(
    *, verdict: str, run_id: str | None = None, keep_data: bool = False, scenario: str = "full"
) -> int:
    settings = _demo_settings(keep_data=keep_data)
    if settings.is_prod or not settings.doudian.dry_run:
        return _fail(
            "demo 需要 dev + doudian.dry_run=true（当前 "
            f"app.env={settings.app.env}, doudian.dry_run={settings.doudian.dry_run}）。"
            "把 backend/config/settings.yaml 改回默认值再试。"
        )
    if not CASSETTE_DIR.is_dir():
        return _fail(f"找不到离线夹具目录：{CASSETTE_DIR}")

    run_id = run_id or f"demo-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    if not _RUN_ID.fullmatch(run_id):
        return _fail(f"--run-id 必须匹配 {_RUN_ID.pattern}（API 的字符集约束），收到 {run_id!r}")

    _rule()
    print("doudian-agent · demo（零凭据 / 离线夹具 / 固定时钟 2026-10-23 20:00 Asia/Shanghai）")
    _rule()
    _kv("业务日期", "2026-10-23")
    _kv("店铺", f"{DEMO_SHOP_ID}（夹具里的「示例旗舰店」）")
    _kv("上游模式", "dry_run=true → CassetteTransport 回放 order.searchList / product.listV2 …")
    _kv("本次 run", run_id)
    _kv("数据目录", f"{DEMO_DATA_DIR}（每次运行前清空；--keep-data 保留）")
    _kv("夹具目录", str(CASSETTE_DIR))

    app = create_app(settings)
    total = 6
    async with app.router.lifespan_context(app):
        # lifespan 已按配置建好运行时（dry_run → 夹具 client）。这里只把时钟换成固定钟：
        # 业务日期、executed_at、效果窗口的 due_at 全部由它决定，演示才可复现。
        wired = adapters.get_runtime()
        adapters.set_runtime(
            adapters.AgentRuntime(
                settings=wired.settings,
                client=wired.client,
                notifier=wired.notifier,
                rules=wired.rules,
                clock=lambda: DEMO_NOW,
            )
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://demo.local"
        ) as http:
            # --- 1) 触发 run --------------------------------------------------------
            _step(1, total, "触发 run：POST /api/agent/run（采集 → 指标 → 检测 → 待审批）")
            if scenario == "collect-failure":
                # 只替换"上游给回来的那份 JSON"：结构坏了 → 整批契约错 → failed（不是 500）。
                _break_order_response(wired.client)
                print("  场景 collect-failure：订单接口的 data 被换成 {\"oops\": 1}（模拟上游改结构）")
            response = await http.post(
                "/api/agent/run",
                json={"shop_id": DEMO_SHOP_ID, "run_id": run_id, "trigger": "manual"},
            )
            body = _body_of(response)
            if response.status_code != 202:
                return _fail(f"POST /api/agent/run 返回 {response.status_code}：{_json(body)}")
            pending = body.get("pending_approvals") or []
            status_1 = body["status"]
            print(f"  HTTP {response.status_code} status={status_1} trace_id={body['trace_id']}")
            _kv("thread_id", body["thread_id"])
            if scenario == "collect-failure":
                # 判据：整批失败是"202 + status=failed"（契约 §7.2：不可重试的业务/结构错误
                # 不许报 502，也不许 500），并且**没有**待审批动作。
                if status_1 != RunStatus.FAILED.value or pending:
                    return _fail(
                        f"预期整批失败（status=failed 且无待办），实际 status={status_1} "
                        f"pending={len(pending)}"
                    )
                print("  → collect 判定整批不可用：本轮 status=failed，无审批、无副作用。")
                print("    事件流里应有一条 level=error 的 collect.failed（reason=batch_contract）。")
                print()
                print("场景跑通：整批契约坏 → collect.failed 红行 + node.trace(outcome=error)。")
                return 0
            if status_1 != "awaiting_approval" or not pending:
                return _fail(
                    f"预期停在 awaiting_approval 且有 1 条待办，实际 status={status_1} "
                    f"pending={len(pending)}。"
                    "（多半是 config/rules.yaml 被改过：demo 依赖 refund_spike 规则）"
                )
            print("  → 图停在 approval_gate：涉及资金/库存的动作必须人工裁决，不自动放行。")
            if scenario == "pending":
                # 刻意不裁决：这一档演示的是"等人工裁决"的真实事件流（挂起后没有人恢复）。
                print()
                print("场景跑通：run 停在 awaiting_approval（不做裁决，线程保持挂起）。")
                return 0

            # --- 2) 审批台 ----------------------------------------------------------
            _step(2, total, "读审批台：GET /api/alerts（数据源是 approval_request 表，不是内存）")
            alerts_response = await http.get(
                "/api/alerts", params={"shop_id": DEMO_SHOP_ID, "status": "pending", "limit": 20}
            )
            alerts = _body_of(alerts_response).get("items") or []
            print(f"  HTTP {alerts_response.status_code} 待办 {len(alerts)} 条")
            for item in alerts:
                if item.get("run_id") != run_id:
                    continue  # 上次 demo 遗留的待办（没裁决就跑掉了）：不是本次的事实
                _kv("action_id", item["action_id"])
                _kv("动作", f"{item['action_type']} → {item['target']}")
                _kv("参数", _json(item["params"]))
                _kv("风险", item["risk"])
                _kv("理由", item["reason"])
                _kv("证据", _json(item["evidence"]))
                _kv("interrupt_id", item["interrupt_id"])

            # --- 3) 人工裁决 --------------------------------------------------------
            _step(
                3,
                total,
                f"人工裁决：POST /api/approve（operator={DEMO_OPERATOR}, verdict={verdict}）",
            )
            decisions = [
                {
                    "action_id": item["action_id"],
                    "verdict": verdict,
                    "edited_params": None,
                    "operator": DEMO_OPERATOR,
                    "reason": "demo：按规则建议执行" if verdict == "approve" else "demo：人工否决",
                }
                for item in pending
            ]
            approve_response = await http.post(
                "/api/approve",
                json={
                    "thread_id": body["thread_id"],
                    "interrupt_id": pending[0]["interrupt_id"],
                    "operator": DEMO_OPERATOR,
                    "decisions": decisions,
                },
            )
            approved = _body_of(approve_response)
            print(f"  HTTP {approve_response.status_code} status={approved.get('status')}")
            if approve_response.status_code != 200:
                return _fail(f"POST /api/approve 失败：{_json(approved)}")
            for applied in approved.get("applied") or []:
                _kv("applied", _json(applied))
            report = approved.get("effect_report") or {}
            if report:
                _kv("归因口径", report.get("attribution"))
                _kv("报告结论", report.get("conclusion"))

            # --- 4) 回测（效果评估）-------------------------------------------------
            _step(
                4,
                total,
                "回测：run_effect_evaluator_once()（图内不等 T+1/T+3/T+7，由调度器到点评估）",
            )
            now = DEMO_NOW + timedelta(days=1)  # 直接把时钟推到 T+1：T+0 与 T+1 两个窗口到期
            with session_scope() as session:
                evaluated = await run_effect_evaluator_once(
                    session=session, client=wired.client, now=now
                )
                rows = list(
                    session.execute(
                        select(EffectSnapshot)
                        .where(EffectSnapshot.run_id == run_id)
                        .order_by(EffectSnapshot.offset_days.asc())
                    ).scalars()
                )
                print(f"  评估时间推到 {now.isoformat()}（T+1），本次评估 {evaluated} 个窗口")
                if not rows:
                    print("  （没有登记任何观察窗：动作没有执行成功时不会登记，见 record_effect）")
                for row in rows:
                    print(
                        f"  T+{row.offset_days:<2} status={row.status:<10} "
                        f"due={row.due_at.isoformat()} delta={_json(row.delta or {})}"
                    )
                if rows:
                    print(f"  caveats: {_json(list(rows[0].caveats or []))}")

            # --- 5) 看板数据 --------------------------------------------------------
            _step(5, total, "看板数据：GET /api/metrics（金额单位：分）")
            metrics_response = await http.get(
                "/api/metrics",
                params={
                    "shop_id": DEMO_SHOP_ID,
                    "date_from": "2026-10-16",
                    "date_to": "2026-10-23",
                },
            )
            if metrics_response.status_code == 404:
                print("  HTTP 404：这个店铺还没有指标数据（先跑一次 run 再看）")
            else:
                points = _body_of(metrics_response).get("points") or []
                print(f"  HTTP {metrics_response.status_code} 共 {len(points)} 个业务日")
                header = (
                    f"  {'date':<12}{'gmv(分)':>10}{'订单':>6}{'支付':>6}"
                    f"{'退款单':>7}{'aov':>8}{'退款率':>9}{'uv':>6}{'cvr':>8}"
                )
                print(header)
                for point in points:
                    print(
                        f"  {point['date']:<12}{point['gmv_cents']:>10}{point['order_count']:>6}"
                        f"{point['paid_order_count']:>6}{point['refund_order_count']:>7}"
                        f"{point['aov_cents']:>8}"
                        f"{'-' if point['refund_rate'] is None else round(point['refund_rate'], 4)!s:>9}"
                        f"{'-' if point['uv'] is None else point['uv']!s:>6}"
                        f"{'-' if point['cvr'] is None else round(point['cvr'], 4)!s:>8}"
                    )
                print("  注：uv/cvr 为 '-' 表示上游没返回访客数（null ≠ 0，见 README 已知限制）")

            # --- 6) 执行日志 --------------------------------------------------------
            _step(6, total, "执行日志：GET /api/logs（节点级事实源是 JSONL，不是表）")
            logs_response = await http.get(
                "/api/logs", params={"run_id": run_id, "limit": 50}
            )
            items = _body_of(logs_response).get("items") or []
            print(f"  HTTP {logs_response.status_code} 本次 run 共 {len(items)} 条事件")
            for item in sorted(items, key=lambda row: row["seq"]):
                node = item.get("node") or "-"
                print(f"  #{item['seq']:<4} {item['level']:<5} {item['event']:<26} node={node}")

            # --- 收尾 --------------------------------------------------------------
            health = await http.get("/api/healthz")
            _rule()
            print(f"healthz → HTTP {health.status_code} {_json(_body_of(health))}")
            print()
            print("全链路跑通：采集 → 指标 → 检测 → 审批（interrupt/resume）→ 执行 → 效果窗口。")
            print("同一个 run_id 再打一次 POST /api/agent/run 会回放已存结果（200，不重跑图）。")
            print()
            print("接下来（真实服务 + 页面）：")
            print("  .venv\\Scripts\\python.exe -m uvicorn app.main:app --app-dir backend --port 8000")
            print("  cd frontend && npm install && npm run dev   # http://localhost:5173")
            print("重来一遍：删掉 data/demo 目录（或换一个 --run-id）。")
            _rule()
    return 0


def _body_of(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:  # pragma: no cover - 非 JSON 响应说明链路坏了
        return {"raw": response.text}
    return payload if isinstance(payload, dict) else {"raw": payload}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="doudian-agent demo：零凭据跑通 采集→指标→检测→审批→执行→回测"
    )
    parser.add_argument(
        "--verdict",
        choices=("approve", "reject"),
        default="approve",
        help="人工裁决结果（默认 approve；reject 演示「否决后不写 action_log」）",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="指定 run_id（默认 demo-<UTC 时间戳>；同一个 run_id 再跑会回放而不是重跑）",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="保留 data/demo 里上次运行的库与日志（默认每次清空，保证演示可复现）",
    )
    parser.add_argument(
        "--scenario",
        choices=("full", "pending", "collect-failure"),
        default="full",
        help=(
            "演示到哪：full=全链路（默认）；pending=只到审批挂起就停；"
            "collect-failure=订单响应结构坏 → 整批契约失败（collect.failed）"
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            verdict=args.verdict,
            run_id=args.run_id,
            keep_data=args.keep_data,
            scenario=args.scenario,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
