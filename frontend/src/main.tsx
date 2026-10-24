import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root 不存在：index.html 与 main.tsx 不一致");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
