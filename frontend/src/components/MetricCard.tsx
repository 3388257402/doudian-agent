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
  const delta = formatDelta(latest?.value ?? null, previous?.value ?? null);

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
        <strong>{renderValue(series, latest?.value ?? null)}</strong>
        <span className="metric-date">{latest ? latest.date : "无数据"}</span>
      </div>

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
