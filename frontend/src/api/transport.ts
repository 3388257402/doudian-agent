// 传输层：把"怎么把请求发出去"与"发什么/怎么解析"分开。
// - HttpTransport：fetch 到相对路径 /api/*（dev 由 vite proxy 转发，见 vite.config.ts）
// - MockTransport：在浏览器里跑一份按契约实现的假后端（frontend/mock/*.json fixtures），
//   用于无后端时静态渲染三个页面 + 演示 400/409/502 的错误 UI 分支。
// 两条路径共用同一个 client.ts，因此页面代码不需要知道当前是哪种模式。

import type { ApiErrorCode } from "./types";

export type HttpMethod = "GET" | "POST";

export interface ApiRequest {
  method: HttpMethod;
  /** 相对路径，必须以 /api/ 开头（CONTRACT §11：只请求 /api/*）。 */
  path: string;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
}

export interface TransportResponse {
  status: number;
  /** 已解析的 JSON；无法解析时为 undefined（由 client 判为 MALFORMED_RESPONSE）。 */
  body: unknown;
  headers: Record<string, string>;
}

export interface ApiTransport {
  readonly kind: "http" | "mock";
  send(req: ApiRequest, signal: AbortSignal): Promise<TransportResponse>;
}

/** 传输层自身失败（没拿到 HTTP 响应）。由 client 翻译成 NETWORK_ERROR / TIMEOUT / ABORTED。 */
export class TransportError extends Error {
  readonly reason: "network" | "timeout" | "aborted";
  constructor(reason: "network" | "timeout" | "aborted", message: string) {
    super(message);
    this.name = "TransportError";
    this.reason = reason;
  }
}

export function buildUrl(path: string, query: ApiRequest["query"]): string {
  if (!path.startsWith("/api/")) {
    // 契约外的路径一律拒绝，避免前端悄悄打到别的服务上。
    throw new Error(`拒绝请求非 /api/* 路径: ${path}`);
  }
  if (!query) return path;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

export class HttpTransport implements ApiTransport {
  readonly kind = "http" as const;

  async send(req: ApiRequest, signal: AbortSignal): Promise<TransportResponse> {
    const url = buildUrl(req.path, req.query);
    let response: Response;
    try {
      response = await fetch(url, {
        method: req.method,
        headers: req.body === undefined ? undefined : { "Content-Type": "application/json; charset=utf-8" },
        body: req.body === undefined ? undefined : JSON.stringify(req.body),
        signal,
        credentials: "same-origin",
      });
    } catch (err) {
      if (signal.aborted) throw new TransportError("aborted", "请求已取消");
      throw new TransportError("network", err instanceof Error ? err.message : String(err));
    }

    const headers: Record<string, string> = {};
    response.headers.forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });

    const text = await response.text();
    let body: unknown;
    if (text.length > 0) {
      try {
        body = JSON.parse(text);
      } catch {
        body = undefined;
      }
    }
    return { status: response.status, body, headers };
  }
}

/** 假后端返回契约包络时的构造工具（mock 内部用）。 */
export function envelope(
  status: number,
  code: ApiErrorCode,
  message: string,
  details: Record<string, unknown> | null,
  traceId: string,
): TransportResponse {
  return {
    status,
    body: { error: { code, message, details, trace_id: traceId } },
    headers: { "x-trace-id": traceId },
  };
}
