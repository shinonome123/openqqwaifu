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
from .cells.prompt_builder import RelationshipContext
from .cells.skill_pack import build_skill_pack_template, export_skill_pack, import_skill_pack
from .cells.skill_registry import SkillRegistry, SkillSpec, build_skill_markdown_template
from .cells.tool_registry import ToolInvocation, ToolRegistry
from .cells.utils import MASK_SENTINEL as _MASK_SENTINEL, mask_key as _mask_key, safe_float as _safe_float, safe_int as _safe_int
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
from .organs.memories import Memory
from .organs.dashboard_service import DashboardService
from .organs.knowledge_manager import KnowledgeManager
from .organs.member_manager import MemberManager
from .organs.proactive import ProactivePlanner
from .organs.relationship import RelationshipTracker
from .organs.session_detail_graph import SessionDetailGraphService
from .services import CapturingOutboundPort
from .state_store import InMemoryRuntimeStateStore, SqliteRuntimeStateStore
from .systems.events import BehaviorEventEngine
from .systems.emotions import EmotionSensor
from .systems.searching import SearchContext, SearchDecider
from .systems.value_game import ValueGameEngine

_FOLLOW_UP_METADATA_KEY = "follow_up_until"
_PENDING_SEARCH_METADATA_KEY = "pending_search"
_PENDING_SEARCH_TTL_SECONDS = 1800.0


def _is_masked(value: str) -> bool:
    """Return True if *value* looks like it was produced by ``_mask_key``."""
    return value == "***" or _MASK_SENTINEL in value


@dataclass(slots=True)
class WaifuService:
    config: AppConfig
    memory: Memory
    emotions: EmotionSensor
    generator: Generator
    cards: CardManager
    search: SearchDecider
    event_engine: BehaviorEventEngine
    value_game: ValueGameEngine
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
    members: MemberManager = field(init=False, repr=False)
    knowledge: KnowledgeManager = field(init=False, repr=False)
    relationships: RelationshipTracker = field(init=False, repr=False)
    dashboard: DashboardService = field(init=False, repr=False)
    session_graphs: SessionDetailGraphService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rebuild_managers()

    def _rebuild_managers(self) -> None:
        self.members = MemberManager(
            config=self.config,
            memory=self.memory,
            generator=self.generator,
            cards=self.cards,
            state_store=self.state_store,
            value_game=self.value_game,
            current_character_id=self._active_character_id,
            emit_message=self._emit_message,
            requires_live_llm=self._requires_live_llm,
            should_retry_onboarding_prompt=self._should_retry_member_onboarding_prompt,
        )
        self.knowledge = KnowledgeManager(
            config=self.config,
            generator=self.generator,
            state_store=self.state_store,
            current_character_id=self._active_character_id,
            member_record=self.members.member_record,
            extract_directory_preferred_name=self.members.extract_directory_preferred_name,
            extract_image_prompt=self._extract_image_prompt,
            update_member_profile_summary=self.members.update_member_profile_summary,
        )
        self.relationships = RelationshipTracker(
            state_store=self.state_store,
            value_game=self.value_game,
            sanitize_profile_summary=self.members.sanitize_profile_summary_text,
        )
        self.dashboard = DashboardService(
            config=self.config,
            generator=self.generator,
            search=self.search,
            proactive=self.proactive,
            marketplace=self.marketplace,
            skills=self.skills,
            tools=self.tools,
            state_store=self.state_store,
            active_character_id=self._active_character_id,
            list_sessions=self.list_sessions,
            outbound_mode_label=self._outbound_mode_label,
            refresh_runtime_components=self._refresh_runtime_components,
            persist_config=self._persist_config,
        )
        self.session_graphs = SessionDetailGraphService(
            config=self.config,
            state_store=self.state_store,
            active_character_id=self._active_character_id,
            get_behavior_events=self.get_behavior_events,
        )

    def _active_character_id(self) -> str:
        return str(self.cards.active_character() or self.config.character or "default").strip() or "default"

    def handle_event(self, event: InboundEvent) -> OutboundMessage | None:
        with self._session_lock_for(event):
            return self._handle_event_locked(event)

    def _handle_event_locked(self, event: InboundEvent) -> OutboundMessage | None:
        current_character = self._active_character_id()
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
        session = self.memory.save_user_event(event, character_id=current_character)
        assistant_name = self.generator.resolve_assistant_name(event.launcher_type, session)
        session = self._sanitize_session_persona_state(session, assistant_name=assistant_name)
        self._sanitize_member_persona_state(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=current_character,
        )
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

        pending_search_query = self._pending_search_query_for_message(session, latest_message)
        if pending_search_query:
            return self._handle_search_request(
                event,
                session,
                query=pending_search_query,
                address=address,
                assistant_name=assistant_name,
            )

        active_skills = self.skills.match(latest_message)
        self._store_active_skills(session, active_skills)

        dispatch = self.skills.resolve_dispatch(latest_message)
        if dispatch is None:
            dispatch = self._resolve_builtin_skill_dispatch(latest_message)
            if dispatch is not None:
                skill, _ = dispatch
                if all(existing.skill_id != skill.skill_id for existing in active_skills):
                    active_skills = [*active_skills, skill]
                    self._store_active_skills(session, active_skills)
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

        emotion = self.emotions.quick_estimate(event.plain_text, history_size=len(session.history))
        search_context = self.search.build_context(event)
        self._store_search_context(session, search_context)
        conversation_view = self.memory.format_dialogue(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            limit=self.config.history_window_messages,
            character_id=current_character,
        )
        memory_hints = self.knowledge.recall(
            event,
            query=latest_message,
            limit=self.config.memory_recall_limit,
        )
        member_record = self._member_record(event)
        relationship_context = self._build_relationship_context(
            event,
            address=address,
            member=member_record,
        )
        reply_text = self.generator.generate_reply(
            event,
            session,
            assistant_name=assistant_name,
            address_override=address,
            search_hint=search_context.summary,
            search_context=search_context.to_prompt_block(),
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            relationship_context=relationship_context,
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
        emitted = self._emit_message(
            event,
            message,
            assistant_name=assistant_name,
            emotion=emotion,
            search_used=bool(search_context.active),
            behavior_reason="reply",
            character_id=current_character,
        )
        self._writeback_knowledge_if_needed(
            event,
            session=self.memory.load(event.launcher_id, event.launcher_type, character_id=current_character),
            latest_message=latest_message,
            assistant_name=assistant_name,
            address=address,
            conversation_view=conversation_view,
        )
        return emitted

    def _session_lock_for(self, event: InboundEvent) -> threading.Lock:
        key = (
            self._active_character_id(),
            str(event.launcher_type or "").strip(),
            str(event.launcher_id or "").strip(),
        )
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

    def _resolve_builtin_skill_dispatch(self, text: str) -> tuple[SkillSpec, str] | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        if self.skills.has_dispatch_tool("skill-list") and self._looks_like_skill_list_request(normalized):
            skill = self.skills.get_skill("skill-list-command")
            if skill is not None and skill.dispatches_tool:
                return skill, ""
        return None

    def _looks_like_skill_list_request(self, text: str) -> bool:
        normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not normalized:
            return False
        patterns = (
            r"^(?:说说|讲讲|聊聊|介绍下|介绍一下)?你(?:现在)?(?:都)?会(?:什么|干什么|做什么|哪些|啥)(?:技能|能力|功能)?$",
            r"^(?:说说|讲讲|聊聊|介绍下|介绍一下)?你(?:有|会)(?:的)?(?:技能|能力|功能)(?:都)?(?:有哪些|是什么)?$",
            r"^(?:技能|功能|命令)(?:列表|菜单)$",
        )
        return any(re.match(pattern, normalized) for pattern in patterns)

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
        self._clear_pending_search(session)
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
        character_id: str = "",
    ) -> OutboundMessage:
        if event.launcher_type == "group":
            delay = max(0.0, float(self.config.group_response_delay_seconds))
            if delay > 0:
                time.sleep(delay)
        resolved_character_id = str(character_id or self._active_character_id()).strip()
        self.outbound.send(message)
        self._record_outbound(message)
        self.memory.save_assistant_message(
            event.launcher_id,
            event.launcher_type,
            message.text,
            character_id=resolved_character_id,
        )
        self.state_store.mark_member_addressed(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=resolved_character_id,
        )
        self._archive_if_needed(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            character_id=resolved_character_id,
        )
        self._record_behavior_event(
            self.event_engine.capture_outbound(event=event, message=message, reason=behavior_reason or "reply")
        )
        self.relationships.apply_reply(
            event=event,
            emotion=emotion or EmotionState(),
            reply_text=message.text,
            search_used=search_used,
        )
        self._refresh_follow_up_window(event)
        return message

    def _archive_if_needed(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        assistant_name: str,
        character_id: str = "",
    ) -> None:
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
            character_id=str(character_id or self._active_character_id()).strip(),
        )
        if archived is None:
            return
        summary = str(archived.get("summary", "") or "").strip()
        if not summary:
            return
        self.state_store.add_knowledge(
            character_id=self._active_character_id(),
            scope_type=launcher_type,
            scope_id=launcher_id,
            memory_type="summary",
            summary=summary,
            tags=[str(tag).strip() for tag in archived.get("tags", []) if str(tag).strip()]
            if isinstance(archived.get("tags"), list)
            else [],
            confidence=0.62,
        )

    def _writeback_knowledge_if_needed(
        self,
        event: InboundEvent,
        *,
        session: SessionMemory,
        latest_message: str,
        assistant_name: str,
        address: str,
        conversation_view: str,
    ) -> None:
        self.knowledge.writeback_knowledge_if_needed(
            event,
            session=session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            address=address,
            conversation_view=conversation_view,
        )

    def _persist_extracted_knowledge(
        self,
        event: InboundEvent,
        entry: dict[str, object],
        *,
        message_id: str = "",
    ) -> dict[str, object] | None:
        return self.knowledge.persist_extracted_knowledge(
            event,
            entry,
            message_id=message_id,
        )

    def _existing_knowledge_entry(self, scope_type: str, scope_id: str, summary: str) -> dict[str, object] | None:
        return self.knowledge.existing_knowledge_entry(scope_type, scope_id, summary)

    def _knowledge_scope_for_candidate(self, event: InboundEvent, entry: dict[str, object]) -> tuple[str, str]:
        return self.knowledge.knowledge_scope_for_candidate(event, entry)

    def _known_assistant_aliases(self) -> dict[str, set[str]]:
        return self.members.known_assistant_aliases()

    def _known_assistant_names(self) -> dict[str, str]:
        return self.members.known_assistant_names()

    @staticmethod
    def _mentions_any_assistant_name(text: str, names: set[str]) -> bool:
        return MemberManager.mentions_any_assistant_name(text, names)

    def _sanitize_profile_summary_text(self, summary: str) -> str:
        return self.members.sanitize_profile_summary_text(summary)

    def _sanitize_session_persona_state(
        self,
        session: SessionMemory,
        *,
        assistant_name: str,
    ) -> SessionMemory:
        return self.members.sanitize_session_persona_state(session, assistant_name=assistant_name)

    def _sanitize_member_persona_state(
        self,
        *,
        group_id: str,
        user_id: str,
        character_id: str,
    ) -> dict[str, object] | None:
        return self.members.sanitize_member_persona_state(
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )

    def _update_member_profile_summary(self, event: InboundEvent, *, extra_summary: str = "") -> None:
        self.members.update_member_profile_summary(event, extra_summary=extra_summary)

    @staticmethod
    def _merge_profile_summary(existing: str, addition: str) -> str:
        return MemberManager.merge_profile_summary(existing, addition)

    def _sidecar_action_client(self) -> OneBotActionClient:
        if not self.config.qq_sidecar.outbound_base_url:
            raise ValueError("sidecar is not available")
        return OneBotActionClient(
            base_url=self.config.qq_sidecar.outbound_base_url,
            timeout=self.config.qq_sidecar.outbound_timeout_seconds,
            access_token=self.config.qq_sidecar.access_token,
        )

    def _handle_group_increase_notice(self, payload: dict[str, object]) -> dict[str, object]:
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        bot_id = str(payload.get("self_id", "") or self.config.bot_account_id or "").strip()
        if bot_id and user_id == bot_id and self.config.member_auto_sync:
            synced = self.sync_group_members(group_id)
            return {"status": "ok", "reason": "bot_joined_group", "sync": synced}

        member_payload: dict[str, object] = {
            "group_id": group_id,
            "user_id": user_id,
            "membership_status": "active",
            "last_sync_at": int(time.time()),
        }
        if self.config.member_auto_sync:
            try:
                response = self._sidecar_action_client().get_group_member_info(group_id, user_id, no_cache=False)
                data = response.get("data", response)
                if isinstance(data, dict):
                    member_payload["qq_nickname"] = str(data.get("nickname", "") or "")
                    member_payload["group_card"] = str(data.get("card", "") or "")
            except Exception:
                pass
        saved = self.state_store.save_member(member_payload)
        return {"status": "ok", "reason": "member_joined", "member": saved}

    def _handle_group_decrease_notice(self, payload: dict[str, object]) -> dict[str, object]:
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        sub_type = str(payload.get("sub_type", "") or "").strip().lower()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        bot_id = str(payload.get("self_id", "") or self.config.bot_account_id or "").strip()
        synced_at = int(time.time())
        if bot_id and user_id == bot_id:
            mark_missing = getattr(self.state_store, "mark_group_members_missing", None)
            affected = 0
            if callable(mark_missing):
                affected = int(
                    mark_missing(
                        group_id=group_id,
                        active_user_ids=[],
                        membership_status="removed",
                        last_sync_at=synced_at,
                    )
                    or 0
                )
            return {"status": "ok", "reason": "bot_left_group", "affected_members": affected}

        status = "left" if sub_type == "leave" else "removed"
        saved = self.state_store.mark_member_membership(
            group_id=group_id,
            user_id=user_id,
            membership_status=status,
            last_sync_at=synced_at,
        )
        if saved is None:
            saved = self.state_store.save_member(
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "membership_status": status,
                    "last_sync_at": synced_at,
                }
            )
        return {"status": "ok", "reason": "member_left_group", "member": saved}

    def _handle_group_card_notice(self, payload: dict[str, object]) -> dict[str, object]:
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        card_value = (
            str(payload.get("card_new", "") or "").strip()
            or str(payload.get("card", "") or "").strip()
            or str(payload.get("nickname", "") or "").strip()
        )
        saved = self.state_store.save_member(
            {
                "group_id": group_id,
                "user_id": user_id,
                "group_card": card_value,
                "membership_status": "active",
                "last_sync_at": int(time.time()),
            }
        )
        return {"status": "ok", "reason": "group_card_updated", "member": saved}

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
        if self._is_follow_up_window_active(event.launcher_id):
            return True
        return self._has_pending_search_follow_up(event)

    def _is_follow_up_window_active(self, launcher_id: str) -> bool:
        with self._state_lock:
            deadline = self._group_follow_up_until.get(launcher_id, 0.0)
        if time.monotonic() <= deadline:
            return True
        session = self.memory.load(
            launcher_id,
            "group",
            character_id=self._active_character_id(),
        )
        return self._session_follow_up_until(session) >= time.time()

    def _refresh_follow_up_window(self, event: InboundEvent) -> None:
        if event.launcher_type != "group":
            return
        window = max(0.0, float(self.config.group_follow_up_window_seconds))
        if window <= 0:
            return
        deadline_monotonic = time.monotonic() + window
        deadline_wall = time.time() + window
        with self._state_lock:
            self._group_follow_up_until[event.launcher_id] = deadline_monotonic
        self._persist_follow_up_window(
            event.launcher_id,
            event.launcher_type,
            deadline_wall=deadline_wall,
            character_id=self._active_character_id(),
        )

    def _persist_follow_up_window(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        deadline_wall: float,
        character_id: str,
    ) -> None:
        if launcher_type != "group":
            return
        session = self.memory.load(launcher_id, launcher_type, character_id=character_id)
        session.metadata[_FOLLOW_UP_METADATA_KEY] = float(deadline_wall)
        self.memory.store.save(session)

    def _session_follow_up_until(self, session: SessionMemory) -> float:
        raw_value = session.metadata.get(_FOLLOW_UP_METADATA_KEY)
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return 0.0

    def _pending_search_query_for_message(self, session: SessionMemory, latest_message: str) -> str:
        pending = self._pending_search_payload(session)
        if not pending:
            return ""
        expires_at = _safe_float(pending, "expires_at", 0.0)
        if expires_at and expires_at < time.time():
            self._clear_pending_search(session)
            return ""
        if not self._looks_like_search_confirmation(latest_message):
            if not self._looks_like_search_clarification(latest_message):
                return ""
            base_query = str(pending.get("query", "") or "").strip()
            clarification = " ".join(str(latest_message or "").split()).strip()
            return " ".join(part for part in (base_query, clarification) if part).strip()
        return str(pending.get("query", "") or "").strip()

    def _has_pending_search_follow_up(self, event: InboundEvent) -> bool:
        if event.launcher_type != "group":
            return False
        latest_message = event.command_text(self.config.bot_account_id).strip() or event.to_memory_text()
        session = self.memory.load(
            event.launcher_id,
            event.launcher_type,
            character_id=self._active_character_id(),
        )
        return bool(self._pending_search_query_for_message(session, latest_message))

    def _pending_search_payload(self, session: SessionMemory) -> dict[str, object]:
        raw_value = session.metadata.get(_PENDING_SEARCH_METADATA_KEY, {})
        if isinstance(raw_value, dict) and str(raw_value.get("query", "") or "").strip():
            return raw_value
        legacy_search = session.metadata.get("last_search", {})
        if not isinstance(legacy_search, dict):
            return {}
        query = str(legacy_search.get("query", "") or "").strip()
        results = legacy_search.get("results", [])
        fetched_at = _safe_float(legacy_search, "fetched_at", 0.0)
        if not query or results:
            return {}
        if fetched_at and time.time() - fetched_at > _PENDING_SEARCH_TTL_SECONDS:
            return {}
        return {
            "query": query,
            "created_at": fetched_at or time.time(),
            "expires_at": (fetched_at or time.time()) + _PENDING_SEARCH_TTL_SECONDS,
        }

    def _store_pending_search(self, session: SessionMemory, *, query: str) -> None:
        cleaned_query = " ".join(str(query or "").split()).strip()
        if not cleaned_query:
            self._clear_pending_search(session)
            return
        session.metadata[_PENDING_SEARCH_METADATA_KEY] = {
            "query": cleaned_query,
            "created_at": time.time(),
            "expires_at": time.time() + _PENDING_SEARCH_TTL_SECONDS,
        }

    def _clear_pending_search(self, session: SessionMemory) -> None:
        if _PENDING_SEARCH_METADATA_KEY in session.metadata:
            session.metadata.pop(_PENDING_SEARCH_METADATA_KEY, None)
            self.memory.store.save(session)

    @staticmethod
    def _looks_like_search_confirmation(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not compact:
            return False
        patterns = (
            "好的你帮我查查吧",
            "好你帮我查查吧",
            "帮我查查吧",
            "你帮我查查吧",
            "帮我查一下吧",
            "你帮我查一下吧",
            "那你帮我查查吧",
            "那你帮我查一下吧",
            "查查吧",
            "查一下吧",
            "去查查吧",
            "去查一下吧",
            "帮我查查",
            "帮我查一下",
        )
        if any(pattern in compact for pattern in patterns):
            return True
        short_affirmations = {
            "好的",
            "好",
            "行",
            "行吧",
            "嗯",
            "嗯嗯",
            "可以",
            "好呀",
            "好哦",
        }
        return compact in short_affirmations

    @staticmethod
    def _looks_like_search_clarification(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not compact or len(compact) > 24:
            return False
        blockers = ("怎么", "为什么", "查不了", "不查", "不用查", "别查", "不是", "我不是")
        if any(blocker in compact for blocker in blockers):
            return False
        markers = ("公司", "集团", "股价", "股票", "港股", "美股", "a股", "hk", "us", "sz", "sh")
        if any(marker in compact for marker in markers):
            return True
        if re.search(r"[a-z]{2,}|\d{3,}", compact):
            return True
        cjk_chars = [char for char in compact if "\u4e00" <= char <= "\u9fff"]
        return 2 <= len(cjk_chars) <= 8

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
        self.members.remember_directory_member(event)

    def _maybe_handle_member_onboarding(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> OutboundMessage | None:
        return self.members.maybe_handle_member_onboarding(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
        )

    def _extract_directory_preferred_name(self, text: str) -> str:
        return self.members.extract_directory_preferred_name(text)
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
        return KnowledgeManager.merge_memory_hints(primary, secondary, limit=limit)

    def _knowledge_scopes(self, event: InboundEvent) -> list[tuple[str, str]]:
        return self.knowledge.knowledge_scopes(event)

    def _member_record(self, event: InboundEvent) -> dict[str, Any] | None:
        return self.members.member_record(event)

    def _build_relationship_context(
        self,
        event: InboundEvent,
        *,
        address: str,
        member: dict[str, Any] | None,
    ) -> RelationshipContext:
        return self.relationships.build_context(event, member, address=address)

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
        return self.members.directory_member_notes(event)
        notes: list[str] = []
        current_group = event.launcher_id if event.launcher_type == "group" else ""
        current_character = self._active_character_id()
        self._sanitize_member_persona_state(
            group_id=current_group,
            user_id=event.sender_id,
            character_id=current_character,
        )
        members = self.state_store.list_members(limit=240, character_id=current_character)
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
            profile_summary = self._sanitize_profile_summary_text(profile_summary)
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
            for entry in self.state_store.list_knowledge(
                limit=max(1, self.state_store.knowledge_count(character_id=self._active_character_id())),
                character_id=self._active_character_id(),
            )
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
                            "character_id": str(session.character_id or self._active_character_id()).strip(),
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
                        character_id=str(session.character_id or self._active_character_id()).strip(),
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

    def _repair_character_isolation_state(self, character_id: str = "") -> None:
        known = self._known_assistant_names()
        target_ids = [str(character_id or "").strip()] if str(character_id or "").strip() else list(known.keys())
        target_ids = [item for item in target_ids if item]
        if not target_ids:
            return
        store = self.memory.store
        if hasattr(store, "list_sessions"):
            for session in store.list_sessions():  # type: ignore[no-any-return]
                session_character = str(getattr(session, "character_id", "") or "").strip()
                if session_character not in target_ids:
                    continue
                assistant_name = known.get(session_character, "")
                if not assistant_name:
                    try:
                        assistant_name = self.cards.load(session.launcher_type, session).assistant_name
                    except Exception:
                        assistant_name = ""
                if assistant_name:
                    self._sanitize_session_persona_state(session, assistant_name=assistant_name)
        list_members = getattr(self.state_store, "list_members", None)
        if callable(list_members):
            for session_character in target_ids:
                for member in list_members(limit=5000, character_id=session_character):
                    self._sanitize_member_persona_state(
                        group_id=str(member.get("group_id", "") or ""),
                        user_id=str(member.get("user_id", "") or ""),
                        character_id=session_character,
                    )

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
        with self._state_lock:
            recent_outbound = [self._message_to_dict(message) for message in self._recent_outbound[-12:][::-1]]
            recent_behavior_events = [dict(item) for item in self._recent_behavior_events[-12:][::-1]]
            active_launchers = sorted(
                launcher_id
                for launcher_id, until in self._group_follow_up_until.items()
                if until > time.monotonic()
            )
        snapshot = self.dashboard.snapshot(
            recent_outbound=recent_outbound,
            recent_behavior_events=recent_behavior_events,
            active_launchers=active_launchers,
        )
        snapshot["updated_at"] = time.time()
        return snapshot

    def list_sessions(self, limit: int = 24) -> list[dict[str, object]]:
        store = self.memory.store
        if not hasattr(store, "list_sessions"):
            return []
        current_character = self._active_character_id()
        sessions = [
            session
            for session in store.list_sessions()  # type: ignore[no-any-return]
            if str(getattr(session, "character_id", "") or current_character).strip() == current_character
        ]
        result: list[dict[str, object]] = []
        for session in sessions[-limit:][::-1]:
            history = list(session.history)
            card = self.cards.load(session.launcher_type, session)
            active_skill_names = self._active_skill_names(session)
            last_search_query = self._last_search_query(session)
            result.append(
                    {
                        "character_id": current_character,
                        "launcher_id": session.launcher_id,
                        "launcher_type": session.launcher_type,
                    "preferred_name": self._session_preferred_name(session),
                    "assistant_name": card.assistant_name,
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
        current_character = self._active_character_id()
        session = self.memory.load(launcher_id, launcher_type, character_id=current_character)
        clean_metadata = self._sanitize_session_metadata(session.metadata)
        if not session.history and not clean_metadata:
            return None
        graph = self.session_graphs.build(session)
        return {
            "character_id": str(session.character_id or current_character).strip(),
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
            "archived": self.dashboard.archived_controls_panel(),
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
            set_default_character = getattr(self.state_store, "set_default_character", None)
            if callable(set_default_character):
                set_default_character(active_character)
            self._repair_character_isolation_state(active_character)
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
        conversation_view = self._preview_conversation_view(
            session.history,
            assistant_name=card.assistant_name,
            limit=self.config.history_window_messages,
        )
        reply_text = self.generator.generate_reply(
            event,
            session,
            assistant_name=card.assistant_name,
            address_override=user_name,
            card_override=card,
            conversation_view=conversation_view,
            memory_hints=[],
            relationship_context=RelationshipContext(address=user_name),
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
        current_character = self._active_character_id()
        knowledge_entries = self.state_store.list_knowledge(limit=80, character_id=current_character)
        return {
            "character_id": current_character,
            "sessions": sessions,
            "knowledge_entries": knowledge_entries,
            "knowledge_count": self.state_store.knowledge_count(character_id=current_character),
            "embedded_knowledge_count": self.state_store.embedded_knowledge_count(character_id=current_character),
            "member_count": self.state_store.member_count(),
            "behavior_event_count": len(self.get_behavior_events(limit=200)),
        }

    def save_memory_session(
        self,
        launcher_type: str,
        launcher_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        current_character = self._active_character_id()
        session = self.memory.load(launcher_id, launcher_type, character_id=current_character)
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
        current_character = self._active_character_id()
        return {
            "character_id": current_character,
            "members": self.state_store.list_members(limit=limit, character_id=current_character),
            "member_count": self.state_store.member_count(),
            "proactive_enabled": self.config.proactive_mode,
        }

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        next_payload = dict(payload)
        next_payload["character_id"] = self._active_character_id()
        return self.state_store.save_member(next_payload)

    def reset_directory_member_persona(self, payload: dict[str, object]) -> dict[str, object] | None:
        return self.state_store.reset_member_persona(
            group_id=payload.get("group_id"),
            user_id=payload.get("user_id"),
            character_id=self._active_character_id(),
        )

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        next_payload = dict(payload)
        next_payload["character_id"] = self._active_character_id()
        return self.state_store.save_knowledge(next_payload)

    def delete_knowledge_entry(self, entry_id: int) -> bool:
        return bool(
            self.state_store.delete_knowledge(
                entry_id,
                character_id=self._active_character_id(),
            )
        )

    def handle_notice_payload(self, payload: dict[str, object]) -> dict[str, object]:
        notice_type = str(payload.get("notice_type", "") or "").strip().lower()
        if notice_type == "group_increase":
            return self._handle_group_increase_notice(payload)
        if notice_type == "group_decrease":
            return self._handle_group_decrease_notice(payload)
        if notice_type == "group_card":
            return self._handle_group_card_notice(payload)
        return {"status": "ignored", "reason": "unsupported notice_type"}

    def sync_group_members(self, group_id: str) -> dict[str, object]:
        safe_group_id = str(group_id or "").strip()
        if not safe_group_id:
            raise ValueError("group_id is required")
        client = self._sidecar_action_client()
        response = client.get_group_member_list(safe_group_id)
        members = response.get("data", response)
        if not isinstance(members, list):
            raise ValueError("group member payload is invalid")
        synced: list[dict[str, object]] = []
        active_user_ids: list[str] = []
        synced_at = int(time.time())
        for item in members:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", "") or "").strip()
            if not user_id:
                continue
            active_user_ids.append(user_id)
            saved = self.state_store.save_member(
                {
                    "group_id": safe_group_id,
                    "user_id": user_id,
                    "qq_nickname": str(item.get("nickname", "") or ""),
                    "group_card": str(item.get("card", "") or ""),
                    "membership_status": "active",
                    "last_sync_at": synced_at,
                }
            )
            synced.append(saved)
        missing_count = 0
        mark_missing = getattr(self.state_store, "mark_group_members_missing", None)
        if callable(mark_missing):
            missing_count = int(
                mark_missing(
                    group_id=safe_group_id,
                    active_user_ids=active_user_ids,
                    membership_status="left",
                    last_sync_at=synced_at,
                )
                or 0
            )
        return {
            "status": "ok",
            "group_id": safe_group_id,
            "count": len(synced),
            "marked_missing_count": missing_count,
            "members": synced[:20],
        }

    def get_abilities_panel(self) -> dict[str, object]:
        return self.dashboard.get_abilities_panel()

    def save_abilities_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.dashboard.save_abilities_panel(payload)

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
        current_character = self._active_character_id()
        members = self.state_store.list_members(limit=max(120, limit * 8), character_id=current_character)
        candidates = self.proactive.list_candidates(members=members, limit=limit)
        return {
            "character_id": current_character,
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
        member = self.state_store.get_member(group_id=group_id, user_id=user_id, character_id=self._active_character_id())
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
        next_webui_api_prefix = str(
            self.config.qq_sidecar.webui_api_prefix if webui_api_prefix is None else webui_api_prefix
        ).strip()
        self.config.qq_sidecar.webui_api_prefix = next_webui_api_prefix or "/api"
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
            self.memory.set_character_resolver(self.cards.active_character)
        self.search = SearchDecider(self.config)
        self.event_engine = BehaviorEventEngine(self.config)
        self.value_game = ValueGameEngine(self.config)
        self.proactive = ProactivePlanner(self.config)
        self.marketplace = MarketplaceClient(self.config.marketplace)
        self.skills.config = self.config
        self.napcat_login = _build_napcat_login_bridge(self.config)
        if hasattr(self.state_store, "set_default_character"):
            self.state_store.set_default_character(self._active_character_id())
        if hasattr(self.state_store, "set_embedder"):
            self.state_store.set_embedder(_build_embedding_client(self.config))
        if hasattr(self.state_store, "refresh_knowledge_embeddings"):
            self.state_store.refresh_knowledge_embeddings()
        if rebuild_outbound:
            self.outbound = _build_runtime_outbound(self.config)
        self._rebuild_managers()

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
            if search_context.active:
                session.metadata.pop(_PENDING_SEARCH_METADATA_KEY, None)
            else:
                self._store_pending_search(session, query=search_context.query)
        else:
            session.metadata.pop("last_search", None)
            session.metadata.pop(_PENDING_SEARCH_METADATA_KEY, None)
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
        if event.launcher_type == "person":
            user_name = str(self.cards.load(event.launcher_type, session).user_name or "").strip()
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

    def _knowledge_count_for_session(self, session: SessionMemory) -> int:
        current_character = str(session.character_id or self._active_character_id()).strip()
        count_fn = getattr(self.state_store, "count_knowledge_for_scopes", None)
        if callable(count_fn):
            if session.launcher_type == "group":
                scopes = [("group", session.launcher_id)]
            else:
                scopes = [
                    ("person", session.launcher_id),
                    ("member", session.launcher_id),
                ]
            return count_fn(scopes, character_id=current_character)
        total = max(1, int(self.state_store.knowledge_count(character_id=current_character)))
        entries = self.state_store.list_knowledge(limit=total, character_id=current_character)
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
        members = self.state_store.list_members(limit=5000, character_id=self._active_character_id())
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
        generator=generator,
        cards=cards,
        search=SearchDecider(app_config),
        event_engine=BehaviorEventEngine(app_config),
        value_game=ValueGameEngine(app_config),
        proactive=ProactivePlanner(app_config),
        marketplace=MarketplaceClient(app_config.marketplace),
        skills=skills,
        tools=tools,
        state_store=state_store,
        outbound=outbound,
        napcat_login=_build_napcat_login_bridge(app_config),
    )
    service.memory.set_character_resolver(service.cards.active_character)
    if hasattr(service.state_store, "set_default_character"):
        service.state_store.set_default_character(service.cards.active_character())
    service._migrate_legacy_session_state()
    adopt_character = getattr(service.state_store, "adopt_legacy_character", None)
    if callable(adopt_character):
        adopt_character(service.cards.active_character())
    service._repair_character_isolation_state()
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
