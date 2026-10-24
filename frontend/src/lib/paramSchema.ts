// 动作参数 schema —— 逐条镜像 CONTRACT §5.5 的 ACTION_PARAM_SCHEMA 与 validate_params 规则：
//   1. 键集合必须**恰好等于** schema 的键集合（多一个、少一个都错）；
//   2. int 字段必须是整数，且布尔不算整数（§8.3.3 同一条哲学：True 不是 1）；
//   3. price_cents > 0、stock >= 0、discount_cents > 0、total_count > 0；
//   4. 时间字段必须能被 ISO-8601 解析（后端用 datetime.fromisoformat）。
//
// 前端做这份校验不是为了替代后端（后端仍是唯一权威），而是为了：
//   - 编辑参数时即时标红，不必等一个 400 往返；
//   - mock 假后端复用同一份规则，避免"前端校验通过、mock 放行、真后端 400"的假绿灯。

import type { ActionType } from "../api/types";

export type ParamFieldKind = "int" | "iso_datetime";

export interface ParamFieldSpec {
  key: string;
  kind: ParamFieldKind;
  label: string;
  /** int 字段的下界；minInclusive=true 表示 >=，false 表示 > */
  min?: number;
  minInclusive?: boolean;
  hint?: string;
}

export const ACTION_PARAM_SCHEMA: Record<ActionType, ParamFieldSpec[]> = {
  price_update: [
    { key: "price_cents", kind: "int", label: "新价格", min: 0, minInclusive: false, hint: "整数分，必须 > 0" },
  ],
  stock_update: [
    { key: "stock", kind: "int", label: "新库存", min: 0, minInclusive: true, hint: "件数，必须 >= 0（0 = 下架/断货）" },
  ],
  coupon_create: [
    { key: "discount_cents", kind: "int", label: "券面额", min: 0, minInclusive: false, hint: "整数分，必须 > 0" },
    { key: "total_count", kind: "int", label: "发放张数", min: 0, minInclusive: false, hint: "必须 > 0" },
    { key: "begin_time", kind: "iso_datetime", label: "开始时间", hint: "ISO-8601，如 2026-10-24T00:00:00+08:00" },
    { key: "end_time", kind: "iso_datetime", label: "结束时间", hint: "ISO-8601，且必须晚于开始时间" },
  ],
};

/** 输入框里的原始文本 → 提交用的 params。 */
export type ParamDraft = Record<string, string>;

export function draftFromParams(actionType: ActionType, params: Record<string, unknown>): ParamDraft {
  const draft: ParamDraft = {};
  for (const spec of ACTION_PARAM_SCHEMA[actionType]) {
    const value = params[spec.key];
    draft[spec.key] = value === undefined || value === null ? "" : String(value);
  }
  return draft;
}

export type ValidationResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; errors: Record<string, string> };

const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?$/;

export function isIsoDateTime(value: string): boolean {
  if (!ISO_RE.test(value)) return false;
  const ms = Date.parse(value);
  return Number.isFinite(ms);
}

/**
 * 校验 draft（表单字符串）并产出 params（真实类型）。
 * draft 与 schema 的键集合必须完全一致——编辑表单永远按 schema 渲染，所以缺键意味着 bug，
 * 这里同样按"多/少都错"处理，不做静默补齐。
 */
export function validateDraft(actionType: ActionType, draft: ParamDraft): ValidationResult {
  const specs = ACTION_PARAM_SCHEMA[actionType];
  const errors: Record<string, string> = {};
  const value: Record<string, unknown> = {};

  const specKeys = new Set(specs.map((s) => s.key));
  for (const key of Object.keys(draft)) {
    if (!specKeys.has(key)) errors[key] = `schema 不允许字段 ${key}`;
  }

  for (const spec of specs) {
    const raw = draft[spec.key];
    if (raw === undefined || raw.trim() === "") {
      errors[spec.key] = "必填";
      continue;
    }
    const text = raw.trim();
    if (spec.kind === "int") {
      if (!/^-?\d+$/.test(text)) {
        errors[spec.key] = "必须是整数（不接受小数/科学计数法/布尔）";
        continue;
      }
      const num = Number(text);
      if (!Number.isSafeInteger(num)) {
        errors[spec.key] = "超出安全整数范围";
        continue;
      }
      if (spec.min !== undefined) {
        const bad = spec.minInclusive === true ? num < spec.min : num <= spec.min;
        if (bad) {
          errors[spec.key] = spec.minInclusive === true ? `必须 >= ${spec.min}` : `必须 > ${spec.min}`;
          continue;
        }
      }
      value[spec.key] = num;
      continue;
    }
    if (!isIsoDateTime(text)) {
      errors[spec.key] = "必须是 ISO-8601 时间";
      continue;
    }
    value[spec.key] = text;
  }

  if (actionType === "coupon_create" && !errors["begin_time"] && !errors["end_time"]) {
    const begin = Date.parse(String(value["begin_time"]));
    const end = Date.parse(String(value["end_time"]));
    if (Number.isFinite(begin) && Number.isFinite(end) && end <= begin) {
      errors["end_time"] = "结束时间必须晚于开始时间";
    }
  }

  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, value };
}

/** 后端（或 mock）侧校验：输入已经是 JSON 值而不是表单字符串。 */
export function validateParams(actionType: ActionType, params: unknown): ValidationResult {
  if (params === null || typeof params !== "object" || Array.isArray(params)) {
    return { ok: false, errors: { _: "params 必须是对象" } };
  }
  const obj = params as Record<string, unknown>;
  const specs = ACTION_PARAM_SCHEMA[actionType];
  const specKeys = new Set(specs.map((s) => s.key));
  const errors: Record<string, string> = {};

  for (const key of Object.keys(obj)) {
    if (!specKeys.has(key)) errors[key] = `schema 不允许字段 ${key}`;
  }
  for (const spec of specs) {
    if (!(spec.key in obj)) {
      errors[spec.key] = "缺少字段";
      continue;
    }
    const v = obj[spec.key];
    if (spec.kind === "int") {
      if (typeof v !== "number" || !Number.isInteger(v)) {
        errors[spec.key] = "必须是整数";
        continue;
      }
      if (spec.min !== undefined) {
        const bad = spec.minInclusive === true ? v < spec.min : v <= spec.min;
        if (bad) errors[spec.key] = spec.minInclusive === true ? `必须 >= ${spec.min}` : `必须 > ${spec.min}`;
      }
      continue;
    }
    if (typeof v !== "string" || !isIsoDateTime(v)) errors[spec.key] = "必须是 ISO-8601 时间字符串";
  }

  if (actionType === "coupon_create" && !errors["begin_time"] && !errors["end_time"]) {
    const begin = Date.parse(String(obj["begin_time"]));
    const end = Date.parse(String(obj["end_time"]));
    if (Number.isFinite(begin) && Number.isFinite(end) && end <= begin) errors["end_time"] = "结束时间必须晚于开始时间";
  }

  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, value: obj };
}

/** 让用户看到"这个动作能改哪些键"（编辑表单与详情面板共用）。 */
export function describeParams(actionType: ActionType, params: Record<string, unknown>): string {
  const specs = ACTION_PARAM_SCHEMA[actionType];
  return specs
    .map((spec) => {
      const value = params[spec.key];
      if (spec.kind === "int" && typeof value === "number") {
        return spec.key.endsWith("_cents") ? `${spec.label} ¥${(value / 100).toFixed(2)}` : `${spec.label} ${value}`;
      }
      return `${spec.label} ${value === undefined || value === null ? "—" : String(value)}`;
    })
    .join(" · ");
}
