"""**子进程**里恢复一个挂起的审批：证明 checkpoint 真的在磁盘上、与进程内存无关。

父进程已经跑完 `run_agent` 并退出图执行；这个进程从零启动，只拿到四个路径
（业务库 / checkpoint 文件 / 日志文件 / 规则文件）与 thread_id，把审批裁决喂回去。
如果 checkpointer 是 `MemorySaver`（或任何进程内状态），这里必然 `THREAD_NOT_FOUND`。

用法（由 `test_restart_resume.py` 调起）：

    python _restart_child.py '<json-config>'

stdout 只输出一行 JSON，便于父进程断言。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class _EdgeClient:
    """只实现恢复路径真正会用到的两个方法（`approval_gate` 重放不会碰上游）。"""

    def __init__(self) -> None:
        self.price_updates: list[dict[str, Any]] = []

    async def edit_price(self, *, product_id: str, sku_id: str, price_cents: int) -> Any:
        self.price_updates.append(
            {"product_id": product_id, "sku_id": sku_id, "price_cents": price_cents}
        )
        return _Resp({"updated": True}, "req-child-1")

    async def update_stock(self, *, product_id: str, sku_id: str, stock: int) -> Any:
        return _Resp({"updated": True}, "req-child-2")

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _Resp:
    def __init__(self, data: Any, request_id: str | None) -> None:
        self.data = data
        self.request_id = request_id
        self.ok = True


def _build_settings(cfg: dict[str, Any]) -> Any:
    import app.config as config_module
    from app.config import load_settings

    base = load_settings()
    resolved = dataclasses.replace(
        base,
        database=dataclasses.replace(base.database, url=cfg["database_url"]),
        checkpoint=dataclasses.replace(
            base.checkpoint, type="sqlite", path=cfg["checkpoint_path"]
        ),
        observability=dataclasses.replace(base.observability, log_file=cfg["log_file"]),
    )
    # 与父进程一致：本进程是新进程，配置缓存本来就是空的，直接写进去即可。
    config_module._settings = resolved
    return resolved


def main(argv: Sequence[str]) -> int:
    cfg = json.loads(argv[1])
    settings = _build_settings(cfg)

    from app.agent import adapters, resume_agent
    from app.db import create_engine_from_settings, init_db
    from app.observability.logging import JsonlLogSink, get_logger
    from app.services.notifier import DryRunNotifier
    from app.services.rules import load_rules

    init_db(create_engine_from_settings(settings))
    sink = JsonlLogSink(
        settings.observability.log_file,
        redact_keys=settings.observability.redact_keys,
    )
    adapters.set_log_sink(sink)
    client = _EdgeClient()
    adapters.set_runtime(
        adapters.AgentRuntime(
            settings=settings,
            client=client,
            notifier=DryRunNotifier(logger=get_logger("tests.restart"), sink=sink),
            rules=load_rules(cfg["rules_path"]),
            clock=lambda: datetime(2026, 10, 23, 12, 10, 0, tzinfo=UTC),
        )
    )

    result = asyncio.run(
        resume_agent(
            cfg["thread_id"],
            cfg["decisions"],
            expected_interrupt_id=cfg.get("interrupt_id"),
        )
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "applied": [
                    {
                        "action_id": item.action_id,
                        "verdict": item.verdict,
                        "status": item.status,
                        "action_log_id": item.action_log_id,
                        "error": item.error,
                    }
                    for item in result.applied
                ],
                "effect_windows": len((result.effect_report or {}).get("windows") or []),
                "price_updates": client.price_updates,
            },
            ensure_ascii=False,
        )
    )
    sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
