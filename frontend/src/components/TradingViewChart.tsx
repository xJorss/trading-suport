import { useMemo } from "react";

/**
 * TradingView Advanced Chart embed.
 *
 * Why an iframe and not the CDN script (`embed-widget-advanced-chart.js`):
 * that script locates its host through `document.currentScript.parentElement`.
 * Under React 18 StrictMode the effect runs twice, the first script is
 * orphaned when its container is cleared, and it then throws
 * "Cannot read properties of null (reading 'querySelector')" — the chart
 * either fails to mount or mounts twice. `widgetembed` is the same official
 * widget rendered in a sandboxed frame: no third-party JS runs in our page,
 * teardown is just removing the element, and the height is ours to control.
 */

const BASE_URL = "https://www.tradingview.com/widgetembed/";

export interface TradingViewChartProps {
  /** TradingView symbol, e.g. "BINANCE:BTCUSDT". */
  symbol: string;
  height?: number;
  /** Chart interval: "1", "5", "15", "60", "240", "D"… */
  interval?: string;
}

export function TradingViewChart({ symbol, height = 400, interval = "15" }: TradingViewChartProps) {
  const src = useMemo(() => {
    const params = new URLSearchParams({
      symbol,
      interval,
      theme: "dark",
      style: "1", // candles
      locale: "es",
      timezone: "Etc/UTC",
      withdateranges: "1",
      hide_side_toolbar: "1",
      allow_symbol_change: "0",
      save_image: "0",
      details: "0",
      calendar: "0",
      hide_volume: "0",
      backgroundColor: "rgba(3,7,18,1)",
      gridColor: "rgba(31,41,55,0.5)",
    });
    return `${BASE_URL}?${params.toString()}`;
  }, [symbol, interval]);

  return (
    <div className="overflow-hidden rounded-xl border border-gray-800 bg-gray-900">
      <div className="flex items-center justify-between border-b border-gray-800 px-4 py-2">
        <span className="text-xs font-semibold tracking-wide text-gray-400">{symbol}</span>
        <span className="font-mono text-[10px] uppercase tracking-wider text-gray-600">
          {interval === "60" ? "1h" : `${interval}m`} · TradingView
        </span>
      </div>
      <iframe
        key={symbol}
        title={`Gráfico ${symbol}`}
        src={src}
        height={height}
        width="100%"
        style={{ border: "none", display: "block", height, width: "100%" }}
        frameBorder={0}
        scrolling="no"
        referrerPolicy="no-referrer-when-downgrade"
        loading="lazy"
      />
    </div>
  );
}
