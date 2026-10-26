"""`GET /api/logs` —— 执行日志页的数据源（CONTRACT §7.6）。

数据来自 JSONL 日志汇（`settings.observability.log_file`），**不是**表：
节点事件是高频 append-only 流，查询形态是"某 run 最近 N 条"（ADR-015）。

分页同样走 keyset（`seq < before_seq`），`next_cursor` 是最后一条 `seq` 的字符串形式。
`seq` 由写入端在进程启动时从文件末尾续上，因此跨重启单调递增——重复的 `seq` 会让前端
分页丢行，这一条由 `JsonlLogSink` 保证，本层不做二次编号。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from app.agent.adapters import get_log_sink
from app.schemas import ErrorEnvelope, LogItem, LogLevel, LogList

__all__ = ["router"]

router = APIRouter(tags=["logs"])


@router.get(
    "/logs",
    response_model=LogList,
    summary="执行日志（seq DESC，event 前缀匹配）",
    responses={400: {"model": ErrorEnvelope}},
)
async def list_logs(
    run_id: str | None = Query(default=None),
    # `level` 用 Literal：枚举外的值由 FastAPI 直接 400，不会走到 sink 查询。
    level: LogLevel | None = Query(default=None),
    event: str | None = Query(default=None, description="事件名前缀，如 action."),
    limit: int = Query(default=100, ge=1, le=500),
    # 严格小于：传上一页 `next_cursor` 的 int 值即可，不会把上一页最后一条再取一次。
    before_seq: int | None = Query(default=None, ge=1, description="取 seq < before_seq"),
) -> LogList:
    """查询执行日志（JSONL 日志汇，`seq DESC`）。

    输入边界：
        - `run_id`/`level`/`event` 均可选；`level` ∈ `debug/info/warn/error`；
        - `event` 是**前缀**匹配（`action.` 命中 `action.executed`），不是模糊/正则/大小写不敏感匹配；
        - `limit` 1..500（默认 100）；`before_seq` 取**严格小于**它的行（把上一页 `next_cursor` 转 int 传来）；
        - 排序固定 `seq DESC`（最新在前），不支持升序：要时间序列请在前端反转。

    保证：返回的每一行都过了 `LogItem` 校验（§7.6 的 10 个字段齐全）。
    不保证：不保证 `level=info` 就等于"没出事"——挂起也是 info（ADR-016），判据是 `data.outcome`。

    错误：只有 `400 VALIDATION_ERROR`（`limit` 越界/`level` 枚举外/`before_seq <= 0`）。
          查不到数据不是错误：`200 + items=[] + next_cursor=null`。

    调用方坑：
        - `data.outcome` 才是"这个节点成功/挂起/失败"的判据：挂起 = `interrupted` + `level=info`
          （§7.6.1 / ADR-016）；**禁止**用 `level=="error"` 或 `data.error` 反推挂起态——那正是 v1.1.0
          把正常暂停画成故障的成因；
        - `seq` 是进程内单调写入序号（启动时从文件末尾续上），不是时间戳也不是全局序号：
          用 `seq` 分页、用 `ts` 显示；
        - 日志汇是文件不是表：文件被清空/轮转后按 `before_seq` 翻页会"什么都没有"，属预期；
        - 读取会全量扫文件（dev 规模）；别在轮询里把 `limit` 调大刷，`/api/logs` 不是指标接口。
    """
    # 这里必须用 `get_log_sink()` + 显式抛错，**不能**用 `require_log_sink()`：
    # 后者名为 require 实则"拿不到就 refresh_log_sink() 就地新建"（adapters.py:101-110），
    # 于是每个读请求都可能 mkdir + 全量扫文件续 seq（阻塞 IO，且没走 to_thread）
    # + 建出第二个 sink 实例、各自持有独立 `_seq` —— 直接破坏 §7.6 的
    # "seq 跨重启单调递增"不变量。读接口不该有写副作用。
    sink = get_log_sink()
    if sink is None:
        # 装配漏了就要炸：返回空列表会把"装配错误"伪装成"没发生过事"。
        # 500 在这里是**对的**：lifespan 没跑，是本服务的 bug，不是查询的问题。
        raise RuntimeError("日志汇未装配：lifespan 未执行（§7.6 / ADR-015）")

    # 全量扫描读文件是阻塞 IO：丢到线程池，别在事件循环里读盘（与 `arun_db` 同一理由）。
    # 多取一条（`limit + 1`）判断"还有没有下一页"，与 `/api/alerts` 同一套分页手法。
    rows = await asyncio.to_thread(
        sink.read,
        run_id=run_id,
        level=level,
        event_prefix=event,
        limit=limit + 1,
        before_seq=before_seq,
    )
    page = rows[:limit]
    has_more = len(rows) > limit
    # 逐行过 `LogItem`：写入端已经保证形状，这里再校验一次是**读出口**的契约保证
    # （老日志文件里可能有历史版本的字段，缺了会让 pydantic 报错而不是静默丢行）。
    items = [LogItem.model_validate(row) for row in page]
    return LogList(
        items=items,
        # 游标是最后一条的 `seq`；`has_more and page` 两个条件都要（空页时 `page[-1]` 会 IndexError）。
        next_cursor=str(page[-1]["seq"]) if has_more and page else None,
    )
