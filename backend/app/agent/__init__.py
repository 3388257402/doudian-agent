"""agent 包：只做 re-export（契约 §1），不含任何逻辑。

## 为什么用模块级 `__getattr__`

`app/agent/__init__.py` 一旦在导入时就 `from app.agent.graph import ...`，就会出现
`app.services.anomalies → app.agent.state → app.agent/__init__ → graph → nodes →
app.services.anomalies（还没定义完）` 这样的环。用 PEP 562 的模块级 `__getattr__` 把
"取属性"推迟到真正访问时，`from app.agent import run_agent` 仍然是契约要求的唯一写法，
但 `import app.agent.state` 不会连带把整张图拉起来。这是 re-export 的实现细节，不是逻辑。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

#: 公开名字 → 定义它的子模块
_EXPORTS: dict[str, str] = {
    "build_graph": "app.agent.graph",
    "list_pending_approvals": "app.agent.graph",
    "resume_agent": "app.agent.graph",
    "run_agent": "app.agent.graph",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
