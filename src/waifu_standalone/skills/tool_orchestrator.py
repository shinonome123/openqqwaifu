from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import InboundEvent, SessionMemory
from .registry import SkillSpec
from .tool_intent import NAMING_TOOL_IDS, relevant_tool_ids
from .tool_registry import ToolExposureContext, ToolInvocation, ToolSpec

if TYPE_CHECKING:
    from ..app import WaifuService


_NAMING_TOOL_IDS = {
    "set-preferred-name",
    "set-assistant-alias",
    "reject-third-party-naming",
    "clarify-naming-intent",
}

# Risk levels that touch the host filesystem, run commands, or reach the network.
# Tools at these levels are only offered to users on the authorization allowlist.
_PRIVILEGED_RISK_LEVELS = {"filesystem", "command", "network"}


class ToolCallingOrchestrator:
    """Build the model-visible tool and skill surface for one reply turn."""

    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        service.tools.execution_policy = self._denial_reason

    def _denial_reason(self, tool: ToolSpec, invocation: ToolInvocation) -> str:
        svc = self._service
        tool_id = tool.tool_id
        if not svc.config.skills_enabled:
            return "skills are disabled"
        skills = svc.skills.list_skills()
        requires_authorization = False
        if invocation.skill is not None:
            current = next((item for item in skills if item.skill_id == invocation.skill.skill_id), None)
            if (current is None or not current.enabled or not current.eligible or current.validation_errors
                    or not current.is_tool_handler or svc.tools.get(current.handler_target) is not tool):
                return f"skill is disabled or unavailable: {invocation.skill.skill_id}"
            requires_authorization = current.policy.requires_authorization
        else:
            bound = [item for item in skills if item.is_tool_handler and svc.tools.get(item.handler_target) is tool]
            if bound and not any(item.enabled and item.eligible and not item.validation_errors for item in bound):
                return f"skill is disabled or unavailable: {tool_id}"
        if tool_id == "image" and not svc.generator.image_ready:
            return "image generation is disabled or unavailable"
        if tool_id in {"search", "search-links"} and not svc.config.search_enabled:
            return "search is disabled"
        policy = svc.config.tool_policy
        if (tool_id in {"read-file", "list-files", "write-file", "send-file", "exec-command"}
                or tool.risk_level == "command") and not policy.enabled:
            return f"{tool_id} is disabled by tool policy"
        if tool_id == "write-file" and not policy.write_enabled:
            return "write-file is disabled by tool policy"
        if (tool_id == "exec-command" or tool.risk_level == "command") and not policy.exec_enabled:
            return f"{tool_id} is disabled by tool policy"
        if (tool_id in {"read-file", "list-files", "write-file", "send-file", "exec-command"}
                or tool.risk_level in _PRIVILEGED_RISK_LEVELS or requires_authorization):
            if not self._is_tool_authorized(invocation.event):
                return f"permission denied: {tool_id} requires an authorized caller"
        if invocation.event.launcher_type == "group" and tool_id in _NAMING_TOOL_IDS:
            bot_id = str(svc.config.bot_account_id or "").strip()
            if not (bot_id and invocation.event.has_bot_mention(bot_id)):
                return "naming tools require a bot mention in group conversations"
        return ""

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
        all_tools: bool = False,
        spontaneous: bool = False,
    ) -> ToolExposureContext:
        """The tools the model may call this turn; ``all_tools`` for an explicit "/skill" request.

        Someone talking to her also gets ``always_offered_tool_ids`` whatever the wording (and in
        a private chat ``private_offered_tool_ids``): the model judges from the conversation
        whether to look something up or draw. Joining in unasked
        (``spontaneous``) she only gets what the wording points at.
        """
        svc = self._service
        allowed = self._allowed_tool_ids(event)
        manifests = svc.skills.exposed_manifests(latest_message) if svc.config.skills_enabled else []
        if svc.config.tools_on_demand:
            # Only tools the wording points at, plus skills whose own keywords match.
            wanted = set(allowed) if all_tools else relevant_tool_ids(latest_message, has_images=event.image_count > 0)
            if not spontaneous:
                wanted.update(str(tool_id) for tool_id in svc.config.always_offered_tool_ids)
                if event.launcher_type == "person":
                    wanted.update(str(tool_id) for tool_id in svc.config.private_offered_tool_ids)
            if self._just_asked_for_name(event, session):
                # A bare "luna" right after the bot asked for a name is an answer.
                wanted.update(NAMING_TOOL_IDS)
            # The skills behind the wanted tools come along even when no skill keyword
            # ranked them: "看看文件夹里有什么" names no file skill, yet wants list-files.
            listed = {skill.skill_id for skill in manifests}
            for skill in svc.skills.list_skills() if svc.config.skills_enabled else []:
                if (
                    skill.skill_id not in listed
                    and skill.is_tool_handler
                    and skill.model_visible
                    and not skill.validation_errors
                    and skill.handler_target in wanted & allowed
                ):
                    manifests.append(skill)
                    listed.add(skill.skill_id)
            manifests = [
                skill
                for skill in manifests
                if skill.handler_target in wanted or skill.keyword_score(latest_message) > 0
            ]
            wanted.update(skill.handler_target for skill in manifests)
            allowed = allowed & wanted
        return ToolExposureContext(
            event=event,
            session=session,
            latest_message=latest_message,
            address=address,
            assistant_name=assistant_name,
            allowed_tool_ids=allowed,
            skill_cards_by_tool=self._skill_cards_by_tool(),
            skill_manifests=manifests,
            explicit=all_tools,
        )

    def _just_asked_for_name(self, event: InboundEvent, session: SessionMemory) -> bool:
        """The bot asks for a name once, with its first reply; this is the answer to it.

        The member stays ``asked_once`` until they give a name, so the status alone
        would offer the naming tools for the rest of the conversation.
        """
        member = self._service.member_repository.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
        )
        if str((member or {}).get("onboarding_status", "") or "") != "asked_once":
            return False
        return sum(1 for line in session.history if str(line).startswith("assistant: ")) == 1

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
        if not svc.config.skills_enabled:
            return set()
        items = svc.tools.describe().get("items", [])
        all_model_tools = {
            str(item.get("id", "") or "").strip().lower()
            for item in items
            if bool(item.get("model_callable"))
        }
        all_model_tools.discard("")
        allowed = set(all_model_tools)

        if event.launcher_type == "group":
            bot_account_id = str(svc.config.bot_account_id or "").strip()
            if not (bot_account_id and event.has_bot_mention(bot_account_id)):
                allowed.difference_update(_NAMING_TOOL_IDS)

        # High-risk tools (filesystem / command / network) are withheld unless the
        # sender is explicitly authorized. Default allowlist is empty, so no QQ user
        # can reach them until the owner opts specific accounts in.
        if not self._is_tool_authorized(event):
            privileged = {
                str(item.get("id", "") or "").strip().lower()
                for item in items
                if bool(item.get("model_callable"))
                and str(item.get("risk_level", "") or "").strip().lower() in _PRIVILEGED_RISK_LEVELS
            }
            privileged.discard("")
            allowed.difference_update(privileged)
        return allowed

    def _is_tool_authorized(self, event: InboundEvent) -> bool:
        svc = self._service
        authorized = {
            str(uid).strip()
            for uid in getattr(svc.config.tool_policy, "authorized_user_ids", []) or []
            if str(uid).strip()
        }
        if not authorized:
            return False
        sender = str(getattr(event, "sender_id", "") or "").strip()
        return bool(sender and sender in authorized)
