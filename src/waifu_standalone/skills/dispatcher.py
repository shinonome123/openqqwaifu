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
import html
import json
import os
import re
import shlex
import shutil
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, urlparse

from ..infra import AsyncHttpTransport, SyncHttpTransport, TransportError
from ..models import InboundEvent, OutboundMessage, SessionMemory
from .registry import SkillSpec
from .tool_registry import ToolExecutionResult, ToolInvocation

if TYPE_CHECKING:
    from ..app import WaifuService


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
        slash_match = re.match(r"^\s*/skill\s+(?P<skill>[^\s:]+)(?:[\s:]+(?P<args>.*))?$", normalized, flags=re.IGNORECASE)
        if slash_match is not None:
            skill_name = str(slash_match.group("skill") or "").strip()
            skill = self._service.skills.find_by_name_or_id(skill_name)
            if skill is not None and skill.enabled and skill.eligible and skill.user_invocable:
                raw_args = str(slash_match.group("args") or "").strip()
                return skill, raw_args
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
            if skill is None or not skill.enabled or not skill.eligible or not skill.user_invocable:
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
        invocation = self._build_tool_invocation(
            event,
            session,
            skill=skill,
            raw_args=raw_args,
            address=address,
            assistant_name=assistant_name,
            active_skills=active_skills,
        )
        message = service.tools.execute(skill.command_tool, invocation)
        if message is not None:
            return message
        runtime_message = self._try_emit_claw_runtime_tool(invocation)
        if runtime_message is not None:
            return runtime_message
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
            invocation = self._build_tool_invocation(
                event,
                session,
                skill=skill,
                raw_args=raw_args,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
                tool_id=tool_id,
            )
            message = service.tools.execute(tool_id, invocation)
            if message is not None:
                return message
            runtime_message = self._try_emit_claw_runtime_tool(invocation)
            if runtime_message is not None:
                return runtime_message
        elif skill.command_tool:
            invocation = self._build_tool_invocation(
                event,
                session,
                skill=skill,
                raw_args=raw_args,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
                tool_id=skill.command_tool,
            )
            runtime_message = self._try_emit_claw_runtime_tool(invocation)
            if runtime_message is not None:
                return runtime_message
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
        background_image_delivery: bool = True,
    ) -> OutboundMessage | None:
        service = self._service
        invocation = self._build_tool_invocation(
            event,
            session,
            skill=skill,
            raw_args=raw_args,
            address=address,
            assistant_name=assistant_name,
            active_skills=active_skills,
        )
        if skill.command_tool == "image":
            invocation.arguments["background_delivery"] = bool(background_image_delivery)
        message = await service.tools.aexecute(skill.command_tool, invocation)
        if message is not None:
            return message
        runtime_message = await self._atry_emit_claw_runtime_tool(invocation)
        if runtime_message is not None:
            return runtime_message
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
        background_image_delivery: bool = True,
    ) -> OutboundMessage | None:
        service = self._service
        tool_id = self._explicit_tool_id(skill)
        if tool_id:
            invocation = self._build_tool_invocation(
                event,
                session,
                skill=skill,
                raw_args=raw_args,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
                tool_id=tool_id,
            )
            if tool_id == "image":
                invocation.arguments["background_delivery"] = bool(background_image_delivery)
            message = await service.tools.aexecute(tool_id, invocation)
            if message is not None:
                return message
            runtime_message = await self._atry_emit_claw_runtime_tool(invocation)
            if runtime_message is not None:
                return runtime_message
        elif skill.command_tool:
            invocation = self._build_tool_invocation(
                event,
                session,
                skill=skill,
                raw_args=raw_args,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
                tool_id=skill.command_tool,
            )
            if skill.command_tool == "image":
                invocation.arguments["background_delivery"] = bool(background_image_delivery)
            runtime_message = await self._atry_emit_claw_runtime_tool(invocation)
            if runtime_message is not None:
                return runtime_message
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
            caption_result = self.use_image_caption_tool(
                ToolInvocation(
                    tool_id="image-caption",
                    raw_args=image.prompt,
                    event=event,
                    session=session,
                    address=address,
                    assistant_name=assistant_name,
                    active_skills=active_skills,
                    arguments={"prompt": image.prompt},
                )
            )
            text = caption_result.text or "图片已经准备好了。"
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
        background_delivery: bool = True,
    ) -> OutboundMessage:
        service = self._service
        if not background_delivery:
            try:
                image = await service.generator.agenerate_image(prompt)
                caption_result = await self.ause_image_caption_tool(
                    ToolInvocation(
                        tool_id="image-caption",
                        raw_args=image.prompt,
                        event=event,
                        session=session,
                        address=address,
                        assistant_name=assistant_name,
                        active_skills=active_skills,
                        arguments={"prompt": image.prompt},
                    )
                )
                text = caption_result.text or "图片已经准备好了。"
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

        event_copy = deepcopy(event)
        session_copy = SessionMemory(
            launcher_id=session.launcher_id,
            launcher_type=session.launcher_type,
            character_id=session.character_id,
            history=list(session.history),
            preferred_name=session.preferred_name,
            metadata=deepcopy(session.metadata),
        )
        active_skills_copy = list(active_skills)
        service._submit_background_coro(
            "image_generation",
            self._adeliver_generated_image(
                event_copy,
                session_copy,
                address=address,
                assistant_name=assistant_name,
                prompt=prompt,
                active_skills=active_skills_copy,
            ),
        )
        ack = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=f"{address}，我先去画，画好就马上发你。",
        )
        return await service.emitter.aemit(event, ack, assistant_name=assistant_name)

    async def _adeliver_generated_image(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
        prompt: str,
        active_skills: list[SkillSpec],
    ) -> None:
        service = self._service
        try:
            image = await service.generator.agenerate_image(prompt)
            caption_result = await self.ause_image_caption_tool(
                ToolInvocation(
                    tool_id="image-caption",
                    raw_args=image.prompt,
                    event=event,
                    session=session,
                    address=address,
                    assistant_name=assistant_name,
                    active_skills=active_skills,
                    arguments={"prompt": image.prompt},
                )
            )
            text = caption_result.text or "图片已经准备好了。"
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=text,
                images=[image.image_ref],
            )
        except Exception as exc:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，这次图没画成，原因是：{str(exc) or '生成失败'}",
            )
        await service.emitter.aemit(event, message, assistant_name=assistant_name)

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
        if service.generator.llm_ready:
            return self._emit_tool_execution_result(invocation, self.use_image_tool(invocation))
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
        if service.generator.llm_ready:
            result = await self.ause_image_tool(invocation)
            return await self._aemit_tool_execution_result(invocation, result)
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
            background_delivery=bool(invocation.argument("background_delivery", True)),
        )

    def run_search_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        service = self._service
        if service.generator.llm_ready:
            return self._emit_tool_execution_result(invocation, self.use_search_tool(invocation))
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
        if service.generator.llm_ready:
            result = await self.ause_search_tool(invocation)
            return await self._aemit_tool_execution_result(invocation, result)
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
        if self._service.generator.llm_ready:
            return self._emit_tool_execution_result(invocation, self.use_summary_tool(invocation))
        return self.handle_summary_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    async def arun_summary_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        if self._service.generator.llm_ready:
            result = await self.ause_summary_tool(invocation)
            return await self._aemit_tool_execution_result(invocation, result)
        return await self.ahandle_summary_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    def run_skill_list_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        if self._service.generator.llm_ready:
            return self._emit_tool_execution_result(invocation, self.use_skill_list_tool(invocation))
        return self.handle_skill_list_request(
            invocation.event,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    async def arun_skill_list_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        if self._service.generator.llm_ready:
            result = await self.ause_skill_list_tool(invocation)
            return await self._aemit_tool_execution_result(invocation, result)
        return await self.ahandle_skill_list_request(
            invocation.event,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    def run_summarize_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        if self._service.generator.llm_ready:
            return self._emit_tool_execution_result(invocation, self.use_summarize_tool(invocation))
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

        payload, error = self._resolve_summarize_payload(target, invocation=invocation)
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
        if self._service.generator.llm_ready:
            result = await self.ause_summarize_tool(invocation)
            return await self._aemit_tool_execution_result(invocation, result)
        return await asyncio.to_thread(self.run_summarize_tool, invocation)

    def run_weather_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_weather_tool(invocation))

    async def arun_weather_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_weather_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def run_search_links_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_search_links_tool(invocation))

    async def arun_search_links_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_search_links_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def run_image_caption_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_image_caption_tool(invocation))

    async def arun_image_caption_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_image_caption_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def run_web_fetch_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_web_fetch_tool(invocation))

    async def arun_web_fetch_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_web_fetch_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def run_read_file_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_read_file_tool(invocation))

    async def arun_read_file_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_read_file_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def run_list_files_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_list_files_tool(invocation))

    async def arun_list_files_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_list_files_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def run_write_file_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_write_file_tool(invocation))

    async def arun_write_file_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_write_file_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def run_exec_command_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._emit_tool_execution_result(invocation, self.use_exec_command_tool(invocation))

    async def arun_exec_command_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        result = await self.ause_exec_command_tool(invocation)
        return await self._aemit_tool_execution_result(invocation, result)

    def use_image_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        prompt = str(
            invocation.argument("prompt")
            or invocation.raw_args
            or self.extract_image_prompt(invocation.event.command_text(service.config.bot_account_id))
            or ""
        ).strip()
        if not prompt:
            return ToolExecutionResult(error="missing prompt", text="请提供要生成图片的提示词。")
        try:
            image = service.generator.generate_image(prompt)
        except Exception as exc:
            return ToolExecutionResult(error=f"image generation failed: {exc}", text="图片生成失败。")
        return ToolExecutionResult(
            text=f"已生成图片，prompt: {image.prompt}",
            images=[image.image_ref],
            metadata={"prompt": image.prompt, "image_ref": image.image_ref},
            display_mode="media",
        )

    async def ause_image_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        prompt = str(
            invocation.argument("prompt")
            or invocation.raw_args
            or self.extract_image_prompt(invocation.event.command_text(service.config.bot_account_id))
            or ""
        ).strip()
        if not prompt:
            return ToolExecutionResult(error="missing prompt", text="请提供要生成图片的提示词。")
        try:
            image = await service.generator.agenerate_image(prompt)
        except Exception as exc:
            return ToolExecutionResult(error=f"image generation failed: {exc}", text="图片生成失败。")
        return ToolExecutionResult(
            text=f"已生成图片，prompt: {image.prompt}",
            images=[image.image_ref],
            metadata={"prompt": image.prompt, "image_ref": image.image_ref},
            display_mode="media",
        )

    def use_search_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        query = self._normalized_tool_text(
            invocation.argument("query")
            or invocation.raw_args
            or invocation.event.command_text(service.config.bot_account_id)
        )
        service.gate.clear_pending_search(invocation.session)
        if not query:
            return ToolExecutionResult(error="missing query", text="请提供要搜索的关键词。")
        search_context = service.search.search_query(query, reason="model-tool")
        service._store_search_context(invocation.session, search_context)
        if not search_context.active:
            return ToolExecutionResult(
                error=f"no stable result for query: {query}",
                text=f"没有查到“{query}”的稳定结果。",
                metadata=search_context.as_dict(),
            )
        lines = []
        if search_context.summary:
            lines.append(search_context.summary)
        for result in search_context.results[:3]:
            line = f"- {result.title}: {service.generator._clip(result.snippet, limit=120)}"
            if result.url:
                line += f" ({result.url})"
            lines.append(line)
        return ToolExecutionResult(
            text="\n".join(lines).strip() or search_context.summary,
            metadata=search_context.as_dict(),
            display_mode="inline",
        )

    async def ause_search_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        query = self._normalized_tool_text(
            invocation.argument("query")
            or invocation.raw_args
            or invocation.event.command_text(service.config.bot_account_id)
        )
        service.gate.clear_pending_search(invocation.session)
        if not query:
            return ToolExecutionResult(error="missing query", text="请提供要搜索的关键词。")
        search_context = await service.search.asearch_query(query, reason="model-tool")
        await service._store_search_context_async(invocation.session, search_context)
        if not search_context.active:
            return ToolExecutionResult(
                error=f"no stable result for query: {query}",
                text=f"没有查到“{query}”的稳定结果。",
                metadata=search_context.as_dict(),
            )
        lines = []
        if search_context.summary:
            lines.append(search_context.summary)
        for result in search_context.results[:3]:
            line = f"- {result.title}: {service.generator._clip(result.snippet, limit=120)}"
            if result.url:
                line += f" ({result.url})"
            lines.append(line)
        return ToolExecutionResult(
            text="\n".join(lines).strip() or search_context.summary,
            metadata=search_context.as_dict(),
            display_mode="inline",
        )

    def use_summary_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        recent_history = list(invocation.session.history)[-max(4, service.config.memory_summary_batch_size):]
        if not recent_history:
            return ToolExecutionResult(error="insufficient history", text="当前上下文还不够，暂时无法总结。")
        summary, tags = service.generator.summarize_history(recent_history, assistant_name=invocation.assistant_name)
        if not summary:
            return ToolExecutionResult(error="summary unavailable", text="这段对话暂时没有稳定总结结果。")
        text = summary
        if tags:
            text += "\n标签：" + "、".join(tags[:4])
        return ToolExecutionResult(
            text=text,
            metadata={"summary": summary, "tags": tags[:4]},
            display_mode="inline",
        )

    async def ause_summary_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        recent_history = list(invocation.session.history)[-max(4, service.config.memory_summary_batch_size):]
        if not recent_history:
            return ToolExecutionResult(error="insufficient history", text="当前上下文还不够，暂时无法总结。")
        summary, tags = await service.generator.asummarize_history(
            recent_history,
            assistant_name=invocation.assistant_name,
        )
        if not summary:
            return ToolExecutionResult(error="summary unavailable", text="这段对话暂时没有稳定总结结果。")
        text = summary
        if tags:
            text += "\n标签：" + "、".join(tags[:4])
        return ToolExecutionResult(
            text=text,
            metadata={"summary": summary, "tags": tags[:4]},
            display_mode="inline",
        )

    def use_skill_list_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        skills = [
            skill
            for skill in self._service.skills.list_skills()
            if skill.enabled and skill.skill_id != "skill-list-command"
        ]
        if not skills:
            return ToolExecutionResult(error="no enabled skills", text="当前没有已启用的技能。")
        lines: list[str] = []
        items: list[dict[str, object]] = []
        for skill in skills:
            summary = skill.name
            if skill.description:
                summary += f": {skill.description}"
            if skill.triggers:
                summary += " | triggers=" + ", ".join(skill.triggers[:3])
            if skill.command_tool:
                summary += f" | tool={skill.command_tool}"
            lines.append(summary)
            items.append(skill.as_dict())
        return ToolExecutionResult(
            text="\n".join(lines),
            metadata={"count": len(items), "skills": items},
            display_mode="raw_block",
        )

    async def ause_skill_list_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_skill_list_tool, invocation)

    def use_summarize_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        target = self._extract_summarize_target(
            str(invocation.argument("target") or invocation.raw_args or invocation.event.command_text(service.config.bot_account_id))
        )
        if not target:
            return ToolExecutionResult(error="missing target", text="请提供要总结的链接或文件路径。")
        payload, error = self._resolve_summarize_payload(target, invocation=invocation)
        if error:
            return ToolExecutionResult(error=error, text=f"summarize 执行失败：{error}")
        return ToolExecutionResult(
            text=self._format_summarize_reply(payload, address=invocation.address or "你"),
            metadata=payload,
            display_mode="inline",
        )

    async def ause_summarize_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_summarize_tool, invocation)

    def use_weather_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        location = self._extract_weather_location(invocation)
        if not location:
            return ToolExecutionResult(error="missing location", text="请告诉我要查哪个城市或地区的天气。")
        url = f"https://wttr.in/{quote_plus(location)}?format=j1&lang=zh-cn"
        transport = SyncHttpTransport(timeout_seconds=max(5.0, float(self._service.config.search_timeout_seconds or 8.0)))
        try:
            response = transport.request(
                "GET",
                url,
                headers={
                    "User-Agent": "openqqwaifu-weather/1.0",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
        except TransportError as exc:
            return ToolExecutionResult(error=str(exc), text=f"天气查询失败：{exc}")
        finally:
            transport.close()
        payload, error = self._parse_weather_payload(response.text, location=location)
        if error:
            return ToolExecutionResult(error=error, text=f"天气查询失败：{error}")
        return ToolExecutionResult(
            text=self._format_weather_reply(payload, address=invocation.address or "你"),
            metadata=payload,
            display_mode="inline",
        )

    async def ause_weather_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        location = self._extract_weather_location(invocation)
        if not location:
            return ToolExecutionResult(error="missing location", text="请告诉我要查哪个城市或地区的天气。")
        url = f"https://wttr.in/{quote_plus(location)}?format=j1&lang=zh-cn"
        transport = AsyncHttpTransport(timeout_seconds=max(5.0, float(self._service.config.search_timeout_seconds or 8.0)))
        try:
            response = await transport.request(
                "GET",
                url,
                headers={
                    "User-Agent": "openqqwaifu-weather/1.0",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
        except TransportError as exc:
            return ToolExecutionResult(error=str(exc), text=f"天气查询失败：{exc}")
        finally:
            transport.close()
        payload, error = self._parse_weather_payload(response.text, location=location)
        if error:
            return ToolExecutionResult(error=error, text=f"天气查询失败：{error}")
        return ToolExecutionResult(
            text=self._format_weather_reply(payload, address=invocation.address or "你"),
            metadata=payload,
            display_mode="inline",
        )

    def use_search_links_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        query = self._normalized_tool_text(invocation.argument("query") or invocation.raw_args)
        payload = invocation.session.metadata.get("last_search", {})
        payload = payload if isinstance(payload, dict) else {}
        if query:
            search_context = service.search.search_query(query, reason="model-link-sources")
            service._store_search_context(invocation.session, search_context)
            payload = search_context.as_dict()
        sources = self._search_sources_from_payload(payload)
        if not sources:
            return ToolExecutionResult(
                error="no search sources available",
                text="当前没有可返回的稳定来源链接。",
                metadata={"query": payload.get("query", ""), "results": payload.get("results", [])},
            )
        query_text = self._normalized_tool_text(payload.get("query", ""))
        lines: list[str] = []
        if query_text:
            lines.append(f"query: {query_text}")
        for title, url in sources[:5]:
            lines.append(f"- {title}")
            lines.append(url)
        return ToolExecutionResult(
            text="\n".join(lines),
            metadata={"query": query_text, "sources": [{"title": title, "url": url} for title, url in sources[:5]]},
            display_mode="raw_block",
        )

    async def ause_search_links_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        service = self._service
        query = self._normalized_tool_text(invocation.argument("query") or invocation.raw_args)
        payload = invocation.session.metadata.get("last_search", {})
        payload = payload if isinstance(payload, dict) else {}
        if query:
            search_context = await service.search.asearch_query(query, reason="model-link-sources")
            await service._store_search_context_async(invocation.session, search_context)
            payload = search_context.as_dict()
        sources = self._search_sources_from_payload(payload)
        if not sources:
            return ToolExecutionResult(
                error="no search sources available",
                text="当前没有可返回的稳定来源链接。",
                metadata={"query": payload.get("query", ""), "results": payload.get("results", [])},
            )
        query_text = self._normalized_tool_text(payload.get("query", ""))
        lines: list[str] = []
        if query_text:
            lines.append(f"query: {query_text}")
        for title, url in sources[:5]:
            lines.append(f"- {title}")
            lines.append(url)
        return ToolExecutionResult(
            text="\n".join(lines),
            metadata={"query": query_text, "sources": [{"title": title, "url": url} for title, url in sources[:5]]},
            display_mode="raw_block",
        )

    def use_image_caption_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        prompt = str(invocation.argument("prompt") or invocation.raw_args or "").strip()
        if not prompt:
            return ToolExecutionResult(error="missing prompt", text="请提供图片提示词。")
        text = self._service.generator.generate_image_caption(
            prompt,
            launcher_type=invocation.event.launcher_type,
            session=invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
            active_skills=invocation.active_skills,
        )
        return ToolExecutionResult(
            text=text,
            metadata={"prompt": prompt, "already_persona": True},
            display_mode="inline",
        )

    async def ause_image_caption_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        prompt = str(invocation.argument("prompt") or invocation.raw_args or "").strip()
        if not prompt:
            return ToolExecutionResult(error="missing prompt", text="请提供图片提示词。")
        text = await self._service.generator.agenerate_image_caption(
            prompt,
            launcher_type=invocation.event.launcher_type,
            session=invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
            active_skills=invocation.active_skills,
        )
        return ToolExecutionResult(
            text=text,
            metadata={"prompt": prompt, "already_persona": True},
            display_mode="inline",
        )

    def use_web_fetch_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        url = self._normalized_tool_text(invocation.argument("url") or invocation.raw_args)
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return ToolExecutionResult(error="missing url", text="请提供 http 或 https 链接。")
        transport = SyncHttpTransport(timeout_seconds=max(5.0, float(self._service.config.search_timeout_seconds or 8.0)))
        try:
            response = transport.request(
                "GET",
                url,
                headers={"User-Agent": "openqqwaifu-tool-fetch/1.0", "Accept": "text/html,application/json,text/plain,*/*"},
            )
        except TransportError as exc:
            return ToolExecutionResult(error=str(exc), text=f"网页抓取失败：{exc}")
        finally:
            transport.close()
        text, title = self._normalize_web_document(response.text)
        max_chars = self._bounded_int(invocation.argument("max_chars"), default=2400, minimum=200, maximum=12000)
        clipped = self._service.generator._clip(text, limit=max_chars)
        lines = []
        if title:
            lines.append(f"title: {title}")
        if clipped:
            lines.append(clipped)
        return ToolExecutionResult(
            text="\n".join(lines).strip() or f"Fetched {url}",
            metadata={
                "url": url,
                "title": title,
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
            },
            display_mode="raw_block",
        )

    async def ause_web_fetch_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        url = self._normalized_tool_text(invocation.argument("url") or invocation.raw_args)
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return ToolExecutionResult(error="missing url", text="请提供 http 或 https 链接。")
        transport = AsyncHttpTransport(timeout_seconds=max(5.0, float(self._service.config.search_timeout_seconds or 8.0)))
        try:
            response = await transport.request(
                "GET",
                url,
                headers={"User-Agent": "openqqwaifu-tool-fetch/1.0", "Accept": "text/html,application/json,text/plain,*/*"},
            )
        except TransportError as exc:
            return ToolExecutionResult(error=str(exc), text=f"网页抓取失败：{exc}")
        finally:
            transport.close()
        text, title = self._normalize_web_document(response.text)
        max_chars = self._bounded_int(invocation.argument("max_chars"), default=2400, minimum=200, maximum=12000)
        clipped = self._service.generator._clip(text, limit=max_chars)
        lines = []
        if title:
            lines.append(f"title: {title}")
        if clipped:
            lines.append(clipped)
        return ToolExecutionResult(
            text="\n".join(lines).strip() or f"Fetched {url}",
            metadata={
                "url": url,
                "title": title,
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
            },
            display_mode="raw_block",
        )

    def use_read_file_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        raw_path = self._normalized_tool_text(invocation.argument("path") or invocation.raw_args)
        if not raw_path:
            return ToolExecutionResult(error="missing path", text="请提供文件路径。")
        try:
            path = self._resolve_tool_path(raw_path, allow_missing=False, purpose="read")
        except ValueError as exc:
            return ToolExecutionResult(error=str(exc), text=str(exc))
        if not path.exists() or not path.is_file():
            return ToolExecutionResult(error=f"file not found: {path}", text=f"文件不存在：{path}")
        max_chars = self._bounded_int(invocation.argument("max_chars"), default=2400, minimum=200, maximum=16000)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="replace")
        clipped = self._service.generator._clip(content, limit=max_chars)
        return ToolExecutionResult(
            text=f"path: {path}\n{clipped}".strip(),
            metadata={"path": str(path), "size": path.stat().st_size},
            display_mode="raw_block",
        )

    async def ause_read_file_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_read_file_tool, invocation)

    def use_list_files_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        raw_path = self._normalized_tool_text(invocation.argument("path") or invocation.raw_args or ".")
        recursive = bool(invocation.argument("recursive", False))
        limit = self._bounded_int(invocation.argument("limit"), default=20, minimum=1, maximum=200)
        try:
            root = self._resolve_tool_path(
                raw_path,
                allow_missing=False,
                expect_directory=True,
                purpose="read",
            )
        except ValueError as exc:
            return ToolExecutionResult(error=str(exc), text=str(exc))
        entries = self._list_directory_entries(root, recursive=recursive, limit=limit)
        if not entries:
            return ToolExecutionResult(text=f"目录为空：{root}", metadata={"path": str(root), "entries": []})
        return ToolExecutionResult(
            text="\n".join(entries),
            metadata={"path": str(root), "recursive": recursive, "entries": entries},
            display_mode="raw_block",
        )

    async def ause_list_files_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_list_files_tool, invocation)

    def use_write_file_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        raw_path = self._normalized_tool_text(invocation.argument("path") or "")
        content = str(invocation.argument("content", "") or "")
        if not raw_path:
            raw_parts = str(invocation.raw_args or "").split(None, 1)
            if raw_parts:
                raw_path = raw_parts[0].strip()
                if len(raw_parts) > 1 and not content:
                    content = raw_parts[1]
        if not raw_path:
            return ToolExecutionResult(error="missing path", text="请提供要写入的文件路径。")
        try:
            path = self._resolve_tool_path(raw_path, allow_missing=True, purpose="write")
        except ValueError as exc:
            return ToolExecutionResult(error=str(exc), text=str(exc))
        policy = self._service.config.tool_policy
        if not policy.enabled or not policy.write_enabled:
            return ToolExecutionResult(
                error="write-file is disabled by tool policy",
                text="写入文件能力当前被安全策略禁用。",
                metadata={"policy": "write-disabled"},
            )
        append = bool(invocation.argument("append", False))
        path.parent.mkdir(parents=True, exist_ok=True)
        if append:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return ToolExecutionResult(
            text=f"已写入文件：{path}",
            metadata={"path": str(path), "append": append, "chars": len(content)},
            display_mode="inline",
        )

    async def ause_write_file_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_write_file_tool, invocation)

    def use_exec_command_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        raw_command = invocation.argument("command")
        argv = invocation.argument("argv")
        if isinstance(argv, list):
            command = [str(item) for item in argv if str(item).strip()]
        else:
            text = self._normalized_tool_text(raw_command or invocation.raw_args)
            command = shlex.split(text, posix=os.name != "nt") if text else []
        if not command:
            return ToolExecutionResult(error="missing command", text="请提供要执行的命令。")
        raw_cwd = self._normalized_tool_text(invocation.argument("cwd") or ".")
        try:
            cwd = self._resolve_tool_path(
                raw_cwd,
                allow_missing=False,
                expect_directory=True,
                purpose="exec",
            )
        except ValueError as exc:
            return ToolExecutionResult(error=str(exc), text=str(exc))
        policy = self._service.config.tool_policy
        if not policy.enabled or not policy.exec_enabled:
            return ToolExecutionResult(
                error="exec-command is disabled by tool policy",
                text="执行命令能力当前被安全策略禁用。",
                metadata={"policy": "exec-disabled"},
            )
        command_name = self._normalized_command_token(command[0])
        allowed_commands = self._allowed_exec_commands()
        if command_name not in allowed_commands:
            return ToolExecutionResult(
                error=f"command is not allowlisted: {command[0]}",
                text=f"命令未在白名单内：{command[0]}",
                metadata={
                    "policy": "exec-allowlist",
                    "command": command[0],
                    "allowed_commands": allowed_commands,
                },
            )
        timeout_seconds = float(invocation.argument("timeout_seconds", 20) or 20)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=str(cwd),
                timeout=max(1.0, min(timeout_seconds, 120.0)),
                check=False,
                shell=False,
            )
        except Exception as exc:
            return ToolExecutionResult(error=str(exc), text=f"命令执行失败：{exc}")
        output = (completed.stdout or completed.stderr or "").strip()
        clipped = self._service.generator._clip(output or "(no output)", limit=4000)
        return ToolExecutionResult(
            text=clipped,
            metadata={
                "argv": command,
                "cwd": str(cwd),
                "returncode": completed.returncode,
            },
            error="" if completed.returncode == 0 else f"command exited with {completed.returncode}",
            display_mode="raw_block",
        )

    async def ause_exec_command_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_exec_command_tool, invocation)

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
    def _emit_tool_execution_result(
        self,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
    ) -> OutboundMessage:
        reply = self._service.generator.generate_tool_reply_message(invocation, result)
        text = self._compose_tool_display_text(reply.text, result)
        message = OutboundMessage(
            launcher_id=invocation.event.launcher_id,
            launcher_type=invocation.event.launcher_type,
            text=text,
            images=list(reply.images or result.images),
        )
        return self._service.emitter.emit(
            invocation.event,
            message,
            assistant_name=invocation.assistant_name,
        )

    async def _aemit_tool_execution_result(
        self,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
    ) -> OutboundMessage:
        reply = await self._service.generator.agenerate_tool_reply_message(invocation, result)
        text = self._compose_tool_display_text(reply.text, result)
        message = OutboundMessage(
            launcher_id=invocation.event.launcher_id,
            launcher_type=invocation.event.launcher_type,
            text=text,
            images=list(reply.images or result.images),
        )
        return await self._service.emitter.aemit(
            invocation.event,
            message,
            assistant_name=invocation.assistant_name,
        )

    @staticmethod
    def _compose_tool_display_text(prefix: str, result: ToolExecutionResult) -> str:
        lead = str(prefix or "").strip()
        payload = str(result.text or result.error or "工具没有返回内容。").strip()
        if result.display_mode != "raw_block":
            return lead or payload
        block = "```text\n" + payload + "\n```"
        if not lead:
            return block
        return lead + "\n" + block

    def _claw_runtime_allows_execution(self) -> bool:
        config = self._service.config.claw_runtime
        if not config.enabled:
            return False
        return str(config.routing_mode or "shadow").strip().lower() in {"hybrid", "authoritative"}

    def _claw_runtime_payload(self, invocation: ToolInvocation) -> dict[str, object]:
        payload = dict(invocation.arguments)
        payload.setdefault("raw_args", invocation.raw_args)
        if invocation.raw_args and "input" not in payload:
            payload["input"] = invocation.raw_args
        if invocation.skill is not None:
            payload.setdefault("skill_id", invocation.skill.skill_id)
        return payload

    @staticmethod
    def _tool_result_from_claw_runtime(payload: dict[str, object]) -> ToolExecutionResult:
        status = str(payload.get("status", "") or "").strip().lower()
        text = str(payload.get("text", "") or "").strip()
        reason = str(payload.get("reason", "") or "").strip()
        metadata = payload.get("metadata", {})
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        raw_images = payload.get("images", [])
        images = (
            [str(item).strip() for item in raw_images if str(item).strip()]
            if isinstance(raw_images, list)
            else []
        )
        if status == "ok":
            return ToolExecutionResult(
                text=text or "已通过 ClawRuntime 执行。",
                images=images,
                metadata=metadata,
            )
        return ToolExecutionResult(
            text=text or reason or "ClawRuntime 未能执行该工具。",
            images=images,
            metadata=metadata,
            error=reason or status or "claw runtime invocation failed",
        )

    def _try_emit_claw_runtime_tool(self, invocation: ToolInvocation) -> OutboundMessage | None:
        if not self._claw_runtime_allows_execution():
            return None
        tool_id = str(invocation.tool_id or "").strip()
        if not tool_id:
            return None
        try:
            payload = self._service.claw_runtime.invoke_tool(
                tool_id,
                self._claw_runtime_payload(invocation),
            )
        except Exception:
            return None
        return self._emit_tool_execution_result(
            invocation,
            self._tool_result_from_claw_runtime(payload),
        )

    async def _atry_emit_claw_runtime_tool(self, invocation: ToolInvocation) -> OutboundMessage | None:
        if not self._claw_runtime_allows_execution():
            return None
        tool_id = str(invocation.tool_id or "").strip()
        if not tool_id:
            return None
        try:
            payload = await asyncio.to_thread(
                self._service.claw_runtime.invoke_tool,
                tool_id,
                self._claw_runtime_payload(invocation),
            )
        except Exception:
            return None
        return await self._aemit_tool_execution_result(
            invocation,
            self._tool_result_from_claw_runtime(payload),
        )

    def _build_tool_invocation(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        skill: SkillSpec,
        raw_args: str,
        address: str,
        assistant_name: str,
        active_skills: list[SkillSpec],
        tool_id: str = "",
    ) -> ToolInvocation:
        normalized_tool_id = str(tool_id or skill.command_tool or "").strip().lower()
        parsed_arguments = self._parse_command_arguments(raw_args, mode=skill.command_arg_mode)
        return ToolInvocation(
            tool_id=normalized_tool_id,
            raw_args=str(raw_args or "").strip(),
            event=event,
            session=session,
            skill=skill,
            address=address,
            assistant_name=assistant_name,
            active_skills=active_skills,
            arguments=parsed_arguments,
        )

    @staticmethod
    def _normalized_tool_text(raw: object) -> str:
        return " ".join(str(raw or "").split()).strip()

    @staticmethod
    def _bounded_int(raw: object, *, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = int(default)
        return max(int(minimum), min(int(maximum), value))

    def _config_base_dir(self) -> Path:
        config_path = str(self._service.config.config_path or "").strip()
        if config_path:
            try:
                return Path(config_path).resolve().parent
            except OSError:
                pass
        return Path.cwd().resolve()

    def _tool_roots(self, purpose: str = "read") -> list[Path]:
        policy = self._service.config.tool_policy
        base_dir = self._config_base_dir()
        default_common = [base_dir, Path(self._service.config.data_root or ".")]
        configured: list[str]
        if purpose == "write":
            configured = list(policy.write_allowed_roots or policy.allowed_roots)
        elif purpose == "exec":
            configured = list(policy.exec_allowed_roots or policy.allowed_roots)
        else:
            configured = list(policy.allowed_roots)
        raw_roots: list[Path] = []
        if configured:
            for item in configured:
                raw = self._resolve_policy_root(item)
                if raw is not None:
                    raw_roots.append(raw)
        else:
            raw_roots = default_common
        roots: list[Path] = []
        for raw in raw_roots:
            try:
                resolved = raw.resolve(strict=False)
            except OSError:
                continue
            if resolved not in roots:
                roots.append(resolved)
        return roots

    def _resolve_policy_root(self, raw_root: str) -> Path | None:
        token = str(raw_root or "").strip()
        if not token:
            return None
        lowered = token.replace("\\", "/").strip().lower()
        if lowered in {"data", "./data", ".\\data"}:
            return Path(self._service.config.data_root or ".").expanduser()
        if lowered in {".", "./", ".\\"}:
            return self._config_base_dir()
        path = Path(token).expanduser()
        if not path.is_absolute():
            path = self._config_base_dir() / path
        return path

    def _resolve_tool_path(
        self,
        raw_path: str,
        *,
        allow_missing: bool,
        expect_directory: bool = False,
        purpose: str = "read",
    ) -> Path:
        path_text = str(raw_path or "").strip()
        if not path_text:
            raise ValueError("路径不能为空。")
        candidate = Path(path_text).expanduser()
        if not candidate.is_absolute():
            candidate = self._config_base_dir() / candidate
        resolved = candidate.resolve(strict=False)
        if not any(self._is_within_root(resolved, root) for root in self._tool_roots(purpose)):
            raise ValueError(f"路径超出允许范围：{resolved}")
        if not allow_missing and not resolved.exists():
            raise ValueError(f"路径不存在：{resolved}")
        if resolved.exists():
            if expect_directory and not resolved.is_dir():
                raise ValueError(f"路径不是目录：{resolved}")
            if not expect_directory and resolved.is_dir():
                raise ValueError(f"路径是目录不是文件：{resolved}")
        return resolved

    @staticmethod
    def _is_within_root(candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    def _normalized_command_token(self, value: str) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        name = Path(raw).name
        stem = Path(name).stem
        return stem or name

    def _allowed_exec_commands(self) -> list[str]:
        allowlist = self._service.config.tool_policy.exec_allowlist
        normalized: list[str] = []
        seen: set[str] = set()
        for item in allowlist:
            token = self._normalized_command_token(str(item or ""))
            if token and token not in seen:
                seen.add(token)
                normalized.append(token)
        return normalized

    def _list_directory_entries(self, root: Path, *, recursive: bool, limit: int) -> list[str]:
        entries: list[str] = []
        iterator = root.rglob("*") if recursive else root.iterdir()
        for item in iterator:
            relative = item.relative_to(root)
            suffix = "/" if item.is_dir() else ""
            entries.append(str(relative).replace("\\", "/") + suffix)
            if len(entries) >= limit:
                break
        return entries

    @staticmethod
    def _normalize_web_document(body: str) -> tuple[str, str]:
        raw = str(body or "")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
        title = ""
        if title_match is not None:
            title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title_match.group(1)))).strip()
        cleaned = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
        cleaned = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", cleaned)
        cleaned = re.sub(r"(?is)<!--.*?-->", " ", cleaned)
        cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned, title

    def _parse_command_arguments(self, raw_args: str, *, mode: str) -> dict[str, object]:
        normalized_mode = str(mode or "raw").strip().lower() or "raw"
        text = str(raw_args or "").strip()
        if normalized_mode == "json":
            parsed = self._safe_json_object(text)
            return parsed if parsed is not None else {}
        if normalized_mode == "kv":
            return self._parse_key_value_args(text)
        if normalized_mode == "auto":
            parsed = self._safe_json_object(text)
            if parsed is not None:
                return parsed
            return self._parse_key_value_args(text)
        return {}

    @staticmethod
    def _safe_json_object(text: str) -> dict[str, object] | None:
        payload = str(text or "").strip()
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return {str(key): value for key, value in parsed.items()}

    @staticmethod
    def _parse_key_value_args(text: str) -> dict[str, object]:
        payload: dict[str, object] = {}
        for part in re.split(r"\s+", str(text or "").strip()):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = str(key or "").strip()
            value = str(value or "").strip()
            if not key:
                continue
            payload[key] = value
        return payload

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

    def _resolve_summarize_payload(
        self,
        target: str,
        *,
        invocation: ToolInvocation,
    ) -> tuple[dict[str, object], str]:
        payload, error = self._invoke_summarize_cli(target, invocation=invocation)
        if not error:
            return payload, ""
        fallback_payload, fallback_error = self._summarize_with_runtime(
            target,
            invocation=invocation,
            cli_error=error,
        )
        if fallback_error:
            return {}, fallback_error
        fallback_payload.setdefault("runtime", {})
        runtime = fallback_payload.get("runtime", {})
        runtime = runtime if isinstance(runtime, dict) else {}
        runtime.setdefault("fallback", "internal")
        runtime.setdefault("cli_error", error)
        fallback_payload["runtime"] = runtime
        return fallback_payload, ""

    def _summarize_with_runtime(
        self,
        target: str,
        *,
        invocation: ToolInvocation,
        cli_error: str = "",
    ) -> tuple[dict[str, object], str]:
        content, title, extracted, error = self._load_summarize_source(target)
        if error:
            if cli_error:
                return {}, f"{cli_error}；内部总结回退也失败了：{error}"
            return {}, error
        if self._service.generator.llm_ready:
            prompt = self._build_summarize_fallback_query(target, content=content, title=title)
            try:
                response = self._service.generator._dify_client.invoke(
                    prompt,
                    user=self._service.generator._llm_user_key(
                        invocation.event,
                        invocation.session,
                        purpose="summarize-fallback",
                    ),
                )
                payload = self._parse_summarize_fallback_payload(
                    response,
                    target=target,
                    title=title,
                    extracted=extracted,
                )
                if payload:
                    return payload, ""
            except Exception:
                pass
        return self._fallback_summarize_payload(target, content=content, title=title, extracted=extracted), ""

    def _load_summarize_source(self, target: str) -> tuple[str, str, dict[str, object], str]:
        if re.match(r"^https?://", target, flags=re.IGNORECASE):
            transport = SyncHttpTransport(timeout_seconds=max(5.0, float(self._service.config.search_timeout_seconds or 8.0)))
            try:
                response = transport.request(
                    "GET",
                    target,
                    headers={
                        "User-Agent": "openqqwaifu-summarize/1.0",
                        "Accept": "text/html,application/json,text/plain,*/*",
                    },
                )
            except TransportError as exc:
                return "", "", {}, f"网页抓取失败：{exc}"
            finally:
                transport.close()
            content, title = self._normalize_web_document(response.text)
            clipped = self._service.generator._clip(content, limit=16000)
            if not clipped:
                return "", "", {}, "没有抓到可总结的正文。"
            site_name = self._site_name_from_target(target)
            return clipped, title, {
                "title": title,
                "url": target,
                "siteName": site_name,
                "contentType": response.headers.get("content-type", ""),
                "transcriptSource": "internal-fallback",
            }, ""

        path = Path(target)
        if not path.exists() or not path.is_file():
            return "", "", {}, f"文件不存在：{target}"
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="replace")
        clipped = self._service.generator._clip(content, limit=16000)
        if not clipped:
            return "", "", {}, "文件里没有可总结的文本。"
        return clipped, path.name, {
            "title": path.name,
            "path": str(path),
            "siteName": "local-file",
            "transcriptSource": "internal-fallback",
        }, ""

    def _build_summarize_fallback_query(self, target: str, *, content: str, title: str) -> str:
        lines = [
            "你是一个内容总结工具。",
            "请严格根据给定内容输出 JSON，不要输出额外解释。",
            '格式：{"summary":"...","title":"...","highlights":["...","...","..."]}',
            "- summary：简体中文，2 到 4 句，准确克制，不要编造。",
            "- title：如果已有标题可信就沿用，否则给空字符串。",
            "- highlights：最多 3 条，每条不超过 28 个字。",
            f"来源：{target}",
            f"已有标题：{title}",
            "",
            "正文：",
            content,
        ]
        return "\n".join(lines)

    def _parse_summarize_fallback_payload(
        self,
        response: str,
        *,
        target: str,
        title: str,
        extracted: dict[str, object],
    ) -> dict[str, object]:
        payload = self._service.generator._extract_json_block(response)
        summary = ""
        resolved_title = title
        highlights: list[str] = []
        if payload:
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                decoded = {}
            if isinstance(decoded, dict):
                summary = str(decoded.get("summary", "") or "").strip()
                resolved_title = str(decoded.get("title", "") or title).strip() or title
                raw_highlights = decoded.get("highlights", [])
                if isinstance(raw_highlights, list):
                    highlights = [
                        self._service.generator._clip(str(item or "").strip(), limit=40)
                        for item in raw_highlights
                        if str(item or "").strip()
                    ][:3]
        if not summary:
            summary = self._service.generator._clip(str(response or "").strip(), limit=480)
        if not summary:
            return {}
        merged_extracted = dict(extracted)
        if resolved_title:
            merged_extracted["title"] = resolved_title
        merged_extracted.setdefault("source", target)
        return {
            "summary": summary,
            "highlights": highlights,
            "extracted": merged_extracted,
        }

    def _fallback_summarize_payload(
        self,
        target: str,
        *,
        content: str,
        title: str,
        extracted: dict[str, object],
    ) -> dict[str, object]:
        normalized = re.sub(r"\s+", " ", str(content or "")).strip()
        summary = self._service.generator._clip(normalized, limit=220)
        if not summary:
            summary = "正文已经抓到了，但内容太少，暂时没法给出稳定摘要。"
        merged_extracted = dict(extracted)
        if title:
            merged_extracted["title"] = title
        merged_extracted.setdefault("source", target)
        return {
            "summary": summary,
            "extracted": merged_extracted,
            "runtime": {"fallback": "clip"},
        }

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

    @staticmethod
    def _site_name_from_target(target: str) -> str:
        parsed = urlparse(str(target or "").strip())
        site_name = str(parsed.netloc or "").strip().lower()
        if site_name.startswith("www."):
            site_name = site_name[4:]
        return site_name

    def _extract_weather_location(self, invocation: ToolInvocation) -> str:
        location = self._normalized_tool_text(invocation.argument("location") or invocation.raw_args)
        if location:
            cleaned = self._clean_weather_location(location)
            if cleaned:
                return cleaned
        text = self._normalized_tool_text(invocation.event.command_text(self._service.config.bot_account_id))
        if not text:
            return ""
        cleaned = self._clean_weather_location(text)
        return cleaned

    def _clean_weather_location(self, text: str) -> str:
        cleaned = self._normalized_tool_text(text)
        if not cleaned:
            return ""
        cleaned = re.sub(r"^/skill\s+weather\b", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"^weather\b", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"^forecast\b", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"^(帮我|给我|请问|麻烦你|麻烦|想知道|告诉我|我想知道|查一下|查查|看下|看看)\s*", "", cleaned)
        cleaned = re.sub(r"(天气预报|天气|气温|温度)", " ", cleaned)
        cleaned = re.sub(r"(怎么样|如何|咋样|怎样|呢|吗|吧|呀|啊|嘛|多少度|会下雨吗|会下雨不|会不会下雨)", " ", cleaned)
        cleaned = re.sub(r"^[,，:：\\-\\s]+|[,，:：\\-\\s]+$", "", cleaned)
        return self._normalized_tool_text(cleaned)

    def _parse_weather_payload(self, raw: str, *, location: str) -> tuple[dict[str, object], str]:
        try:
            decoded = json.loads(str(raw or "").strip())
        except json.JSONDecodeError:
            return {}, "天气服务返回的不是有效 JSON。"
        if not isinstance(decoded, dict):
            return {}, "天气服务返回了异常结构。"
        current_list = decoded.get("current_condition", [])
        weather_list = decoded.get("weather", [])
        nearest_list = decoded.get("nearest_area", [])
        current = current_list[0] if isinstance(current_list, list) and current_list else {}
        current = current if isinstance(current, dict) else {}
        today = weather_list[0] if isinstance(weather_list, list) and weather_list else {}
        today = today if isinstance(today, dict) else {}
        tomorrow = weather_list[1] if isinstance(weather_list, list) and len(weather_list) > 1 else {}
        tomorrow = tomorrow if isinstance(tomorrow, dict) else {}
        nearest = nearest_list[0] if isinstance(nearest_list, list) and nearest_list else {}
        nearest = nearest if isinstance(nearest, dict) else {}
        area = self._weather_area_name(nearest) or location
        description = self._weather_desc(current.get("lang_zh")) or self._weather_desc(current.get("weatherDesc"))
        temp_c = str(current.get("temp_C", "") or "").strip()
        feels_like = str(current.get("FeelsLikeC", "") or "").strip()
        humidity = str(current.get("humidity", "") or "").strip()
        wind_kmph = str(current.get("windspeedKmph", "") or "").strip()
        rain_chance = self._weather_rain_chance(today)
        if not (area and temp_c):
            return {}, "天气服务没有返回稳定的当前天气数据。"
        return {
            "location": area,
            "requested_location": location,
            "current": {
                "temp_c": temp_c,
                "feels_like_c": feels_like,
                "description": description,
                "humidity": humidity,
                "wind_kmph": wind_kmph,
            },
            "forecast": {
                "today": self._weather_forecast_day(today),
                "tomorrow": self._weather_forecast_day(tomorrow),
            },
            "rain_chance_today": rain_chance,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }, ""

    @staticmethod
    def _weather_desc(raw: object) -> str:
        if isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, dict):
                return str(first.get("value", "") or "").strip()
        if isinstance(raw, dict):
            return str(raw.get("value", "") or "").strip()
        return str(raw or "").strip()

    @staticmethod
    def _weather_area_name(raw: dict[str, object]) -> str:
        for key in ("areaName", "region", "country"):
            value = raw.get(key, [])
            text = SkillDispatcher._weather_desc(value)
            if text:
                return text
        return ""

    def _weather_forecast_day(self, raw: dict[str, object]) -> dict[str, str]:
        description = ""
        hourly = raw.get("hourly", [])
        if isinstance(hourly, list) and hourly:
            first = hourly[0]
            if isinstance(first, dict):
                description = self._weather_desc(first.get("lang_zh")) or self._weather_desc(first.get("weatherDesc"))
        return {
            "date": str(raw.get("date", "") or "").strip(),
            "max_c": str(raw.get("maxtempC", "") or "").strip(),
            "min_c": str(raw.get("mintempC", "") or "").strip(),
            "description": description,
        }

    @staticmethod
    def _weather_rain_chance(raw: dict[str, object]) -> str:
        hourly = raw.get("hourly", [])
        if not isinstance(hourly, list):
            return ""
        chances: list[int] = []
        for item in hourly:
            if not isinstance(item, dict):
                continue
            try:
                chances.append(int(item.get("chanceofrain", 0) or 0))
            except (TypeError, ValueError):
                continue
        return str(max(chances)) if chances else ""

    def _format_weather_reply(self, payload: dict[str, object], *, address: str) -> str:
        current = payload.get("current", {})
        current = current if isinstance(current, dict) else {}
        forecast = payload.get("forecast", {})
        forecast = forecast if isinstance(forecast, dict) else {}
        today = forecast.get("today", {})
        today = today if isinstance(today, dict) else {}
        tomorrow = forecast.get("tomorrow", {})
        tomorrow = tomorrow if isinstance(tomorrow, dict) else {}
        location = str(payload.get("location", "") or "这个地方").strip()
        temp_c = str(current.get("temp_c", "") or "").strip()
        feels_like = str(current.get("feels_like_c", "") or "").strip()
        description = str(current.get("description", "") or "").strip()
        humidity = str(current.get("humidity", "") or "").strip()
        wind_kmph = str(current.get("wind_kmph", "") or "").strip()
        rain_chance = str(payload.get("rain_chance_today", "") or "").strip()
        lines = [f"{address}，{location}现在 {temp_c}°C，{description or '天气一般'}。"]
        extras: list[str] = []
        if feels_like:
            extras.append(f"体感 {feels_like}°C")
        if humidity:
            extras.append(f"湿度 {humidity}%")
        if wind_kmph:
            extras.append(f"风速 {wind_kmph} km/h")
        if extras:
            lines.append("，".join(extras) + "。")
        if rain_chance:
            try:
                chance_value = int(rain_chance)
            except ValueError:
                chance_value = 0
            if chance_value >= 50:
                lines.append(f"今天下雨概率大概 {chance_value}%，出门最好带伞。")
            elif chance_value >= 20:
                lines.append(f"今天有 {chance_value}% 左右的降雨可能，外出留意一下。")
        today_line = self._format_weather_day(today, prefix="今天")
        if today_line:
            lines.append(today_line)
        tomorrow_line = self._format_weather_day(tomorrow, prefix="明天")
        if tomorrow_line:
            lines.append(tomorrow_line)
        return "\n".join(lines)

    @staticmethod
    def _format_weather_day(day: dict[str, object], *, prefix: str) -> str:
        max_c = str(day.get("max_c", "") or "").strip()
        min_c = str(day.get("min_c", "") or "").strip()
        description = str(day.get("description", "") or "").strip()
        if not (max_c or min_c or description):
            return ""
        parts: list[str] = [prefix]
        if description:
            parts.append(description)
        if max_c or min_c:
            parts.append(f"{min_c or '?'} 到 {max_c or '?'}°C")
        return "，".join(parts) + "。"

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
