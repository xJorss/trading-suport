"""
Safe mode — 1h trend following on an EMA cross, gated by a macro trend filter.

Timeframe: 1h closed candles only. A 1h close is a slow event (~24 per day), so
this strategy is deliberately patient.

Two conditions must agree before a trade exists:

  1. REGIME (EMA200)
     Longs only above the EMA200, shorts only below it. Without this gate the
     EMA9/21 cross fires into every counter-trend bounce and gets stopped out —
     which is exactly what the first backtest showed (18% win rate, PF 0.40).

  2. TRIGGER (EMA9 / EMA21 cross)
     Detected between the two most recent states (previous candle vs current),
     so one cross fires exactly once.

Risk: 3.5% stop, 7.0% target, 3x. A 1h EMA cross needs room to breathe; the old
1.5% stop sat inside normal 1h noise and was taken out before the move started.

Both EMAs are computed on *closes* of closed candles.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseStrategy, Signal

log = logging.getLogger(__name__)

TIMEFRAME = "1h"

FAST_PERIOD = 9
SLOW_PERIOD = 21
TREND_PERIOD = 200  # macro regime filter

# Need 200 closes for the trend EMA, and two EMA9/21 states for the cross.
MIN_HISTORY = max(SLOW_PERIOD + 2, TREND_PERIOD + 1)

STOP_PCT = 0.035    # 3.5%
TARGET_PCT = 0.070  # 7.0% — 1:2 R:R, wide enough for a 1h swing
LEVERAGE = 3

# Do not chase: enter no further than this from the slow EMA. Scaled with the
# wider stop — 0.5% was calibrated for a 1.5% stop and starved the strategy of
# entries once the stop moved out.
MAX_EXTENSION_PCT = 0.015


def _ema(values: list[float], period: int) -> float | None:
    """Plain EMA, seeded with an SMA of the first `period` samples."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = value * k + ema * (1.0 - k)
    return ema


class SafeStrategy(BaseStrategy):
    mode = "safe"
    timeframe = TIMEFRAME

    def evaluate(self, data: dict[str, Any]) -> Signal | None:
        try:
            pair = str(data["pair"])
            timeframe = str(data.get("timeframe", TIMEFRAME))
            if timeframe != TIMEFRAME:
                return None  # bound to 1h; the manager should never ask otherwise
            if data.get("stale"):
                return None

            history = data.get("history") or []
            if len(history) < MIN_HISTORY:
                return None

            closes = [float(c["c"]) for c in history]
            price = closes[-1]  # the close of the candle that just closed
            if price <= 0:
                return None

            trend = _ema(closes, TREND_PERIOD)
            if trend is None or trend <= 0:
                return None  # no regime, no trade

            fast = _ema(closes, FAST_PERIOD)
            slow = _ema(closes, SLOW_PERIOD)
            prev_fast = _ema(closes[:-1], FAST_PERIOD)
            prev_slow = _ema(closes[:-1], SLOW_PERIOD)
            if None in (fast, slow, prev_fast, prev_slow):
                return None

            # Confirmation: price must still be near the slow EMA.
            extension = abs(price - slow) / slow
            if extension > MAX_EXTENSION_PCT:
                return None

            crossed_up = prev_fast <= prev_slow and fast > slow
            crossed_down = prev_fast >= prev_slow and fast < slow

            # Regime gate: only trade with the macro trend.
            if crossed_up and price > trend:
                return self._build_signal(
                    pair=pair,
                    direction="long",
                    entry=price,
                    stop_loss=price * (1.0 - STOP_PCT),
                    take_profit=price * (1.0 + TARGET_PCT),
                    leverage=LEVERAGE,
                    reason="1h EMA9 crossed EMA21 above EMA200",
                )

            if crossed_down and price < trend:
                return self._build_signal(
                    pair=pair,
                    direction="short",
                    entry=price,
                    stop_loss=price * (1.0 + STOP_PCT),
                    take_profit=price * (1.0 - TARGET_PCT),
                    leverage=LEVERAGE,
                    reason="1h EMA9 lost EMA21 below EMA200",
                )

            return None
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            log.debug("SafeStrategy.evaluate skipped bad payload: %s", exc)
            return None
