// 展示层格式化。硬规则（CONTRACT §0）：
// - 金额字段一律整数分（*_cents），展示时 /100 并保留两位小数；前端内部绝不出现 float 金额运算。
// - 时间戳是 UTC ISO-8601 带 Z，展示时转 Asia/Shanghai。
// - 比例是 0..1 的 float（6 位小数），分母为 0 时后端给 null → 展示 "—"，不是 "0.00%"。

const CNY = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const INT = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });

/** 整数分 → "¥12,840.00"；null/undefined → "—" */
export function formatCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return "—";
  return `¥${CNY.format(cents / 100)}`;
}

/** 坐标轴/紧凑场景：整数分 → "1.3万" / "960" （单位：元） */
export function formatCentsCompact(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return "—";
  const yuan = cents / 100;
  const abs = Math.abs(yuan);
  if (abs >= 100_000_000) return `${(yuan / 100_000_000).toFixed(1)}亿`;
  if (abs >= 10_000) return `${(yuan / 10_000).toFixed(1)}万`;
  if (abs >= 100) return yuan.toFixed(0);
  return yuan.toFixed(2);
}

/** 比例 → "3.46%"；null → "—" */
export function formatPercent(ratio: number | null | undefined, digits = 2): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return "—";
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return INT.format(n);
}

/**
 * 毫秒耗时 → "42.7 ms" / "1.32 s" / "3.0 s"。
 * null（字段缺失）→ "—"，不显示 0ms —— "没有这个字段"和"耗时 0"是两件事。
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms.toFixed(1)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function formatDecimal(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

const SHANGHAI_DATETIME = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** UTC ISO → "2026-10-23 22:26:02"（Asia/Shanghai，与业务日期同源） */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  return SHANGHAI_DATETIME.format(new Date(ms)).replace(/\//g, "-");
}

/** UTC ISO → "22:26:02.113"（表格里的时间列，紧凑） */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  const d = new Date(ms);
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
  const millis = String(d.getUTCMilliseconds()).padStart(3, "0");
  return `${parts}.${millis}`;
}

/** 业务日期（已经是 Asia/Shanghai 的 YYYY-MM-DD）→ "10-22" */
export function formatShortDate(date: string): string {
  const m = /^\d{4}-(\d{2})-(\d{2})$/.exec(date);
  return m ? `${m[1]}-${m[2]}` : date;
}

export interface Delta {
  text: string;
  tone: "up" | "down" | "flat" | "unknown";
}

/** 环比变化（用于卡片右上角的涨跌）。值缺失时返回 unknown，不伪装成 0%。 */
export function formatDelta(current: number | null | undefined, previous: number | null | undefined): Delta {
  if (current === null || current === undefined || previous === null || previous === undefined) {
    return { text: "无可比数据", tone: "unknown" };
  }
  if (!Number.isFinite(current) || !Number.isFinite(previous)) return { text: "无可比数据", tone: "unknown" };
  if (previous === 0) return { text: "上期为 0", tone: "unknown" };
  const ratio = (current - previous) / Math.abs(previous);
  if (Math.abs(ratio) < 0.00005) return { text: "持平", tone: "flat" };
  const sign = ratio > 0 ? "+" : "";
  return { text: `${sign}${(ratio * 100).toFixed(2)}%`, tone: ratio > 0 ? "up" : "down" };
}

/** "12 秒前" —— 用于"最后更新"提示，让轮询是否还活着可见。 */
export function formatAgo(timestampMs: number | null, nowMs: number = Date.now()): string {
  if (timestampMs === null) return "尚未刷新";
  const seconds = Math.max(0, Math.round((nowMs - timestampMs) / 1000));
  if (seconds < 1) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时前`;
}

/** 分 → 元 的可读预览（编辑参数时贴在输入框旁，避免用户自己算） */
export function centsPreview(centsText: string): string {
  if (!/^-?\d+$/.test(centsText.trim())) return "—";
  const cents = Number(centsText.trim());
  if (!Number.isSafeInteger(cents)) return "—";
  return formatCents(cents);
}

/** 安全乘法（券让利上限这类整数分乘法）；溢出返回 null 而不是 Infinity。 */
export function safeMultiply(a: number, b: number): number | null {
  if (!Number.isSafeInteger(a) || !Number.isSafeInteger(b)) return null;
  const product = a * b;
  return Number.isSafeInteger(product) ? product : null;
}
