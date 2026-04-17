from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..config import AppConfig
from ..contracts import GeneratedImage
from ..models import EmotionState, InboundEvent, SessionMemory
from .cards import CardManager, CharacterCard
from .image_clients import ImageClient, ImageClientError, build_image_client
from .llm_clients import LLMClient, LLMClientError, build_llm_client
from .skill_registry import SkillSpec


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

    @property
    def llm_ready(self) -> bool:
        return self.config.llm.enabled and self._llm_client.enabled

    @property
    def _llm_client(self) -> LLMClient:
        return self._dify_client

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
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = card.assistant_name or assistant_name or self.config.assistant_name
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
                response = self._dify_client.invoke(
                    prompt,
                    user=self._llm_user_key(event, session, purpose="chat"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return cleaned
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_reply(
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
        return ""

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
        if "generate_reply" in getattr(self, "__dict__", {}) and "agenerate_reply" not in getattr(self, "__dict__", {}):
            return await asyncio.to_thread(
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
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = card.assistant_name or assistant_name or self.config.assistant_name
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
                response = await self._ainvoke_client(
                    prompt,
                    user=self._llm_user_key(event, session, purpose="chat"),
                )
                cleaned = self._clean_response(response)
                if cleaned:
                    return cleaned
            except LLMClientError:
                pass
        if allow_fallback:
            return self._fallback_reply(
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
        return ""

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
        address_override: str = "",
        card_override: CharacterCard | None = None,
        allow_fallback: bool = True,
    ) -> str:
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = card.assistant_name or assistant_name or self.config.assistant_name
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
                address_override=address_override,
                card_override=card_override,
                allow_fallback=allow_fallback,
            )
        card = card_override or self._cards.load(event.launcher_type, session)
        resolved_assistant_name = card.assistant_name or assistant_name or self.config.assistant_name
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
            )
        return ""

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
                    f"对方当前显示昵称：{event.sender_name or address}",
                    f"对方刚刚说：{latest_message or '（空）'}",
                    "请用一句自然中文、保持角色语气，先接住对方的话，再问对方想让你怎么称呼 ta。",
                    "不要输出英文，不要解释内部流程，不要说自己在 onboarding。",
                ]
            )
        elif stage == "confirm_name":
            parts.extend(
                [
                    f"对方刚刚明确告诉你，希望被称呼为：{candidate_name}",
                    f"当前显示昵称：{event.sender_name or address}",
                    "请用一句自然中文、保持角色语气，确认你记住了这个称呼。",
                    "可以轻微延续聊天，但不要复述规则，不要输出英文。",
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
        if analysis_hint:
            tone = f"{self._clip(analysis_hint, limit=22)} {tone}"
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

    def _fallback_onboarding_reply(
        self,
        *,
        stage: str,
        address: str,
        candidate_name: str,
    ) -> str:
        if stage == "confirm_name" and candidate_name:
            return f"好呀，{candidate_name}，我记住你了，以后就这样叫你。"
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
