import { useEffect, useState } from "react";
import { getApiMode, setApiMode, type ApiMode } from "./api/mode";
import { useApiMode, useClient } from "./hooks/useClient";
import { useResource } from "./hooks/useResource";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LogsPage } from "./pages/LogsPage";

type Tab = "approvals" | "dashboard" | "logs";

const TABS: { key: Tab; label: string; hash: string }[] = [
  { key: "approvals", label: "审批台", hash: "#/approvals" },
  { key: "dashboard", label: "效果看板", hash: "#/dashboard" },
  { key: "logs", label: "执行日志", hash: "#/logs" },
];

function tabFromHash(hash: string): Tab {
  const found = TABS.find((tab) => tab.hash === hash);
  return found ? found.key : "approvals";
}

export function App(): JSX.Element {
  const [tab, setTab] = useState<Tab>(() => tabFromHash(window.location.hash));

  useEffect(() => {
    const onHashChange = (): void => {
      setTab(tabFromHash(window.location.hash));
    };
    window.addEventListener("hashchange", onHashChange);
    return () => {
      window.removeEventListener("hashchange", onHashChange);
    };
  }, []);

  return (
    <div className="app">
      <Header tab={tab} />
      <main className="app-main">
        {tab === "approvals" ? <ApprovalsPage /> : null}
        {tab === "dashboard" ? <DashboardPage /> : null}
        {tab === "logs" ? <LogsPage /> : null}
      </main>
      <footer className="app-footer">
        <span>
          抖店智能运营 Agent · 控制台（只读 + 审批）。契约来源：<code>docs/CONTRACT.md</code> §7 REST / §12 TS 类型。
        </span>
        <span className="muted">
          当前模式：<strong>{getApiMode() === "mock" ? "Mock（fixtures 假后端）" : "实时（vite proxy → 127.0.0.1:8000）"}</strong>
        </span>
      </footer>
    </div>
  );
}

function Header({ tab }: { tab: Tab }): JSX.Element {
  const mode = useApiMode();
  const client = useClient();
  const health = useResource({
    key: `healthz|${mode}`,
    intervalMs: 15_000,
    fetcher: (signal) => client.healthz(signal),
  });

  const healthTone = health.error ? "bad" : health.data?.status === "ok" ? "ok" : health.data ? "warn" : "muted";
  const healthLabel = health.error
    ? health.error.code === "NETWORK_ERROR"
      ? "后端未连接"
      : health.error.code
    : health.data
      ? `后端 ${health.data.status}`
      : "检查中…";

  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          ◧
        </span>
        <div>
          <h1>抖店智能运营 Agent</h1>
          <p className="brand-sub">采集 → 指标 → 异常 → 人工审批 → 执行 → 效果回执</p>
        </div>
      </div>

      <nav className="tabs" aria-label="主导航">
        {TABS.map((item) => (
          <a
            key={item.key}
            className={`tab ${tab === item.key ? "tab-active" : ""}`}
            href={item.hash}
            aria-current={tab === item.key ? "page" : undefined}
          >
            {item.label}
          </a>
        ))}
      </nav>

      <div className="header-tools">
        <span className={`pill pill-${healthTone}`} title={health.data ? `version ${health.data.version}` : "健康检查 GET /api/healthz"}>
          {healthLabel}
        </span>
        <div className="mode-switch" role="group" aria-label="数据源模式">
          {(["mock", "live"] as ApiMode[]).map((candidate) => (
            <button
              key={candidate}
              type="button"
              className={`btn btn-sm ${mode === candidate ? "btn-primary" : "btn-ghost"}`}
              aria-pressed={mode === candidate}
              onClick={() => {
                setApiMode(candidate);
              }}
              title={
                candidate === "mock"
                  ? "读 frontend/mock/*.json fixtures（有状态假后端，可演示 400/409/502）"
                  : "请求 /api/*，dev 由 vite proxy 转发到 FastAPI"
              }
            >
              {candidate === "mock" ? "Mock" : "实时"}
            </button>
          ))}
        </div>
      </div>
    </header>
  );
}
