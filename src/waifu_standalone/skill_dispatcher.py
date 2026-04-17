"""Skill-triggered message dispatch.

Extracted from :mod:`waifu_standalone.app`. :class:`SkillDispatcher` owns
the hand-off from *the event loop decided a skill applies* to *a concrete
:class:`OutboundMessage`*: image generation, live search, history
summaries, the skill-menu response, and the generic tool-registry bridge.

The class stays thin — it borrows ``generator``, ``tools``, ``skills``,
``search``, ``gate`` and ``emitter`` from the owning service rather
than copying state.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .cells.skill_registry import SkillSpec
from .cells.tool_registry import ToolInvocation
from .models import InboundEvent, OutboundMessage, SessionMemory

if TYPE_CHECKING:
    from .app import WaifuService


_SKILL_ICONS: dict[str, str] = {
    "image-command": "\U0001f3a8",
    "image-handoff": "\U0001f5bc\ufe0f",
    "search-command": "\U0001f50d",
    "summary-command": "\U0001f4dd",
    "concise-answer": "\U0001f4ac",
    "freshness-check": "\U0001f550",
}


class SkillDispatcher:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    # ------------------------------------------------------------------
    # Dispatch entry points (called from WaifuService event loop)
    # ------------------------------------------------------------------
    def resolve_builtin_dispatch(self, text: str) -> tuple[SkillSpec, str] | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        skills = self._service.skills
        if skills.has_dispatch_tool("skill-list") and self._looks_like_skill_list_request(
            normalized
        ):
            skill = skills.get_skill("skill-list-command")
            if skill is not None and skill.dispatches_tool:
                return skill, ""
        return None

    def dispatch_skill(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        skill: SkillSpec,
        raw_args: str,
        address: str,
        assistant_name: str,
        active_skills: list[SkillSpec],
    ) -> OutboundMessage | None:
        service = self._service
        invocation = ToolInvocation(
            tool_id=skill.command_tool,
            raw_args=raw_args,
            event=event,
            session=session,
            skill=skill,
            address=address,
            assistant_name=assistant_name,
            active_skills=active_skills,
        )
        message = service.tools.execute(skill.command_tool, invocation)
        if message is not None:
            return message
        unavailable = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=f"{address}，这个技能绑定的工具还没有注册：{skill.command_tool or 'unknown'}。",
        )
        return service.emitter.emit(event, unavailable, assistant_name=assistant_name)

    async def adispatch_skill(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        skill: SkillSpec,
        raw_args: str,
        address: str,
        assistant_name: str,
        active_skills: list[SkillSpec],
    ) -> OutboundMessage | None:
        service = self._service
        invocation = ToolInvocation(
            tool_id=skill.command_tool,
            raw_args=raw_args,
            event=event,
            session=session,
            skill=skill,
            address=address,
            assistant_name=assistant_name,
            active_skills=active_skills,
        )
        message = await service.tools.aexecute(skill.command_tool, invocation)
        if message is not None:
            return message
        unavailable = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=f"{address}，这个技能绑定的工具还没有注册：{skill.command_tool or 'unknown'}。",
        )
        return await service.emitter.aemit(event, unavailable, assistant_name=assistant_name)

    def handle_image_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
        prompt: str,
        active_skills: list[SkillSpec],
    ) -> OutboundMessage:
        service = self._service
        try:
            image = service.generator.generate_image(prompt)
            text = service.generator.generate_image_caption(
                image.prompt,
                launcher_type=event.launcher_type,
                session=session,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
            )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=text,
                images=[image.image_ref],
            )
        except Exception:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text="呜，这次图片没有画好，稍后再试一次吧。",
            )
        return service.emitter.emit(event, message, assistant_name=assistant_name)

    async def ahandle_image_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
        prompt: str,
        active_skills: list[SkillSpec],
    ) -> OutboundMessage:
        service = self._service
        try:
            image = await service.generator.agenerate_image(prompt)
            text = await service.generator.agenerate_image_caption(
                image.prompt,
                launcher_type=event.launcher_type,
                session=session,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
            )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=text,
                images=[image.image_ref],
            )
        except Exception:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text="呜，这次图片没有画好，稍后再试一次吧。",
            )
        return await service.emitter.aemit(event, message, assistant_name=assistant_name)

    def handle_search_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        query: str,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        cleaned_query = " ".join(query.split())
        service.gate.clear_pending_search(session)
        if not cleaned_query:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，你想让我查什么呀，把关键词直接告诉我就好。",
            )
            return service.emitter.emit(event, message, assistant_name=assistant_name)

        search_context = service.search.search_query(cleaned_query, reason="skill-dispatch")
        service._store_search_context(session, search_context)
        if not search_context.active:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，这次我没查到稳定结果，要不要换个关键词让我再试一次？",
            )
            return service.emitter.emit(event, message, assistant_name=assistant_name)

        lines = [f"{address}，我帮你查了一下。"]
        if search_context.summary:
            lines.append(search_context.summary)
        for result in search_context.results[1:3]:
            lines.append(f"- {result.title}：{service.generator._clip(result.snippet, limit=56)}")
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
        )
        return service.emitter.emit(event, message, assistant_name=assistant_name)

    async def ahandle_search_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        query: str,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        cleaned_query = " ".join(query.split())
        service.gate.clear_pending_search(session)
        if not cleaned_query:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，你想让我查什么呀，把关键词直接告诉我就好。",
            )
            return await service.emitter.aemit(event, message, assistant_name=assistant_name)

        search_context = await service.search.asearch_query(cleaned_query, reason="skill-dispatch")
        await service._store_search_context_async(session, search_context)
        if not search_context.active:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，这次我没查到稳定结果，要不要换个关键词让我再试一次？",
            )
            return await service.emitter.aemit(event, message, assistant_name=assistant_name)

        lines = [f"{address}，我帮你查了一下。"]
        if search_context.summary:
            lines.append(search_context.summary)
        for result in search_context.results[1:3]:
            lines.append(f"- {result.title}：{service.generator._clip(result.snippet, limit=56)}")
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
        )
        return await service.emitter.aemit(event, message, assistant_name=assistant_name)

    def handle_summary_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        recent_history = list(session.history)[-max(4, service.config.memory_summary_batch_size):]
        if not recent_history:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，现在还没有足够的上下文，我再多陪你聊几句就能帮你总结啦。",
            )
            return service.emitter.emit(event, message, assistant_name=assistant_name)
        summary, tags = service.generator.summarize_history(recent_history, assistant_name=assistant_name)
        if summary:
            text = f"{address}，我先帮你收一下重点：{summary}"
            if tags:
                text += "\n标签：" + "、".join(tags[:4])
        else:
            text = f"{address}，这段对话我还没法总结得漂亮，你再给我一点上下文吧。"
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=text,
        )
        return service.emitter.emit(event, message, assistant_name=assistant_name)

    async def ahandle_summary_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        recent_history = list(session.history)[-max(4, service.config.memory_summary_batch_size):]
        if not recent_history:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，现在还没有足够的上下文，我再多陪你聊几句就能帮你总结啦。",
            )
            return await service.emitter.aemit(event, message, assistant_name=assistant_name)
        summary, tags = await service.generator.asummarize_history(recent_history, assistant_name=assistant_name)
        if summary:
            text = f"{address}，我先帮你收一下重点：{summary}"
            if tags:
                text += "\n标签：" + "、".join(tags[:4])
        else:
            text = f"{address}，这段对话我还没法总结得漂亮，你再给我一点上下文吧。"
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=text,
        )
        return await service.emitter.aemit(event, message, assistant_name=assistant_name)

    def handle_skill_list_request(
        self,
        event: InboundEvent,
        *,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        all_skills = service.skills.list_skills()
        enabled_skills = [s for s in all_skills if s.enabled and s.skill_id != "skill-list-command"]

        if not enabled_skills:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，我现在还没有任何已启用的技能。",
            )
            return service.emitter.emit(event, message, assistant_name=assistant_name)

        lines = [f"{address}，我目前掌握的技能有：\n"]
        for skill in enabled_skills:
            icon = _SKILL_ICONS.get(skill.skill_id, "\u2728")
            trigger_hint = ""
            if skill.triggers:
                if skill.mode == "prefix":
                    examples = skill.triggers[:2]
                    trigger_hint = " — 说「" + "」或「".join(examples) + "」"
                else:
                    examples = skill.triggers[:3]
                    trigger_hint = " — 提到「" + "」「".join(examples) + "」时激活"
            lines.append(f"{icon} {skill.name}{trigger_hint}")
            if skill.description:
                lines.append(f"   {skill.description}")

        total = len(enabled_skills)
        workspace_count = sum(1 for s in enabled_skills if s.source_kind == "workspace")
        if workspace_count:
            lines.append(f"\n共 {total} 个技能（其中 {workspace_count} 个是自定义技能）。")
        else:
            lines.append(f"\n共 {total} 个技能。")

        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
        )
        return service.emitter.emit(event, message, assistant_name=assistant_name)

    async def ahandle_skill_list_request(
        self,
        event: InboundEvent,
        *,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        all_skills = service.skills.list_skills()
        enabled_skills = [s for s in all_skills if s.enabled and s.skill_id != "skill-list-command"]

        if not enabled_skills:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，我现在还没有任何已启用的技能。",
            )
            return await service.emitter.aemit(event, message, assistant_name=assistant_name)

        lines = [f"{address}，我目前掌握的技能有：\n"]
        for skill in enabled_skills:
            icon = _SKILL_ICONS.get(skill.skill_id, "\u2728")
            trigger_hint = ""
            if skill.triggers:
                if skill.mode == "prefix":
                    examples = skill.triggers[:2]
                    trigger_hint = " — 说「" + "」或「".join(examples) + "」"
                else:
                    examples = skill.triggers[:3]
                    trigger_hint = " — 提到「" + "」「".join(examples) + "」时激活"
            lines.append(f"{icon} {skill.name}{trigger_hint}")
            if skill.description:
                lines.append(f"   {skill.description}")

        total = len(enabled_skills)
        workspace_count = sum(1 for s in enabled_skills if s.source_kind == "workspace")
        if workspace_count:
            lines.append(f"\n共 {total} 个技能（其中 {workspace_count} 个是自定义技能）。")
        else:
            lines.append(f"\n共 {total} 个技能。")

        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
        )
        return await service.emitter.aemit(event, message, assistant_name=assistant_name)

    # ------------------------------------------------------------------
    # Tool-registry handlers
    # ------------------------------------------------------------------
    def run_image_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        service = self._service
        prompt = invocation.raw_args or self.extract_image_prompt(
            invocation.event.command_text(service.config.bot_account_id)
        )
        if not prompt:
            message = OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=f"{invocation.address}，你想让我画什么呀，直接把主题告诉我就好。",
            )
            return service.emitter.emit(
                invocation.event,
                message,
                assistant_name=invocation.assistant_name,
            )
        return self.handle_image_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
            prompt=prompt,
            active_skills=invocation.active_skills,
        )

    async def arun_image_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        service = self._service
        prompt = invocation.raw_args or self.extract_image_prompt(
            invocation.event.command_text(service.config.bot_account_id)
        )
        if not prompt:
            message = OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=f"{invocation.address}，你想让我画什么呀，直接把主题告诉我就好。",
            )
            return await service.emitter.aemit(
                invocation.event,
                message,
                assistant_name=invocation.assistant_name,
            )
        return await self.ahandle_image_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
            prompt=prompt,
            active_skills=invocation.active_skills,
        )

    def run_search_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        service = self._service
        query = (
            invocation.raw_args.strip()
            or invocation.event.command_text(service.config.bot_account_id).strip()
        )
        return self.handle_search_request(
            invocation.event,
            invocation.session,
            query=query,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    async def arun_search_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        service = self._service
        query = (
            invocation.raw_args.strip()
            or invocation.event.command_text(service.config.bot_account_id).strip()
        )
        return await self.ahandle_search_request(
            invocation.event,
            invocation.session,
            query=query,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    def run_summary_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self.handle_summary_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    async def arun_summary_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return await self.ahandle_summary_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    def run_skill_list_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self.handle_skill_list_request(
            invocation.event,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    async def arun_skill_list_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return await self.ahandle_skill_list_request(
            invocation.event,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    # ------------------------------------------------------------------
    # Image-prompt parsing
    # ------------------------------------------------------------------
    def extract_image_prompt(self, text: str) -> str | None:
        stripped = str(text or "").strip()
        if not stripped:
            return None
        for prefix in self._image_command_prefixes():
            match = re.match(
                rf"^\s*{re.escape(prefix)}\s*(?:\:|\uFF1A)?\s*(.*?)\s*$",
                stripped,
                flags=re.DOTALL,
            )
            if match:
                prompt = re.sub(r"\s+", " ", match.group(1)).strip()
                if prompt:
                    return prompt
        return None

    def _image_command_prefixes(self) -> list[str]:
        config = self._service.config
        prefixes = [config.image_command_prefix, *config.image_command_aliases]
        seen: set[str] = set()
        unique: list[str] = []
        for item in prefixes:
            prefix = str(item or "").strip()
            if not prefix or prefix in seen:
                continue
            seen.add(prefix)
            unique.append(prefix)
        return unique

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_skill_list_request(text: str) -> bool:
        normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not normalized:
            return False
        patterns = (
            r"^(?:说说|讲讲|聊聊|介绍下|介绍一下)?你(?:现在)?(?:都)?会(?:什么|干什么|做什么|哪些|啥)(?:技能|能力|功能)?$",
            r"^(?:说说|讲讲|聊聊|介绍下|介绍一下)?你(?:有|会)(?:的)?(?:技能|能力|功能)(?:都)?(?:有哪些|是什么)?$",
            r"^(?:技能|功能|命令)(?:列表|菜单)$",
        )
        return any(re.match(pattern, normalized) for pattern in patterns)
