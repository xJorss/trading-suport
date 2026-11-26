import { useEffect, useState } from "react";
import { format } from "date-fns";
import { Clock, Gauge } from "lucide-react";

import type { CandleStatus } from "../types";

const TIMEFRAME_MS: Record<string, number> = {
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
  "4h": 14_400_000,
};

export interface EngineStatusProps {
  candles: Record<string, Record<string, CandleStatus>> | undefined;
  evaluations: number;
  connected: boolean;
}

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

function countdown(target: number | null, now: number): string {
  if (target === null) return "--:--";
  const remaining = Math.max(0, target - now);
  const minutes = Math.floor(remaining / 60_000);
  const seconds = Math.floor((remaining % 60_000) / 1000);
  return `${pad(minutes)}:${pad(seconds)}`;
}

/**
 * "Is the engine alive?" strip.
 *
 * Strategies only run when a candle closes, so between closes the panel can
 * look frozen even though everything works. This shows the last close, the
 * buffer depth and a live countdown to the next evaluation for each timeframe.
 */
export function EngineStatus({ candles, evaluations, connected }: EngineStatusProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const timeframes = Array.from(
    new Set(Object.values(candles ?? {}).flatMap((perTf) => Object.keys(perTf))),
  ).sort((a, b) => (TIMEFRAME_MS[a] ?? 0) - (TIMEFRAME_MS[b] ?? 0));

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-gray-800 bg-gray-900/50 px-4 py-2.5 text-xs">
      <span className="flex items-center gap-1.5 text-gray-400">
        <Gauge className="h-3.5 w-3.5 text-gray-500" />
        evaluaciones
        <span className="font-mono text-gray-200">{evaluations}</span>
      </span>

      {timeframes.map((timeframe) => {
        const rows = Object.entries(candles ?? {})
          .map(([pair, perTf]) => [pair, perTf[timeframe]] as const)
          .filter(([, status]) => Boolean(status));

        const lastCloseMs = rows.reduce<number | null>((latest, [, status]) => {
          if (status?.last_close_ms == null) return latest;
          return latest === null ? status.last_close_ms : Math.max(latest, status.last_close_ms);
        }, null);

        const interval = TIMEFRAME_MS[timeframe];
        const nextClose = lastCloseMs !== null && interval ? lastCloseMs + interval : null;
        const depth = rows.map(([pair, status]) => `${pair.slice(0, 3)} ${status.count}`).join(" · ");

        return (
          <span key={timeframe} className="flex items-center gap-1.5 text-gray-400">
            <Clock className="h-3.5 w-3.5 text-gray-500" />
            <span className="font-mono text-gray-200">{timeframe}</span>
            <span className="text-gray-500">última</span>
            <span className="font-mono text-gray-300">
              {lastCloseMs ? format(new Date(lastCloseMs), "HH:mm") : "--:--"}
            </span>
            <span className="text-gray-500">siguiente en</span>
            <span
              className={`font-mono ${
                connected ? "text-emerald-400" : "text-gray-600"
              }`}
            >
              {countdown(nextClose, now)}
            </span>
            <span className="hidden text-gray-600 sm:inline">({depth})</span>
          </span>
        );
      })}
    </div>
  );
}
