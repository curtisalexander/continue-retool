"""Bounded environment configuration helpers."""

from __future__ import annotations

import math
import os


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer setting, falling back and clamping unsafe extremes."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)

def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a finite float setting, falling back and clamping extremes."""
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, minimum), maximum)
