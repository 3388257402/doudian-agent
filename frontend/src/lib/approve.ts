// `POST /api/approve` 的请求体构造（纯函数，无 React、无 IO）。
//
// 为什么把它从页面里抽出来：§7.4 有两条**硬规则**，它们是这个项目里唯一"错一次就产生真实副作用"的字段：
//   1. verdict=edit  → edited_params 必须是校验通过的参数对象（非空）；
//   2. verdict=approve|reject → edited_params 必须恒为 null（不是 {}，不是省略）。
// 写在 JSX 的 submit 闭包里，这两条只能靠肉眼保证；抽成纯函数后可以被脚本直接断言（scripts/verify-suite.ts）。
//
// 字段集逐字对齐 §7.4：顶层 {thread_id, interrupt_id, operator, decisions}；
// decisions[] {action_id, verdict, edited_params, operator, reason}。

import type { ApproveRequest, DecisionInput, PendingApproval, Verdict } from "../api/types";
import { validateDraft, type ParamDraft } from "./paramSchema";

/** 页面上的一条裁决草稿（表单态：参数是字符串）。 */
export interface DecisionDraft {
  verdict: Verdict | null;
  /** 仅在 verdict === "edit" 时有意义；初始值是提议参数 */
  draft: ParamDraft;
  reason: string;
}

export interface BuildApproveArgs {
  threadId: string;
  interruptId: string;
  operator: string;
  /** 同一 thread 下所有待裁决动作（§4.4 规则 2：必须整批提交） */
  items: PendingApproval[];
  /** 取某条动作的草稿（页面用 decisions[action_id] ?? 默认草稿） */
  decisionOf: (approval: PendingApproval) => DecisionDraft;
}

export type BuildApproveResult =
  | { ok: true; body: ApproveRequest }
  | {
      ok: false;
      /** 还没有裁决的动作（§4.4 规则 2：少一条就会被后端 400） */
      missingActionIds: string[];
      /** edit 的参数校验错误，按 action_id 分组，字段名 → 错误文案 */
      paramErrors: Record<string, Record<string, string>>;
    };

export function buildApproveRequest(args: BuildApproveArgs): BuildApproveResult {
  const decisions: DecisionInput[] = [];
  const missingActionIds: string[] = [];
  const paramErrors: Record<string, Record<string, string>> = {};

  for (const approval of args.items) {
    const decision = args.decisionOf(approval);
    if (decision.verdict === null) {
      missingActionIds.push(approval.action_id);
      continue;
    }

    // 硬规则 1/2 的唯一出口：只有 edit 会产出非 null 的 edited_params。
    let editedParams: Record<string, unknown> | null = null;
    if (decision.verdict === "edit") {
      const validated = validateDraft(approval.action_type, decision.draft);
      if (!validated.ok) {
        paramErrors[approval.action_id] = validated.errors;
        continue;
      }
      editedParams = validated.value;
    }

    decisions.push({
      action_id: approval.action_id,
      verdict: decision.verdict,
      edited_params: editedParams,
      operator: args.operator,
      // 空理由写 null（契约 §0：没有就是 null，不用空字符串）
      reason: decision.reason.trim() === "" ? null : decision.reason.trim(),
    });
  }

  if (missingActionIds.length > 0 || Object.keys(paramErrors).length > 0) {
    return { ok: false, missingActionIds, paramErrors };
  }

  return {
    ok: true,
    body: {
      thread_id: args.threadId,
      interrupt_id: args.interruptId,
      operator: args.operator,
      decisions,
    },
  };
}
