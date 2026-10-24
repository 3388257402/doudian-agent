// 纯 SVG 折线图：不引图表库（任务要求）。
// 三个必须做对的细节：
//   1. value 为 null 的点**断开**而不是画成 0（缺数据 ≠ 零销量）；
//   2. 每个点带 <title>，浏览器原生 tooltip，不需要额外依赖也不需要鼠标事件状态；
//   3. 按容器真实像素布局（useElementWidth），线宽与圆点不会被非等比缩放拉歪。
import { useMemo } from "react";
import { useElementWidth } from "../hooks/useElementWidth";
import { formatShortDate } from "../lib/format";
import type { SeriesPoint, SeriesUnit } from "../lib/metrics";

export interface LineChartProps {
  points: SeriesPoint[];
  unit: SeriesUnit;
  color: string;
  height?: number;
  formatValue: (value: number | null) => string;
  ariaLabel: string;
}

const PAD_LEFT = 52;
const PAD_RIGHT = 10;
const PAD_TOP = 10;
const PAD_BOTTOM = 20;

export function LineChart({ points, unit, color, height = 132, formatValue, ariaLabel }: LineChartProps): JSX.Element {
  const [ref, width] = useElementWidth<HTMLDivElement>();

  const geometry = useMemo(() => {
    const innerWidth = Math.max(width - PAD_LEFT - PAD_RIGHT, 10);
    const innerHeight = Math.max(height - PAD_TOP - PAD_BOTTOM, 10);
    const values = points.map((p) => p.value).filter((v): v is number => v !== null);
    if (values.length === 0) return null;

    let min = Math.min(...values);
    let max = Math.max(...values);
    if (min === max) {
      const pad = Math.abs(min) > 0 ? Math.abs(min) * 0.02 : 1;
      min -= pad;
      max += pad;
    }
    const span = max - min;
    const step = points.length > 1 ? innerWidth / (points.length - 1) : 0;
    const x = (index: number): number => PAD_LEFT + (points.length > 1 ? index * step : innerWidth / 2);
    const y = (value: number): number => PAD_TOP + innerHeight - ((value - min) / span) * innerHeight;

    // 把连续有值的点切成若干段，null 处断开。
    const segments: { index: number; point: SeriesPoint; value: number }[][] = [];
    let current: { index: number; point: SeriesPoint; value: number }[] = [];
    points.forEach((point, index) => {
      if (point.value === null) {
        if (current.length > 0) segments.push(current);
        current = [];
        return;
      }
      current.push({ index, point, value: point.value });
    });
    if (current.length > 0) segments.push(current);

    const gridValues = [max, min + span / 2, min];
    return { innerHeight, min, max, span, x, y, segments, gridValues };
  }, [points, width, height]);

  const first = points[0];
  const last = points[points.length - 1];
  const middle = points.length > 2 ? points[Math.floor(points.length / 2)] : undefined;

  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={height} role="img" aria-label={ariaLabel} className="chart-svg">
        {geometry === null ? (
          <text x={width / 2} y={height / 2} textAnchor="middle" className="chart-empty">
            区间内没有可绘制的数据点
          </text>
        ) : (
          <>
            {geometry.gridValues.map((value, index) => {
              const yy = geometry.y(value);
              return (
                <g key={`grid-${index}`}>
                  <line x1={PAD_LEFT} x2={width - PAD_RIGHT} y1={yy} y2={yy} className="chart-grid" />
                  <text x={PAD_LEFT - 6} y={yy + 3} textAnchor="end" className="chart-axis-label">
                    {formatValue(value)}
                  </text>
                </g>
              );
            })}

            {geometry.segments.map((segment, segmentIndex) => {
              const d = segment
                .map((entry, i) => `${i === 0 ? "M" : "L"}${geometry.x(entry.index).toFixed(1)},${geometry.y(entry.value).toFixed(1)}`)
                .join(" ");
              return <path key={`seg-${segmentIndex}`} d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />;
            })}

            {points.map((point, index) =>
              point.value === null ? null : (
                <circle key={`dot-${point.date}`} cx={geometry.x(index)} cy={geometry.y(point.value)} r={3} fill={color} className="chart-dot">
                  <title>{`${point.date} · ${formatValue(point.value)}`}</title>
                </circle>
              ),
            )}

            {[first, middle, last].map((point, index) =>
              point === undefined ? null : (
                <text
                  key={`xlabel-${index}-${point.date}`}
                  x={geometry.x(points.indexOf(point))}
                  y={height - 5}
                  textAnchor={index === 0 ? "start" : index === 2 ? "end" : "middle"}
                  className="chart-axis-label"
                >
                  {formatShortDate(point.date)}
                </text>
              ),
            )}
          </>
        )}
      </svg>
      <div className="chart-footnote">
        单位：{unitLabel(unit)} · 断点表示该日无数据（不补零）
      </div>
    </div>
  );
}

function unitLabel(unit: SeriesUnit): string {
  switch (unit) {
    case "cents":
      return "元（后端为整数分）";
    case "ratio":
      return "%";
    case "count":
      return "次/件";
  }
}
