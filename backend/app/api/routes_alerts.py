"""`GET /api/alerts` —— 审批台首屏（CONTRACT §7.3）。

**数据源是持久化的 `approval_request` 表，不是内存里的 state**：审批台可能在进程重启之后才
被打开（这才是 HITL 的意义），从内存读会让"重启一次，待办全空"。ADR-002 也明确：本表是
`checkpoint` 的**索引**，列表查询只走它，不必把每个 thread 的图状态读一遍。

分页是 **keyset**（`id < before_id`）而不是 `OFFSET`：OFFSET 在"审批不断新增/裁决"的表上会
跳行/重复行（翻页期间前面插了新行），而 keyset 只看"上一页最后一条的 id"。

## 本端点**不**读 checkpointer（有意的取舍）

列表必须能在进程重启后工作，所以它只读 `approval_request`（§5.7 说这张表是 checkpoint 的
**索引**）。代价是：thread 被清理后，这里仍可能列出"点不动的待办"——列表照样 200，
点审批才 404。把"列表可工作"排在"列表精确"前面，是 HITL 的基本要求：先能看见，再谈点得动。
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

#: 这个路由器只有一个端点；`/api` 前缀由 `app.main.create_app` 统一加
#: （这里写死前缀会让"改 api_prefix"变成动六个文件）。


@router.get(
    "/alerts",
    response_model=AlertList,
    summary="列出审批提议（默认只看 pending）",
    # 只声明 400：这个端点的失败面只有"参数不合法"（没有数据不是错误）。
    responses={400: {"model": ErrorEnvelope}},
)
async def list_alerts(
    # 四个参数全走 Query：这是只读端点，没有请求体（`before_id` 用 GET 的查询串传游标）。
    shop_id: str | None = Query(default=None, pattern=SHOP_ID_PATTERN, description="过滤店铺"),
    status: AlertStatusFilter = Query(
        default="pending", description="pending/approved/rejected/expired/all"
    ),
    # `ge/le` 由 FastAPI 直接转成 400：越界的 `limit` 不会进到 SQL。
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

    保证：只要 200，`items` 里的每一行都通过了 `AlertItem` 的校验（形状/枚举都在契约内）。
    不保证：不保证每一行此刻还能被审批——thread 被清理后照样列出（点审批才 404，见下文坑）。

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
        # `all` 在**这里**翻译成 None（不过滤），而不是把这个字符串传进 SQL：
        # `status = 'all'` 会匹配不到任何行，那是最难发现的一类空列表。
        # 这一层同时是"查询参数取值域 → 仓储参数"的唯一翻译点。
        status=None if status == "all" else status,
        shop_id=shop_id,
        # 多取一条（`limit + 1`）用来判断"还有没有下一页"：省掉一次 `COUNT(*)`，
        # 也避免 COUNT 与结果集之间的竞态。
        limit=limit + 1,
        before_id=before_id,
    )
    # 多取的那一条只用来判断 `has_more`，不返回给调用方（保证每页最多 `limit` 条）。
    page = rows[:limit]
    has_more = len(rows) > limit
    # 逐行转成 `AlertItem`：领域 dict → pydantic 模型（形状/枚举在这里被校验）。
    items = [
        AlertItem.model_validate(
            # `id` 必须剔掉：`AlertItem` 是 `extra="forbid"`，带着它会被 pydantic 拒成 400。
            # 时间过 `iso_z` 统一成 §0 的毫秒 + Z 形状（库里读出来是微秒 + +00:00）。
            {key: (iso_z(value) if key == "created_at" else value)
             for key, value in row.items() if key != "id"}
        )
        for row in page
    ]
    return AlertList(
        items=items,
        # 游标 = 本页最后一条的 `id`（字符串）；没有下一页时给 `None`（不是空串）。
        next_cursor=str(page[-1]["id"]) if has_more and page else None,
    )
