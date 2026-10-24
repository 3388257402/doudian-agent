// 业务日期工具：契约规定 `date` 字段是 Asia/Shanghai 的 YYYY-MM-DD（§0）。
// 浏览器本地时区可能是别的时区，所以不能用 new Date().toISOString().slice(0,10) 取"今天"。

const SHANGHAI_DATE = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** Asia/Shanghai 的今天，格式 YYYY-MM-DD。 */
export function todayShanghai(now: Date = new Date()): string {
  // en-CA 的日期格式就是 YYYY-MM-DD，避免再手工拼。
  return SHANGHAI_DATE.format(now);
}

/** 日期字符串加减天数（用 UTC 运算避免夏令时/时区漂移）。 */
export function addDays(date: string, delta: number): string {
  const ms = Date.parse(`${date}T00:00:00Z`);
  if (!Number.isFinite(ms)) return date;
  return new Date(ms + delta * 86_400_000).toISOString().slice(0, 10);
}

/** 默认区间：今天往前 N-1 天到今天（含端点）。 */
export function defaultRange(days = 7, now: Date = new Date()): { from: string; to: string } {
  const to = todayShanghai(now);
  return { from: addDays(to, -(days - 1)), to };
}

export function isValidDate(text: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(text) && Number.isFinite(Date.parse(`${text}T00:00:00Z`));
}
