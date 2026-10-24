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

#: JSONL 里我们自己的事件名只有 `node.trace` 一族（其它事件走 state 的 `audit` 进库，
#: 不走 JSONL）——所以"链路走完"的判据是**8 个节点的 node.trace 都在**，不是某个收尾事件名。
#: 别在这里发明一个 `run.finished`：JSONL 里没有它（它只在 state.audit 里）。

#: 本文件在 `backend/tests/fixtures/demo/`；parents[3] 是仓库根
REPO_ROOT = HERE.parents[3]
DEMO_SCRIPT = HERE / "run_demo.py"
SOURCE_LOG = REPO_ROOT / "data" / "demo" / "logs" / "app.jsonl"


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
    args = parser.parse_args(argv)

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


def main_check(path: Path) -> int:
    checker = HERE / "check_event_names.py"
    completed = subprocess.run([sys.executable, str(checker), str(path)], cwd=str(REPO_ROOT))
    return 0 if completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
