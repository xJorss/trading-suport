"""
trading-copilot — FastAPI entrypoint.

Run from `backend/`:
    uvicorn main:app --reload

Endpoints
---------
GET  /api/health     liveness, feed transport, per-timeframe candle state
GET  /api/signals    full in-memory signal history (bounded to 500)
GET  /api/state      one-shot state snapshot (same payload as the socket)
POST /api/reset      wipe the paper account (capital 1000, book + history cleared)
WS   /ws             pushes engine state every second — prices keep moving
                     even though strategies only run on candle closes

Lifespan
--------
The engine starts as a background task in `startup` and is torn down in
`shutdown`. Neither blocks: if Binance is unreachable the API still serves,
and the feed reports the error through `feed.last_error`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from engine.manager import EngineManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("trading-copilot")

WS_PUSH_INTERVAL = 1.0  # seconds between state pushes

engine = EngineManager()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the engine before serving, tear it down after."""
    log.info("Starting trading-copilot engine")
    engine_task = asyncio.create_task(engine.start(), name="engine-start")
    try:
        yield
    finally:
        log.info("Shutting down trading-copilot engine")
        engine_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await engine_task
        await engine.stop()


app = FastAPI(
    title="trading-copilot",
    version="0.2.0",
    description="Binance Futures closed-kline signal engine — safe (1h) & aggressive (5m)",
    lifespan=lifespan,
)

# Local dev: the frontend runs on a different port during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ REST API

@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    """Index — handy when someone opens the bare host in a browser."""
    return {
        "service": "trading-copilot",
        "version": app.version,
        "endpoints": ["/api/health", "/api/state", "/api/signals", "/api/reset", "/ws", "/docs"],
    }


# `/health` is served as an alias because every uptime probe, container check
# and browser tab guesses that path before it guesses `/api/health`.
@app.get("/api/health")
@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "feed_connected": engine.feed.connected,
        "feed_source": engine.feed.source,
        "feed_last_error": engine.feed.last_error,
        "market": engine.market.as_dict(),
        "timeframes": list(engine.timeframes),
        "evaluations": engine.evaluation_count,
        "signals": len(engine.signals),
        "alerts": engine.alerts.status(),
        "candles": {
            pair: {
                timeframe: len(engine.feed.history(pair, timeframe))
                for timeframe in engine.timeframes
            }
            for pair in engine.pairs
        },
    }


@app.get("/api/signals")
async def get_signals(limit: int = 500) -> dict[str, Any]:
    """Signal history, oldest first. `limit` clamped to the in-memory ceiling."""
    limit = max(1, min(limit, 500))
    return {
        "count": len(engine.signals),
        "signals": engine.signals[-limit:],
    }


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return engine.state()


@app.post("/api/reset")
async def reset_account() -> dict[str, Any]:
    """
    Wipe the paper account: capital back to 1000, book and history cleared.

    Synchronous and fast — it touches in-memory structures and writes one small
    JSON file, so it cannot stall the event loop. The websocket pushes the new
    state on its next tick (<= 1 s), which is what the UI reacts to.
    """
    result = engine.reset_account()
    log.info("Paper account reset via API: %s", result)
    return result


# ------------------------------------------------------------------ WebSocket

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """
    Push engine state once per second.

    Two failure modes handled explicitly:
      * client disconnects  -> WebSocketDisconnect, exit cleanly
      * send fails mid-push -> generic exception, exit and free the pump
    """
    await ws.accept()
    log.info("WS client connected: %s", ws.client)

    pump = asyncio.create_task(_push_loop(ws), name="ws-push")
    try:
        # Keep the receive side alive so we notice a disconnect promptly.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        log.info("WS client disconnected: %s", ws.client)
    except Exception:
        log.debug("WS receive failed", exc_info=True)
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump


async def _push_loop(ws: WebSocket) -> None:
    """Pump state until the socket dies. Exits on any send error."""
    while True:
        try:
            await ws.send_json(engine.state())
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception:
            log.debug("WS push failed, closing pump", exc_info=True)
            return
        await asyncio.sleep(WS_PUSH_INTERVAL)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
