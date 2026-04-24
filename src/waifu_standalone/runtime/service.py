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

from ..character import CardManager, CharacterService
from ..claw_runtime_admin import ClawRuntimeAdminService
from ..config import AppConfig
from ..contracts import OutboundPort
from ..gateways import NoticeDispatcher
from ..gateways.napcat_login import (
    NapCatLoginBridge,
    NapCatLoginError,
    normalize_webui_settings,
    qrcode_payload_to_image_source,
)
from ..gateways.onebot_actions import OneBotActionClient, OneBotHttpOutboundPort
from ..gateways.onebot_ws import OneBotWsGateway, OneBotWsOutboundPort
from ..infra import AsyncRuntime, ClawRuntimeBridge, build_claw_runtime_bridge
from ..infra import MetricsRegistry, logging_is_configured, set_active_metrics_registry
from ..memory import FileMemoryStore, InMemoryStore, KnowledgeService, Memory, MemoryGraphBuilder
from ..memory_admin import MemoryAdminService
from ..members import MemberOnboarding, MemberService, NamingIntentRouter, PersonaGuard
from ..models import EmotionState, InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from ..organs.proactive import ProactivePlanner
from ..organs.thoughts import Thoughts
from ..reply_flow import Generator, OutboundEmitter, PENDING_SEARCH_METADATA_KEY, ReplyFlowService, ReplyGate
from ..repositories import (
    InMemoryRuntimeStateStore,
    KnowledgeRepository,
    MemberRepository,
    SessionRepository,
    SqliteRuntimeStateStore,
)
from ..settings_admin import SettingsAdminService
from ..settings_admin.config_manager import ConfigManager, serialize_app_config
from ..skills import (
    IntentRouter,
    MarketplaceClient,
    OPENCLAW_TOOL_ALIASES,
    PluginContext,
    SkillDispatcher,
    SkillRegistry,
    SkillSpec,
    ToolCallingOrchestrator,
    ToolRegistry,
    load_tool_plugins,
)
from ..skills_admin import SkillsAdminService
from ..systems.emotions import EmotionSensor
from ..systems.events import BehaviorEventEngine
from ..systems.searching import SearchContext, SearchDecider
from ..systems.value_game import ValueGameEngine
from ..web.console_panels import ConsolePanels
from .container import (
    _build_embedding_client,
    _build_napcat_login_bridge,
    _build_runtime_outbound,
    build_default_service,
    build_file_service,
    build_runtime_service,
)
from .legacy_migrator import LegacyMigrator
from .testing import CapturingOutboundPort

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
    claw_runtime: ClawRuntimeBridge
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
    member_repository: MemberRepository = field(init=False, repr=False)
    knowledge_repository: KnowledgeRepository = field(init=False, repr=False)
    session_repository: SessionRepository = field(init=False, repr=False)
    console: ConsolePanels = field(init=False, repr=False)
    character_service: CharacterService = field(init=False, repr=False)
    member_service: MemberService = field(init=False, repr=False)
    memory_admin_service: MemoryAdminService = field(init=False, repr=False)
    skills_admin_service: SkillsAdminService = field(init=False, repr=False)
    settings_admin_service: SettingsAdminService = field(init=False, repr=False)
    claw_runtime_admin_service: ClawRuntimeAdminService = field(init=False, repr=False)
    knowledge: KnowledgeService = field(init=False, repr=False)
    notice: NoticeDispatcher = field(init=False, repr=False)
    gate: ReplyGate = field(init=False, repr=False)
    emitter: OutboundEmitter = field(init=False, repr=False)
    onboarding: MemberOnboarding = field(init=False, repr=False)
    migrator: LegacyMigrator = field(init=False, repr=False)
    persona: PersonaGuard = field(init=False, repr=False)
    dispatcher: SkillDispatcher = field(init=False, repr=False)
    intent_router: IntentRouter = field(init=False, repr=False)
    naming_router: NamingIntentRouter = field(init=False, repr=False)
    tool_orchestrator: ToolCallingOrchestrator = field(init=False, repr=False)
    reply_flow: ReplyFlowService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        set_active_metrics_registry(self.metrics)
        self.member_repository = MemberRepository(
            self.state_store,
            character_resolver=lambda: self.cards.active_character(),
        )
        self.knowledge_repository = KnowledgeRepository(
            self.state_store,
            character_resolver=lambda: self.cards.active_character(),
        )
        self.session_repository = SessionRepository(self.memory.store)
        self.character_service = CharacterService(self)
        self.member_service = MemberService(self)
        self.memory_admin_service = MemoryAdminService(self)
        self.skills_admin_service = SkillsAdminService(self)
        self.settings_admin_service = SettingsAdminService(self)
        self.claw_runtime_admin_service = ClawRuntimeAdminService(self)
        self.reply_flow = ReplyFlowService(self)
        self.console = ConsolePanels(self)
        self.knowledge = KnowledgeService(self)
        self.notice = NoticeDispatcher(self)
        self.gate = ReplyGate(self)
        self.dispatcher = SkillDispatcher(self)
        self.intent_router = IntentRouter(self)
        self.naming_router = NamingIntentRouter(self)
        self.tool_orchestrator = ToolCallingOrchestrator(self)
        self.emitter = OutboundEmitter(self)
        self.onboarding = MemberOnboarding(self)
        self.migrator = LegacyMigrator(self)
        self.persona = PersonaGuard(self)

    def _active_character_id(self) -> str:
        return self.character_service.active_character_id()

    def activate_character(self, character: str, *, reset_sessions: bool = True) -> str:
        return self.character_service.activate_character(character, reset_sessions=reset_sessions)

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
            return await self._handle_event_async_locked(
                event, background_image_delivery=True
            )
        finally:
            lock.release()

    def _handle_event_locked(self, event: InboundEvent) -> OutboundMessage | None:
        return _ASYNC_RUNTIME.submit(
            self._handle_event_async_locked(event, background_image_delivery=False)
        ).result()

    async def _handle_event_async_locked(
        self,
        event: InboundEvent,
        *,
        background_image_delivery: bool,
    ) -> OutboundMessage | None:
        return await self.reply_flow.handle_event_async_locked(
            event,
            background_image_delivery=background_image_delivery,
        )

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
            self.claw_runtime,
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
        self.knowledge_repository.add_knowledge_for_active(
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
            self.knowledge_repository.add_knowledge_for_active,
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
        return self.memory_admin_service.list_sessions(limit=limit)

    def get_session_detail(self, launcher_type: str, launcher_id: str) -> dict[str, object] | None:
        return self.memory_admin_service.get_session_detail(launcher_type, launcher_id)

    def list_skills(self) -> dict[str, object]:
        return self.skills_admin_service.list_skills()

    def list_tools(self) -> dict[str, object]:
        return self.skills_admin_service.list_tools()

    def get_console_panels(self) -> dict[str, object]:
        return self.console.get_console_panels()

    def get_character_panel(self, character: str = "") -> dict[str, object]:
        return self.character_service.get_character_panel(character)

    def save_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.character_service.save_character_panel(payload)

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.character_service.get_character_portrait(character)

    def preview_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.character_service.preview_character_panel(payload)

    def _generate_character_portrait(
        self,
        character: str,
        bundle: dict[str, object],
        portrait_payload: dict[str, object],
    ) -> dict[str, object]:
        return self.character_service.generate_character_portrait(character, bundle, portrait_payload)

    def get_ai_panel(self) -> dict[str, object]:
        return self.settings_admin_service.get_ai_panel()

    def save_ai_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.settings_admin_service.save_ai_panel(payload)

    def get_memory_panel(self) -> dict[str, object]:
        return self.memory_admin_service.get_memory_panel()

    def save_memory_session(
        self,
        launcher_type: str,
        launcher_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        return self.memory_admin_service.save_memory_session(launcher_type, launcher_id, payload)

    def get_member_directory_panel(self, *, limit: int = 120) -> dict[str, object]:
        return self.member_service.get_member_directory_panel(limit=limit)

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        return self.member_service.save_directory_member(payload)

    def reset_directory_member_persona(self, payload: dict[str, object]) -> dict[str, object] | None:
        return self.member_service.reset_directory_member_persona(payload)

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        return self.memory_admin_service.save_knowledge_entry(payload)

    def delete_knowledge_entry(self, entry_id: int) -> bool:
        return self.memory_admin_service.delete_knowledge_entry(entry_id)

    def handle_notice_payload(self, payload: dict[str, object]) -> dict[str, object]:
        return self.notice.handle_notice_payload(payload)

    async def handle_notice_payload_async(self, payload: dict[str, object]) -> dict[str, object]:
        return await self.notice.ahandle_notice_payload(payload)

    def sync_group_members(self, group_id: str) -> dict[str, object]:
        return self.notice.sync_group_members(group_id)

    def get_abilities_panel(self) -> dict[str, object]:
        return self.settings_admin_service.get_abilities_panel()

    def save_abilities_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.settings_admin_service.save_abilities_panel(payload)

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
        return self.member_service.get_proactive_panel(limit=limit)

    def generate_proactive_draft(self, payload: dict[str, object]) -> dict[str, object]:
        return self.member_service.generate_proactive_draft(payload)

    def get_skills_panel(self) -> dict[str, object]:
        return self.skills_admin_service.get_skills_panel()

    def get_claw_runtime_panel(self, *, refresh: bool = False) -> dict[str, object]:
        return self.claw_runtime_admin_service.get_claw_runtime_panel(refresh=refresh)

    def list_claw_plugins(self) -> dict[str, object]:
        return self.claw_runtime_admin_service.list_claw_plugins()

    def inspect_claw_plugin(self, plugin_id: str) -> dict[str, object] | None:
        return self.claw_runtime_admin_service.inspect_claw_plugin(plugin_id)

    def check_claw_plugins(self) -> dict[str, object]:
        return self.claw_runtime_admin_service.check_claw_plugins()

    def update_claw_plugin(self, plugin_id: str) -> dict[str, object]:
        return self.claw_runtime_admin_service.update_claw_plugin(plugin_id)

    def search_marketplace(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        return self.skills_admin_service.search_marketplace(query, source_id=source_id, limit=limit)

    def import_marketplace_skill(
        self,
        *,
        source_id: str,
        github_url: str,
    ) -> dict[str, object]:
        return self.skills_admin_service.import_marketplace_skill(source_id=source_id, github_url=github_url)

    def get_sidecar_panel(self, *, refresh: bool = False) -> dict[str, object]:
        return self.settings_admin_service.get_sidecar_panel(refresh=refresh)

    def save_sidecar_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.settings_admin_service.save_sidecar_panel(payload)

    def get_qq_login_panel(self, *, refresh: bool = False) -> dict[str, object]:
        return self.settings_admin_service.get_qq_login_panel(refresh=refresh)

    def save_qq_login_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.settings_admin_service.save_qq_login_panel(payload)

    def refresh_qq_login_panel(self) -> dict[str, object]:
        return self.settings_admin_service.refresh_qq_login_panel()

    def get_qq_login_qrcode_image(self) -> tuple[bytes, str] | None:
        return self.settings_admin_service.get_qq_login_qrcode_image()

    def _apply_sidecar_payload(self, payload: dict[str, object]) -> None:
        self.settings_admin_service._apply_sidecar_payload(payload)

    def get_other_panel(self) -> dict[str, object]:
        return self.settings_admin_service.get_other_panel()

    def save_other_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.settings_admin_service.save_other_panel(payload)

    def skill_pack_template(self) -> dict[str, object]:
        return self.skills_admin_service.skill_pack_template()

    def export_skill_pack(
        self,
        *,
        skill_ids: list[str] | None = None,
        include_builtin: bool = False,
        name: str = "",
        description: str = "",
    ) -> dict[str, object]:
        return self.skills_admin_service.export_skill_pack(
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
        return self.skills_admin_service.import_skill_pack(payload, overwrite=overwrite)

    def import_skill_bundle(
        self,
        source: str | Path,
        *,
        overwrite: bool = True,
        source_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self.skills_admin_service.import_skill_bundle(
            source,
            overwrite=overwrite,
            source_metadata=source_metadata,
        )

    def get_skill_detail(self, skill_id: str) -> dict[str, object] | None:
        return self.skills_admin_service.get_skill_detail(skill_id)

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, object] | None:
        return self.skills_admin_service.set_skill_enabled(skill_id, enabled)

    def install_skill(self, markdown: str, filename: str | None = None) -> dict[str, object]:
        return self.skills_admin_service.install_skill(markdown, filename=filename)

    def save_skill(self, skill_id: str, markdown: str) -> dict[str, object] | None:
        return self.skills_admin_service.save_skill(skill_id, markdown)

    def delete_skill(self, skill_id: str) -> bool:
        return self.skills_admin_service.delete_skill(skill_id)

    def reload_skills(self) -> dict[str, object]:
        return self.skills_admin_service.reload_skills()

    def new_skill_template(self) -> dict[str, str]:
        return self.skills_admin_service.new_skill_template()

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
                "claw_runtime": self.claw_runtime.describe(refresh=False),
            }

    def _persist_config(self) -> None:
        if not self.config.config_path:
            return
        ConfigManager().save(self.config)

    def _sync_claw_runtime_workspace_bundles(self) -> dict[str, object]:
        return self.claw_runtime_admin_service.sync_workspace_bundles()

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
        self.session_repository.bind(self.memory.store)
        if self._state_store_builder is not None:
            previous_state_store = self.state_store
            self.state_store = self._state_store_builder(active_character)
            if previous_state_store is not self.state_store:
                _close_component(previous_state_store)
        self.member_repository.bind(self.state_store)
        self.knowledge_repository.bind(self.state_store)
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
        old_claw_runtime = self.claw_runtime
        self.claw_runtime = build_claw_runtime_bridge(self.config.claw_runtime, data_root=self.config.data_root)
        self.napcat_login = _build_napcat_login_bridge(self.config)
        if old_claw_runtime is not self.claw_runtime:
            _close_component(old_claw_runtime)
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
        self.session_repository.save(session)

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
        self.session_repository.save(session)

    async def _store_search_context_async(self, session: SessionMemory, search_context: SearchContext) -> None:
        await asyncio.to_thread(self._store_search_context, session, search_context)

    def _resolve_address(self, event: InboundEvent, session: SessionMemory) -> str:
        return self.member_service.resolve_address(event, session)

    async def _resolve_address_async(self, event: InboundEvent, session: SessionMemory) -> str:
        return await self.member_service.resolve_address_async(event, session)

    def _assistant_alias_for_user(self, user_id: str, *, character_id: str = "") -> str:
        return self.member_service.assistant_alias_for_user(user_id, character_id=character_id)

    def _resolve_assistant_name(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        character_id: str = "",
    ) -> str:
        return self.member_service.resolve_assistant_name(
            event,
            session,
            character_id=character_id,
        )

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
