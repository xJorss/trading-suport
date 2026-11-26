import { useEffect, useRef, useState } from "react";
import { Activity, AlertTriangle } from "lucide-react";

export interface PriceTickerProps {
  pair: string;
  price: number | null;
  stale: boolean;
}

const priceFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/**
 * One pair tile. The dot pulses for ~550ms whenever the price actually
 * changes, so a frozen feed is visually obvious even before `stale` flips.
 */
export function PriceTicker({ pair, price, stale }: PriceTickerProps) {
  const [pulsing, setPulsing] = useState(false);
  const previousPrice = useRef<number | null>(null);

  useEffect(() => {
    if (price === null) return;

    if (previousPrice.current !== null && previousPrice.current !== price) {
      setPulsing(true);
      const timeout = window.setTimeout(() => setPulsing(false), 550);
      previousPrice.current = price;
      return () => window.clearTimeout(timeout);
    }

    previousPrice.current = price;
  }, [price]);

  const hasPrice = price !== null;

  return (
    <div className="flex flex-1 items-center justify-between gap-4 rounded-xl border border-gray-800 bg-gray-900 px-5 py-4">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-semibold tracking-wide text-gray-400">
            {pair}
          </span>
          {stale && (
            <span className="flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-amber-400">
              <AlertTriangle className="h-3 w-3" />
              stale
            </span>
          )}
        </div>

        <div
          className={`mt-1 text-3xl font-semibold tabular-nums ${
            hasPrice ? "text-gray-50" : "text-gray-600"
          }`}
        >
          {hasPrice ? `$${priceFormatter.format(price)}` : "—"}
        </div>
      </div>

      <div className="flex flex-col items-end gap-1">
        <span
          className={`h-2.5 w-2.5 rounded-full transition-colors ${
            !hasPrice
              ? "bg-gray-700"
              : stale
                ? "bg-amber-500"
                : "bg-emerald-500"
          } ${pulsing ? "animate-tick-pulse" : ""}`}
          aria-hidden
        />
        <Activity
          className={`h-4 w-4 ${
            hasPrice && !stale ? "text-emerald-500/70" : "text-gray-700"
          }`}
        />
      </div>
    </div>
  );
}
