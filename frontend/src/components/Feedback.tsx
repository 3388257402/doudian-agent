// 统一的错误横幅：按 error.code 取文案与建议动作（不按 HTTP 状态猜）。
// 展示三样东西：契约 code + message、details 的字段级明细、trace_id（本地 bug 只能靠它定位）。
import { ApiFailure, detailsLines, feedbackFor } from "../api/errors";

export interface ErrorBannerProps {
  failure: ApiFailure;
  onRetry?: () => void;
  onRefresh?: () => void;
  onDismiss?: () => void;
  /** 表单场景下额外传入的字段级错误（来自前端本地校验），与 details 合并展示 */
  extraLines?: string[];
}

export function ErrorBanner({ failure, onRetry, onRefresh, onDismiss, extraLines = [] }: ErrorBannerProps): JSX.Element {
  const feedback = feedbackFor(failure.code);
  const lines = [...detailsLines(failure.details), ...extraLines];

  return (
    <div className="banner banner-error" role="alert">
      <div className="banner-head">
        <div>
          <strong>{feedback.title}</strong>
          <code className="code-chip">{failure.code}</code>
          {failure.httpStatus !== null ? <span className="muted">HTTP {failure.httpStatus}</span> : null}
        </div>
        <div className="banner-actions">
          {feedback.action === "retry" && onRetry ? (
            <button type="button" className="btn btn-sm" onClick={onRetry}>
              重试
            </button>
          ) : null}
          {feedback.action === "refresh" && onRefresh ? (
            <button type="button" className="btn btn-sm" onClick={onRefresh}>
              刷新列表
            </button>
          ) : null}
          {onDismiss ? (
            <button type="button" className="btn btn-sm btn-ghost" onClick={onDismiss} aria-label="关闭">
              关闭
            </button>
          ) : null}
        </div>
      </div>
      <p className="banner-hint">{feedback.hint}</p>
      <p className="banner-message">{failure.message}</p>
      {lines.length > 0 ? (
        <ul className="banner-details">
          {lines.map((line, index) => (
            <li key={`${line}-${index}`}>{line}</li>
          ))}
        </ul>
      ) : null}
      {failure.traceId ? <p className="banner-trace">trace_id: {failure.traceId}</p> : null}
    </div>
  );
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: JSX.Element }): JSX.Element {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {hint ? <p className="empty-hint">{hint}</p> : null}
      {action}
    </div>
  );
}

export function LoadingBlock({ label = "加载中…" }: { label?: string }): JSX.Element {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}
