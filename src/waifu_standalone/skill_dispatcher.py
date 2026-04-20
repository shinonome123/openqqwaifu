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

import asyncio
import json
import os
import re
import shutil
import subprocess
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

    @staticmethod
    def _search_sources_from_payload(payload: dict[str, object]) -> list[tuple[str, str]]:
        results = payload.get("results", [])
        if not isinstance(results, list):
            return []
        sources: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "") or "").strip()
            if not url or url in seen_urls:
                continue
            title = str(item.get("title", "") or "").strip() or url
            seen_urls.add(url)
            sources.append((title, url))
        return sources

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

    def resolve_explicit_skill_request(self, text: str) -> tuple[SkillSpec, str] | None:
        normalized = " ".join(str(text or "").split()).strip()
        if not normalized:
            return None
        patterns = (
            r'^\s*(?:请)?(?:用|使用|按|拿)\s*[\"“「]?(?P<skill>.+?)[\"”」]?\s*(?:这个)?(?:技能|skill)\s*(?:来|去|帮我|给我|直接|试着|一下)?\s*(?P<args>.*)$',
            r'^\s*use\s+[\"“「]?(?P<skill>.+?)[\"”」]?\s+skill\b[\s:：，,]*(?P<args>.*)$',
        )
        for pattern in patterns:
            match = re.match(pattern, normalized, flags=re.IGNORECASE)
            if match is None:
                continue
            skill_name = str(match.group("skill") or "").strip().strip("\"'“”「」")
            if not skill_name:
                continue
            skill = self._service.skills.find_by_name_or_id(skill_name)
            if skill is None or not skill.enabled or not skill.user_invocable:
                continue
            raw_args = str(match.group("args") or "").lstrip(" ：:，,")
            return skill, raw_args
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

    def dispatch_explicit_skill(
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
        tool_id = self._explicit_tool_id(skill)
        if tool_id:
            invocation = ToolInvocation(
                tool_id=tool_id,
                raw_args=raw_args,
                event=event,
                session=session,
                skill=skill,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
            )
            message = service.tools.execute(tool_id, invocation)
            if message is not None:
                return message
        return self.handle_unbound_skill_request(
            event,
            skill=skill,
            address=address,
            assistant_name=assistant_name,
        )

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

    async def adispatch_explicit_skill(
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
        tool_id = self._explicit_tool_id(skill)
        if tool_id:
            invocation = ToolInvocation(
                tool_id=tool_id,
                raw_args=raw_args,
                event=event,
                session=session,
                skill=skill,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
            )
            message = await service.tools.aexecute(tool_id, invocation)
            if message is not None:
                return message
        return await self.ahandle_unbound_skill_request(
            event,
            skill=skill,
            address=address,
            assistant_name=assistant_name,
        )

    def handle_unbound_skill_request(
        self,
        event: InboundEvent,
        *,
        skill: SkillSpec,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        trigger_hint = ""
        if skill.triggers:
            trigger_hint = "你可以直接用它的触发词： " + " / ".join(skill.triggers[:3]) + "。"
        text = (
            f"{address}，`{skill.name}` 这个技能现在还没有绑定可执行工具，"
            "我不能假装已经替你跑完。"
        )
        if trigger_hint:
            text += trigger_hint
        else:
            text += "要让它真的执行，需要给这个技能补 `command-dispatch / command-tool`，或者接上对应运行时。"
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=text,
        )
        return service.emitter.emit(event, message, assistant_name=assistant_name)

    async def ahandle_unbound_skill_request(
        self,
        event: InboundEvent,
        *,
        skill: SkillSpec,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        service = self._service
        trigger_hint = ""
        if skill.triggers:
            trigger_hint = "你可以直接用它的触发词： " + " / ".join(skill.triggers[:3]) + "。"
        text = (
            f"{address}，`{skill.name}` 这个技能现在还没有绑定可执行工具，"
            "我不能假装已经替你跑完。"
        )
        if trigger_hint:
            text += trigger_hint
        else:
            text += "要让它真的执行，需要给这个技能补 `command-dispatch / command-tool`，或者接上对应运行时。"
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=text,
        )
        return await service.emitter.aemit(event, message, assistant_name=assistant_name)

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

    def handle_search_link_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
        search_payload: dict[str, object] | None = None,
    ) -> OutboundMessage:
        service = self._service
        payload = search_payload or {}
        query = " ".join(str(payload.get("query", "") or "").split()).strip()
        sources = self._search_sources_from_payload(payload)
        if not sources:
            if query:
                text = (
                    f"{address}，我这边只有“{query}”那次检索的摘要，没有拿到稳定的原文链接，"
                    "不能给你乱编。你要的话我可以换个关键词再查一次官网或原始来源。"
                )
            else:
                text = (
                    f"{address}，你要的是来源链接的话，我得先拿到一轮真实检索结果，"
                    "现在手里没有可核对的链接，不能给你乱编。你把关键词再发我一次，我查到真实链接就直接贴给你。"
                )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=text,
            )
            return service.emitter.emit(event, message, assistant_name=assistant_name)

        lines = [f"{address}，我把这次检索里拿到的真实链接发你。"]
        if query:
            lines.append(f"关键词：{query}")
        for title, url in sources[:3]:
            lines.append(f"- {service.generator._clip(title, limit=56)}")
            lines.append(url)
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
        )
        return service.emitter.emit(event, message, assistant_name=assistant_name)

    async def ahandle_search_link_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
        search_payload: dict[str, object] | None = None,
    ) -> OutboundMessage:
        service = self._service
        payload = search_payload or {}
        query = " ".join(str(payload.get("query", "") or "").split()).strip()
        sources = self._search_sources_from_payload(payload)
        if not sources:
            if query:
                text = (
                    f"{address}，我这边只有“{query}”那次检索的摘要，没有拿到稳定的原文链接，"
                    "不能给你乱编。你要的话我可以换个关键词再查一次官网或原始来源。"
                )
            else:
                text = (
                    f"{address}，你要的是来源链接的话，我得先拿到一轮真实检索结果，"
                    "现在手里没有可核对的链接，不能给你乱编。你把关键词再发我一次，我查到真实链接就直接贴给你。"
                )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=text,
            )
            return await service.emitter.aemit(event, message, assistant_name=assistant_name)

        lines = [f"{address}，我把这次检索里拿到的真实链接发你。"]
        if query:
            lines.append(f"关键词：{query}")
        for title, url in sources[:3]:
            lines.append(f"- {service.generator._clip(title, limit=56)}")
            lines.append(url)
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
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
            trigger_hint = self._skill_list_trigger_hint(skill)
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
            trigger_hint = self._skill_list_trigger_hint(skill)
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

    def run_summarize_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        service = self._service
        target = self._extract_summarize_target(
            invocation.raw_args or invocation.event.command_text(service.config.bot_account_id)
        )
        if not target:
            message = OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=f"{invocation.address}，把要总结的链接或文件路径直接发给我，我才能真的调用 summarize 去处理。",
            )
            return service.emitter.emit(
                invocation.event,
                message,
                assistant_name=invocation.assistant_name,
            )

        payload, error = self._invoke_summarize_cli(target, invocation=invocation)
        if error:
            message = OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=f"{invocation.address}，我这次没法真正跑 summarize：{error}",
            )
            return service.emitter.emit(
                invocation.event,
                message,
                assistant_name=invocation.assistant_name,
            )

        text = self._format_summarize_reply(payload, address=invocation.address)
        message = OutboundMessage(
            launcher_id=invocation.event.launcher_id,
            launcher_type=invocation.event.launcher_type,
            text=text,
        )
        return service.emitter.emit(
            invocation.event,
            message,
            assistant_name=invocation.assistant_name,
        )

    async def arun_summarize_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return await asyncio.to_thread(self.run_summarize_tool, invocation)

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
    def _skill_list_trigger_hint(self, skill: SkillSpec) -> str:
        if skill.triggers:
            if skill.mode == "prefix":
                examples = skill.triggers[:2]
                return " — 说「" + "」或「".join(examples) + "」"
            examples = skill.triggers[:3]
            return " — 提到「" + "」「".join(examples) + "」时激活"
        tool_id = self._explicit_tool_id(skill)
        if tool_id:
            return f" — 也可以直接说「用{skill.name}技能 ...」"
        return " — 仅导入说明，当前还没有触发或执行入口"

    def _explicit_tool_id(self, skill: SkillSpec) -> str:
        candidates = [str(skill.command_tool or "").strip().lower()]
        for raw in (skill.name, skill.skill_id):
            normalized = self._tool_like_name(raw)
            if normalized:
                candidates.append(normalized)
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if self._service.tools.has(candidate):
                return candidate
        return ""

    @staticmethod
    def _tool_like_name(value: str) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
        if not normalized or not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", normalized):
            return ""
        return normalized

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

    @staticmethod
    def _extract_summarize_target(raw_args: str) -> str:
        text = " ".join(str(raw_args or "").split()).strip()
        if not text:
            return ""
        url_match = re.search(r"https?://\S+", text, flags=re.IGNORECASE)
        if url_match is not None:
            return url_match.group(0).rstrip(".,!?)]}>'\"”」")
        if os.path.exists(text):
            return text
        return ""

    def _invoke_summarize_cli(
        self,
        target: str,
        *,
        invocation: ToolInvocation,
    ) -> tuple[dict[str, object], str]:
        cli = shutil.which("summarize")
        if not cli:
            return {}, "当前运行时还没安装 summarize CLI。"
        command = [
            cli,
            target,
            "--json",
            "--plain",
            "--no-color",
            "--stream",
            "off",
            "--timeout",
            "90s",
            "--length",
            "short",
            "--language",
            self._summarize_output_language(invocation),
        ]
        model = self._summarize_cli_model()
        if model:
            command.extend(["--model", model])
        if "youtube.com/" in target or "youtu.be/" in target:
            command.extend(["--youtube", "auto"])
        env = self._summarize_cli_env()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {}, "summarize 超时了，这次没有拿到稳定结果。"
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            return {}, self._service.generator._clip(detail or "summarize 执行失败。", limit=120)
        stdout = str(completed.stdout or "").strip()
        if not stdout:
            return {}, "summarize 没有返回可解析的结果。"
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return {}, "summarize 返回的不是可解析的 JSON。"
        return payload if isinstance(payload, dict) else {}, ""

    def _summarize_cli_env(self) -> dict[str, str]:
        env = dict(os.environ)
        llm = self._service.config.llm
        api_key = str(llm.api_key or "").strip()
        model_backend = str(llm.backend or "").strip().lower()
        base_url = str(llm.base_url or "").strip().lower()
        if api_key:
            if model_backend == "xai" or "api.x.ai" in base_url:
                env.setdefault("XAI_API_KEY", api_key)
            elif model_backend == "anthropic":
                env.setdefault("ANTHROPIC_API_KEY", api_key)
            elif model_backend == "google":
                env.setdefault("GEMINI_API_KEY", api_key)
            else:
                env.setdefault("OPENAI_API_KEY", api_key)
        return env

    def _summarize_cli_model(self) -> str:
        llm = self._service.config.llm
        model = str(llm.model or "").strip()
        if not model:
            return ""
        if "/" in model:
            return model
        backend = str(llm.backend or "").strip().lower()
        base_url = str(llm.base_url or "").strip().lower()
        if backend == "xai" or "api.x.ai" in base_url:
            return f"xai/{model}"
        if backend == "anthropic":
            return f"anthropic/{model}"
        if backend == "google":
            return f"google/{model}"
        return f"openai/{model}"

    def _summarize_output_language(self, invocation: ToolInvocation) -> str:
        card = self._service.cards.load(invocation.event.launcher_type, invocation.session)
        language = str(card.language or "").strip().lower()
        if "zh" in language or "中文" in language or "chinese" in language:
            return "zh"
        if language.startswith("en") or "english" in language:
            return "en"
        return "auto"

    def _format_summarize_reply(self, payload: dict[str, object], *, address: str) -> str:
        extracted = payload.get("extracted", {})
        extracted = extracted if isinstance(extracted, dict) else {}
        summary = str(payload.get("summary", "") or "").strip()
        site_name = str(extracted.get("siteName", "") or "").strip().lower()
        transcript_source = str(extracted.get("transcriptSource", "") or "").strip().lower()
        transcript_meta = extracted.get("transcriptMetadata", {})
        transcript_meta = transcript_meta if isinstance(transcript_meta, dict) else {}
        transcript_reason = str(transcript_meta.get("reason", "") or "").strip().lower()
        if (
            "youtube" in site_name
            and transcript_source in {"", "unavailable"}
            and transcript_reason in {"no_transcript_available", "unavailable", ""}
        ):
            return (
                f"{address}，我这次确实调用了 summarize，"
                "但它没有拿到这个视频的可用字幕或正文，只抓到了 YouTube 壳页，"
                "所以我不能假装已经总结出视频内容。你可以换一个有字幕的视频，或者直接把文稿/字幕发我。"
            )
        if not summary:
            return f"{address}，我这次确实调用了 summarize，但它没有返回稳定摘要。"
        lines = [f"{address}，我这次是按 summarize 技能真的跑的。", summary]
        title = str(extracted.get("title", "") or "").strip()
        if title:
            lines.append(f"来源：{self._service.generator._clip(title, limit=72)}")
        return "\n".join(lines)
