from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

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


ToolHandler = Callable[[ToolInvocation], OutboundMessage | None]
AsyncToolHandler = Callable[[ToolInvocation], Awaitable[OutboundMessage | None]]


@dataclass(slots=True)
class ToolSpec:
    tool_id: str
    name: str
    description: str
    handler: ToolHandler
    async_handler: AsyncToolHandler | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.tool_id,
            "name": self.name,
            "description": self.description,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        tool_id: str,
        *,
        name: str,
        description: str,
        handler: ToolHandler,
        async_handler: AsyncToolHandler | None = None,
    ) -> ToolSpec:
        normalized = str(tool_id or "").strip().lower()
        if not normalized:
            raise ValueError("tool_id is required")
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
        )
        self._tools[normalized] = spec
        return spec

    def has(self, tool_id: str) -> bool:
        return str(tool_id or "").strip().lower() in self._tools

    def get(self, tool_id: str) -> ToolSpec | None:
        return self._tools.get(str(tool_id or "").strip().lower())

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

    def describe(self) -> dict[str, object]:
        items = [tool.as_dict() for tool in sorted(self._tools.values(), key=lambda item: item.name.lower())]
        return {
            "count": len(items),
            "items": items,
        }
