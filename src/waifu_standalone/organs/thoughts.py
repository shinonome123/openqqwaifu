from __future__ import annotations

from typing import TYPE_CHECKING

from ..cells.generator import Generator
from ..config import AppConfig
from ..models import InboundEvent, SessionMemory

if TYPE_CHECKING:
    from ..cells.skill_registry import SkillSpec


class Thoughts:
    def __init__(self, config: AppConfig, generator: Generator):
        self.config = config
        self.generator = generator

    def analyze(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        conversation_view: str,
        memory_hints: list[str],
        speaker_notes: list[str],
        active_skills: list[SkillSpec] | None = None,
        address: str = "",
        allow_fallback: bool = True,
    ) -> str:
        if not self.config.thinking_mode:
            return ""
        if not self.config.conversation_analysis and not memory_hints and not speaker_notes:
            return ""
        return self.generator.generate_analysis(
            event,
            session,
            assistant_name=assistant_name,
            address_override=address,
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            active_skills=active_skills or [],
            allow_fallback=allow_fallback,
        )

    async def aanalyze(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        conversation_view: str,
        memory_hints: list[str],
        speaker_notes: list[str],
        active_skills: list[SkillSpec] | None = None,
        address: str = "",
        allow_fallback: bool = True,
    ) -> str:
        if not self.config.thinking_mode:
            return ""
        if not self.config.conversation_analysis and not memory_hints and not speaker_notes:
            return ""
        return await self.generator.agenerate_analysis(
            event,
            session,
            assistant_name=assistant_name,
            address_override=address,
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            active_skills=active_skills or [],
            allow_fallback=allow_fallback,
        )
