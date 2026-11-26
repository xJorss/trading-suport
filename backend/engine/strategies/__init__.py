"""Strategy registry. New strategies register here, the manager picks them up."""

from .base import BaseStrategy
from .safe import SafeStrategy
from .aggressive import AggressiveStrategy

__all__ = ["BaseStrategy", "SafeStrategy", "AggressiveStrategy"]
