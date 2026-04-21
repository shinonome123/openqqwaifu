from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..models import InboundEvent, OutboundMessage, SessionMemory
from .skill_registry import SkillSpec

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolInvocation:
    tool_id: str
    raw_args: str
    event: InboundEvent
    session: SessionMemory
    skill: SkillSpec | None = None
    address: str = ""
    assistant_name: str = ""
    active_skills: list[SkillSpec] = field(default_factory=list)
    arguments: dict[str, object] = field(default_factory=dict)

    def argument(self, name: str, default: Any = None) -> Any:
        return self.arguments.get(str(name or "").strip(), default)


@dataclass(slots=True)
class ToolExecutionResult:
    text: str = ""
    images: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    error: str = ""

    def format_for_model(self, *, limit: int = 6000) -> str:
        lines = [f"status: {'error' if self.error else 'ok'}"]
        if self.error:
            lines.append(f"error: {self.error}")
        if self.text:
            lines.append("text:\n" + str(self.text).strip())
        if self.images:
            lines.append("images:\n" + "\n".join(f"- {item}" for item in self.images))
        if self.metadata:
            lines.append(
                "metadata:\n"
                + json.dumps(
                    self.metadata,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        content = "\n\n".join(part for part in lines if str(part).strip()).strip()
        if len(content) <= max(1, int(limit)):
            return content
        clipped = content[: max(1, int(limit)) - 1].rstrip()
        return clipped + "…"


ToolHandler = Callable[[ToolInvocation], OutboundMessage | None]
AsyncToolHandler = Callable[[ToolInvocation], Awaitable[OutboundMessage | None]]
ModelToolHandler = Callable[[ToolInvocation], ToolExecutionResult]
AsyncModelToolHandler = Callable[[ToolInvocation], Awaitable[ToolExecutionResult]]


@dataclass(slots=True)
class ToolSpec:
    tool_id: str
    name: str
    description: str
    handler: ToolHandler
    async_handler: AsyncToolHandler | None = None
    parameters: dict[str, object] = field(default_factory=dict)
    model_handler: ModelToolHandler | None = None
    async_model_handler: AsyncModelToolHandler | None = None
    model_callable: bool = False

    def as_dict(self, *, aliases: list[str] | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "model_callable": self.model_callable,
        }
        if aliases:
            payload["aliases"] = list(aliases)
        return payload


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._aliases: dict[str, str] = {}

    def register(
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
    ) -> ToolSpec:
        normalized = str(tool_id or "").strip().lower()
        if not normalized:
            raise ValueError("tool_id is required")
        resolved_model_callable = bool(
            (model_callable if model_callable is not None else (model_handler is not None or async_model_handler is not None))
        )
        if resolved_model_callable and model_handler is None and async_model_handler is None:
            raise ValueError("model callable tools require a model_handler or async_model_handler")
        existing = self._tools.get(normalized)
        if existing is not None:
            _LOGGER.warning("tool %s is already registered; skipping duplicate registration", normalized)
            return existing
        spec = ToolSpec(
            tool_id=normalized,
            name=str(name or normalized).strip() or normalized,
            description=str(description or "").strip(),
            handler=handler,
            async_handler=async_handler,
            parameters=dict(parameters or _default_parameters_schema()),
            model_handler=model_handler,
            async_model_handler=async_model_handler,
            model_callable=resolved_model_callable,
        )
        self._tools[normalized] = spec
        return spec

    def register_alias(self, alias: str, target_tool_id: str) -> ToolSpec | None:
        normalized_alias = str(alias or "").strip().lower()
        if not normalized_alias:
            raise ValueError("alias is required")
        resolved_target = self._resolve_tool_id(target_tool_id)
        if not resolved_target:
            raise ValueError(f"target tool is not registered: {target_tool_id}")
        if normalized_alias == resolved_target:
            return self._tools.get(resolved_target)
        if normalized_alias in self._tools:
            _LOGGER.warning(
                "tool alias %s conflicts with a registered tool; skipping alias registration",
                normalized_alias,
            )
            return self._tools.get(normalized_alias)
        current = self._aliases.get(normalized_alias)
        if current and current != resolved_target:
            _LOGGER.warning(
                "tool alias %s is already mapped to %s; skipping remap to %s",
                normalized_alias,
                current,
                resolved_target,
            )
            return self._tools.get(current)
        self._aliases[normalized_alias] = resolved_target
        return self._tools.get(resolved_target)

    def _resolve_tool_id(self, tool_id: str) -> str:
        normalized = str(tool_id or "").strip().lower()
        seen: set[str] = set()
        while normalized and normalized not in seen:
            seen.add(normalized)
            if normalized in self._tools:
                return normalized
            normalized = self._aliases.get(normalized, "")
        return ""

    def has(self, tool_id: str) -> bool:
        return bool(self._resolve_tool_id(tool_id))

    def get(self, tool_id: str) -> ToolSpec | None:
        resolved = self._resolve_tool_id(tool_id)
        if not resolved:
            return None
        return self._tools.get(resolved)

    def execute(self, tool_id: str, invocation: ToolInvocation) -> OutboundMessage | None:
        spec = self.get(tool_id)
        if spec is None:
            return None
        return spec.handler(invocation)

    async def aexecute(self, tool_id: str, invocation: ToolInvocation) -> OutboundMessage | None:
        spec = self.get(tool_id)
        if spec is None:
            return None
        if spec.async_handler is not None:
            return await spec.async_handler(invocation)
        return await asyncio.to_thread(spec.handler, invocation)

    def execute_model(self, tool_id: str, invocation: ToolInvocation) -> ToolExecutionResult:
        spec = self.get(tool_id)
        if spec is None:
            return ToolExecutionResult(error=f"tool is not registered: {tool_id}")
        if not spec.model_callable or spec.model_handler is None:
            return ToolExecutionResult(error=f"tool is not available for model invocation: {spec.tool_id}")
        return spec.model_handler(invocation)

    async def aexecute_model(self, tool_id: str, invocation: ToolInvocation) -> ToolExecutionResult:
        spec = self.get(tool_id)
        if spec is None:
            return ToolExecutionResult(error=f"tool is not registered: {tool_id}")
        if not spec.model_callable:
            return ToolExecutionResult(error=f"tool is not available for model invocation: {spec.tool_id}")
        if spec.async_model_handler is not None:
            return await spec.async_model_handler(invocation)
        if spec.model_handler is None:
            return ToolExecutionResult(error=f"tool is not available for model invocation: {spec.tool_id}")
        return await asyncio.to_thread(spec.model_handler, invocation)

    def model_schemas(self) -> list[dict[str, object]]:
        items = []
        for tool in sorted(self._tools.values(), key=lambda item: item.name.lower()):
            if not tool.model_callable:
                continue
            items.append(
                {
                    "name": tool.tool_id,
                    "description": tool.description or tool.name,
                    "parameters": dict(tool.parameters or _default_parameters_schema()),
                }
            )
        return items

    def describe(self) -> dict[str, object]:
        aliases_by_target: dict[str, list[str]] = {}
        for alias, target in sorted(self._aliases.items()):
            aliases_by_target.setdefault(target, []).append(alias)
        items = [
            tool.as_dict(aliases=aliases_by_target.get(tool.tool_id, []))
            for tool in sorted(self._tools.values(), key=lambda item: item.name.lower())
        ]
        return {
            "count": len(items),
            "alias_count": len(self._aliases),
            "model_callable_count": sum(1 for item in items if item.get("model_callable")),
            "items": items,
        }


def _default_parameters_schema() -> dict[str, object]:
    return {"type": "object", "properties": {}}
