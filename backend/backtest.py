#!/usr/bin/env python3
"""
Backtesting harness for trading-copilot.

Replays the live strategies over historical Binance USDⓈ-M Futures klines and
prints a per-pair/per-timeframe report. No API key needed: klines are public.

    cd backend
    python backtest.py                        # 1000 candles, fees on
    python backtest.py --candles=3000         # longer window
    python backtest.py --fee=0                # gross, pre-fee view
    python backtest.py --no-trail             # disable the break-even stop
    python backtest.py --candles=500 --sleep=0.5

Trade lifecycle
---------------
1. ENTRY
   The strategy is asked to evaluate on every *closed* candle, with the full
   history up to and including that candle (identical payload shape to the live
   manager). When it returns a signal, the trade opens at `signal["entry"]`,
   which is the close of that candle. No slippage: the report measures strategy
   logic, not execution quality. Fees ARE charged (see 6).

2. OPEN
   From the next candle onward the trade is resolved before any new evaluation
   happens. While a trade is open, no new signal is accepted — one position per
   pair per timeframe, like the live engine.

3. INTRABAR ORDER
   A candle is processed as open -> high/low -> close, in this exact order:
       a. gap through the stop      (fills at the open)
       b. gap through the target    (fills at the open)
       c. break-even arming         (stop moves to entry)
       d. stop touched              (fills at the stop, which may now be entry)
       e. target touched            (fills at the level)
   The stop is checked before the target so a candle spanning both levels
   resolves as a loss: intrabar order is unknowable from OHLC and assuming the
   target would flatter every strategy. A candle that runs to the trigger and
   closes back at entry therefore exits at break-even, not at the original stop.

4. FIRST TOUCH WINS
   For a long:  low  <= stop_loss  -> stopped out
                high >= take_profit -> target hit
   For a short: high >= stop_loss  -> stopped out
                 low <= take_profit -> target hit
   If a single candle touches both, the STOP is assumed to fill first. That is
   the pessimistic reading: intrabar order is unknowable from OHLC alone, and
   assuming the target would flatter every strategy.

5. BREAK-EVEN TRAILING (aggressive mode)
   Once price moves TRAIL_TRIGGER (1.5%) in favour of the trade, the stop is
   moved to the entry price. From then on the worst case is a fee-only loss.
   Applied to `mode == "aggressive"` only; disable with `--no-trail`.

6. FEES
   Binance USDⓈ-M taker fee, charged on both sides, on the leveraged notional:

       fee_pct = fee * 2 * leverage * 100
       net_pct = gross_pct - fee_pct

   At fee=0.0004 (0.04%) and 10x that is 0.8% of margin per round trip, which
   is exactly the kind of drag that decides whether a 20%-win-rate strategy is
   profitable. The report shows gross, fees and net separately so the drag is
   never hidden.

7. AFTER EXIT
   Simulation resumes on the candle after the exit. The strategy's own cooldown
   (candle-indexed for Aggressive) still applies, so it cannot re-enter on the
   same bar it closed.

A trade still open when the data runs out is marked `open` and marked to market
at the last close — it counts in the totals, flagged separately in the report.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Windows consoles default to cp1252, which cannot encode arrows or the
# infinity sign used in the report. Pin the stream to UTF-8 so the run never
# dies on an encoding error mid-report.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):  # redirected / exotic stream
    pass

# Allow `python backtest.py` from backend/ without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.strategies.aggressive import AggressiveStrategy  # noqa: E402
from engine.strategies.base import BaseStrategy  # noqa: E402
from engine.strategies.safe import SafeStrategy  # noqa: E402

API_URL = "https://fapi.binance.com/fapi/v1/klines"
MAX_LIMIT = 1500          # Binance hard cap per klines request
HTTP_TIMEOUT = 20.0
DEFAULT_CANDLES = 1000
DEFAULT_PAIRS = ("BTCUSDT", "ETHUSDT")
DEFAULT_SLEEP = 1.0       # seconds between requests, to stay clear of rate limits
DEFAULT_FEE = 0.0004      # 0.04% per side, Binance USDⓈ-M taker
DEFAULT_TRAIL_TRIGGER = 0.015  # 1.5% favourable move -> stop to break-even

# (pair, timeframe) -> strategy factory. Mirrors EngineManager's mapping.
SUITE: dict[str, type[BaseStrategy]] = {
    "5m": AggressiveStrategy,
    "1h": SafeStrategy,
}


# --------------------------------------------------------------------- models


@dataclass(slots=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "t": self.open_time,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
        }


@dataclass(slots=True)
class Trade:
    pair: str
    timeframe: str
    mode: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    leverage: int
    reason: str
    entry_index: int
    entry_time: int
    exit_index: int = -1
    exit_price: float = 0.0
    exit_reason: str = "open"
    gross_pnl_pct: float = 0.0
    fee_pct: float = 0.0
    pnl_pct: float = 0.0      # net of fees
    be_armed: bool = False    # stop was trailed to break-even

    @property
    def is_win(self) -> bool:
        return self.pnl_pct > 0


@dataclass(slots=True)
class Stats:
    pair: str
    timeframe: str
    trades: list[Trade]

    # ------------------------------------------------------------- derived

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def longs(self) -> int:
        return sum(1 for t in self.trades if t.direction == "long")

    @property
    def shorts(self) -> int:
        return sum(1 for t in self.trades if t.direction == "short")

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.is_win)

    @property
    def losses(self) -> int:
        return self.total - self.wins

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total * 100.0) if self.total else 0.0

    @property
    def gross_profit(self) -> float:
        return sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)

    @property
    def gross_loss(self) -> float:
        return abs(sum(t.pnl_pct for t in self.trades if t.pnl_pct < 0))

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    @property
    def total_pnl(self) -> float:
        """Net PnL, after fees."""
        return sum(t.pnl_pct for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        """PnL before fees."""
        return sum(t.gross_pnl_pct for t in self.trades)

    @property
    def fees_paid(self) -> float:
        return sum(t.fee_pct for t in self.trades)

    @property
    def break_even_exits(self) -> int:
        return sum(1 for t in self.trades if t.be_armed)

    @property
    def equity_curve(self) -> list[float]:
        """Cumulative net PnL in percentage points, in trade order."""
        curve: list[float] = []
        running = 0.0
        for trade in self.trades:
            running += trade.pnl_pct
            curve.append(running)
        return curve

    @property
    def max_drawdown(self) -> float:
        """Deepest peak-to-trough fall of the cumulative net PnL curve."""
        peak = 0.0
        worst = 0.0
        for value in self.equity_curve:
            peak = max(peak, value)
            worst = max(worst, peak - value)
        return worst

    @property
    def longest_losing_streak(self) -> int:
        streak = worst = 0
        for trade in self.trades:
            if trade.pnl_pct < 0:
                streak += 1
                worst = max(worst, streak)
            else:
                streak = 0
        return worst

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl_pct > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl_pct < 0]
        return sum(losses) / len(losses) if losses else 0.0


# ------------------------------------------------------------------- fetching


def _request_klines(
    symbol: str, interval: str, limit: int, end_time: int | None
) -> list[list[Any]]:
    params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["endTime"] = end_time
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={"User-Agent": "trading-copilot-backtest/1.1"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        payload = json.loads(response.read())
    return payload if isinstance(payload, list) else []


def fetch_klines(
    symbol: str, interval: str, count: int, sleep_s: float = DEFAULT_SLEEP
) -> list[Candle]:
    """
    Download `count` closed candles, newest last.

    Binance caps a single request at MAX_LIMIT rows, so long windows are paged
    backwards with `endTime`. One second of sleep between requests keeps the
    whole run at a few weight units per second instead of tripping the limit.
    """
    rows: list[list[Any]] = []
    end_time: int | None = None
    requests = 0

    while len(rows) < count:
        batch = min(MAX_LIMIT, count - len(rows) + 1)  # +1: the forming candle
        page = _request_klines(symbol, interval, batch, end_time)
        requests += 1
        if not page:
            break

        rows = page + rows
        end_time = int(page[0][0]) - 1
        if len(page) < batch:
            break  # history exhausted
        if len(rows) < count:
            time.sleep(sleep_s)

    # Deduplicate by open_time (pagination can overlap) and sort ascending.
    seen: dict[int, list[Any]] = {int(row[0]): row for row in rows}
    ordered = [seen[key] for key in sorted(seen)]

    now_ms = int(time.time() * 1000)
    candles = [
        Candle(
            open_time=int(row[0]),
            close_time=int(row[6]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in ordered
        if int(row[6]) < now_ms  # closed candles only
    ]

    print(
        f"  {symbol} {interval}: {len(candles)} velas cerradas "
        f"({requests} peticiones)"
    )
    return candles[-count:]


# ----------------------------------------------------------------- simulation


def simulate(
    candles: list[Candle],
    strategy: BaseStrategy,
    pair: str,
    timeframe: str,
    *,
    fee: float = DEFAULT_FEE,
    trail_trigger: float | None = None,
) -> list[Trade]:
    """
    Walk the candles once. Each closed candle is offered to the strategy unless
    a position is already open; an open position is resolved first.

    `fee` is the per-side taker rate (0.0004 = 0.04%). `trail_trigger` is the
    favourable move, as a fraction of entry, that arms the break-even stop;
    None disables trailing entirely.
    """
    trades: list[Trade] = []
    open_trade: Trade | None = None
    index = 0

    while index < len(candles):
        candle = candles[index]

        # --- an open position resolves before anything else ----------------
        if open_trade is not None:
            exit_price, exit_reason = _resolve_candle(open_trade, candle, trail_trigger)
            if exit_price is not None:
                open_trade.exit_index = index
                open_trade.exit_price = exit_price
                open_trade.exit_reason = exit_reason
                open_trade.gross_pnl_pct = _gross_pct(open_trade, exit_price)
                open_trade.fee_pct = _fee_pct(open_trade, fee)
                open_trade.pnl_pct = open_trade.gross_pnl_pct - open_trade.fee_pct
                trades.append(open_trade)
                open_trade = None
                index += 1  # a new entry may start on the next candle
                continue
            index += 1
            continue

        # --- no position: ask the strategy --------------------------------
        window = candles[: index + 1]
        payload = {
            "pair": pair,
            "timeframe": timeframe,
            "price": candle.close,
            "history": [c.as_dict() for c in window],
            "stale": False,
        }

        signal = strategy.evaluate(payload)
        if signal is not None:
            open_trade = Trade(
                pair=pair,
                timeframe=timeframe,
                mode=signal["mode"],
                direction=signal["direction"],
                entry=float(signal["entry"]),
                stop_loss=float(signal["stop_loss"]),
                take_profit=float(signal["take_profit"]),
                leverage=int(signal["leverage"]),
                reason=signal["reason"],
                entry_index=index,
                entry_time=candle.open_time,
            )

        index += 1

    # --- mark an unfinished position to market ------------------------------
    if open_trade is not None and candles:
        last = candles[-1]
        open_trade.exit_index = len(candles) - 1
        open_trade.exit_price = last.close
        open_trade.exit_reason = "open"
        open_trade.gross_pnl_pct = _gross_pct(open_trade, last.close)
        open_trade.fee_pct = _fee_pct(open_trade, fee)
        open_trade.pnl_pct = open_trade.gross_pnl_pct - open_trade.fee_pct
        trades.append(open_trade)

    return trades


def _resolve_candle(
    trade: Trade, candle: Candle, trail_trigger: float | None = None
) -> tuple[float | None, str]:
    """
    Resolve a position against one candle. Returns (exit_price, reason) or
    (None, "") when the candle touches neither level.

    Order mirrors the bar: gaps at the open first, then break-even arming, then
    the stop, then the target. The stop is checked against the level *in force
    after* arming, so a candle that runs to the trigger and reverses exits at
    break-even.

    The stop is checked before the target on purpose: when one candle spans
    both levels, intrabar order is unknowable from OHLC, and assuming the
    target would flatter every strategy. Pessimism is the only honest default.
    """
    is_long = trade.direction == "long"
    stop_label = "breakeven" if trade.be_armed else "stop"

    # --- 1/2. gap at the open, against the stop currently in force ---------
    if is_long:
        if candle.open <= trade.stop_loss:
            return candle.open, f"{stop_label}_gap"
        if candle.open >= trade.take_profit:
            return candle.open, "target_gap"
    else:
        if candle.open >= trade.stop_loss:
            return candle.open, f"{stop_label}_gap"
        if candle.open <= trade.take_profit:
            return candle.open, "target_gap"

    # --- 3. arm break-even ------------------------------------------------
    if trail_trigger and not trade.be_armed:
        if is_long and candle.high >= trade.entry * (1.0 + trail_trigger):
            trade.stop_loss = trade.entry
            trade.be_armed = True
        elif not is_long and candle.low <= trade.entry * (1.0 - trail_trigger):
            trade.stop_loss = trade.entry
            trade.be_armed = True

    # --- 4. stop touched (wins any same-candle tie with the target) --------
    stop_label = "breakeven" if trade.be_armed else "stop"
    if is_long:
        if candle.low <= trade.stop_loss:
            return trade.stop_loss, stop_label
    else:
        if candle.high >= trade.stop_loss:
            return trade.stop_loss, stop_label

    # --- 5. target touched -------------------------------------------------
    if is_long:
        if candle.high >= trade.take_profit:
            return trade.take_profit, "target"
    else:
        if candle.low <= trade.take_profit:
            return trade.take_profit, "target"

    return None, ""


def _gross_pct(trade: Trade, exit_price: float) -> float:
    """Return on margin before fees, in percent, including leverage."""
    move = (exit_price - trade.entry) / trade.entry
    if trade.direction == "short":
        move = -move
    return move * 100.0 * trade.leverage


def _fee_pct(trade: Trade, fee: float) -> float:
    """Round-trip fee cost on margin, in percent: both sides, leveraged."""
    return fee * 2.0 * trade.leverage * 100.0


def _pnl_pct(trade: Trade, exit_price: float, fee: float = DEFAULT_FEE) -> float:
    """Net return on margin, in percent: gross minus round-trip fees."""
    return _gross_pct(trade, exit_price) - _fee_pct(trade, fee)


# --------------------------------------------------------------------- report


def _fmt_pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def _fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _print_block(item: Stats) -> None:
    print()
    print(f"  {item.pair} | {item.timeframe}  ({item.trades[0].mode if item.trades else '-'})")
    print("  " + "-" * 74)
    print(f"  Operaciones       : {item.total}  (long {item.longs} / short {item.shorts})")

    if item.total == 0:
        print("  Win Rate          : -")
        print("  Profit Factor     : -")
        print("  PnL Bruto         : 0.00%")
        print("  Comisiones        : 0.00%")
        print("  PnL Neto          : 0.00%")
        print("  Max Drawdown      : 0.00 pts")
        print("  Racha perdedora   : 0")
        print("  Nota              : sin señales en la ventana; nada que simular.")
        return

    print(f"  Win Rate          : {item.win_rate:.1f}%  ({item.wins}G / {item.losses}P)")
    print(
        f"  Profit Factor     : {_fmt_pf(item.profit_factor)}  "
        f"(GP {item.gross_profit:.2f}% / GL {item.gross_loss:.2f}%)"
    )
    print(f"  PnL Bruto         : {item.gross_pnl:+.2f}%")
    print(f"  Comisiones        : -{item.fees_paid:.2f}%  (ambos lados, x{_leverage_of(item)})")
    print(f"  PnL Neto          : {item.total_pnl:+.2f}%  (riesgo constante)")
    print(f"  Max Drawdown      : {item.max_drawdown:.2f} pts")
    print(f"  Racha perdedora   : {item.longest_losing_streak} operaciones")
    print(f"  Media G / P       : {item.avg_win:+.2f}% / {item.avg_loss:+.2f}%")

    exits: dict[str, int] = {}
    for trade in item.trades:
        exits[trade.exit_reason] = exits.get(trade.exit_reason, 0) + 1
    print("  Salidas           : " + ", ".join(f"{k} {v}" for k, v in sorted(exits.items())))

    if item.break_even_exits:
        print(f"  Break-even        : {item.break_even_exits} operaciones tocaron el trailing")

    first, last = item.trades[0], item.trades[-1]
    print(
        f"  Ventana           : {_fmt_time(first.entry_time)} -> "
        f"{_fmt_time(last.entry_time)} UTC"
    )


def _leverage_of(item: Stats) -> str:
    """Leverage label for the fee line; a mixed set shows the range."""
    values = sorted({t.leverage for t in item.trades})
    if not values:
        return "-"
    if len(values) == 1:
        return str(values[0])
    return f"{values[0]}-{values[-1]}"


def print_report(
    stats: Iterable[Stats],
    candles: int,
    fee: float = DEFAULT_FEE,
    trail_trigger: float | None = None,
) -> None:
    stats = list(stats)

    # Built outside the f-string: nested same-quote f-strings only parse on
    # Python 3.12+, and this file targets 3.10+.
    trail_label = "off" if not trail_trigger else f"{trail_trigger * 100:.1f}%"

    print()
    print("=" * 78)
    print(f"  BACKTEST — trading-copilot   ({candles} velas por timeframe)")
    print(f"  fee {fee * 100:.3f}%/lado · trailing break-even {trail_label}")
    print("=" * 78)

    for item in stats:
        _print_block(item)

    # ---------------------------------------------------------- aggregate
    all_trades = [trade for item in stats for trade in item.trades]
    aggregate = Stats(pair="TOTAL", timeframe="all", trades=all_trades)

    print()
    print("=" * 78)
    print("  AGREGADO")
    print("=" * 78)
    if not all_trades:
        print("  Sin operaciones en ningún par/timeframe.")
        print()
        return

    print(f"  Operaciones       : {aggregate.total}  (long {aggregate.longs} / short {aggregate.shorts})")
    print(f"  Win Rate          : {aggregate.win_rate:.1f}%  ({aggregate.wins}G / {aggregate.losses}P)")
    print(
        f"  Profit Factor     : {_fmt_pf(aggregate.profit_factor)}  "
        f"(GP {aggregate.gross_profit:.2f}% / GL {aggregate.gross_loss:.2f}%)"
    )
    print(f"  PnL Bruto         : {aggregate.gross_pnl:+.2f}%")
    print(f"  Comisiones        : -{aggregate.fees_paid:.2f}%")
    print(f"  PnL Neto          : {aggregate.total_pnl:+.2f}%")
    print(f"  Max Drawdown      : {aggregate.max_drawdown:.2f} pts")
    print(f"  Racha perdedora   : {aggregate.longest_losing_streak} operaciones")
    print(f"  Media G / P       : {aggregate.avg_win:+.2f}% / {aggregate.avg_loss:+.2f}%")
    print(f"  Break-even        : {aggregate.break_even_exits} operaciones tocaron el trailing")

    # Top 3 / worst 3, so a single lucky trade is visible rather than hidden.
    ranked = sorted(all_trades, key=lambda t: t.pnl_pct, reverse=True)
    print()
    print("  Mejores:")
    for trade in ranked[:3]:
        print(
            f"    {trade.pnl_pct:+7.2f}%  {trade.pair:<8} {trade.timeframe:<3} "
            f"{trade.direction:<5} {trade.reason}"
        )
    print("  Peores:")
    for trade in ranked[-3:][::-1]:
        print(
            f"    {trade.pnl_pct:+7.2f}%  {trade.pair:<8} {trade.timeframe:<3} "
            f"{trade.direction:<5} {trade.reason}"
        )
    print()


# ----------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest trading-copilot strategies over Binance Futures klines.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--candles",
        type=int,
        default=DEFAULT_CANDLES,
        help="closed candles to download per pair and timeframe",
    )
    parser.add_argument(
        "--pairs",
        default=",".join(DEFAULT_PAIRS),
        help="comma-separated symbols",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP,
        help="seconds between API requests",
    )
    parser.add_argument(
        "--fee",
        type=float,
        default=DEFAULT_FEE,
        help="per-side taker fee as a fraction (0.0004 = 0.04%%)",
    )
    parser.add_argument(
        "--trail-trigger",
        type=float,
        default=DEFAULT_TRAIL_TRIGGER,
        help="favourable move, as a fraction of entry, that arms the break-even stop",
    )
    parser.add_argument(
        "--no-trail",
        action="store_true",
        help="disable the break-even trailing stop",
    )
    args = parser.parse_args(argv)

    if args.candles < 100:
        parser.error("--candles must be at least 100 (indicators need history)")
    if args.fee < 0:
        parser.error("--fee cannot be negative")
    if not 0 <= args.trail_trigger < 1:
        parser.error("--trail-trigger must be in [0, 1)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pairs = [pair.strip().upper() for pair in args.pairs.split(",") if pair.strip()]
    trail_trigger = None if args.no_trail else args.trail_trigger

    print()
    print(f"Descargando {args.candles} velas por timeframe para {', '.join(pairs)}…")

    all_stats: list[Stats] = []

    for pair in pairs:
        for timeframe, strategy_cls in SUITE.items():
            try:
                candles = fetch_klines(pair, timeframe, args.candles, args.sleep)
            except urllib.error.HTTPError as exc:
                print(f"  {pair} {timeframe}: HTTP {exc.code} — {exc.reason}")
                continue
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                print(f"  {pair} {timeframe}: {type(exc).__name__}: {exc}")
                continue

            if not candles:
                print(f"  {pair} {timeframe}: sin datos, se omite")
                continue

            strategy = strategy_cls((pair,))
            # Trailing is a feature of the aggressive mode: its ATR target sits
            # close enough that protecting the move matters. The 1h safe trade
            # targets 7% and would be cut early by a 1.5% trigger.
            mode_trail = trail_trigger if strategy.mode == "aggressive" else None

            trades = simulate(
                candles,
                strategy,
                pair,
                timeframe,
                fee=args.fee,
                trail_trigger=mode_trail,
            )
            all_stats.append(Stats(pair=pair, timeframe=timeframe, trades=trades))
            print(f"  {pair} {timeframe}: {len(trades)} operaciones simuladas")

            if args.sleep > 0:
                time.sleep(args.sleep)

    print_report(all_stats, args.candles, fee=args.fee, trail_trigger=trail_trigger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
