import { useMemo, useState, useSyncExternalStore } from "react";
import { DEFAULT_SHOP_ID } from "../api/mode";
import { useApiMode, useClient } from "../hooks/useClient";
import { useResource } from "../hooks/useResource";
import { EmptyState, ErrorBanner, LoadingBlock } from "../components/Feedback";
import { MetricCard, MiniMetric } from "../components/MetricCard";
import { getMockFlags, setMockFlags, subscribeMockFlags } from "../mock/flags";
import { defaultRange, isValidDate, todayShanghai } from "../lib/dates";
import { formatAgo } from "../lib/format";
import { deriveDashboard } from "../lib/metrics";

const COLORS = {
  gross_margin: "#4f8cff",
  paid_order_count: "#34d399",
  inventory_turnover: "#f59e0b",
};

export function DashboardPage(): JSX.Element {
  const client = useClient();
  const mode = useApiMode();
  const mockFlags = useSyncExternalStore(subscribeMockFlags, getMockFlags, getMockFlags);
  const [shopId, setShopId] = useState(DEFAULT_SHOP_ID);
  const initial = defaultRange(14);
  const [dateFrom, setDateFrom] = useState(initial.from);
  const [dateTo, setDateTo] = useState(initial.to);

  const rangeValid = isValidDate(dateFrom) && isValidDate(dateTo) && dateFrom <= dateTo;

  const metrics = useResource({
    key: `metrics|${shopId}|${dateFrom}|${dateTo}`,
    intervalMs: 30_000,
    enabled: shopId !== "" && rangeValid,
    fetcher: (signal) => client.getMetrics({ shop_id: shopId, date_from: dateFrom, date_to: dateTo, granularity: "day" }, signal),
  });

  const points = metrics.data?.points ?? [];
  const model = useMemo(() => deriveDashboard(points), [points]);
  const gapByKey = useMemo(() => new Map(model.gaps.map((gap) => [gap.key, gap])), [model.gaps]);

  const failure = metrics.error;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h2>效果看板</h2>
          <p className="page-sub">
            数据源 <code>GET /api/metrics</code>（granularity=day，业务日期为 Asia/Shanghai）。金额一律整数分，展示时
            /100；比例为 0..1，分母为 0 时后端返回 <code>null</code>，这里显示 “—”。
          </p>
        </div>
        <div className="page-tools">
          <span className="muted small">更新于 {formatAgo(metrics.updatedAt)}</span>
          <button type="button" className="btn btn-sm" onClick={metrics.refresh} disabled={metrics.loading || metrics.refreshing}>
            刷新
          </button>
        </div>
      </header>

      <div className="filter-bar">
        <label className="field field-inline">
          <span className="field-label">店铺 shop_id</span>
          <input value={shopId} onChange={(e) => setShopId(e.target.value.trim())} placeholder="12345678" />
        </label>
        <label className="field field-inline">
          <span className="field-label">起始日期 date_from</span>
          <input
            type="date"
            value={dateFrom}
            max={dateTo}
            onChange={(e) => setDateFrom(e.target.value)}
            aria-invalid={isValidDate(dateFrom) ? undefined : true}
          />
        </label>
        <label className="field field-inline">
          <span className="field-label">结束日期 date_to</span>
          <input
            type="date"
            value={dateTo}
            min={dateFrom}
            max={todayShanghai()}
            onChange={(e) => setDateTo(e.target.value)}
            aria-invalid={isValidDate(dateTo) ? undefined : true}
          />
        </label>
        <div className="quick-range">
          {[
            { label: "近 7 天", days: 7 },
            { label: "近 14 天", days: 14 },
            { label: "近 30 天", days: 30 },
          ].map((preset) => (
            <button
              key={preset.days}
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => {
                const range = defaultRange(preset.days);
                setDateFrom(range.from);
                setDateTo(range.to);
              }}
            >
              {preset.label}
            </button>
          ))}
        </div>
        {mode === "mock" ? (
          <label className="field field-inline" title="只在 Mock 模式可见：真实后端按 response_model 禁止透传契约外字段">
            <span className="field-label">Mock：扩展字段</span>
            <input
              type="checkbox"
              checked={mockFlags.extendedMetrics}
              onChange={(event) => {
                setMockFlags({ extendedMetrics: event.target.checked });
                metrics.refresh();
              }}
            />
            <span className="field-hint">模拟后端额外返回 inventory_units（契约外，用于库存周转）</span>
          </label>
        ) : null}
      </div>

      {!rangeValid ? (
        <div className="banner banner-error">
          <strong>日期区间不合法</strong>
          <p className="banner-hint">
            date_from / date_to 必须是 YYYY-MM-DD，且 date_from 不得晚于 date_to（后端会返回 400 VALIDATION_ERROR）。
          </p>
        </div>
      ) : null}

      {failure ? (
        failure.code === "RESOURCE_NOT_FOUND" ? (
          <EmptyState
            title="该店铺没有指标数据"
            hint={`${failure.message}（RESOURCE_NOT_FOUND 属于空态，不是错误）`}
          />
        ) : (
          <ErrorBanner failure={failure} onRetry={metrics.refresh} onRefresh={metrics.refresh} />
        )
      ) : null}

      {metrics.loading && metrics.data === null ? <LoadingBlock label="正在拉取指标…" /> : null}

      {metrics.data !== null ? (
        <>
          <p className="muted small">
            区间 {dateFrom} → {dateTo} · 返回 {points.length} 个数据点 · unit.money=
            <code>{metrics.data.unit.money}</code> · unit.date=<code>{metrics.data.unit.date}</code> · unit.ratio=
            <code>{metrics.data.unit.ratio}</code>
          </p>

          {model.gaps.length > 0 ? (
            <section className="banner banner-warn" aria-label="契约缺口">
              <strong>有 {model.gaps.length} 个指标无法从当前数据源计算（不画假曲线）</strong>
              <ul className="banner-details">
                {model.gaps.map((gap) => (
                  <li key={gap.key}>
                    <strong>{gap.label}</strong>：缺字段 <code>{gap.missingFields.join(", ")}</code> —— {gap.reason}
                  </li>
                ))}
              </ul>
              <p className="banner-hint">
                这是契约缺口，不是前端 bug：<code>MetricPoint</code>（§7.5/§12）里没有库存字段（
                <code>inventory_units</code>），库存周转因此算不出来。已向 spec-lead 提议把它作为可选扩展字段增补进契约（
                <code>frontend/src/api/extension.ts</code> 有完整说明）。Mock 模式下可用筛选栏的「Mock：扩展字段」
                开关演示后端补齐后的样子。
              </p>
            </section>
          ) : null}

          {mode === "mock" && mockFlags.extendedMetrics ? (
            <section className="banner banner-warn" aria-label="Mock 扩展字段">
              <strong>当前响应含契约外扩展字段（inventory_units）</strong>
              <p className="banner-hint">
                这是 Mock 假后端在模拟"后端哪天补上库存字段"。真实后端按 §7 的 <code>response_model</code>{" "}
                禁止透传未知字段，所以默认不返回它；关掉开关即可看到契约原样下的行为（库存周转那张卡变成缺口提示）。
                毛利率不在此列：它读的是契约字段 <code>gross_margin</code>，开关不影响。
              </p>
            </section>
          ) : null}

          <div className="metric-grid">
            {model.primary.map((series) => (
              <MetricCard
                key={series.key}
                series={series}
                color={COLORS[series.key as keyof typeof COLORS] ?? "#4f8cff"}
                gap={gapByKey.get(series.key)}
              />
            ))}
          </div>

          <h3 className="section-title">其他指标</h3>
          <div className="mini-grid">
            {model.secondary.map((series) => (
              <MiniMetric key={series.key} series={series} />
            ))}
          </div>

          {model.notes.length > 0 ? (
            <section className="notes">
              <h4>数据说明</h4>
              <ul>
                {model.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </section>
          ) : null}
        </>
      ) : null}

      <p className="muted small">
        看板只读：不提供"手工改指标"。所有指标由后端 <code>compute_metrics</code> 按 §5.3 公式算出；毛利率与成本覆盖率
        <strong>直接取后端字段</strong>（前端不自算、不读契约外成本字段），前端只负责派生同比/环比与库存周转（
        <code>src/lib/metrics.ts</code>，公式写在函数注释里）。
      </p>
    </div>
  );
}
