"""
Market configuration — which pairs and timeframes the engine runs on.

Source of truth is `backend/pairs.json`:

    {"pairs": ["BTCUSDT", "ETHUSDT"], "timeframes": ["5m", "1h"]}

Everything here is fail-soft. A missing file, a truncated file, a JSON array
where an object was expected, a timeframe no strategy is bound to — none of
those may stop the engine. Each one logs what was wrong and falls back to the
built-in defaults, because a signal engine that refuses to boot over a typo in
a config file is worse than one that boots on BTC/ETH and says so.

Normalisation happens on read, not on write: symbols are upper-cased and
de-duplicated in declaration order, so `" btcusdt"` and `"BTCUSDT"` are the same
stream and a repeated pair does not open a duplicate subscription.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Anchored to backend/ so the file is found regardless of the working directory
# uvicorn was launched from.
CONFIG_FILE = Path(__file__).resolve().parents[1] / "pairs.json"

DEFAULT_PAIRS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
DEFAULT_TIMEFRAMES: tuple[str, ...] = ("5m", "1h")

# Only timeframes with a strategy bound to them are usable. The manager builds
# one strategy per entry here; anything else in pairs.json is dropped with a
# warning instead of reaching a KeyError mid-loop.
SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("5m", "1h")

# Binance USDⓈ-M symbols are upper-case alphanumeric. The bound is generous
# enough for long symbolic pairs and tight enough to reject junk.
SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,24}$")

# Ceiling on subscriptions: the combined stream URL is one request line, and
# 20 pairs x 2 timeframes is already 40 streams on a single socket.
MAX_PAIRS = 20


@dataclass(frozen=True, slots=True)
class MarketConfig:
    """Resolved market configuration plus where it came from."""

    pairs: tuple[str, ...]
    timeframes: tuple[str, ...]
    source: str  # "file" | "defaults"
    path: Path

    def as_dict(self) -> dict[str, Any]:
        """Shape published by /api/health so the UI can explain the config."""
        return {
            "pairs": list(self.pairs),
            "timeframes": list(self.timeframes),
            "source": self.source,
            "path": str(self.path),
        }


def _clean_pairs(raw: Iterable[Any]) -> tuple[str, ...]:
    """Upper-case, validate, de-duplicate — preserving declaration order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            log.warning("pairs.json: ignoring non-string pair %r", item)
            continue
        symbol = item.strip().upper()
        if not SYMBOL_RE.match(symbol):
            log.warning("pairs.json: ignoring invalid symbol %r", item)
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append(symbol)
        if len(cleaned) >= MAX_PAIRS:
            log.warning("pairs.json: capped at %d pairs, rest ignored", MAX_PAIRS)
            break
    return tuple(cleaned)


def _clean_timeframes(raw: Iterable[Any]) -> tuple[str, ...]:
    """Keep only timeframes a strategy exists for."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            log.warning("pairs.json: ignoring non-string timeframe %r", item)
            continue
        timeframe = item.strip().lower()
        if timeframe not in SUPPORTED_TIMEFRAMES:
            log.warning(
                "pairs.json: timeframe %r has no strategy (supported: %s)",
                item,
                ", ".join(SUPPORTED_TIMEFRAMES),
            )
            continue
        if timeframe in seen:
            continue
        seen.add(timeframe)
        cleaned.append(timeframe)
    return tuple(cleaned)


def _defaults(path: Path, reason: str) -> MarketConfig:
    log.warning("%s — using defaults (%s / %s)",
                reason, ", ".join(DEFAULT_PAIRS), ", ".join(DEFAULT_TIMEFRAMES))
    return MarketConfig(
        pairs=DEFAULT_PAIRS,
        timeframes=DEFAULT_TIMEFRAMES,
        source="defaults",
        path=path,
    )


def load_market_config(path: Path | None = None) -> MarketConfig:
    """
    Read pairs.json and return a validated config.

    Never raises: every failure path returns the defaults and logs why. Callers
    can therefore treat the result as always usable.
    """
    config_path = Path(path) if path is not None else CONFIG_FILE

    try:
        # utf-8-sig, not utf-8: Notepad and PowerShell 5.1 write a BOM, and
        # json.loads chokes on it. The codec also reads plain UTF-8 unchanged.
        text = config_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return _defaults(config_path, f"No {config_path.name} at {config_path}")
    except OSError as exc:
        return _defaults(config_path, f"Cannot read {config_path.name}: {exc}")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return _defaults(config_path, f"{config_path.name} is not valid JSON: {exc}")

    if not isinstance(payload, dict):
        return _defaults(config_path, f"{config_path.name} must contain a JSON object")

    raw_pairs = payload.get("pairs")
    raw_timeframes = payload.get("timeframes")

    pairs = _clean_pairs(raw_pairs) if isinstance(raw_pairs, list) else ()
    timeframes = _clean_timeframes(raw_timeframes) if isinstance(raw_timeframes, list) else ()

    if not isinstance(raw_pairs, list):
        log.warning("pairs.json: \"pairs\" missing or not a list — using defaults")
    if not isinstance(raw_timeframes, list):
        log.warning("pairs.json: \"timeframes\" missing or not a list — using defaults")

    if not pairs:
        pairs = DEFAULT_PAIRS
        log.warning("pairs.json: no usable pairs — falling back to %s", ", ".join(pairs))
    if not timeframes:
        timeframes = DEFAULT_TIMEFRAMES
        log.warning(
            "pairs.json: no usable timeframes — falling back to %s", ", ".join(timeframes)
        )

    log.info(
        "Market config loaded from %s: pairs=%s timeframes=%s",
        config_path.name,
        ", ".join(pairs),
        ", ".join(timeframes),
    )
    return MarketConfig(pairs=pairs, timeframes=timeframes, source="file", path=config_path)
