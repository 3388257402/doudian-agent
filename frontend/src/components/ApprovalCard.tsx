// 单条待审批卡片：异常描述 + 建议动作 + 影响预估 + SKU 与当前指标 + 裁决控件。
import type { PendingApproval, Verdict } from "../api/types";
import { formatDateTime } from "../lib/format";
import { evidenceEntries, evidenceMetrics, evidenceScalars } from "../lib/evidence";
import { assessImpact } from "../lib/impact";
import { draftFromParams } from "../lib/paramSchema";
import type { DecisionDraft } from "../lib/approve";
import { ParamEditor } from "./ParamEditor";
import { actionTypeLabel, approvalStatusLabel, approvalStatusTone, riskLabel, riskTone, StatusPill } from "./StatusPill";

export function makeDecisionDraft(approval: PendingApproval): DecisionDraft {
  return { verdict: null, draft: draftFromParams(approval.action_type, approval.params), reason: "" };
}

export type { DecisionDraft };

export interface ApprovalCardProps {
  approval: PendingApproval;
  decision: DecisionDraft;
  paramErrors: Record<string, string>;
  disabled?: boolean;
  onVerdict: (verdict: Verdict) => void;
  onDraftChange: (key: string, value: string) => void;
  onReasonChange: (reason: string) => void;
}

export function ApprovalCard({
  approval,
  decision,
  paramErrors,
  disabled = false,
  onVerdict,
  onDraftChange,
  onReasonChange,
}: ApprovalCardProps): JSX.Element {
  const impact = assessImpact(approval);
  const metrics = evidenceEntries(evidenceMetrics(approval.evidence));
  const scalars = evidenceScalars(approval.evidence);

  return (
    <article className={`card approval-card ${decision.verdict ? `verdict-${decision.verdict}` : ""}`}>
      <header className="approval-head">
        <div className="approval-title">
          <StatusPill label={actionTypeLabel(approval.action_type)} tone="info" />
          <StatusPill label={riskLabel(approval.risk)} tone={riskTone(approval.risk)} title={`后端 risk=${approval.risk}`} />
          <StatusPill
            label={approvalStatusLabel(approval.status)}
            tone={approvalStatusTone(approval.status)}
            title={`approval_request.status=${approval.status}`}
          />
          <h3>
            {actionTypeLabel(approval.action_type)} · SKU <code>{approval.target}</code>
          </h3>
        </div>
        <div className="approval-meta">
          <span title={approval.action_id}>action_id: <code>{shorten(approval.action_id)}</code></span>
          <span>创建于 {formatDateTime(approval.created_at)}</span>
        </div>
      </header>

      <p className="approval-reason">{approval.reason}</p>

      <div className="approval-grid">
        <section>
          <h4>当前指标（evidence.metrics）</h4>
          {metrics.length === 0 ? (
            <p className="muted">evidence 里没有 metrics。</p>
          ) : (
            <dl className="kv">
              {metrics.map((entry) => (
                <div key={entry.key} className="kv-row">
                  <dt title={entry.key}>{entry.label}</dt>
                  <dd>{entry.value}</dd>
                </div>
              ))}
            </dl>
          )}
          {scalars.length > 0 ? (
            <p className="muted small">
              证据来源：{scalars.map((s) => `${s.label}=${s.value}`).join(" · ")}
            </p>
          ) : null}
        </section>

        <section>
          <h4>影响预估</h4>
          <dl className="kv">
            {impact.lines.map((line) => (
              <div key={line.label} className="kv-row">
                <dt>{line.label}</dt>
                <dd className={`tone-${line.tone}`}>{line.value}</dd>
              </div>
            ))}
          </dl>
          {impact.notes.length > 0 ? (
            <ul className="impact-notes">
              {impact.notes.map((note) => (
                <li key={note} className={note.includes("不一致") ? "tone-warn" : ""}>
                  {note}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      </div>

      <section className="approval-params">
        <h4>建议参数（params）</h4>
        <pre className="json-block">{JSON.stringify(approval.params, null, 2)}</pre>
      </section>

      <section className="approval-decide">
        <h4>裁决</h4>
        <div className="verdict-buttons" role="group" aria-label="裁决">
          <button
            type="button"
            className={`btn ${decision.verdict === "approve" ? "btn-ok" : ""}`}
            disabled={disabled}
            aria-pressed={decision.verdict === "approve"}
            onClick={() => {
              onVerdict("approve");
            }}
          >
            批准
          </button>
          <button
            type="button"
            className={`btn ${decision.verdict === "reject" ? "btn-bad" : ""}`}
            disabled={disabled}
            aria-pressed={decision.verdict === "reject"}
            onClick={() => {
              onVerdict("reject");
            }}
          >
            拒绝
          </button>
          <button
            type="button"
            className={`btn ${decision.verdict === "edit" ? "btn-warn" : ""}`}
            disabled={disabled}
            aria-pressed={decision.verdict === "edit"}
            onClick={() => {
              onVerdict("edit");
            }}
          >
            编辑参数
          </button>
          {decision.verdict !== null ? (
            <button
              type="button"
              className="btn btn-ghost"
              disabled={disabled}
              onClick={() => {
                onVerdict(decision.verdict === "edit" ? "approve" : "edit");
              }}
              title="在当前裁决之间快速切换"
            >
              {decision.verdict === "edit" ? "改为按原参数批准" : "改为编辑参数"}
            </button>
          ) : null}
        </div>

        {decision.verdict === "edit" ? (
          <>
            <p className="muted small">
              verdict=edit 时提交的是「编辑后的最终参数」（edited_params），后端会用生效参数重算
              idempotency_key（§4.5）。
            </p>
            <ParamEditor
              actionType={approval.action_type}
              draft={decision.draft}
              errors={paramErrors}
              disabled={disabled}
              onChange={onDraftChange}
            />
          </>
        ) : null}

        {decision.verdict === "reject" ? (
          <p className="muted small">verdict=reject 时 edited_params 必须为 null，本动作不会写 action_log（§7.4）。</p>
        ) : null}

        <label className="field">
          <span className="field-label">裁决理由（可选，建议写）</span>
          <input
            type="text"
            value={decision.reason}
            disabled={disabled}
            maxLength={500}
            onChange={(event) => {
              onReasonChange(event.target.value);
            }}
            placeholder="例如：降幅收敛到 3% / 该 SKU 已停售"
          />
        </label>
      </section>
    </article>
  );
}

function shorten(actionId: string): string {
  const parts = actionId.split(":");
  if (parts.length < 3) return actionId;
  return `${parts[0]}:${parts[1]}:${parts[2]?.slice(-6) ?? ""}`;
}
