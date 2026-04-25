from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..models import InboundEvent, OutboundMessage, SessionMemory
from .registry import SkillSpec

_LOGGER = logging.getLogger("waifu_standalone.cells.tool_registry")


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
    display_mode: str = "inline"

    def format_for_model(self, *, limit: int = 6000) -> str:
        lines = [f"status: {'error' if self.error else 'ok'}"]
        lines.append(f"display_mode: {self.display_mode or 'inline'}")
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
class ToolExposureContext:
    event: InboundEvent
    session: SessionMemory
    latest_message: str = ""
    address: str = ""
    assistant_name: str = ""
    allowed_tool_ids: set[str] | None = None
    skill_cards_by_tool: dict[str, list[SkillSpec]] = field(default_factory=dict)
    skill_manifests: list[SkillSpec] = field(default_factory=list)


ToolExposurePolicy = Callable[[ToolExposureContext], bool]


@dataclass(slots=True)
class ToolCallTurn:
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    result_text: str = ""
    error: str = ""


@dataclass(slots=True)
class ToolOrchestrationResult:
    text: str = ""
    images: list[str] = field(default_factory=list)
    turns: list[ToolCallTurn] = field(default_factory=list)


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
    risk_level: str = "safe"
    exposure_policy: ToolExposurePolicy | None = None
    skill_source: str = ""

    def as_dict(self, *, aliases: list[str] | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "model_callable": self.model_callable,
            "risk_level": self.risk_level,
            "skill_source": self.skill_source,
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
        risk_level: str = "safe",
        exposure_policy: ToolExposurePolicy | None = None,
        skill_source: str = "",
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
            risk_level=_normalize_risk_level(risk_level),
            exposure_policy=exposure_policy,
            skill_source=str(skill_source or "").strip(),
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
        skill = _manifest_for_tool_call(tool_id, invocation.active_skills)
        if skill is not None:
            from .executor import SkillExecutor

            return SkillExecutor(self).run(skill, invocation)
        spec = self.get(tool_id)
        if spec is None:
            return ToolExecutionResult(error=f"tool is not registered: {tool_id}")
        if not spec.model_callable or spec.model_handler is None:
            return ToolExecutionResult(error=f"tool is not available for model invocation: {spec.tool_id}")
        return spec.model_handler(invocation)

    async def aexecute_model(self, tool_id: str, invocation: ToolInvocation) -> ToolExecutionResult:
        skill = _manifest_for_tool_call(tool_id, invocation.active_skills)
        if skill is not None:
            from .executor import SkillExecutor

            return await SkillExecutor(self).arun(skill, invocation)
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

    def model_schemas(self, context: ToolExposureContext | None = None) -> list[dict[str, object]]:
        items = []
        skill_bound_tool_ids: set[str] = set()
        if context is not None and context.skill_manifests:
            for skill in sorted(context.skill_manifests, key=lambda item: (-item.priority, item.name.lower())):
                if not skill.model_visible or not skill.is_tool_handler:
                    continue
                if context.allowed_tool_ids is not None and skill.handler_target not in context.allowed_tool_ids:
                    continue
                tool = self.get(skill.handler_target)
                if tool is None or not tool.model_callable:
                    continue
                if tool.exposure_policy is not None and not tool.exposure_policy(context):
                    continue
                skill_bound_tool_ids.add(tool.tool_id)
                description = _description_with_skill_manifest(tool.description or tool.name, skill)
                items.append(
                    {
                        "name": skill.skill_id,
                        "description": description,
                        "parameters": dict(skill.input_schema or tool.parameters or _default_parameters_schema()),
                    }
                )
        for tool in sorted(self._tools.values(), key=lambda item: item.name.lower()):
            if not tool.model_callable:
                continue
            if context is not None and tool.tool_id in context.skill_cards_by_tool:
                continue
            if tool.tool_id in skill_bound_tool_ids:
                continue
            if context is not None:
                if context.allowed_tool_ids is not None and tool.tool_id not in context.allowed_tool_ids:
                    continue
                if tool.exposure_policy is not None and not tool.exposure_policy(context):
                    continue
            description = tool.description or tool.name
            skill_cards = (context.skill_cards_by_tool.get(tool.tool_id, []) if context is not None else [])
            if skill_cards:
                description = _description_with_skill_cards(description, skill_cards)
            items.append(
                {
                    "name": tool.tool_id,
                    "description": description,
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


def _normalize_risk_level(value: str) -> str:
    normalized = str(value or "safe").strip().lower()
    if normalized in {"safe", "filesystem", "command", "mutating"}:
        return normalized
    return "safe"


def _description_with_skill_cards(description: str, skill_cards: list[SkillSpec]) -> str:
    lines = [str(description or "").strip()]
    lines.append("可用 skill 能力卡：")
    for skill in skill_cards[:4]:
        summary = str(skill.description or skill.name or skill.skill_id).strip()
        content = " ".join(str(skill.content or "").split()).strip()
        if content:
            summary = f"{summary} 约束：{content[:240]}"
        lines.append(f"- {skill.name} ({skill.skill_id}): {summary}")
    return "\n".join(line for line in lines if line).strip()


def _description_with_skill_manifest(description: str, skill: SkillSpec) -> str:
    lines = [str(description or "").strip()]
    lines.append(f"Skill: {skill.name} ({skill.skill_id})")
    if skill.description:
        lines.append(skill.description)
    if skill.content:
        lines.append("执行策略：" + " ".join(str(skill.content).split())[:500])
    if skill.keywords:
        lines.append("相关表达：" + " / ".join(skill.keywords[:8]))
    return "\n".join(line for line in lines if line).strip()


def _manifest_for_tool_call(tool_id: str, active_skills: list[SkillSpec]) -> SkillSpec | None:
    target = str(tool_id or "").strip()
    if not target:
        return None
    for skill in active_skills:
        if skill.skill_id == target:
            return skill
    return None
