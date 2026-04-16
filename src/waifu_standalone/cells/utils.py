from __future__ import annotations

from collections.abc import Mapping


MASK_SENTINEL = "..."


def mask_key(key: str) -> str:
    """Return a masked version of a sensitive value (e.g. API key)."""
    if not key or len(key) < 8:
        return "***" if key else ""
    return key[:4] + MASK_SENTINEL + key[-4:]


def safe_int(payload: Mapping[str, object], key: str, default: int) -> int:
    """Return ``int(payload[key])`` if *key* is present, otherwise *default*."""
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def safe_float(payload: Mapping[str, object], key: str, default: float) -> float:
    """Return ``float(payload[key])`` if *key* is present, otherwise *default*."""
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
