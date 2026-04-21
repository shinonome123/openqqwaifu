from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points as discover_entry_points
from typing import Any, Iterable

from .cells.skill_registry import SkillRegistry, SkillSpec, build_skill_markdown_template
from .cells.tool_registry import (
    AsyncModelToolHandler,
    AsyncToolHandler,
    ModelToolHandler,
    ToolHandler,
    ToolRegistry,
    ToolSpec,
)
from .config import AppConfig
from .observability import MetricsRegistry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PluginContext:
    app_config: AppConfig
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    logger: logging.Logger
    metrics: MetricsRegistry

    def register_skill_tool(
        self,
        tool_id: str,
        *,
        name: str,
        description: str,
        handler: ToolHandler,
        async_handler: AsyncToolHandler | None = None,
        parameters: dict[str, object] | None = None,
        model_handler: ModelToolHandler | None = None,
        async_model_handler: AsyncModelToolHandler | None = None,
        model_callable: bool | None = None,
        skill_id: str = "",
        skill_name: str = "",
        skill_description: str = "",
        triggers: list[str] | None = None,
        aliases: list[str] | None = None,
        mode: str = "prefix",
        priority: int = 0,
        body: str = "",
        user_invocable: bool = True,
        disable_model_invocation: bool = True,
        command_arg_mode: str = "raw",
        homepage: str = "",
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
        source_name: str = "",
    ) -> "SkillToolRegistration":
        tool = self.tool_registry.register(
            tool_id,
            name=name,
            description=description,
            handler=handler,
            async_handler=async_handler,
            parameters=parameters,
            model_handler=model_handler,
            async_model_handler=async_model_handler,
            model_callable=model_callable,
        )
        resolved_skill_id = str(skill_id or tool.tool_id).strip() or tool.tool_id
        resolved_skill_name = str(skill_name or name or resolved_skill_id).strip() or resolved_skill_id
        markdown = build_skill_markdown_template(
            skill_id=resolved_skill_id,
            name=resolved_skill_name,
            description=str(skill_description or description or "").strip(),
            triggers=list(triggers or []),
            aliases=list(aliases or []),
            mode=mode,
            priority=priority,
            user_invocable=user_invocable,
            disable_model_invocation=disable_model_invocation,
            command_dispatch="tool",
            command_tool=tool.tool_id,
            command_arg_mode=command_arg_mode,
            homepage=homepage,
            metadata=dict(metadata or {}),
            body=(body or f"Use the `{tool.tool_id}` tool when this skill is selected.").strip(),
        )
        skill = self.skill_registry.register_runtime_skill(
            markdown,
            source_name=source_name or f"plugin://{tool.tool_id}/{resolved_skill_id}",
            enabled=enabled,
        )
        return SkillToolRegistration(tool=tool, skill=skill)


@dataclass(frozen=True, slots=True)
class SkillToolRegistration:
    tool: ToolSpec
    skill: SkillSpec


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
