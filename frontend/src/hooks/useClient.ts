import { useMemo, useSyncExternalStore } from "react";
import { ApiClient } from "../api/client";
import { getApiMode, subscribeApiMode, type ApiMode } from "../api/mode";
import { getMockTransport } from "../mock/mockServer";
import { HttpTransport } from "../api/transport";

export function useApiMode(): ApiMode {
  return useSyncExternalStore(subscribeApiMode, getApiMode, getApiMode);
}

/** 当前模式对应的客户端。mock 传输层是单例，保证假后端的状态（已裁决集合）在页面间保持。 */
export function useClient(): ApiClient {
  const mode = useApiMode();
  return useMemo(() => (mode === "mock" ? new ApiClient(getMockTransport()) : new ApiClient(new HttpTransport())), [mode]);
}
