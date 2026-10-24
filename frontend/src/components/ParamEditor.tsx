// 参数编辑表单：字段完全由 ACTION_PARAM_SCHEMA 驱动（与后端 §5.5 的 schema 一一对应），
// 所以不会出现"前端能填一个后端不认识的键"这种情况。
import type { ActionType } from "../api/types";
import { centsPreview } from "../lib/format";
import { ACTION_PARAM_SCHEMA, type ParamDraft } from "../lib/paramSchema";

export interface ParamEditorProps {
  actionType: ActionType;
  draft: ParamDraft;
  errors: Record<string, string>;
  disabled?: boolean;
  onChange: (key: string, value: string) => void;
}

export function ParamEditor({ actionType, draft, errors, disabled = false, onChange }: ParamEditorProps): JSX.Element {
  const specs = ACTION_PARAM_SCHEMA[actionType];
  return (
    <div className="param-editor">
      {specs.map((spec) => {
        const error = errors[spec.key];
        const raw = draft[spec.key] ?? "";
        return (
          <label className={`field ${error ? "field-error" : ""}`} key={spec.key}>
            <span className="field-label">
              {spec.label}
              <code className="field-key">{spec.key}</code>
            </span>
            <input
              type="text"
              inputMode={spec.kind === "int" ? "numeric" : "text"}
              value={raw}
              disabled={disabled}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? `err-${spec.key}` : undefined}
              onChange={(event) => {
                onChange(spec.key, event.target.value);
              }}
              placeholder={spec.kind === "int" ? "整数" : "2026-10-24T00:00:00+08:00"}
            />
            <span className="field-hint">
              {spec.hint}
              {spec.kind === "int" && spec.key.endsWith("_cents") ? ` · 预览 ${centsPreview(raw)}` : ""}
            </span>
            {error ? (
              <span className="field-msg" id={`err-${spec.key}`}>
                {error}
              </span>
            ) : null}
          </label>
        );
      })}
      <p className="param-note">
        提交时只有这些键会被带上（后端 §5.5 要求键集合恰好相等，多一个少一个都会 400）。
      </p>
    </div>
  );
}
