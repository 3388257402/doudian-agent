"""`GET /api/alerts` —— 审批台首屏（CONTRACT §7.3）。

**数据源是持久化的 `approval_request` 表，不是内存里的 state**：审批台可能在进程重启之后才
被打开（这才是 HITL 的意义），从内存读会让"重启一次，待办全空"。ADR-002 也明确：本表是
`checkpoint` 的**索引**，列表查询只走它，不必把每个 thread 的图状态读一遍。

分页是 **keyset**（`id < before_id`）而不是 `OFFSET`：OFFSET 在"审批不断新增/裁决"的表上会
跳行/重复行（翻页期间前面插了新行），而 keyset 只看"上一页最后一条的 id"。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api import queries
from app.api.convert import iso_z
from app.schemas import (
    SHOP_ID_PATTERN,
    AlertItem,
    AlertList,
    AlertStatusFilter,
    ErrorEnvelope,
)

__all__ = ["router"]

router = APIRouter(tags=["alerts"])


@router.get(
    "/alerts",
    response_model=AlertList,
    summary="列出审批提议（默认只看 pending）",
    responses={400: {"model": ErrorEnvelope}},
)
async def list_alerts(
    shop_id: str | None = Query(default=None, pattern=SHOP_ID_PATTERN, description="过滤店铺"),
    status: AlertStatusFilter = Query(
        default="pending", description="pending/approved/rejected/expired/all"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(
        default=None, ge=1, description="keyset 分页：取 id < before_id"
    ),
) -> AlertList:
    """列出一页审批提议（审批台首屏）。

    输入边界：
        - `shop_id` 可选，`^[A-Za-z0-9_-]{1,64}$`（与本服务接受的 run 同字符集，不是"任意字符串过滤"）；
        - `status` 默认 `pending`，取值 `pending/approved/rejected/expired/all`（`all` = 不过滤）；
        - `limit` 1..200（默认 50）；`before_id` 必须是**上一页 `next_cursor` 的整数**，`0`/负数 400；
        - 排序固定 `created_at DESC, id DESC`，不接受调用方指定排序（换排序键就等于让前端的分页游标失效）。

    错误：只有 `400 VALIDATION_ERROR`（参数越界/枚举外/`shop_id` 字符集不符）。
          本端点不返回 404/409/502：**没有数据不是错误**，是 `200 + items=[] + next_cursor=null`。

    调用方坑：
        - `next_cursor` 是**不透明游标**（字符串形式的行 id），只许原样回传给 `before_id`，不许自己加减；
        - 别用 `len(items) == limit` 判断"还有下一页"：最后一页刚好整页时 `next_cursor` 也是 `null`
          （本端点是多取一条 `limit+1` 来判断的，不是看这一页的长度）；
        - 本端点读 `approval_request` 表（持久化索引），**不读 checkpointer**：thread 已被清理时这里仍可能
          列出"点不动的待办"——列表照样 200，点审批才 404（§7.4）。这是有意的取舍：列表必须能在进程
          重启后工作，代价由 `approve` 的 404 承担。
    """
    rows = await queries.list_approval_page(
        status=None if status == "all" else status,
        shop_id=shop_id,
        limit=limit + 1,
        before_id=before_id,
    )
    page = rows[:limit]
    has_more = len(rows) > limit
    items = [
        AlertItem.model_validate(
            {key: (iso_z(value) if key == "created_at" else value)
             for key, value in row.items() if key != "id"}
        )
        for row in page
    ]
    return AlertList(
        items=items,
        next_cursor=str(page[-1]["id"]) if has_more and page else None,
    )
