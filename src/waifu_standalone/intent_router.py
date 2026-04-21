from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .cells.skill_registry import SkillSpec

if TYPE_CHECKING:
    from .app import WaifuService
    from .models import InboundEvent, SessionMemory


@dataclass(slots=True)
class IntentRouteResult:
    mode: str = "none"
    active_skill_ids: list[str] = field(default_factory=list)
    dispatch_skill_id: str = ""
    raw_args: str = ""
    clarification_text: str = ""


class IntentRouter:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    def route(
        self,
        event: "InboundEvent",
        session: "SessionMemory",
        *,
        latest_message: str,
        assistant_name: str,
        address: str,
    ) -> IntentRouteResult:
        if not self._routing_ready():
            return IntentRouteResult()
        candidates = self._candidate_skills()
        if not candidates:
            return IntentRouteResult()
        payload = self._service.generator.resolve_skill_intent(
            event,
            session,
            assistant_name=assistant_name,
            address=address,
            latest_message=latest_message,
            candidate_skills=[self._candidate_payload(skill) for skill in candidates],
        )
        return self._normalize_route_result(payload, candidates)

    async def aroute(
        self,
        event: "InboundEvent",
        session: "SessionMemory",
        *,
        latest_message: str,
        assistant_name: str,
        address: str,
    ) -> IntentRouteResult:
        if not self._routing_ready():
            return IntentRouteResult()
        candidates = self._candidate_skills()
        if not candidates:
            return IntentRouteResult()
        payload = await self._service.generator.aresolve_skill_intent(
            event,
            session,
            assistant_name=assistant_name,
            address=address,
            latest_message=latest_message,
            candidate_skills=[self._candidate_payload(skill) for skill in candidates],
        )
        return self._normalize_route_result(payload, candidates)

    def _routing_ready(self) -> bool:
        return bool(self._service.config.skills_enabled and self._service.generator.llm_ready)

    def _candidate_skills(self) -> list[SkillSpec]:
        return [
            skill
            for skill in self._service.skills.list_skills()
            if skill.enabled and skill.eligible and skill.user_invocable
        ]

    @staticmethod
    def _candidate_payload(skill: SkillSpec) -> dict[str, object]:
        return {
            "id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "triggers": list(skill.triggers),
            "aliases": list(skill.aliases),
            "dispatchable": skill.dispatches_tool,
            "prompt_visible": skill.prompt_visible,
            "command_tool": skill.command_tool,
            "source_kind": skill.source_kind,
        }

    def _normalize_route_result(
        self,
        payload: dict[str, Any],
        candidates: list[SkillSpec],
    ) -> IntentRouteResult:
        by_id = {skill.skill_id: skill for skill in candidates}
        mode = str(payload.get("mode", "") or "none").strip().lower()
        if mode not in {"activate_only", "dispatch", "clarify", "none"}:
            mode = "none"

        raw_active = payload.get("active_skill_ids", [])
        active_skill_ids = (
            [str(item).strip() for item in raw_active if str(item).strip()]
            if isinstance(raw_active, list)
            else []
        )
        active_skill_ids = [
            skill_id
            for skill_id in active_skill_ids
            if skill_id in by_id and by_id[skill_id].prompt_visible
        ]
        active_skill_ids = active_skill_ids[: max(1, int(self._service.config.max_active_skills))]

        dispatch_skill_id = str(payload.get("dispatch_skill_id", "") or "").strip()
        if dispatch_skill_id and dispatch_skill_id not in by_id:
            dispatch_skill_id = ""
        if dispatch_skill_id and dispatch_skill_id in by_id and not by_id[dispatch_skill_id].dispatches_tool:
            dispatch_skill_id = ""

        raw_args = str(payload.get("raw_args", "") or "").strip()
        clarification_text = str(payload.get("clarification_text", "") or "").strip()

        if mode == "dispatch" and not dispatch_skill_id:
            mode = "none"
        if mode == "activate_only" and not active_skill_ids:
            mode = "none"
        if mode == "clarify" and not clarification_text:
            clarification_text = "你是想让我直接跑一个技能，还是先正常聊这个话题？"

        return IntentRouteResult(
            mode=mode,
            active_skill_ids=active_skill_ids,
            dispatch_skill_id=dispatch_skill_id,
            raw_args=raw_args,
            clarification_text=clarification_text,
        )
