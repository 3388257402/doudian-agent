import { ApiFailure } from "./errors";
import { TransportError, type ApiRequest, type ApiTransport, type TransportResponse } from "./transport";
import type {
  AlertsResponse,
  ApiError,
  ApiErrorCode,
  ApproveRequest,
  ApproveResponse,
  LogItem,
  LogsResponse,
  MetricsResponse,
  RunResponse,
} from "./types";

export interface AlertsQuery {
  shop_id?: string | null;
  status?: "pending" | "approved" | "rejected" | "expired" | "all";
  limit?: number;
  before_id?: number | null;
}

export interface MetricsQuery {
  shop_id: string;
  date_from?: string | null;
  date_to?: string | null;
  granularity?: "day";
}

export interface LogsQuery {
  run_id?: string | null;
  level?: LogItem["level"] | null;
  event?: string | null;
  limit?: number;
  before_seq?: number | null;
}

export interface RunAgentRequest {
  shop_id: string;
  run_id?: string | null;
  trigger?: "manual" | "scheduled" | "retry";
  dry_run?: boolean;
}

export interface HealthzResponse {
  status: "ok" | "degraded";
  version: string;
  db: string;
  checkpointer: string;
  time: string;
}

export interface ApiClientOptions {
  /** 单次请求预算；超时 → ApiFailure("TIMEOUT")。默认 12s。 */
  timeoutMs?: number;
}

const CONTRACT_CODES: ReadonlySet<string> = new Set<ApiErrorCode>([
  "VALIDATION_ERROR",
  "THREAD_NOT_FOUND",
  "APPROVAL_NOT_FOUND",
  "RESOURCE_NOT_FOUND",
  "DUPLICATE_DECISION",
  "RUN_IN_PROGRESS",
  "RUN_STATE_CONFLICT",
  "UPSTREAM_ERROR",
  "INTERNAL_ERROR",
]);

/**
 * 契约 §7 的类型化客户端。所有页面只通过它访问后端。
 * 错误一律以 ApiFailure 抛出（携带契约 code / details / trace_id），页面按 code 分支。
 */
export class ApiClient {
  private readonly transport: ApiTransport;
  private readonly timeoutMs: number;

  constructor(transport: ApiTransport, options: ApiClientOptions = {}) {
    this.transport = transport;
    this.timeoutMs = options.timeoutMs ?? 12_000;
  }

  get transportKind(): "http" | "mock" {
    return this.transport.kind;
  }

  listAlerts(query: AlertsQuery = {}, signal?: AbortSignal): Promise<AlertsResponse> {
    return this.call<AlertsResponse>(
      {
        method: "GET",
        path: "/api/alerts",
        query: {
          shop_id: query.shop_id ?? null,
          status: query.status ?? "pending",
          limit: query.limit ?? 50,
          before_id: query.before_id ?? null,
        },
      },
      signal,
    );
  }

  approve(body: ApproveRequest, signal?: AbortSignal): Promise<ApproveResponse> {
    return this.call<ApproveResponse>({ method: "POST", path: "/api/approve", body }, signal);
  }

  getMetrics(query: MetricsQuery, signal?: AbortSignal): Promise<MetricsResponse> {
    return this.call<MetricsResponse>(
      {
        method: "GET",
        path: "/api/metrics",
        query: {
          shop_id: query.shop_id,
          date_from: query.date_from ?? null,
          date_to: query.date_to ?? null,
          granularity: query.granularity ?? "day",
        },
      },
      signal,
    );
  }

  getLogs(query: LogsQuery = {}, signal?: AbortSignal): Promise<LogsResponse> {
    return this.call<LogsResponse>(
      {
        method: "GET",
        path: "/api/logs",
        query: {
          run_id: query.run_id ?? null,
          level: query.level ?? null,
          event: query.event ?? null,
          limit: query.limit ?? 100,
          before_seq: query.before_seq ?? null,
        },
      },
      signal,
    );
  }

  runAgent(body: RunAgentRequest, signal?: AbortSignal): Promise<RunResponse> {
    return this.call<RunResponse>({ method: "POST", path: "/api/agent/run", body }, signal);
  }

  healthz(signal?: AbortSignal): Promise<HealthzResponse> {
    return this.call<HealthzResponse>({ method: "GET", path: "/api/healthz" }, signal);
  }

  private async call<T>(req: ApiRequest, external?: AbortSignal): Promise<T> {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.timeoutMs);
    const onExternalAbort = (): void => controller.abort();
    if (external) {
      if (external.aborted) controller.abort();
      else external.addEventListener("abort", onExternalAbort, { once: true });
    }

    let res: TransportResponse;
    try {
      res = await this.transport.send(req, controller.signal);
    } catch (err) {
      if (err instanceof TransportError) {
        if (err.reason === "timeout" || timedOut) {
          throw new ApiFailure("TIMEOUT", `请求超时（${this.timeoutMs}ms）：${req.method} ${req.path}`);
        }
        if (err.reason === "aborted") {
          throw new ApiFailure("ABORTED", `请求已取消：${req.method} ${req.path}`);
        }
        throw new ApiFailure("NETWORK_ERROR", `无法连接后端：${err.message}（${req.method} ${req.path}）`);
      }
      throw new ApiFailure("NETWORK_ERROR", err instanceof Error ? err.message : String(err));
    } finally {
      clearTimeout(timer);
      external?.removeEventListener("abort", onExternalAbort);
    }

    const traceHeader = res.headers["x-trace-id"] ?? null;

    if (res.status >= 400) {
      const env = extractEnvelope(res.body);
      if (env) {
        const code: ApiErrorCode | null = CONTRACT_CODES.has(env.code) ? (env.code as ApiErrorCode) : null;
        if (code === null) {
          throw new ApiFailure("MALFORMED_RESPONSE", `后端返回了契约外的错误码 ${env.code}`, {
            httpStatus: res.status,
            details: env.details,
            traceId: env.trace_id ?? traceHeader,
          });
        }
        throw new ApiFailure(code, env.message, {
          httpStatus: res.status,
          details: env.details,
          traceId: env.trace_id ?? traceHeader,
        });
      }
      // 4xx/5xx 但没有 §7.1 包络：代理/网关错误页。不猜 code，如实报格式不符。
      throw new ApiFailure("MALFORMED_RESPONSE", `HTTP ${res.status} 响应缺少错误包络（§7.1）`, {
        httpStatus: res.status,
        traceId: traceHeader,
      });
    }

    if (res.body === undefined || res.body === null || typeof res.body !== "object") {
      throw new ApiFailure("MALFORMED_RESPONSE", `HTTP ${res.status} 响应体不是 JSON 对象`, {
        httpStatus: res.status,
        traceId: traceHeader,
      });
    }
    return res.body as T;
  }
}

interface EnvelopeShape {
  code: string;
  message: string;
  details: Record<string, unknown> | null;
  trace_id: string | null;
}

function extractEnvelope(body: unknown): EnvelopeShape | null {
  if (body === null || typeof body !== "object") return null;
  const err = (body as Partial<ApiError>).error;
  if (!err || typeof err !== "object") return null;
  const { code, message, details, trace_id } = err as ApiError["error"];
  if (typeof code !== "string" || typeof message !== "string") return null;
  return {
    code,
    message,
    details: (details ?? null) as Record<string, unknown> | null,
    trace_id: typeof trace_id === "string" ? trace_id : null,
  };
}
