"""
Aggressive mode — 5m Donchian breakout with a momentum confirmation, ATR stops.

Timeframe: 5m closed candles only. The breakout level is the highest high /
lowest low of the previous LOOKBACK closed candles (excluding the candle that
just closed, which is the breakout candidate). Confirmation comes from the
short EMA slope of closes.

Risk is volatility-scaled instead of a fixed percentage:

    stop_loss   = entry ∓ ATR(14) * 2.0
    take_profit = entry ± ATR(14) * 6.0        (1:3 R:R)

A fixed 0.8% stop is meaningless on a quiet 5m tape (never touched) and lethal
on a volatile one (touched by noise). ATR adapts to the pair and the session.

ATR is Wilder's smoothing (RMA), the canonical definition: seeded with the SMA
of the first `ATR_PERIOD` true ranges, then recursively smoothed. True range
includes the gap from the previous close, so a weekend gap widens the stop
instead of being ignored.

Leverage is 3x, not 10x. The backtest was unambiguous: at 10x the round-trip
fee is 0.8% of margin, and ~100 trades in 10 days burned 80% of margin on fees
alone. Leverage multiplies fees exactly like it multiplies PnL.

Regime filter: a breakout is only taken with the 200-period trend (longs above
the EMA200, shorts below it). Without it the strategy traded both directions
into a trending tape and its gross PnL sat at zero — every counter-trend
breakout was a donation.

Cooldown is measured in candles, not wall time: after a signal, the next
COOLDOWN_CANDLES closes are skipped for that pair (6 x 5m = 30 minutes), so a
choppy range cannot produce back-to-back entries. A 12-candle cooldown was
measured on identical data and lost ~18 points: positions outlive the window,
so it never reduced trade count, it only skipped valid re-entries.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseStrategy, Signal

log = logging.getLogger(__name__)

TIMEFRAME = "5m"

LOOKBACK = 30
SLOPE_PERIOD = 5
ATR_PERIOD = 14
TREND_PERIOD = 200

MIN_HISTORY = max(LOOKBACK + 2, ATR_PERIOD + 2, TREND_PERIOD + 1)

ATR_STOP_MULT = 2.0
ATR_TARGET_MULT = 6.0   # 1:3 R:R — wider stop, much wider target
LEVERAGE = 3
COOLDOWN_CANDLES = 6    # 30 minutes on 5m: no re-entry after a trade

# A degenerate ATR (flat tape, bad data) would place the stop inside the spread.
# Skip the signal rather than emit an unexecutable one.
MIN_STOP_DISTANCE_PCT = 0.001  # 0.1% of price


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = value * k + ema * (1.0 - k)
    return ema


def _atr(history: list[dict[str, Any]], period: int = ATR_PERIOD) -> float | None:
    """
    Wilder's ATR over the tail of `history`.

    Returns None when there are not enough candles to seed the RMA.
    """
    if len(history) < period + 1:
        return None

    # True range needs the previous close, so start at index 1.
    true_ranges: list[float] = []
    for index in range(1, len(history)):
        high = float(history[index]["h"])
        low = float(history[index]["l"])
        prev_close = float(history[index - 1]["c"])
        true_ranges.append(
            max(high - low, abs(high - prev_close), abs(low - prev_close))
        )

    if len(true_ranges) < period:
        return None

    # Seed with the SMA of the first `period` ranges, then Wilder-smooth the
    # rest. Only the tail matters, so iterate over everything for correctness
    # of the recursion.
    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


class AggressiveStrategy(BaseStrategy):
    mode = "aggressive"
    timeframe = TIMEFRAME

    def __init__(self, pairs: tuple[str, ...]) -> None:
        super().__init__(pairs)
        # Bounded: one open_time per pair. Candle-indexed cooldown.
        self._last_signal_open_time: dict[str, int] = {}

    def evaluate(self, data: dict[str, Any]) -> Signal | None:
        try:
            pair = str(data["pair"])
            timeframe = str(data.get("timeframe", TIMEFRAME))
            if timeframe != TIMEFRAME:
                return None  # bound to 5m; the manager should never ask otherwise
            if data.get("stale"):
                return None

            history = data.get("history") or []
            if len(history) < MIN_HISTORY:
                return None

            current = history[-1]
            open_time = int(current["t"])
            price = float(current["c"])  # close of the candle that just closed
            if price <= 0:
                return None

            # Candle-indexed cooldown: skip the next N closes after a signal.
            last_open = self._last_signal_open_time.get(pair, 0)
            if last_open:
                skipped = sum(1 for c in history if int(c["t"]) > last_open) - 1
                if skipped < COOLDOWN_CANDLES:
                    return None

            prior = history[-LOOKBACK - 1 : -1]
            if len(prior) < LOOKBACK:
                return None

            prior_high = max(float(c["h"]) for c in prior)
            prior_low = min(float(c["l"]) for c in prior)

            closes = [float(c["c"]) for c in history]
            fast = _ema(closes, SLOPE_PERIOD)
            fast_prev = _ema(closes[:-1], SLOPE_PERIOD)
            if fast is None or fast_prev is None:
                return None
            slope_up = fast > fast_prev
            slope_down = fast < fast_prev

            # Macro regime: no breakout is taken against the 200-period trend.
            trend_ema = _ema(closes, TREND_PERIOD)
            if trend_ema is None or trend_ema <= 0:
                return None  # no regime, no trade

            atr = _atr(history, ATR_PERIOD)
            if atr is None or atr <= 0:
                return None
            if atr * ATR_STOP_MULT < price * MIN_STOP_DISTANCE_PCT:
                return None  # degenerate volatility, unexecutable stop

            if price > prior_high and slope_up and price > trend_ema:
                self._last_signal_open_time[pair] = open_time
                return self._build_signal(
                    pair=pair,
                    direction="long",
                    entry=price,
                    stop_loss=price - atr * ATR_STOP_MULT,
                    take_profit=price + atr * ATR_TARGET_MULT,
                    leverage=LEVERAGE,
                    reason=f"5m broke {LOOKBACK}-bar high (with trend, atr {atr:.4f})",
                )

            if price < prior_low and slope_down and price < trend_ema:
                self._last_signal_open_time[pair] = open_time
                return self._build_signal(
                    pair=pair,
                    direction="short",
                    entry=price,
                    stop_loss=price + atr * ATR_STOP_MULT,
                    take_profit=price - atr * ATR_TARGET_MULT,
                    leverage=LEVERAGE,
                    reason=f"5m broke {LOOKBACK}-bar low (with trend, atr {atr:.4f})",
                )

            return None
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            log.debug("AggressiveStrategy.evaluate skipped bad payload: %s", exc)
            return None
