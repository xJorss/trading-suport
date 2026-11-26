/**
 * Wire types — mirror the JSON produced by backend/engine/manager.py:state().
 * If the backend payload changes, this file changes first.
 */

export type SignalMode = "safe" | "aggressive";
export type SignalDirection = "long" | "short";

export interface Signal {
  pair: string;
  mode: SignalMode;
  direction: SignalDirection;
  entry: number;
  stop_loss: number;
  take_profit: number;
  leverage: number;
  reason: string;
  /**
   * Notional position size in USDT, assigned by the backend RiskManager.
   * Pre-leverage: at 3x this ties up roughly a third of it in margin.
   * Optional because a signal can arrive without sizing (e.g. a manual test
   * send); the card omits the cell in that case.
   */
  size_usdt?: number;
}

/** A signal the tracker is currently watching. One per pair per timeframe. */
export interface ActivePosition {
  /** `PAIR_TIMEFRAME`, e.g. "BTCUSDT_5m". Unique while open. */
  key: string;
  pair: string;
  timeframe: string;
  mode: string;
  direction: SignalDirection;
  entry: number;
  stop_loss: number;
  take_profit: number;
  leverage: number;
  /** Notional in USDT, pre-leverage. */
  size_usdt: number;
  /** Notional / leverage — the money actually committed. */
  margin_usdt: number;
  /** Exchange open time of the candle that produced the signal, ms. */
  entry_time: number;
  reason: string;
  /** Net PnL (fees included) if the target is touched. */
  potential_pnl_usdt_tp: number;
  /** Net PnL (fees included) if the stop is touched. */
  potential_pnl_usdt_sl: number;
}

export type ExitReason = "stop_loss" | "take_profit";

/** A position that hit its stop or target. Newest last in the array. */
export interface ClosedPosition extends ActivePosition {
  exit: number;
  exit_reason: ExitReason;
  gross_pnl_usdt: number;
  fees_usdt: number;
  net_pnl_usdt: number;
  /** Net PnL as a percentage of margin — what the trader actually feels. */
  pnl_pct: number;
  /** Net PnL as a percentage of notional — comparable with the backtest. */
  pnl_pct_notional: number;
}

export interface PositionsSummary {
  opened: number;
  closed: number;
  wins: number;
  losses: number;
  net_pnl_usdt: number;
}

export interface RiskStatus {
  capital: number;
  risk_pct: Record<string, number>;
  max_stop_distance_pct: number;
  min_stop_distance_pct: number;
}

export interface PriceTick {
  price: number;
  /** Local monotonic-derived ms stamp from the backend. Not a wall clock. */
  updated_at_ms: number;
  stale: boolean;
}

/** Which transport last produced a tick: websocket, or the REST fallback. */
export type FeedSource = "websocket" | "rest" | "none";

export interface FeedStatus {
  connected: boolean;
  last_error: string | null;
  ticks: number;
  source?: FeedSource;
  timeframes?: string[];
  evaluations?: number;
}

/** Per pair/timeframe closed-candle state published by the engine. */
export interface CandleStatus {
  count: number;
  last_close: number | null;
  last_open_ms: number | null;
  last_close_ms: number | null;
  seq: number;
  last_eval_ms: number | null;
}

export interface EngineState {
  type: "state";
  server_time: number;
  uptime_s: number;
  feed: FeedStatus;
  prices: Record<string, PriceTick>;
  candles?: Record<string, Record<string, CandleStatus>>;
  /** Positions currently being watched for a stop/target touch. */
  active_positions?: ActivePosition[];
  /** Last 50 closed positions, oldest first. */
  closed_positions?: ClosedPosition[];
  positions_summary?: PositionsSummary;
  risk?: RiskStatus;
  /** Newest last, capped at 20 entries by the backend. */
  signals: Signal[];
  /** Total signals held in the backend log (may exceed signals.length). */
  signal_count: number;
}

export type SocketStatus = "connecting" | "open" | "closed";
