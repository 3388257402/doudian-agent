import { formatCentsCompact, formatCount, formatDelta, formatDecimal, formatPercent } from "../lib/format";
import { summarize, type MetricGap, type MetricSeries } from "../lib/metrics";
import { LineChart } from "./LineChart";

export interface MetricCardProps {
  series: MetricSeries;
  color: string;
  /** 该指标在数据源里缺字段时的说明；有 gap 时卡片不画图，直接讲缺什么。 */
  gap?: MetricGap;
}

function renderValue(series: MetricSeries, value: number | null): string {
  if (value === null) return "—";
  switch (series.unit) {
    case "cents":
      return `¥${formatDecimal(value / 100, 2)}`;
    case "ratio":
      return formatPercent(value, 2);
    case "count":
      return Number.isInteger(value) ? formatCount(value) : formatDecimal(value, 3);
  }
}

function renderAxis(series: MetricSeries): (value: number | null) => string {
  return (value) => {
    if (value === null) return "—";
    switch (series.unit) {
      case "cents":
        return formatCentsCompact(value);
      case "ratio":
        return formatPercent(value, 1);
      case "count":
        return Number.isInteger(value) ? formatCount(value) : formatDecimal(value, 3);
    }
  };
}

export function MetricCard({ series, color, gap }: MetricCardProps): JSX.Element {
  const { latest, previous } = summarize(series);
  // 头条值：默认取"最后一个有值点"（趋势卡惯例）；毛利率声明了 headline="newest"，
  // 于是最新一天是 null 时头条就是 null → 卡片显示「未配置成本」，不会拿更早那天的数冒充今天。
  const headline =
    series.headline === "newest" ? (series.points[series.points.length - 1] ?? null) : latest;
  const delta = formatDelta(headline?.value ?? null, previous?.value ?? null);
  const value = headline?.value ?? null;
  // 最新值为 null 时不能含糊：毛利率写「未配置成本」（null = 没配成本，不是 0% 也不是"数据没刷新"）。
  // 没有 nullLabel 的序列（其他指标）沿用 "—"，因为对它们来说 null 只是"没有这个数"。
  const valueText = value === null ? (series.nullLabel ?? "—") : renderValue(series, value);
  // 显示数值时必须一起显示的口径前提（毛利率 = "仅覆盖 X% 成交额"）。值本身为 null 时 metrics.ts 不挂它。
  const qualifier = value === null ? null : (headline?.qualifier ?? null);

  return (
    <section className="card metric-card" aria-label={series.label}>
      <header className="metric-head">
        <div>
          <h3 className="metric-title">{series.label}</h3>
          <p className="metric-desc" title={series.description}>
            {series.description}
          </p>
        </div>
        <span className={`delta delta-${delta.tone}`}>{delta.text}</span>
      </header>

      <div className="metric-value">
        <strong>{valueText}</strong>
        <span className="metric-date">{headline ? headline.date : "无数据"}</span>
      </div>

      {qualifier !== null ? <p className="metric-qualifier">{qualifier}</p> : null}

      {gap ? (
        <div className="gap-box">
          <strong>数据源缺字段，无法绘制</strong>
          <p>{gap.reason}</p>
          <p className="muted">
            缺少：<code>{gap.missingFields.join(", ")}</code>（影响 {gap.affectedDays} 天）
          </p>
        </div>
      ) : (
        <LineChart
          points={series.points}
          unit={series.unit}
          color={color}
          formatValue={renderAxis(series)}
          ariaLabel={`${series.label} 趋势`}
        />
      )}
    </section>
  );
}

/** 次要指标：一行小卡，不画图（避免 7 张图堆在一起没人看）。 */
export function MiniMetric({ series }: { series: MetricSeries }): JSX.Element {
  const { latest, previous } = summarize(series);
  const delta = formatDelta(latest?.value ?? null, previous?.value ?? null);
  return (
    <div className="mini-metric">
      <span className="mini-label">{series.label}</span>
      <span className="mini-value">{renderValue(series, latest?.value ?? null)}</span>
      <span className={`delta delta-${delta.tone}`}>{delta.text}</span>
    </div>
  );
}
