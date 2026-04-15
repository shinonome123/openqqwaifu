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
from .cells.embedding_service import EmbeddingClient
from .cells.generator import Generator
from .cells.marketplace import MarketplaceClient
from .cells.skill_pack import build_skill_pack_template, export_skill_pack, import_skill_pack
from .cells.skill_registry import SkillRegistry, SkillSpec, build_skill_markdown_template
from .cells.tool_registry import ToolInvocation, ToolRegistry
from .config import AppConfig
from .contracts import OutboundPort
from .gateways.napcat_login import (
    NapCatLoginBridge,
    NapCatLoginError,
    normalize_webui_settings,
    qrcode_payload_to_image_source,
)
from .gateways.onebot_actions import OneBotActionClient, OneBotHttpOutboundPort
from .memory import FileMemoryStore, InMemoryStore
from .models import EmotionState, InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from .organs.memory_graph import MemoryGraphBuilder
from .organs.memories import Memory
from .organs.proactive import ProactivePlanner
from .organs.thoughts import Thoughts
from .services import CapturingOutboundPort
from .state_store import InMemoryRuntimeStateStore, SqliteRuntimeStateStore
from .systems.events import BehaviorEventEngine
from .systems.emotions import EmotionSensor
from .systems.narrator import Narrator
from .systems.searching import SearchContext, SearchDecider
from .systems.value_game import ValueGameEngine

_MASK_SENTINEL = "..."


def _mask_key(key: str) -> str:
    """Return a masked version of a sensitive value (e.g. API key)."""
    if not key or len(key) < 8:
        return "***" if key else ""
    return key[:4] + "..." + key[-4:]


def _is_masked(value: str) -> bool:
    """Return True if *value* looks like it was produced by ``_mask_key``."""
    return value == "***" or _MASK_SENTINEL in value


def _safe_int(payload: dict[str, object], key: str, default: int) -> int:
    """Return ``int(payload[key])`` if *key* is present, otherwise *default*.

    Unlike the ``int(x or default)`` pattern this correctly accepts ``0``.
    """
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _safe_float(payload: dict[str, object], key: str, default: float) -> float:
    """Return ``float(payload[key])`` if *key* is present, otherwise *default*."""
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class WaifuService:
    config: AppConfig
    memory: Memory
    emotions: EmotionSensor
    thoughts: Thoughts
    generator: Generator
    cards: CardManager
    search: SearchDecider
    event_engine: BehaviorEventEngine
    narrator: Narrator
    value_game: ValueGameEngine
    memory_graph: MemoryGraphBuilder
    proactive: ProactivePlanner
    marketplace: MarketplaceClient
    skills: SkillRegistry
    tools: ToolRegistry
    state_store: Any
    outbound: OutboundPort
    napcat_login: NapCatLoginBridge | None = None
    _group_follow_up_until: dict[str, float] = field(default_factory=dict)
    _recent_outbound: list[OutboundMessage] = field(default_factory=list)
    _recent_events: list[dict[str, Any]] = field(default_factory=list)
    _recent_behavior_events: list[dict[str, Any]] = field(default_factory=list)
    _started_at: float = field(default_factory=time.monotonic, repr=False)
    _event_counter: int = 0
    _state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _session_locks: dict[tuple[str, str], threading.Lock] = field(default_factory=dict, repr=False)

    def handle_event(self, event: InboundEvent) -> OutboundMessage | None:
        with self._session_lock_for(event):
            return self._handle_event_locked(event)

    def _handle_event_locked(self, event: InboundEvent) -> OutboundMessage | None:
        text = event.command_text(self.config.bot_account_id)
        live_runtime = self._requires_live_llm()
        self._record_inbound(event, text)
        if not text and event.image_count == 0:
            return None
        if text and any(text.startswith(prefix) for prefix in self.config.ignore_prefixes):
            return None
        if not self._should_reply(event):
            return None
        if live_runtime and not self.generator.llm_ready:
            return None

        self._remember_directory_member(event)
        session = self.memory.save_user_event(event)
        assistant_name = self.generator.resolve_assistant_name(event.launcher_type, session)
        address = self._resolve_address(event, session)
        latest_message = self._latest_message_text(event, text)
        inbound_behavior = self.event_engine.capture_inbound(event, text=latest_message)
        self._record_behavior_event(inbound_behavior)

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
        recalled_memory_hints = self.state_store.recall_knowledge(
            scopes=self._knowledge_scopes(event),
            query=latest_message,
            limit=self.config.memory_recall_limit,
        )
        knowledge_entries = self._session_knowledge_entries(event, latest_message)
        memory_hints = self._merge_memory_hints(
            recalled_memory_hints,
            [str(item.get("summary", "") or "").strip() for item in knowledge_entries],
            limit=self.config.memory_recall_limit,
        )
        member_record = self._member_record(event)
        behavior_context = self._behavior_context(event)
        graph_snapshot = self.memory_graph.build(
            event=event,
            session=session,
            member=member_record,
            knowledge_entries=knowledge_entries,
            behavior_events=behavior_context,
        )
        speaker_notes = self._directory_member_notes(event)
        narrator_hint = self.narrator.build_hint(
            event=event,
            latest_message=latest_message,
            address=address,
            emotion=emotion,
            memory_graph=graph_snapshot,
        )
        if narrator_hint:
            speaker_notes.append(narrator_hint)
        for highlight in graph_snapshot.get("highlights", [])[:3] if isinstance(graph_snapshot, dict) else []:
            text_hint = str(highlight or "").strip()
            if text_hint:
                speaker_notes.append(text_hint)
        analysis_hint = self.thoughts.analyze(
            event,
            session,
            assistant_name=assistant_name,
            address=address,
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            active_skills=active_skills,
            allow_fallback=not live_runtime,
        )
        reply_text = self.generator.generate_reply(
            event,
            session,
            emotion,
            assistant_name=assistant_name,
            address_override=address,
            search_hint=search_context.summary,
            search_context=search_context.to_prompt_block(),
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            analysis_hint=analysis_hint,
            active_skills=active_skills,
            allow_fallback=not live_runtime,
        )
        if live_runtime and not str(reply_text or "").strip():
            return None
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return self._emit_message(
            event,
            message,
            assistant_name=assistant_name,
            emotion=emotion,
            search_used=bool(search_context.active),
            behavior_reason="reply",
        )

    def _session_lock_for(self, event: InboundEvent) -> threading.Lock:
        key = (str(event.launcher_type or "").strip(), str(event.launcher_id or "").strip())
        with self._state_lock:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[key] = lock
            return lock

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

    def _run_skill_list_tool(self, invocation: ToolInvocation) -> OutboundMessage:
        return self._handle_skill_list_request(
            invocation.event,
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

    def _handle_skill_list_request(
        self,
        event: InboundEvent,
        *,
        address: str,
        assistant_name: str,
    ) -> OutboundMessage:
        all_skills = self.skills.list_skills()
        enabled_skills = [s for s in all_skills if s.enabled and s.skill_id != "skill-list-command"]

        if not enabled_skills:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=f"{address}，我现在还没有任何已启用的技能。",
            )
            return self._emit_message(event, message, assistant_name=assistant_name)

        _SKILL_ICONS: dict[str, str] = {
            "image-command": "\U0001f3a8",
            "image-handoff": "\U0001f5bc\ufe0f",
            "search-command": "\U0001f50d",
            "summary-command": "\U0001f4dd",
            "concise-answer": "\U0001f4ac",
            "freshness-check": "\U0001f550",
        }
        lines = [f"{address}，我目前掌握的技能有：\n"]
        for skill in enabled_skills:
            icon = _SKILL_ICONS.get(skill.skill_id, "\u2728")
            trigger_hint = ""
            if skill.triggers:
                if skill.mode == "prefix":
                    examples = skill.triggers[:2]
                    trigger_hint = " \u2014 \u8bf4\u300c" + "\u300d\u6216\u300c".join(examples) + "\u300d"
                else:
                    examples = skill.triggers[:3]
                    trigger_hint = " \u2014 \u63d0\u5230\u300c" + "\u300d\u300c".join(examples) + "\u300d\u65f6\u6fc0\u6d3b"
            lines.append(f"{icon} {skill.name}{trigger_hint}")
            if skill.description:
                lines.append(f"   {skill.description}")

        total = len(enabled_skills)
        workspace_count = sum(1 for s in enabled_skills if s.source_kind == "workspace")
        if workspace_count:
            lines.append(f"\n\u5171 {total} \u4e2a\u6280\u80fd\uff08\u5176\u4e2d {workspace_count} \u4e2a\u662f\u81ea\u5b9a\u4e49\u6280\u80fd\uff09\u3002")
        else:
            lines.append(f"\n\u5171 {total} \u4e2a\u6280\u80fd\u3002")

        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text="\n".join(lines),
        )
        return self._emit_message(event, message, assistant_name=assistant_name)

    def _emit_message(
        self,
        event: InboundEvent,
        message: OutboundMessage,
        *,
        assistant_name: str,
        emotion: EmotionState | None = None,
        search_used: bool = False,
        behavior_reason: str = "",
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
        self._record_behavior_event(
            self.event_engine.capture_outbound(event=event, message=message, reason=behavior_reason or "reply")
        )
        self.value_game.apply(
            state_store=self.state_store,
            event=event,
            emotion=emotion or EmotionState(),
            reply_text=message.text,
            search_used=search_used,
        )
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
        summary = str(archived.get("summary", "") or "").strip()
        if not summary:
            return
        self.state_store.add_knowledge(
            scope_type=launcher_type,
            scope_id=launcher_id,
            memory_type="summary",
            summary=summary,
            tags=[str(tag).strip() for tag in archived.get("tags", []) if str(tag).strip()]
            if isinstance(archived.get("tags"), list)
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

    def _extract_image_prompt_legacy(self, text: str) -> str | None:
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

    def _maybe_handle_member_onboarding(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> OutboundMessage | None:
        allow_fallback = not self._requires_live_llm()
        if event.launcher_type == "person":
            member = self.state_store.get_member(group_id="", user_id=event.sender_id) or {}
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return None
            candidate = self.memory.extract_preferred_name(latest_message)
            if not candidate:
                return None
            reply_text = self.generator.generate_onboarding_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="confirm_name",
                candidate_name=candidate,
                address_override=candidate,
                allow_fallback=allow_fallback,
            )
            if not reply_text:
                return None
            self.state_store.save_member(
                {
                    "group_id": "",
                    "user_id": event.sender_id,
                    "qq_nickname": event.sender_name,
                    "preferred_name": candidate,
                    "onboarding_status": "ready",
                }
            )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=reply_text,
            )
            return self._emit_message(event, message, assistant_name=assistant_name)
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
                reply_text = self.generator.generate_onboarding_reply(
                    event,
                    session,
                    assistant_name=assistant_name,
                    stage="confirm_name",
                    candidate_name=candidate,
                    address_override=candidate,
                    allow_fallback=allow_fallback,
                )
                if not reply_text:
                    return None
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
                    text=reply_text,
                )
                return self._emit_message(event, message, assistant_name=assistant_name)
            if self._should_retry_member_onboarding_prompt(event):
                reply_text = self.generator.generate_onboarding_reply(
                    event,
                    session,
                    assistant_name=assistant_name,
                    stage="retry_name",
                    allow_fallback=allow_fallback,
                )
                if reply_text:
                    message = OutboundMessage(
                        launcher_id=event.launcher_id,
                        launcher_type=event.launcher_type,
                        text=reply_text,
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
            reply_text = self.generator.generate_onboarding_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="ask_name",
                allow_fallback=allow_fallback,
            )
            if not reply_text:
                return None
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=reply_text,
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
        if compact.startswith("@"):
            return ""
        blocked_fragments = (
            "什么",
            "玩意",
            "东西",
            "怎么",
            "咋",
            "为啥",
            "干嘛",
            "谁",
            "你是",
            "不是",
        )
        if any(fragment in compact for fragment in blocked_fragments):
            return ""
        if compact.endswith(("吗", "呢", "呀", "啊", "吧")):
            return ""
        lowered = compact.casefold()
        blocked_exact = {
            "你好",
            "您好",
            "嗨",
            "哈喽",
            "hello",
            "hi",
            "hey",
            "在吗",
            "在嘛",
            "是吗",
            "不是",
            "爸爸吗",
            "卧槽",
        }
        if lowered in blocked_exact:
            return ""
        return compact

    def _should_retry_member_onboarding_prompt(self, event: InboundEvent) -> bool:
        if event.launcher_type != "group":
            return False
        bot_account_id = str(self.config.bot_account_id or "").strip()
        return bool(bot_account_id and event.has_bot_mention(bot_account_id))

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

    def _member_record(self, event: InboundEvent) -> dict[str, Any] | None:
        return self.state_store.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
        )

    def _session_knowledge_entries(self, event: InboundEvent, query: str) -> list[dict[str, Any]]:
        scopes = set(self._knowledge_scopes(event))
        query_terms = self._extract_terms(query)
        entries = self.state_store.list_knowledge(limit=max(80, self.config.memory_graph_limit * 6))
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            scope = (
                str(entry.get("scope_type", "") or "").strip().lower() or "global",
                str(entry.get("scope_id", "") or "").strip(),
            )
            if scope not in scopes:
                continue
            summary = str(entry.get("summary", "") or "").strip().lower()
            tags = [str(item or "").strip().lower() for item in entry.get("tags", []) if str(item or "").strip()]
            score = float(entry.get("confidence") or 0.0)
            if not query_terms:
                score += 0.1
            else:
                for term in query_terms:
                    if term in summary:
                        score += 1.2
                    elif any(term in tag or tag in term for tag in tags):
                        score += 1.6
            scored.append((score, dict(entry)))
        scored.sort(
            key=lambda item: (
                -float(item[0]),
                -int(item[1].get("updated_at") or 0),
                -int(item[1].get("id") or 0),
            )
        )
        return [entry for _, entry in scored[: max(1, int(self.config.memory_graph_limit))]]

    def _behavior_context(self, event: InboundEvent, *, limit: int = 8) -> list[dict[str, Any]]:
        launcher_type = str(event.launcher_type or "").strip()
        launcher_id = str(event.launcher_id or "").strip()
        sender_id = str(event.sender_id or "").strip()
        with self._state_lock:
            items = [dict(item) for item in self._recent_behavior_events]
        scoped = [
            item
            for item in items
            if str(item.get("launcher_type", "") or "").strip() == launcher_type
            and str(item.get("launcher_id", "") or "").strip() == launcher_id
            and str(item.get("sender_id", "") or "").strip() == sender_id
        ]
        return scoped[-max(1, int(limit)) :][::-1]

    def _directory_member_notes(self, event: InboundEvent) -> list[str]:
        notes: list[str] = []
        current_group = event.launcher_id if event.launcher_type == "group" else ""
        members = self.state_store.list_members(limit=240)
        scoped = [
            item
            for item in members
            if str(item.get("group_id", "") or "").strip() == current_group
        ]
        active_key = (current_group, event.sender_id)
        scoped.sort(
            key=lambda item: (
                (str(item.get("group_id", "") or "").strip(), str(item.get("user_id", "") or "").strip()) != active_key,
                -(int(item.get("last_seen_at") or 0)),
                -(int(item.get("updated_at") or 0)),
            )
        )
        for member in scoped[:4]:
            qq_name = str(member.get("qq_nickname", "") or "").strip() or event.sender_name
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            profile_summary = str(member.get("profile_summary", "") or "").strip()
            onboarding_status = str(member.get("onboarding_status", "") or "").strip()
            affinity_score = float(member.get("affinity_score") or 0.0)
            bond_stage = self.value_game.bond_stage(affinity_score)
            if str(member.get("user_id", "") or "").strip() == event.sender_id:
                if preferred_name:
                    notes.append(f"{qq_name} prefers to be called {preferred_name}.")
                else:
                    notes.append(f"The current speaker is {qq_name}.")
                notes.append(f"Bond stage with {qq_name}: {bond_stage} ({affinity_score:.2f}).")
            elif preferred_name:
                notes.append(f"{qq_name} is usually addressed as {preferred_name}.")
            if profile_summary:
                notes.append(f"Profile summary for {qq_name}: {profile_summary}")
            if onboarding_status and onboarding_status not in {"", "ready"}:
                notes.append(f"{qq_name} is still in onboarding status: {onboarding_status}.")
        return notes

    def _migrate_legacy_session_state(self) -> None:
        store = self.memory.store
        if not hasattr(store, "list_sessions"):
            return
        sessions = store.list_sessions()  # type: ignore[no-any-return]
        existing_keys = {
            self._knowledge_entry_key(entry)
            for entry in self.state_store.list_knowledge(limit=max(1, self.state_store.knowledge_count()))
        }
        for session in sessions:
            changed = False
            member_group_id = session.launcher_id if session.launcher_type == "group" else ""
            member_user_id = session.launcher_id if session.launcher_type == "person" else ""
            legacy_preferred_name = str(session.preferred_name or "").strip()
            if legacy_preferred_name and session.launcher_type == "person":
                self.state_store.record_member_seen(
                    group_id="",
                    user_id=session.launcher_id,
                    qq_nickname=member_user_id,
                )
                self.state_store.save_member(
                    {
                        "group_id": "",
                        "user_id": session.launcher_id,
                        "preferred_name": legacy_preferred_name,
                        "onboarding_status": "ready",
                    }
                )
                changed = True
            if legacy_preferred_name:
                session.preferred_name = ""
                changed = True

            legacy_members = session.metadata.get("group_members")
            if legacy_members:
                for payload in self._iter_legacy_group_members(legacy_members):
                    user_id = str(payload.get("user_id", "") or "").strip()
                    if not user_id:
                        continue
                    self.state_store.record_member_seen(
                        group_id=session.launcher_id if session.launcher_type == "group" else "",
                        user_id=user_id,
                        qq_nickname=payload.get("qq_nickname", ""),
                        group_card=payload.get("group_card", ""),
                    )
                    preferred_name = str(payload.get("preferred_name", "") or "").strip()
                    if preferred_name or payload.get("profile_summary") or payload.get("onboarding_status"):
                        member_payload = {
                            "group_id": session.launcher_id if session.launcher_type == "group" else "",
                            "user_id": user_id,
                            "qq_nickname": payload.get("qq_nickname", ""),
                            "group_card": payload.get("group_card", ""),
                            "preferred_name": preferred_name,
                            "profile_summary": payload.get("profile_summary", ""),
                            "onboarding_status": payload.get("onboarding_status", "ready" if preferred_name else "new"),
                        }
                        self.state_store.save_member(member_payload)
                session.metadata.pop("group_members", None)
                changed = True

            legacy_long_term = session.metadata.get("long_term_memory")
            if isinstance(legacy_long_term, list):
                for item in legacy_long_term:
                    if not isinstance(item, dict):
                        continue
                    summary = str(item.get("summary", "") or "").strip()
                    if not summary:
                        continue
                    entry = {
                        "scope_type": session.launcher_type,
                        "scope_id": session.launcher_id,
                        "memory_type": str(item.get("memory_type", "summary") or "summary"),
                        "summary": summary,
                        "tags": [str(tag).strip() for tag in item.get("tags", []) if str(tag).strip()]
                        if isinstance(item.get("tags"), list)
                        else [],
                    }
                    key = self._knowledge_entry_key(entry)
                    if key in existing_keys:
                        continue
                    self.state_store.add_knowledge(
                        scope_type=entry["scope_type"],
                        scope_id=entry["scope_id"],
                        memory_type=entry["memory_type"],
                        summary=entry["summary"],
                        tags=entry["tags"],
                        confidence=float(item.get("confidence", 0.58) or 0.58),
                        archived=bool(item.get("archived", False)),
                    )
                    existing_keys.add(key)
                session.metadata.pop("long_term_memory", None)
                changed = True

            if changed:
                store.save(session)

    @staticmethod
    def _iter_legacy_group_members(raw_members: object) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        if isinstance(raw_members, dict):
            iterable = raw_members.items()
            for member_key, value in iterable:
                if isinstance(value, dict):
                    payload = dict(value)
                    payload.setdefault("user_id", payload.get("sender_id") or payload.get("qq") or member_key)
                    payload.setdefault("qq_nickname", payload.get("sender_name") or payload.get("nickname") or "")
                    payload.setdefault("group_card", payload.get("card") or "")
                    entries.append(payload)
            return entries
        if isinstance(raw_members, list):
            for value in raw_members:
                if isinstance(value, dict):
                    payload = dict(value)
                    payload.setdefault("user_id", payload.get("sender_id") or payload.get("qq") or "")
                    payload.setdefault("qq_nickname", payload.get("sender_name") or payload.get("nickname") or "")
                    payload.setdefault("group_card", payload.get("card") or "")
                    entries.append(payload)
        return entries

    @staticmethod
    def _knowledge_entry_key(entry: dict[str, object]) -> tuple[str, str, str, str, tuple[str, ...]]:
        tags = tuple(
            sorted(
                str(tag).strip()
                for tag in entry.get("tags", [])
                if str(tag).strip()
            )
        ) if isinstance(entry.get("tags"), list) else ()
        return (
            str(entry.get("scope_type", "") or "").strip(),
            str(entry.get("scope_id", "") or "").strip(),
            str(entry.get("memory_type", "") or "").strip(),
            str(entry.get("summary", "") or "").strip(),
            tags,
        )

    def dashboard_snapshot(self) -> dict[str, object]:
        sessions = self.list_sessions(limit=24)
        knowledge_count = self.state_store.knowledge_count()
        member_count = self.state_store.member_count()
        proactive_candidates = self.proactive.list_candidates(
            members=self.state_store.list_members(limit=200),
            limit=self.config.proactive_candidate_limit,
        )
        with self._state_lock:
            recent_outbound = [self._message_to_dict(message) for message in self._recent_outbound[-12:][::-1]]
            recent_behavior_events = [dict(item) for item in self._recent_behavior_events[-12:][::-1]]
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
            "event_mode": self.config.event_mode,
            "narrator_mode": self.config.narrator_mode,
            "value_game_mode": self.config.value_game_mode,
            "memory_graph_mode": self.config.memory_graph_mode,
            "proactive_mode": self.config.proactive_mode,
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
                "dry_run": False,
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
            "recent_behavior_events": recent_behavior_events,
            "proactive_candidates": proactive_candidates[:6],
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
            active_skill_names = self._active_skill_names(session)
            last_search_query = self._last_search_query(session)
            result.append(
                {
                    "launcher_id": session.launcher_id,
                    "launcher_type": session.launcher_type,
                    "preferred_name": self._session_preferred_name(session),
                    "assistant_name": card_metadata.get(
                        "assistant_name",
                        session.metadata.get("assistant_name", self.config.assistant_name),
                    )
                    if isinstance(card_metadata, dict)
                    else self.config.assistant_name,
                    "history_count": len(history),
                    "message_count": len(history),
                    "long_term_count": self._knowledge_count_for_session(session),
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
        clean_metadata = self._sanitize_session_metadata(session.metadata)
        if not session.history and not clean_metadata:
            return None
        graph = self._session_detail_graph(session)
        return {
            "launcher_id": session.launcher_id,
            "launcher_type": session.launcher_type,
            "preferred_name": self._session_preferred_name(session),
            "history": list(session.history),
            "metadata": clean_metadata,
            "memory_graph": graph,
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
            "qq_login": self.get_qq_login_panel(refresh=False),
            "sidecar": self.get_sidecar_panel(refresh=False),
            "other": self.get_other_panel(),
        }

    def get_character_panel(self, character: str = "") -> dict[str, object]:
        target = str(character or self.cards.active_character()).strip() or "default"
        bundle = self.cards.get_editor_bundle(target)
        return {
            "current_character": self.cards.active_character(),
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
            active_character = self.cards.set_active_character(character)
            self.config.character = active_character
        self._persist_config()
        return {
            "current_character": self.cards.active_character(),
            **bundle,
        }

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.cards.load_portrait_asset(character)

    def preview_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        launcher_type = str(payload.get("launcher_type") or "person").strip().lower()
        if launcher_type not in {"person", "group"}:
            raise ValueError("launcher_type must be 'person' or 'group'")
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")

        shared_fields = payload.get("shared_fields") if isinstance(payload.get("shared_fields"), dict) else {}
        person_fields = payload.get("person_fields") if isinstance(payload.get("person_fields"), dict) else {}
        group_fields = payload.get("group_fields") if isinstance(payload.get("group_fields"), dict) else {}
        variant_fields = person_fields if launcher_type == "person" else group_fields
        card = self.cards.build_preview_card(
            shared_fields=shared_fields if isinstance(shared_fields, dict) else None,
            variant_fields=variant_fields if isinstance(variant_fields, dict) else None,
        )

        user_name = str(payload.get("user_name") or card.user_name or "User").strip() or card.user_name or "User"
        history = self._normalize_preview_history(payload.get("history"), assistant_name=card.assistant_name)
        session = SessionMemory(
            launcher_id=f"preview-{launcher_type}",
            launcher_type=launcher_type,
            history=self._preview_history_lines(history),
            metadata={},
        )
        event = InboundEvent(
            launcher_id=session.launcher_id,
            launcher_type=launcher_type,
            sender_id="preview-user",
            sender_name=user_name,
            segments=[MessageSegment(kind="text", text=message)],
        )
        emotion = self.emotions.analyze(event, session)
        conversation_view = self._preview_conversation_view(
            session.history,
            assistant_name=card.assistant_name,
            limit=self.config.history_window_messages,
        )
        analysis_hint = self.generator.generate_analysis(
            event,
            session,
            assistant_name=card.assistant_name,
            conversation_view=conversation_view,
            memory_hints=[],
            speaker_notes=[],
            active_skills=[],
            address_override=user_name,
            card_override=card,
        )
        reply_text = self.generator.generate_reply(
            event,
            session,
            emotion,
            assistant_name=card.assistant_name,
            address_override=user_name,
            card_override=card,
            conversation_view=conversation_view,
            memory_hints=[],
            speaker_notes=[],
            analysis_hint=analysis_hint,
            active_skills=[],
        )
        transcript = [
            *history,
            {"role": "user", "text": message},
            {"role": "assistant", "text": reply_text},
        ]
        return {
            "launcher_type": launcher_type,
            "assistant_name": card.assistant_name,
            "user_name": user_name,
            "reply_text": reply_text,
            "analysis_hint": analysis_hint,
            "transcript": transcript,
            "llm_ready": self.generator.llm_ready,
        }

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
        panel = {
            "llm": deepcopy(serialize_app_config(self.config)["llm"]),
            "image_generation": deepcopy(serialize_app_config(self.config)["image_generation"]),
            "embedding": deepcopy(serialize_app_config(self.config)["embedding"]),
        }
        for section in ("llm", "image_generation", "embedding"):
            sub = panel.get(section)
            if isinstance(sub, dict) and "api_key" in sub:
                sub["api_key"] = _mask_key(str(sub["api_key"] or ""))
        return panel

    def save_ai_panel(self, payload: dict[str, object]) -> dict[str, object]:
        llm = payload.get("llm", {})
        image_generation = payload.get("image_generation", {})
        embedding = payload.get("embedding", {})
        if isinstance(llm, dict):
            self.config.llm.enabled = bool(llm.get("enabled", self.config.llm.enabled))
            self.config.llm.backend = str(llm.get("backend", self.config.llm.backend) or self.config.llm.backend)
            self.config.llm.base_url = str(llm.get("base_url", self.config.llm.base_url) or "")
            _llm_key = str(llm.get("api_key", "") or "")
            if _llm_key and not _is_masked(_llm_key):
                self.config.llm.api_key = _llm_key
            self.config.llm.model = str(llm.get("model", self.config.llm.model) or "")
            self.config.llm.app_type = str(llm.get("app_type", self.config.llm.app_type) or self.config.llm.app_type)
            self.config.llm.timeout_seconds = _safe_float(llm, "timeout_seconds", self.config.llm.timeout_seconds)
        if isinstance(image_generation, dict):
            self.config.image_generation.enabled = bool(image_generation.get("enabled", self.config.image_generation.enabled))
            self.config.image_generation.base_url = str(image_generation.get("base_url", self.config.image_generation.base_url) or "")
            _img_key = str(image_generation.get("api_key", "") or "")
            if _img_key and not _is_masked(_img_key):
                self.config.image_generation.api_key = _img_key
            self.config.image_generation.model = str(image_generation.get("model", self.config.image_generation.model) or self.config.image_generation.model)
            self.config.image_generation.timeout_seconds = _safe_float(image_generation, "timeout_seconds", self.config.image_generation.timeout_seconds)
            self.config.image_generation.response_format = str(image_generation.get("response_format", self.config.image_generation.response_format) or self.config.image_generation.response_format)
            self.config.image_generation.aspect_ratio = str(image_generation.get("aspect_ratio", self.config.image_generation.aspect_ratio) or self.config.image_generation.aspect_ratio)
            self.config.image_generation.resolution = str(image_generation.get("resolution", self.config.image_generation.resolution) or "")
        if isinstance(embedding, dict):
            self.config.embedding.enabled = bool(embedding.get("enabled", self.config.embedding.enabled))
            self.config.embedding.backend = str(embedding.get("backend", self.config.embedding.backend) or self.config.embedding.backend)
            self.config.embedding.base_url = str(embedding.get("base_url", self.config.embedding.base_url) or "")
            _emb_key = str(embedding.get("api_key", "") or "")
            if _emb_key and not _is_masked(_emb_key):
                self.config.embedding.api_key = _emb_key
            self.config.embedding.model = str(embedding.get("model", self.config.embedding.model) or self.config.embedding.model)
            self.config.embedding.timeout_seconds = _safe_float(embedding, "timeout_seconds", self.config.embedding.timeout_seconds)
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
            "embedded_knowledge_count": self.state_store.embedded_knowledge_count(),
            "member_count": self.state_store.member_count(),
            "behavior_event_count": len(self.get_behavior_events(limit=200)),
        }

    def save_memory_session(
        self,
        launcher_type: str,
        launcher_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        session = self.memory.load(launcher_id, launcher_type)
        if not session.history and not self._sanitize_session_metadata(session.metadata):
            return None
        session.preferred_name = ""
        history_value = payload.get("history", session.history)
        if isinstance(history_value, list):
            session.history = [str(item) for item in history_value]
        elif isinstance(history_value, str):
            session.history = [line for line in history_value.splitlines() if line.strip()]
        metadata_value = payload.get("metadata", session.metadata)
        if isinstance(metadata_value, dict):
            session.metadata = self._sanitize_session_metadata(metadata_value)
        self.memory.store.save(session)
        return self.get_session_detail(launcher_type, launcher_id)

    def get_member_directory_panel(self, *, limit: int = 120) -> dict[str, object]:
        return {
            "members": self.state_store.list_members(limit=limit),
            "member_count": self.state_store.member_count(),
            "proactive_enabled": self.config.proactive_mode,
        }

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        return self.state_store.save_member(dict(payload))

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        return self.state_store.save_knowledge(dict(payload))

    def sync_group_members(self, group_id: str) -> dict[str, object]:
        safe_group_id = str(group_id or "").strip()
        if not safe_group_id:
            raise ValueError("group_id is required")
        if not self.config.qq_sidecar.outbound_base_url:
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
            "event_mode": self.config.event_mode,
            "event_buffer_limit": self.config.event_buffer_limit,
            "narrator_mode": self.config.narrator_mode,
            "narrator_style": self.config.narrator_style,
            "narrator_detail_level": self.config.narrator_detail_level,
            "value_game_mode": self.config.value_game_mode,
            "value_game_reply_bonus": self.config.value_game_reply_bonus,
            "memory_graph_mode": self.config.memory_graph_mode,
            "memory_graph_limit": self.config.memory_graph_limit,
            "proactive_mode": self.config.proactive_mode,
            "proactive_inactive_hours": self.config.proactive_inactive_hours,
            "proactive_candidate_limit": self.config.proactive_candidate_limit,
            "proactive_min_affinity": self.config.proactive_min_affinity,
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
        self.config.search_result_limit = _safe_int(payload, "search_result_limit", self.config.search_result_limit)
        self.config.search_timeout_seconds = _safe_float(payload, "search_timeout_seconds", self.config.search_timeout_seconds)
        self.config.thinking_mode = bool(payload.get("thinking_mode", self.config.thinking_mode))
        self.config.conversation_analysis = bool(payload.get("conversation_analysis", self.config.conversation_analysis))
        self.config.summarization_mode = bool(payload.get("summarization_mode", self.config.summarization_mode))
        self.config.event_mode = bool(payload.get("event_mode", self.config.event_mode))
        self.config.event_buffer_limit = _safe_int(payload, "event_buffer_limit", self.config.event_buffer_limit)
        self.config.narrator_mode = bool(payload.get("narrator_mode", self.config.narrator_mode))
        self.config.narrator_style = str(payload.get("narrator_style", self.config.narrator_style) or self.config.narrator_style)
        self.config.narrator_detail_level = _safe_int(payload, "narrator_detail_level", self.config.narrator_detail_level)
        self.config.value_game_mode = bool(payload.get("value_game_mode", self.config.value_game_mode))
        self.config.value_game_reply_bonus = _safe_float(payload, "value_game_reply_bonus", self.config.value_game_reply_bonus)
        self.config.memory_graph_mode = bool(payload.get("memory_graph_mode", self.config.memory_graph_mode))
        self.config.memory_graph_limit = _safe_int(payload, "memory_graph_limit", self.config.memory_graph_limit)
        self.config.proactive_mode = bool(payload.get("proactive_mode", self.config.proactive_mode))
        self.config.proactive_inactive_hours = _safe_float(payload, "proactive_inactive_hours", self.config.proactive_inactive_hours)
        self.config.proactive_candidate_limit = _safe_int(payload, "proactive_candidate_limit", self.config.proactive_candidate_limit)
        self.config.proactive_min_affinity = _safe_float(payload, "proactive_min_affinity", self.config.proactive_min_affinity)
        self.config.max_active_skills = _safe_int(payload, "max_active_skills", self.config.max_active_skills)
        self.config.history_window_messages = _safe_int(payload, "history_window_messages", self.config.history_window_messages)
        self.config.memory_recall_limit = _safe_int(payload, "memory_recall_limit", self.config.memory_recall_limit)
        self.config.max_thinking_words = _safe_int(payload, "max_thinking_words", self.config.max_thinking_words)
        self.config.short_term_memory_limit = _safe_int(payload, "short_term_memory_limit", self.config.short_term_memory_limit)
        self.config.memory_summary_batch_size = _safe_int(payload, "memory_summary_batch_size", self.config.memory_summary_batch_size)
        self._refresh_runtime_components()
        self._persist_config()
        return self.get_abilities_panel()

    def get_behavior_events(
        self,
        *,
        limit: int = 80,
        launcher_type: str = "",
        launcher_id: str = "",
    ) -> list[dict[str, object]]:
        safe_type = str(launcher_type or "").strip()
        safe_id = str(launcher_id or "").strip()
        with self._state_lock:
            items = [dict(item) for item in self._recent_behavior_events[-max(1, int(limit)) :][::-1]]
        if not safe_type and not safe_id:
            return items
        filtered: list[dict[str, object]] = []
        for item in items:
            if safe_type and str(item.get("launcher_type", "") or "").strip() != safe_type:
                continue
            if safe_id and str(item.get("launcher_id", "") or "").strip() != safe_id:
                continue
            filtered.append(item)
        return filtered

    def get_proactive_panel(self, *, limit: int = 12) -> dict[str, object]:
        members = self.state_store.list_members(limit=max(120, limit * 8))
        candidates = self.proactive.list_candidates(members=members, limit=limit)
        return {
            "enabled": self.config.proactive_mode,
            "inactive_hours": self.config.proactive_inactive_hours,
            "candidate_limit": self.config.proactive_candidate_limit,
            "candidates": candidates,
        }

    def generate_proactive_draft(self, payload: dict[str, object]) -> dict[str, object]:
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        member = self.state_store.get_member(group_id=group_id, user_id=user_id)
        if member is None:
            raise ValueError("member not found")
        relationship_hint = str(payload.get("relationship_hint", "") or "").strip()
        draft = self.proactive.build_draft(
            member=member,
            assistant_name=self.config.assistant_name,
            relationship_hint=relationship_hint,
        )
        return {"status": "ok", "draft": draft}

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
            "mode": "offline",
            "adapter_name": self.config.qq_sidecar.adapter_name,
            "outbound_base_url": self.config.qq_sidecar.outbound_base_url,
            "access_token": _mask_key(self.config.qq_sidecar.access_token),
            "inbound_host": self.config.qq_sidecar.inbound_host,
            "inbound_port": self.config.qq_sidecar.inbound_port,
            "webui_base_url": self.config.qq_sidecar.webui_base_url,
            "webui_api_prefix": self.config.qq_sidecar.webui_api_prefix,
            "webui_timeout_seconds": self.config.qq_sidecar.webui_timeout_seconds,
            "webui_token": _mask_key(self.config.qq_sidecar.webui_token),
            "webui_url": self.napcat_login.webui_url() if self.napcat_login is not None else "",
            "reverse_ws_url": self.config.qq_sidecar.reverse_ws_url,
            "outbound_timeout_seconds": self.config.qq_sidecar.outbound_timeout_seconds,
            "dry_run": False,
            "llm_ready": self.generator.llm_ready,
            "details": {},
        }
        if self.config.qq_sidecar.outbound_base_url:
            status["mode"] = "configured"
        if refresh and self.config.qq_sidecar.outbound_base_url:
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
        if self.napcat_login is not None:
            qq_login = self.napcat_login.panel(refresh=refresh)
            status["qq_login"] = qq_login
            if refresh:
                self._adopt_login_info(qq_login.get("login_info"))
        return status

    def save_sidecar_panel(self, payload: dict[str, object]) -> dict[str, object]:
        self._apply_sidecar_payload(payload)
        self._refresh_runtime_components(rebuild_outbound=True)
        self._persist_config()
        return self.get_sidecar_panel(refresh=False)

    def get_qq_login_panel(self, *, refresh: bool = False) -> dict[str, object]:
        panel = {
            "configured": bool(str(self.config.qq_sidecar.webui_base_url or "").strip()),
            "token_configured": bool(str(self.config.qq_sidecar.webui_token or "").strip()),
            "webui_base_url": self.config.qq_sidecar.webui_base_url,
            "webui_api_prefix": self.config.qq_sidecar.webui_api_prefix,
            "webui_timeout_seconds": self.config.qq_sidecar.webui_timeout_seconds,
            "webui_token": _mask_key(self.config.qq_sidecar.webui_token),
            "webui_url": self.napcat_login.webui_url() if self.napcat_login is not None else "",
            "status": {
                "is_login": False,
                "is_offline": False,
                "qrcode_url": "",
                "login_error": "",
            },
            "login_info": {},
        }
        if self.napcat_login is None:
            return panel
        bridge_panel = self.napcat_login.panel(refresh=refresh)
        panel.update(bridge_panel)
        if refresh:
            self._adopt_login_info(bridge_panel.get("login_info"))
        return panel

    def save_qq_login_panel(self, payload: dict[str, object]) -> dict[str, object]:
        self._apply_sidecar_payload(payload)
        self._refresh_runtime_components(rebuild_outbound=False)
        self._persist_config()
        return self.get_qq_login_panel(refresh=False)

    def refresh_qq_login_panel(self) -> dict[str, object]:
        if self.napcat_login is None:
            raise ValueError("NapCat QQ login bridge is unavailable")
        try:
            self.napcat_login.refresh_qrcode()
        except NapCatLoginError as exc:
            raise ValueError(str(exc)) from exc
        return self.get_qq_login_panel(refresh=True)

    def get_qq_login_qrcode_image(self) -> tuple[bytes, str] | None:
        if self.napcat_login is None:
            return None
        try:
            payload = self.napcat_login.qrcode_payload()
            content_type, body = qrcode_payload_to_image_source(payload)
        except NapCatLoginError as exc:
            raise ValueError(str(exc)) from exc
        return body, content_type

    def _apply_sidecar_payload(self, payload: dict[str, object]) -> None:
        self.config.qq_sidecar.adapter_name = str(
            payload.get("adapter_name", self.config.qq_sidecar.adapter_name)
            or self.config.qq_sidecar.adapter_name
        )
        self.config.qq_sidecar.outbound_base_url = str(
            payload.get("outbound_base_url", self.config.qq_sidecar.outbound_base_url) or ""
        )
        self.config.qq_sidecar.outbound_timeout_seconds = _safe_float(
            payload, "outbound_timeout_seconds", self.config.qq_sidecar.outbound_timeout_seconds
        )
        _access_token_raw = str(
            payload.get("access_token", "") or ""
        )
        if _access_token_raw and not _is_masked(_access_token_raw):
            self.config.qq_sidecar.access_token = _access_token_raw
        self.config.qq_sidecar.inbound_host = str(
            payload.get("inbound_host", self.config.qq_sidecar.inbound_host)
            or self.config.qq_sidecar.inbound_host
        )
        self.config.qq_sidecar.inbound_port = _safe_int(
            payload, "inbound_port", self.config.qq_sidecar.inbound_port
        )
        webui_base_raw = str(
            payload.get("webui_base_url", self.config.qq_sidecar.webui_base_url)
            or ""
        )
        webui_api_prefix = payload.get("webui_api_prefix", self.config.qq_sidecar.webui_api_prefix)
        self.config.qq_sidecar.webui_api_prefix = str(
            self.config.qq_sidecar.webui_api_prefix if webui_api_prefix is None else webui_api_prefix
        )
        self.config.qq_sidecar.webui_timeout_seconds = _safe_float(
            payload, "webui_timeout_seconds", self.config.qq_sidecar.webui_timeout_seconds
        )
        webui_token_raw = str(
            payload.get("webui_token", "") or ""
        )
        if _is_masked(webui_token_raw):
            webui_token_raw = self.config.qq_sidecar.webui_token
        normalized_webui_base, normalized_webui_token = normalize_webui_settings(
            webui_base_raw,
            webui_token_raw,
        )
        self.config.qq_sidecar.webui_base_url = normalized_webui_base
        self.config.qq_sidecar.webui_token = normalized_webui_token
        self.config.qq_sidecar.reverse_ws_url = str(
            payload.get("reverse_ws_url", self.config.qq_sidecar.reverse_ws_url)
            or self.config.qq_sidecar.reverse_ws_url
        )
        self.config.qq_sidecar.dry_run = False

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

    def _record_behavior_event(self, behavior_event: Any) -> None:
        if behavior_event is None:
            return
        payload = behavior_event.as_dict() if hasattr(behavior_event, "as_dict") else dict(behavior_event)
        payload["seq"] = int(payload.get("seq") or 0)
        with self._state_lock:
            self._recent_behavior_events.append(payload)
            self._recent_behavior_events = self._recent_behavior_events[-max(40, int(self.config.event_buffer_limit)) :]

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
            behavior = len(self._recent_behavior_events)
            active = sum(
                1
                for _, until in self._group_follow_up_until.items()
                if until > time.monotonic()
            )
            return {
                "uptime_seconds": uptime,
                "recent_inbound": inbound,
                "recent_outbound": outbound,
                "recent_behavior": behavior,
                "active_followups": active,
                "total_events": self._event_counter,
            }

    def _persist_config(self) -> None:
        if not self.config.config_path:
            return
        ConfigManager().save(self.config)

    def _adopt_login_info(self, login_info: object) -> None:
        if not isinstance(login_info, dict):
            return
        uin = str(login_info.get("uin") or "").strip()
        if not uin:
            return
        if str(self.config.bot_account_id or "").strip() == uin:
            return
        self.config.bot_account_id = uin
        self._persist_config()

    def _refresh_runtime_components(self, *, rebuild_generator: bool = False, rebuild_outbound: bool = False) -> None:
        if rebuild_generator:
            self.generator = Generator(self.config)
            self.cards = self.generator._cards
            self.thoughts = Thoughts(self.config, self.generator)
        self.search = SearchDecider(self.config)
        self.event_engine = BehaviorEventEngine(self.config)
        self.narrator = Narrator(self.config)
        self.value_game = ValueGameEngine(self.config)
        self.memory_graph = MemoryGraphBuilder(self.config)
        self.proactive = ProactivePlanner(self.config)
        self.marketplace = MarketplaceClient(self.config.marketplace)
        self.skills.config = self.config
        self.napcat_login = _build_napcat_login_bridge(self.config)
        if hasattr(self.state_store, "set_embedder"):
            self.state_store.set_embedder(_build_embedding_client(self.config))
        if hasattr(self.state_store, "refresh_knowledge_embeddings"):
            self.state_store.refresh_knowledge_embeddings()
        if rebuild_outbound:
            self.outbound = _build_runtime_outbound(self.config)

    @staticmethod
    def _coerce_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.splitlines() if item.strip()]
        return []

    @staticmethod
    def _extract_terms(value: object) -> list[str]:
        raw = str(value or "").strip().lower()
        if not raw:
            return []
        cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in raw)
        terms: list[str] = []
        seen: set[str] = set()
        for part in cleaned.split():
            if len(part) < 2 or part in seen:
                continue
            seen.add(part)
            terms.append(part)
        return terms

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
        card_metadata = session.metadata.get("card", {})
        if event.launcher_type == "person" and isinstance(card_metadata, dict):
            user_name = str(card_metadata.get("user_name", "") or "").strip()
            if user_name:
                return user_name
        return event.sender_name or "你"

    def _outbound_mode_label(self) -> str:
        if not self.config.qq_sidecar.outbound_base_url:
            return "offline"
        return f"{self.config.qq_sidecar.adapter_name} -> {self.config.qq_sidecar.outbound_base_url}"

    def _requires_live_llm(self) -> bool:
        return not isinstance(self.outbound, CapturingOutboundPort)

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

    def _session_preferred_name(self, session: SessionMemory) -> str:
        if session.launcher_type != "person":
            return ""
        member = self.state_store.get_member(group_id="", user_id=session.launcher_id)
        if member is None:
            return ""
        return str(member.get("preferred_name", "") or "").strip()

    def _session_detail_graph(self, session: SessionMemory) -> dict[str, Any]:
        launcher_id = str(session.launcher_id or "").strip()
        launcher_type = str(session.launcher_type or "").strip()
        if launcher_type == "person":
            member = self.state_store.get_member(group_id="", user_id=launcher_id)
            sender_id = launcher_id
            sender_name = str((member or {}).get("preferred_name") or (member or {}).get("qq_nickname") or launcher_id)
        else:
            recent_behavior = self.get_behavior_events(limit=1, launcher_type=launcher_type, launcher_id=launcher_id)
            sender_id = str(recent_behavior[0].get("sender_id", "") or "") if recent_behavior else ""
            member = self.state_store.get_member(group_id=launcher_id, user_id=sender_id) if sender_id else None
            sender_name = str((member or {}).get("preferred_name") or (member or {}).get("qq_nickname") or launcher_id)
        detail_event = InboundEvent(
            launcher_id=launcher_id,
            launcher_type=launcher_type if launcher_type in {"group", "person"} else "person",
            sender_id=sender_id,
            sender_name=sender_name,
            segments=[MessageSegment(kind="text", text=session.history[-1] if session.history else "")],
        )
        knowledge_entries = self._detail_knowledge_entries(session)
        behavior_events = self.get_behavior_events(limit=8, launcher_type=launcher_type, launcher_id=launcher_id)
        return self.memory_graph.build(
            event=detail_event,
            session=session,
            member=member,
            knowledge_entries=knowledge_entries,
            behavior_events=behavior_events,
        )

    def _detail_knowledge_entries(self, session: SessionMemory) -> list[dict[str, Any]]:
        entries = self.state_store.list_knowledge(limit=max(80, self.config.memory_graph_limit * 8))
        scopes = {(session.launcher_type, session.launcher_id), ("global", "")}
        if session.launcher_type == "person":
            scopes.add(("member", session.launcher_id))
        filtered = [
            dict(entry)
            for entry in entries
            if (
                str(entry.get("scope_type", "") or "").strip(),
                str(entry.get("scope_id", "") or "").strip(),
            )
            in scopes
        ]
        filtered.sort(key=lambda item: (-float(item.get("confidence") or 0.0), -int(item.get("updated_at") or 0)))
        return filtered[: max(1, int(self.config.memory_graph_limit))]

    def _knowledge_count_for_session(self, session: SessionMemory) -> int:
        count_fn = getattr(self.state_store, "count_knowledge_for_scopes", None)
        if callable(count_fn):
            if session.launcher_type == "group":
                scopes = [("group", session.launcher_id)]
            else:
                scopes = [
                    ("person", session.launcher_id),
                    ("member", session.launcher_id),
                ]
            return count_fn(scopes)
        total = max(1, int(self.state_store.knowledge_count()))
        entries = self.state_store.list_knowledge(limit=total)
        if session.launcher_type == "group":
            member_prefix = f"{session.launcher_id}:"
            return sum(
                1
                for entry in entries
                if (
                    str(entry.get("scope_type", "") or "").strip() == "group"
                    and str(entry.get("scope_id", "") or "").strip() == session.launcher_id
                )
                or (
                    str(entry.get("scope_type", "") or "").strip() == "member"
                    and str(entry.get("scope_id", "") or "").strip().startswith(member_prefix)
                )
            )
        return sum(
            1
            for entry in entries
            if (
                str(entry.get("scope_type", "") or "").strip() == "person"
                and str(entry.get("scope_id", "") or "").strip() == session.launcher_id
            )
            or (
                str(entry.get("scope_type", "") or "").strip() == "member"
                and str(entry.get("scope_id", "") or "").strip() == session.launcher_id
            )
        )

    @staticmethod
    def _sanitize_session_metadata(metadata: dict[str, object] | object) -> dict[str, object]:
        if not isinstance(metadata, dict):
            return {}
        cleaned = deepcopy(metadata)
        cleaned.pop("group_members", None)
        cleaned.pop("long_term_memory", None)
        return cleaned

    @staticmethod
    def _normalize_preview_history(
        raw_history: object,
        *,
        assistant_name: str,
        limit: int = 16,
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        if not isinstance(raw_history, list):
            return items
        for item in raw_history[-max(1, int(limit)):]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = str(item.get("text", "") or "").strip()
            if not text:
                continue
            items.append(
                {
                    "role": role,
                    "text": text,
                    "speaker": assistant_name if role == "assistant" else str(item.get("speaker", "") or "").strip(),
                }
            )
        return items

    @staticmethod
    def _preview_history_lines(history: list[dict[str, str]]) -> list[str]:
        lines: list[str] = []
        for item in history:
            role = str(item.get("role", "") or "").strip().lower()
            text = str(item.get("text", "") or "").strip()
            if not text:
                continue
            speaker = "assistant" if role == "assistant" else (str(item.get("speaker", "") or "").strip() or "user")
            lines.append(f"{speaker}: {text}")
        return lines

    def _preview_conversation_view(self, history_lines: list[str], *, assistant_name: str, limit: int = 8) -> str:
        lines: list[str] = []
        for raw_line in history_lines[-max(1, int(limit)):]:
            speaker, content = self._split_history_line(raw_line)
            if speaker == "assistant":
                speaker = assistant_name
            lines.append(f"{speaker}: {content}")
        return "\n".join(lines)

    def _count_group_members(self, session: SessionMemory) -> int:
        if session.launcher_type != "group":
            return 0
        count_fn = getattr(self.state_store, "count_members_in_group", None)
        if callable(count_fn):
            return count_fn(session.launcher_id)
        members = self.state_store.list_members(limit=5000)
        return sum(
            1
            for item in members
            if str(item.get("group_id", "") or "").strip() == session.launcher_id
        )


def build_default_service(config: AppConfig | None = None) -> tuple[WaifuService, CapturingOutboundPort]:
    app_config = config or AppConfig()
    return _build_service(
        app_config,
        InMemoryStore(),
        InMemoryRuntimeStateStore(embedder=_build_embedding_client(app_config)),
        CapturingOutboundPort(),
    )


def build_file_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, CapturingOutboundPort]:
    app_config = config or AppConfig()
    root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    state_root = Path(app_config.data_root) / "state" / "runtime.sqlite3"
    return _build_service(
        app_config,
        FileMemoryStore(root),
        SqliteRuntimeStateStore(state_root, embedder=_build_embedding_client(app_config)),
        CapturingOutboundPort(),
    )


def build_runtime_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, OutboundPort]:
    app_config = config or AppConfig()
    root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    outbound = _build_runtime_outbound(app_config)
    state_root = Path(app_config.data_root) / "state" / "runtime.sqlite3"
    return _build_service(
        app_config,
        FileMemoryStore(root),
        SqliteRuntimeStateStore(state_root, embedder=_build_embedding_client(app_config)),
        outbound,
    )


def _build_runtime_outbound(app_config: AppConfig) -> OutboundPort:
    if not app_config.qq_sidecar.outbound_base_url:
        return CapturingOutboundPort()
    client = OneBotActionClient(
        base_url=app_config.qq_sidecar.outbound_base_url,
        timeout=app_config.qq_sidecar.outbound_timeout_seconds,
        access_token=app_config.qq_sidecar.access_token,
    )
    return OneBotHttpOutboundPort(client)


def _build_embedding_client(app_config: AppConfig) -> EmbeddingClient:
    return EmbeddingClient(
        enabled=app_config.embedding.enabled,
        backend=app_config.embedding.backend,
        base_url=app_config.embedding.base_url,
        api_key=app_config.embedding.api_key,
        model=app_config.embedding.model,
        timeout_seconds=app_config.embedding.timeout_seconds,
    )


def _build_napcat_login_bridge(app_config: AppConfig) -> NapCatLoginBridge:
    return NapCatLoginBridge(
        base_url=app_config.qq_sidecar.webui_base_url,
        api_prefix=app_config.qq_sidecar.webui_api_prefix,
        webui_token=app_config.qq_sidecar.webui_token,
        timeout=app_config.qq_sidecar.webui_timeout_seconds,
    )


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
        event_engine=BehaviorEventEngine(app_config),
        narrator=Narrator(app_config),
        value_game=ValueGameEngine(app_config),
        memory_graph=MemoryGraphBuilder(app_config),
        proactive=ProactivePlanner(app_config),
        marketplace=MarketplaceClient(app_config.marketplace),
        skills=skills,
        tools=tools,
        state_store=state_store,
        outbound=outbound,
        napcat_login=_build_napcat_login_bridge(app_config),
    )
    service._migrate_legacy_session_state()
    if hasattr(service.state_store, "refresh_knowledge_embeddings"):
        service.state_store.refresh_knowledge_embeddings()
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
    tools.register(
        "skill-list",
        name="技能列表",
        description="列出当前所有已启用的技能及其触发方式。",
        handler=service._run_skill_list_tool,
    )
    return service, outbound
