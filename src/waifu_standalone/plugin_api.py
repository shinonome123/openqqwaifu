from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points as discover_entry_points
from typing import Iterable

from .cells.tool_registry import ToolRegistry
from .config import AppConfig
from .observability import MetricsRegistry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PluginContext:
    app_config: AppConfig
    tool_registry: ToolRegistry
    logger: logging.Logger
    metrics: MetricsRegistry


def load_tool_plugins(
    ctx: PluginContext,
    *,
    disabled: frozenset[str] | set[str] = frozenset(),
    entry_points: Iterable[EntryPoint] | None = None,
) -> list[str]:
    entries = (
        list(entry_points)
        if entry_points is not None
        else list(discover_entry_points(group="openqqwaifu.tools"))
    )
    disabled_names = {str(name or "").strip() for name in disabled if str(name or "").strip()}
    loaded: list[str] = []
    for entry in entries:
        name = str(entry.name or "").strip()
        if name in disabled_names:
            continue
        try:
            entry.load()(ctx)
            loaded.append(name)
        except Exception:
            _LOGGER.exception("plugin %s failed to load", name or "<unnamed>")
    return loaded
