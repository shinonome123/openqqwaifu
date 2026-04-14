from __future__ import annotations

import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cells.cards import CardManager
from .cells.config import ConfigManager, serialize_app_config
from .cells.generator import Generator
from .cells.marketplace import MarketplaceClient
from .cells.skill_pack import build_skill_pack_template, export_skill_pack, import_skill_pack
from .cells.skill_registry import SkillRegistry, SkillSpec, build_skill_markdown_template
from .cells.tool_registry import ToolInvocation, ToolRegistry
from .config import AppConfig
from .contracts import OutboundPort
from .gateways.onebot_actions import OneBotActionClient, OneBotHttpOutboundPort
from .memory import FileMemoryStore, InMemoryStore
from .models import InboundEvent, OutboundMessage, SessionMemory
from .organs.memories import Memory
from .organs.thoughts import Thoughts
from .services import CapturingOutboundPort
from .state_store import InMemoryRuntimeStateStore, SqliteRuntimeStateStore
from .systems.emotions import EmotionSensor
from .systems.searching import SearchContext, SearchDecider


@dataclass(slots=True)
class WaifuService:
    config: AppConfig
    memory: Memory
    emotions: EmotionSensor
    thoughts: Thoughts
    generator: Generator
    cards: CardManager
    search: SearchDecider
    marketplace: MarketplaceClient
    skills: SkillRegistry
    tools: ToolRegistry
    state_store: Any
    outbound: OutboundPort
    _group_follow_up_until: dict[str, float] = field(default_factory=dict)
    _recent_outbound: list[OutboundMessage] = field(default_factory=list)
    _recent_events: list[dict[str, Any]] = field(default_factory=list)
    _started_at: float = field(default_factory=time.monotonic, repr=False)
    _event_counter: int = 0
    _state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def handle_event(self, event: InboundEvent) -> OutboundMessage | None:
        text = event.command_text(self.config.bot_account_id)
        self._record_inbound(event, text)
        if not text and event.image_count == 0:
            return None
        if text and any(text.startswith(prefix) for prefix in self.config.ignore_prefixes):
            return None
        if not self._should_reply(event):
            return None

        self._remember_directory_member(event)
        session = self.memory.save_user_event(event)
        self._sync_directory_preferred_name(event, session)
        assistant_name = self.generator.resolve_assistant_name(event.launcher_type, session)
        address = self._resolve_address(event, session)
        latest_message = self._latest_message_text(event, text)

        repeat_reply = self._build_repeat_reply(event, session, address=address)
        if repeat_reply is not None:
            return self._emit_message(event, repeat_reply, assistant_name=assistant_name)

        onboarding_reply = self._maybe_handle_member_onboarding(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
        )
        if onboarding_reply is not None:
            return onboarding_reply

        active_skills = self.skills.match(latest_message)
        self._store_active_skills(session, active_skills)

        dispatch = self.skills.resolve_dispatch(latest_message)
        if dispatch is not None:
            skill, raw_args = dispatch
            return self._dispatch_skill_request(
                event,
                session,
                skill=skill,
                raw_args=raw_args,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
            )

        image_prompt = None if self.skills.has_dispatch_tool("image") else self._extract_image_prompt(text)
        if image_prompt is not None:
            return self._handle_image_request(
                event,
                session,
                address=address,
                assistant_name=assistant_name,
                prompt=image_prompt,
                active_skills=active_skills,
            )

        emotion = self.emotions.analyze(event, session)
        search_context = self.search.build_context(event)
        self._store_search_context(session, search_context)
        conversation_view = self.memory.format_dialogue(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            limit=self.config.history_window_messages,
        )
        memory_hints = self.memory.recall_long_term_memories(
            event.launcher_id,
            event.launcher_type,
            latest_message,
            limit=self.config.memory_recall_limit,
        )
        memory_hints = self._merge_memory_hints(
            memory_hints,
            self.state_store.recall_knowledge(
                scopes=self._knowledge_scopes(event),
                query=latest_message,
                limit=self.config.memory_recall_limit,
            ),
            limit=self.config.memory_recall_limit,
        )
        speaker_notes = self.memory.group_member_notes(
            event.launcher_id,
            event.launcher_type,
            active_sender_id=event.sender_id,
        )
        speaker_notes.extend(self._directory_member_notes(event))
        analysis_hint = self.thoughts.analyze(
            event,
            session,
            assistant_name=assistant_name,
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            active_skills=active_skills,
        )
        reply_text = self.generator.generate_reply(
            event,
            session,
            emotion,
            assistant_name=assistant_name,
            search_hint=search_context.summary,
            search_context=search_context.to_prompt_block(),
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            analysis_hint=analysis_hint,
            active_skills=active_skills,
        )
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return self._emit_message(event, message, assistant_name=assistant_name)

    def _handle_image_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
        prompt: str,
        active_skills: list[SkillSpec],
    ) -> OutboundMessage:
        try:
            image = self.generator.generate_image(prompt)
            text = self.generator.generate_image_caption(
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
        return self._emit_message(event, message, assistant_name=assistant_name)

    def _dispatch_skill_request(
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
        message = self.tools.execute(skill.command_tool, invocation)
        if message is not None:
            return message
        unavailable = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=f"{address}，这个技能绑定的工具还没有注册：{skill.command_tool or 'unknown'}。",
        )
        return self._emit_message(event, unavailable, assistant_name=assistant_name)

    def _run_image_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        prompt = invocation.raw_args or self._extract_image_prompt(
            invocation.event.command_text(self.config.bot_account_id)
        )
        if not prompt:
            message = OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=f"{invocation.address}，你想让我画什么呀，直接把主题告诉我就好。",
            )
            return self._emit_message(
                invocation.event,
                message,
                assistant_name=invocation.assistant_name,
            )
        return self._handle_image_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
            prompt=prompt,
            active_skills=invocation.active_skills,
        )

    def _run_search_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        query = invocation.raw_args.strip() or invocation.event.command_text(self.config.bot_account_id).strip()
        return self._handle_search_request(
            invocation.event,
            invocation.session,
            query=query,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    def _run_summary_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._handle_summary_request(
            invocation.event,
            invocation.session,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
        )

    def _handle_search_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        query: str,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        cleaned_query = " ".join(query.split())
        if not cleaned_query:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，你想让我查什么呀，把关键词直接告诉我就好。",
            )
            return self._emit_message(event, message, assistant_name=assistant_name)

        search_context = self.search.search_query(cleaned_query, reason="skill-dispatch")
        self._store_search_context(session, search_context)
        if not search_context.active:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，这次我没查到稳定结果，要不要换个关键词让我再试一次？",
            )
            return self._emit_message(event, message, assistant_name=assistant_name)

        lines = [f"{address}，我帮你查了一下。"]
        if search_context.summary:
            lines.append(search_context.summary)
        for result in search_context.results[1:3]:
            lines.append(f"- {result.title}：{self.generator._clip(result.snippet, limit=56)}")
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
        )
        return self._emit_message(event, message, assistant_name=assistant_name)

    def _handle_summary_request(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        recent_history = list(session.history)[-max(4, self.config.memory_summary_batch_size) :]
        if not recent_history:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，现在还没有足够的上下文，我再多陪你聊几句就能帮你总结啦。",
            )
            return self._emit_message(event, message, assistant_name=assistant_name)
        summary, tags = self.generator.summarize_history(recent_history, assistant_name=assistant_name)
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
        return self._emit_message(event, message, assistant_name=assistant_name)

    def _emit_message(
        self,
        event: InboundEvent,
        message: OutboundMessage,
        *,
        assistant_name: str,
    ) -> OutboundMessage:
        if event.launcher_type == "group":
            delay = max(0.0, float(self.config.group_response_delay_seconds))
            if delay > 0:
                time.sleep(delay)
        self.outbound.send(message)
        self._record_outbound(message)
        self.memory.save_assistant_message(event.launcher_id, event.launcher_type, message.text)
        self.state_store.mark_member_addressed(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
        )
        self._archive_if_needed(event.launcher_id, event.launcher_type, assistant_name=assistant_name)
        self._refresh_follow_up_window(event)
        return message

    def _archive_if_needed(self, launcher_id: str, launcher_type: str, *, assistant_name: str) -> None:
        if not self.config.summarization_mode:
            return
        archived = self.memory.maybe_archive_history(
            launcher_id,
            launcher_type,
            max_history_lines=self.config.short_term_memory_limit,
            batch_size=self.config.memory_summary_batch_size,
            summarizer=lambda history_lines: self.generator.summarize_history(
                history_lines,
                assistant_name=assistant_name,
            ),
        )
        if archived is None:
            return
        long_term = archived.metadata.get("long_term_memory", [])
        if not isinstance(long_term, list) or not long_term:
            return
        latest = long_term[-1]
        if not isinstance(latest, dict):
            return
        summary = str(latest.get("summary", "") or "").strip()
        if not summary:
            return
        self.state_store.add_knowledge(
            scope_type=launcher_type,
            scope_id=launcher_id,
            memory_type="summary",
            summary=summary,
            tags=[str(tag).strip() for tag in latest.get("tags", []) if str(tag).strip()]
            if isinstance(latest.get("tags"), list)
            else [],
            confidence=0.62,
        )

    def _should_reply(self, event: InboundEvent) -> bool:
        if event.launcher_type != "group":
            return True
        bot_account_id = str(self.config.bot_account_id or "").strip()
        if bot_account_id and event.has_bot_mention(bot_account_id):
            self._refresh_follow_up_window(event)
            return True
        if not self.config.group_reply_requires_mention:
            return True
        if not bot_account_id:
            return True
        return self._is_follow_up_window_active(event.launcher_id)

    def _is_follow_up_window_active(self, launcher_id: str) -> bool:
        with self._state_lock:
            deadline = self._group_follow_up_until.get(launcher_id, 0.0)
        return time.monotonic() <= deadline

    def _refresh_follow_up_window(self, event: InboundEvent) -> None:
        if event.launcher_type != "group":
            return
        window = max(0.0, float(self.config.group_follow_up_window_seconds))
        if window <= 0:
            return
        with self._state_lock:
            self._group_follow_up_until[event.launcher_id] = time.monotonic() + window

    def _extract_image_prompt(self, text: str) -> str | None:
        stripped = str(text or "").strip()
        if not stripped:
            return None
        for prefix in self._image_command_prefixes():
            match = re.match(rf"^\s*{re.escape(prefix)}\s*[:：]?\s*(.*?)\s*$", stripped, flags=re.DOTALL)
            if match:
                prompt = re.sub(r"\s+", " ", match.group(1)).strip()
                if prompt:
                    return prompt
        return None

    def _image_command_prefixes(self) -> list[str]:
        prefixes = [self.config.image_command_prefix, *self.config.image_command_aliases]
        seen: set[str] = set()
        unique: list[str] = []
        for item in prefixes:
            prefix = str(item or "").strip()
            if not prefix or prefix in seen:
                continue
            seen.add(prefix)
            unique.append(prefix)
        return unique

    def _extract_image_prompt(self, text: str) -> str | None:
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

    def _latest_message_text(self, event: InboundEvent, command_text: str) -> str:
        if self.config.multimodal_enabled and event.image_count > 0:
            return event.to_memory_text()
        return command_text or event.to_memory_text()

    def _build_repeat_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
    ) -> OutboundMessage | None:
        threshold = max(0, int(self.config.repeat_trigger_count))
        if threshold <= 0 or event.launcher_type != "group":
            return None
        normalized = self._normalize_repeat_text(event.command_text(self.config.bot_account_id))
        if not normalized:
            return None
        repeat_count = self._recent_repeat_count(session, normalized, sender_name=event.sender_name)
        if repeat_count != threshold:
            return None
        return OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=f"嗯，{address}，这句话你已经重复{repeat_count}次了，我听到了。",
        )

    def _recent_repeat_count(self, session: SessionMemory, target: str, *, sender_name: str) -> int:
        count = 0
        for raw_line in reversed(session.history):
            speaker, content = self._split_history_line(raw_line)
            if speaker == "assistant":
                continue
            if speaker != sender_name:
                break
            normalized = self._normalize_repeat_text(content)
            if not normalized:
                if count:
                    break
                continue
            if normalized != target:
                break
            count += 1
        return count

    @staticmethod
    def _split_history_line(line: str) -> tuple[str, str]:
        speaker, sep, content = str(line or "").partition(": ")
        if sep:
            return speaker.strip() or "user", content.strip()
        return "user", str(line or "").strip()

    @staticmethod
    def _normalize_repeat_text(text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        return normalized.casefold()

    def _remember_directory_member(self, event: InboundEvent) -> None:
        self.state_store.record_member_seen(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            qq_nickname=event.sender_name,
        )

    def _sync_directory_preferred_name(self, event: InboundEvent, session: SessionMemory) -> None:
        preferred_name = str(session.preferred_name or "").strip()
        if not preferred_name:
            return
        self.state_store.save_member(
            {
                "group_id": event.launcher_id if event.launcher_type == "group" else "",
                "user_id": event.sender_id,
                "qq_nickname": event.sender_name,
                "preferred_name": preferred_name,
                "onboarding_status": "ready",
            }
        )

    def _maybe_handle_member_onboarding(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> OutboundMessage | None:
        if event.launcher_type != "group":
            return None
        member = self.state_store.get_member(group_id=event.launcher_id, user_id=event.sender_id)
        if member is None:
            return None

        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            return None

        onboarding_status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if onboarding_status == "pending_name":
            candidate = self._extract_directory_preferred_name(latest_message)
            if candidate:
                session.preferred_name = candidate
                self.memory.store.save(session)
                self.state_store.save_member(
                    {
                        "group_id": event.launcher_id,
                        "user_id": event.sender_id,
                        "qq_nickname": event.sender_name,
                        "preferred_name": candidate,
                        "onboarding_status": "ready",
                    }
                )
                message = OutboundMessage(
                    launcher_id=event.launcher_id,
                    launcher_type=event.launcher_type,
                    text=f"{candidate}, got it. I will call you that from now on.",
                )
                return self._emit_message(event, message, assistant_name=assistant_name)

        bot_account_id = str(self.config.bot_account_id or "").strip()
        if bot_account_id and event.has_bot_mention(bot_account_id):
            self.state_store.save_member(
                {
                    "group_id": event.launcher_id,
                    "user_id": event.sender_id,
                    "qq_nickname": event.sender_name,
                    "group_card": str(member.get("group_card", "") or ""),
                    "onboarding_status": "pending_name",
                }
            )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text="What should I call you?",
            )
            return self._emit_message(event, message, assistant_name=assistant_name)
        return None

    def _extract_directory_preferred_name(self, text: str) -> str:
        candidate = self.memory.extract_preferred_name(text)
        if candidate:
            return candidate
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return ""
        compact = compact.strip(".,!?;:，。！？；：\"'`()[]{}")
        if not compact or len(compact) > 12:
            return ""
        if " " in compact or any(token in compact for token in ("http://", "https://", "/", "\\")):
            return ""
        return compact

    @staticmethod
    def _merge_memory_hints(primary: list[str], secondary: list[str], *, limit: int) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for candidate in [*(primary or []), *(secondary or [])]:
            text = str(candidate or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
            if len(merged) >= max(1, int(limit)):
                break
        return merged

    def _knowledge_scopes(self, event: InboundEvent) -> list[tuple[str, str]]:
        scopes = [(event.launcher_type, event.launcher_id), ("global", "")]
        if event.launcher_type == "group":
            scopes.append(("member", f"{event.launcher_id}:{event.sender_id}"))
        else:
            scopes.append(("member", event.sender_id))
        return scopes

    def _directory_member_notes(self, event: InboundEvent) -> list[str]:
        member = self.state_store.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
        )
        if member is None:
            return []
        notes: list[str] = []
        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            notes.append(f"{event.sender_name} prefers to be called {preferred_name}.")
        profile_summary = str(member.get("profile_summary", "") or "").strip()
        if profile_summary:
            notes.append(f"Profile summary for {event.sender_name}: {profile_summary}")
        onboarding_status = str(member.get("onboarding_status", "") or "").strip()
        if onboarding_status and onboarding_status not in {"", "ready"}:
            notes.append(f"{event.sender_name} is still in onboarding status: {onboarding_status}.")
        return notes

    def dashboard_snapshot(self) -> dict[str, object]:
        sessions = self.list_sessions(limit=24)
        knowledge_count = self.state_store.knowledge_count()
        member_count = self.state_store.member_count()
        with self._state_lock:
            recent_outbound = [self._message_to_dict(message) for message in self._recent_outbound[-12:][::-1]]
            active_launchers = sorted(
                launcher_id
                for launcher_id, until in self._group_follow_up_until.items()
                if until > time.monotonic()
            )
        return {
            "service_name": self.config.service_name,
            "assistant_name": self.config.assistant_name,
            "character": self.config.character,
            "bot_account_id": self.config.bot_account_id,
            "group_reply_requires_mention": self.config.group_reply_requires_mention,
            "max_active_skills": self.config.max_active_skills,
            "search_enabled": self.config.search_enabled,
            "thinking_mode": self.config.thinking_mode,
            "summarization_mode": self.config.summarization_mode,
            "reply_window_seconds": self.config.group_follow_up_window_seconds,
            "group_response_delay_seconds": self.config.group_response_delay_seconds,
            "repeat_trigger_count": self.config.repeat_trigger_count,
            "multimodal_enabled": self.config.multimodal_enabled,
            "history_window_messages": self.config.history_window_messages,
            "memory_recall_limit": self.config.memory_recall_limit,
            "short_term_memory_limit": self.config.short_term_memory_limit,
            "memory_summary_batch_size": self.config.memory_summary_batch_size,
            "ignore_prefixes": list(self.config.ignore_prefixes),
            "message_behavior": {
                "requires_mention": self.config.group_reply_requires_mention,
                "follow_up_window_seconds": self.config.group_follow_up_window_seconds,
                "response_delay_seconds": self.config.group_response_delay_seconds,
                "repeat_trigger_count": self.config.repeat_trigger_count,
                "multimodal_enabled": self.config.multimodal_enabled,
            },
            "skills": self.skills.describe(),
            "tools": self.tools.describe(),
            "search": {
                "enabled": self.config.search_enabled,
                "result_limit": self.config.search_result_limit,
                "timeout_seconds": self.config.search_timeout_seconds,
                "cache_size": self.search.cache_size(),
            },
            "outbound_mode": self._outbound_mode_label(),
            "llm": {
                "enabled": self.config.llm.enabled,
                "backend": self.config.llm.backend,
                "base_url": self.config.llm.base_url,
                "ready": self.generator.llm_ready,
            },
            "image_generation": {
                "enabled": self.config.image_generation.enabled,
                "base_url": self.config.image_generation.base_url,
                "model": self.config.image_generation.model,
                "ready": self.generator.image_ready,
            },
            "qq_sidecar": {
                "adapter_name": self.config.qq_sidecar.adapter_name,
                "dry_run": self.config.qq_sidecar.dry_run,
                "outbound_base_url": self.config.qq_sidecar.outbound_base_url,
                "inbound_host": self.config.qq_sidecar.inbound_host,
                "inbound_port": self.config.qq_sidecar.inbound_port,
            },
            "skill_workspace": str(self.skills.workspace_root),
            "session_count": len(sessions),
            "knowledge_count": knowledge_count,
            "member_count": member_count,
            "sessions": sessions,
            "recent_outbound_count": len(recent_outbound),
            "recent_outbound": recent_outbound,
            "active_follow_up_count": len(active_launchers),
            "active_follow_up_launchers": active_launchers,
            "updated_at": time.time(),
        }

    def list_sessions(self, limit: int = 24) -> list[dict[str, object]]:
        store = self.memory.store
        if not hasattr(store, "list_sessions"):
            return []
        sessions = store.list_sessions()  # type: ignore[no-any-return]
        result: list[dict[str, object]] = []
        for session in sessions[-limit:][::-1]:
            history = list(session.history)
            card_metadata = session.metadata.get("card", {})
            long_term = session.metadata.get("long_term_memory", [])
            active_skill_names = self._active_skill_names(session)
            last_search_query = self._last_search_query(session)
            result.append(
                {
                    "launcher_id": session.launcher_id,
                    "launcher_type": session.launcher_type,
                    "preferred_name": session.preferred_name,
                    "assistant_name": card_metadata.get(
                        "assistant_name",
                        session.metadata.get("assistant_name", self.config.assistant_name),
                    )
                    if isinstance(card_metadata, dict)
                    else self.config.assistant_name,
                    "history_count": len(history),
                    "long_term_count": len(long_term) if isinstance(long_term, list) else 0,
                    "active_skill_count": len(active_skill_names),
                    "active_skill_names": active_skill_names,
                    "last_search_query": last_search_query,
                    "last_line": history[-1] if history else "",
                    "group_member_count": self._count_group_members(session),
                }
            )
        return result

    def get_session_detail(self, launcher_type: str, launcher_id: str) -> dict[str, object] | None:
        session = self.memory.load(launcher_id, launcher_type)
        if not session.history and not session.metadata and not session.preferred_name:
            return None
        return {
            "launcher_id": session.launcher_id,
            "launcher_type": session.launcher_type,
            "preferred_name": session.preferred_name,
            "history": list(session.history),
            "metadata": session.metadata,
        }

    def list_skills(self) -> dict[str, object]:
        return self.skills.describe()

    def list_tools(self) -> dict[str, object]:
        return self.tools.describe()

    def get_console_panels(self) -> dict[str, object]:
        return {
            "character": self.get_character_panel(),
            "ai": self.get_ai_panel(),
            "memory": self.get_memory_panel(),
            "abilities": self.get_abilities_panel(),
            "skills": self.get_skills_panel(),
            "sidecar": self.get_sidecar_panel(refresh=False),
            "other": self.get_other_panel(),
        }

    def get_character_panel(self, character: str = "") -> dict[str, object]:
        target = str(character or self.config.character or "default").strip() or "default"
        bundle = self.cards.get_editor_bundle(target)
        return {
            "current_character": self.config.character,
            **bundle,
        }

    def save_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        character = str(payload.get("character") or self.config.character or "default").strip() or "default"
        set_active = bool(payload.get("set_active", True))
        person = str(payload.get("person_content") or "")
        group = str(payload.get("group_content") or "")
        person_fields = payload.get("person_fields")
        group_fields = payload.get("group_fields")
        shared_fields = payload.get("shared_fields")
        portrait = payload.get("portrait")
        bundle = self.cards.save_editor_bundle(
            character,
            person,
            group,
            person_fields=person_fields if isinstance(person_fields, dict) else None,
            group_fields=group_fields if isinstance(group_fields, dict) else None,
            shared_fields=shared_fields if isinstance(shared_fields, dict) else None,
            portrait=portrait if isinstance(portrait, dict) else None,
        )
        if isinstance(portrait, dict) and bool(portrait.get("generate", portrait.get("auto_generate", False))):
            generated = self._generate_character_portrait(character, bundle, portrait)
            bundle["portrait"] = generated
        if set_active:
            self.config.character = character
        self._persist_config()
        return {
            "current_character": self.config.character,
            **bundle,
        }

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.cards.load_portrait_asset(character)

    def _generate_character_portrait(
        self,
        character: str,
        bundle: dict[str, object],
        portrait_payload: dict[str, object],
    ) -> dict[str, object]:
        current = dict(bundle.get("portrait", {}) if isinstance(bundle.get("portrait"), dict) else {})
        shared_fields = bundle.get("shared", {}) if isinstance(bundle.get("shared"), dict) else {}
        person = bundle.get("person", {}) if isinstance(bundle.get("person"), dict) else {}
        group = bundle.get("group", {}) if isinstance(bundle.get("group"), dict) else {}
        person_fields = person.get("fields", {}) if isinstance(person.get("fields"), dict) else {}
        group_fields = group.get("fields", {}) if isinstance(group.get("fields"), dict) else {}
        style = str(portrait_payload.get("style") or current.get("style") or "neon-pixel")
        prompt_suffix = str(
            portrait_payload.get("prompt_suffix")
            or current.get("prompt_suffix")
            or ""
        )
        auto_generate = bool(portrait_payload.get("auto_generate", current.get("auto_generate", True)))
        prompt = self.cards.build_portrait_prompt(
            character,
            shared_fields=shared_fields,
            person_fields=person_fields,
            group_fields=group_fields,
            portrait={
                "style": style,
                "prompt_suffix": prompt_suffix,
            },
        )
        if not self.generator.image_ready:
            current.update(
                {
                    "style": style,
                    "prompt_suffix": prompt_suffix,
                    "auto_generate": auto_generate,
                    "last_prompt": prompt,
                    "notice": "image provider is not configured",
                }
            )
            return current
        try:
            generated = self.generator.generate_image(prompt)
            image_bytes, content_type = self.generator.resolve_generated_image(generated.image_ref)
            portrait = self.cards.save_portrait_asset(
                character,
                image_bytes,
                content_type,
                prompt=prompt,
                style=style,
                prompt_suffix=prompt_suffix,
                auto_generate=auto_generate,
            )
            portrait["generated"] = True
            return portrait
        except Exception as exc:
            current.update(
                {
                    "style": style,
                    "prompt_suffix": prompt_suffix,
                    "auto_generate": auto_generate,
                    "last_prompt": prompt,
                    "error": str(exc),
                }
            )
            return current

    def get_ai_panel(self) -> dict[str, object]:
        return {
            "llm": deepcopy(serialize_app_config(self.config)["llm"]),
            "image_generation": deepcopy(serialize_app_config(self.config)["image_generation"]),
        }

    def save_ai_panel(self, payload: dict[str, object]) -> dict[str, object]:
        llm = payload.get("llm", {})
        image_generation = payload.get("image_generation", {})
        if isinstance(llm, dict):
            self.config.llm.enabled = bool(llm.get("enabled", self.config.llm.enabled))
            self.config.llm.backend = str(llm.get("backend", self.config.llm.backend) or self.config.llm.backend)
            self.config.llm.base_url = str(llm.get("base_url", self.config.llm.base_url) or "")
            self.config.llm.api_key = str(llm.get("api_key", self.config.llm.api_key) or "")
            self.config.llm.app_type = str(llm.get("app_type", self.config.llm.app_type) or self.config.llm.app_type)
            self.config.llm.timeout_seconds = float(llm.get("timeout_seconds", self.config.llm.timeout_seconds) or self.config.llm.timeout_seconds)
        if isinstance(image_generation, dict):
            self.config.image_generation.enabled = bool(image_generation.get("enabled", self.config.image_generation.enabled))
            self.config.image_generation.base_url = str(image_generation.get("base_url", self.config.image_generation.base_url) or "")
            self.config.image_generation.api_key = str(image_generation.get("api_key", self.config.image_generation.api_key) or "")
            self.config.image_generation.model = str(image_generation.get("model", self.config.image_generation.model) or self.config.image_generation.model)
            self.config.image_generation.timeout_seconds = float(image_generation.get("timeout_seconds", self.config.image_generation.timeout_seconds) or self.config.image_generation.timeout_seconds)
            self.config.image_generation.response_format = str(image_generation.get("response_format", self.config.image_generation.response_format) or self.config.image_generation.response_format)
            self.config.image_generation.aspect_ratio = str(image_generation.get("aspect_ratio", self.config.image_generation.aspect_ratio) or self.config.image_generation.aspect_ratio)
            self.config.image_generation.resolution = str(image_generation.get("resolution", self.config.image_generation.resolution) or "")
        self._refresh_runtime_components(rebuild_generator=True)
        self._persist_config()
        return self.get_ai_panel()

    def get_memory_panel(self) -> dict[str, object]:
        sessions = self.list_sessions(limit=60)
        knowledge_entries = self.state_store.list_knowledge(limit=80)
        return {
            "sessions": sessions,
            "knowledge_entries": knowledge_entries,
            "knowledge_count": self.state_store.knowledge_count(),
            "member_count": self.state_store.member_count(),
        }

    def save_memory_session(
        self,
        launcher_type: str,
        launcher_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        session = self.memory.load(launcher_id, launcher_type)
        if not session.history and not session.metadata and not session.preferred_name:
            return None
        session.preferred_name = str(payload.get("preferred_name", session.preferred_name) or "")
        history_value = payload.get("history", session.history)
        if isinstance(history_value, list):
            session.history = [str(item) for item in history_value]
        elif isinstance(history_value, str):
            session.history = [line for line in history_value.splitlines() if line.strip()]
        metadata_value = payload.get("metadata", session.metadata)
        if isinstance(metadata_value, dict):
            session.metadata = metadata_value
        self.memory.store.save(session)
        return self.get_session_detail(launcher_type, launcher_id)

    def get_member_directory_panel(self, *, limit: int = 120) -> dict[str, object]:
        return {
            "members": self.state_store.list_members(limit=limit),
            "member_count": self.state_store.member_count(),
        }

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        return self.state_store.save_member(dict(payload))

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        return self.state_store.save_knowledge(dict(payload))

    def sync_group_members(self, group_id: str) -> dict[str, object]:
        safe_group_id = str(group_id or "").strip()
        if not safe_group_id:
            raise ValueError("group_id is required")
        if self.config.qq_sidecar.dry_run or not self.config.qq_sidecar.outbound_base_url:
            raise ValueError("sidecar is not available")
        client = OneBotActionClient(
            base_url=self.config.qq_sidecar.outbound_base_url,
            timeout=self.config.qq_sidecar.outbound_timeout_seconds,
            access_token=self.config.qq_sidecar.access_token,
        )
        response = client.get_group_member_list(safe_group_id)
        members = response.get("data", response)
        if not isinstance(members, list):
            raise ValueError("group member payload is invalid")
        synced: list[dict[str, object]] = []
        for item in members:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", "") or "").strip()
            if not user_id:
                continue
            saved = self.state_store.record_member_seen(
                group_id=safe_group_id,
                user_id=user_id,
                qq_nickname=str(item.get("nickname", "") or ""),
                group_card=str(item.get("card", "") or ""),
            )
            synced.append(saved)
        return {
            "status": "ok",
            "group_id": safe_group_id,
            "count": len(synced),
            "members": synced[:20],
        }

    def get_abilities_panel(self) -> dict[str, object]:
        return {
            "search_enabled": self.config.search_enabled,
            "search_result_limit": self.config.search_result_limit,
            "search_timeout_seconds": self.config.search_timeout_seconds,
            "thinking_mode": self.config.thinking_mode,
            "conversation_analysis": self.config.conversation_analysis,
            "summarization_mode": self.config.summarization_mode,
            "max_active_skills": self.config.max_active_skills,
            "history_window_messages": self.config.history_window_messages,
            "memory_recall_limit": self.config.memory_recall_limit,
            "max_thinking_words": self.config.max_thinking_words,
            "short_term_memory_limit": self.config.short_term_memory_limit,
            "memory_summary_batch_size": self.config.memory_summary_batch_size,
            "tools": self.list_tools(),
            "marketplace": self.marketplace.describe(),
        }

    def save_abilities_panel(self, payload: dict[str, object]) -> dict[str, object]:
        self.config.search_enabled = bool(payload.get("search_enabled", self.config.search_enabled))
        self.config.search_result_limit = int(payload.get("search_result_limit", self.config.search_result_limit) or self.config.search_result_limit)
        self.config.search_timeout_seconds = float(payload.get("search_timeout_seconds", self.config.search_timeout_seconds) or self.config.search_timeout_seconds)
        self.config.thinking_mode = bool(payload.get("thinking_mode", self.config.thinking_mode))
        self.config.conversation_analysis = bool(payload.get("conversation_analysis", self.config.conversation_analysis))
        self.config.summarization_mode = bool(payload.get("summarization_mode", self.config.summarization_mode))
        self.config.max_active_skills = int(payload.get("max_active_skills", self.config.max_active_skills) or self.config.max_active_skills)
        self.config.history_window_messages = int(payload.get("history_window_messages", self.config.history_window_messages) or self.config.history_window_messages)
        self.config.memory_recall_limit = int(payload.get("memory_recall_limit", self.config.memory_recall_limit) or self.config.memory_recall_limit)
        self.config.max_thinking_words = int(payload.get("max_thinking_words", self.config.max_thinking_words) or self.config.max_thinking_words)
        self.config.short_term_memory_limit = int(payload.get("short_term_memory_limit", self.config.short_term_memory_limit) or self.config.short_term_memory_limit)
        self.config.memory_summary_batch_size = int(payload.get("memory_summary_batch_size", self.config.memory_summary_batch_size) or self.config.memory_summary_batch_size)
        self._refresh_runtime_components()
        self._persist_config()
        return self.get_abilities_panel()

    def get_skills_panel(self) -> dict[str, object]:
        return {
            "skills": self.list_skills(),
            "tools": self.list_tools(),
            "marketplace": self.marketplace.describe(),
        }

    def search_marketplace(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        return self.marketplace.search(query, source_id=source_id, limit=limit)

    def import_marketplace_skill(
        self,
        *,
        source_id: str,
        github_url: str,
    ) -> dict[str, object]:
        payload = self.marketplace.fetch_skill_markdown(source_id, github_url)
        skill = self.install_skill(payload["markdown"], filename=payload["filename"])
        skill["raw_url"] = payload["raw_url"]
        return skill

    def get_sidecar_panel(self, *, refresh: bool = False) -> dict[str, object]:
        status: dict[str, object] = {
            "mode": "dry_run" if self.config.qq_sidecar.dry_run else "offline",
            "adapter_name": self.config.qq_sidecar.adapter_name,
            "outbound_base_url": self.config.qq_sidecar.outbound_base_url,
            "access_token": self.config.qq_sidecar.access_token,
            "inbound_host": self.config.qq_sidecar.inbound_host,
            "inbound_port": self.config.qq_sidecar.inbound_port,
            "reverse_ws_url": self.config.qq_sidecar.reverse_ws_url,
            "outbound_timeout_seconds": self.config.qq_sidecar.outbound_timeout_seconds,
            "dry_run": self.config.qq_sidecar.dry_run,
            "details": {},
        }
        if refresh and not self.config.qq_sidecar.dry_run and self.config.qq_sidecar.outbound_base_url:
            client = OneBotActionClient(
                base_url=self.config.qq_sidecar.outbound_base_url,
                timeout=self.config.qq_sidecar.outbound_timeout_seconds,
                access_token=self.config.qq_sidecar.access_token,
            )
            try:
                status["mode"] = "online"
                status["details"] = {
                    "version": client.get_version_info(),
                    "login": client.get_login_info(),
                    "status": client.get_status(),
                }
            except Exception as exc:
                status["mode"] = "error"
                status["error"] = str(exc)
        return status

    def save_sidecar_panel(self, payload: dict[str, object]) -> dict[str, object]:
        self.config.qq_sidecar.adapter_name = str(payload.get("adapter_name", self.config.qq_sidecar.adapter_name) or self.config.qq_sidecar.adapter_name)
        self.config.qq_sidecar.outbound_base_url = str(payload.get("outbound_base_url", self.config.qq_sidecar.outbound_base_url) or "")
        self.config.qq_sidecar.outbound_timeout_seconds = float(payload.get("outbound_timeout_seconds", self.config.qq_sidecar.outbound_timeout_seconds) or self.config.qq_sidecar.outbound_timeout_seconds)
        self.config.qq_sidecar.access_token = str(payload.get("access_token", self.config.qq_sidecar.access_token) or "")
        self.config.qq_sidecar.inbound_host = str(payload.get("inbound_host", self.config.qq_sidecar.inbound_host) or self.config.qq_sidecar.inbound_host)
        self.config.qq_sidecar.inbound_port = int(payload.get("inbound_port", self.config.qq_sidecar.inbound_port) or self.config.qq_sidecar.inbound_port)
        self.config.qq_sidecar.reverse_ws_url = str(payload.get("reverse_ws_url", self.config.qq_sidecar.reverse_ws_url) or self.config.qq_sidecar.reverse_ws_url)
        self.config.qq_sidecar.dry_run = bool(payload.get("dry_run", self.config.qq_sidecar.dry_run))
        self._refresh_runtime_components(rebuild_outbound=True)
        self._persist_config()
        return self.get_sidecar_panel(refresh=False)

    def get_other_panel(self) -> dict[str, object]:
        return {
            "service_name": self.config.service_name,
            "assistant_name": self.config.assistant_name,
            "bot_account_id": self.config.bot_account_id,
            "group_reply_requires_mention": self.config.group_reply_requires_mention,
            "image_command_prefix": self.config.image_command_prefix,
            "image_command_aliases": list(self.config.image_command_aliases),
            "ignore_prefixes": list(self.config.ignore_prefixes),
            "group_follow_up_window_seconds": self.config.group_follow_up_window_seconds,
            "group_response_delay_seconds": self.config.group_response_delay_seconds,
            "repeat_trigger_count": self.config.repeat_trigger_count,
            "multimodal_enabled": self.config.multimodal_enabled,
            "data_root": self.config.data_root,
            "config_path": self.config.config_path,
            "marketplace": deepcopy(serialize_app_config(self.config)["marketplace"]),
        }

    def save_other_panel(self, payload: dict[str, object]) -> dict[str, object]:
        follow_up_raw = payload.get("group_follow_up_window_seconds", self.config.group_follow_up_window_seconds)
        reply_delay_raw = payload.get("group_response_delay_seconds", self.config.group_response_delay_seconds)
        repeat_trigger_raw = payload.get("repeat_trigger_count", self.config.repeat_trigger_count)
        self.config.service_name = str(payload.get("service_name", self.config.service_name) or self.config.service_name)
        self.config.assistant_name = str(payload.get("assistant_name", self.config.assistant_name) or self.config.assistant_name)
        self.config.bot_account_id = str(payload.get("bot_account_id", self.config.bot_account_id) or "")
        self.config.group_reply_requires_mention = bool(payload.get("group_reply_requires_mention", self.config.group_reply_requires_mention))
        self.config.image_command_prefix = str(payload.get("image_command_prefix", self.config.image_command_prefix) or self.config.image_command_prefix)
        self.config.image_command_aliases = self._coerce_list(payload.get("image_command_aliases", self.config.image_command_aliases))
        self.config.ignore_prefixes = self._coerce_list(payload.get("ignore_prefixes", self.config.ignore_prefixes))
        self.config.group_follow_up_window_seconds = max(
            0.0,
            float(self.config.group_follow_up_window_seconds if follow_up_raw is None else follow_up_raw),
        )
        self.config.group_response_delay_seconds = max(
            0.0,
            float(self.config.group_response_delay_seconds if reply_delay_raw is None else reply_delay_raw),
        )
        self.config.repeat_trigger_count = max(
            0,
            int(self.config.repeat_trigger_count if repeat_trigger_raw is None else repeat_trigger_raw),
        )
        self.config.multimodal_enabled = bool(payload.get("multimodal_enabled", self.config.multimodal_enabled))
        marketplace = payload.get("marketplace", {})
        if isinstance(marketplace, dict):
            self.config.marketplace.enabled = bool(marketplace.get("enabled", self.config.marketplace.enabled))
            self.config.marketplace.default_query = str(marketplace.get("default_query", self.config.marketplace.default_query) or self.config.marketplace.default_query)
            sources = marketplace.get("sources")
            if isinstance(sources, list):
                next_sources = []
                for item in sources:
                    if not isinstance(item, dict):
                        continue
                    next_sources.append(type(self.config.marketplace.sources[0])(
                        source_id=str(item.get("source_id", item.get("id", "skillsmp")) or "skillsmp"),
                        name=str(item.get("name", "SkillsMP") or "SkillsMP"),
                        kind=str(item.get("kind", "skillsmp") or "skillsmp"),
                        enabled=bool(item.get("enabled", True)),
                        base_url=str(item.get("base_url", "https://skillsmp.com") or "https://skillsmp.com"),
                        search_path=str(item.get("search_path", "/api/v1/skills/search") or "/api/v1/skills/search"),
                        browse_url=str(item.get("browse_url", "https://skillsmp.com/zh") or "https://skillsmp.com/zh"),
                        api_key=str(item.get("api_key", "") or ""),
                        timeout_seconds=float(item.get("timeout_seconds", 10.0) or 10.0),
                        max_results=int(item.get("max_results", 12) or 12),
                    ))
                if next_sources:
                    self.config.marketplace.sources = next_sources
        self._refresh_runtime_components()
        self._persist_config()
        return self.get_other_panel()

    def skill_pack_template(self) -> dict[str, object]:
        return build_skill_pack_template()

    def export_skill_pack(
        self,
        *,
        skill_ids: list[str] | None = None,
        include_builtin: bool = False,
        name: str = "",
        description: str = "",
    ) -> dict[str, object]:
        return export_skill_pack(
            self.skills,
            skill_ids=skill_ids,
            include_builtin=include_builtin,
            name=name,
            description=description,
        )

    def import_skill_pack(
        self,
        payload: dict[str, object] | str,
        *,
        overwrite: bool = True,
    ) -> dict[str, object]:
        return import_skill_pack(self.skills, payload, overwrite=overwrite)

    def get_skill_detail(self, skill_id: str) -> dict[str, object] | None:
        skill = self.skills.get_skill(skill_id)
        if skill is None:
            return None
        detail = skill.as_dict()
        detail["markdown"] = self.skills.get_skill_markdown(skill_id) or ""
        return detail

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, object] | None:
        skill = self.skills.set_enabled(skill_id, enabled)
        if skill is None:
            return None
        return skill.as_dict()

    def install_skill(self, markdown: str, filename: str | None = None) -> dict[str, object]:
        skill = self.skills.install_workspace_skill(markdown, filename=filename)
        detail = skill.as_dict()
        detail["markdown"] = self.skills.get_skill_markdown(skill.skill_id) or markdown
        return detail

    def save_skill(self, skill_id: str, markdown: str) -> dict[str, object] | None:
        skill = self.skills.save_workspace_skill(skill_id, markdown)
        if skill is None:
            return None
        detail = skill.as_dict()
        detail["markdown"] = self.skills.get_skill_markdown(skill.skill_id) or markdown
        return detail

    def delete_skill(self, skill_id: str) -> bool:
        return self.skills.delete_workspace_skill(skill_id)

    def reload_skills(self) -> dict[str, object]:
        return self.skills.reload()

    def new_skill_template(self) -> dict[str, str]:
        return {
            "markdown": build_skill_markdown_template(
                skill_id="custom-skill",
                name="自定义技能",
                description="描述这个技能的职责。",
                triggers=["触发词"],
                mode="prefix",
                priority=4,
                body="在这里写下给模型看的技能说明，或者加上 command-dispatch / command-tool 做工具分发。",
            )
        }

    def _record_outbound(self, message: OutboundMessage) -> None:
        with self._state_lock:
            self._recent_outbound.append(message)
            self._recent_outbound = self._recent_outbound[-24:]
            self._event_counter += 1
            entry = {
                "seq": self._event_counter,
                "kind": "outbound",
                "timestamp": time.time(),
                "launcher_type": message.launcher_type,
                "launcher_id": message.launcher_id,
                "text": message.text,
                "images": list(message.images),
            }
            self._recent_events.append(entry)
            self._recent_events = self._recent_events[-200:]

    def _record_inbound(self, event: InboundEvent, text: str) -> None:
        fallback = ""
        to_memory_text = getattr(event, "to_memory_text", None)
        if callable(to_memory_text):
            try:
                fallback = str(to_memory_text() or "")
            except Exception:
                fallback = ""
        display_text = text or fallback
        with self._state_lock:
            self._event_counter += 1
            entry = {
                "seq": self._event_counter,
                "kind": "inbound",
                "timestamp": time.time(),
                "launcher_type": event.launcher_type,
                "launcher_id": event.launcher_id,
                "sender_id": event.sender_id,
                "sender_name": event.sender_name,
                "message_id": event.message_id,
                "text": display_text,
                "image_count": event.image_count,
                "mentioned_bot": event.has_bot_mention(self.config.bot_account_id),
            }
            self._recent_events.append(entry)
            self._recent_events = self._recent_events[-200:]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._state_lock:
            return [dict(entry) for entry in self._recent_events[-limit:][::-1]]

    def runtime_stats(self) -> dict[str, Any]:
        with self._state_lock:
            uptime = max(0.0, time.monotonic() - self._started_at)
            inbound = sum(1 for entry in self._recent_events if entry.get("kind") == "inbound")
            outbound = sum(1 for entry in self._recent_events if entry.get("kind") == "outbound")
            active = sum(
                1
                for _, until in self._group_follow_up_until.items()
                if until > time.monotonic()
            )
            return {
                "uptime_seconds": uptime,
                "recent_inbound": inbound,
                "recent_outbound": outbound,
                "active_followups": active,
                "total_events": self._event_counter,
            }

    def _persist_config(self) -> None:
        if not self.config.config_path:
            return
        ConfigManager().save(self.config)

    def _refresh_runtime_components(self, *, rebuild_generator: bool = False, rebuild_outbound: bool = False) -> None:
        if rebuild_generator:
            self.generator = Generator(self.config)
            self.cards = self.generator._cards
            self.thoughts = Thoughts(self.config, self.generator)
        self.search = SearchDecider(self.config)
        self.marketplace = MarketplaceClient(self.config.marketplace)
        self.skills.config = self.config
        if rebuild_outbound:
            self.outbound = _build_runtime_outbound(self.config)

    @staticmethod
    def _coerce_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.splitlines() if item.strip()]
        return []

    def _store_active_skills(self, session: SessionMemory, active_skills: list[SkillSpec]) -> None:
        if active_skills:
            session.metadata["active_skills"] = [self._skill_to_dict(skill) for skill in active_skills]
        else:
            session.metadata.pop("active_skills", None)
        self.memory.store.save(session)

    def _store_search_context(self, session: SessionMemory, search_context: SearchContext) -> None:
        if search_context.query:
            session.metadata["last_search"] = search_context.as_dict()
        else:
            session.metadata.pop("last_search", None)
        self.memory.store.save(session)

    def _resolve_address(self, event: InboundEvent, session: SessionMemory) -> str:
        member = self.state_store.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
        )
        if member is not None:
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return preferred_name
        if session.preferred_name:
            return session.preferred_name
        card_metadata = session.metadata.get("card", {})
        if event.launcher_type == "person" and isinstance(card_metadata, dict):
            user_name = str(card_metadata.get("user_name", "") or "").strip()
            if user_name:
                return user_name
        return event.sender_name or "you"

    def _outbound_mode_label(self) -> str:
        if self.config.qq_sidecar.dry_run or not self.config.qq_sidecar.outbound_base_url:
            return "capture(dry-run)"
        return f"{self.config.qq_sidecar.adapter_name} -> {self.config.qq_sidecar.outbound_base_url}"

    @staticmethod
    def _message_to_dict(message: OutboundMessage) -> dict[str, object]:
        return {
            "launcher_id": message.launcher_id,
            "launcher_type": message.launcher_type,
            "text": message.text,
            "images": list(message.images),
        }

    @staticmethod
    def _skill_to_dict(skill: SkillSpec) -> dict[str, object]:
        return {
            "id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "triggers": list(skill.triggers),
            "mode": skill.mode,
            "priority": skill.priority,
            "source": skill.source,
            "source_kind": skill.source_kind,
            "enabled": skill.enabled,
            "user_invocable": skill.user_invocable,
            "disable_model_invocation": skill.disable_model_invocation,
            "command_dispatch": skill.command_dispatch,
            "command_tool": skill.command_tool,
            "command_arg_mode": skill.command_arg_mode,
            "editable": skill.source_kind == "workspace",
            "deletable": skill.source_kind == "workspace",
        }

    @staticmethod
    def _active_skill_names(session: SessionMemory) -> list[str]:
        raw_value = session.metadata.get("active_skills", [])
        if not isinstance(raw_value, list):
            return []
        names: list[str] = []
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _last_search_query(session: SessionMemory) -> str:
        raw_value = session.metadata.get("last_search", {})
        if not isinstance(raw_value, dict):
            return ""
        return str(raw_value.get("query", "") or "").strip()

    @staticmethod
    def _count_group_members(session: SessionMemory) -> int:
        group_members = session.metadata.get("group_members", {})
        if isinstance(group_members, dict):
            return len(group_members)
        return 0


def build_default_service(config: AppConfig | None = None) -> tuple[WaifuService, CapturingOutboundPort]:
    app_config = config or AppConfig()
    return _build_service(app_config, InMemoryStore(), InMemoryRuntimeStateStore(), CapturingOutboundPort())


def build_file_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, CapturingOutboundPort]:
    app_config = config or AppConfig()
    root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    state_root = Path(app_config.data_root) / "state" / "runtime.sqlite3"
    return _build_service(app_config, FileMemoryStore(root), SqliteRuntimeStateStore(state_root), CapturingOutboundPort())


def build_runtime_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, OutboundPort]:
    app_config = config or AppConfig()
    root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    outbound = _build_runtime_outbound(app_config)
    state_root = Path(app_config.data_root) / "state" / "runtime.sqlite3"
    return _build_service(app_config, FileMemoryStore(root), SqliteRuntimeStateStore(state_root), outbound)


def _build_runtime_outbound(app_config: AppConfig) -> OutboundPort:
    if app_config.qq_sidecar.dry_run or not app_config.qq_sidecar.outbound_base_url:
        return CapturingOutboundPort()
    client = OneBotActionClient(
        base_url=app_config.qq_sidecar.outbound_base_url,
        timeout=app_config.qq_sidecar.outbound_timeout_seconds,
        access_token=app_config.qq_sidecar.access_token,
    )
    return OneBotHttpOutboundPort(client)


def _build_service(
    app_config: AppConfig,
    store: Any,
    state_store: Any,
    outbound: OutboundPort,
) -> tuple[WaifuService, OutboundPort]:
    generator = Generator(app_config)
    cards = generator._cards
    skills = SkillRegistry(app_config)
    tools = ToolRegistry()
    service = WaifuService(
        config=app_config,
        memory=Memory(store),
        emotions=EmotionSensor(),
        thoughts=Thoughts(app_config, generator),
        generator=generator,
        cards=cards,
        search=SearchDecider(app_config),
        marketplace=MarketplaceClient(app_config.marketplace),
        skills=skills,
        tools=tools,
        state_store=state_store,
        outbound=outbound,
    )
    tools.register(
        "image",
        name="图片生成",
        description="调用图像生成能力并返回图文消息。",
        handler=service._run_image_tool,
    )
    tools.register(
        "search",
        name="联网搜索",
        description="执行联网检索并把摘要整理成回复。",
        handler=service._run_search_tool,
    )
    tools.register(
        "summary",
        name="会话总结",
        description="总结最近会话并提取重点标签。",
        handler=service._run_summary_tool,
    )
    return service, outbound
