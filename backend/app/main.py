"""应用入口 —— `create_app()` + lifespan（CONTRACT §7 / §8.1 / §10.4）。

## 启动顺序（每一步失败都必须让进程起不来，不能带病服务）

1. **配置**：`load_settings()`（yaml + 环境变量）→ `assert_serving_safe()`。
   `prod + doudian.dry_run=true` 直接拒绝启动：那种组合会"接口全 200、上游没改一个字节"，
   属于沉默型事故，只能靠"起不来"逼配置改对（§8.2）。
2. **日志汇**：`JsonlLogSink` + `configure_logging`。先于其它步骤，后面每一步的失败都要留痕。
3. **建表**：`init_db()`（dev `create_all` + SQLite WAL，§6.4）。生产建表由 alembic 负责，
   但这里仍执行一次幂等的 `create_all`，保证"clone 下来直接能跑"。
4. **运行时**：`adapters.set_runtime(build_runtime(settings))` —— 一个进程一个
   `DoudianClient`（ADR-004）、一个规则集、一个 notifier。
5. **checkpointer**：打开并登记进程级 saver（HITL 的持久化基础，§5.7）。
6. **对账**：对每个非终态 run 调一次 `calibrate_run_steps`（= `reconcile_step_count`，§4.7）。
   为什么必须做：进程可能在"节点写日志"和"DB +1"之间被杀；不校准的话
   `count(node.trace) != agent_traces.step_count`，而这条等式是 agent-core 的不变量。

## 启动失败 = 非 0 退出

lifespan 启动阶段抛异常时，uvicorn 会打印 `Application startup failed. Exiting.` 并以非 0
退出（实测 uvicorn 0.54：`sys.exit(STARTUP_FAILURE)`）。所以这里的规矩是：**部分启动的部件
先拆掉，再把异常抛出去**——绝不留一个"半初始化但还在服务"的进程。

## 为什么 `app = create_app()` 放在模块级

`uvicorn app.main:app` 要的就是这个属性。配置错误在**导入期**就炸（进程直接非 0 退出），
而不是等到第一个请求——§8.1 明确禁止"加载失败第一个请求才炸"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent import adapters
from app.agent.checkpoint import checkpoint_saver, set_checkpointer
from app.agent.tracing import calibrate_run_steps
from app.api import API_VERSION, InFlightRuns, TraceIdMiddleware, build_api_router
from app.api.errors import register_error_handlers
from app.api.queries import list_active_runs
from app.config import (
    Settings,
    assert_serving_safe,
    load_settings,
    startup_warnings,
)
from app.db import create_engine_from_settings, init_db, reset_engine, session_scope
from app.observability.logging import JsonlLogSink, configure_logging, get_logger

__all__ = ["create_app", "lifespan_for", "app"]

logger = get_logger("app.main")


class _Lifecycle:
    """启动/停止的实际动作（把"谁在什么时候关掉"写在一处，避免半启动泄漏）。

    只持有**自己创建**的资源：`JsonlLogSink`、引擎、checkpointer 上下文、运行时。
    测试可能 `adapters.set_runtime(...)` 换成假 client，所以这里不通过 `get_runtime()`
    去猜当前运行时（那个函数在未装配时会**新建**一份，关机时新建等于又泄漏一个 client）。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sink: JsonlLogSink | None = None
        self.saver_cm: Any = None
        self.runtime: adapters.AgentRuntime | None = None
        self.running = False

    # --- 启动 ---
    async def startup(self) -> None:
        """按 §8.1 的顺序把进程级设施点亮；任一步失败都向上抛（uvicorn → 非 0 退出）。

        为什么顺序是"日志 → 库 → 运行时 → checkpointer → 对账"：
            1. 日志汇最先：后面任何一步失败都要留痕，否则"起不来"只有一行 traceback、没有结构化证据；
            2. 建表在运行时之前：`build_runtime` 里的规则/通知/客户端都不需要表，但 `run_agent`
               一旦被请求触发就会写 `agent_traces`，所以表必须比"能接请求"更早存在；
            3. checkpointer 在对账之前：对账要写 `step_count`，而"权威状态"在 checkpoint 里，
               没有 saver 时对账只能修索引、修不了事实；
            4. 对账最后：它读 JSONL 与 `agent_traces`，两者都得先就位。
        调用方坑：本方法**不做**幂等（由 `lifespan_for` 保证只在启动时调一次）；
            失败路径由 `lifespan_for` 调 `shutdown()` 收尾，这里不吞异常。
        """
        settings = self.settings
        sink = JsonlLogSink(
            settings.observability.log_file,
            redact_keys=settings.observability.redact_keys,
        )
        self.sink = sink
        configure_logging(sink=sink, level=settings.observability.log_level)
        self._log_startup_warnings()

        engine = create_engine_from_settings(settings)
        init_db(engine)  # 建表 + WAL；engine 由 db 模块持有，关机时 reset_engine() 释放

        adapters.set_log_sink(sink)
        runtime = adapters.build_runtime(settings)
        self.runtime = runtime
        adapters.set_runtime(runtime)

        self.saver_cm = checkpoint_saver(settings)
        saver = await self.saver_cm.__aenter__()
        set_checkpointer(saver)

        await self._reconcile_active_runs(sink)
        self.running = True
        logger.info(
            "应用启动完成：db=%s checkpoint=%s dry_run=%s log_file=%s",
            settings.database.url,
            settings.checkpoint.type,
            settings.doudian.dry_run,
            settings.observability.log_file,
            extra={
                "event": "app.started",
                "data": {
                    "version": API_VERSION,
                    "app_env": settings.app.env,
                    "database_url": settings.database.url,
                    "checkpoint_type": settings.checkpoint.type,
                    "dry_run": settings.doudian.dry_run,
                    "log_file": settings.observability.log_file,
                },
            },
        )

    def _log_startup_warnings(self) -> None:
        """把启动期风险横幅写进**结构化日志**（§8.2：禁止静默）。"""
        for warning in startup_warnings(self.settings):
            logger.error(
                "启动期配置风险：%s",
                warning,
                extra={
                    "event": "config.startup_warning",
                    "data": {
                        "app_env": self.settings.app.env,
                        "dry_run": self.settings.doudian.dry_run,
                    },
                },
            )

    async def _reconcile_active_runs(self, sink: JsonlLogSink) -> None:
        """对每个非终态 run 校准 `step_count`（§4.7）。

        单个 run 对账失败只记 warn 不阻断启动：一个坏 run 不该让整个服务起不来，
        但也不能静默——两条规则都在这里。
        """
        try:
            rows = await list_active_runs()
        except Exception:  # noqa: BLE001 - 列表都读不到 = 库还没准备好：这是启动失败
            logger.exception("读取非终态 run 失败（启动中止）")
            raise
        for row in rows:
            try:
                with session_scope() as session:
                    await calibrate_run_steps(
                        session=session, run_id=row["run_id"], sink=sink
                    )
            except Exception:  # noqa: BLE001 - 单个 run 的问题不阻断启动
                logger.warning(
                    "run %s 的 step_count 对账失败（跳过，不阻断启动）",
                    row["run_id"],
                    extra={"event": "app.reconcile_failed", "run_id": row["run_id"]},
                )
        if rows:
            logger.info(
                "启动对账完成：%d 个非终态 run",
                len(rows),
                extra={"event": "app.reconciled", "data": {"runs": len(rows)}},
            )

    # --- 停止（幂等：启动失败时也会被调用一次）---
    async def shutdown(self) -> None:
        """按"依赖的反序"释放自己创建的设施；重复调用安全（字段置 None 当哨兵）。

        为什么每个关闭动作都各自 `try/except`：启动失败时也会走到这里，此时**真正的错误**
        是 startup 抛出的那个；关连接再抛一次会把它盖掉，日志里只剩"关闭失败"。
        关闭顺序与启动相反：checkpointer → 运行时（client）→ 日志汇 → engine。
        日志汇最后关：前面任何一步的失败都还能被记下来。
        调用方坑：`adapters.reset_runtime()` 会清掉**进程级**运行时；如果测试替换过运行时
        （`adapters.set_runtime(fake)`），这里清的是替换后的那个——所以测试要在退出 lifespan
        之前自己决定要不要还原（见 `tests/api/conftest.py`）。
        """
        saver_cm, self.saver_cm = self.saver_cm, None
        if saver_cm is not None:
            set_checkpointer(None)
            try:
                await saver_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 关连接失败不该掩盖真正的启动错误
                logger.exception("关闭 checkpointer 失败")

        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            client = getattr(runtime, "client", None)
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - 同上
                    logger.exception("关闭抖店客户端失败")
        adapters.reset_runtime()

        sink, self.sink = self.sink, None
        adapters.set_log_sink(None)
        if sink is not None:
            sink.close()
        _detach_logging()
        reset_engine()
        self.running = False


def _detach_logging() -> None:
    """摘掉 `configure_logging` 装上的 handler（重复调用幂等）。

    不摘掉的话，`uvicorn --reload` / 同一进程里的多次 `create_app` 会让日志翻倍，
    而且测试之间会互相往对方的 tmp 文件里写（用例顺序一变就红）。
    """
    import logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_doudian_agent", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 关 handler 失败不影响关停
                pass


def lifespan_for(settings: Settings):
    """生成 `FastAPI(lifespan=...)` 需要的上下文管理器（把 settings 绑进去）。

    为什么用**工厂**而不是模块级 `@asynccontextmanager`：`create_app(settings)` 允许注入
    （测试用 tmp 库/日志），lifespan 必须跟着那份 settings 走；写成模块级单例会逼测试去
    patch 全局 `get_settings()`，那正是"测试顺序一变就红"的来源。

    为什么启动失败要"先 shutdown 再 raise"：uvicorn 看到 lifespan 抛异常会打印
    `Application startup failed.` 并 `sys.exit(STARTUP_FAILURE)`（§8.1 要的非 0 退出）；
    但如果不先拆掉已建好的连接/文件句柄，进程在退出前会留下锁文件与半写入的库。
    `BaseException` 而不是 `Exception`：`CancelledError`（部署时被 kill）同样要走收尾。
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        lifecycle = _Lifecycle(settings)
        try:
            await lifecycle.startup()
        except BaseException:
            # 半启动的部件先拆掉再抛：uvicorn 会因此非 0 退出（§8.1）。
            await lifecycle.shutdown()
            raise
        try:
            yield
        finally:
            await lifecycle.shutdown()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。`settings=None` 时按 §8.1 从 yaml + 环境变量加载。

    配置/安全闸在**这里**就执行一次（不等到 lifespan）：`create_app()` 的任何调用方
    （测试、CLI、`uvicorn app.main:app`）都得到同一个结论，不存在"某条启动路径漏了闸"。

    为什么用 `app.state` 而不是模块级全局：一个进程里可能同时存在多个 app 实例
    （`tests/api` 每个用例一个，`--reload` 也会短暂并存）。`settings` 与并发闸门挂在实例上，
    才不会出现"上一个用例的 inflight 集合把下一个用例的 run_id 锁住"。
    调用方坑：本函数**不**启动任何东西（不建表、不装运行时），那些在 lifespan 里；
    只调用 `create_app()` 然后发请求会得到 500（`session factory 未初始化`）——测试必须
    手动进入 `app.router.lifespan_context(app)`（见 `tests/api/conftest.py`）。
    """
    resolved = load_settings() if settings is None else settings
    assert_serving_safe(resolved)

    app = FastAPI(
        title=f"{resolved.app.name} API",
        version=API_VERSION,
        lifespan=lifespan_for(resolved),
    )
    app.state.settings = resolved
    app.state.inflight = InFlightRuns()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.app.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        # 前端从响应头取 trace_id 兜底（§7：每个响应都带它）
        expose_headers=["X-Trace-Id"],
    )
    # 最后 add = 最外层：错误响应（含 CORS 短路、未捕获异常）也必须带 X-Trace-Id
    app.add_middleware(TraceIdMiddleware)

    register_error_handlers(app)
    app.include_router(build_api_router(), prefix=resolved.app.api_prefix)
    return app


#: `uvicorn app.main:app --app-dir backend` 的入口。导入期即完成配置加载与安全闸。
app = create_app()
