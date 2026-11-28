"""
Public market-data feed for Binance USDⓈ-M Futures — kline driven.

Architecture
------------
* One persistent websocket to the combined-stream endpoint, carrying closed
  klines for every (pair, timeframe) combination:
      btcusdt@kline_5m / ethusdt@kline_5m / btcusdt@kline_1h / ethusdt@kline_1h
  A single TCP connection serves all four streams, so the reconnect surface
  stays flat as pairs are added.
* Two things are tracked separately:
    1. `ticks` — the last traded price per pair, refreshed on *every* kline
       frame (forming or closed) so the UI never freezes between closes.
    2. `histories[pair][timeframe]` — closed klines only, appended in order.
       Forming candles (k.x == False) are never stored: evaluating a half-built
       candle is what produced the contradictory signals in the old tick feed.
* Every accepted closed candle bumps `close_seq[(pair, timeframe)]`. The
  manager polls that counter each second; a counter change is the "candle
  closed" event. No callbacks, no cross-task mutable state beyond the ints.
* Reconnection uses exponential backoff with jitter, capped, driven by the
  same task loop, so `start()` only returns on cancellation.

REST fallback
-------------
Some networks complete the websocket handshake but never deliver frames
(fstream.binance.com is commonly filtered). A watchdog watches the time since
the last frame; if the socket stays mute for WS_SILENCE_FALLBACK seconds, a
poller against `/fapi/v1/klines` takes over, fetching the same pairs and
timeframes and ingesting any candle that closed since the last stored one.
The poller stops as soon as websocket frames resume.

Threading model: single event loop. REST calls run in a worker thread via
`asyncio.to_thread` so a slow request cannot block the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from .config import DEFAULT_PAIRS, DEFAULT_TIMEFRAMES, load_market_config

log = logging.getLogger(__name__)

FSTREAM_BASE = "wss://fstream.binance.com/stream"
REST_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

# Re-exported from engine.config so existing imports of these names keep
# working. The live values come from backend/pairs.json at construction time.
__all__ = ["BinanceFeed", "Kline", "Tick", "DEFAULT_PAIRS", "DEFAULT_TIMEFRAMES"]

# Closed candles retained per (pair, timeframe). 300 x 1h ≈ 12 days, which is
# far more than any indicator here needs; memory stays fixed per pair.
# 300 is the floor, not a comfort margin: SafeStrategy gates on EMA200, so the
# buffer must hold at least TREND_PERIOD candles or the regime filter can never
# resolve and the strategy stays silent forever.
HISTORY_MAXLEN = {"5m": 300, "1h": 300}
DEFAULT_MAXLEN = 300

# A pair whose last price update is older than this is reported as stale.
PRICE_STALE_AFTER = 20.0

# Reconnect policy.
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0

# Fail fast if the socket cannot be established (seconds).
CONNECT_TIMEOUT = 10.0

# Websocket silence before the REST fallback takes over (seconds).
WS_SILENCE_FALLBACK = 6.0
# 2 s keeps the UI ticker lively at ~2 req/s (klines weight 2 => 240/min,
# well under the 2400/min futures budget).
REST_POLL_INTERVAL = 2.0
REST_TIMEOUT = 8.0
REST_KLINES_LIMIT = 300  # must backfill EMA200 in one poll, not 120


@dataclass(slots=True)
class Tick:
    """Last traded price for one symbol. Drives the UI price ticker."""

    pair: str
    price: float
    updated_at: float  # time.monotonic()
    event_time_ms: int  # exchange event time, for ordering / debugging


@dataclass(slots=True)
class Kline:
    """One closed candle. Immutable once appended to a history buffer."""

    pair: str
    timeframe: str
    open_time: int  # exchange ms
    close_time: int  # exchange ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed_at: float  # time.monotonic() at ingest

    def as_dict(self) -> dict[str, Any]:
        """Shape handed to strategies — mirrors the REST kline layout."""
        return {
            "t": self.open_time,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
        }


@dataclass(slots=True)
class BinanceFeed:
    """
    Subscribes to closed klines for a fixed set of pairs and timeframes.

    Public surface:
        await feed.start()                  -> runs until cancelled
        await feed.stop()                   -> closes the socket, cancels tasks
        feed.snapshot()                     -> {pair: Tick}
        feed.price(pair)                    -> float | None
        feed.is_stale(pair)                 -> bool
        feed.history(pair, timeframe)       -> list[Kline], oldest first
        feed.closed_sequences()             -> {(pair, timeframe): int}
        feed.last_closed(pair, timeframe)   -> Kline | None
        feed.source                         -> "websocket" | "rest" | "none"

    Pairs and timeframes default to backend/pairs.json. The manager passes them
    explicitly, so the file is read once per boot; a standalone
    `BinanceFeed()` still picks up the configured market.
    """

    pairs: tuple[str, ...] = field(default_factory=lambda: load_market_config().pairs)
    timeframes: tuple[str, ...] = field(default_factory=lambda: load_market_config().timeframes)

    ticks: dict[str, Tick] = field(default_factory=dict)
    histories: dict[str, dict[str, deque[Kline]]] = field(default_factory=dict)
    close_seq: dict[tuple[str, str], int] = field(default_factory=dict)

    connected: bool = False
    last_error: str | None = None
    source: str = "none"  # which transport last produced data

    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _ws: Any = field(default=None, repr=False)
    _last_frame_at: float = field(default=0.0, repr=False)
    _last_open_time: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Nested deques: histories[pair][timeframe] -> closed candles.
        self.histories = {
            pair: {
                tf: deque(maxlen=HISTORY_MAXLEN.get(tf, DEFAULT_MAXLEN))
                for tf in self.timeframes
            }
            for pair in self.pairs
        }
        self.close_seq = {(pair, tf): 0 for pair in self.pairs for tf in self.timeframes}

    # ------------------------------------------------------------------ URL

    def _stream_url(self) -> str:
        streams = "/".join(
            f"{pair.lower()}@kline_{tf}" for tf in self.timeframes for pair in self.pairs
        )
        return f"{FSTREAM_BASE}?streams={streams}"

    # --------------------------------------------------------------- public

    def snapshot(self) -> dict[str, Tick]:
        """Shallow copy: callers iterate without holding a lock."""
        return dict(self.ticks)

    def price(self, pair: str) -> float | None:
        tick = self.ticks.get(pair.upper())
        return tick.price if tick else None

    def is_stale(self, pair: str) -> bool:
        tick = self.ticks.get(pair.upper())
        if tick is None:
            return True
        return (time.monotonic() - tick.updated_at) > PRICE_STALE_AFTER

    def history(self, pair: str, timeframe: str) -> list[Kline]:
        """Closed candles, oldest first. Empty list for unknown keys."""
        buffer = self.histories.get(pair.upper(), {}).get(timeframe)
        return list(buffer) if buffer else []

    def closed_sequences(self) -> dict[tuple[str, str], int]:
        """Snapshot of the per-(pair, timeframe) closed-candle counters."""
        return dict(self.close_seq)

    def last_closed(self, pair: str, timeframe: str) -> Kline | None:
        buffer = self.histories.get(pair.upper(), {}).get(timeframe)
        return buffer[-1] if buffer else None

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """
        Run the feed forever. Idempotent: a second call is a no-op.
        Cancelling the calling task shuts it down via the finally block.
        """
        if self._task is not None and not self._task.done():
            log.warning("BinanceFeed.start() called while already running")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="binance-feed")
        try:
            await self._task
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        """Close the socket and wait for the task to unwind."""
        self._stop_event.set()

        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # socket already dead is not an error
                pass

        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        self.connected = False
        self.source = "none"
        log.info("BinanceFeed stopped")

    # -------------------------------------------------------------- internals

    async def _run(self) -> None:
        delay = RECONNECT_BASE_DELAY
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                # Clean exit (server closed) — treat as a drop.
                delay = RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Binance feed dropped (%s); retry in %.1fs", self.last_error, delay)
            except Exception as exc:  # never let the loop die on a parse bug
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Unexpected feed error; retry in %.1fs", delay)
            finally:
                self.connected = False

            if self._stop_event.is_set():
                break

            # Backoff with jitter so a Binance-side outage does not stampede.
            sleep_for = delay + random.uniform(0, delay / 2)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
                break  # stop was requested during the wait
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _connect_once(self) -> None:
        url = self._stream_url()
        log.info("Connecting to %s", url)

        async with websockets.connect(
            url,
            ping_interval=20,   # Binance drops silent peers at ~3 min
            ping_timeout=20,
            close_timeout=5,
            max_queue=1024,
            open_timeout=CONNECT_TIMEOUT,
        ) as ws:
            self._ws = ws
            self.connected = True
            self._last_frame_at = time.monotonic()
            log.info(
                "Feed connected: %d streams (%s x %s)",
                len(self.pairs) * len(self.timeframes),
                ", ".join(self.pairs),
                ", ".join(self.timeframes),
            )

            watchdog = asyncio.create_task(self._watchdog(), name="feed-watchdog")
            try:
                async for raw in ws:
                    self._last_frame_at = time.monotonic()
                    if self._stop_event.is_set():
                        break
                    self._handle_message(raw)
            finally:
                watchdog.cancel()
                try:
                    await watchdog
                except (asyncio.CancelledError, Exception):
                    pass
                self._ws = None

    async def _watchdog(self) -> None:
        """
        Promote to REST when the socket is open but mute.

        Runs for the lifetime of one websocket connection: cancels the poller
        and demotes `source` back to "websocket" the moment frames resume.
        """
        poller: asyncio.Task[None] | None = None
        try:
            while not self._stop_event.is_set():
                silent_for = time.monotonic() - self._last_frame_at
                if silent_for > WS_SILENCE_FALLBACK:
                    if poller is None or poller.done():
                        log.warning(
                            "Websocket silent for %.0fs — starting REST kline fallback",
                            silent_for,
                        )
                        poller = asyncio.create_task(self._rest_poll(), name="feed-rest-poll")
                elif poller is not None and not poller.done():
                    log.info("Websocket frames resumed — stopping REST fallback")
                    poller.cancel()
                    try:
                        await poller
                    except (asyncio.CancelledError, Exception):
                        pass
                    poller = None
                    self.source = "websocket"
                await asyncio.sleep(1.0)
        finally:
            if poller is not None and not poller.done():
                poller.cancel()
                try:
                    await poller
                except (asyncio.CancelledError, Exception):
                    pass

    async def _rest_poll(self) -> None:
        """
        5 s REST fallback. One request per (pair, timeframe); only closed
        candles are ingested, so the strategy inputs stay identical to the
        websocket path.
        """
        while not self._stop_event.is_set():
            for pair in self.pairs:
                for tf in self.timeframes:
                    if self._stop_event.is_set():
                        return
                    try:
                        rows = await asyncio.to_thread(self._fetch_rest_klines, pair, tf)
                        now_ms = int(time.time() * 1000)
                        for row in rows:
                            # The final row is the forming candle — skip it.
                            if int(row[6]) >= now_ms:
                                continue
                            self._ingest_kline(
                                pair=pair,
                                timeframe=tf,
                                open_time=int(row[0]),
                                close_time=int(row[6]),
                                open_price=float(row[1]),
                                high=float(row[2]),
                                low=float(row[3]),
                                close=float(row[4]),
                                volume=float(row[5]),
                                closed=True,
                            )

                        # Live price for the UI: close of the newest row, which
                        # is the forming candle. Same field the websocket path
                        # uses, so the ticker behaves identically either way.
                        if rows:
                            live_price = float(rows[-1][4])
                            if live_price > 0:
                                self.ticks[pair] = Tick(
                                    pair=pair,
                                    price=live_price,
                                    updated_at=time.monotonic(),
                                    event_time_ms=now_ms,
                                )
                        self.source = "rest"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_error = f"REST fallback: {type(exc).__name__}: {exc}"
                        log.warning("REST kline request failed: %s", self.last_error)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=REST_POLL_INTERVAL)
                return  # stop requested
            except asyncio.TimeoutError:
                pass

    def _fetch_rest_klines(self, pair: str, timeframe: str) -> list[list[Any]]:
        """
        Blocking HTTP GET — always called through `asyncio.to_thread`.
        Returns raw Binance kline rows, oldest first. The final row is the
        forming candle; `_ingest_kline` filters by close_time.
        """
        query = urllib.parse.urlencode(
            {"symbol": pair.upper(), "interval": timeframe, "limit": REST_KLINES_LIMIT}
        )
        request = urllib.request.Request(
            f"{REST_KLINES_URL}?{query}",
            headers={"User-Agent": "trading-copilot/0.2"},
        )
        with urllib.request.urlopen(request, timeout=REST_TIMEOUT) as response:
            payload = json.loads(response.read())
        return payload if isinstance(payload, list) else []

    # -------------------------------------------------------------- parsing

    def _handle_message(self, raw: str | bytes) -> None:
        """
        Parse one combined-stream frame:
            {"stream": "btcusdt@kline_5m",
             "data": {"e": "kline", "s": "BTCUSDT", "E": 1717000000000,
                      "k": {"t":..., "T":..., "i":"5m", "o":..., "h":...,
                            "l":..., "c":..., "v":..., "x": false}}}

        The price is refreshed on every frame (forming or closed) so the UI
        ticks; the candle itself is stored only when `x` is true.
        """
        try:
            payload = json.loads(raw)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                return

            candle = data.get("k")
            if not isinstance(candle, dict):
                return

            pair = str(data.get("s", "")).upper()
            timeframe = str(candle.get("i", ""))
            if pair not in self.pairs or timeframe not in self.timeframes:
                return

            close_price = float(candle["c"])
            if close_price <= 0:
                return

            # Live price for the UI: every frame, closed or not.
            self.ticks[pair] = Tick(
                pair=pair,
                price=close_price,
                updated_at=time.monotonic(),
                event_time_ms=int(data.get("E", 0)),
            )
            self.source = "websocket"

            if not bool(candle.get("x", False)):
                return  # forming candle — never evaluated

            self._ingest_kline(
                pair=pair,
                timeframe=timeframe,
                open_time=int(candle["t"]),
                close_time=int(candle["T"]),
                open_price=float(candle["o"]),
                high=float(candle["h"]),
                low=float(candle["l"]),
                close=close_price,
                volume=float(candle.get("v", 0.0)),
                closed=True,
            )
        except (ValueError, KeyError, TypeError) as exc:
            log.debug("Dropped malformed kline: %s (%s)", exc, str(raw)[:120])

    def _ingest_kline(
        self,
        *,
        pair: str,
        timeframe: str,
        open_time: int,
        close_time: int,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        closed: bool,
    ) -> bool:
        """
        Append a candle if it is closed, valid, and newer than the last one
        stored. Returns True when a new candle entered the buffer — the only
        moment the manager is allowed to evaluate that (pair, timeframe).

        Ordering is enforced by open_time, so a REST backfill of several
        candles appends them in order and a duplicate frame is a no-op.
        """
        if not closed or close <= 0:
            return False

        key = (pair, timeframe)
        last_open = self._last_open_time.get(key, 0)
        if open_time <= last_open:
            return False

        buffer = self.histories.get(pair, {}).get(timeframe)
        if buffer is None:
            return False

        buffer.append(
            Kline(
                pair=pair,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                closed_at=time.monotonic(),
            )
        )
        self._last_open_time[key] = open_time
        self.close_seq[key] = self.close_seq.get(key, 0) + 1
        # debug: a REST backfill can ingest hundreds of candles in one pass.
        log.debug("Closed %s %s candle @ %.2f", pair, timeframe, close)
        return True
