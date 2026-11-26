import { ArrowDownRight, ArrowUpRight, ShieldAlert, ShieldCheck } from "lucide-react";
import type { Signal } from "../types";

export interface SignalCardProps {
  signal: Signal;
}

const priceFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const sizeFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

function money(value: number): string {
  return `$${priceFormatter.format(value)}`;
}

function notional(value: number): string {
  return `$${sizeFormatter.format(value)}`;
}

export function SignalCard({ signal }: SignalCardProps) {
  const isLong = signal.direction === "long";
  const isSafe = signal.mode === "safe";

  const accent = isLong
    ? "border-l-emerald-500"
    : "border-l-rose-500";
  const directionText = isLong ? "text-emerald-400" : "text-rose-400";
  const DirectionIcon = isLong ? ArrowUpRight : ArrowDownRight;

  return (
    <article
      className={`animate-fade-in-up rounded-xl border border-gray-800 border-l-4 ${accent} bg-gray-900 p-4 transition-colors hover:border-gray-700`}
    >
      {/* Header: pair + badges */}
      <header className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-base font-semibold tracking-wide text-gray-100">
          {signal.pair}
        </span>

        <div className="flex items-center gap-2">
          <span
            className={`flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${
              isSafe
                ? "bg-blue-500/15 text-blue-300 ring-1 ring-inset ring-blue-500/30"
                : "bg-purple-500/15 text-purple-300 ring-1 ring-inset ring-purple-500/30"
            }`}
          >
            {isSafe ? (
              <ShieldCheck className="h-3 w-3" />
            ) : (
              <ShieldAlert className="h-3 w-3" />
            )}
            {signal.mode}
          </span>

          <span
            className={`flex items-center gap-0.5 rounded-md bg-gray-800/80 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${directionText}`}
          >
            <DirectionIcon className="h-3 w-3" />
            {signal.direction}
          </span>
        </div>
      </header>

      {/* Data grid */}
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-5">
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-gray-500">Entrada</dt>
          <dd className="mt-0.5 text-sm font-medium tabular-nums text-gray-200">
            {money(signal.entry)}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-gray-500">Stop Loss</dt>
          <dd className="mt-0.5 text-sm font-medium tabular-nums text-rose-300/90">
            {money(signal.stop_loss)}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-gray-500">Take Profit</dt>
          <dd className="mt-0.5 text-sm font-medium tabular-nums text-emerald-300/90">
            {money(signal.take_profit)}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-gray-500">Leverage</dt>
          <dd className="mt-0.5 text-sm font-medium tabular-nums text-gray-200">
            {signal.leverage}x
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-gray-500">Tamaño</dt>
          <dd
            className="mt-0.5 text-sm font-medium tabular-nums text-gray-200"
            title="Tamaño de posición en USDT (nocional, antes de leverage)"
          >
            {signal.size_usdt != null ? notional(signal.size_usdt) : "—"}
          </dd>
        </div>
      </dl>

      {/* Reason */}
      <footer className="mt-4 border-t border-gray-800 pt-3">
        <p className="truncate text-xs text-gray-500" title={signal.reason}>
          {signal.reason}
        </p>
      </footer>
    </article>
  );
}
