/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "mock"（默认，跑 fixtures 假后端）| "live"（打真实 FastAPI，dev 走 vite proxy） */
  readonly VITE_API_MODE?: "mock" | "live";
  /** 默认店铺 id，与 .env.example 的 DOUDIAN_SHOP_ID 对齐 */
  readonly VITE_SHOP_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
