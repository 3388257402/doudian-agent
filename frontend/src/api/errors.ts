// 统一错误处理：前端**只按 error.code 分支**（CONTRACT §11 前端自检第 3 条），不按 HTTP 状态猜。
// 唯一例外是传输层失败（没拿到包络），那时根本没有 code，于是合成一个本地 code，
// 并显式标成非契约 code —— 不允许把"网络断了"伪装成 UPSTREAM_ERROR 或 INTERNAL_ERROR。

import type { ApiErrorCode } from "./types";

/** 契约内的 9 个错误码（§7.1）+ 4 个纯前端 code（后端永远不会返回）。 */
export type FailureCode =
  | ApiErrorCode
  | "NETWORK_ERROR"
  | "TIMEOUT"
  | "MALFORMED_RESPONSE"
  | "ABORTED";

/** 前端拿到错误后该做什么。 */
export type UiAction =
  | "refresh" // 列表已过期 → 刷新
  | "retry" // 上游抖动 → 提供重试
  | "fix_form" // 参数/裁决校验失败 → 标红表单并展示 details
  | "show_trace" // 本地 bug → 展示 trace_id 让后端定位
  | "empty" // 无数据 → 空态
  | "disable"; // 状态冲突 → 禁用入口

export interface UiFeedback {
  title: string;
  hint: string;
  action: UiAction;
  /** 只有传输抖动与 502 值得重试；校验类错误重试一百次也一样。 */
  retriable: boolean;
}

export class ApiFailure extends Error {
  readonly code: FailureCode;
  readonly httpStatus: number | null;
  readonly details: Record<string, unknown> | null;
  readonly traceId: string | null;

  constructor(
    code: FailureCode,
    message: string,
    init: { httpStatus?: number | null; details?: Record<string, unknown> | null; traceId?: string | null } = {},
  ) {
    super(message);
    this.name = "ApiFailure";
    this.code = code;
    this.httpStatus = init.httpStatus ?? null;
    this.details = init.details ?? null;
    this.traceId = init.traceId ?? null;
  }

  get isContractCode(): boolean {
    return (
      this.code !== "NETWORK_ERROR" &&
      this.code !== "TIMEOUT" &&
      this.code !== "MALFORMED_RESPONSE" &&
      this.code !== "ABORTED"
    );
  }
}

const FEEDBACK: Record<FailureCode, UiFeedback> = {
  VALIDATION_ERROR: {
    title: "参数校验未通过",
    hint: "按下面的字段错误修改后重新提交；未做任何裁决提交。",
    action: "fix_form",
    retriable: false,
  },
  THREAD_NOT_FOUND: {
    title: "该审批已失效",
    hint: "thread 在 checkpointer 中已无 checkpoint，请刷新列表。",
    action: "refresh",
    retriable: false,
  },
  APPROVAL_NOT_FOUND: {
    title: "该审批已失效",
    hint: "挂起的 interrupt 与提交的 interrupt_id 不一致（可能已被他人裁决），请刷新列表。",
    action: "refresh",
    retriable: false,
  },
  RESOURCE_NOT_FOUND: { title: "没有数据", hint: "该店铺在所选区间内没有任何指标。", action: "empty", retriable: false },
  DUPLICATE_DECISION: {
    title: "该动作已被裁决",
    hint: "同一条动作不能裁决两次（幂等保护），列表已刷新。",
    action: "refresh",
    retriable: false,
  },
  RUN_IN_PROGRESS: {
    title: "该 run 正在运行",
    hint: "同一 (shop_id, run_id) 不允许并发，等本轮结束后再触发。",
    action: "disable",
    retriable: false,
  },
  RUN_STATE_CONFLICT: {
    title: "thread 状态冲突",
    hint: "thread 存在但当前不处于 awaiting_approval，刷新后按最新状态操作。",
    action: "refresh",
    retriable: false,
  },
  UPSTREAM_ERROR: {
    title: "抖店上游不可用",
    hint: "上游拒绝或不可达，本地未产生副作用；可稍后重试。",
    action: "retry",
    retriable: true,
  },
  INTERNAL_ERROR: {
    title: "服务内部错误",
    hint: "这不是你的操作问题，把 trace_id 报给后端定位。",
    action: "show_trace",
    retriable: false,
  },
  NETWORK_ERROR: {
    title: "无法连接后端",
    hint: "请求没有到达服务（网络/进程未起）。检查后端是否在 127.0.0.1:8000，或切到 Mock 模式。",
    action: "retry",
    retriable: true,
  },
  TIMEOUT: {
    title: "请求超时",
    hint: "后端在超时预算内没有响应；重试前先确认它没有卡住。",
    action: "retry",
    retriable: true,
  },
  MALFORMED_RESPONSE: {
    title: "响应格式不符契约",
    hint: "收到的 JSON 不是 §7 定义的包络（可能是代理返回的 HTML 错误页）。",
    action: "show_trace",
    retriable: false,
  },
  ABORTED: { title: "请求已取消", hint: "组件卸载或新一轮轮询顶替了这次请求。", action: "none" as UiAction, retriable: false },
};

export function feedbackFor(code: FailureCode): UiFeedback {
  return FEEDBACK[code];
}

/**
 * 把 details 里的字段级错误摊平成可读行（VALIDATION_ERROR 的 details 是
 * {"missing_action_ids": [...]} / {"params": {...}} 这类形状，键名由后端定义）。
 */
export function detailsLines(details: Record<string, unknown> | null): string[] {
  if (!details) return [];
  const out: string[] = [];
  for (const [key, value] of Object.entries(details)) {
    if (value === null || value === undefined) {
      out.push(`${key}: null`);
    } else if (Array.isArray(value)) {
      out.push(`${key}: ${value.map((v) => stringifyLeaf(v)).join(", ")}`);
    } else if (typeof value === "object") {
      for (const [k2, v2] of Object.entries(value as Record<string, unknown>)) {
        out.push(`${key}.${k2}: ${stringifyLeaf(v2)}`);
      }
    } else {
      out.push(`${key}: ${stringifyLeaf(value)}`);
    }
  }
  return out;
}

function stringifyLeaf(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function toApiFailure(err: unknown): ApiFailure {
  if (err instanceof ApiFailure) return err;
  const message = err instanceof Error ? err.message : String(err);
  return new ApiFailure("INTERNAL_ERROR", `前端未预期异常：${message}`);
}
