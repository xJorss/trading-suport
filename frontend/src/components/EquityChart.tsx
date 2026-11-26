import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ClosedPosition } from "../types";

export interface EquityChartProps {
  /** Closed trades, oldest first — the same order the backend publishes. */
  closedPositions: ClosedPosition[];
  /** Equity before the first trade. The account always starts here. */
  startingCapital?: number;
  height?: number;
}

interface EquityPoint {
  /** Trade number, 0 = starting point. Used as the X category. */
  trade: number;
  equity: number;
  pair: string;
  mode: string;
  direction: string;
  exitReason: string;
  net: number;
  pnlPct: number;
}

const DEFAULT_START = 1000;

const usdt = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function signed(value: number): string {
  return `${value > 0 ? "+" : ""}${usdt.format(value)}`;
}

interface TooltipProps {
  active?: boolean;
  payload?: Array<{ payload: EquityPoint }>;
}

function EquityTooltip({ active, payload }: TooltipProps) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  if (point.trade === 0) {
    return (
      <div className="rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-xs shadow-lg">
        <p className="font-semibold text-gray-200">Capital inicial</p>
        <p className="mt-0.5 tabular-nums text-gray-400">${usdt.format(point.equity)}</p>
      </div>
    );
  }

  const netClass = point.net > 0 ? "text-emerald-400" : point.net < 0 ? "text-rose-400" : "text-gray-400";

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-xs shadow-lg">
      <p className="font-semibold text-gray-200">
        #{point.trade} · {point.pair}{" "}
        <span className={point.direction === "long" ? "text-emerald-400" : "text-rose-400"}>
          {point.direction.toUpperCase()}
        </span>
      </p>
      <p className="mt-1 text-[11px] uppercase tracking-wider text-gray-500">
        {point.mode} · {point.exitReason === "take_profit" ? "TP" : "SL"}
      </p>
      <p className={`mt-1 tabular-nums ${netClass}`}>
        {signed(point.net)} USDT ({signed(point.pnlPct)}%)
      </p>
      <p className="mt-1 border-t border-gray-800 pt-1 tabular-nums text-gray-300">
        Capital: ${usdt.format(point.equity)}
      </p>
    </div>
  );
}

/**
 * Cumulative equity curve over the closed-trade history.
 *
 * Starts at `startingCapital`, adds each trade's net PnL (fees already
 * deducted by the backend) and plots the running balance. Green when the
 * account is above water, red when it is not — the colour is derived from the
 * final point, so a chart that ends down never renders in green.
 */
export function EquityChart({
  closedPositions,
  startingCapital = DEFAULT_START,
  height = 240,
}: EquityChartProps) {
  const { data, finalEquity, minEquity, maxEquity } = useMemo(() => {
    let running = startingCapital;
    const points: EquityPoint[] = [
      {
        trade: 0,
        equity: running,
        pair: "—",
        mode: "",
        direction: "",
        exitReason: "",
        net: 0,
        pnlPct: 0,
      },
    ];

    for (let index = 0; index < closedPositions.length; index += 1) {
      const position = closedPositions[index];
      running += position.net_pnl_usdt;
      points.push({
        trade: index + 1,
        equity: Math.round(running * 100) / 100,
        pair: position.pair,
        mode: position.mode,
        direction: position.direction,
        exitReason: position.exit_reason,
        net: position.net_pnl_usdt,
        pnlPct: position.pnl_pct,
      });
    }

    const values = points.map((point) => point.equity);
    return {
      data: points,
      finalEquity: running,
      minEquity: Math.min(...values),
      maxEquity: Math.max(...values),
    };
  }, [closedPositions, startingCapital]);

  const isUp = finalEquity >= startingCapital;
  const stroke = isUp ? "#34d399" : "#fb7185";
  const delta = finalEquity - startingCapital;
  const deltaPct = startingCapital > 0 ? (delta / startingCapital) * 100 : 0;

  // Pad the Y domain so the reference line never sits on the chart edge.
  const span = Math.max(maxEquity - minEquity, startingCapital * 0.01);
  const domain: [number, number] = [
    Math.floor((minEquity - span * 0.15) * 100) / 100,
    Math.ceil((maxEquity + span * 0.15) * 100) / 100,
  ];

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">
          Curva de capital
        </h3>
        <div className="flex items-baseline gap-2 text-xs">
          <span className="font-mono tabular-nums text-gray-200">
            ${usdt.format(finalEquity)}
          </span>
          <span
            className={`font-medium tabular-nums ${isUp ? "text-emerald-400" : "text-rose-400"}`}
          >
            {signed(delta)} ({signed(deltaPct)}%)
          </span>
        </div>
      </div>

      {closedPositions.length === 0 ? (
        <div
          className="flex flex-col items-center justify-center rounded-lg border border-dashed border-gray-800 bg-gray-950/40"
          style={{ height }}
        >
          <p className="text-sm text-gray-400">Sin operaciones cerradas todavía.</p>
          <p className="mt-1 text-xs text-gray-600">
            La curva empieza en ${usdt.format(startingCapital)} y se mueve con cada cierre.
          </p>
        </div>
      ) : (
        <div style={{ height }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
                </linearGradient>
              </defs>

              <CartesianGrid stroke="#1f2937" strokeDasharray="3 3" vertical={false} />

              <XAxis
                dataKey="trade"
                tick={{ fill: "#6b7280", fontSize: 10 }}
                tickLine={false}
                axisLine={{ stroke: "#1f2937" }}
                tickFormatter={(value: number) => (value === 0 ? "inicio" : `#${value}`)}
                minTickGap={24}
              />

              <YAxis
                domain={domain}
                tick={{ fill: "#6b7280", fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                width={64}
                tickFormatter={(value: number) => `$${usdt.format(value)}`}
              />

              <ReferenceLine
                y={startingCapital}
                stroke="#4b5563"
                strokeDasharray="4 4"
                label={{
                  value: `base $${usdt.format(startingCapital)}`,
                  position: "insideTopLeft",
                  fill: "#6b7280",
                  fontSize: 10,
                }}
              />

              <Tooltip
                content={<EquityTooltip />}
                cursor={{ stroke: "#374151", strokeWidth: 1 }}
              />

              <Area
                type="monotone"
                dataKey="equity"
                stroke={stroke}
                strokeWidth={2}
                fill="url(#equityFill)"
                dot={false}
                activeDot={{ r: 4, stroke, strokeWidth: 2, fill: "#111827" }}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
