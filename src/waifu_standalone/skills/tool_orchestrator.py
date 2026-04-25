from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import InboundEvent, SessionMemory
from .registry import SkillSpec
from .tool_registry import ToolExposureContext

if TYPE_CHECKING:
    from ..app import WaifuService


_NAMING_TOOL_IDS = {
    "set-preferred-name",
    "set-assistant-alias",
    "reject-third-party-naming",
    "clarify-naming-intent",
}


class ToolCallingOrchestrator:
    """Build the model-visible tool and skill surface for one reply turn."""

    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    def prompt_skill_cards(self, latest_message: str = "") -> list[SkillSpec]:
        svc = self._service
        if not svc.config.skills_enabled:
            return []
        return svc.skills.candidate_manifests(latest_message)

    def exposure_context(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        address: str,
        assistant_name: str,
    ) -> ToolExposureContext:
        svc = self._service
        return ToolExposureContext(
            event=event,
            session=session,
            latest_message=latest_message,
            address=address,
            assistant_name=assistant_name,
            allowed_tool_ids=self._allowed_tool_ids(event),
            skill_cards_by_tool=self._skill_cards_by_tool(),
            skill_manifests=svc.skills.candidate_manifests(latest_message),
        )

    def model_schemas(self, context: ToolExposureContext) -> list[dict[str, object]]:
        return self._service.tools.model_schemas(context)

    def is_ready(self, context: ToolExposureContext) -> bool:
        svc = self._service
        return bool(
            svc.generator.llm_ready
            and getattr(svc.generator._llm_client, "supports_tool_calling", False)
            and self.model_schemas(context)
        )

    def unavailable_reply(self, address: str) -> str:
        prefix = str(address or "").strip()
        if prefix:
            return f"{prefix}，现在 AI 工具编排模型还没连上，我不能可靠判断该调用哪个工具。"
        return "现在 AI 工具编排模型还没连上，我不能可靠判断该调用哪个工具。"

    def _skill_cards_by_tool(self) -> dict[str, list[SkillSpec]]:
        svc = self._service
        if not svc.config.skills_enabled:
            return {}
        grouped: dict[str, list[SkillSpec]] = {}
        for skill in svc.skills.list_skills():
            if not (skill.enabled and skill.eligible and not skill.validation_errors and skill.is_tool_handler):
                continue
            grouped.setdefault(skill.handler_target, []).append(skill)
        return grouped

    def _allowed_tool_ids(self, event: InboundEvent) -> set[str]:
        svc = self._service
        all_model_tools = {
            str(item.get("id", "") or "").strip().lower()
            for item in svc.tools.describe().get("items", [])
            if bool(item.get("model_callable"))
        }
        all_model_tools.discard("")
        allowed = set(all_model_tools)

        if event.launcher_type == "group":
            bot_account_id = str(svc.config.bot_account_id or "").strip()
            if not (bot_account_id and event.has_bot_mention(bot_account_id)):
                allowed.difference_update(_NAMING_TOOL_IDS)
        return allowed
