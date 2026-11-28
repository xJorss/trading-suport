import { useEffect, useState } from "react";
import type { EngineState, SocketStatus } from "../types";

const DEFAULT_URL = resolveSocketUrl();

/**
 * Websocket endpoint.
 *
 * Order of precedence: an explicit VITE_WS_URL, then VITE_API_BASE with the
 * scheme swapped to ws(s), then the page's own origin. The last case is what
 * makes the container work — nginx proxies /ws to the backend, so the browser
 * never needs to know the backend's host.
 */
function resolveSocketUrl(): string {
  const explicit = import.meta.env.VITE_WS_URL;
  if (explicit) return explicit;

  const apiBase = import.meta.env.VITE_API_BASE;
  if (apiBase) {
    return `${apiBase.replace(/^http/, "ws").replace(/\/+$/, "")}/ws`;
  }

  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws`;
}

// Reconnect policy: 1s, 2s, 4s ... capped at 15s, plus jitter so a backend
// restart doesn't get hammered by every tab at the same millisecond.
const BASE_DELAY_MS = 1_000;
const MAX_DELAY_MS = 15_000;
const JITTER_MS = 400;

export interface UseEngineSocketResult {
  state: EngineState | null;
  connected: boolean;
  status: SocketStatus;
  /** Wall-clock ms of the last frame, for a "last seen" readout. */
  lastMessageAt: number | null;
}

/**
 * Opens one websocket to the Python engine and keeps it open.
 *
 * Guarantees:
 *  - exactly one socket per mount (StrictMode-safe: cleanup closes + disposes)
 *  - malformed frames are ignored, never crash the render
 *  - a closed socket retries with exponential backoff until unmount
 */
export function useEngineSocket(url: string = DEFAULT_URL): UseEngineSocketResult {
  const [state, setState] = useState<EngineState | null>(null);
  const [status, setStatus] = useState<SocketStatus>("connecting");
  const [connected, setConnected] = useState(false);
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let disposed = false;

    const scheduleReconnect = () => {
      if (disposed) return;
      const backoff = Math.min(BASE_DELAY_MS * 2 ** retry, MAX_DELAY_MS);
      const delay = backoff + Math.random() * JITTER_MS;
      retry += 1;
      timer = window.setTimeout(connect, delay);
    };

    const connect = () => {
      if (disposed) return;

      setStatus("connecting");
      ws = new WebSocket(url);

      ws.onopen = () => {
        if (disposed) {
          ws?.close();
          return;
        }
        retry = 0;
        setConnected(true);
        setStatus("open");
      };

      ws.onmessage = (event: MessageEvent<string>) => {
        try {
          const payload = JSON.parse(event.data) as EngineState;
          // The backend only emits {type:"state"}; anything else is future work.
          if (payload?.type !== "state") return;
          setState(payload);
          setLastMessageAt(Date.now());
        } catch {
          // A truncated frame during shutdown is normal — drop it.
        }
      };

      ws.onerror = () => {
        // onclose always follows onerror; let it own the reconnect.
        ws?.close();
      };

      ws.onclose = () => {
        setConnected(false);
        setStatus("closed");
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      ws?.close();
    };
  }, [url]);

  return { state, connected, status, lastMessageAt };
}
