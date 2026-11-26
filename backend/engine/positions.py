"""
Position tracking for trading-copilot.

The strategies emit levels, the risk layer sizes them, and this module is the
only place that decides whether an idea actually worked. It watches the live
price for every tracked position and closes it the moment the stop or the
target is touched, then hands the realised PnL back to the RiskManager so the
virtual equity compounds or bleeds.

Why track instead of just logging signals
-----------------------------------------
A signal log is a list of opinions. Without a close-out, the engine can never
answer the only question that matters — is the edge real, or is it a nice
reason string? Tracking turns every signal into a row with a number on it.

Model
-----
    gross_pnl = size_usdt * direction_sign * (exit - entry) / entry
    fees      = size_usdt * FEE_RATE * 2        # entry + exit, on notional
    net_pnl   = gross_pnl - fees

`size_usdt` is the notional the RiskManager assigned, pre-leverage. Fees are
charged on notional because that is what the exchange charges on — leverage
multiplies the notional, and it multiplies the fee with it. This matches the
backtest's `--fee` model exactly, so live and simulated numbers are comparable.

`pnl_pct` is reported against **margin** (notional / leverage), because that is
the number a trader actually feels: a 2% stop at 3x costs ~6% of the money
committed, not 2%. `pnl_pct_notional` is included for anyone comparing against
the backtest, which reports notional percentages.

Fill assumption
---------------
A touch closes at the level, not at the tick that touched it: the target is a
resting limit order, and the stop is modelled without slippage. That is the
optimistic side of the stop, so treat a run of stop-outs as a floor, never a
ceiling. Real fills on a fast 5m candle are worse, not better.

One position per (pair, timeframe)
----------------------------------
The key is `PAIR_TIMEFRAME` (e.g. "BTCUSDT_5m"). Each strategy is bound to one
timeframe, so this also means one position per pair per strategy — a second
signal on the same pair and timeframe is ignored while the first is open. The
alternative (stacking) would silently multiply exposure past what the risk
layer budgeted for.

Persistence
-----------
The book survives restarts. `save_state()` merges its slice into a single JSON
file and `load_state()` reads it back; a missing file is a clean first boot and
a corrupt one is a logged warning plus defaults, never a crash. Writes go to a
`.tmp` sibling and land via `os.replace`, so a kill mid-write leaves the old
file intact instead of a truncated one.

The file is shared with RiskManager (it holds `capital` too), which is why both
sides *merge* instead of overwriting: each writes only its own keys, after
re-reading whatever is on disk. One file, two owners, no lost fields.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 0.04% per side, the Binance Futures taker rate. Matches backtest --fee.
FEE_RATE = 0.0004

MAX_CLOSED = 50

# Anchored to backend/ so the file is found no matter which directory uvicorn
# was launched from. A cwd-relative path would silently move the state on every
# start from a different shell.
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "engine_state.json"

# Strategy mode -> the timeframe it is bound to. Used to build the position key
# when a signal does not carry an explicit timeframe.
MODE_TIMEFRAME: dict[str, str] = {
    "safe": "1h",
    "aggressive": "5m",
}


def read_state_file(path: str | Path) -> dict[str, Any]:
    """
    Load a JSON object, tolerating every first-boot and corruption case.

    Returns {} for a missing, unreadable or non-object file. Never raises: a
    broken state file must degrade to defaults, not take the engine down.
    """
    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        log.info("No state file at %s — starting fresh", file_path)
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("State file %s unreadable (%s) — starting fresh", file_path, exc)
        return {}

    if not isinstance(data, dict):
        log.warning("State file %s is not a JSON object — starting fresh", file_path)
        return {}
    return data


def write_state_file(path: str | Path, payload: dict[str, Any]) -> bool:
    """
    Atomic JSON write: tmp sibling -> fsync -> os.replace.

    Returns False on any I/O failure. The engine keeps running on a failed save;
    the next close retries. Losing one snapshot is survivable, a crash is not.
    """
    file_path = Path(path)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, file_path)  # atomic on NTFS and POSIX
    except OSError as exc:
        log.warning("Could not write state file %s: %s", file_path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


_REQUIRED_POSITION_KEYS = (
    "key", "pair", "direction", "entry", "stop_loss",
    "take_profit", "size_usdt", "leverage",
)


def _is_valid_position(position: dict[str, Any]) -> bool:
    """Cheap structural check: enough fields to render and to reason about."""
    if not all(field in position for field in _REQUIRED_POSITION_KEYS):
        return False
    if str(position.get("direction", "")).lower() not in ("long", "short"):
        return False
    for field in ("entry", "stop_loss", "take_profit", "size_usdt"):
        value = position.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
    return True


def _as_count(value: Any, fallback: int) -> int:
    """Non-negative int or the current value — counters are never trusted."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


class PositionTracker:
    """
    Live position book.

    Public surface:
        positions.active_positions          -> dict[str, dict]  key: PAIR_TIMEFRAME
        positions.closed_positions          -> list[dict]       oldest first, max 50
        positions.open_position(signal, t)  -> bool
        positions.check_price_update(pair, price) -> list[dict]
        positions.status()                  -> dict for /api/state
    """

    def __init__(self, fee_rate: float = FEE_RATE, max_closed: int = MAX_CLOSED) -> None:
        self.fee_rate = float(fee_rate)
        self.max_closed = int(max_closed)
        self.active_positions: dict[str, dict[str, Any]] = {}
        self.closed_positions: list[dict[str, Any]] = []
        # Lifetime counters, cheap to keep and useful for the UI header.
        self.opened = 0
        self.closed = 0
        self.wins = 0
        self.losses = 0
        self.net_pnl_usdt = 0.0

    # ------------------------------------------------------------- key helpers

    @staticmethod
    def position_key(signal: dict[str, Any]) -> str:
        """`PAIR_TIMEFRAME` — one open position per pair per strategy."""
        pair = str(signal.get("pair", "")).upper()
        timeframe = signal.get("timeframe") or MODE_TIMEFRAME.get(
            str(signal.get("mode", "")).lower(), "?"
        )
        return f"{pair}_{timeframe}"

    # ---------------------------------------------------------------- opening

    def open_position(self, signal: dict[str, Any], entry_time: int) -> bool:
        """
        Track a sized signal. Returns False if one is already open on the key.

        The signal must already carry `size_usdt` — an unsized signal cannot be
        tracked because there is no notional to compute PnL against.
        """
        try:
            pair = str(signal["pair"]).upper()
            direction = str(signal["direction"]).lower()
            entry = float(signal["entry"])
            stop_loss = float(signal["stop_loss"])
            take_profit = float(signal["take_profit"])
            size_usdt = float(signal["size_usdt"])
            leverage = int(signal.get("leverage", 1) or 1)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("open_position skipped malformed signal: %s", exc)
            return False

        if direction not in ("long", "short"):
            log.warning("open_position skipped unknown direction %r", direction)
            return False
        if entry <= 0 or size_usdt <= 0 or leverage <= 0:
            log.warning(
                "open_position skipped non-positive values (entry %s, size %s, lev %s)",
                entry, size_usdt, leverage,
            )
            return False

        key = self.position_key(signal)
        existing = self.active_positions.get(key)
        if existing is not None:
            log.info(
                "Position already open on %s (%s @ %.4f) — new signal not tracked",
                key, existing["direction"], existing["entry"],
            )
            return False

        sign = 1.0 if direction == "long" else -1.0
        tp_gross = size_usdt * sign * (take_profit - entry) / entry
        sl_gross = size_usdt * sign * (stop_loss - entry) / entry
        fees = size_usdt * self.fee_rate * 2.0

        position = {
            "key": key,
            "pair": pair,
            "timeframe": key.split("_", 1)[1] if "_" in key else "?",
            "mode": str(signal.get("mode", "")),
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "leverage": leverage,
            "size_usdt": round(size_usdt, 2),
            "margin_usdt": round(size_usdt / leverage, 2),
            "entry_time": int(entry_time),
            "reason": str(signal.get("reason", "")),
            # Potential outcomes at the two levels, fees included. Shown to the
            # user so a pending trade has a number before it resolves.
            "potential_pnl_usdt_tp": round(tp_gross - fees, 2),
            "potential_pnl_usdt_sl": round(sl_gross - fees, 2),
        }

        self.active_positions[key] = position
        self.opened += 1
        log.info(
            "POSITION OPEN %s %s %s @ %.4f (size %.2f USDT, tp %.2f / sl %.2f USDT net)",
            key, position["mode"], direction, entry, size_usdt,
            position["potential_pnl_usdt_tp"], position["potential_pnl_usdt_sl"],
        )
        return True

    # ---------------------------------------------------------------- closing

    def check_price_update(self, pair: str, current_price: float) -> list[dict[str, Any]]:
        """
        Close every position on `pair` whose stop or target the price reached.

        Returns the closed position dicts, oldest key first. Cheap: at most one
        position per timeframe per pair, so this iterates a handful of dicts.
        """
        pair = str(pair).upper()
        if current_price <= 0:
            return []

        closed: list[dict[str, Any]] = []
        # Snapshot the keys: closing mutates the dict.
        for key in [k for k in self.active_positions if k.startswith(f"{pair}_")]:
            position = self.active_positions.get(key)
            if position is None:
                continue

            is_long = position["direction"] == "long"
            stop = position["stop_loss"]
            target = position["take_profit"]

            hit_stop = current_price <= stop if is_long else current_price >= stop
            hit_target = current_price >= target if is_long else current_price <= target

            if not (hit_stop or hit_target):
                continue

            # A single tick cannot be both, but a gap can cross both levels in
            # one move. The stop wins the tie: assume the worst fill, the same
            # convention the backtest uses.
            if hit_stop:
                reason, exit_price = "stop_loss", stop
            else:
                reason, exit_price = "take_profit", target

            closed.append(self._close(position, exit_price, reason))

        return closed

    def _close(self, position: dict[str, Any], exit_price: float, reason: str) -> dict[str, Any]:
        """Realise a position: compute PnL, move it to the closed log."""
        entry = position["entry"]
        size_usdt = position["size_usdt"]
        leverage = max(1, int(position["leverage"]))
        sign = 1.0 if position["direction"] == "long" else -1.0

        gross = size_usdt * sign * (exit_price - entry) / entry
        fees = size_usdt * self.fee_rate * 2.0
        net = gross - fees
        margin = size_usdt / leverage

        record = {
            **position,
            "exit": round(exit_price, 8),
            "exit_reason": reason,
            "gross_pnl_usdt": round(gross, 2),
            "fees_usdt": round(fees, 2),
            "net_pnl_usdt": round(net, 2),
            # Margin-relative: what the trader actually feels.
            "pnl_pct": round(net / margin * 100.0, 2) if margin > 0 else 0.0,
            # Notional-relative: comparable with the backtest report.
            "pnl_pct_notional": round(net / size_usdt * 100.0, 2) if size_usdt > 0 else 0.0,
        }

        self.active_positions.pop(position["key"], None)
        self.closed_positions.append(record)
        if len(self.closed_positions) > self.max_closed:
            del self.closed_positions[: len(self.closed_positions) - self.max_closed]

        self.closed += 1
        self.net_pnl_usdt = round(self.net_pnl_usdt + net, 2)
        if net >= 0:
            self.wins += 1
        else:
            self.losses += 1

        log.info(
            "POSITION CLOSE %s %s @ %.4f (%s) net %+.2f USDT (%+.2f%% margin)",
            position["key"], position["direction"], exit_price, reason,
            record["net_pnl_usdt"], record["pnl_pct"],
        )
        return record

    # ------------------------------------------------------------ persistence

    def save_state(self, filepath: str | Path | None = None) -> bool:
        """
        Merge the book into the shared state file.

        Read-merge-write, not overwrite: RiskManager writes `capital` into the
        same file and neither side may drop the other's keys.
        """
        path = Path(filepath) if filepath is not None else DEFAULT_STATE_PATH
        payload = read_state_file(path)
        payload.update(
            {
                "active_positions": {
                    key: dict(position)
                    for key, position in self.active_positions.items()
                },
                "closed_positions": [dict(p) for p in self.closed_positions],
                "opened": self.opened,
                "closed": self.closed,
                "wins": self.wins,
                "losses": self.losses,
                "net_pnl_usdt": self.net_pnl_usdt,
            }
        )
        return write_state_file(path, payload)

    def load_state(self, filepath: str | Path | None = None) -> bool:
        """
        Restore the book from disk. Returns True when something was loaded.

        Every field is validated individually: a partially corrupt file keeps
        whatever is still readable and drops the rest, instead of rejecting the
        whole snapshot.
        """
        path = Path(filepath) if filepath is not None else DEFAULT_STATE_PATH
        payload = read_state_file(path)
        if not payload:
            return False

        raw_active = payload.get("active_positions")
        active: dict[str, dict[str, Any]] = {}
        if isinstance(raw_active, dict):
            for key, position in raw_active.items():
                if not isinstance(position, dict):
                    log.warning("Dropping malformed active position %r", key)
                    continue
                if not _is_valid_position(position):
                    log.warning("Dropping incomplete active position %r", key)
                    continue
                active[str(key)] = position

        raw_closed = payload.get("closed_positions")
        closed: list[dict[str, Any]] = []
        if isinstance(raw_closed, list):
            for position in raw_closed:
                if isinstance(position, dict) and _is_valid_position(position):
                    closed.append(position)
                else:
                    log.warning("Dropping malformed closed position entry")

        self.active_positions = active
        self.closed_positions = closed[-self.max_closed :] if closed else []
        self.opened = _as_count(payload.get("opened"), self.opened)
        self.closed = _as_count(payload.get("closed"), self.closed)
        self.wins = _as_count(payload.get("wins"), self.wins)
        self.losses = _as_count(payload.get("losses"), self.losses)
        net = payload.get("net_pnl_usdt")
        if isinstance(net, (int, float)) and not isinstance(net, bool):
            self.net_pnl_usdt = round(float(net), 2)

        log.info(
            "State loaded from %s: %d open, %d closed, net %+.2f USDT",
            path, len(self.active_positions), len(self.closed_positions),
            self.net_pnl_usdt,
        )
        return True

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        """Serialisable snapshot for /api/state."""
        return {
            "active_positions": [dict(p) for p in self.active_positions.values()],
            "closed_positions": [dict(p) for p in self.closed_positions],
            "opened": self.opened,
            "closed": self.closed,
            "wins": self.wins,
            "losses": self.losses,
            "net_pnl_usdt": self.net_pnl_usdt,
        }
