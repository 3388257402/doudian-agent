# demo 模式的数据从哪来（别在这里造第二套）

`run_demo.py` **不内联任何业务数据**。它跑的是真实链路：

```
create_app() 的 lifespan  →  DoudianClient(dry_run=true)  →  CassetteTransport
                                                                   ↑
                                  backend/tests/fixtures/doudian/cassettes/*.json
```

也就是说：demo 唯一的数据源是 `backend/tests/fixtures/doudian/cassettes/`（和 API 层测试、
`pytest` 用的是同一批夹具）。本目录只有**一个文件**——跑演示的脚本。

## 为什么这样做

| 做法 | 后果 |
|---|---|
| demo 里内联一份"好看的"数据 | 两边字段一改就对不上，demo 变成第二个事实源，谁都不知道哪个才是真的 |
| demo 读夹具目录（本方案） | 夹具改字段，demo 自动跟着变；demo 跑通 = 别人 clone 下来跑通同一条链 |

## 加一条 demo 数据 = 加一个 cassette

在 `backend/tests/fixtures/doudian/cassettes/` 下放 `*.json`，文件名决定匹配顺序（字典序）：

```json
{
  "name": "orders_today_ok",
  "match": {"method": "order.searchList", "body_contains": {}},
  "response": {"status": 200, "headers": {"x-tt-logid": "..."},
               "json": {"err_no": 0, "message": "success", "data": {"orders": [...] }}},
  "delay_ms": 0
}
```

`match.method` 是抖店协议方法名（`shop.getShopInfo` / `product.listV2` / `order.searchList` /
`product.editPrice` / `product.editStock`）；`body_contains` 是请求体子集匹配，`{}` 表示"任何请求"。

## 相关事实

- 时钟：demo 固定 `2026-10-23 12:00 UTC`（= 上海 20:00）。夹具里的订单都在这个业务日，
  所以换任何一天跑，输出都一致。这个值是 `run_demo.py` 里的 `DEMO_NOW`。
- 目录：`data/demo/`（SQLite + checkpoint + JSONL）。已被 `.gitignore` 的 `data/` 忽略。
- 想重来：删 `data/demo/`，或换 `--run-id`。
