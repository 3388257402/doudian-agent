import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiFailure } from "../api/errors";
import type { ApproveRequest, ApproveResponse, PendingApproval, Verdict } from "../api/types";
import { DEFAULT_SHOP_ID } from "../api/mode";
import { useClient } from "../hooks/useClient";
import { useResource } from "../hooks/useResource";
import { ApprovalCard, makeDecisionDraft } from "../components/ApprovalCard";
import { EmptyState, ErrorBanner, LoadingBlock } from "../components/Feedback";
import { StatusPill } from "../components/StatusPill";
import { buildApproveRequest, type DecisionDraft } from "../lib/approve";
import { formatAgo, formatDateTime } from "../lib/format";

const OPERATOR_STORAGE_KEY = "doudian.operator";
const OPERATOR_RE = /^[A-Za-z0-9_.-]{1,64}$/;
const LIST_LIMIT = 200;

type StatusFilter = "pending" | "approved" | "rejected" | "expired" | "all";

interface ThreadGroup {
  threadId: string;
  interruptId: string;
  runId: string;
  items: PendingApproval[];
}

function readOperator(): string {
  try {
    const stored = window.localStorage.getItem(OPERATOR_STORAGE_KEY);
    if (stored && OPERATOR_RE.test(stored)) return stored;
  } catch {
    // localStorage 不可用 → 用默认值
  }
  return "ops_demo";
}

export function ApprovalsPage(): JSX.Element {
  const client = useClient();
  const [shopId, setShopId] = useState(DEFAULT_SHOP_ID);
  const [status, setStatus] = useState<StatusFilter>("pending");
  const [operator, setOperator] = useState(readOperator);
  const [decisions, setDecisions] = useState<Record<string, DecisionDraft>>({});
  const [paramErrors, setParamErrors] = useState<Record<string, Record<string, string>>>({});
  const [submittingThread, setSubmittingThread] = useState<string | null>(null);
  const [submitFailure, setSubmitFailure] = useState<ApiFailure | null>(null);
  const [lastResult, setLastResult] = useState<ApproveResponse | null>(null);
  const [runNotice, setRunNotice] = useState<string | null>(null);
  const [runningAgent, setRunningAgent] = useState(false);

  const alerts = useResource({
    key: `alerts|${shopId}|${status}`,
    intervalMs: 8_000,
    fetcher: (signal) => client.listAlerts({ shop_id: shopId === "" ? null : shopId, status, limit: LIST_LIMIT }, signal),
  });

  const items = alerts.data?.items ?? [];

  // 列表刷新后，把已不在列表里的动作的本地裁决草稿清掉，避免"提交了看不见的东西"。
  useEffect(() => {
    const present = new Set(items.map((item) => item.action_id));
    setDecisions((prev) => {
      const next: Record<string, DecisionDraft> = {};
      let changed = false;
      for (const [actionId, draft] of Object.entries(prev)) {
        if (present.has(actionId)) next[actionId] = draft;
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [items]);

  const groups = useMemo<ThreadGroup[]>(() => {
    const byThread = new Map<string, ThreadGroup>();
    for (const item of items) {
      const existing = byThread.get(item.thread_id);
      if (existing) existing.items.push(item);
      else
        byThread.set(item.thread_id, {
          threadId: item.thread_id,
          interruptId: item.interrupt_id,
          runId: item.run_id,
          items: [item],
        });
    }
    return [...byThread.values()];
  }, [items]);

  const decisionFor = useCallback(
    (approval: PendingApproval): DecisionDraft => decisions[approval.action_id] ?? makeDecisionDraft(approval),
    [decisions],
  );

  const setVerdict = (approval: PendingApproval, verdict: Verdict): void => {
    setDecisions((prev) => {
      const current = prev[approval.action_id] ?? makeDecisionDraft(approval);
      return { ...prev, [approval.action_id]: { ...current, verdict } };
    });
    setSubmitFailure(null);
  };

  const setDraftValue = (approval: PendingApproval, key: string, value: string): void => {
    setDecisions((prev) => {
      const current = prev[approval.action_id] ?? makeDecisionDraft(approval);
      return { ...prev, [approval.action_id]: { ...current, draft: { ...current.draft, [key]: value } } };
    });
    setParamErrors((prev) => {
      const forAction = prev[approval.action_id];
      if (!forAction || forAction[key] === undefined) return prev;
      const nextErrors = { ...forAction };
      delete nextErrors[key];
      return { ...prev, [approval.action_id]: nextErrors };
    });
  };

  const setReason = (approval: PendingApproval, reason: string): void => {
    setDecisions((prev) => {
      const current = prev[approval.action_id] ?? makeDecisionDraft(approval);
      return { ...prev, [approval.action_id]: { ...current, reason } };
    });
  };

  const submit = async (group: ThreadGroup): Promise<void> => {
    setSubmitFailure(null);
    setRunNotice(null);

    if (!OPERATOR_RE.test(operator)) {
      setSubmitFailure(new ApiFailure("VALIDATION_ERROR", "operator 必须匹配 ^[A-Za-z0-9_.-]{1,64}$", { details: { operator } }));
      return;
    }

    // 请求体由纯函数构造（lib/approve.ts）：§7.4 的两条硬规则在那里统一出口，
    // edit 必带 edited_params、approve/reject 恒为 null，本地先按 §5.5 校验一遍参数。
    const built = buildApproveRequest({
      threadId: group.threadId,
      interruptId: group.interruptId,
      operator,
      items: group.items,
      decisionOf: decisionFor,
    });

    if (!built.ok) {
      setParamErrors(built.paramErrors);
      if (built.missingActionIds.length > 0) {
        setSubmitFailure(
          new ApiFailure("VALIDATION_ERROR", "同一 thread 的动作必须整批裁决（§4.4 规则 2）", {
            details: { missing_action_ids: built.missingActionIds },
          }),
        );
        return;
      }
      setSubmitFailure(
        new ApiFailure("VALIDATION_ERROR", "参数校验未通过，未提交任何裁决", {
          details: {
            params: Object.fromEntries(
              Object.entries(built.paramErrors).flatMap(([actionId, errs]) =>
                Object.entries(errs).map(([field, message]) => [`${actionId}.${field}`, message]),
              ),
            ),
          },
        }),
      );
      return;
    }

    setParamErrors({});
    const body: ApproveRequest = built.body;

    setSubmittingThread(group.threadId);
    try {
      const result = await client.approve(body);
      setLastResult(result);
      setDecisions((prev) => {
        const next = { ...prev };
        for (const item of group.items) delete next[item.action_id];
        return next;
      });
      alerts.refresh();
    } catch (err) {
      const failure = err instanceof ApiFailure ? err : new ApiFailure("INTERNAL_ERROR", String(err));
      setSubmitFailure(failure);
      // 这几类是"列表已过期"，刷新后用户才知道真实状态。
      if (
        failure.code === "DUPLICATE_DECISION" ||
        failure.code === "THREAD_NOT_FOUND" ||
        failure.code === "APPROVAL_NOT_FOUND" ||
        failure.code === "RUN_STATE_CONFLICT"
      ) {
        alerts.refresh();
      }
    } finally {
      setSubmittingThread(null);
    }
  };

  const triggerRun = async (): Promise<void> => {
    setRunningAgent(true);
    setRunNotice(null);
    setSubmitFailure(null);
    try {
      const result = await client.runAgent({ shop_id: shopId, run_id: null, trigger: "manual", dry_run: true });
      setRunNotice(`已触发 ${result.run_id}：status=${result.status}，新增 ${result.pending_approvals.length} 条待审批。`);
      alerts.refresh();
    } catch (err) {
      setSubmitFailure(err instanceof ApiFailure ? err : new ApiFailure("INTERNAL_ERROR", String(err)));
    } finally {
      setRunningAgent(false);
    }
  };

  const failure = submitFailure ?? alerts.error;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h2>审批台</h2>
          <p className="page-sub">
            待审批动作来自 <code>GET /api/alerts</code>；提交走 <code>POST /api/approve</code>。同一 thread
            的动作必须整批裁决（契约 §4.4 规则 2：不多、不少、不重复）。
          </p>
        </div>
        <div className="page-tools">
          <span className="muted small">更新于 {formatAgo(alerts.updatedAt)}</span>
          <button type="button" className="btn btn-sm" onClick={alerts.refresh} disabled={alerts.loading || alerts.refreshing}>
            刷新
          </button>
          <button type="button" className="btn btn-sm" onClick={() => void triggerRun()} disabled={runningAgent}>
            {runningAgent ? "触发中…" : "触发新一轮采集"}
          </button>
        </div>
      </header>

      <div className="filter-bar">
        <label className="field field-inline">
          <span className="field-label">店铺 shop_id</span>
          <input value={shopId} onChange={(e) => setShopId(e.target.value.trim())} placeholder="12345678" />
        </label>
        <label className="field field-inline">
          <span className="field-label">状态</span>
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as StatusFilter);
            }}
          >
            <option value="pending">pending</option>
            <option value="approved">approved</option>
            <option value="rejected">rejected</option>
            <option value="expired">expired</option>
            <option value="all">all</option>
          </select>
        </label>
        <label className="field field-inline">
          <span className="field-label">操作人 operator</span>
          <input
            value={operator}
            onChange={(e) => {
              const next = e.target.value;
              setOperator(next);
              if (OPERATOR_RE.test(next)) {
                try {
                  window.localStorage.setItem(OPERATOR_STORAGE_KEY, next);
                } catch {
                  // 忽略持久化失败
                }
              }
            }}
            aria-invalid={OPERATOR_RE.test(operator) ? undefined : true}
          />
        </label>
      </div>

      {runNotice ? <div className="banner banner-ok">{runNotice}</div> : null}
      {failure ? (
        <ErrorBanner
          failure={failure}
          onRetry={() => void triggerRun()}
          onRefresh={alerts.refresh}
          onDismiss={() => {
            setSubmitFailure(null);
          }}
        />
      ) : null}

      {lastResult ? <ApproveResultPanel result={lastResult} onDismiss={() => setLastResult(null)} /> : null}

      {alerts.loading && alerts.data === null ? <LoadingBlock label="正在拉取待审批列表…" /> : null}

      {!alerts.loading && items.length === 0 && alerts.error === null ? (
        <EmptyState
          title={status === "pending" ? "当前没有待审批动作" : `没有 ${status} 状态的审批`}
          hint="跑一轮采集（右上角按钮）或切换状态筛选看看。审批请求由 agent 的 approval_gate 节点产生。"
        />
      ) : null}

      {items.length >= LIST_LIMIT ? (
        <p className="muted small">
          列表已达 limit={LIST_LIMIT} 上限，可能截断了同一 thread 的动作 —— 这种情况下提交会得到 400
          missing_action_ids，属于预期行为（契约不允许部分裁决）。
        </p>
      ) : null}

      <div className="thread-list">
        {groups.map((group) => {
          const decidedCount = group.items.filter((item) => decisionFor(item).verdict !== null).length;
          const allDecided = decidedCount === group.items.length;
          const busy = submittingThread === group.threadId;
          return (
            <section className="thread-group" key={group.threadId}>
              <header className="thread-head">
                <div>
                  <h3>
                    thread <code>{group.threadId}</code>
                  </h3>
                  <p className="muted small">
                    run_id <code>{group.runId}</code> · interrupt_id <code>{group.interruptId}</code> ·{" "}
                    {group.items.length} 条待裁决
                  </p>
                </div>
                <div className="thread-actions">
                  <StatusPill
                    label={`${decidedCount}/${group.items.length} 已裁决`}
                    tone={allDecided ? "ok" : "warn"}
                  />
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!allDecided || busy}
                    title={allDecided ? "提交本 thread 的全部裁决" : "必须先给每条动作一个裁决（§4.4 规则 2）"}
                    onClick={() => void submit(group)}
                  >
                    {busy ? "提交中…" : `提交 ${group.items.length} 条裁决`}
                  </button>
                </div>
              </header>

              <div className="approval-list">
                {group.items.map((approval) => (
                  <ApprovalCard
                    key={approval.action_id}
                    approval={approval}
                    decision={decisionFor(approval)}
                    paramErrors={paramErrors[approval.action_id] ?? {}}
                    disabled={busy}
                    onVerdict={(verdict) => {
                      setVerdict(approval, verdict);
                    }}
                    onDraftChange={(key, value) => {
                      setDraftValue(approval, key, value);
                    }}
                    onReasonChange={(reason) => {
                      setReason(approval, reason);
                    }}
                  />
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

function ApproveResultPanel({ result, onDismiss }: { result: ApproveResponse; onDismiss: () => void }): JSX.Element {
  return (
    <div className="banner banner-ok result-panel">
      <div className="banner-head">
        <strong>
          裁决已生效 · status={result.status} · thread <code>{result.thread_id}</code>
        </strong>
        <button type="button" className="btn btn-sm btn-ghost" onClick={onDismiss}>
          关闭
        </button>
      </div>
      <table className="table table-compact">
        <thead>
          <tr>
            <th>action_id</th>
            <th>verdict</th>
            <th>执行状态</th>
            <th>action_log_id</th>
            <th>error</th>
          </tr>
        </thead>
        <tbody>
          {result.applied.map((applied) => (
            <tr key={applied.action_id}>
              <td>
                <code>{applied.action_id}</code>
              </td>
              <td>{applied.verdict}</td>
              <td>{applied.status}</td>
              <td>{applied.action_log_id ? <code>{applied.action_log_id.slice(0, 12)}…</code> : "—"}</td>
              <td>{applied.error ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {result.effect_report ? (
        <div className="effect-report">
          <h4>效果回执（effect_report）</h4>
          <p>
            <strong>{result.effect_report.conclusion}</strong>
          </p>
          <p className="muted small">
            归因方式：{result.effect_report.attribution} · 生成于 {formatDateTime(result.effect_report.generated_at)}
          </p>
          <ul className="effect-windows">
            {result.effect_report.windows.map((w) => (
              <li key={w.snapshot_id}>
                T+{w.offset_days} · {w.status} · 到期 {formatDateTime(w.due_at)} · snapshot{" "}
                <code>{w.snapshot_id.slice(0, 8)}…</code>
              </li>
            ))}
          </ul>
          <ul className="impact-notes">
            {result.effect_report.caveats.map((caveat) => (
              <li key={caveat}>{caveat}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
