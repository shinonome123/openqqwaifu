from __future__ import annotations

import asyncio
import logging
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
from .cells.tool_registry import ToolRegistry
from .console_panels import ConsolePanels
from .config import AppConfig
from .contracts import OutboundPort
from .knowledge_curator import KnowledgeCurator
from .legacy_migrator import LegacyMigrator
from .gateways.napcat_login import (
    NapCatLoginBridge,
    NapCatLoginError,
    normalize_webui_settings,
    qrcode_payload_to_image_source,
)
from .gateways.onebot_actions import OneBotActionClient, OneBotHttpOutboundPort
from .gateways.onebot_ws import OneBotWsGateway, OneBotWsOutboundPort
from .http_transport import AsyncRuntime
from .memory import FileMemoryStore, InMemoryStore
from .models import EmotionState, InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from .notice_dispatcher import NoticeDispatcher
from .organs.memory_graph import MemoryGraphBuilder
from .organs.memories import Memory
from .organs.proactive import ProactivePlanner
from .organs.thoughts import Thoughts
from .member_onboarding import MemberOnboarding
from .outbound_emitter import OutboundEmitter
from .observability import MetricsRegistry, logging_is_configured, set_active_metrics_registry
from .persona_guard import PersonaGuard
from .plugin_api import PluginContext, load_tool_plugins
from .reply_gate import PENDING_SEARCH_METADATA_KEY, ReplyGate
from .services import CapturingOutboundPort
from .skill_dispatcher import SkillDispatcher
from .state_store import InMemoryRuntimeStateStore, SqliteRuntimeStateStore
from .systems.events import BehaviorEventEngine
from .systems.emotions import EmotionSensor
from .systems.searching import SearchContext, SearchDecider
from .systems.value_game import ValueGameEngine

_MASK_SENTINEL = "..."
_BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="openqqwaifu-bg")
_ASYNC_RUNTIME = AsyncRuntime()
_LOGGER = logging.getLogger(__name__)


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
    metrics: MetricsRegistry
    memory: Memory
    emotions: EmotionSensor
    thoughts: Thoughts
    generator: Generator
    cards: CardManager
    search: SearchDecider
    event_engine: BehaviorEventEngine
    value_game: ValueGameEngine
    memory_graph: MemoryGraphBuilder
    proactive: ProactivePlanner
    marketplace: MarketplaceClient
    skills: SkillRegistry
    tools: ToolRegistry
    state_store: Any
    outbound: OutboundPort
    reverse_ws_gateway: OneBotWsGateway | None = None
    napcat_login: NapCatLoginBridge | None = None
    _recent_outbound: list[OutboundMessage] = field(default_factory=list)
    _recent_events: list[dict[str, Any]] = field(default_factory=list)
    _recent_behavior_events: list[dict[str, Any]] = field(default_factory=list)
    _started_at: float = field(default_factory=time.monotonic, repr=False)
    _event_counter: int = 0
    _state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _session_locks: dict[tuple[str, str], threading.Lock] = field(default_factory=dict, repr=False)
    _background_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _background_tasks: set[Future[object]] = field(default_factory=set, repr=False)
    _memory_store_builder: Callable[[str], Any] | None = field(default=None, repr=False)
    _state_store_builder: Callable[[str], Any] | None = field(default=None, repr=False)
    _bound_character_id: str = field(default="", repr=False)
    console: ConsolePanels = field(init=False, repr=False)
    knowledge: KnowledgeCurator = field(init=False, repr=False)
    notice: NoticeDispatcher = field(init=False, repr=False)
    gate: ReplyGate = field(init=False, repr=False)
    emitter: OutboundEmitter = field(init=False, repr=False)
    onboarding: MemberOnboarding = field(init=False, repr=False)
    migrator: LegacyMigrator = field(init=False, repr=False)
    persona: PersonaGuard = field(init=False, repr=False)
    dispatcher: SkillDispatcher = field(init=False, repr=False)

    def __post_init__(self) -> None:
        set_active_metrics_registry(self.metrics)
        self.console = ConsolePanels(self)
        self.knowledge = KnowledgeCurator(self)
        self.notice = NoticeDispatcher(self)
        self.gate = ReplyGate(self)
        self.dispatcher = SkillDispatcher(self)
        self.emitter = OutboundEmitter(self)
        self.onboarding = MemberOnboarding(self)
        self.migrator = LegacyMigrator(self)
        self.persona = PersonaGuard(self)

    def _active_character_id(self) -> str:
        return str(self.cards.active_character() or self.config.character or "default").strip() or "default"

    def activate_character(self, character: str, *, reset_sessions: bool = True) -> str:
        active_character = self.cards.set_active_character(character)
        self.config.character = active_character
        self._rebind_character_scoped_storage(active_character, reset_sessions=reset_sessions)
        return active_character

    def _ensure_character_scope_bound(self) -> None:
        active_character = self._active_character_id()
        if self._bound_character_id == active_character:
            return
        self._rebind_character_scoped_storage(active_character, reset_sessions=False)

    def handle_event(self, event: InboundEvent) -> OutboundMessage | None:
        with self._session_lock_for(event):
            return self._handle_event_locked(event)

    async def handle_event_async(self, event: InboundEvent) -> OutboundMessage | None:
        lock = self._session_lock_for(event)
        await asyncio.to_thread(lock.acquire)
        try:
            return await self._handle_event_async_locked(event)
        finally:
            lock.release()

    def _handle_event_locked(self, event: InboundEvent) -> OutboundMessage | None:
        return _ASYNC_RUNTIME.submit(self._handle_event_async_locked(event)).result()

    async def _handle_event_async_locked(self, event: InboundEvent) -> OutboundMessage | None:
        self._ensure_character_scope_bound()
        current_character = self._active_character_id()
        text = event.command_text(self.config.bot_account_id)
        live_runtime = self._requires_live_llm()
        latest_message = self._latest_message_text(event, text)
        naming_input = self.onboarding.looks_like_naming_input(latest_message)
        self._record_inbound(event, text)
        if not text and event.image_count == 0:
            return None
        if text and any(text.startswith(prefix) for prefix in self.config.ignore_prefixes):
            if self.onboarding.looks_like_naming_input(text):
                pass
            else:
                return None
        if not self.gate.should_reply(event):
            return None
        if live_runtime and not self.generator.llm_ready and not naming_input:
            return None

        await self.onboarding.aremember_directory_member(event)
        session = await self.memory.asave_user_event(event, character_id=current_character)
        assistant_name = self._resolve_assistant_name(event, session, character_id=current_character)
        session = await self.persona.asanitize_session_state(session, assistant_name=assistant_name)
        await self.persona.asanitize_member_state(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=current_character,
        )
        address = await self._resolve_address_async(event, session)
        inbound_behavior = self.event_engine.capture_inbound(
            event,
            text=latest_message,
            character_id=current_character,
        )
        self._record_behavior_event(inbound_behavior)

        repeat_reply = self.gate.build_repeat_reply(event, session, address=address)
        if repeat_reply is not None:
            return await self.emitter.aemit(event, repeat_reply, assistant_name=assistant_name)

        onboarding_reply = await self.onboarding.amaybe_handle(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
        )
        if onboarding_reply is not None:
            return onboarding_reply

        pending_search_query = self.gate.pending_search_query_for_message(session, latest_message)
        if pending_search_query:
            return await self.dispatcher.ahandle_search_request(
                event,
                session,
                query=pending_search_query,
                address=address,
                assistant_name=assistant_name,
            )

        active_skills = self.skills.match(latest_message)
        await self._store_active_skills_async(session, active_skills)

        dispatch = self.skills.resolve_dispatch(latest_message)
        if dispatch is None:
            dispatch = self.dispatcher.resolve_builtin_dispatch(latest_message)
            if dispatch is not None:
                skill, _ = dispatch
                if all(existing.skill_id != skill.skill_id for existing in active_skills):
                    active_skills = [*active_skills, skill]
                    await self._store_active_skills_async(session, active_skills)
        if dispatch is not None:
            skill, raw_args = dispatch
            return await self.dispatcher.adispatch_skill(
                event,
                session,
                skill=skill,
                raw_args=raw_args,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
            )

        image_prompt = None if self.skills.has_dispatch_tool("image") else self.dispatcher.extract_image_prompt(text)
        if image_prompt is not None:
            return await self.dispatcher.ahandle_image_request(
                event,
                session,
                address=address,
                assistant_name=assistant_name,
                prompt=image_prompt,
                active_skills=active_skills,
            )

        emotion = self.emotions.analyze(event, session)
        search_context = await self._build_search_context_async(event)
        await self._store_search_context_async(session, search_context)
        conversation_view = await self.memory.aformat_dialogue(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            limit=self.config.history_window_messages,
            character_id=current_character,
        )
        recalled_memory_hints = await asyncio.to_thread(
            self.state_store.recall_knowledge,
            scopes=self.knowledge._knowledge_scopes(event),
            query=latest_message,
            limit=self.config.memory_recall_limit,
            character_id=current_character,
        )
        knowledge_entries = await self.knowledge._asession_knowledge_entries(event, latest_message)
        memory_hints = KnowledgeCurator._merge_memory_hints(
            recalled_memory_hints,
            [str(item.get("summary", "") or "").strip() for item in knowledge_entries],
            limit=self.config.memory_recall_limit,
        )
        member_record = await self.knowledge._amember_record(event)
        behavior_context = self._behavior_context(event)
        graph_snapshot = self.memory_graph.build(
            event=event,
            session=session,
            member=member_record,
            knowledge_entries=knowledge_entries,
            behavior_events=behavior_context,
        )
        speaker_notes = await self.knowledge._adirectory_member_notes(event)
        for highlight in graph_snapshot.get("highlights", [])[:3] if isinstance(graph_snapshot, dict) else []:
            text_hint = str(highlight or "").strip()
            if text_hint:
                speaker_notes.append(text_hint)
        analysis_hint = await self.thoughts.aanalyze(
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
        reply_text = await self.generator.agenerate_reply(
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
        reply_text = await self.onboarding.amaybe_append_soft_ask(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            reply_text=reply_text,
            character_id=current_character,
        )
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        emitted = await self.emitter.aemit(
            event,
            message,
            assistant_name=assistant_name,
            emotion=emotion,
            search_used=bool(search_context.active),
            behavior_reason="reply",
            character_id=current_character,
        )
        await self._schedule_knowledge_writeback_async(
            event,
            session=await self.memory.aload(event.launcher_id, event.launcher_type, character_id=current_character),
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

    def _track_async_task(self, task_name: str, task: asyncio.Task[object]) -> None:
        proxy: Future[object] = Future()
        with self._background_lock:
            self._background_tasks.add(proxy)

        def _finish(completed: asyncio.Task[object], *, name: str = task_name, tracked: Future[object] = proxy) -> None:
            if completed.cancelled():
                tracked.cancel()
            else:
                try:
                    tracked.set_result(completed.result())
                except Exception as exc:
                    tracked.set_exception(exc)
            self._background_task_done(name, tracked)

        task.add_done_callback(_finish)

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

    async def _build_search_context_async(self, event: InboundEvent) -> SearchContext:
        if not self.search.should_search(event):
            return SearchContext()
        search_state = getattr(self.search, "__dict__", {})
        if "build_context" in search_state and "abuild_context" not in search_state:
            return await asyncio.to_thread(self.search.build_context, event)
        try:
            return await self.search.abuild_context(event)
        except Exception:
            return await asyncio.to_thread(self.search.build_context, event)

    def _background_task_done(self, task_name: str, future: Future[object]) -> None:
        with self._background_lock:
            self._background_tasks.discard(future)
        try:
            future.result()
        except Exception as exc:
            if logging_is_configured():
                _LOGGER.exception("background task failed task=%s", task_name)
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
            self.knowledge._awriteback_knowledge_if_needed(
                event_copy,
                session=session_copy,
                latest_message=latest_message,
                assistant_name=assistant_name,
                address=address,
                conversation_view=conversation_view,
            ),
        )

    async def _schedule_knowledge_writeback_async(
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
        task = asyncio.create_task(
            self.knowledge._awriteback_knowledge_if_needed(
                event_copy,
                session=session_copy,
                latest_message=latest_message,
                assistant_name=assistant_name,
                address=address,
                conversation_view=conversation_view,
            )
        )
        self._track_async_task("knowledge_writeback", task)

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

    async def _archive_if_needed_async(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        assistant_name: str,
        character_id: str = "",
    ) -> None:
        if not self.config.summarization_mode:
            return
        archived = await self.memory.amaybe_archive_history(
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
        await asyncio.to_thread(
            self.state_store.add_knowledge,
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

    def _latest_message_text(self, event: InboundEvent, command_text: str) -> str:
        if self.config.multimodal_enabled and event.image_count > 0:
            return event.to_memory_text()
        return command_text or event.to_memory_text()

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

    def _behavior_context(self, event: InboundEvent, *, limit: int = 8) -> list[dict[str, Any]]:
        current_character = self._active_character_id()
        launcher_type = str(event.launcher_type or "").strip()
        launcher_id = str(event.launcher_id or "").strip()
        sender_id = str(event.sender_id or "").strip()
        with self._state_lock:
            items = [dict(item) for item in self._recent_behavior_events]
        scoped = [
            item
            for item in items
            if str(item.get("character_id", "") or "").strip() == current_character
            and str(item.get("launcher_type", "") or "").strip() == launcher_type
            and str(item.get("launcher_id", "") or "").strip() == launcher_id
            and str(item.get("sender_id", "") or "").strip() == sender_id
        ]
        return scoped[-max(1, int(limit)) :][::-1]

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
                    "assistant_alias": self._session_assistant_alias(session),
                    "assistant_name": card.assistant_name,
                    "history_count": len(history),
                    "message_count": len(history),
                    "long_term_count": self.knowledge._knowledge_count_for_session(session),
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
            "assistant_alias": self._session_assistant_alias(session),
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

    async def handle_notice_payload_async(self, payload: dict[str, object]) -> dict[str, object]:
        return await self.notice.ahandle_notice_payload(payload)

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
        character_id: str = "",
    ) -> list[dict[str, object]]:
        safe_character = str(character_id or self._active_character_id()).strip()
        safe_type = str(launcher_type or "").strip()
        safe_id = str(launcher_id or "").strip()
        with self._state_lock:
            items = [dict(item) for item in self._recent_behavior_events[-max(1, int(limit)) :][::-1]]
        if not safe_type and not safe_id and not safe_character:
            return items
        filtered: list[dict[str, object]] = []
        for item in items:
            if safe_character and str(item.get("character_id", "") or "").strip() != safe_character:
                continue
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
            current_character = self._active_character_id()
            active = self.gate.active_followup_count_locked(
                current_character, now_monotonic=time.monotonic()
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

    def _rebind_character_scoped_storage(self, character_id: str, *, reset_sessions: bool = False) -> None:
        active_character = str(character_id or "").strip() or self._active_character_id()
        if self._memory_store_builder is not None:
            self.memory.store = self._memory_store_builder(active_character)
        if self._state_store_builder is not None:
            previous_state_store = self.state_store
            self.state_store = self._state_store_builder(active_character)
            if previous_state_store is not self.state_store:
                _close_component(previous_state_store)
        if hasattr(self.state_store, "set_default_character"):
            self.state_store.set_default_character(active_character)
        if hasattr(self.state_store, "refresh_knowledge_embeddings"):
            self.state_store.refresh_knowledge_embeddings()
        self.memory.set_character_resolver(self.cards.active_character)
        self._bound_character_id = active_character
        self._reset_character_runtime_context(reset_sessions=reset_sessions)

    def _reset_character_runtime_context(self, *, reset_sessions: bool = False) -> None:
        if reset_sessions:
            clear_sessions = getattr(self.memory.store, "clear", None)
            if callable(clear_sessions):
                clear_sessions()
        with self._state_lock:
            self.gate.clear_all_windows_locked()
            self._recent_behavior_events.clear()
            self._session_locks.clear()

    def _refresh_runtime_components(self, *, rebuild_generator: bool = False, rebuild_outbound: bool = False) -> None:
        set_active_metrics_registry(self.metrics)
        if rebuild_generator:
            self.generator = Generator(self.config)
            self.cards = self.generator._cards
            self.thoughts = Thoughts(self.config, self.generator)
            self.memory.set_character_resolver(self.cards.active_character)
        self.search = SearchDecider(self.config)
        self.event_engine = BehaviorEventEngine(self.config)
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
            self.outbound, self.reverse_ws_gateway = _build_runtime_outbound(
                self.config,
                existing_gateway=self.reverse_ws_gateway,
            )

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

    async def _store_active_skills_async(self, session: SessionMemory, active_skills: list[SkillSpec]) -> None:
        await asyncio.to_thread(self._store_active_skills, session, active_skills)

    def _store_search_context(self, session: SessionMemory, search_context: SearchContext) -> None:
        if search_context.query:
            session.metadata["last_search"] = search_context.as_dict()
            if search_context.active:
                session.metadata.pop(PENDING_SEARCH_METADATA_KEY, None)
            else:
                self.gate.store_pending_search(session, query=search_context.query)
        else:
            session.metadata.pop("last_search", None)
            session.metadata.pop(PENDING_SEARCH_METADATA_KEY, None)
        self.memory.store.save(session)

    async def _store_search_context_async(self, session: SessionMemory, search_context: SearchContext) -> None:
        await asyncio.to_thread(self._store_search_context, session, search_context)

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
            identity_metadata: dict[str, object] = {}
            waifu_root = session.metadata.get("waifu_root")
            if isinstance(waifu_root, str) and waifu_root.strip():
                identity_metadata["waifu_root"] = waifu_root
            identity_session = SessionMemory(
                launcher_id=session.launcher_id,
                launcher_type=session.launcher_type,
                character_id=str(session.character_id or self._active_character_id()).strip(),
                metadata=identity_metadata,
            )
            user_name = str(self.cards.load(event.launcher_type, identity_session).user_name or "").strip()
            if user_name:
                return user_name
        return event.sender_name or "你"

    async def _resolve_address_async(self, event: InboundEvent, session: SessionMemory) -> str:
        return await asyncio.to_thread(self._resolve_address, event, session)

    def _assistant_alias_for_user(self, user_id: str, *, character_id: str = "") -> str:
        getter = getattr(self.state_store, "get_assistant_alias", None)
        if not callable(getter):
            return ""
        safe_user_id = str(user_id or "").strip()
        safe_character_id = str(character_id or self._active_character_id()).strip()
        if not (safe_user_id and safe_character_id):
            return ""
        try:
            record = getter(character_id=safe_character_id, user_id=safe_user_id)
        except ValueError:
            return ""
        return str((record or {}).get("assistant_alias", "") or "").strip()

    def _resolve_assistant_name(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        character_id: str = "",
    ) -> str:
        base_name = self.generator.resolve_assistant_name(event.launcher_type, session)
        alias = self._assistant_alias_for_user(
            event.sender_id,
            character_id=str(character_id or session.character_id or self._active_character_id()).strip(),
        )
        return alias or base_name

    def _session_assistant_alias(self, session: SessionMemory) -> str:
        character_id = str(session.character_id or self._active_character_id()).strip()
        if not character_id:
            return ""
        if session.launcher_type == "person":
            return self._assistant_alias_for_user(session.launcher_id, character_id=character_id)
        recent_behavior = self.get_behavior_events(
            limit=1,
            launcher_type=session.launcher_type,
            launcher_id=session.launcher_id,
            character_id=character_id,
        )
        sender_id = str(recent_behavior[0].get("sender_id", "") or "").strip() if recent_behavior else ""
        if not sender_id:
            return ""
        return self._assistant_alias_for_user(sender_id, character_id=character_id)

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
        knowledge_entries = self.knowledge._detail_knowledge_entries(session)
        behavior_events = self.get_behavior_events(
            limit=8,
            launcher_type=launcher_type,
            launcher_id=launcher_id,
            character_id=str(session.character_id or self._active_character_id()).strip(),
        )
        return self.memory_graph.build(
            event=detail_event,
            session=session,
            member=member,
            knowledge_entries=knowledge_entries,
            behavior_events=behavior_events,
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
    metrics = MetricsRegistry(service_name=str(app_config.service_name or "openqqwaifu"))
    set_active_metrics_registry(metrics)
    initial_character = _initial_character_id(app_config)
    memory_builder = lambda character_id: InMemoryStore(scoped_character_id=character_id)
    state_builder = lambda character_id: InMemoryRuntimeStateStore(embedder=_build_embedding_client(app_config))
    return _build_service(
        app_config,
        metrics,
        memory_builder(initial_character),
        state_builder(initial_character),
        CapturingOutboundPort(),
        memory_store_builder=memory_builder,
        state_store_builder=state_builder,
    )


def build_file_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, CapturingOutboundPort]:
    app_config = config or AppConfig()
    metrics = MetricsRegistry(service_name=str(app_config.service_name or "openqqwaifu"))
    set_active_metrics_registry(metrics)
    session_root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    state_root = Path(app_config.data_root) / "state" / "characters"
    initial_character = _initial_character_id(app_config)
    memory_builder = lambda character_id: FileMemoryStore(
        session_root / character_id,
        scoped_character_id=character_id,
    )
    state_builder = lambda character_id: SqliteRuntimeStateStore(
        state_root / character_id / "runtime.sqlite3",
        embedder=_build_embedding_client(app_config),
    )
    return _build_service(
        app_config,
        metrics,
        memory_builder(initial_character),
        state_builder(initial_character),
        CapturingOutboundPort(),
        memory_store_builder=memory_builder,
        state_store_builder=state_builder,
    )


def build_runtime_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, OutboundPort]:
    app_config = config or AppConfig()
    metrics = MetricsRegistry(service_name=str(app_config.service_name or "openqqwaifu"))
    set_active_metrics_registry(metrics)
    session_root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    outbound, reverse_ws_gateway = _build_runtime_outbound(app_config)
    state_root = Path(app_config.data_root) / "state" / "characters"
    initial_character = _initial_character_id(app_config)
    memory_builder = lambda character_id: FileMemoryStore(
        session_root / character_id,
        scoped_character_id=character_id,
    )
    state_builder = lambda character_id: SqliteRuntimeStateStore(
        state_root / character_id / "runtime.sqlite3",
        embedder=_build_embedding_client(app_config),
    )
    return _build_service(
        app_config,
        metrics,
        memory_builder(initial_character),
        state_builder(initial_character),
        outbound,
        reverse_ws_gateway=reverse_ws_gateway,
        memory_store_builder=memory_builder,
        state_store_builder=state_builder,
    )


def _build_runtime_outbound(
    app_config: AppConfig,
    *,
    existing_gateway: OneBotWsGateway | None = None,
) -> tuple[OutboundPort, OneBotWsGateway | None]:
    gateway_mode = str(app_config.qq_sidecar.gateway_mode or "http").strip().lower()
    if gateway_mode == "reverse_ws":
        gateway = existing_gateway or OneBotWsGateway(
            access_token=app_config.qq_sidecar.reverse_ws_access_token or app_config.qq_sidecar.access_token,
            send_timeout_seconds=app_config.qq_sidecar.reverse_ws_send_timeout_seconds,
        )
        gateway.configure(
            access_token=app_config.qq_sidecar.reverse_ws_access_token or app_config.qq_sidecar.access_token,
            send_timeout_seconds=app_config.qq_sidecar.reverse_ws_send_timeout_seconds,
        )
        return OneBotWsOutboundPort(gateway), gateway
    if not app_config.qq_sidecar.outbound_base_url:
        return CapturingOutboundPort(), None
    client = OneBotActionClient(
        base_url=app_config.qq_sidecar.outbound_base_url,
        timeout=app_config.qq_sidecar.outbound_timeout_seconds,
        access_token=app_config.qq_sidecar.access_token,
    )
    return OneBotHttpOutboundPort(client), None


def _build_embedding_client(app_config: AppConfig) -> EmbeddingClient:
    return build_embedding_client(app_config)


def _build_napcat_login_bridge(app_config: AppConfig) -> NapCatLoginBridge:
    return NapCatLoginBridge(
        base_url=app_config.qq_sidecar.webui_base_url,
        api_prefix=app_config.qq_sidecar.webui_api_prefix,
        webui_token=app_config.qq_sidecar.webui_token,
        timeout=app_config.qq_sidecar.webui_timeout_seconds,
    )


def _initial_character_id(app_config: AppConfig) -> str:
    return str(CardManager(app_config).active_character() or app_config.character or "default").strip() or "default"


def _build_service(
    app_config: AppConfig,
    metrics: MetricsRegistry,
    store: Any,
    state_store: Any,
    outbound: OutboundPort,
    *,
    reverse_ws_gateway: OneBotWsGateway | None = None,
    memory_store_builder: Callable[[str], Any] | None = None,
    state_store_builder: Callable[[str], Any] | None = None,
) -> tuple[WaifuService, OutboundPort]:
    set_active_metrics_registry(metrics)
    generator = Generator(
        app_config,
        llm_client=build_llm_client(app_config),
        image_client=build_image_client(app_config),
    )
    cards = generator._cards
    skills = SkillRegistry(app_config)
    tools = ToolRegistry()
    if app_config.plugins.enabled:
        load_tool_plugins(
            PluginContext(
                app_config=app_config,
                tool_registry=tools,
                logger=logging.getLogger("waifu.plugins"),
                metrics=metrics,
            ),
            disabled=set(app_config.plugins.disabled_names),
        )
    service = WaifuService(
        config=app_config,
        metrics=metrics,
        memory=Memory(store),
        emotions=EmotionSensor(),
        thoughts=Thoughts(app_config, generator),
        generator=generator,
        cards=cards,
        search=SearchDecider(app_config),
        event_engine=BehaviorEventEngine(app_config),
        value_game=ValueGameEngine(app_config),
        memory_graph=MemoryGraphBuilder(app_config),
        proactive=ProactivePlanner(app_config),
        marketplace=MarketplaceClient(app_config.marketplace),
        skills=skills,
        tools=tools,
        state_store=state_store,
        outbound=outbound,
        reverse_ws_gateway=reverse_ws_gateway,
        napcat_login=_build_napcat_login_bridge(app_config),
        _memory_store_builder=memory_store_builder,
        _state_store_builder=state_store_builder,
    )
    service.memory.set_character_resolver(service.cards.active_character)
    service._bound_character_id = service.cards.active_character()
    if hasattr(service.state_store, "set_default_character"):
        service.state_store.set_default_character(service.cards.active_character())
    service.migrator.repair_character_isolation(service.cards.active_character())
    if hasattr(service.state_store, "refresh_knowledge_embeddings"):
        service.state_store.refresh_knowledge_embeddings()
    tools.register(
        "image",
        name="图片生成",
        description="调用图像生成能力并返回图文消息。",
        handler=service.dispatcher.run_image_tool,
        async_handler=service.dispatcher.arun_image_tool,
    )
    tools.register(
        "search",
        name="联网搜索",
        description="执行联网检索并把摘要整理成回复。",
        handler=service.dispatcher.run_search_tool,
        async_handler=service.dispatcher.arun_search_tool,
    )
    tools.register(
        "summary",
        name="会话总结",
        description="总结最近会话并提取重点标签。",
        handler=service.dispatcher.run_summary_tool,
        async_handler=service.dispatcher.arun_summary_tool,
    )
    tools.register(
        "skill-list",
        name="技能列表",
        description="列出当前所有已启用的技能及其触发方式。",
        handler=service.dispatcher.run_skill_list_tool,
        async_handler=service.dispatcher.arun_skill_list_tool,
    )
    return service, outbound
