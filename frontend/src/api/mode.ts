// 运行模式（mock / live）：默认 mock，便于无后端时静态渲染三个页面；
// 生产部署时设 VITE_API_MODE=live（或在页面右上角切换，切换值存 localStorage）。
// 这是**显式**开关，不是"探测失败就偷偷降级"——真实后端挂了在 live 模式下会如实报 NETWORK_ERROR。

export type ApiMode = "mock" | "live";

const STORAGE_KEY = "doudian.apiMode";
const listeners = new Set<() => void>();

function readInitialMode(): ApiMode {
  const fromEnv = import.meta.env.VITE_API_MODE;
  if (fromEnv === "live" || fromEnv === "mock") return fromEnv;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "live" || stored === "mock") return stored;
  } catch {
    // localStorage 不可用（隐私模式）→ 用默认值，不抛错。
  }
  return "mock";
}

let currentMode: ApiMode = readInitialMode();

export function getApiMode(): ApiMode {
  return currentMode;
}

export function setApiMode(mode: ApiMode): void {
  if (mode === currentMode) return;
  currentMode = mode;
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // 忽略：持久化失败不影响本次会话。
  }
  for (const listener of listeners) listener();
}

export function subscribeApiMode(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export const DEFAULT_SHOP_ID: string = import.meta.env.VITE_SHOP_ID ?? "12345678";
