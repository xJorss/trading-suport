"""
Risk sizing for trading-copilot.

The strategies say *where* to enter and where to stop out. They say nothing
about how much money to put behind the idea — that is this module's job, and
it is the only thing that decides position size.

Model
-----
Fixed-fractional risk. Each trade risks a fixed percentage of equity, so a
stop-out costs the same USDT whether the stop is 0.5% or 5% away from entry:

    risk_usdt        = capital * risk_pct(mode)
    stop_distance    = |entry - stop_loss| / entry
    size_usdt        = risk_usdt / stop_distance          (notional, pre-leverage)

`size_usdt` is the *notional* position value, not the margin. At 3x leverage a
1000 USDT position ties up ~333 USDT of margin; the fee model and the exchange
both work off the notional, which is why the notional is what gets published.

Per-mode budgets
----------------
    safe        2.0%   — 1h trades, ~1 per day, wide stops
    aggressive  0.5%   — 5m trades, ~50 per week, and every one of them pays
                         0.24% of notional in fees. A bigger budget there just
                         buys a faster way to lose the same edge.

Guards
------
A size is refused (returns 0.0) when:

  * the signal has no usable entry or stop price
  * the stop is on the wrong side of entry (a long with the stop above it)
  * the stop is further than MAX_STOP_DISTANCE_PCT (10%) — that is not a stop,
    it is a different trade, and sizing it at fixed risk would produce a
    position smaller than the exchange minimum
  * the stop is closer than MIN_STOP_DISTANCE_PCT (0.1%) — fixed-risk sizing
    against a near-zero distance produces an absurd notional, and a stop inside
    the spread is unexecutable anyway
  * capital or the risk budget is zero or negative

Every refusal carries a human-readable reason in `last_reason`, which the
manager logs when it discards a signal.

Persistence
-----------
The capital survives restarts through the same JSON file PositionTracker uses.
`save_state()` merges `capital` into whatever is already on disk (the tracker's
keys stay untouched) and `load_state()` restores it, defaulting to
DEFAULT_CAPITAL when the file is missing or the value is unusable. The atomic
read/merge/write helpers live in `positions.py` so there is exactly one
implementation of the file protocol.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from .positions import DEFAULT_STATE_PATH, read_state_file, write_state_file

log = logging.getLogger(__name__)

DEFAULT_CAPITAL = 1000.0

# Fraction of capital risked per trade, by strategy mode.
RISK_PCT_BY_MODE: dict[str, float] = {
    "safe": 0.02,        # 2.0%
    "aggressive": 0.005,  # 0.5%
}

# Unknown mode: assume the aggressive budget. Being wrong on the small side
# costs opportunity; being wrong on the large side costs capital.
DEFAULT_RISK_PCT = 0.005

MAX_STOP_DISTANCE_PCT = 0.10   # 10% — beyond this the trade is not ours to size
MIN_STOP_DISTANCE_PCT = 0.001  # 0.1% — below this the stop is inside the spread


class RiskManager:
    """
    Fixed-fractional position sizing against a virtual capital base.

    Public surface:
        risk.capital                                  -> float
        risk.set_capital(value)                       -> None
        risk.calculate_position_size(signal, capital) -> float  (notional USDT)
        risk.last_reason                              -> str | None
        risk.status()                                 -> dict for /api/state
    """

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        risk_pct_by_mode: Mapping[str, float] | None = None,
    ) -> None:
        self._capital = max(0.0, float(capital))
        self.risk_pct_by_mode: dict[str, float] = dict(
            RISK_PCT_BY_MODE if risk_pct_by_mode is None else risk_pct_by_mode
        )
        # Reason for the most recent refusal. Written and read synchronously by
        # the engine loop, so it needs no locking.
        self.last_reason: str | None = None

    # ------------------------------------------------------------- capital

    @property
    def capital(self) -> float:
        return self._capital

    @capital.setter
    def capital(self, value: float) -> None:
        self.set_capital(value)

    def set_capital(self, value: float) -> None:
        """Replace the virtual equity. Negative values clamp to zero."""
        self._capital = max(0.0, float(value))
        log.info("Risk capital set to %.2f USDT", self._capital)

    # ---------------------------------------------------------------- sizing

    def risk_pct(self, mode: str) -> float:
        """Fraction of capital this mode is allowed to risk per trade."""
        return float(self.risk_pct_by_mode.get(str(mode).lower(), DEFAULT_RISK_PCT))

    def risk_budget(self, mode: str, capital: float | None = None) -> float:
        """USDT at risk for one trade in this mode."""
        base = self._capital if capital is None else float(capital)
        return max(0.0, base) * self.risk_pct(mode)

    def calculate_position_size(
        self, signal: dict[str, Any], current_capital: float | None = None
    ) -> float:
        """
        Notional position size in USDT, before leverage.

        Returns 0.0 when the signal cannot be sized; `last_reason` says why.
        """
        self.last_reason = None

        try:
            entry = float(signal["entry"])
            stop_loss = float(signal["stop_loss"])
        except (KeyError, TypeError, ValueError):
            self.last_reason = "missing or non-numeric entry/stop_loss"
            return 0.0

        mode = str(signal.get("mode", ""))
        direction = str(signal.get("direction", "")).lower()
        capital = self._capital if current_capital is None else float(current_capital)

        if entry <= 0 or stop_loss <= 0:
            self.last_reason = f"non-positive price (entry {entry}, stop {stop_loss})"
            return 0.0

        if capital <= 0:
            self.last_reason = f"no capital (equity {capital:.2f})"
            return 0.0

        # The stop must sit on the protective side of the entry.
        if direction == "long" and stop_loss >= entry:
            self.last_reason = f"long stop {stop_loss} is not below entry {entry}"
            return 0.0
        if direction == "short" and stop_loss <= entry:
            self.last_reason = f"short stop {stop_loss} is not above entry {entry}"
            return 0.0

        stop_distance = abs(entry - stop_loss) / entry

        if stop_distance > MAX_STOP_DISTANCE_PCT:
            self.last_reason = (
                f"stop distance {stop_distance * 100:.2f}% exceeds "
                f"{MAX_STOP_DISTANCE_PCT * 100:.0f}% cap"
            )
            return 0.0
        if stop_distance < MIN_STOP_DISTANCE_PCT:
            self.last_reason = (
                f"stop distance {stop_distance * 100:.3f}% is below "
                f"{MIN_STOP_DISTANCE_PCT * 100:.1f}% floor"
            )
            return 0.0

        risk_usdt = self.risk_budget(mode, capital)
        if risk_usdt <= 0:
            self.last_reason = f"risk budget is zero for mode {mode!r}"
            return 0.0

        size = risk_usdt / stop_distance
        log.debug(
            "sizing %s %s: risk %.2f USDT / stop %.2f%% = %.2f USDT notional",
            mode, direction, risk_usdt, stop_distance * 100, size,
        )
        return size

    # ------------------------------------------------------------ persistence

    def save_state(self, filepath: str | Path | None = None) -> bool:
        """Merge the current capital into the shared state file."""
        path = Path(filepath) if filepath is not None else DEFAULT_STATE_PATH
        payload = read_state_file(path)
        payload["capital"] = round(self._capital, 2)
        return write_state_file(path, payload)

    def load_state(self, filepath: str | Path | None = None) -> bool:
        """
        Restore capital from disk. Returns True when a usable value was found.

        A missing file, a non-numeric value, or a negative one leaves the
        constructor default in place — equity is never loaded as garbage.
        """
        path = Path(filepath) if filepath is not None else DEFAULT_STATE_PATH
        payload = read_state_file(path)
        raw = payload.get("capital")

        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            if payload:
                log.warning("State file %s has no usable capital — keeping %.2f", path, self._capital)
            return False

        capital = float(raw)
        if capital < 0:
            log.warning("State file %s has negative capital %.2f — ignoring", path, capital)
            return False

        self._capital = capital
        log.info("Capital restored from %s: %.2f USDT", path, self._capital)
        return True

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        """Serialisable snapshot for /api/state and /api/health."""
        return {
            "capital": round(self._capital, 2),
            "risk_pct": dict(self.risk_pct_by_mode),
            "max_stop_distance_pct": MAX_STOP_DISTANCE_PCT,
            "min_stop_distance_pct": MIN_STOP_DISTANCE_PCT,
        }
