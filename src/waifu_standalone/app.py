from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .cells.cards import CardManager
from .cells.config import ConfigManager, serialize_app_config
from .cells.embedding_clients import EmbeddingClient, build_embedding_client
from .cells.generator import Generator
from .cells.image_clients import build_image_client
from .cells.llm_clients import build_llm_client
from .cells.marketplace import MarketplaceClient
from .cells.skill_pack import build_skill_pack_template, export_skill_pack, import_skill_pack
from .cells.skill_registry import SkillRegistry, SkillSpec, build_skill_markdown_template
from .cells.tool_registry import ToolInvocation, ToolRegistry
from .console_panels import ConsolePanels
from .config import AppConfig
from .contracts import OutboundPort
from .knowledge_curator import KnowledgeCurator
from .gateways.napcat_login import (
    NapCatLoginBridge,
    NapCatLoginError,
    normalize_webui_settings,
    qrcode_payload_to_image_source,
)
from .gateways.onebot_actions import OneBotActionClient, OneBotHttpOutboundPort
from .http_transport import AsyncRuntime
from .memory import FileMemoryStore, InMemoryStore
from .models import EmotionState, InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from .notice_dispatcher import NoticeDispatcher
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
_FOLLOW_UP_METADATA_KEY = "follow_up_until"
_PENDING_SEARCH_METADATA_KEY = "pending_search"
_PENDING_SEARCH_TTL_SECONDS = 1800.0
_BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="openqqwaifu-bg")
_ASYNC_RUNTIME = AsyncRuntime()


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


def _close_component(component: object) -> None:
    close = getattr(component, "close", None)
    if callable(close):
        close()


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
    _background_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _background_tasks: set[Future[object]] = field(default_factory=set, repr=False)
    console: ConsolePanels = field(init=False, repr=False)
    knowledge: KnowledgeCurator = field(init=False, repr=False)
    notice: NoticeDispatcher = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.console = ConsolePanels(self)
        self.knowledge = KnowledgeCurator(self)
        self.notice = NoticeDispatcher(self)

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

        search_future = self._start_search_context(event)
        emotion = self.emotions.analyze(event, session)
        search_context = self._resolve_search_context(event, search_future)
        self._store_search_context(session, search_context)
        conversation_view = self.memory.format_dialogue(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            limit=self.config.history_window_messages,
            character_id=current_character,
        )
        recalled_memory_hints = self.state_store.recall_knowledge(
            scopes=self._knowledge_scopes(event),
            query=latest_message,
            limit=self.config.memory_recall_limit,
            character_id=current_character,
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
        emitted = self._emit_message(
            event,
            message,
            assistant_name=assistant_name,
            emotion=emotion,
            search_used=bool(search_context.active),
            behavior_reason="reply",
            character_id=current_character,
        )
        self._schedule_knowledge_writeback(
            event,
            session=self.memory.load(event.launcher_id, event.launcher_type, character_id=current_character),
            latest_message=latest_message,
            assistant_name=assistant_name,
            address=address,
            conversation_view=conversation_view,
        )
        return emitted

    _SESSION_LOCKS_MAX = 512

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
                # Bound the lock table so a long-running server with many distinct
                # (character, launcher) combos doesn't grow memory forever.
                # Evict idle locks (ones nobody is currently holding) oldest-first.
                if len(self._session_locks) > self._SESSION_LOCKS_MAX:
                    self._evict_idle_session_locks()
            return lock

    def _evict_idle_session_locks(self) -> None:
        """Drop unheld session locks, keeping map size bounded.

        Called under ``self._state_lock``. A lock is only removed if ``acquire``
        succeeds non-blocking — that proves nobody else holds it, so freeing it
        is safe. We intentionally don't use ``WeakValueDictionary`` because the
        caller-side ``with lock:`` blocks would be unsafe if the lock got GC'd.
        """
        evicted = 0
        target = max(self._SESSION_LOCKS_MAX // 4, 1)
        # Iterate over a snapshot so we can mutate the underlying dict.
        for stale_key, stale_lock in list(self._session_locks.items()):
            if evicted >= target:
                break
            if stale_lock.acquire(blocking=False):
                try:
                    self._session_locks.pop(stale_key, None)
                    evicted += 1
                finally:
                    stale_lock.release()

    def _submit_background_task(self, task_name: str, callback: Callable[[], object]) -> None:
        future = _BACKGROUND_EXECUTOR.submit(callback)
        with self._background_lock:
            self._background_tasks.add(future)
        future.add_done_callback(lambda completed, name=task_name: self._background_task_done(name, completed))

    def _submit_background_coro(self, task_name: str, coroutine: object) -> None:
        future = _ASYNC_RUNTIME.submit(coroutine)
        with self._background_lock:
            self._background_tasks.add(future)
        future.add_done_callback(lambda completed, name=task_name: self._background_task_done(name, completed))

    def _start_search_context(self, event: InboundEvent) -> Future[SearchContext] | None:
        if not self.search.should_search(event):
            return None
        search_state = getattr(self.search, "__dict__", {})
        if "build_context" in search_state and "abuild_context" not in search_state:
            return None
        return _ASYNC_RUNTIME.submit(self.search.abuild_context(event))

    def _resolve_search_context(
        self,
        event: InboundEvent,
        search_future: Future[SearchContext] | None,
    ) -> SearchContext:
        if search_future is None:
            if self.search.should_search(event):
                return self.search.build_context(event)
            return SearchContext()
        try:
            return search_future.result()
        except Exception:
            return self.search.build_context(event)

    def _background_task_done(self, task_name: str, future: Future[object]) -> None:
        with self._background_lock:
            self._background_tasks.discard(future)
        try:
            future.result()
        except Exception as exc:
            with self._state_lock:
                self._event_counter += 1
                self._recent_events.append(
                    {
                        "seq": self._event_counter,
                        "kind": "background_error",
                        "task": task_name,
                        "timestamp": time.time(),
                        "error": str(exc),
                    }
                )
                self._recent_events = self._recent_events[-200:]

    def flush_background_tasks(self, *, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._background_lock:
                pending = list(self._background_tasks)
            if not pending:
                return
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if deadline is not None and remaining <= 0.0:
                raise TimeoutError("background tasks did not finish before timeout")
            wait(pending, timeout=remaining)

    def close(self, *, timeout: float | None = None) -> None:
        self.flush_background_tasks(timeout=timeout)
        for component in (
            self.generator,
            self.search,
            self.marketplace,
            self.outbound,
            self.napcat_login,
            self.state_store,
        ):
            if component is not None:
                _close_component(component)

    def _schedule_knowledge_writeback(
        self,
        event: InboundEvent,
        *,
        session: SessionMemory,
        latest_message: str,
        assistant_name: str,
        address: str,
        conversation_view: str,
    ) -> None:
        if not self.config.knowledge_auto_extract:
            return
        event_copy = deepcopy(event)
        session_copy = SessionMemory(
            launcher_id=session.launcher_id,
            launcher_type=session.launcher_type,
            character_id=session.character_id,
            history=list(session.history),
            preferred_name=session.preferred_name,
            metadata=deepcopy(session.metadata),
        )
        self._submit_background_coro(
            "knowledge_writeback",
            self._awriteback_knowledge_if_needed(
                event_copy,
                session=session_copy,
                latest_message=latest_message,
                assistant_name=assistant_name,
                address=address,
                conversation_view=conversation_view,
            ),
        )

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
        resolved_character_id = str(character_id or self._active_character_id()).strip()
        delay = self._outbound_delay_seconds(event)
        self._dispatch_outbound_message(message, delay_seconds=delay)
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
        self.value_game.apply(
            state_store=self.state_store,
            event=event,
            emotion=emotion or EmotionState(),
            reply_text=message.text,
            search_used=search_used,
        )
        self._refresh_follow_up_window(event)
        return message

    def _outbound_delay_seconds(self, event: InboundEvent) -> float:
        if event.launcher_type != "group":
            return 0.0
        if type(self.outbound) is CapturingOutboundPort:
            return 0.0
        return max(0.0, float(self.config.group_response_delay_seconds))

    def _dispatch_outbound_message(self, message: OutboundMessage, *, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            self.outbound.send(message)
            return
        send_async = getattr(self.outbound, "send_async", None)
        if callable(send_async):
            self._submit_background_coro(
                "delayed_outbound_send",
                self._adelayed_outbound_send(message, delay_seconds=delay_seconds),
            )
            return

        def delayed_send() -> None:
            time.sleep(delay_seconds)
            self.outbound.send(message)

        self._submit_background_task("delayed_outbound_send", delayed_send)

    async def _adelayed_outbound_send(self, message: OutboundMessage, *, delay_seconds: float) -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        send_async = getattr(self.outbound, "send_async")
        await send_async(message)

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
        self.knowledge._writeback_knowledge_if_needed(
            event,
            session=session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            address=address,
            conversation_view=conversation_view,
        )

    async def _awriteback_knowledge_if_needed(
        self,
        event: InboundEvent,
        *,
        session: SessionMemory,
        latest_message: str,
        assistant_name: str,
        address: str,
        conversation_view: str,
    ) -> None:
        await self.knowledge._awriteback_knowledge_if_needed(
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
        return self.knowledge._persist_extracted_knowledge(event, entry, message_id=message_id)

    def _existing_knowledge_entry(self, scope_type: str, scope_id: str, summary: str) -> dict[str, object] | None:
        return self.knowledge._existing_knowledge_entry(scope_type, scope_id, summary)

    def _knowledge_scope_for_candidate(self, event: InboundEvent, entry: dict[str, object]) -> tuple[str, str]:
        return self.knowledge._knowledge_scope_for_candidate(event, entry)

    def _known_assistant_aliases(self) -> dict[str, set[str]]:
        aliases: dict[str, set[str]] = {}
        try:
            for item in self.cards.list_characters():
                character_id = str(item.get("character", "") or "").strip()
                if not character_id:
                    continue
                names = aliases.setdefault(character_id, set())
                if bool(item.get("has_person")):
                    try:
                        names.add(
                            str(
                                self.cards.load(
                                    "person",
                                    SessionMemory(
                                        launcher_id="probe",
                                        launcher_type="person",
                                        character_id=character_id,
                                    ),
                                ).assistant_name
                            ).strip()
                        )
                    except Exception:
                        pass
                if bool(item.get("has_group")):
                    try:
                        names.add(
                            str(
                                self.cards.load(
                                    "group",
                                    SessionMemory(
                                        launcher_id="probe",
                                        launcher_type="group",
                                        character_id=character_id,
                                    ),
                                ).assistant_name
                            ).strip()
                        )
                    except Exception:
                        pass
                direct_name = str(item.get("assistant_name", "") or "").strip()
                if direct_name:
                    names.add(direct_name)
        except Exception:
            pass
        current_character = self._active_character_id()
        current_names = aliases.setdefault(current_character, set())
        for launcher_type in ("person", "group"):
            try:
                current_names.add(
                    str(
                        self.cards.load(
                            launcher_type,
                            SessionMemory(
                                launcher_id="probe",
                                launcher_type=launcher_type,
                                character_id=current_character,
                            ),
                        ).assistant_name
                    ).strip()
                )
            except Exception:
                continue
        fallback = str(self.config.assistant_name or "").strip()
        if fallback:
            current_names.add(fallback)
        return {
            character_id: {name for name in names if name}
            for character_id, names in aliases.items()
            if any(name for name in names)
        }

    def _known_assistant_names(self) -> dict[str, str]:
        aliases = self._known_assistant_aliases()
        return {
            character_id: sorted(names, key=lambda item: (len(item), item.casefold()))[0]
            for character_id, names in aliases.items()
            if names
        }

    @staticmethod
    def _mentions_any_assistant_name(text: str, names: set[str]) -> bool:
        lowered = str(text or "").casefold()
        return any(name and name.casefold() in lowered for name in names)

    def _sanitize_profile_summary_text(self, summary: str) -> str:
        fragments: list[str] = []
        seen: set[str] = set()
        assistant_names: set[str] = set()
        for names in self._known_assistant_aliases().values():
            assistant_names.update(names)
        for fragment in str(summary or "").split(";"):
            cleaned = " ".join(fragment.split()).strip().strip(",")
            if not cleaned:
                continue
            if assistant_names and self._mentions_any_assistant_name(cleaned, assistant_names):
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            fragments.append(cleaned)
        return "; ".join(fragments[:3])

    def _sanitize_session_persona_state(
        self,
        session: SessionMemory,
        *,
        assistant_name: str,
    ) -> SessionMemory:
        known_aliases = self._known_assistant_aliases()
        other_names: set[str] = set()
        current_character = str(session.character_id or "").strip()
        for character_id, names in known_aliases.items():
            if character_id == current_character:
                continue
            other_names.update(name for name in names if name and name != assistant_name)
        if not other_names:
            return session
        filtered_history: list[str] = []
        changed = False
        for raw_line in list(session.history):
            speaker, content = self._split_history_line(raw_line)
            speaker_is_assistant = speaker == "assistant" or self._mentions_any_assistant_name(speaker, other_names)
            if speaker_is_assistant and (
                self._mentions_any_assistant_name(speaker, other_names)
                or self._mentions_any_assistant_name(content, other_names)
            ):
                changed = True
                continue
            filtered_history.append(raw_line)
        if not changed:
            return session
        session.history = filtered_history
        self.memory.store.save(session)
        return session

    def _sanitize_member_persona_state(
        self,
        *,
        group_id: str,
        user_id: str,
        character_id: str,
    ) -> dict[str, object] | None:
        member = self.state_store.get_member(
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )
        if member is None:
            return None
        cleaned_summary = self._sanitize_profile_summary_text(str(member.get("profile_summary", "") or ""))
        if cleaned_summary == str(member.get("profile_summary", "") or ""):
            return member
        return self.state_store.save_member(
            {
                "group_id": group_id,
                "user_id": user_id,
                "character_id": character_id,
                "qq_nickname": str(member.get("qq_nickname", "") or ""),
                "group_card": str(member.get("group_card", "") or ""),
                "preferred_name": str(member.get("preferred_name", "") or ""),
                "onboarding_status": str(member.get("onboarding_status", "") or "new"),
                "membership_status": str(member.get("membership_status", "") or "active"),
                "profile_summary": cleaned_summary,
                "affinity_score": member.get("affinity_score", 0.0),
                "notes_count": int(member.get("notes_count", 0) or 0),
                "last_seen_at": int(member.get("last_seen_at", 0) or 0),
                "last_sync_at": int(member.get("last_sync_at", 0) or 0),
                "last_addressed_at": int(member.get("last_addressed_at", 0) or 0),
            }
        )

    def _update_member_profile_summary(self, event: InboundEvent, *, extra_summary: str = "") -> None:
        group_id = event.launcher_id if event.launcher_type == "group" else ""
        current_character = self._active_character_id()
        member = self._sanitize_member_persona_state(
            group_id=group_id,
            user_id=event.sender_id,
            character_id=current_character,
        )
        if member is None:
            return
        scope_id = f"{event.launcher_id}:{event.sender_id}" if event.launcher_type == "group" else event.sender_id
        notes_count = 0
        count_scope = getattr(self.state_store, "count_knowledge_for_scope", None)
        if callable(count_scope):
            notes_count = int(count_scope("member", scope_id, character_id=current_character) or 0)
        merged_summary = self._merge_profile_summary(
            self._sanitize_profile_summary_text(str(member.get("profile_summary", "") or "")),
            self._sanitize_profile_summary_text(extra_summary),
        )
        self.state_store.save_member(
            {
                "group_id": group_id,
                "user_id": event.sender_id,
                "character_id": current_character,
                "qq_nickname": str(member.get("qq_nickname", "") or event.sender_name),
                "group_card": str(member.get("group_card", "") or ""),
                "preferred_name": str(member.get("preferred_name", "") or ""),
                "onboarding_status": str(member.get("onboarding_status", "") or "new"),
                "membership_status": str(member.get("membership_status", "") or "active"),
                "profile_summary": merged_summary,
                "affinity_score": member.get("affinity_score", 0.0),
                "notes_count": notes_count,
                "last_seen_at": member.get("last_seen_at", 0),
                "last_sync_at": member.get("last_sync_at", 0),
                "last_addressed_at": member.get("last_addressed_at", 0),
            }
        )

    @staticmethod
    def _merge_profile_summary(existing: str, addition: str) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        for candidate in (existing, addition):
            for fragment in str(candidate or "").split(";"):
                cleaned = " ".join(fragment.split()).strip().strip(",")
                if not cleaned:
                    continue
                key = cleaned.casefold()
                if key in seen:
                    continue
                seen.add(key)
                parts.append(cleaned)
        return "; ".join(parts[:3])

    def _handle_group_increase_notice(self, payload: dict[str, object]) -> dict[str, object]:
        return self.notice._handle_group_increase_notice(payload)

    def _handle_group_decrease_notice(self, payload: dict[str, object]) -> dict[str, object]:
        return self.notice._handle_group_decrease_notice(payload)

    def _handle_group_card_notice(self, payload: dict[str, object]) -> dict[str, object]:
        return self.notice._handle_group_card_notice(payload)

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
        return KnowledgeCurator._merge_memory_hints(primary, secondary, limit=limit)

    def _knowledge_scopes(self, event: InboundEvent) -> list[tuple[str, str]]:
        return self.knowledge._knowledge_scopes(event)

    def _member_record(self, event: InboundEvent) -> dict[str, Any] | None:
        return self.knowledge._member_record(event)

    def _session_knowledge_entries(self, event: InboundEvent, query: str) -> list[dict[str, Any]]:
        return self.knowledge._session_knowledge_entries(event, query)

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
        return self.knowledge._directory_member_notes(event)

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
        return self.console.dashboard_snapshot()

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
        graph = self._session_detail_graph(session)
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
        return self.console.get_console_panels()

    def get_character_panel(self, character: str = "") -> dict[str, object]:
        return self.console.get_character_panel(character)

    def save_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_character_panel(payload)

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.console.get_character_portrait(character)

    def preview_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.preview_character_panel(payload)

    def _generate_character_portrait(
        self,
        character: str,
        bundle: dict[str, object],
        portrait_payload: dict[str, object],
    ) -> dict[str, object]:
        return self.console._generate_character_portrait(character, bundle, portrait_payload)

    def get_ai_panel(self) -> dict[str, object]:
        return self.console.get_ai_panel()

    def save_ai_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_ai_panel(payload)

    def get_memory_panel(self) -> dict[str, object]:
        return self.console.get_memory_panel()

    def save_memory_session(
        self,
        launcher_type: str,
        launcher_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        return self.console.save_memory_session(launcher_type, launcher_id, payload)

    def get_member_directory_panel(self, *, limit: int = 120) -> dict[str, object]:
        return self.console.get_member_directory_panel(limit=limit)

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_directory_member(payload)

    def reset_directory_member_persona(self, payload: dict[str, object]) -> dict[str, object] | None:
        return self.console.reset_directory_member_persona(payload)

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_knowledge_entry(payload)

    def delete_knowledge_entry(self, entry_id: int) -> bool:
        return self.console.delete_knowledge_entry(entry_id)

    def handle_notice_payload(self, payload: dict[str, object]) -> dict[str, object]:
        return self.notice.handle_notice_payload(payload)

    def sync_group_members(self, group_id: str) -> dict[str, object]:
        return self.notice.sync_group_members(group_id)

    def get_abilities_panel(self) -> dict[str, object]:
        return self.console.get_abilities_panel()

    def save_abilities_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_abilities_panel(payload)

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
        return self.console.get_proactive_panel(limit=limit)

    def generate_proactive_draft(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.generate_proactive_draft(payload)

    def get_skills_panel(self) -> dict[str, object]:
        return self.console.get_skills_panel()

    def search_marketplace(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        return self.console.search_marketplace(query, source_id=source_id, limit=limit)

    def import_marketplace_skill(
        self,
        *,
        source_id: str,
        github_url: str,
    ) -> dict[str, object]:
        return self.console.import_marketplace_skill(source_id=source_id, github_url=github_url)

    def get_sidecar_panel(self, *, refresh: bool = False) -> dict[str, object]:
        return self.console.get_sidecar_panel(refresh=refresh)

    def save_sidecar_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_sidecar_panel(payload)

    def get_qq_login_panel(self, *, refresh: bool = False) -> dict[str, object]:
        return self.console.get_qq_login_panel(refresh=refresh)

    def save_qq_login_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_qq_login_panel(payload)

    def refresh_qq_login_panel(self) -> dict[str, object]:
        return self.console.refresh_qq_login_panel()

    def get_qq_login_qrcode_image(self) -> tuple[bytes, str] | None:
        return self.console.get_qq_login_qrcode_image()

    def _apply_sidecar_payload(self, payload: dict[str, object]) -> None:
        self.console._apply_sidecar_payload(payload)

    def get_other_panel(self) -> dict[str, object]:
        return self.console.get_other_panel()

    def save_other_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.console.save_other_panel(payload)

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
            self.memory.set_character_resolver(self.cards.active_character)
        self.search = SearchDecider(self.config)
        self.event_engine = BehaviorEventEngine(self.config)
        self.narrator = Narrator(self.config)
        self.value_game = ValueGameEngine(self.config)
        self.memory_graph = MemoryGraphBuilder(self.config)
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

    def _session_detail_graph(self, session: SessionMemory) -> dict[str, Any]:
        launcher_id = str(session.launcher_id or "").strip()
        launcher_type = str(session.launcher_type or "").strip()
        character_id = str(session.character_id or self._active_character_id()).strip()
        if launcher_type == "person":
            member = self.state_store.get_member(group_id="", user_id=launcher_id, character_id=character_id)
            sender_id = launcher_id
            sender_name = str((member or {}).get("preferred_name") or (member or {}).get("qq_nickname") or launcher_id)
        else:
            recent_behavior = self.get_behavior_events(limit=1, launcher_type=launcher_type, launcher_id=launcher_id)
            sender_id = str(recent_behavior[0].get("sender_id", "") or "") if recent_behavior else ""
            member = self.state_store.get_member(
                group_id=launcher_id,
                user_id=sender_id,
                character_id=character_id,
            ) if sender_id else None
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
        return self.knowledge._detail_knowledge_entries(session)

    def _knowledge_count_for_session(self, session: SessionMemory) -> int:
        return self.knowledge._knowledge_count_for_session(session)

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
    return build_embedding_client(app_config)


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
    generator = Generator(
        app_config,
        llm_client=build_llm_client(app_config),
        image_client=build_image_client(app_config),
    )
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
