#!/usr/bin/env python
"""一致性断言：**文件里出现的每个事件名，必须在 `backend/app` 里找得到生产者**。

这是"演示数据不靠手写"的机器判据。它替代的是"下次审查再抓一遍"：

    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\check_event_names.py
    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\check_event_names.py frontend/mock/logs.json
    .venv\\Scripts\\python.exe backend\\tests\\fixtures\\demo\\check_event_names.py backend/tests/fixtures/demo/logs_export.jsonl

退出码：`0` = 全部事件都有生产者且不含凭据形状；`1` = 有事件名漂移；`2` = 文件里像凭据。

顺带（不计入退出码）报告"实现会产出、但该文件里没出现"的事件名——用于发现**漏演示**，
与漂移是两个方向：漂移是"演示了不存在的事实"，漏演示是"存在的事实没演示"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from event_names import (  # noqa: E402
    BACKEND_APP_DIR,
    extract_events_from_file,
    producer_names,
    scan_credentials,
    unknown_events,
)

DEFAULT_TARGET = HERE / "logs_export.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="断言文件里的事件名都能在 backend/app 找到生产者（演示数据不许手写）"
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help=f"要检查的 JSON / JSONL 文件（默认 {DEFAULT_TARGET.name}）",
    )
    parser.add_argument(
        "--quiet-missing",
        action="store_true",
        help="不打印「实现有、文件没有」的事件（只看漂移）",
    )
    args = parser.parse_args(argv)

    targets: list[Path] = list(args.files) or [DEFAULT_TARGET]
    for target in targets:
        if not target.is_file():
            print(f"!! 找不到文件：{target}", file=sys.stderr)
            return 2

    producers = producer_names()
    if not producers:
        print(f"!! 在 {BACKEND_APP_DIR} 里没扫到任何事件名生产者，检查器自身可疑", file=sys.stderr)
        return 2

    status = 0
    for target in targets:
        text = target.read_text(encoding="utf-8", errors="replace")
        events = extract_events_from_file(target)
        missing = unknown_events(events, producers)
        leaks = scan_credentials(text)
        seen = set(events)

        print(f"== {target}")
        print(f"   事件 {len(events)} 条 / 事件名 {len(seen)} 个；backend/app 生产者 {len(producers)} 个")
        if missing:
            status = 1
            print(f"   ✗ 没有生产者的事件名 {len(missing)} 个（漂移）：")
            for name in missing:
                print(f"       - {name}")
        else:
            print("   ✓ 所有事件名都能在 backend/app 里找到生产者")
        if leaks:
            status = max(status, 2)
            print(f"   ✗ 命中凭据形状 {len(leaks)} 处：{', '.join(leaks)}")
        if not args.quiet_missing:
            untouched = sorted(producers - seen)
            if untouched:
                print(f"   · 实现会产出、本文件未演示 {len(untouched)} 个：{', '.join(untouched)}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
