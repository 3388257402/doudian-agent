"""入口层的冻结常量（只放"一句话说得清、被多处引用"的值）。

`API_VERSION` 同时出现在 `/api/healthz` 的响应与 FastAPI 的 OpenAPI 元信息里：
两个地方各写一份，迟早会出现"探活说 0.1.0、OpenAPI 说 0.1.1"的分裂。
"""

from __future__ import annotations

__all__ = ["API_VERSION"]

#: 服务版本（`/api/healthz` 与 OpenAPI 共用）
API_VERSION = "0.1.0"
