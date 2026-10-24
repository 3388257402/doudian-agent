// Mock 假后端的开关（真实后端没有这些概念）。
//
// 为什么需要它：契约 §7.5 的 MetricPoint **没有库存字段**，所以"库存周转"按契约算不出来
// （成本/毛利率自 v1.5.0 起已是契约字段，不再需要开关）。两个都不诚实的做法是：
//   a) 在 fixtures 里塞上库存字段，让看板看起来什么都能画（等于宣称契约支持）；
//   b) 把这张卡删掉（等于假装任务没要求）。
// 这里选第三种：默认按 §7.5 的真实形状返回（看板显示"数据源缺字段"），
// 再用一个**只在 Mock 模式可见**的开关模拟"后端哪天补上扩展字段"——看板立刻恢复。
// 这样"契约缺口"和"缺口补齐后前端会自动好"两件事都能被演示，而不是靠嘴说。

export interface MockFlags {
  /** 让 mock 的 GET /api/metrics 额外返回 points[].inventory_units（契约外扩展字段，用于库存周转）。 */
  extendedMetrics: boolean;
}

let flags: MockFlags = { extendedMetrics: false };
const listeners = new Set<() => void>();

export function getMockFlags(): MockFlags {
  return flags;
}

export function setMockFlags(patch: Partial<MockFlags>): void {
  flags = { ...flags, ...patch };
  for (const listener of listeners) listener();
}

export function subscribeMockFlags(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
