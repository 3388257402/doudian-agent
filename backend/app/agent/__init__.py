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
    # 四个名字全在 `graph`（§4.1 的公开入口）：包本身不实现任何逻辑，
    # 只负责"从哪拿"。加新入口就加一行，不要在 `__init__` 里写包装函数。
    "build_graph": "app.agent.graph",
    "list_pending_approvals": "app.agent.graph",
    "resume_agent": "app.agent.graph",
    "run_agent": "app.agent.graph",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """按需导入并返回公开名字（PEP 562 的模块级 `__getattr__`）。

    保证：`from app.agent import run_agent` 这类写法仍然可用，且**此时才**导入 `graph`。
    不保证：不认识的属性会抛 `AttributeError`（不是返回 None），因此 `hasattr` 语义正常。
    调用方坑：这是模块级钩子，只在属性**没被定义**时触发；一旦 `graph` 被导入过，
        后续访问走正常的模块字典（所以不要依赖"每次都重新导入"）。
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)


def __dir__() -> list[str]:
    """让 `dir(app.agent)` 只列出公开名字（不含 `_EXPORTS` 等实现细节）。

    保证：返回值与 `__all__` 一致且已排序（`dir()` 的约定是返回列表）。
    不保证：不触发任何导入（`dir()` 不该有副作用）。
    调用方坑：REPL 的补全会用它，所以这里多列一个名字等于对外多承诺一个 API。
    """
    return sorted(__all__)
