from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..cells.generator import Generator
from ..cells.marketplace import MarketplaceClient
from ..cells.utils import safe_float, safe_int
from ..config import AppConfig
from ..organs.proactive import ProactivePlanner
from ..systems.searching import SearchDecider


@dataclass(slots=True)
class DashboardService:
    config: AppConfig
    generator: Generator
    search: SearchDecider
    proactive: ProactivePlanner
    marketplace: MarketplaceClient
    skills: Any
    tools: Any
    state_store: Any
    active_character_id: Callable[[], str]
    list_sessions: Callable[[int], list[dict[str, object]]]
    outbound_mode_label: Callable[[], str]
    refresh_runtime_components: Callable[..., None]
    persist_config: Callable[[], None]

    def archived_controls_panel(self) -> dict[str, object]:
        return {
            "note": "Legacy prompt-side analysis and narrator controls have been consolidated into the unified PromptBuilder.",
            "fields": {
                "thinking_mode": self.config.thinking_mode,
                "conversation_analysis": self.config.conversation_analysis,
                "narrator_mode": self.config.narrator_mode,
                "memory_graph_mode": self.config.memory_graph_mode,
            },
        }

    def snapshot(
        self,
        *,
        recent_outbound: list[dict[str, object]],
        recent_behavior_events: list[dict[str, object]],
        active_launchers: list[str],
    ) -> dict[str, object]:
        current_character = self.active_character_id()
        sessions = self.list_sessions(24)
        knowledge_count = self.state_store.knowledge_count(character_id=current_character)
        member_count = self.state_store.member_count()
        proactive_candidates = self.proactive.list_candidates(
            members=self.state_store.list_members(limit=200, character_id=current_character),
            limit=self.config.proactive_candidate_limit,
        )
        return {
            "service_name": self.config.service_name,
            "assistant_name": self.config.assistant_name,
            "character": current_character,
            "bot_account_id": self.config.bot_account_id,
            "group_reply_requires_mention": self.config.group_reply_requires_mention,
            "max_active_skills": self.config.max_active_skills,
            "search_enabled": self.config.search_enabled,
            "summarization_mode": self.config.summarization_mode,
            "event_mode": self.config.event_mode,
            "value_game_mode": self.config.value_game_mode,
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
            "outbound_mode": self.outbound_mode_label(),
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
            "archived_runtime": self.archived_controls_panel(),
        }

    def get_abilities_panel(self) -> dict[str, object]:
        return {
            "search_enabled": self.config.search_enabled,
            "search_result_limit": self.config.search_result_limit,
            "search_timeout_seconds": self.config.search_timeout_seconds,
            "summarization_mode": self.config.summarization_mode,
            "member_auto_sync": self.config.member_auto_sync,
            "knowledge_auto_extract": self.config.knowledge_auto_extract,
            "knowledge_auto_extract_limit": self.config.knowledge_auto_extract_limit,
            "event_mode": self.config.event_mode,
            "event_buffer_limit": self.config.event_buffer_limit,
            "value_game_mode": self.config.value_game_mode,
            "value_game_reply_bonus": self.config.value_game_reply_bonus,
            "proactive_mode": self.config.proactive_mode,
            "proactive_inactive_hours": self.config.proactive_inactive_hours,
            "proactive_candidate_limit": self.config.proactive_candidate_limit,
            "proactive_min_affinity": self.config.proactive_min_affinity,
            "max_active_skills": self.config.max_active_skills,
            "history_window_messages": self.config.history_window_messages,
            "memory_recall_limit": self.config.memory_recall_limit,
            "short_term_memory_limit": self.config.short_term_memory_limit,
            "memory_summary_batch_size": self.config.memory_summary_batch_size,
            "tools": self.tools.describe(),
            "marketplace": self.marketplace.describe(),
        }

    def save_abilities_panel(self, payload: dict[str, object]) -> dict[str, object]:
        self.config.search_enabled = bool(payload.get("search_enabled", self.config.search_enabled))
        self.config.search_result_limit = safe_int(payload, "search_result_limit", self.config.search_result_limit)
        self.config.search_timeout_seconds = safe_float(
            payload,
            "search_timeout_seconds",
            self.config.search_timeout_seconds,
        )
        self.config.summarization_mode = bool(payload.get("summarization_mode", self.config.summarization_mode))
        self.config.member_auto_sync = bool(payload.get("member_auto_sync", self.config.member_auto_sync))
        self.config.knowledge_auto_extract = bool(payload.get("knowledge_auto_extract", self.config.knowledge_auto_extract))
        self.config.knowledge_auto_extract_limit = safe_int(
            payload,
            "knowledge_auto_extract_limit",
            self.config.knowledge_auto_extract_limit,
        )
        self.config.event_mode = bool(payload.get("event_mode", self.config.event_mode))
        self.config.event_buffer_limit = safe_int(payload, "event_buffer_limit", self.config.event_buffer_limit)
        self.config.value_game_mode = bool(payload.get("value_game_mode", self.config.value_game_mode))
        self.config.value_game_reply_bonus = safe_float(
            payload,
            "value_game_reply_bonus",
            self.config.value_game_reply_bonus,
        )
        self.config.proactive_mode = bool(payload.get("proactive_mode", self.config.proactive_mode))
        self.config.proactive_inactive_hours = safe_float(
            payload,
            "proactive_inactive_hours",
            self.config.proactive_inactive_hours,
        )
        self.config.proactive_candidate_limit = safe_int(
            payload,
            "proactive_candidate_limit",
            self.config.proactive_candidate_limit,
        )
        self.config.proactive_min_affinity = safe_float(
            payload,
            "proactive_min_affinity",
            self.config.proactive_min_affinity,
        )
        self.config.max_active_skills = safe_int(payload, "max_active_skills", self.config.max_active_skills)
        self.config.history_window_messages = safe_int(
            payload,
            "history_window_messages",
            self.config.history_window_messages,
        )
        self.config.memory_recall_limit = safe_int(payload, "memory_recall_limit", self.config.memory_recall_limit)
        self.config.short_term_memory_limit = safe_int(
            payload,
            "short_term_memory_limit",
            self.config.short_term_memory_limit,
        )
        self.config.memory_summary_batch_size = safe_int(
            payload,
            "memory_summary_batch_size",
            self.config.memory_summary_batch_size,
        )
        self.refresh_runtime_components()
        self.persist_config()
        return self.get_abilities_panel()
