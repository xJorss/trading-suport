"""
Strategy contract.

A strategy is a pure function over closed-candle state: it receives a snapshot
dict and returns either a signal payload or None. It must not perform I/O, must
not keep unbounded state, and must be cheap enough to run once per closed
candle for every pair.

The `evaluate` input dict is deliberately plain (no class coupling) so backtest
replays and live klines feed the same shape:

    {
        "pair": "BTCUSDT",
        "timeframe": "1h",
        "price": 65000.0,                       # close of the latest candle
        "history": [{"t":..., "o":..., "h":..., "l":..., "c":..., "v":...}, ...],
        "stale": False,
    }

`history` contains closed candles only, oldest first, and always ends with the
candle that just closed. Strategies never see a forming candle.
"""

from __future__ import annotations

import abc
from typing import Any, Literal, TypedDict

Mode = Literal["safe", "aggressive"]
Direction = Literal["long", "short"]


class Signal(TypedDict):
    """Wire format consumed by the API and the frontend."""

    pair: str
    mode: Mode
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    leverage: int
    reason: str


class BaseStrategy(abc.ABC):
    """All strategies implement `evaluate` and declare `mode` + `timeframe`."""

    mode: Mode
    timeframe: str  # which kline interval this strategy is bound to

    def __init__(self, pairs: tuple[str, ...]) -> None:
        self.pairs = pairs

    # ------------------------------------------------------------- contract

    @abc.abstractmethod
    def evaluate(self, data: dict[str, Any]) -> Signal | None:
        """
        Return a signal payload or None.

        Implementations must:
          * return None when history is too short to compute indicators
          * never raise on malformed input — degrade to None and log
          * produce stop_loss on the protective side of entry
            (below entry for long, above entry for short)
          * ignore `data["price"]` if it disagrees with the last close —
            the close is the source of truth for a closed-candle strategy
        """

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _round(value: float, price: float) -> float:
        """Tick-size-aware rounding that keeps sub-dollar precision on cheap alts."""
        if price >= 1000:
            return round(value, 1)
        if price >= 1:
            return round(value, 4)
        return round(value, 6)

    def _build_signal(
        self,
        *,
        pair: str,
        direction: Direction,
        entry: float,
        stop_loss: float,
        take_profit: float,
        leverage: int,
        reason: str,
    ) -> Signal:
        return Signal(
            pair=pair,
            mode=self.mode,
            direction=direction,
            entry=self._round(entry, entry),
            stop_loss=self._round(stop_loss, entry),
            take_profit=self._round(take_profit, entry),
            leverage=leverage,
            reason=reason,
        )
