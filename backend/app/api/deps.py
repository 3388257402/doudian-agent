"""路由层依赖：进程级设置、同一 `(shop_id, run_id)` 的并发闸门。

为什么并发闸门在**入口层**而不是图里：`run_agent` 的幂等检查是"先查 `agent_traces` 再
`insert-if-absent` 再 `ainvoke`"，两步之间不是原子的；两个 HTTP 请求同时打进来，第二个的
`insert_run_trace` 返回 `False`（行已存在）但代码仍会继续跑图 —— 同一份采集/改价被执行两次。
`idempotency_key` 挡得住"重复改价"，挡不住"重复采集+重复提议+重复覆盖 run 状态"。
所以这道闸必须挂在请求入口，键就是 `(shop_id, run_id)`（§7.1 `RUN_IN_PROGRESS` 的定义）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request

from app.config import Settings
from app.errors import RunInProgress

__all__ = ["InFlightRuns", "app_settings", "inflight_runs"]


class InFlightRuns:
    """进程内的"正在跑"集合（一个 app 一个实例，挂在 `app.state.inflight`）。

    保证：同一 `(shop_id, run_id)` 在**同一进程**内至多一个持有者。
    不保证：挡不住跨进程/多 worker 的并发（那需要 DB 级互斥，当前未实现）。
    调用方坑：它只持有"字符串键"，不做任何归一化——`ABC` 与 `abc` 是两个 run
        （归一化会让两个不同 run 互相顶掉对方）。
    """

    def __init__(self) -> None:
        # `asyncio.Lock` 只在**判重与登记**那一小段持有：图执行是秒级，
        # 持锁跑完会把所有 run 串行化（那等于没有并发）。
        self._lock = asyncio.Lock()
        self._keys: set[tuple[str, str]] = set()

    @property
    def keys(self) -> frozenset[tuple[str, str]]:
        """当前正在跑的键（只读快照；测试与 `/api/healthz` 之外的诊断用）。

        保证：返回的是**快照**（frozenset），调用方拿到后集合不会自己变化。
        不保证：不保证与"这一瞬间真实在跑"完全同步（复制之后可能有新的进入/退出）。
        调用方坑：只用于诊断/断言，不要拿它做"能不能开跑"的判断（判据在 `hold` 里）。
        """
        return frozenset(self._keys)

    @asynccontextmanager
    async def hold(self, *, shop_id: str, run_id: str) -> AsyncIterator[None]:
        """占住 `(shop_id, run_id)`；已被占用 → `RunInProgress`(409)。

        输入边界：键是**原样字符串**的 `(shop_id, run_id)`，不做 strip/大小写归一化
            （"ABC" 与 "abc" 是两个 run，归一化会让两个不同 run 互相顶掉对方）。
        错误：`RunInProgress`（→ §7.1 包络 409），由调用方（路由）决定是不是这个码。
        调用方坑：
            - 这是**进程内**闸门：多 worker / 多实例部署时挡不住跨进程并发，"同一 run 被两个进程同时
              跑图"仍需数据库级互斥（当前未实现——部署请单实例或按 shop 分片）；
            - 异常路径也会在 `finally` 里释放：否则一次 502 就把这个 `run_id` 永久锁死，后续永远 409。

        保证：进入 `with` 后该键属于你；退出（含异常）后一定被释放。
        不保证：不保证跨进程互斥（多 worker 部署需要数据库级互斥，当前未实现）。
        调用方坑：`raise` 发生在**持锁期间**，所以"已被占用"这条路径不会把键留在集合里
            （根本没 add）。
        """
        key = (shop_id, run_id)
        async with self._lock:
            # 只在**集合**上做判重，不在 await 期间持锁：图执行是秒级，持锁会把所有 run 串行化。
            # 判重 + 登记在同一段锁内完成，中间没有 await，所以两个并发请求不会都通过。
            if key in self._keys:
                raise RunInProgress(
                    f"同一 (shop_id, run_id) 正在运行：{key[1]}",
                    details={"shop_id": shop_id, "run_id": run_id},
                )
            self._keys.add(key)
        try:
            yield
        finally:
            # 异常路径也必须释放：否则一次 502 就把这个 run_id 永久锁死（后续永远 409）。
            # `discard` 而不是 `remove`：重复释放不该抛 KeyError（关停路径说不清顺序）。
            async with self._lock:
                self._keys.discard(key)


def app_settings(request: Request) -> Settings:
    """`create_app(settings)` 注入的设置（测试可换成 tmp 库/日志，见 `tests/api/conftest.py`）。

    保证：返回的是真实例（`isinstance` 校验过），不是 `app.state` 上的任意值。
    不保证：不保证它与 `get_settings()` 相同——测试常常注入不同的那份。
    调用方坑：没装配时抛 `RuntimeError`（→ 500）。这是"忘了用 create_app 建应用"的早期信号，
        不该被降级成默认配置。
    """
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        # 不是"退化成默认配置"，而是直接炸：没有 settings 的 app 一定不是 create_app 造的，
        # 那样连库/日志都指向错误的路径，继续服务只会更难查。
        raise RuntimeError("app.state.settings 未装配：先用 create_app(settings) 建应用")
    return settings


def inflight_runs(request: Request) -> InFlightRuns:
    """同一应用的并发闸门实例。

    保证：一个 app 实例一个（挂在 `app.state.inflight`），测试之间不会互相锁住对方的 run_id。
    不保证：不保证跨进程可见（见 `InFlightRuns.hold` 的说明）。
    调用方坑：没装配时抛 `RuntimeError`——与 `app_settings` 同一条硬要求。
    """
    gate = getattr(request.app.state, "inflight", None)
    if not isinstance(gate, InFlightRuns):
        # 同上：装错了就炸。"没有闸门"意味着并发请求会同时跑同一轮图（重复改价）。
        raise RuntimeError("app.state.inflight 未装配：先用 create_app(settings) 建应用")
    return gate
