/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Absolute backend origin, e.g. "http://127.0.0.1:8000".
   * Unset means same-origin: Vite's dev proxy in development, nginx in Docker.
   */
  readonly VITE_API_BASE?: string;
  /** Full websocket URL override; derived from VITE_API_BASE or the page when unset. */
  readonly VITE_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
