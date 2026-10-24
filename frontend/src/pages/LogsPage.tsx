import { useCallback, useEffect, useMemo, useState } from "react";
import type { LogItem } from "../api/types";
import { useClient } from "../hooks/useClient";
import { useResource } from "../hooks/useResource";
import { EmptyState, ErrorBanner, LoadingBlock } from "../components/Feedback";
import { levelTone, StatusPill } from "../components/StatusPill";
import { formatAgo, formatClock, formatDateTime, formatDuration } from "../lib/format";
import {
  deriveRunSummaries,
  digestOf,
  durationOf,
  errorOf,
  hasOutcome,
  outcomeLabel,
  outcomeOf,
  outcomeTitle,
  outcomeTone,
} from "../lib/outcome";

type Level = "debug" | "info" | "warn" | "error";
type LevelFilter = Level | "all";

const PAGE_SIZES = [50, 100, 200] as const;

export function LogsPage(): JSX.Element {
  const client = useClient();
  const [level, setLevel] = useState<LevelFilter>("all");
  const [eventPrefix, setEventPrefix] = useState("");
  const [runId, setRunId] = useState("");
  const [limit, setLimit] = useState<number>(100);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [older, setOlder] = useState<LogItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);

  const filterKey = `${level}|${eventPrefix.trim()}|${runId.trim()}|${limit}`;

  const head = useResource({
    key: `logs|${filterKey}`,
    intervalMs: 5_000,
    fetcher: (signal) =>
      client.getLogs(
        {
          level: level === "all" ? null : level,
          event: eventPrefix.trim() === "" ? null : eventPrefix.trim(),
          run_id: runId.trim() === "" ? null : runId.trim(),
          limit,
        },
        signal,
      ),
  });

  // 筛选条件变了，之前翻出来的"更早"页就不再属于当前视图，必须清掉（否则会串数据）。
  useEffect(() => {
    setOlder([]);
    setCursor(null);
    setExpanded(new Set());
    setOlderError(null);
  }, [filterKey]);

  const rows = useMemo<LogItem[]>(() => {
    const bySeq = new Map<number, LogItem>();
    for (const item of head.data?.items ?? []) bySeq.set(item.seq, item);
    for (const item of older) if (!bySeq.has(item.seq)) bySeq.set(item.seq, item);
    return [...bySeq.values()].sort((a, b) => b.seq - a.seq);
  }, [head.data, older]);

  const loadOlder = useCallback(async (): Promise<void> => {
    const last = rows[rows.length - 1];
    const beforeSeq = cursor !== null ? Number(cursor) : last?.seq;
    if (beforeSeq === undefined || !Number.isFinite(beforeSeq)) return;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const page = await client.getLogs({
        level: level === "all" ? null : level,
        event: eventPrefix.trim() === "" ? null : eventPrefix.trim(),
        run_id: runId.trim() === "" ? null : runId.trim(),
        limit,
        before_seq: beforeSeq,
      });
      setOlder((prev) => [...prev, ...page.items]);
      setCursor(page.next_cursor);
    } catch (err) {
      // 不吞异常：翻页失败要能看见（否则用户以为"没有更早的日志了"）。
      setOlderError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingOlder(false);
    }
  }, [client, cursor, eventPrefix, level, limit, rows, runId]);

  const toggle = (seq: number): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  };

  // agent_traces 是**表**（一次 run 一行摘要，§6.1），没有对应 REST 接口；
  // /api/logs 只给日志行。这里不编造接口，而是按 §7.6.1 的不变量从 node.trace 派生 run 摘要：
  //   count(run_id=X, event="node.trace") == agent_traces[X].step_count（不区分 outcome）。
  const runSummaries = useMemo(() => deriveRunSummaries(rows), [rows]);
  const traceCount = rows.filter((row) => row.event === "node.trace").length;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h2>执行日志</h2>
          <p className="page-sub">
            数据源 <code>GET /api/logs</code>（JSONL 日志汇，seq DESC）。节点级 trace 只以 <code>event="node.trace"</code>{" "}
            事件存在（§7.6.1），按 <code>data.outcome</code> 分色：<code>ok</code> 绿、<code>interrupted</code>{" "}
            黄（等待审批，正常控制流）、<code>error</code> 红。
          </p>
        </div>
        <div className="page-tools">
          <span className="muted small">更新于 {formatAgo(head.updatedAt)}</span>
          <button type="button" className="btn btn-sm" onClick={head.refresh} disabled={head.loading || head.refreshing}>
            刷新
          </button>
        </div>
      </header>

      <div className="filter-bar">
        <label className="field field-inline">
          <span className="field-label">level（服务端过滤，不用于分色）</span>
          <select
            value={level}
            onChange={(e) => {
              setLevel(e.target.value as LevelFilter);
            }}
          >
            <option value="all">all</option>
            <option value="debug">debug</option>
            <option value="info">info</option>
            <option value="warn">warn</option>
            <option value="error">error</option>
          </select>
        </label>
        <label className="field field-inline">
          <span className="field-label">event 前缀</span>
          <input
            value={eventPrefix}
            onChange={(e) => setEventPrefix(e.target.value)}
            placeholder="例如 node.trace 或 action."
          />
        </label>
        <label className="field field-inline">
          <span className="field-label">run_id</span>
          <input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="run_20261023T142530Z_9f3c1a7b" />
        </label>
        <label className="field field-inline">
          <span className="field-label">每页 limit</span>
          <select
            value={limit}
            onChange={(e) => {
              setLimit(Number(e.target.value));
            }}
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
      </div>

      {head.error ? <ErrorBanner failure={head.error} onRetry={head.refresh} onRefresh={head.refresh} /> : null}

      {head.loading && head.data === null ? <LoadingBlock label="正在拉取日志…" /> : null}

      {!head.loading && rows.length === 0 && head.error === null ? (
        <EmptyState title="没有匹配的日志" hint="放宽筛选条件，或确认后端把日志写进了 settings.observability.log_file。" />
      ) : null}

      {runSummaries.length > 0 ? (
        <section className="run-summary" aria-label="run 摘要（从 node.trace 派生）">
          <div className="run-summary-head">
            <h3>run 摘要</h3>
            <p className="muted small">
              从当前已加载的 {traceCount} 条 <code>node.trace</code> 派生；按 §7.6.1 不变量，<strong>步数</strong>{" "}
              等于 <code>agent_traces.step_count</code>（不区分 outcome）。筛选/分页会改变这里的计数——它不是
              <code>agent_traces</code> 表的读数（该表无 REST 接口）。
            </p>
          </div>
          <table className="table table-compact run-summary-table">
            <thead>
              <tr>
                <th>run_id</th>
                <th>步数</th>
                <th>ok</th>
                <th>等待审批</th>
                <th>error</th>
                <th>总耗时</th>
                <th>最后活动</th>
              </tr>
            </thead>
            <tbody>
              {runSummaries.map((summary) => (
                <tr key={summary.runId}>
                  <td>
                    <code>{summary.runId}</code>
                  </td>
                  <td>{summary.steps}</td>
                  <td className="tone-ok">{summary.ok}</td>
                  <td className="tone-warn">{summary.interrupted}</td>
                  <td className={summary.error > 0 ? "tone-bad" : undefined}>{summary.error}</td>
                  <td>{formatDuration(summary.totalDurationMs)}</td>
                  <td title={summary.lastTs ?? undefined}>{summary.lastTs ? formatClock(summary.lastTs) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {rows.length > 0 ? (
        <>
          <table className="table logs-table">
            <thead>
              <tr>
                <th className="col-seq">seq</th>
                <th className="col-time">时间（Asia/Shanghai）</th>
                <th className="col-level">level</th>
                <th className="col-node">节点</th>
                <th className="col-event">event</th>
                <th className="col-outcome">状态（outcome）</th>
                <th className="col-duration">耗时</th>
                <th className="col-message">摘要</th>
                <th className="col-expand" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const isOpen = expanded.has(row.seq);
                return (
                  <FragmentRow
                    key={row.seq}
                    row={row}
                    isOpen={isOpen}
                    onToggle={() => {
                      toggle(row.seq);
                    }}
                  />
                );
              })}
            </tbody>
          </table>

          <div className="table-foot">
            <span className="muted small">
              已显示 {rows.length} 条（seq 从 {rows[0]?.seq ?? "—"} 到 {rows[rows.length - 1]?.seq ?? "—"}）
            </span>
            <button type="button" className="btn btn-sm" onClick={() => void loadOlder()} disabled={loadingOlder}>
              {loadingOlder ? "加载中…" : "加载更早"}
            </button>
          </div>
          {olderError !== null ? (
            <p className="banner banner-error small">
              加载更早的日志失败：{olderError}（列表未变化，可再点一次）
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function FragmentRow({ row, isOpen, onToggle }: { row: LogItem; isOpen: boolean; onToggle: () => void }): JSX.Element {
  const isTrace = row.event === "node.trace";
  const outcome = outcomeOf(row);
  const duration = durationOf(row);
  const errorInfo = errorOf(row);
  // 分色规则（§7.6.1 / §11）：node.trace 只按 data.outcome 上色，**不按 level**；
  // 其它事件没有 outcome 字段，才退回 level。
  const rowClass = isTrace ? `outcome-${outcome}` : `level-${row.level}`;

  return (
    <>
      <tr className={`log-row ${rowClass} ${isOpen ? "is-open" : ""}`}>
        <td className="col-seq">{row.seq}</td>
        <td className="col-time" title={row.ts}>
          {formatClock(row.ts)}
        </td>
        <td className="col-level">
          <StatusPill label={row.level} tone={levelTone(row.level)} title="日志行的 level 字段（服务端过滤/告警阈值用）" />
        </td>
        <td className="col-node">
          <code>{row.node ?? "—"}</code>
        </td>
        <td className="col-event">
          <code>{row.event}</code>
        </td>
        <td className="col-outcome">
          {isTrace ? (
            <StatusPill
              label={outcomeLabel(outcome)}
              tone={outcomeTone(outcome)}
              title={hasOutcome(row) ? outcomeTitle(outcome) : "事件里没有 data.outcome（§7.6.1 要求必填），按 ok 读"}
            />
          ) : (
            <span className="muted small">—（非节点 trace）</span>
          )}
        </td>
        <td className="col-duration">{formatDuration(duration)}</td>
        <td className="col-message">
          {row.message}
          {errorInfo !== null ? (
            <span className="log-error-inline">
              {" "}
              <code>{errorInfo.type}</code> {errorInfo.message}
            </span>
          ) : null}
        </td>
        <td className="col-expand">
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            aria-expanded={isOpen}
            onClick={onToggle}
            title={isOpen ? "收起详情" : "展开详情"}
          >
            {isOpen ? "收起" : "详情"}
          </button>
        </td>
      </tr>
      {isOpen ? (
        <tr className="log-detail-row">
          <td colSpan={9}>
            <div className="log-detail">
              <dl className="kv kv-inline">
                <div className="kv-row">
                  <dt>seq</dt>
                  <dd>{row.seq}</dd>
                </div>
                <div className="kv-row">
                  <dt>时间（UTC）</dt>
                  <dd>{row.ts}</dd>
                </div>
                <div className="kv-row">
                  <dt>时间（Asia/Shanghai）</dt>
                  <dd>{formatDateTime(row.ts)}</dd>
                </div>
                <div className="kv-row">
                  <dt>level</dt>
                  <dd>{row.level}</dd>
                </div>
                <div className="kv-row">
                  <dt>event</dt>
                  <dd>
                    <code>{row.event}</code>
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>node</dt>
                  <dd>
                    <code>{row.node ?? "—"}</code>
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>run_id</dt>
                  <dd>
                    <code>{row.run_id ?? "—"}</code>
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>trace_id</dt>
                  <dd>
                    <code>{row.trace_id ?? "—"}</code>
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>shop_id</dt>
                  <dd>
                    <code>{row.shop_id ?? "—"}</code>
                  </dd>
                </div>
                {isTrace ? (
                  <>
                    <div className="kv-row">
                      <dt>outcome</dt>
                      <dd>
                        {outcomeLabel(outcome)}{" "}
                        <span className="muted small">
                          {hasOutcome(row) ? "（data.outcome）" : "（事件里没有 data.outcome，按 ok 读）"}
                        </span>
                      </dd>
                    </div>
                    <div className="kv-row">
                      <dt>duration_ms</dt>
                      <dd>{formatDuration(duration)}</dd>
                    </div>
                    <div className="kv-row">
                      <dt>input_digest</dt>
                      <dd>
                        <code>{digestOf(row, "input_digest") ?? "—"}</code>
                      </dd>
                    </div>
                    <div className="kv-row">
                      <dt>output_digest</dt>
                      <dd>
                        <code>{digestOf(row, "output_digest") ?? "—"}</code>
                      </dd>
                    </div>
                  </>
                ) : null}
              </dl>
              <div>
                <h5>message</h5>
                <p>{row.message}</p>
                {errorInfo !== null ? (
                  <>
                    <h5>error</h5>
                    <p className="tone-bad">
                      <code>{errorInfo.type}</code> · {errorInfo.message}
                    </p>
                  </>
                ) : null}
                <h5>data</h5>
                <pre className="json-block">{JSON.stringify(row.data, null, 2)}</pre>
              </div>
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

// —— node.trace 的读取语义全部在 src/lib/outcome.ts（可被 scripts/verify-suite.ts 直接断言）——
