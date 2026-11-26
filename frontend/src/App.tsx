import { useCallback, useMemo, useState } from "react";
import { formatDuration, intervalToDuration } from "date-fns";
import {
  Activity,
  History,
  RadioTower,
  RotateCcw,
  Signal as SignalIcon,
} from "lucide-react";

import { PriceTicker } from "./components/PriceTicker";
import { EngineStatus } from "./components/EngineStatus";
import { TradingViewChart } from "./components/TradingViewChart";
import { SignalCard } from "./components/SignalCard";
import { EquityChart } from "./components/EquityChart";
import { useEngineSocket } from "./hooks/useEngineSocket";
import type { ClosedPosition } from "./types";

// The backend is fixed at :8000 for local dev (see README / uvicorn command).
// The websocket hook uses the same host, so the two never disagree.
const API_BASE = "http://127.0.0.1:8000";

function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0s";
  const duration = intervalToDuration({ start: 0, end: Math.round(seconds * 1000) });
  const label = formatDuration(duration, {
    format: ["hours", "minutes", "seconds"],
    delimiter: " ",
  });
  return label || "0s";
}

const priceFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const usdtFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function money(value: number): string {
  return `$${priceFormatter.format(value)}`;
}

function signedUsdt(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${usdtFormatter.format(value)}`;
}

function signedPct(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function pnlClass(value: number): string {
  if (value > 0) return "text-emerald-400";
  if (value < 0) return "text-rose-400";
  return "text-gray-400";
}

const EXIT_LABEL: Record<ClosedPosition["exit_reason"], string> = {
  stop_loss: "SL",
  take_profit: "TP",
};

export default function App() {
  const { state, connected } = useEngineSocket();
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);

  // Backend appends newest last, so reverse for a newest-first grid.
  const signals = useMemo(() => {
    if (!state?.signals?.length) return [];
    return [...state.signals].reverse();
  }, [state?.signals]);

  // Same convention for the trade history: newest close at the top.
  const closedPositions = useMemo(() => {
    if (!state?.closed_positions?.length) return [];
    return [...state.closed_positions].reverse();
  }, [state?.closed_positions]);

  // The curve needs chronological order, which is what the backend sends.
  const closedChronological = state?.closed_positions ?? [];

  const activePositions = state?.active_positions ?? [];
  const summary = state?.positions_summary;
  const capital = state?.risk?.capital;

  const pairs = useMemo(() => {
    if (state?.prices) return Object.keys(state.prices).sort();
    return ["BTCUSDT", "ETHUSDT"];
  }, [state?.prices]);

  const totalSignals = state?.signal_count ?? 0;
  const feedConnected = state?.feed.connected ?? false;
  const feedError = state?.feed.last_error ?? null;

  /**
   * Reset the paper account.
   *
   * The POST wipes the backend book and writes the snapshot; the websocket
   * then pushes the new state on its next tick (<= 1 s), which is what makes
   * the panel update. No manual refetch needed, and no risk of the UI showing
   * a stale account while the socket is the source of truth.
   */
  const handleReset = useCallback(async () => {
    const confirmed = window.confirm(
      "¿Reiniciar la cuenta de paper trading?\n\n" +
        "Se borrarán las posiciones abiertas, el historial de operaciones y las señales. " +
        "El capital vuelve a 1000 USDT. Esto no se puede deshacer.",
    );
    if (!confirmed) return;

    setResetting(true);
    setResetError(null);
    try {
      const response = await fetch(`${API_BASE}/api/reset`, { method: "POST" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
    } catch (error) {
      setResetError(
        error instanceof Error
          ? `No se pudo reiniciar la cuenta: ${error.message}`
          : "No se pudo reiniciar la cuenta.",
      );
    } finally {
      setResetting(false);
    }
  }, []);

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* Fixed header */}
      <header className="sticky top-0 z-20 border-b border-gray-800 bg-gray-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-500/10 ring-1 ring-inset ring-emerald-500/30">
              <Activity className="h-5 w-5 text-emerald-400" />
            </div>
            <div>
              <h1 className="text-base font-semibold leading-tight tracking-tight">
                Trading Copilot
              </h1>
              <p className="text-[11px] text-gray-500">
                Binance Futures · signal engine
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3 text-xs">
            {/* Socket status */}
            <div className="flex items-center gap-2">
              <span
                className={`h-2 w-2 rounded-full ${
                  connected ? "bg-emerald-500" : "bg-rose-500"
                }`}
                aria-hidden
              />
              <span className={connected ? "text-emerald-400" : "text-rose-400"}>
                {connected ? "WS conectado" : "WS desconectado"}
              </span>
            </div>

            {/* Feed status */}
            <div className="hidden items-center gap-2 sm:flex">
              <RadioTower
                className={`h-3.5 w-3.5 ${
                  feedConnected ? "text-emerald-400" : "text-gray-600"
                }`}
              />
              <span className={feedConnected ? "text-gray-300" : "text-gray-500"}>
                {feedConnected
                  ? state?.feed.source === "rest"
                    ? "Binance feed · REST"
                    : "Binance feed"
                  : "sin feed"}
              </span>
            </div>

            {/* Uptime */}
            <div className="rounded-md bg-gray-900 px-2 py-1 font-mono text-[11px] text-gray-400 ring-1 ring-inset ring-gray-800">
              up {formatUptime(state?.uptime_s ?? 0)}
            </div>

            {/* Capital + reset */}
            <div className="flex items-center gap-1.5 rounded-md bg-gray-900 py-0.5 pl-2 pr-0.5 ring-1 ring-inset ring-gray-800">
              <span className="font-mono text-[11px] tabular-nums text-gray-200">
                {capital != null ? money(capital) : "—"}
              </span>
              <button
                type="button"
                onClick={handleReset}
                disabled={resetting}
                title="Reiniciar la cuenta de paper trading (capital a 1000 USDT)"
                aria-label="Reiniciar cuenta"
                className="flex items-center gap-1 rounded px-1.5 py-1 text-gray-400 transition-colors hover:bg-rose-500/10 hover:text-rose-300 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <RotateCcw className={`h-3.5 w-3.5 ${resetting ? "animate-spin" : ""}`} />
                <span className="hidden sm:inline">Reset</span>
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-4 py-6 sm:px-6">
        {/* Prices */}
        <section aria-label="Precios en vivo">
          <div className="flex flex-col gap-3 sm:flex-row">
            {pairs.map((pair) => (
              <PriceTicker
                key={pair}
                pair={pair}
                price={state?.prices?.[pair]?.price ?? null}
                stale={state?.prices?.[pair]?.stale ?? true}
              />
            ))}
          </div>
        </section>

        {/* Feed error strip — only when the socket is up but Binance is not */}
        {/* Charts — one per configured pair, symbol derived from the feed */}
        <section aria-label="Gráficos" className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          {pairs.map((pair) => (
            <TradingViewChart key={pair} symbol={`BINANCE:${pair}`} height={400} interval="15" />
          ))}
        </section>

        {/* Engine heartbeat: evaluations, last close, countdown to next one */}
        <section aria-label="Estado del motor">
          <EngineStatus
            candles={state?.candles}
            evaluations={state?.feed.evaluations ?? 0}
            connected={connected}
          />
        </section>

        {resetError && (
          <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-4 py-2.5 text-xs text-rose-300">
            {resetError}
          </div>
        )}

        {/* Equity curve — cumulative capital over the closed trades */}
        <section aria-label="Curva de capital">
          <EquityChart closedPositions={closedChronological} startingCapital={1000} height={240} />
        </section>

        {/* Trade history — every signal the tracker resolved */}
        <section aria-label="Historial de operaciones">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-gray-400">
              <History className="h-4 w-4 text-gray-500" />
              Historial de Operaciones
            </h2>

            <div className="flex flex-wrap items-center gap-3 text-xs text-gray-500">
              <span>
                <span className="text-gray-300">{activePositions.length}</span> abiertas
              </span>
              <span>
                <span className="text-gray-300">{summary?.closed ?? 0}</span> cerradas
              </span>
              {summary && summary.closed > 0 && (
                <span>
                  <span className="text-emerald-400">{summary.wins}G</span>
                  <span className="text-gray-600"> / </span>
                  <span className="text-rose-400">{summary.losses}P</span>
                </span>
              )}
              {summary && summary.closed > 0 && (
                <span className={`font-medium ${pnlClass(summary.net_pnl_usdt)}`}>
                  {signedUsdt(summary.net_pnl_usdt)} USDT
                </span>
              )}
            </div>
          </div>

          {closedPositions.length === 0 ? (
            <div className="rounded-xl border border-dashed border-gray-800 bg-gray-900/40 px-6 py-10 text-center">
              <p className="text-sm text-gray-400">
                Sin operaciones cerradas todavía.
              </p>
              <p className="mt-1 text-xs text-gray-600">
                {activePositions.length > 0
                  ? `${activePositions.length} posición(es) abierta(s) esperando stop o target.`
                  : "Cada señal se sigue en vivo hasta que toque el Stop Loss o el Take Profit."}
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto rounded-xl border border-gray-800 bg-gray-900">
              <table className="w-full min-w-[720px] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-[10px] uppercase tracking-wider text-gray-500">
                    <th className="px-4 py-2.5 text-left font-semibold">Par</th>
                    <th className="px-3 py-2.5 text-left font-semibold">Modo</th>
                    <th className="px-3 py-2.5 text-left font-semibold">Dirección</th>
                    <th className="px-3 py-2.5 text-right font-semibold">Entrada</th>
                    <th className="px-3 py-2.5 text-right font-semibold">Salida</th>
                    <th className="px-3 py-2.5 text-right font-semibold">PnL (USDT)</th>
                    <th className="px-4 py-2.5 text-right font-semibold">PnL (%)</th>
                  </tr>
                </thead>
                <tbody>
                  {closedPositions.map((position, index) => {
                    const isLong = position.direction === "long";
                    const isSafe = position.mode === "safe";
                    return (
                      <tr
                        key={`${position.key}-${position.entry_time}-${index}`}
                        className="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/30"
                      >
                        <td className="px-4 py-2.5 font-medium tracking-wide text-gray-100">
                          {position.pair}
                        </td>
                        <td className="px-3 py-2.5">
                          <span
                            className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider ${
                              isSafe
                                ? "bg-blue-500/15 text-blue-300"
                                : "bg-purple-500/15 text-purple-300"
                            }`}
                          >
                            {position.mode}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <span
                            className={`text-xs font-semibold uppercase ${
                              isLong ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {position.direction}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 text-right tabular-nums text-gray-300">
                          {money(position.entry)}
                        </td>
                        <td className="px-3 py-2.5 text-right tabular-nums text-gray-300">
                          <span className="mr-1.5 text-[10px] font-bold uppercase tracking-wider text-gray-500">
                            {EXIT_LABEL[position.exit_reason] ?? position.exit_reason}
                          </span>
                          {money(position.exit)}
                        </td>
                        <td
                          className={`px-3 py-2.5 text-right font-medium tabular-nums ${pnlClass(
                            position.net_pnl_usdt,
                          )}`}
                        >
                          {signedUsdt(position.net_pnl_usdt)}
                        </td>
                        <td
                          className={`px-4 py-2.5 text-right font-medium tabular-nums ${pnlClass(
                            position.pnl_pct,
                          )}`}
                        >
                          {signedPct(position.pnl_pct)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <p className="mt-2 text-[11px] text-gray-600">
            PnL (%) sobre margen comprometido (nocional / leverage), comisiones 0.04% × 2 incluidas.
          </p>
        </section>

        {connected && feedError && !feedConnected && (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-2.5 text-xs text-amber-300">
            <span className="font-semibold">Feed caído:</span> {feedError}
          </div>
        )}

        {/* Signals */}
        <section aria-label="Señales">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-gray-400">
              <SignalIcon className="h-4 w-4 text-gray-500" />
              Señales
            </h2>
            <span className="text-xs text-gray-500">
              {signals.length} visibles · {totalSignals} en total
            </span>
          </div>

          {signals.length === 0 ? (
            <div className="rounded-xl border border-dashed border-gray-800 bg-gray-900/40 px-6 py-14 text-center">
              <p className="text-sm text-gray-400">
                {connected
                  ? "Sin señales todavía — el motor está evaluando."
                  : "Esperando conexión con el motor…"}
              </p>
              <p className="mt-1 text-xs text-gray-600">
                {connected
                  ? "Las estrategias solo corren al cierre de vela: Safe en 1h, Aggressive en 5m."
                  : "Arranca el backend con uvicorn main:app --reload dentro de backend/"}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {signals.map((signal, index) => (
                <SignalCard
                  key={`${signal.pair}-${signal.mode}-${signal.entry}-${index}`}
                  signal={signal}
                />
              ))}
            </div>
          )}
        </section>
      </main>

      <footer className="mx-auto max-w-6xl px-4 pb-8 pt-2 text-center text-[11px] text-gray-600 sm:px-6">
        trading-copilot · datos públicos de Binance Futures · no es consejo financiero
      </footer>
    </div>
  );
}
