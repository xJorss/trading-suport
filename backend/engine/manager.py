"""
EngineManager — the orchestrator.

Owns:
  * one BinanceFeed (closed klines for every pair x timeframe)
  * the strategy instances, each bound to one timeframe
  * the in-memory signal log
  * the 1 Hz state publishing loop

Evaluation model
----------------
There is no "evaluate every second" anymore. The feed keeps a closed-candle
counter per (pair, timeframe); the manager polls that counter once per second
and evaluates only the (pair, timeframe) whose counter moved. So:

    new 1h candle  -> SafeStrategy.evaluate()       (that pair only)
    new 5m candle  -> AggressiveStrategy.evaluate() (that pair only)

The 1 s loop still runs — it exists to publish fresh prices and feed health to
the UI, not to generate signals. Forming candles never reach a strategy, which
is what killed the contradictory signals from the old tick feed.

Signals are bounded (MAX_SIGNALS) and deduped per (pair, mode) over a window,
so a re-evaluated pair cannot flood the log.

Position sizing
---------------
Strategies decide direction and levels; they never decide money. Every signal
that survives dedupe is sized by RiskManager against the virtual equity, and
the resulting notional is attached as `size_usdt`. A signal the risk layer
refuses (stop too wide, stop on the wrong side, no capital) is dropped before
it reaches the log, the UI or Telegram — an unsized signal is not actionable.

Position tracking
-----------------
Every stored signal is handed to PositionTracker, which watches the live price
and closes it when the stop or the target is touched. Realised PnL flows back
into `risk.capital`, so the equity base compounds on wins and bleeds on losses.
Closed trades are published in `state()` for the history table.

A stale tick never closes a position: the feed's own staleness gate decides,
so a dead websocket cannot fake a stop-out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, TypedDict

from .alerts import AlertManager
from .binance_feed import BinanceFeed, Kline, Tick
from .positions import MODE_TIMEFRAME, PositionTracker
from .risk import DEFAULT_CAPITAL, RiskManager
from .strategies.aggressive import AggressiveStrategy
from .strategies.base import BaseStrategy, Signal
from .strategies.safe import SafeStrategy

log = logging.getLogger(__name__)

PAIRS = ("BTCUSDT", "ETHUSDT")
TIMEFRAMES = ("5m", "1h")

MAX_SIGNALS = 500        # signal log ceiling
STATE_INTERVAL = 1.0     # state publishing cadence, seconds
DEDUPE_WINDOW = 30.0     # seconds before the same (pair, mode) can re-fire

# Anchored to backend/ (not the cwd) so the file is the same one regardless of
# where uvicorn was launched from.
STATE_FILE_NAME = "engine_state.json"
STATE_FILE = Path(__file__).resolve().parents[1] / STATE_FILE_NAME


class StoredSignal(Signal):
    """A strategy signal plus the notional the risk layer assigned to it."""

    size_usdt: float


class EngineManager:
    def __init__(
        self,
        pairs: tuple[str, ...] = PAIRS,
        timeframes: tuple[str, ...] = TIMEFRAMES,
    ) -> None:
        self.pairs = pairs
        self.timeframes = timeframes

        self.feed = BinanceFeed(pairs=pairs, timeframes=timeframes)
        self.alerts = AlertManager()
        self.risk = RiskManager()
        self.positions = PositionTracker()

        # Shared snapshot of capital + book. Written atomically by both owners.
        self.state_file: Path = STATE_FILE

        # One strategy per timeframe. Adding a timeframe means adding a
        # strategy here and nothing else — the loop is generic.
        self.strategies_by_timeframe: dict[str, BaseStrategy] = {
            "5m": AggressiveStrategy(pairs=pairs),
            "1h": SafeStrategy(pairs=pairs),
        }

        self.signals: list[StoredSignal] = []

        # Last seen closed-candle counter per (pair, timeframe). The delta is
        # the only trigger for an evaluation.
        self._seen_seq: dict[tuple[str, str], int] = {
            (p, tf): 0 for p in pairs for tf in timeframes
        }
        self._last_eval_at: dict[tuple[str, str], float] = {}
        self._last_signal_key: dict[tuple[str, str], float] = {}

        self._task: asyncio.Task[None] | None = None
        self._feed_task: asyncio.Task[None] | None = None
        # Strong references to in-flight alert deliveries; the loop must not
        # garbage-collect a task mid-send.
        self._alert_tasks: set[asyncio.Task[bool]] = set()
        self._stop_event = asyncio.Event()
        self.started_at: float = 0.0
        self.evaluation_count: int = 0

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Launch the feed and the publishing loop. Idempotent."""
        if self._task is not None and not self._task.done():
            log.warning("EngineManager already running")
            return

        self._stop_event.clear()
        self.started_at = time.time()
        log.info(
            "Starting EngineManager — pairs=%s timeframes=%s",
            ", ".join(self.pairs),
            ", ".join(self.timeframes),
        )

        # Restore before the feed starts: the first signal must be sized
        # against the real equity, not against a fresh 1000.
        self._load_state()

        # Feed runs as its own supervised task; the loop never awaits it
        # directly, so a feed stall cannot freeze state publishing.
        self._feed_task = asyncio.create_task(self.feed.start(), name="feed-start")
        self._task = asyncio.create_task(self._loop(), name="engine-loop")

        try:
            await self._task
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        """Ordered teardown: stop the loop, then the feed."""
        log.info("Stopping EngineManager")
        self._stop_event.set()

        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        await self.feed.stop()

        feed_task, self._feed_task = self._feed_task, None
        if feed_task is not None and not feed_task.done():
            feed_task.cancel()
            try:
                await feed_task
            except (asyncio.CancelledError, Exception):
                pass

        # Grace period so a signal generated seconds before shutdown still
        # reaches Telegram; anything slower gets cancelled.
        if self._alert_tasks:
            pending = list(self._alert_tasks)
            _, still_pending = await asyncio.wait(pending, timeout=3.0)
            for alert_task in still_pending:
                alert_task.cancel()
            self._alert_tasks.clear()

        # Last snapshot on the way out: an open position must survive a restart.
        self._save_state()

        log.info(
            "EngineManager stopped after %d evaluations and %d signals",
            self.evaluation_count,
            len(self.signals),
        )

    # ------------------------------------------------------------ persistence

    def _load_state(self) -> None:
        """Restore capital and the position book from the shared state file."""
        try:
            capital_loaded = self.risk.load_state(self.state_file)
            book_loaded = self.positions.load_state(self.state_file)
        except Exception:
            # A corrupt or unreadable snapshot must never block the boot.
            log.exception("State restore failed — starting from defaults")
            return

        if capital_loaded or book_loaded:
            log.info(
                "State restored: capital %.2f USDT, %d open, %d closed",
                self.risk.capital,
                len(self.positions.active_positions),
                len(self.positions.closed_positions),
            )
        else:
            log.info("No prior state at %s — fresh start", self.state_file)

    def _save_state(self) -> None:
        """Persist capital and the book. Failures are logged, never raised."""
        try:
            self.risk.save_state(self.state_file)
            self.positions.save_state(self.state_file)
        except Exception:
            log.exception("State save failed at %s", self.state_file)

    # ------------------------------------------------------------------ reset

    def reset_account(self) -> dict[str, Any]:
        """
        Wipe the paper account back to a clean 1000 USDT.

        Clears the signal log, the open book, the closed history and every
        lifetime counter, restores the equity base, and writes the snapshot so
        a restart cannot resurrect what was just deleted.

        The dedupe window is cleared too: after a reset the engine should be
        able to act on the next candle instead of waiting out a window that
        refers to signals that no longer exist.
        """
        previous_capital = self.risk.capital
        previous_closed = len(self.positions.closed_positions)

        self.signals.clear()
        self.positions.active_positions.clear()
        self.positions.closed_positions.clear()
        self.positions.opened = 0
        self.positions.closed = 0
        self.positions.wins = 0
        self.positions.losses = 0
        self.positions.net_pnl_usdt = 0.0
        self._last_signal_key.clear()

        # Direct assignment, not +=: the reset sets the base, it does not
        # adjust it. Goes through the setter so the clamp and log still apply.
        self.risk.capital = DEFAULT_CAPITAL

        self._save_state()

        log.info(
            "ACCOUNT RESET: capital %.2f -> %.2f USDT, %d closed trade(s) discarded",
            previous_capital, self.risk.capital, previous_closed,
        )

        return {
            "ok": True,
            "capital": self.risk.capital,
            "discarded_closed": previous_closed,
            "previous_capital": previous_capital,
        }

    # -------------------------------------------------------------- main loop

    async def _loop(self) -> None:
        """
        1 Hz heartbeat. Publishes nothing itself — `state()` is pulled by the
        websocket — but keeps the evaluation dispatch honest and cheap.
        """
        while not self._stop_event.is_set():
            try:
                self._dispatch_closed_candles()
                self._check_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop is the heartbeat: log and continue, never die.
                log.exception("Engine loop iteration failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=STATE_INTERVAL)
                break  # stop requested
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------- dispatch

    def _dispatch_closed_candles(self) -> None:
        """
        Evaluate only the (pair, timeframe) pairs whose closed-candle counter
        advanced since the previous pass. A backfilled batch of candles bumps
        the counter many times but still produces a single evaluation.
        """
        sequences = self.feed.closed_sequences()

        for key, seq in sequences.items():
            pair, timeframe = key
            if pair not in self.pairs or timeframe not in self.timeframes:
                continue
            if seq <= self._seen_seq.get(key, 0):
                continue

            self._seen_seq[key] = seq
            self._evaluate(pair, timeframe)

    def _evaluate(self, pair: str, timeframe: str) -> None:
        strategy = self.strategies_by_timeframe.get(timeframe)
        if strategy is None:
            return

        candles: list[Kline] = self.feed.history(pair, timeframe)
        if not candles:
            return

        payload = {
            "pair": pair,
            "timeframe": timeframe,
            "price": candles[-1].close,
            "history": [candle.as_dict() for candle in candles],
            "stale": self.feed.is_stale(pair),
        }

        self.evaluation_count += 1
        self._last_eval_at[(pair, timeframe)] = time.time()

        try:
            signal = strategy.evaluate(payload)
        except Exception:
            log.exception("%s.evaluate crashed on %s %s", type(strategy).__name__, pair, timeframe)
            return

        if signal is not None:
            self._record(signal)

    def _record(self, signal: Signal) -> None:
        """
        Size the signal, then persist it.

        Order matters: the risk layer runs *before* dedupe so a refused signal
        never burns the (pair, mode) window — a trade rejected for being
        unsizeable must not block a valid one 30 seconds later.
        """
        size = self.risk.calculate_position_size(dict(signal), self.risk.capital)
        if size <= 0:
            log.info(
                "Signal dropped %s %s %s — %s",
                signal["mode"], signal["direction"], signal["pair"],
                self.risk.last_reason or "size is zero",
            )
            return

        key = (signal["pair"], signal["mode"])
        now = time.monotonic()
        if now - self._last_signal_key.get(key, 0.0) < DEDUPE_WINDOW:
            return
        self._last_signal_key[key] = now

        stored: StoredSignal = StoredSignal(
            pair=signal["pair"],
            mode=signal["mode"],
            direction=signal["direction"],
            entry=signal["entry"],
            stop_loss=signal["stop_loss"],
            take_profit=signal["take_profit"],
            leverage=signal["leverage"],
            reason=signal["reason"],
            size_usdt=round(size, 2),
        )

        self.signals.append(stored)
        if len(self.signals) > MAX_SIGNALS:
            # Drop oldest half in one shot: O(n) rarely, never per-candle.
            del self.signals[: len(self.signals) - MAX_SIGNALS]

        log.info(
            "SIGNAL %s %s %s @ %.4f (sl %.4f / tp %.4f, %dx, %.2f USDT)",
            stored["mode"], stored["direction"], stored["pair"],
            stored["entry"], stored["stop_loss"], stored["take_profit"],
            stored["leverage"], stored["size_usdt"],
        )

        self._track_position(stored)
        self._dispatch_alert(stored)

    def _track_position(self, signal: StoredSignal) -> None:
        """
        Hand a stored signal to the tracker.

        The entry timestamp is the open time of the candle that produced the
        signal, so the history reads like the chart instead of like the log
        line. Falls back to wall time if the buffer is somehow empty.
        """
        timeframe = MODE_TIMEFRAME.get(str(signal["mode"]).lower(), "5m")
        candle: Kline | None = self.feed.last_closed(signal["pair"], timeframe)
        entry_time = candle.open_time if candle else int(time.time() * 1000)

        if not self.positions.open_position(dict(signal), entry_time):
            log.info(
                "Signal %s %s %s logged but not tracked (position already open)",
                signal["mode"], signal["direction"], signal["pair"],
            )
            return

        # An open position is the one thing a restart cannot rebuild, so it is
        # persisted immediately rather than waiting for the next close.
        self.positions.save_state(self.state_file)

    # -------------------------------------------------------------- positions

    def _check_positions(self) -> None:
        """
        Close any position whose stop or target the live price reached, then
        fold the realised PnL into the virtual equity.

        Stale pairs are skipped: a dead feed must not be able to close a trade
        on a price that never traded.
        """
        if not self.positions.active_positions:
            return

        closed: list[dict[str, Any]] = []
        for pair, tick in self.feed.snapshot().items():
            if self.feed.is_stale(pair):
                continue
            closed.extend(self.positions.check_price_update(pair, tick.price))

        if not closed:
            return

        realised = round(sum(p["net_pnl_usdt"] for p in closed), 2)
        self.risk.capital += realised
        log.info(
            "Equity updated: %+0.2f USDT over %d closed trade(s) -> %.2f USDT",
            realised, len(closed), self.risk.capital,
        )

        # Capital and history move together: one write, both owners.
        self._save_state()

    # ---------------------------------------------------------------- alerts

    def _dispatch_alert(self, signal: Signal) -> None:
        """
        Fire the Telegram notification without blocking the engine.

        `send_signal_async` runs the HTTP POST in a worker thread; the task is
        kept in `_alert_tasks` until it completes so it cannot be collected,
        and a failed send is logged inside AlertManager, never raised here.
        """
        if not self.alerts.enabled:
            return

        task = asyncio.create_task(
            self.alerts.send_signal_async(dict(signal)),
            name=f"alert-{signal['pair']}-{signal['mode']}",
        )
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    # ----------------------------------------------------------------- state

    def _candle_state(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Per pair/timeframe: last closed candle, buffer fill, eval timestamp."""
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for pair in self.pairs:
            per_tf: dict[str, dict[str, Any]] = {}
            for timeframe in self.timeframes:
                last: Kline | None = self.feed.last_closed(pair, timeframe)
                last_eval = self._last_eval_at.get((pair, timeframe))
                per_tf[timeframe] = {
                    "count": len(self.feed.history(pair, timeframe)),
                    "last_close": last.close if last else None,
                    "last_open_ms": last.open_time if last else None,
                    "last_close_ms": last.close_time if last else None,
                    "seq": self.feed.closed_sequences().get((pair, timeframe), 0),
                    "last_eval_ms": int(last_eval * 1000) if last_eval else None,
                }
            out[pair] = per_tf
        return out

    def state(self) -> dict[str, Any]:
        """Serialisable snapshot for the websocket push."""
        prices = {
            pair: {
                "price": tick.price,
                "updated_at_ms": int(tick.updated_at * 1000),
                "stale": self.feed.is_stale(pair),
            }
            for pair, tick in self.feed.snapshot().items()
        }

        return {
            "type": "state",
            "server_time": int(time.time() * 1000),
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "feed": {
                "connected": self.feed.connected,
                "last_error": self.feed.last_error,
                "source": self.feed.source,
                "timeframes": list(self.timeframes),
                "evaluations": self.evaluation_count,
            },
            "prices": prices,
            "candles": self._candle_state(),
            "alerts": self.alerts.status(),
            "risk": self.risk.status(),
            "active_positions": [dict(p) for p in self.positions.active_positions.values()],
            "closed_positions": [dict(p) for p in self.positions.closed_positions],
            "positions_summary": {
                "opened": self.positions.opened,
                "closed": self.positions.closed,
                "wins": self.positions.wins,
                "losses": self.positions.losses,
                "net_pnl_usdt": self.positions.net_pnl_usdt,
            },
            "signals": self.signals[-20:],  # newest last, bounded push
            "signal_count": len(self.signals),
        }
