#!/usr/bin/env python
"""从**一次真实运行**导出演示用事件流（不手写）。

流程：

1. 子进程跑一遍 `run_demo.py --run-id <本次生成>`（采集→指标→检测→审批→执行→效果窗口）；
2. 读 `data/demo/logs/app.jsonl`，只留 `run_id == 本次` 的行；
3. **只留 `backend/app` 真的有生产者的事件名**（`httpx` / `langgraph.*` 这类第三方 logger
   记录不是我们的事件，它们混在同一个文件里是 `JsonlLogSink` 的既有行为）；
4. 出口闸门：写盘前扫描"像凭据"的串（`access_token=` / `sign=` / `Bearer …` …），
   命中就**拒绝写盘**——那种东西一旦进了提交历史就删不掉了；
5. 断言链路完整（8 个节点的 `node.trace` + `run.finished`），再落成
   `logs_export.jsonl`（一行一条，按 `seq` 升序）+ `logs_export.meta.json`（生成命令与集合）。

什么时候要重跑：**改了事件名就重跑**。判定"事件名有没有漂"用
`check_event_names.py`（它断言文件里的每个事件名都能在 `backend/app` 找到生产者）。

    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\export_event_log.py

退出码：`0` 成功；`3` 源日志里像凭据（拒绝写盘）；`4` 链路不完整；`5` demo 自身失败。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from event_names import (  # noqa: E402
    producer_names,
    scan_credentials,
    unknown_events,
)

#: 一次完整 run 的节点序列（§4.2）。少一个就说明链路没走完，导出的事件流会骗人。
EXPECTED_NODES: tuple[str, ...] = (
    "collect",
    "compute_metrics",
    "detect_anomalies",
    "propose_actions",
    "approval_gate",
    "execute",
    "record_effect",
    "finalize",
)

#: JSONL 里我们自己的事件名分两类：
#:
#: 1. `node.trace`（8 个节点各一条，+ 审批重放一条）——节点级事实源；
#: 2. 审计出口事件（v1.5.1 起）：`metrics.computed` / `anomaly.detected` / `sku_costs.unmatched` /
#:    `collect.units_degraded` / `collect.zero_revenue_paid` / `collect.row_rejected`，以及采集的两个
#:    **结果**事件 `collect.done`（info）/ `collect.failed`（error，`data.reason` 区分
#:    `batch_contract` 与 `all_rows_rejected`）。它们过去**只**进 `state.audit`（checkpoint），
#:    于是 `loss_making` 这类只告警不给动作的规则在日志页上完全不存在，整批失败的采集也和
#:    "上游 503"长得一模一样。
#:
#: "链路走完"的判据仍然是**8 个节点的 node.trace 都在**：审计事件是"事实的可见性"，
#: 不是"节点跑了"的判据（采集失败时 `collect.done` 就不会出现，而 `collect` 的 node.trace 仍在）。

#: 本文件在 `backend/tests/fixtures/demo/`；parents[3] 是仓库根
REPO_ROOT = HERE.parents[3]
DEMO_SCRIPT = HERE / "run_demo.py"
SOURCE_LOG = REPO_ROOT / "data" / "demo" / "logs" / "app.jsonl"

#: `frontend/mock/logs.json` 的来源：**三次真实 demo 运行**，不是手写。
#:
#: 三个场景必须都留着，因为前端 fixtures 的职责就是"三种 run 长什么样"：
#: 成功（含一次挂起与恢复）/ 挂起后无人裁决 / 整批契约坏（`collect.failed`）。
#: `run_id` 写死而不是每次生成：前端断言（verify-suite / dom-check）要引用具体那个 run
#: 的步数；写死之后"重导一次"不会让断言跟着漂。
MOCK_RUNS: tuple[tuple[str, str, bool], ...] = (
    ("run_20261023T142530Z_9f3c1a7b", "full", False),
    ("run_20261023T090000Z_1a2b3c4d", "pending", True),
    ("run_20261023T080000Z_5e6f7a8b", "collect-failure", True),
)

#: `--mock-out` 的默认落点（仓库内相对路径：导出即覆盖前端 fixtures）
DEFAULT_MOCK_OUT = REPO_ROOT / "frontend" / "mock" / "logs.json"


def _read_source_log() -> list[dict[str, Any]]:
    if not SOURCE_LOG.is_file():
        raise SystemExit(f"找不到源日志：{SOURCE_LOG}（demo 没写日志？）")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(SOURCE_LOG.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - 日志被外部改坏
            raise SystemExit(f"{SOURCE_LOG}:{number} 不是合法 JSON：{exc}") from exc
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从一次真实 demo 运行导出事件流 fixture")
    parser.add_argument(
        "--out", type=Path, default=HERE / "logs_export.jsonl", help="输出 JSONL 路径"
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="复用某个已存在的 run_id（默认新生成 demo-export-<时间戳>）",
    )
    parser.add_argument(
        "--mock-out",
        type=Path,
        nargs="?",
        const=DEFAULT_MOCK_OUT,
        default=None,
        help=(
            "重新生成前端 fixtures（默认 frontend/mock/logs.json）：按 MOCK_RUNS 跑三次真实"
            " demo（成功 / 挂起 / 整批失败），不手写任何一行"
        ),
    )
    args = parser.parse_args(argv)

    if args.mock_out is not None:
        return _export_mock(args.mock_out)

    run_id = args.run_id or f"demo-export-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    command = [sys.executable, str(DEMO_SCRIPT), "--run-id", run_id]

    print(f"1) 跑一次真实链路：{subprocess.list2cmdline(command)}")
    completed = subprocess.run(command, cwd=str(REPO_ROOT))
    if completed.returncode != 0:
        print(f"!! demo 自身失败（退出码 {completed.returncode}），不导出", file=sys.stderr)
        return 5

    print(f"2) 读源日志并筛出本次 run：{SOURCE_LOG} run_id={run_id}")
    rows = [row for row in _read_source_log() if row.get("run_id") == run_id]
    if not rows:
        print(f"!! 源日志里没有 run_id={run_id} 的记录", file=sys.stderr)
        return 4

    producers = producer_names()
    kept = [row for row in rows if str(row.get("event") or "") in producers]
    dropped = sorted({str(row.get("event")) for row in rows} - producers)

    print(f"3) 事件筛选：本次 {len(rows)} 条 → 保留 {len(kept)} 条")
    if dropped:
        print(f"   丢掉非本仓库事件（第三方 logger 记录）：{', '.join(dropped)}")

    # --- 出口闸门 1：凭据形状 --------------------------------------------------------
    flat = "\n".join(json.dumps(row, ensure_ascii=False) for row in kept)
    leaks = scan_credentials(flat)
    if leaks:
        print(
            f"!! 拒绝写盘：保留的事件里命中凭据形状 {leaks}。\n"
            "   这是脱敏缺陷，不是演示数据的问题——先把 redact（字符串脱敏）修好，再重跑本脚本。",
            file=sys.stderr,
        )
        return 3

    # --- 出口闸门 2：链路完整性 ------------------------------------------------------
    # `node.trace` 的节点名在顶层 `node` 字段（§7.6 的 LogItem），`data` 里没有副本。
    nodes = {
        str(row.get("node") or row.get("data", {}).get("node"))
        for row in kept
        if row.get("event") == "node.trace"
    }
    missing_nodes = [name for name in EXPECTED_NODES if name not in nodes]
    if missing_nodes:
        print(f"!! 链路不完整：缺少这些节点的 node.trace：{missing_nodes}", file=sys.stderr)
        return 4

    # --- 出口闸门 3：事件名必须都有生产者（写盘前自检，别等下游检查器抓）-------------
    missing_producers = unknown_events((str(row.get("event")) for row in kept), producers)
    if missing_producers:  # pragma: no cover - 上面的 kept 已经过滤过，这里只防未来改动
        print(f"!! 保留的事件里有未知事件名：{missing_producers}", file=sys.stderr)
        return 4

    ordered = sorted(kept, key=lambda row: int(row.get("seq") or 0))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in ordered),
        encoding="utf-8",
    )
    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "source_log": str(SOURCE_LOG),
        "command": "python backend/tests/fixtures/demo/export_event_log.py",
        "demo_command": "python backend/tests/fixtures/demo/run_demo.py --run-id " + run_id,
        "clock": "2026-10-23T12:00:00Z（演示固定时钟，与离线夹具的业务日对齐）",
        "events": len(ordered),
        "event_names": sorted({str(row.get("event")) for row in ordered}),
        "nodes": [name for name in EXPECTED_NODES if name in nodes],
        "note": (
            "本文件由脚本导出，**不要手改**。改了事件名就重跑 export_event_log.py；"
            "一致性由 check_event_names.py 断言（每个事件名都必须在 backend/app 有生产者）。"
        ),
    }
    meta_path = args.out.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"4) 写盘：{args.out}（{len(ordered)} 行）+ {meta_path.name}")
    print(f"   事件名：{', '.join(meta['event_names'])}")
    print(f"   节点：{', '.join(meta['nodes'])}")
    print()
    print("下一步（一致性断言，漂了就红）：")
    print(f"   python backend/tests/fixtures/demo/check_event_names.py {args.out.as_posix()}")
    # 自检一次，让"导出即通过"成为命令的结论而不是人的判断
    return main_check(args.out)


def _node_names_by_run(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """每个 run 的 `node.trace` 节点名（按 seq 升序，含重复的审批重放）。"""
    ordered = sorted(rows, key=lambda row: int(row.get("seq") or 0))
    grouped: dict[str, list[str]] = {}
    for row in ordered:
        if row.get("event") != "node.trace":
            continue
        run_id = str(row.get("run_id") or "")
        grouped.setdefault(run_id, []).append(str(row.get("node") or ""))
    return grouped


def _export_mock(out: Path) -> int:
    """按 `MOCK_RUNS` 跑三次真实 demo，产出 `frontend/mock/logs.json`（§7.6 的响应形状）。

    为什么不用 `logs_export.jsonl` 抄一份：前端那一份的职责是"三种 run 长什么样"——成功、
    挂起后无人裁决、整批契约坏；单跑一次 demo 只有第一种。所以这里按场景跑三次，再把
    **只属于这三个 run_id** 的行合并（启动期的 `app.started` 之类不是"某个 run 的事件"，
    `run_id` 为 null，天然被筛掉）。

    为什么从第二个场景起用 `--keep-data`：第一个场景必须先清掉上次的 `data/demo`
    （动作幂等键不含 run_id，留着旧库会让这次执行变成 `skipped_duplicate`，演示就残了）；
    后两个必须保留——它们要往同一个 `app.jsonl` 里**追加**，否则只剩最后一次的日志。
    """
    run_ids = {run_id for run_id, _, _ in MOCK_RUNS}
    for index, (run_id, scenario, keep) in enumerate(MOCK_RUNS, start=1):
        command = [
            sys.executable,
            str(DEMO_SCRIPT),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ]
        if keep:
            command.append("--keep-data")
        print(f"1.{index}) 跑一次真实链路（场景 {scenario}）：{subprocess.list2cmdline(command)}")
        completed = subprocess.run(command, cwd=str(REPO_ROOT))
        if completed.returncode != 0:
            print(
                f"!! demo 场景 {scenario} 失败（退出码 {completed.returncode}），不导出",
                file=sys.stderr,
            )
            return 5

    print(f"2) 读源日志并筛出这三个 run：{SOURCE_LOG}")
    rows = [row for row in _read_source_log() if str(row.get("run_id") or "") in run_ids]
    if not rows:
        print(f"!! 源日志里没有这些 run 的记录：{sorted(run_ids)}", file=sys.stderr)
        return 4

    producers = producer_names()
    kept = [row for row in rows if str(row.get("event") or "") in producers]
    dropped = sorted({str(row.get("event")) for row in rows} - producers)
    print(f"3) 事件筛选：三个 run 共 {len(rows)} 条 → 保留 {len(kept)} 条")
    if dropped:
        print(f"   丢掉非本仓库事件（第三方 logger 记录）：{', '.join(dropped)}")

    # --- 出口闸门 1：凭据形状（与 JSONL 导出同一道闸门）--------------------------------
    flat = "\n".join(json.dumps(row, ensure_ascii=False) for row in kept)
    leaks = scan_credentials(flat)
    if leaks:
        print(
            f"!! 拒绝写盘：保留的事件里命中凭据形状 {leaks}。\n"
            "   这是脱敏缺陷，不是演示数据的问题——先把 redact 修好，再重跑本脚本。",
            file=sys.stderr,
        )
        return 3

    # --- 出口闸门 2：三个场景必须各自留下真实的事件流 -----------------------------------
    by_run = _node_names_by_run(kept)
    absent = [run_id for run_id in sorted(run_ids) if not by_run.get(run_id)]
    if absent:
        print(f"!! 这些 run 没有任何 node.trace：{absent}（场景没跑到图上）", file=sys.stderr)
        return 4
    success_run = MOCK_RUNS[0][0]
    missing_nodes = [name for name in EXPECTED_NODES if name not in by_run[success_run]]
    if missing_nodes:
        print(f"!! 成功场景的链路不完整：缺少这些节点的 node.trace：{missing_nodes}", file=sys.stderr)
        return 4
    failure_run = MOCK_RUNS[-1][0]
    failure_rows = [
        row for row in kept if row.get("run_id") == failure_run and row.get("event") == "collect.failed"
    ]
    if not failure_rows or failure_rows[0].get("level") != "error":
        print(
            f"!! 失败场景里没有 level=error 的 collect.failed（实际 {failure_rows}）："
            "前端 fixture 的 error 态就失去依据了",
            file=sys.stderr,
        )
        return 4

    # --- 落盘：`/api/logs` 的响应形状（items 按 seq DESC，next_cursor 为 null） ---------
    ordered = sorted(kept, key=lambda row: int(row.get("seq") or 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": list(reversed(ordered)), "next_cursor": None}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"4) 写盘：{out}（{len(ordered)} 行，{len(run_ids)} 个 run）")
    for run_id, scenario, _keep in MOCK_RUNS:
        print(f"   {run_id} [{scenario}] node.trace={len(by_run[run_id])}")
    print(f"   事件名：{', '.join(sorted({str(row.get('event')) for row in ordered}))}")
    print()
    print("下一步（一致性断言，漂了就红）：")
    print(f"   python backend/tests/fixtures/demo/check_event_names.py {out.as_posix()}")
    return main_check(out)


def main_check(path: Path) -> int:
    checker = HERE / "check_event_names.py"
    completed = subprocess.run([sys.executable, str(checker), str(path)], cwd=str(REPO_ROOT))
    return 0 if completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
