"""
Telegram alerting for trading-copilot.

Design notes
------------
* No SDK: Telegram's Bot API is one HTTPS POST, so `urllib.request` is enough.
  Adding `python-telegram-bot` would pull an event loop, a dispatcher and ~5 MB
  of dependencies for a single fire-and-forget call.
* Credentials come from the environment, loaded from `backend/.env` when
  present (`python-dotenv`). Missing credentials are not an error: the manager
  keeps running and `AlertManager.enabled` stays False, so the engine works
  identically without Telegram configured.
* `send_signal()` is blocking and safe to call from a thread; the manager calls
  `send_signal_async()` which wraps it in `asyncio.to_thread`. Network latency
  never touches the engine loop.
* Every failure path returns False and logs — an alert that cannot be delivered
  must never take down the process that generated the signal.
* A per-chat throttle (`ALERT_MIN_INTERVAL`, default 1 s) protects against the
  Telegram 429 limit if several strategies fire in the same tick.

Environment
-----------
    TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
    TELEGRAM_CHAT_ID=123456789
    ALERT_MIN_INTERVAL=1.0      # optional, seconds
    ALERT_TIMEOUT=8.0           # optional, seconds
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:  # python-dotenv is optional at runtime: env vars alone are enough.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only hit if the package is absent
    load_dotenv = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# backend/.env — resolved from this file, never from the shell's cwd, so
# `uvicorn main:app` works whether you launch it from backend/ or the repo root.
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_TIMEOUT = 8.0


def _format_signal(signal: dict[str, Any]) -> str:
    """Human-readable alert. Plain text on purpose: no parse_mode escaping."""
    mode = str(signal.get("mode", "")).upper()
    direction = str(signal.get("direction", "")).upper()
    pair = signal.get("pair", "?")
    # Sized signals carry the notional the risk layer assigned. Manual test
    # sends without one stay valid — the line is simply omitted.
    size = signal.get("size_usdt")
    size_line = f"Tamaño: {float(size):,.2f} USDT\n" if size is not None else ""
    return (
        f"🚨 SEÑAL: {mode} {direction} en {pair}\n"
        f"Entrada: {signal.get('entry')}\n"
        f"Stop Loss: {signal.get('stop_loss')}\n"
        f"Take Profit: {signal.get('take_profit')}\n"
        f"Leverage: {signal.get('leverage')}x\n"
        f"{size_line}"
        f"Motivo: {signal.get('reason')}"
    )


class AlertManager:
    """
    Fire-and-forget Telegram notifier.

    Public surface:
        alerts.enabled                    -> bool
        alerts.status()                   -> dict for /api/health and /ws
        await alerts.send_signal_async(s) -> bool  (non-blocking, for asyncio)
        alerts.send_signal(s)             -> bool  (blocking, for threads/tests)
    """

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        *,
        min_interval: float | None = None,
        timeout: float | None = None,
    ) -> None:
        if load_dotenv is not None:
            # Explicit path first (backend/.env), then the process cwd as a
            # fallback. load_dotenv never overrides variables already set.
            if ENV_PATH.exists():
                load_dotenv(ENV_PATH)
            else:
                cwd_env = Path.cwd() / ".env"
                if cwd_env.exists():
                    load_dotenv(cwd_env)

        self.token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (
            chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        ).strip()
        self.min_interval = float(
            min_interval
            if min_interval is not None
            else os.getenv("ALERT_MIN_INTERVAL", DEFAULT_MIN_INTERVAL)
        )
        self.timeout = float(
            timeout if timeout is not None else os.getenv("ALERT_TIMEOUT", DEFAULT_TIMEOUT)
        )

        self.enabled = bool(self.token and self.chat_id)
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None
        self._last_sent_at = 0.0

        if self.enabled:
            log.info("Telegram alerts enabled (chat %s)", self._mask(self.chat_id))
        else:
            log.info("Telegram alerts disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    # ------------------------------------------------------------------ util

    @staticmethod
    def _mask(value: str) -> str:
        """Never log a full chat id or token."""
        if len(value) <= 4:
            return "***"
        return f"{value[:2]}***{value[-2:]}"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "chat_id": self._mask(self.chat_id) if self.chat_id else None,
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
        }

    # --------------------------------------------------------------- sending

    async def send_signal_async(self, signal: dict[str, Any]) -> bool:
        """
        Non-blocking wrapper for the engine loop.

        `asyncio.to_thread` runs the blocking HTTP call in the default executor,
        so a slow Telegram API cannot stall candle dispatch or the websocket.
        """
        if not self.enabled:
            return False
        return await asyncio.to_thread(self.send_signal, signal)

    def send_signal(self, signal: dict[str, Any]) -> bool:
        """Blocking POST to the Bot API. Returns True on a 2xx JSON `ok`."""
        if not self.enabled:
            return False

        now = time.monotonic()
        if now - self._last_sent_at < self.min_interval:
            log.warning("Telegram alert throttled (%.2fs since last send)", now - self._last_sent_at)
            return False
        self._last_sent_at = now

        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": _format_signal(signal),
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            TELEGRAM_API.format(token=self.token),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "trading-copilot/0.3",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if not 200 <= response.status < 300:
                    self._fail(f"HTTP {response.status}")
                    return False
                parsed = json.loads(body or b"{}")
                if not parsed.get("ok", False):
                    self._fail(f"API rejected: {parsed.get('description', 'unknown')}")
                    return False
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # body already consumed / not readable
                pass
            self._fail(f"HTTP {exc.code} {detail}".strip())
            return False
        except urllib.error.URLError as exc:
            self._fail(f"network: {exc.reason}")
            return False
        except (TimeoutError, OSError) as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
            return False
        except (ValueError, KeyError, TypeError) as exc:
            self._fail(f"bad payload: {exc}")
            return False

        self.sent += 1
        self.last_error = None
        log.info(
            "Telegram alert sent: %s %s %s",
            signal.get("mode"), signal.get("direction"), signal.get("pair"),
        )
        return True

    def _fail(self, message: str) -> None:
        self.failed += 1
        self.last_error = message
        log.warning("Telegram alert failed: %s", message)
