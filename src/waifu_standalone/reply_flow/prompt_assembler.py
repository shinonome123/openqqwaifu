from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from ..config import AppConfig
from ..contracts import GeneratedImage
from ..character import CardManager, CharacterCard
from ..models import EmotionState, InboundEvent, SessionMemory
from ..infra import ImageClient, LLMClient, build_image_client, build_llm_client
from ..infra.image_clients import ImageClientError
from ..infra.llm_clients import LLMClientError
from ..skills import SkillSpec, ToolExecutionResult, ToolExposureContext, ToolInvocation, ToolRegistry


@dataclass(slots=True)
class GeneratedReply:
    text: str = ""
    images: list[str] = field(default_factory=list)


class Generator:
    """Prompt builder plus optional remote model clients."""

    def __init__(
        self,
        config: AppConfig,
        *,
        llm_client: LLMClient | None = None,
        image_client: ImageClient | None = None,
    ):
        self.config = config
        self._cards = CardManager(config)
        self._dify_client = llm_client or build_llm_client(config)
        self._image_client = image_client or build_image_client(config)
        self._tools: ToolRegistry | None = None

    @property
    def llm_ready(self) -> bool:
        return self.config.llm.enabled and self._llm_client.enabled

    @property
    def _llm_client(self) -> LLMClient:
        return self._dify_client

    def bind_tools(self, tools: ToolRegistry) -> None:
        self._tools = tools

    @property
    def image_ready(self) -> bool:
        return self.config.image_generation.enabled and self._image_client.enabled

    def close(self) -> None:
        for client in (self._dify_client, self._image_client):
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def resolve_assistant_name(self, launcher_type: str, session: SessionMemory) -> str:
        card = self._cards.load(launcher_type, session)
        return card.assistant_name or self.config.assistant_name

    @staticmethod
    def _card_with_assistant_name(card: CharacterCard, assistant_name: str) -> CharacterCard:
        resolved_name = str(assistant_name or "").strip()
        if not resolved_name or resolved_name == card.assistant_name:
            return card
        return replace(card, assistant_name=resolved_name)

    @staticmethod
    def _llm_user_key(event: InboundEvent, session: SessionMemory, *, purpose: str) -> str:
        parts = [
            str(purpose or "chat").strip() or "chat",
            str(getattr(session, "character_id", "") or "").strip() or "default",
            str(event.launcher_type or "").strip() or "person",
            str(event.launcher_id or "").strip() or "unknown",
            str(event.sender_id or "").strip() or "waifu-user",
        ]
        return ":".join(part.replace(":", "_") for part in parts)

    async def _ainvoke_client(self, query: str, *, user: str) -> str:
        client_state = getattr(self._dify_client, "__dict__", {})
        if "invoke" in client_state and "ainvoke" not in client_state:
            return await asyncio.to_thread(self._dify_client.invoke, query, user=user)
        ainvoke = getattr(self._dify_client, "ainvoke", None)
        if callable(ainvoke):
            return await ainvoke(query, user=user)
        return await asyncio.to_thread(self._dify_client.invoke, query, user=user)

    def generate_analysis(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        conversation_view: str,
        memory_hints: list[str],
        speaker_notes: list[str],
        active_skills: list[SkillSpec] | None = None,
        address_override: str = "",
        card_override: CharacterCard | None = None,
        allow_fallback: bool = True,
    ) -> str:
        card = card_override or self._cards.load(event.launcher_type, session)
        latest_message = event.command_text(self.config.bot_account_id).strip() or event.to_memory_text()
        address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        active_skills = active_skills or []
        card = self._card_with_assistant_name(card, assistant_name)
        if self.llm_ready:
            prompt = self._build_analysis_query(
                event,
                card=card,
                assistant_name=assistant_name,
                address=address,
                conversation_view=conversation_view,
                memory_hints=memory_hints,
                speaker_notes=speaker_notes,
                latest_message=latest_message,
                active_skills=active_skills,
            )
            try:
                response = self._dify_client.invoke(
                    prompt,
                    user=self._llm_user_key(event, session, purpose="analysis"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return self._clip(cleaned, limit=self.config.max_thinking_words)
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_analysis(event, latest_message, memory_hints, speaker_notes)
        return ""

    async def agenerate_analysis(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        conversation_view: str,
        memory_hints: list[str],
        speaker_notes: list[str],
        active_skills: list[SkillSpec] | None = None,
        address_override: str = "",
        card_override: CharacterCard | None = None,
        allow_fallback: bool = True,
    ) -> str:
        if "generate_analysis" in getattr(self, "__dict__", {}) and "agenerate_analysis" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
                self.generate_analysis,
                event,
                session,
                assistant_name=assistant_name,
                conversation_view=conversation_view,
                memory_hints=memory_hints,
                speaker_notes=speaker_notes,
                active_skills=active_skills,
                address_override=address_override,
                card_override=card_override,
                allow_fallback=allow_fallback,
            )
        card = card_override or self._cards.load(event.launcher_type, session)
        latest_message = event.command_text(self.config.bot_account_id).strip() or event.to_memory_text()
        address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        active_skills = active_skills or []
        card = self._card_with_assistant_name(card, assistant_name)
        if self.llm_ready:
            prompt = self._build_analysis_query(
                event,
                card=card,
                assistant_name=assistant_name,
                address=address,
                conversation_view=conversation_view,
                memory_hints=memory_hints,
                speaker_notes=speaker_notes,
                latest_message=latest_message,
                active_skills=active_skills,
            )
            try:
                response = await self._ainvoke_client(
                    prompt,
                    user=self._llm_user_key(event, session, purpose="analysis"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return self._clip(cleaned, limit=self.config.max_thinking_words)
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_analysis(event, latest_message, memory_hints, speaker_notes)
        return ""

    def generate_reply_message(
        self,
        event: InboundEvent,
        session: SessionMemory,
        emotion: EmotionState,
        *,
        assistant_name: str,
        address_override: str = "",
        card_override: CharacterCard | None = None,
        search_hint: str = "",
        search_context: str = "",
        conversation_view: str = "",
        memory_hints: list[str] | None = None,
        speaker_notes: list[str] | None = None,
        analysis_hint: str = "",
        active_skills: list[SkillSpec] | None = None,
        tool_context: ToolExposureContext | None = None,
        allow_fallback: bool = True,
    ) -> GeneratedReply:
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        latest_message = event.command_text(self.config.bot_account_id).strip() or event.to_memory_text()
        memory_hints = memory_hints or []
        speaker_notes = speaker_notes or []
        active_skills = active_skills or []

        if self.llm_ready:
            prompt = self._build_chat_query(
                event,
                session,
                emotion,
                card=card,
                assistant_name=resolved_assistant_name,
                address=address,
                search_hint=search_hint,
                search_context=search_context,
                conversation_view=conversation_view,
                memory_hints=memory_hints,
                speaker_notes=speaker_notes,
                analysis_hint=analysis_hint,
                latest_message=latest_message,
                active_skills=active_skills,
            )
            try:
                reply = self._generate_model_reply_message(
                    prompt,
                    event=event,
                    session=session,
                    assistant_name=resolved_assistant_name,
                    address=address,
                    active_skills=active_skills,
                    tool_context=tool_context,
                    user=self._llm_user_key(event, session, purpose="chat"),
                )
                if reply.text.strip() or reply.images:
                    return reply
            except LLMClientError:
                pass
        if allow_fallback:
            return GeneratedReply(
                text=self._fallback_reply(
                    event,
                    session,
                    emotion,
                    card=card,
                    assistant_name=resolved_assistant_name,
                    address=address,
                    search_hint=search_hint,
                    memory_hints=memory_hints,
                    analysis_hint=analysis_hint,
                )
            )
        return GeneratedReply()

    async def agenerate_reply_message(
        self,
        event: InboundEvent,
        session: SessionMemory,
        emotion: EmotionState,
        *,
        assistant_name: str,
        address_override: str = "",
        card_override: CharacterCard | None = None,
        search_hint: str = "",
        search_context: str = "",
        conversation_view: str = "",
        memory_hints: list[str] | None = None,
        speaker_notes: list[str] | None = None,
        analysis_hint: str = "",
        active_skills: list[SkillSpec] | None = None,
        tool_context: ToolExposureContext | None = None,
        allow_fallback: bool = True,
    ) -> GeneratedReply:
        if (
            "generate_reply" in getattr(self, "__dict__", {})
            and "agenerate_reply_message" not in getattr(self, "__dict__", {})
            and not self._tool_calling_ready(tool_context)
        ):
            result = await asyncio.to_thread(
                self.generate_reply,
                event,
                session,
                emotion,
                assistant_name=assistant_name,
                address_override=address_override,
                card_override=card_override,
                search_hint=search_hint,
                search_context=search_context,
                conversation_view=conversation_view,
                memory_hints=memory_hints,
                speaker_notes=speaker_notes,
                analysis_hint=analysis_hint,
                active_skills=active_skills,
                allow_fallback=allow_fallback,
            )
            return self._coerce_generated_reply(result)
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        latest_message = event.command_text(self.config.bot_account_id).strip() or event.to_memory_text()
        memory_hints = memory_hints or []
        speaker_notes = speaker_notes or []
        active_skills = active_skills or []

        if self.llm_ready:
            prompt = self._build_chat_query(
                event,
                session,
                emotion,
                card=card,
                assistant_name=resolved_assistant_name,
                address=address,
                search_hint=search_hint,
                search_context=search_context,
                conversation_view=conversation_view,
                memory_hints=memory_hints,
                speaker_notes=speaker_notes,
                analysis_hint=analysis_hint,
                latest_message=latest_message,
                active_skills=active_skills,
            )
            try:
                reply = await self._agenerate_model_reply_message(
                    prompt,
                    event=event,
                    session=session,
                    assistant_name=resolved_assistant_name,
                    address=address,
                    active_skills=active_skills,
                    tool_context=tool_context,
                    user=self._llm_user_key(event, session, purpose="chat"),
                )
                if reply.text.strip() or reply.images:
                    return reply
            except LLMClientError:
                pass
        if allow_fallback:
            return GeneratedReply(
                text=self._fallback_reply(
                    event,
                    session,
                    emotion,
                    card=card,
                    assistant_name=resolved_assistant_name,
                    address=address,
                    search_hint=search_hint,
                    memory_hints=memory_hints,
                    analysis_hint=analysis_hint,
                )
            )
        return GeneratedReply()

    @staticmethod
    def _coerce_generated_reply(value: object) -> GeneratedReply:
        if isinstance(value, GeneratedReply):
            return value
        if isinstance(value, dict):
            return GeneratedReply(
                text=str(value.get("text", "") or "").strip(),
                images=[str(item) for item in value.get("images", []) if str(item).strip()]
                if isinstance(value.get("images"), list)
                else [],
            )
        return GeneratedReply(text=str(value or "").strip())

    def _tool_calling_ready(self, tool_context: ToolExposureContext | None = None) -> bool:
        return bool(
            self.llm_ready
            and self._tools is not None
            and getattr(self._llm_client, "supports_tool_calling", False)
            and self._tools.model_schemas(tool_context)
        )

    def _generate_model_reply_message(
        self,
        prompt: str,
        *,
        event: InboundEvent,
        session: SessionMemory,
        assistant_name: str,
        address: str,
        active_skills: list[SkillSpec],
        tool_context: ToolExposureContext | None,
        user: str,
    ) -> GeneratedReply:
        if self._tool_calling_ready(tool_context):
            try:
                reply = self._invoke_reply_with_tools(
                    prompt,
                    event=event,
                    session=session,
                    assistant_name=assistant_name,
                    address=address,
                    active_skills=active_skills,
                    tool_context=tool_context,
                    user=user,
                )
                if reply.text.strip() or reply.images:
                    return reply
            except LLMClientError:
                pass
        response = self._dify_client.invoke(prompt, user=user)
        cleaned = self._clean_response(response)
        return GeneratedReply(text=cleaned)

    async def _agenerate_model_reply_message(
        self,
        prompt: str,
        *,
        event: InboundEvent,
        session: SessionMemory,
        assistant_name: str,
        address: str,
        active_skills: list[SkillSpec],
        tool_context: ToolExposureContext | None,
        user: str,
    ) -> GeneratedReply:
        if self._tool_calling_ready(tool_context):
            try:
                reply = await self._ainvoke_reply_with_tools(
                    prompt,
                    event=event,
                    session=session,
                    assistant_name=assistant_name,
                    address=address,
                    active_skills=active_skills,
                    tool_context=tool_context,
                    user=user,
                )
                if reply.text.strip() or reply.images:
                    return reply
            except LLMClientError:
                pass
        response = await self._ainvoke_client(prompt, user=user)
        cleaned = self._clean_response(response)
        return GeneratedReply(text=cleaned)

    def _invoke_reply_with_tools(
        self,
        prompt: str,
        *,
        event: InboundEvent,
        session: SessionMemory,
        assistant_name: str,
        address: str,
        active_skills: list[SkillSpec],
        tool_context: ToolExposureContext | None,
        user: str,
    ) -> GeneratedReply:
        if self._tools is None:
            raise LLMClientError("tool registry is not bound")
        tools = self._tools.model_schemas(tool_context)
        messages = [
            {
                "role": "system",
                "content": (
                    "如果需要最新事实、读取内容、联网获取信息或执行具体能力，"
                    "必须优先调用工具，不能假装已经执行。工具返回后，再自然地用中文完成最终回复。"
                    "如果工具返回 display_mode=raw_block，最终回复可以有角色语气承接，但原始结果必须保留在代码块中。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        images: list[str] = []
        raw_block_results: list[ToolExecutionResult] = []
        tool_turns: list[dict[str, object]] = []
        for _ in range(3):
            response = self._llm_client.invoke_with_tools(messages, tools=tools, user=user)
            if response.assistant_message:
                messages.append(response.assistant_message)
            if not response.tool_calls:
                self._record_tool_turns(session, tool_turns)
                text = self._append_raw_tool_blocks(self._clean_response(response.text), raw_block_results)
                return GeneratedReply(text=text, images=images)
            for tool_call in response.tool_calls:
                invocation = ToolInvocation(
                    tool_id=tool_call.tool_name,
                    raw_args=json.dumps(tool_call.arguments, ensure_ascii=False) if tool_call.arguments else "",
                    event=event,
                    session=session,
                    address=address,
                    assistant_name=assistant_name,
                    active_skills=active_skills,
                    arguments=tool_call.arguments,
                )
                result = self._tools.execute_model(tool_call.tool_name, invocation)
                tool_turns.append(self._tool_turn_record(tool_call.tool_name, tool_call.arguments, result))
                for image_ref in result.images:
                    if image_ref not in images:
                        images.append(image_ref)
                if result.display_mode == "raw_block":
                    raw_block_results.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.call_id,
                        "name": tool_call.tool_name,
                        "content": result.format_for_model(limit=4000),
                    }
                )
        self._record_tool_turns(session, tool_turns)
        return GeneratedReply(
            text=self._append_raw_tool_blocks("", raw_block_results),
            images=images,
        )

    async def _ainvoke_reply_with_tools(
        self,
        prompt: str,
        *,
        event: InboundEvent,
        session: SessionMemory,
        assistant_name: str,
        address: str,
        active_skills: list[SkillSpec],
        tool_context: ToolExposureContext | None,
        user: str,
    ) -> GeneratedReply:
        if self._tools is None:
            raise LLMClientError("tool registry is not bound")
        tools = self._tools.model_schemas(tool_context)
        messages = [
            {
                "role": "system",
                "content": (
                    "如果需要最新事实、读取内容、联网获取信息或执行具体能力，"
                    "必须优先调用工具，不能假装已经执行。工具返回后，再自然地用中文完成最终回复。"
                    "如果工具返回 display_mode=raw_block，最终回复可以有角色语气承接，但原始结果必须保留在代码块中。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        images: list[str] = []
        raw_block_results: list[ToolExecutionResult] = []
        tool_turns: list[dict[str, object]] = []
        for _ in range(3):
            response = await self._llm_client.ainvoke_with_tools(messages, tools=tools, user=user)
            if response.assistant_message:
                messages.append(response.assistant_message)
            if not response.tool_calls:
                self._record_tool_turns(session, tool_turns)
                text = self._append_raw_tool_blocks(self._clean_response(response.text), raw_block_results)
                return GeneratedReply(text=text, images=images)
            for tool_call in response.tool_calls:
                invocation = ToolInvocation(
                    tool_id=tool_call.tool_name,
                    raw_args=json.dumps(tool_call.arguments, ensure_ascii=False) if tool_call.arguments else "",
                    event=event,
                    session=session,
                    address=address,
                    assistant_name=assistant_name,
                    active_skills=active_skills,
                    arguments=tool_call.arguments,
                )
                result = await self._tools.aexecute_model(tool_call.tool_name, invocation)
                tool_turns.append(self._tool_turn_record(tool_call.tool_name, tool_call.arguments, result))
                for image_ref in result.images:
                    if image_ref not in images:
                        images.append(image_ref)
                if result.display_mode == "raw_block":
                    raw_block_results.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.call_id,
                        "name": tool_call.tool_name,
                        "content": result.format_for_model(limit=4000),
                    }
                )
        self._record_tool_turns(session, tool_turns)
        return GeneratedReply(
            text=self._append_raw_tool_blocks("", raw_block_results),
            images=images,
        )

    def generate_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        emotion: EmotionState,
        *,
        assistant_name: str,
        address_override: str = "",
        card_override: CharacterCard | None = None,
        search_hint: str = "",
        search_context: str = "",
        conversation_view: str = "",
        memory_hints: list[str] | None = None,
        speaker_notes: list[str] | None = None,
        analysis_hint: str = "",
        active_skills: list[SkillSpec] | None = None,
        allow_fallback: bool = True,
    ) -> str:
        return self.generate_reply_message(
            event,
            session,
            emotion,
            assistant_name=assistant_name,
            address_override=address_override,
            card_override=card_override,
            search_hint=search_hint,
            search_context=search_context,
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            analysis_hint=analysis_hint,
            active_skills=active_skills,
            allow_fallback=allow_fallback,
        ).text

    async def agenerate_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        emotion: EmotionState,
        *,
        assistant_name: str,
        address_override: str = "",
        card_override: CharacterCard | None = None,
        search_hint: str = "",
        search_context: str = "",
        conversation_view: str = "",
        memory_hints: list[str] | None = None,
        speaker_notes: list[str] | None = None,
        analysis_hint: str = "",
        active_skills: list[SkillSpec] | None = None,
        allow_fallback: bool = True,
    ) -> str:
        reply = await self.agenerate_reply_message(
            event,
            session,
            emotion,
            assistant_name=assistant_name,
            address_override=address_override,
            card_override=card_override,
            search_hint=search_hint,
            search_context=search_context,
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            analysis_hint=analysis_hint,
            active_skills=active_skills,
            allow_fallback=allow_fallback,
        )
        return reply.text

    def summarize_history(
        self,
        history_lines: list[str],
        *,
        assistant_name: str,
    ) -> tuple[str, list[str]]:
        if not history_lines:
            return "", []
        if self.llm_ready:
            prompt = self._build_summary_query(history_lines, assistant_name=assistant_name)
            try:
                response = self._dify_client.invoke(prompt, user="summary")
                summary, tags = self._parse_summary_payload(response)
                if summary:
                    return summary, tags
            except LLMClientError:
                pass
        return self._fallback_summary(history_lines)

    async def asummarize_history(
        self,
        history_lines: list[str],
        *,
        assistant_name: str,
    ) -> tuple[str, list[str]]:
        if "summarize_history" in getattr(self, "__dict__", {}) and "asummarize_history" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(self.summarize_history, history_lines, assistant_name=assistant_name)
        if not history_lines:
            return "", []
        if self.llm_ready:
            prompt = self._build_summary_query(history_lines, assistant_name=assistant_name)
            try:
                response = await self._ainvoke_client(prompt, user="summary")
                summary, tags = self._parse_summary_payload(response)
                if summary:
                    return summary, tags
            except LLMClientError:
                pass
        return self._fallback_summary(history_lines)

    def extract_knowledge(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        latest_message: str,
        conversation_view: str,
        address: str = "",
        max_entries: int = 2,
        allow_fallback: bool = True,
    ) -> dict[str, Any]:
        cleaned_message = " ".join(str(latest_message or "").split())
        if not cleaned_message:
            return {"entries": [], "profile_summary": ""}
        if self.llm_ready:
            prompt = self._build_knowledge_query(
                event,
                session,
                assistant_name=assistant_name,
                latest_message=cleaned_message,
                conversation_view=conversation_view,
                address=address,
                max_entries=max_entries,
            )
            try:
                response = self._dify_client.invoke(
                    prompt,
                    user=self._llm_user_key(event, session, purpose="knowledge"),
                )
                parsed = self._parse_knowledge_payload(response, max_entries=max_entries)
                if parsed["entries"] or parsed["profile_summary"]:
                    return parsed
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_extract_knowledge(cleaned_message, max_entries=max_entries)
        return {"entries": [], "profile_summary": ""}

    async def aextract_knowledge(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        latest_message: str,
        conversation_view: str,
        address: str = "",
        max_entries: int = 2,
        allow_fallback: bool = True,
    ) -> dict[str, Any]:
        cleaned_message = " ".join(str(latest_message or "").split())
        if not cleaned_message:
            return {"entries": [], "profile_summary": ""}
        if self.llm_ready:
            prompt = self._build_knowledge_query(
                event,
                session,
                assistant_name=assistant_name,
                latest_message=cleaned_message,
                conversation_view=conversation_view,
                address=address,
                max_entries=max_entries,
            )
            try:
                response = await self._ainvoke_client(
                    prompt,
                    user=self._llm_user_key(event, session, purpose="knowledge"),
                )
                parsed = self._parse_knowledge_payload(response, max_entries=max_entries)
                if parsed["entries"] or parsed["profile_summary"]:
                    return parsed
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_extract_knowledge(cleaned_message, max_entries=max_entries)
        return {"entries": [], "profile_summary": ""}

    def generate_image(self, prompt: str) -> GeneratedImage:
        cleaned = str(prompt or "").strip()
        if not cleaned:
            raise ValueError("image prompt is empty")
        if self.image_ready:
            try:
                return GeneratedImage(prompt=cleaned, image_ref=self._image_client.generate(cleaned))
            except ImageClientError as exc:
                raise ValueError(str(exc)) from exc
        return GeneratedImage(prompt=cleaned, image_ref=f"generated://{cleaned}")

    async def agenerate_image(self, prompt: str) -> GeneratedImage:
        if "generate_image" in getattr(self, "__dict__", {}) and "agenerate_image" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(self.generate_image, prompt)
        cleaned = str(prompt or "").strip()
        if not cleaned:
            raise ValueError("image prompt is empty")
        if self.image_ready:
            try:
                return GeneratedImage(
                    prompt=cleaned,
                    image_ref=await self._image_client.agenerate(cleaned),
                )
            except ImageClientError as exc:
                raise ValueError(str(exc)) from exc
        return GeneratedImage(prompt=cleaned, image_ref=f"generated://{cleaned}")

    def resolve_generated_image(self, image_ref: str) -> tuple[bytes, str]:
        return self._image_client.resolve_image(image_ref)

    def generate_image_caption(
        self,
        prompt: str,
        *,
        launcher_type: str = "person",
        session: SessionMemory | None = None,
        address: str = "你",
        assistant_name: str = "",
        active_skills: list[SkillSpec] | None = None,
    ) -> str:
        card = None
        if session is not None:
            card = self._cards.load(launcher_type, session)
        resolved_assistant_name = assistant_name or (card.assistant_name if card else "") or self.config.assistant_name
        active_skills = active_skills or []
        if self.llm_ready:
            request = (
                f"你是{resolved_assistant_name}。图片已经生成成功。\n"
                f"用户称呼：{address}\n"
                f"用户请求：{prompt}\n"
                "请只用一句自然中文回复，告诉对方图片已经好了，并带一点角色语气。"
            )
            skill_block = self._format_skill_block(active_skills)
            if skill_block:
                request = request + "\n\n" + skill_block
            try:
                response = self._dify_client.invoke(request, user="image-caption")
                cleaned = self._clean_response(response)
                if cleaned:
                    return cleaned
            except LLMClientError:
                pass
        return f"{address}要的图片生成好了~是一个“{self._clip(prompt)}”呢。"

    async def agenerate_image_caption(
        self,
        prompt: str,
        *,
        launcher_type: str = "person",
        session: SessionMemory | None = None,
        address: str = "你",
        assistant_name: str = "",
        active_skills: list[SkillSpec] | None = None,
    ) -> str:
        if "generate_image_caption" in getattr(self, "__dict__", {}) and "agenerate_image_caption" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
                self.generate_image_caption,
                prompt,
                launcher_type=launcher_type,
                session=session,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
            )
        card = None
        if session is not None:
            card = self._cards.load(launcher_type, session)
        resolved_assistant_name = assistant_name or (card.assistant_name if card else "") or self.config.assistant_name
        active_skills = active_skills or []
        if self.llm_ready:
            request = (
                f"你是{resolved_assistant_name}。图片已经生成成功。\n"
                f"用户称呼：{address}\n"
                f"用户请求：{prompt}\n"
                "请只用一句自然中文回复，告诉对方图片已经好了，并带一点角色语气。"
            )
            skill_block = self._format_skill_block(active_skills)
            if skill_block:
                request = request + "\n\n" + skill_block
            try:
                response = await self._ainvoke_client(request, user="image-caption")
                cleaned = self._clean_response(response)
                if cleaned:
                    return cleaned
            except LLMClientError:
                pass
        return f"{address}要的图片生成好了~是一个“{self._clip(prompt)}”呢。"

    def generate_onboarding_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        stage: str,
        candidate_name: str = "",
        intent_hint: str = "",
        address_override: str = "",
        card_override: CharacterCard | None = None,
        allow_fallback: bool = True,
    ) -> str:
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        base_address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        latest_message = event.command_text(self.config.bot_account_id).strip() or event.to_memory_text()
        if self.llm_ready:
            prompt = self._build_onboarding_query(
                event,
                session,
                card=card,
                assistant_name=resolved_assistant_name,
                address=base_address,
                latest_message=latest_message,
                stage=stage,
                candidate_name=candidate_name,
                intent_hint=intent_hint,
            )
            try:
                response = self._dify_client.invoke(
                    prompt,
                    user=self._llm_user_key(event, session, purpose=f"onboarding-{stage}"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return cleaned
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_onboarding_reply(
                stage=stage,
                address=base_address,
                candidate_name=candidate_name,
                intent_hint=intent_hint,
            )
        return ""

    async def agenerate_onboarding_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        stage: str,
        candidate_name: str = "",
        intent_hint: str = "",
        address_override: str = "",
        card_override: CharacterCard | None = None,
        allow_fallback: bool = True,
    ) -> str:
        if "generate_onboarding_reply" in getattr(self, "__dict__", {}) and "agenerate_onboarding_reply" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
                self.generate_onboarding_reply,
                event,
                session,
                assistant_name=assistant_name,
                stage=stage,
                candidate_name=candidate_name,
                intent_hint=intent_hint,
                address_override=address_override,
                card_override=card_override,
                allow_fallback=allow_fallback,
            )
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        base_address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        latest_message = event.command_text(self.config.bot_account_id).strip() or event.to_memory_text()
        if self.llm_ready:
            prompt = self._build_onboarding_query(
                event,
                session,
                card=card,
                assistant_name=resolved_assistant_name,
                address=base_address,
                latest_message=latest_message,
                stage=stage,
                candidate_name=candidate_name,
                intent_hint=intent_hint,
            )
            try:
                response = await self._ainvoke_client(
                    prompt,
                    user=self._llm_user_key(event, session, purpose=f"onboarding-{stage}"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return cleaned
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_onboarding_reply(
                stage=stage,
                address=base_address,
                candidate_name=candidate_name,
                intent_hint=intent_hint,
            )
        return ""

    def extract_preferred_name_hint(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        latest_message: str,
        address_override: str = "",
        card_override: CharacterCard | None = None,
    ) -> dict[str, Any]:
        if not self.llm_ready:
            return self._empty_preferred_name_hint()
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        prompt = self._build_preferred_name_hint_query(
            event,
            session,
            card=card,
            assistant_name=resolved_assistant_name,
            address=address,
            latest_message=latest_message,
        )
        try:
            response = self._dify_client.invoke(
                prompt,
                user=self._llm_user_key(event, session, purpose="preferred-name-hint"),
            )
            return self._parse_preferred_name_hint_payload(response)
        except LLMClientError:
            return self._empty_preferred_name_hint()

    async def aextract_preferred_name_hint(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        latest_message: str,
        address_override: str = "",
        card_override: CharacterCard | None = None,
    ) -> dict[str, Any]:
        if "extract_preferred_name_hint" in getattr(self, "__dict__", {}) and "aextract_preferred_name_hint" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
                self.extract_preferred_name_hint,
                event,
                session,
                assistant_name=assistant_name,
                latest_message=latest_message,
                address_override=address_override,
                card_override=card_override,
            )
        if not self.llm_ready:
            return self._empty_preferred_name_hint()
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        address = str(address_override or "").strip() or self._resolve_address(event, session, card)
        prompt = self._build_preferred_name_hint_query(
            event,
            session,
            card=card,
            assistant_name=resolved_assistant_name,
            address=address,
            latest_message=latest_message,
        )
        try:
            response = await self._ainvoke_client(
                prompt,
                user=self._llm_user_key(event, session, purpose="preferred-name-hint"),
            )
            return self._parse_preferred_name_hint_payload(response)
        except LLMClientError:
            return self._empty_preferred_name_hint()

    def resolve_skill_intent(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        address: str,
        latest_message: str,
        candidate_skills: list[dict[str, object]],
    ) -> dict[str, Any]:
        if not self.llm_ready or not candidate_skills:
            return self._empty_intent_route()
        card = self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        prompt = self._build_intent_router_query(
            event,
            session,
            card=card,
            assistant_name=resolved_assistant_name,
            address=address,
            latest_message=latest_message,
            candidate_skills=candidate_skills,
        )
        try:
            response = self._dify_client.invoke(
                prompt,
                user=self._llm_user_key(event, session, purpose="intent-router"),
            )
            return self._parse_intent_route_payload(response)
        except LLMClientError:
            return self._empty_intent_route()

    def resolve_naming_intent(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        latest_message: str,
        onboarding_status: str,
        preferred_name: str,
        passive_capture_allowed: bool,
    ) -> dict[str, Any]:
        if not self.llm_ready:
            return self._empty_naming_intent_route()
        card = self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        prompt = self._build_naming_intent_router_query(
            event,
            session,
            card=card,
            assistant_name=resolved_assistant_name,
            latest_message=latest_message,
            onboarding_status=onboarding_status,
            preferred_name=preferred_name,
            passive_capture_allowed=passive_capture_allowed,
        )
        try:
            response = self._dify_client.invoke(
                prompt,
                user=self._llm_user_key(event, session, purpose="naming-intent-router"),
            )
            return self._parse_naming_intent_route_payload(response)
        except LLMClientError:
            return self._empty_naming_intent_route()

    async def aresolve_naming_intent(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        latest_message: str,
        onboarding_status: str,
        preferred_name: str,
        passive_capture_allowed: bool,
    ) -> dict[str, Any]:
        if "resolve_naming_intent" in getattr(self, "__dict__", {}) and "aresolve_naming_intent" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
                self.resolve_naming_intent,
                event,
                session,
                assistant_name=assistant_name,
                latest_message=latest_message,
                onboarding_status=onboarding_status,
                preferred_name=preferred_name,
                passive_capture_allowed=passive_capture_allowed,
            )
        if not self.llm_ready:
            return self._empty_naming_intent_route()
        card = self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        prompt = self._build_naming_intent_router_query(
            event,
            session,
            card=card,
            assistant_name=resolved_assistant_name,
            latest_message=latest_message,
            onboarding_status=onboarding_status,
            preferred_name=preferred_name,
            passive_capture_allowed=passive_capture_allowed,
        )
        try:
            response = await self._ainvoke_client(
                prompt,
                user=self._llm_user_key(event, session, purpose="naming-intent-router"),
            )
            return self._parse_naming_intent_route_payload(response)
        except LLMClientError:
            return self._empty_naming_intent_route()

    async def aresolve_skill_intent(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        address: str,
        latest_message: str,
        candidate_skills: list[dict[str, object]],
    ) -> dict[str, Any]:
        if "resolve_skill_intent" in getattr(self, "__dict__", {}) and "aresolve_skill_intent" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
                self.resolve_skill_intent,
                event,
                session,
                assistant_name=assistant_name,
                address=address,
                latest_message=latest_message,
                candidate_skills=candidate_skills,
            )
        if not self.llm_ready or not candidate_skills:
            return self._empty_intent_route()
        card = self._cards.load(event.launcher_type, session)
        resolved_assistant_name = assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        prompt = self._build_intent_router_query(
            event,
            session,
            card=card,
            assistant_name=resolved_assistant_name,
            address=address,
            latest_message=latest_message,
            candidate_skills=candidate_skills,
        )
        try:
            response = await self._ainvoke_client(
                prompt,
                user=self._llm_user_key(event, session, purpose="intent-router"),
            )
            return self._parse_intent_route_payload(response)
        except LLMClientError:
            return self._empty_intent_route()

    def generate_tool_reply_message(
        self,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
        *,
        allow_fallback: bool = True,
    ) -> GeneratedReply:
        event = invocation.event
        session = invocation.session
        card = self._cards.load(event.launcher_type, session)
        resolved_assistant_name = invocation.assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        address = str(invocation.address or "").strip() or self._resolve_address(event, session, card)
        if self.llm_ready and not result.metadata.get("already_persona"):
            prompt = self._build_tool_render_query(
                invocation,
                result,
                card=card,
                assistant_name=resolved_assistant_name,
                address=address,
            )
            try:
                response = self._dify_client.invoke(
                    prompt,
                    user=self._llm_user_key(event, session, purpose=f"tool-render-{invocation.tool_id}"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return GeneratedReply(text=cleaned, images=list(result.images))
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_tool_reply(
                invocation,
                result,
                card=card,
                assistant_name=resolved_assistant_name,
                address=address,
            )
        return GeneratedReply(images=list(result.images))

    async def agenerate_tool_reply_message(
        self,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
        *,
        allow_fallback: bool = True,
    ) -> GeneratedReply:
        if "generate_tool_reply_message" in getattr(self, "__dict__", {}) and "agenerate_tool_reply_message" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
                self.generate_tool_reply_message,
                invocation,
                result,
                allow_fallback=allow_fallback,
            )
        event = invocation.event
        session = invocation.session
        card = self._cards.load(event.launcher_type, session)
        resolved_assistant_name = invocation.assistant_name or card.assistant_name or self.config.assistant_name
        card = self._card_with_assistant_name(card, resolved_assistant_name)
        address = str(invocation.address or "").strip() or self._resolve_address(event, session, card)
        if self.llm_ready and not result.metadata.get("already_persona"):
            prompt = self._build_tool_render_query(
                invocation,
                result,
                card=card,
                assistant_name=resolved_assistant_name,
                address=address,
            )
            try:
                response = await self._ainvoke_client(
                    prompt,
                    user=self._llm_user_key(event, session, purpose=f"tool-render-{invocation.tool_id}"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return GeneratedReply(text=cleaned, images=list(result.images))
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_tool_reply(
                invocation,
                result,
                card=card,
                assistant_name=resolved_assistant_name,
                address=address,
            )
        return GeneratedReply(images=list(result.images))

    def _build_analysis_query(
        self,
        event: InboundEvent,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
        conversation_view: str,
        memory_hints: list[str],
        speaker_notes: list[str],
        latest_message: str,
        active_skills: list[SkillSpec],
    ) -> str:
        lines = [
            f"你是{assistant_name}，正在分析当前对话。",
            f"用户称呼倾向：{address}",
            f"当前最新消息：{latest_message}",
            f"角色设定摘要：{'；'.join(card.profile[:3]) or '无'}",
        ]
        if conversation_view:
            lines.append("[最近对话]\n" + conversation_view)
        if memory_hints:
            lines.append("[相关记忆]\n" + "\n".join(f"- {item}" for item in memory_hints))
        if speaker_notes:
            lines.append("[说话人备注]\n" + "\n".join(f"- {item}" for item in speaker_notes))
        skill_block = self._format_skill_block(active_skills)
        if skill_block:
            lines.append(skill_block)
        lines.append(
            f"请站在{assistant_name}的角度，用不超过{self.config.max_thinking_words}个字，概括这句消息的意图和最合适的回应方向。只输出分析。"
        )
        return "\n\n".join(lines)

    def _build_chat_query(
        self,
        event: InboundEvent,
        session: SessionMemory,
        emotion: EmotionState,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
        search_hint: str,
        search_context: str,
        conversation_view: str,
        memory_hints: list[str],
        speaker_notes: list[str],
        analysis_hint: str,
        latest_message: str,
        active_skills: list[SkillSpec],
    ) -> str:
        system_prompt = card.system_prompt(
            launcher_type=event.launcher_type,
            address=address,
            memories=memory_hints,
            emotion=emotion,
            search_hint=search_hint,
            conversation_view=conversation_view,
            speaker_notes=speaker_notes,
            latest_message=latest_message,
        )
        prompt_parts = [
            system_prompt,
            f"现在请作为{assistant_name}继续回复。",
        ]
        story_block = self._build_story_mode_block(
            event,
            card=card,
            emotion=emotion,
            address=address,
            latest_message=latest_message,
        )
        if story_block:
            prompt_parts.append(story_block)
        if analysis_hint:
            prompt_parts.append("[Inner Thought]\n" + analysis_hint)
        if card.prologue and len(session.history) <= 1:
            prompt_parts.append("[Opening Tone]\n" + "\n".join(f"- {line}" for line in card.prologue))
        if search_context:
            prompt_parts.append(search_context)
        skill_block = self._format_skill_block(active_skills)
        if skill_block:
            prompt_parts.append(skill_block)
        return "\n\n".join(part for part in prompt_parts if part.strip())

    def _build_onboarding_query(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
        latest_message: str,
        stage: str,
        candidate_name: str,
        intent_hint: str,
    ) -> str:
        conversation_view = self._conversation_excerpt(session.history, assistant_name=assistant_name)
        system_prompt = card.system_prompt(
            launcher_type=event.launcher_type,
            address=candidate_name or address,
            memories=[],
            emotion=EmotionState(),
            search_hint="",
            conversation_view=conversation_view,
            speaker_notes=[],
            latest_message=latest_message,
        )
        parts = [system_prompt, "[Onboarding]"]
        if stage == "ask_name":
            parts.extend(
                [
                    "你还不知道对方希望你如何称呼 ta。",
                    f"当前可用称呼参考：{address}",
                    f"对方刚刚说：{latest_message or '（空）'}",
                    "请用一句自然中文、保持角色语气，先接住对方的话，再问对方想让你怎么称呼 ta。",
                    "不要输出英文，不要解释内部流程，不要说自己在 onboarding。",
                ]
            )
        elif stage == "confirm_name":
            parts.extend(
                [
                    f"对方刚刚明确告诉你，希望被称呼为：{candidate_name}",
                    f"从现在开始，请把对方当作：{candidate_name}",
                    "请用一句自然中文、保持角色语气，明确确认：好的，我叫你这个名字。",
                    "不要再叫对方旧昵称、群昵称或显示名。",
                    "不要复述规则，不要输出英文，不要改成别的含义。",
                ]
            )
        elif stage == "soft_ask_name":
            parts.extend(
                [
                    "你已经正常接住了对方这句话，现在只需要补一句很轻的尾句。",
                    "请用一句自然中文、保持角色语气，表达：如果对方想换个称呼，可以直接告诉你“叫我 xx”。",
                    "语气要轻，不要像流程表单，不要重复追问。",
                ]
            )
        elif stage == "confirm_assistant_alias":
            parts.extend(
                [
                    f"对方刚刚明确说，以后想叫你：{candidate_name}",
                    f"对方当前称呼参考：{address}",
                    "请用一句自然中文、保持角色语气，明确确认：好的，那就叫我这个名字。",
                    "不要输出英文，不要解释规则。",
                ]
            )
        elif stage == "reject_third_party_naming":
            parts.extend(
                [
                    "对方正在替第三方决定称呼边界，这件事你不能接受。",
                    f"当前对方称呼参考：{address}",
                    "请用一句自然中文、保持角色语气，明确表达：你只能接受对方给自己定称呼，也只能接受别人亲自来给你定称呼，不能替别人决定怎么叫。",
                    "拒绝要清楚，但不要生硬。",
                ]
            )
        elif stage == "clarify_naming_intent":
            if intent_hint == "assistant_alias":
                parts.extend(
                    [
                        "对方是在问大家是否这样叫你，或者想确认能不能给你起称呼。",
                        f"当前对方称呼参考：{address}",
                        "请用一句自然中文、保持角色语气，说明：如果想给你起称呼，可以直接说“以后就叫你 xx”。",
                    ]
                )
            else:
                parts.extend(
                    [
                        "对方是在问你应该怎么称呼 ta，但还没有正式定义名字。",
                        f"当前对方称呼参考：{address}",
                        "请用一句自然中文、保持角色语气，引导 ta 直接说“叫我 xx”。",
                    ]
                )
        else:
            parts.extend(
                [
                    "你还没有确认到对方真正希望的称呼。",
                    f"对方上一句是：{latest_message or '（空）'}",
                    "不要把这句话本身直接当成名字。",
                    "请用一句自然中文、保持角色语气，礼貌地再问一次希望你怎么称呼 ta。",
                ]
            )
        parts.append("只输出下一句回复。")
        return "\n\n".join(part for part in parts if part.strip())

    def _build_preferred_name_hint_query(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
        latest_message: str,
    ) -> str:
        conversation_view = self._conversation_excerpt(session.history, assistant_name=assistant_name)
        lines = [
            f"You decide whether the speaker just told {assistant_name} how to address them.",
            "Return JSON only.",
            'Use this schema: {"name":"...", "confidence":0.0, "is_self_intro":true}.',
            (
                'If the message is not a self-introduction or address preference, return '
                '{"name":"", "confidence":0.0, "is_self_intro":false}.'
            ),
            'Example: "叫我爷爷" -> {"name":"爷爷","confidence":0.95,"is_self_intro":true}',
            'Example: "叫他爷爷" -> {"name":"","confidence":0.0,"is_self_intro":false}',
            'Example: "你这太机械了" -> {"name":"","confidence":0.0,"is_self_intro":false}',
            "Only extract the address term itself; do not copy whole sentences.",
            f"Launcher type: {event.launcher_type}",
            f"Speaker display name: {address or event.sender_name or event.sender_id}",
            f"Latest message: {latest_message or '(empty)'}",
        ]
        profile_lines = [line for line in card.profile[:3] if str(line or "").strip()]
        if profile_lines:
            lines.append("Persona profile: " + " | ".join(profile_lines))
        if conversation_view:
            lines.append("[Recent conversation]\n" + conversation_view)
        return "\n\n".join(lines)

    def _build_intent_router_query(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
        latest_message: str,
        candidate_skills: list[dict[str, object]],
    ) -> str:
        conversation_view = self._conversation_excerpt(session.history, assistant_name=assistant_name, limit=6)
        lines = [
            "[Intent Router]",
            "你是技能路由器，不要扮演角色本人，也不要输出对白。",
            "你的任务是判断：这句消息是否应该触发技能、触发哪个技能，还是应该先澄清。",
            "Return JSON only.",
            (
                'Schema: {"mode":"activate_only|dispatch|clarify|none",'
                '"active_skill_ids":["..."],'
                '"dispatch_skill_id":"...",'
                '"raw_args":"...",'
                '"clarification_text":"..."}'
            ),
            "规则：",
            "- `dispatch` 只在用户明显要你直接执行某个技能时使用，而且 `dispatch_skill_id` 必须来自候选列表里的 dispatchable 技能。",
            "- `activate_only` 只在需要启用风格/约束类技能，但不需要立即执行工具时使用。",
            "- `clarify` 用在你觉得像技能请求，但目标技能或参数还不够明确的时候。",
            "- `none` 用在普通聊天，不需要技能的时候。",
            "- 如果是带链接、视频、网页、文件的“总结/看看/过一遍/讲什么”，优先考虑外部内容总结类技能，而不是对话总结。",
            "- 如果只有“总结一下”这类模糊说法，没有明确对象，优先 `clarify`，不要自己猜。",
            "- `active_skill_ids` 只放额外要启用的技能，不要把 `dispatch_skill_id` 重复放进去。",
            f"当前角色：{assistant_name}",
            f"角色语言：{card.language or '简体中文'}",
            f"对方当前称呼：{address}",
            f"会话类型：{event.launcher_type}",
            f"最新消息：{latest_message or '(empty)'}",
        ]
        profile_lines = [line for line in card.profile[:3] if str(line or "").strip()]
        if profile_lines:
            lines.append("角色摘要：" + " | ".join(profile_lines))
        if conversation_view:
            lines.append("[Recent conversation]\n" + conversation_view)
        lines.append("[Candidate skills]\n" + json.dumps(candidate_skills, ensure_ascii=False, indent=2))
        return "\n\n".join(lines)

    def _build_naming_intent_router_query(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        card: CharacterCard,
        assistant_name: str,
        latest_message: str,
        onboarding_status: str,
        preferred_name: str,
        passive_capture_allowed: bool,
    ) -> str:
        conversation_view = self._conversation_excerpt(session.history, assistant_name=assistant_name, limit=6)
        lines = [
            "[Naming Intent Router]",
            "你是称呼路由器，不要扮演角色本人，也不要输出对白。",
            "你的任务是判断：这句消息是不是在定义用户称呼、给机器人起别名、越权替别人命名、询问命名规则，还是普通聊天。",
            "Return JSON only.",
            (
                'Schema: {"mode":"set_preferred_name|set_assistant_alias|reject_third_party_naming|clarify_naming_intent|none",'
                '"preferred_name":"...",'
                '"assistant_alias":"...",'
                '"clarification_text":"user_name|assistant_alias|",'
                '"reason":"..."}'
            ),
            "规则：",
            "- `set_preferred_name` 只在说话人是在定义“你以后怎么称呼我”时使用。",
            "- `set_assistant_alias` 只在说话人是在定义“我以后怎么叫你”时使用。",
            "- `reject_third_party_naming` 用于说话人试图替第三方决定称呼边界时。",
            "- `clarify_naming_intent` 用于命名相关问题，但这句话本身还没有正式定义名字。",
            "- `none` 用于普通聊天，不要为了勉强命中而乱选。",
            "- `preferred_name` 和 `assistant_alias` 只能提取短称呼本身，不要复制整句，不要带标点。",
            "- 如果 `passive_capture_allowed` 是 false，就不要把裸词、模糊跟进或含糊自我介绍当成正式命名，除非这句话本身已经非常明确。",
            "- 如果 `clarify_naming_intent`，`clarification_text` 只能填写 `user_name` 或 `assistant_alias`。",
            f"当前角色：{assistant_name}",
            f"角色语言：{card.language or '简体中文'}",
            f"会话类型：{event.launcher_type}",
            f"当前 onboarding 状态：{onboarding_status or 'new'}",
            f"当前已保存的 preferred_name：{preferred_name or '(none)'}",
            f"passive_capture_allowed：{'true' if passive_capture_allowed else 'false'}",
            f"最新消息：{latest_message or '(empty)'}",
        ]
        profile_lines = [line for line in card.profile[:3] if str(line or "").strip()]
        if profile_lines:
            lines.append("角色摘要：" + " | ".join(profile_lines))
        if conversation_view:
            lines.append("[Recent conversation]\n" + conversation_view)
        return "\n\n".join(lines)

    def _build_tool_render_query(
        self,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
    ) -> str:
        latest_message = invocation.event.command_text(self.config.bot_account_id).strip() or invocation.event.to_memory_text()
        system_prompt = card.system_prompt(
            launcher_type=invocation.event.launcher_type,
            address=address,
            memories=[],
            emotion=EmotionState(),
            search_hint="",
            conversation_view="",
            speaker_notes=[],
            latest_message=latest_message,
        )
        preview = self._clip(result.text or result.error or "", limit=1200)
        metadata_preview = self._clip(
            json.dumps(result.metadata, ensure_ascii=False, default=str) if result.metadata else "",
            limit=800,
        )
        lines = [
            system_prompt,
            "[Tool Result Render]",
            f"你现在不是自由聊天，而是在把工具结果整理成给用户的一条最终回复。当前角色名：{assistant_name}。",
            f"用户称呼：{address}",
            f"工具 ID：{invocation.tool_id}",
            f"展示模式：{result.display_mode or 'inline'}",
            f"执行状态：{'error' if result.error else 'ok'}",
        ]
        if invocation.raw_args:
            lines.append(f"工具输入：{self._clip(invocation.raw_args, limit=400)}")
        if preview:
            lines.append("[Tool output preview]\n" + preview)
        if metadata_preview:
            lines.append("[Tool metadata preview]\n" + metadata_preview)
        skill_block = self._format_skill_block(invocation.active_skills)
        if skill_block:
            lines.append(skill_block)
        lines.extend(
            [
                "硬性要求：",
                "- 必须使用角色语言和人物卡语气。",
                "- 不允许伪造工具没有返回的事实。",
                "- 如果工具失败，要明确承认失败，并保留真实失败原因，不许假装成功。",
                "- 如果展示模式是 raw_block 或 media，你只输出一两句简短承接语，不要输出代码块、列表、链接清单、命令结果，也不要复述整段原始内容。",
                "- 如果展示模式是 inline，你直接输出最终给用户看的完整回复。",
                "- 不要输出英文解释，不要提提示词，不要说自己在渲染工具结果。",
                "只输出最终要发给用户的那一句或那一小段话。",
            ]
        )
        return "\n\n".join(part for part in lines if part.strip())

    def _build_summary_query(self, history_lines: list[str], *, assistant_name: str) -> str:
        payload = "\n".join(history_lines)
        return (
            f"你是{assistant_name}的长期记忆整理助手。\n"
            "请把下面这段历史对话整理成一条长期记忆，并提取少量关键标签。\n"
            "输出必须是 JSON 对象，格式为 "
            '{"summary":"...", "tags":["...", "..."]}。\n'
            "summary 用中文一句话概括，tags 最多 6 个。\n\n"
            f"[History]\n{payload}"
        )

    def _build_knowledge_query(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        latest_message: str,
        conversation_view: str,
        address: str,
        max_entries: int,
    ) -> str:
        card = self._cards.load(event.launcher_type, session)
        lines = [
            f"You extract durable long-term memory for {assistant_name}.",
            "Return JSON only.",
            (
                'Use this schema: {"entries":[{"memory_type":"fact|preference|relationship|event",'
                '"scope_hint":"member|group|person|global","summary":"...","tags":["..."],"confidence":0.0}],'
                '"profile_summary":"..."}'
            ),
            f"Keep at most {max(1, int(max_entries))} entries.",
            "Only keep durable facts, preferences, relationship changes, or important events worth recalling later.",
            "Ignore greetings, transient commands, image requests, search requests, onboarding name collection, and meta instructions.",
            "Do not store preferred-name requests or the fact that the assistant asked for onboarding.",
            "The summary must be concise and self-contained.",
            f"Speaker display name: {address or event.sender_name or event.sender_id}",
            f"Launcher type: {event.launcher_type}",
            f"Latest message: {latest_message}",
        ]
        profile_lines = [line for line in card.profile[:3] if str(line or "").strip()]
        if profile_lines:
            lines.append("Persona profile: " + " | ".join(profile_lines))
        if conversation_view:
            lines.append("[Recent conversation]\n" + conversation_view)
        return "\n\n".join(lines)

    def _fallback_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        emotion: EmotionState,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
        search_hint: str,
        memory_hints: list[str],
        analysis_hint: str,
    ) -> str:
        if self.config.story_mode:
            return self._fallback_story_reply(
                event,
                session,
                emotion,
                card=card,
                assistant_name=assistant_name,
                address=address,
                search_hint=search_hint,
                memory_hints=memory_hints,
                analysis_hint=analysis_hint,
            )
        text = event.command_text(self.config.bot_account_id).strip()
        if self._asks_for_name(text):
            default_address = card.user_name if event.launcher_type == "person" and card.user_name else (event.sender_name or "你")
            if address and address != default_address:
                return f"嗯，我记住了，以后就叫你{address}。"
            return f"你想让我怎么称呼你呀，直接告诉{assistant_name}就好。"

        if event.image_count > 0 and text:
            return f"嗯，{address}，这张图我先记下了，你刚刚说的是“{self._clip(text)}”。"

        if search_hint:
            return f"嗯，{address}，我刚查了一下：{self._clip(search_hint, limit=72)}"

        if memory_hints:
            remembered = self._clip(memory_hints[0], limit=32)
            return f"嗯，{address}，我还记得“{remembered}”。你刚刚说“{self._clip(text)}”，我先顺着这个继续聊。"

        if not text:
            return f"嗯，{address}，我在呢。"

        tone = {
            "love": "我会好好陪着你的。",
            "joy": "听起来就让人跟着开心起来了。",
            "sadness": "要是你难受的话，可以继续和我说。",
            "anger": "先别急，慢慢说给我听。",
            "anxiety": "没事的，我们一点点来。",
            "anticipation": "听起来就很让人期待呢。",
        }.get(emotion.primary, "我有在认真听。")
        del analysis_hint
        prefix = "在群里我先接这句。" if event.launcher_type == "group" else f"{assistant_name}在听。"
        if card.skills:
            prefix = f"{assistant_name}在听。"
        return f"嗯，{address}，{prefix} 你刚刚说的是“{self._clip(text)}”，{tone}"

    def _fallback_analysis(
        self,
        event: InboundEvent,
        latest_message: str,
        memory_hints: list[str],
        speaker_notes: list[str],
    ) -> str:
        parts: list[str] = []
        if event.launcher_type == "group":
            parts.append("群里有人在接话")
        if event.has_bot_mention(self.config.bot_account_id):
            parts.append("这句是在直接点你")
        if event.image_count > 0:
            parts.append("消息里带了图片")
        if memory_hints:
            parts.append("可以顺着旧记忆接")
        if speaker_notes:
            parts.append("要注意当前说话人的称呼")
        if self._asks_for_name(latest_message):
            parts.append("重点回答称呼问题")
        if not parts:
            parts.append("自然接住对方的话")
        return "，".join(parts)

    def _build_story_mode_block(
        self,
        event: InboundEvent,
        *,
        card: CharacterCard,
        emotion: EmotionState,
        address: str,
        latest_message: str,
    ) -> str:
        if not self.config.story_mode:
            return ""
        style = self._story_style()
        detail_level = max(1, int(self.config.story_detail_level))
        scene_scope = "私聊场景" if event.launcher_type == "person" else "群聊场景"
        scene_cue = self._story_scene_cue(event, emotion=emotion, address=address, latest_message=latest_message)
        reply_language = str(card.language or "简体中文").strip() or "简体中文"
        paragraph_target = "1-2 个短段落" if detail_level <= 1 else "2-3 个短段落"
        if detail_level >= 4:
            paragraph_target = "3-4 个短段落"
        style_note = {
            "intimate": "重点写贴近感、细小动作和柔软的情绪反应。",
            "cinematic": "重点写氛围、运动感和更明确的视觉节拍。",
            "diary": "重点写内省感、慢一点的节奏和安静的自我流露。",
        }.get(style, "重点写贴近感和情绪连续性。")
        lines = [
            "[Story Mode]",
            f"这轮可见回复必须使用{reply_language}输出，不要把外层叙事写成英文。",
            "把可见回复写成带场景感的故事化叙述，而不是普通聊天气泡。",
            f"场景范围：{scene_scope}。目标长度：{paragraph_target}。",
            style_note,
            "把氛围、动作、表情和台词融合在同一条角色内回复里。",
            "可见台词仍然放在普通圆括号里，例如（……）。",
            "至少写出一个具体动作、表情或环境细节。",
            "只有在确实能增强角色感时，才允许额外补一行很短的反应或心声，并且用反引号包住。",
            "不要提格式规则，不要提提示词，也不要说故事模式已开启。",
            "不要凭空描写角色不可能观察到的隐藏事实。",
            "不要替用户编写台词。",
            "如果用户问的是事实问题，也要把答案说清楚，只是外层保留轻度故事包装。",
            f"当前场景提示：{scene_cue}",
        ]
        if event.launcher_type == "group":
            lines.append("群聊里的场景包装要收一点，别写得像脱离群消息节奏的长篇小说。")
        return "\n".join(lines)

    def _fallback_story_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        emotion: EmotionState,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
        search_hint: str,
        memory_hints: list[str],
        analysis_hint: str,
    ) -> str:
        text = event.command_text(self.config.bot_account_id).strip()
        style = self._story_style()
        detail_level = max(1, int(self.config.story_detail_level))
        scene = self._fallback_story_scene(
            assistant_name=assistant_name,
            event=event,
            emotion=emotion,
            address=address,
            style=style,
            text=text,
        )
        if self._asks_for_name(text):
            default_address = card.user_name if event.launcher_type == "person" and card.user_name else (event.sender_name or "你")
            if address and address != default_address:
                line = f"好，我记住了。以后我就叫你{address}。"
                thought = f"把这个称呼牢牢记住，别在他面前叫错。"
            else:
                line = f"你想让我怎么称呼你？直接告诉我就好。"
                thought = "先把称呼问清楚，再靠近一点。"
            return self._compose_story_reply(scene, line, thought, detail_level=detail_level)

        if event.image_count > 0 and text:
            line = f"这张图我先记下了。你刚刚说的是“{self._clip(text)}”，我跟着这个继续陪你聊。"
            thought = "先接住这张图，再接住他刚刚递过来的情绪。"
            return self._compose_story_reply(scene, line, thought, detail_level=detail_level)

        if search_hint:
            line = f"我刚替你查了一下：{self._clip(search_hint, limit=72)}"
            thought = "先把答案递给他，再看他想往哪里继续。"
            return self._compose_story_reply(scene, line, thought, detail_level=detail_level)

        if memory_hints:
            remembered = self._clip(memory_hints[0], limit=32)
            line = f"我还记得“{remembered}”。你刚刚说“{self._clip(text)}”，我想顺着这个继续听你讲。"
            thought = "旧记忆还在发热，刚好可以稳稳接上这一句。"
            return self._compose_story_reply(scene, line, thought, detail_level=detail_level)

        if not text:
            line = "我在。你可以慢慢说。"
            thought = "先让他知道我没有走开。"
            return self._compose_story_reply(scene, line, thought, detail_level=detail_level)

        tone = {
            "love": "我会好好陪着你，不让你一个人掉下去。",
            "joy": "听着就让人想跟着你一起笑起来。",
            "sadness": "如果你还难受，就继续把话放到我这里来。",
            "anger": "先别急，我们把这口气慢慢顺下来。",
            "anxiety": "没事，我们一点一点来，我不催你。",
            "anticipation": "这样讲下去，真的让人开始期待后面会发生什么。",
        }.get(emotion.primary, "我有在认真听，也会认真接住你。")
        if analysis_hint:
            tone = f"{self._clip(analysis_hint, limit=22)} {tone}"
        line = tone if not text else f"你刚刚说“{self._clip(text)}”。{tone}"
        thought = self._fallback_story_thought(event, emotion=emotion, address=address, style=style)
        return self._compose_story_reply(scene, line, thought, detail_level=detail_level)

    def _compose_story_reply(self, scene: str, line: str, thought: str, *, detail_level: int) -> str:
        parts = [scene, f"({line})"]
        if detail_level >= 2 and thought:
            parts.append(f"`{thought}`")
        return "".join(part for part in parts if part)

    def _fallback_story_scene(
        self,
        *,
        assistant_name: str,
        event: InboundEvent,
        emotion: EmotionState,
        address: str,
        style: str,
        text: str,
    ) -> str:
        mood = self._story_mood_phrase(emotion.primary)
        if event.launcher_type == "group":
            return f"{assistant_name}先把群里的节奏放慢一点，目光稳稳落在{address}那边，语气也跟着收轻了些。"
        if style == "cinematic":
            return f"{assistant_name}把声音放低了一点，像是把周围的空气都轻轻按住，只留下这句话和{address}之间的距离。"
        if style == "diary":
            return f"{assistant_name}安静地望着{address}，像把这一刻悄悄记进心里，连呼吸都放得更慢。"
        if text:
            return f"{assistant_name}轻轻抬眼看向{address}，神情没有躲开，连回应都带着一点{mood}的缓冲。"
        return f"{assistant_name}没有移开视线，只是把语气放得更柔，好让{address}能安心继续说下去。"

    def _fallback_story_thought(
        self,
        event: InboundEvent,
        *,
        emotion: EmotionState,
        address: str,
        style: str,
    ) -> str:
        if event.launcher_type == "group":
            return f"先把这句接稳，别让群里的噪音盖过{address}真正想说的东西。"
        if style == "cinematic":
            return f"让动作和语气一起落下去，给{address}一个可以靠近的画面。"
        if style == "diary":
            return f"把这一刻写得轻一点，让{address}知道我一直在听。"
        mood = self._story_mood_phrase(emotion.primary)
        return f"他的情绪是{mood}的，我得把回应放轻一点，别踩碎现在的气氛。"

    def _story_style(self) -> str:
        normalized = str(self.config.story_style or "").strip().lower()
        if normalized == "subtle":
            return "intimate"
        if normalized in {"intimate", "cinematic", "diary"}:
            return normalized
        return "intimate"

    def _story_scene_cue(
        self,
        event: InboundEvent,
        *,
        emotion: EmotionState,
        address: str,
        latest_message: str,
    ) -> str:
        scene_scope = "私聊" if event.launcher_type == "person" else "群聊"
        latest = self._clip(latest_message or "（空）", limit=48)
        return (
            f"{scene_scope}；当前情绪底色是{self._story_mood_phrase(emotion.primary)}；"
            f"叙述视角要贴近{address or event.sender_name or event.sender_id}；当前最新一句是“{latest}”。"
        )

    @staticmethod
    def _story_mood_phrase(emotion: str) -> str:
        mapping = {
            "joy": "轻快而温暖",
            "love": "亲近而带一点外露的喜欢",
            "sadness": "柔软、谨慎，需要安抚",
            "anger": "绷紧，需要降火和缓冲",
            "anxiety": "迟疑、需要安心感",
            "anticipation": "期待感明显，气氛活一点",
        }
        return mapping.get(str(emotion or "").strip().lower(), "稳定、专注、在认真接话")

    def _fallback_onboarding_reply(
        self,
        *,
        stage: str,
        address: str,
        candidate_name: str,
        intent_hint: str,
    ) -> str:
        if stage == "confirm_name" and candidate_name:
            return f"好的，我叫你{candidate_name}。"
        if stage == "soft_ask_name":
            return "如果你想让我换个叫法，直接告诉我“叫我 xx”就好。"
        if stage == "confirm_assistant_alias" and candidate_name:
            return f"好的，那就叫我{candidate_name}。"
        if stage == "reject_third_party_naming":
            return "我只能接受你给你自己定称呼，也只能接受别人亲自来给我定称呼，不能替别人决定怎么叫哦。"
        if stage == "clarify_naming_intent":
            if intent_hint == "assistant_alias":
                return "如果你想给我起个称呼，可以直接说“以后就叫你 xx”。"
            return "如果你想让我换个叫法，直接告诉我“叫我 xx”就好。"
        if stage == "retry_name":
            return f"等等呀，{address}，我还没记住你的称呼呢，你直接告诉我想让我怎么叫你吧。"
        return f"你好呀，{address}，你想让我怎么称呼你呢？"

    def _fallback_summary(self, history_lines: list[str]) -> tuple[str, list[str]]:
        cleaned = [line.strip() for line in history_lines if line.strip()]
        preview = "；".join(cleaned[:3])
        summary = self._clip(preview or (cleaned[0] if cleaned else ""), limit=60)
        tags = self._extract_tags(" ".join(cleaned))
        return summary, tags[:6]

    def _parse_summary_payload(self, response: str) -> tuple[str, list[str]]:
        payload = self._extract_json_block(response)
        if not payload:
            return "", []
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return "", []
        summary = str(data.get("summary", "") or "").strip()
        raw_tags = data.get("tags", [])
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
        return summary, tags[:6]

    @staticmethod
    def _empty_preferred_name_hint() -> dict[str, Any]:
        return {
            "name": "",
            "confidence": 0.0,
            "is_self_intro": False,
        }

    @staticmethod
    def _empty_intent_route() -> dict[str, Any]:
        return {
            "mode": "none",
            "active_skill_ids": [],
            "dispatch_skill_id": "",
            "raw_args": "",
            "clarification_text": "",
        }

    @staticmethod
    def _empty_naming_intent_route() -> dict[str, Any]:
        return {
            "mode": "none",
            "preferred_name": "",
            "assistant_alias": "",
            "clarification_text": "",
            "reason": "",
        }

    def _parse_intent_route_payload(self, response: str) -> dict[str, Any]:
        payload = self._extract_json_payload(response)
        if not payload:
            return self._empty_intent_route()
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return self._empty_intent_route()
        if not isinstance(decoded, dict):
            return self._empty_intent_route()
        raw_active = decoded.get("active_skill_ids", [])
        active_skill_ids = (
            [str(item).strip() for item in raw_active if str(item).strip()]
            if isinstance(raw_active, list)
            else []
        )
        return {
            "mode": str(decoded.get("mode", "") or "none").strip().lower() or "none",
            "active_skill_ids": active_skill_ids,
            "dispatch_skill_id": str(decoded.get("dispatch_skill_id", "") or "").strip(),
            "raw_args": str(decoded.get("raw_args", "") or "").strip(),
            "clarification_text": str(decoded.get("clarification_text", "") or "").strip(),
        }

    def _parse_naming_intent_route_payload(self, response: str) -> dict[str, Any]:
        payload = self._extract_json_payload(response)
        if not payload:
            return self._empty_naming_intent_route()
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return self._empty_naming_intent_route()
        if not isinstance(decoded, dict):
            return self._empty_naming_intent_route()
        return {
            "mode": str(decoded.get("mode", "") or "none").strip().lower() or "none",
            "preferred_name": str(decoded.get("preferred_name", "") or "").strip(),
            "assistant_alias": str(decoded.get("assistant_alias", "") or "").strip(),
            "clarification_text": str(decoded.get("clarification_text", "") or "").strip(),
            "reason": str(decoded.get("reason", "") or "").strip(),
        }

    def _parse_preferred_name_hint_payload(self, response: str) -> dict[str, Any]:
        payload = self._extract_json_block(response)
        if not payload:
            return self._empty_preferred_name_hint()
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return self._empty_preferred_name_hint()
        if not isinstance(decoded, dict):
            return self._empty_preferred_name_hint()
        raw_name = decoded.get("name")
        name = str(raw_name or "").strip() if raw_name is not None else ""
        try:
            confidence = float(decoded.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        return {
            "name": name,
            "confidence": confidence,
            "is_self_intro": bool(decoded.get("is_self_intro")),
        }

    def _parse_knowledge_payload(self, response: str, *, max_entries: int) -> dict[str, Any]:
        payload = self._extract_json_payload(response)
        if not payload:
            return {"entries": [], "profile_summary": ""}
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return {"entries": [], "profile_summary": ""}
        if isinstance(decoded, list):
            decoded = {"entries": decoded}
        if not isinstance(decoded, dict):
            return {"entries": [], "profile_summary": ""}
        raw_entries = decoded.get("entries", [])
        entries: list[dict[str, Any]] = []
        if isinstance(raw_entries, list):
            for item in raw_entries[: max(1, int(max_entries))]:
                normalized = self._normalize_knowledge_item(item)
                if normalized:
                    entries.append(normalized)
        profile_summary = str(decoded.get("profile_summary", "") or "").strip()
        return {
            "entries": entries,
            "profile_summary": self._clip(profile_summary, limit=120) if profile_summary else "",
        }

    @staticmethod
    def _extract_json_block(text: str) -> str:
        raw = str(text or "")
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return ""
        return raw[start : end + 1]

    @staticmethod
    def _extract_json_payload(text: str) -> str:
        raw = str(text or "")
        object_start = raw.find("{")
        array_start = raw.find("[")
        if object_start == -1 and array_start == -1:
            return ""
        if object_start == -1 or (array_start != -1 and array_start < object_start):
            start = array_start
            end = raw.rfind("]")
        else:
            start = object_start
            end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return ""
        return raw[start : end + 1]

    @staticmethod
    def _extract_tags(text: str) -> list[str]:
        normalized = str(text or "").lower()
        terms = re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", normalized)
        seen: set[str] = set()
        tags: list[str] = []
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            tags.append(term)
        return tags

    def _fallback_extract_knowledge(self, latest_message: str, *, max_entries: int) -> dict[str, Any]:
        compact = " ".join(str(latest_message or "").split()).strip()
        if not compact or self._asks_for_name(compact):
            return {"entries": [], "profile_summary": ""}

        entries: list[dict[str, Any]] = []
        profile_parts: list[str] = []

        def add_entry(
            memory_type: str,
            summary: str,
            *,
            tags: list[str] | None = None,
            scope_hint: str = "member",
            confidence: float = 0.62,
        ) -> None:
            if len(entries) >= max(1, int(max_entries)):
                return
            clean_summary = " ".join(str(summary or "").split()).strip()
            if not clean_summary:
                return
            existing = {str(item.get("summary", "")).strip().casefold() for item in entries}
            if clean_summary.casefold() in existing:
                return
            entries.append(
                {
                    "memory_type": memory_type,
                    "scope_hint": scope_hint,
                    "summary": clean_summary,
                    "tags": (tags or [])[:5],
                    "confidence": confidence,
                }
            )

        preference_patterns = (
            (r"(?:^|[，,。.!? ])我(?:很)?喜欢(.{1,32})$", "Likes {item}", 0.72),
            (r"(?:^|[，,。.!? ])我(?:很)?讨厌(.{1,32})$", "Dislikes {item}", 0.70),
            (r"(?:^|[ ,.!?])i\s+(?:really\s+)?like\s+(.{1,48})$", "Likes {item}", 0.72),
            (r"(?:^|[ ,.!?])i\s+(?:really\s+)?love\s+(.{1,48})$", "Likes {item}", 0.74),
            (r"(?:^|[ ,.!?])i\s+(?:really\s+)?hate\s+(.{1,48})$", "Dislikes {item}", 0.70),
        )
        identity_patterns = (
            (r"(?:^|[，,。.!? ])我是(.{1,18})$", "Is {item}", 0.66),
            (r"(?:^|[ ,.!?])i(?:'m| am)\s+(.{1,24})$", "Is {item}", 0.66),
        )
        group_patterns = (
            (r"(?:这个群|我們群|我们群|本群)(.{1,32})", "The group {item}", 0.58),
            (r"(?:this group)(.{1,40})", "The group {item}", 0.58),
        )

        for pattern, template, confidence in preference_patterns:
            match = re.search(pattern, compact, flags=re.IGNORECASE)
            if not match:
                continue
            item = self._clean_knowledge_fragment(match.group(1))
            if not item:
                continue
            summary = template.format(item=item)
            add_entry("preference", summary, tags=self._extract_tags(item)[:4], confidence=confidence)
            profile_parts.append(summary)
            break

        for pattern, template, confidence in identity_patterns:
            match = re.search(pattern, compact, flags=re.IGNORECASE)
            if not match:
                continue
            item = self._clean_knowledge_fragment(match.group(1))
            if not item or self._looks_like_name_only(item):
                continue
            summary = template.format(item=item)
            add_entry("fact", summary, tags=self._extract_tags(item)[:4], confidence=confidence)
            profile_parts.append(summary)
            break

        for pattern, template, confidence in group_patterns:
            match = re.search(pattern, compact, flags=re.IGNORECASE)
            if not match:
                continue
            item = self._clean_knowledge_fragment(match.group(1))
            if not item:
                continue
            summary = template.format(item=item)
            add_entry("fact", summary, tags=self._extract_tags(item)[:4], scope_hint="group", confidence=confidence)
            break

        return {
            "entries": entries[: max(1, int(max_entries))],
            "profile_summary": "; ".join(profile_parts[:2]),
        }

    def _fallback_tool_reply(
        self,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
        *,
        card: CharacterCard,
        assistant_name: str,
        address: str,
    ) -> GeneratedReply:
        del card, assistant_name
        payload = str(result.text or result.error or "工具没有返回内容。").strip()
        tool_id = str(invocation.tool_id or "").strip().lower()
        if result.display_mode == "media":
            if result.error:
                return GeneratedReply(
                    text=f"{address}，这次没成功，原因是：{result.error or payload}",
                    images=list(result.images),
                )
            prompt = str(result.metadata.get("prompt", "") or "").strip()
            if prompt:
                return GeneratedReply(
                    text=f"{address}要的内容我已经准备好了，主题是“{self._clip(prompt, limit=36)}”。",
                    images=list(result.images),
                )
            return GeneratedReply(
                text=f"{address}，我已经处理好了，结果在这里。",
                images=list(result.images),
            )
        if result.display_mode == "raw_block":
            if result.error:
                return GeneratedReply(
                    text=f"{address}，这次没跑成，我把真实结果原样贴给你。",
                    images=list(result.images),
                )
            prefix = f"{address}，我先把结果整理给你。"
            if tool_id == "skill-list":
                prefix = f"{address}，我先把现在能用的技能整理给你。"
            return GeneratedReply(text=prefix, images=list(result.images))
        if result.error:
            return GeneratedReply(
                text=f"{address}，这次没成功，原因是：{result.error or payload}",
                images=list(result.images),
            )
        if tool_id == "search":
            return GeneratedReply(text=f"{address}，我帮你查了一下。\n{payload}".strip(), images=list(result.images))
        if tool_id == "summary":
            return GeneratedReply(text=f"{address}，我先帮你收一下重点：{payload}".strip(), images=list(result.images))
        if tool_id == "weather":
            return GeneratedReply(text=payload, images=list(result.images))
        if tool_id == "summarize":
            return GeneratedReply(text=payload, images=list(result.images))
        return GeneratedReply(text=payload, images=list(result.images))

    @staticmethod
    def _record_tool_turns(session: SessionMemory, turns: list[dict[str, object]]) -> None:
        session.metadata["last_tool_calls"] = list(turns[-12:])

    @staticmethod
    def _tool_turn_record(
        tool_id: str,
        arguments: dict[str, object],
        result: ToolExecutionResult,
    ) -> dict[str, object]:
        try:
            args_summary = json.dumps(dict(arguments or {}), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            args_summary = str(arguments or {})
        text_preview = " ".join(str(result.text or result.error or "").split()).strip()
        return {
            "tool_id": str(tool_id or "").strip(),
            "arguments": args_summary[:600],
            "status": "error" if result.error else "ok",
            "error": str(result.error or "")[:600],
            "display_mode": str(result.display_mode or "inline"),
            "text_preview": text_preview[:600],
            "image_count": len(result.images),
        }

    @staticmethod
    def _append_raw_tool_blocks(text: str, results: list[ToolExecutionResult]) -> str:
        lead = str(text or "").strip()
        blocks: list[str] = []
        for result in results:
            payload = str(result.text or result.error or "").strip()
            if not payload:
                continue
            block = "```text\n" + payload + "\n```"
            if block not in blocks:
                blocks.append(block)
        if not blocks:
            return lead
        if not lead:
            return "\n\n".join(blocks)
        return lead + "\n\n" + "\n\n".join(blocks)

    def _normalize_knowledge_item(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        memory_type = str(item.get("memory_type", "") or "").strip().lower() or "fact"
        if memory_type not in {"fact", "preference", "relationship", "event", "summary"}:
            memory_type = "fact"
        scope_hint = str(item.get("scope_hint", "") or "").strip().lower() or "member"
        if scope_hint not in {"member", "group", "person", "global"}:
            scope_hint = "member"
        summary = " ".join(str(item.get("summary", "") or "").split()).strip()
        if not summary or self._asks_for_name(summary):
            return None
        raw_tags = item.get("tags", [])
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
        try:
            confidence = float(item.get("confidence", 0.6) or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        confidence = max(0.0, min(1.0, confidence))
        return {
            "memory_type": memory_type,
            "scope_hint": scope_hint,
            "summary": self._clip(summary, limit=160),
            "tags": tags[:5],
            "confidence": confidence,
        }

    @staticmethod
    def _clean_knowledge_fragment(text: str) -> str:
        cleaned = " ".join(str(text or "").split()).strip()
        cleaned = cleaned.strip("，,。.!?;:()[]{}\"'")
        if not cleaned:
            return ""
        if len(cleaned) > 48:
            cleaned = cleaned[:48].rstrip()
        return cleaned

    @staticmethod
    def _looks_like_name_only(text: str) -> bool:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned or len(cleaned) > 12:
            return False
        return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_]{1,12}", cleaned))

    @staticmethod
    def _format_skill_block(active_skills: list[SkillSpec]) -> str:
        prompt_skills = [skill for skill in active_skills if skill.prompt_visible]
        if not prompt_skills:
            return ""
        lines = ["[Active Skills]"]
        for skill in prompt_skills:
            summary = skill.name
            if skill.description:
                summary = f"{summary}: {skill.description}"
            lines.append(f"- {summary}")
        for skill in prompt_skills:
            content = str(skill.content or "").strip()
            if not content:
                continue
            lines.extend(["", f"[Skill: {skill.name}]", content])
        return "\n".join(lines).strip()

    def _resolve_address(self, event: InboundEvent, session: SessionMemory, card: CharacterCard) -> str:
        if event.launcher_type == "person" and card.user_name:
            return card.user_name
        return event.sender_name or "你"

    @staticmethod
    def _conversation_excerpt(history_lines: list[str], *, assistant_name: str, limit: int = 6) -> str:
        lines: list[str] = []
        for raw_line in history_lines[-max(1, int(limit)) :]:
            speaker, content = str(raw_line or "").partition(": ")[::2]
            speaker = speaker.strip() or "user"
            if speaker == "assistant":
                speaker = assistant_name
            lines.append(f"{speaker}: {content.strip()}")
        return "\n".join(line for line in lines if line.strip())

    @staticmethod
    def _clean_response(text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        normalized = normalized.replace("<think>", "").replace("</think>", "").strip()
        return normalized

    @staticmethod
    def _asks_for_name(text: str) -> bool:
        normalized = str(text or "")
        keywords = ("叫我什么", "怎么称呼", "怎么叫我", "该叫我什么", "call me")
        return any(keyword in normalized for keyword in keywords)

    @staticmethod
    def _clip(text: str, limit: int = 24) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit].rstrip() + "..."
