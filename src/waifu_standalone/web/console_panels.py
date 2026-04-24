from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import WaifuService

class ConsolePanels:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def dashboard_snapshot(self) -> dict[str, object]:
        svc = self.service
        current_character = svc._active_character_id()
        sessions = svc.list_sessions(limit=24)
        knowledge_count = svc.knowledge_repository.knowledge_count(character_id=current_character)
        member_count = svc.member_repository.member_count()
        proactive_candidates = svc.proactive.list_candidates(
            members=svc.member_repository.list_members(limit=200, character_id=current_character),
            limit=svc.config.proactive_candidate_limit,
        )
        with svc._state_lock:
            recent_outbound = [svc._message_to_dict(message) for message in svc._recent_outbound[-12:][::-1]]
            recent_behavior_events = [dict(item) for item in svc._recent_behavior_events[-12:][::-1]]
            active_launchers = svc.gate.active_launcher_ids_locked(
                current_character, now_monotonic=time.monotonic()
            )
        return {
            "service_name": svc.config.service_name,
            "assistant_name": svc.config.assistant_name,
            "character": current_character,
            "bot_account_id": svc.config.bot_account_id,
            "group_reply_requires_mention": svc.config.group_reply_requires_mention,
            "max_active_skills": svc.config.max_active_skills,
            "search_enabled": svc.config.search_enabled,
            "thinking_mode": svc.config.thinking_mode,
            "summarization_mode": svc.config.summarization_mode,
            "event_mode": svc.config.event_mode,
            "story_mode": svc.config.story_mode,
            "narrator_mode": svc.config.story_mode,
            "value_game_mode": svc.config.value_game_mode,
            "memory_graph_mode": svc.config.memory_graph_mode,
            "proactive_mode": svc.config.proactive_mode,
            "reply_window_seconds": svc.config.group_follow_up_window_seconds,
            "group_response_delay_seconds": svc.config.group_response_delay_seconds,
            "repeat_trigger_count": svc.config.repeat_trigger_count,
            "multimodal_enabled": svc.config.multimodal_enabled,
            "history_window_messages": svc.config.history_window_messages,
            "memory_recall_limit": svc.config.memory_recall_limit,
            "short_term_memory_limit": svc.config.short_term_memory_limit,
            "memory_summary_batch_size": svc.config.memory_summary_batch_size,
            "ignore_prefixes": list(svc.config.ignore_prefixes),
            "message_behavior": {
                "requires_mention": svc.config.group_reply_requires_mention,
                "follow_up_window_seconds": svc.config.group_follow_up_window_seconds,
                "response_delay_seconds": svc.config.group_response_delay_seconds,
                "repeat_trigger_count": svc.config.repeat_trigger_count,
                "multimodal_enabled": svc.config.multimodal_enabled,
            },
            "skills": svc.skills.describe(),
            "tools": svc.tools.describe(),
            "search": {
                "enabled": svc.config.search_enabled,
                "result_limit": svc.config.search_result_limit,
                "timeout_seconds": svc.config.search_timeout_seconds,
                "cache_size": svc.search.cache_size(),
            },
            "outbound_mode": svc._outbound_mode_label(),
            "llm": {
                "enabled": svc.config.llm.enabled,
                "backend": svc.config.llm.backend,
                "base_url": svc.config.llm.base_url,
                "ready": svc.generator.llm_ready,
            },
            "image_generation": {
                "enabled": svc.config.image_generation.enabled,
                "base_url": svc.config.image_generation.base_url,
                "model": svc.config.image_generation.model,
                "ready": svc.generator.image_ready,
            },
            "qq_sidecar": {
                "adapter_name": svc.config.qq_sidecar.adapter_name,
                "delivery_mode": "live" if svc.config.qq_sidecar.outbound_base_url else "offline_capture",
                "outbound_base_url": svc.config.qq_sidecar.outbound_base_url,
                "inbound_host": svc.config.qq_sidecar.inbound_host,
                "inbound_port": svc.config.qq_sidecar.inbound_port,
            },
            "skill_workspace": str(svc.skills.workspace_root),
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
        return self.service.character_service.get_character_panel(character)

    def save_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.character_service.save_character_panel(payload)

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.service.character_service.get_character_portrait(character)

    def preview_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.character_service.preview_character_panel(payload)

    def _generate_character_portrait(
        self,
        character: str,
        bundle: dict[str, object],
        portrait_payload: dict[str, object],
    ) -> dict[str, object]:
        return self.service.character_service.generate_character_portrait(
            character,
            bundle,
            portrait_payload,
        )

    def get_ai_panel(self) -> dict[str, object]:
        return self.service.settings_admin_service.get_ai_panel()

    def save_ai_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.settings_admin_service.save_ai_panel(payload)

    def get_memory_panel(self) -> dict[str, object]:
        return self.service.memory_admin_service.get_memory_panel()

    def save_memory_session(
        self,
        launcher_type: str,
        launcher_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        return self.service.memory_admin_service.save_memory_session(launcher_type, launcher_id, payload)

    def get_member_directory_panel(self, *, limit: int = 120) -> dict[str, object]:
        return self.service.member_service.get_member_directory_panel(limit=limit)

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.member_service.save_directory_member(payload)

    def reset_directory_member_persona(self, payload: dict[str, object]) -> dict[str, object] | None:
        return self.service.member_service.reset_directory_member_persona(payload)

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.memory_admin_service.save_knowledge_entry(payload)

    def delete_knowledge_entry(self, entry_id: int) -> bool:
        return self.service.memory_admin_service.delete_knowledge_entry(entry_id)

    def get_abilities_panel(self) -> dict[str, object]:
        return self.service.settings_admin_service.get_abilities_panel()

    def save_abilities_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.settings_admin_service.save_abilities_panel(payload)

    def get_proactive_panel(self, *, limit: int = 12) -> dict[str, object]:
        return self.service.member_service.get_proactive_panel(limit=limit)

    def generate_proactive_draft(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.member_service.generate_proactive_draft(payload)

    def get_skills_panel(self) -> dict[str, object]:
        return self.service.skills_admin_service.get_skills_panel()

    def search_marketplace(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        return self.service.skills_admin_service.search_marketplace(query, source_id=source_id, limit=limit)

    def import_marketplace_skill(
        self,
        *,
        source_id: str,
        github_url: str,
    ) -> dict[str, object]:
        return self.service.skills_admin_service.import_marketplace_skill(
            source_id=source_id,
            github_url=github_url,
        )

    def get_sidecar_panel(self, *, refresh: bool = False) -> dict[str, object]:
        return self.service.settings_admin_service.get_sidecar_panel(refresh=refresh)

    def save_sidecar_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.settings_admin_service.save_sidecar_panel(payload)

    def get_qq_login_panel(self, *, refresh: bool = False) -> dict[str, object]:
        return self.service.settings_admin_service.get_qq_login_panel(refresh=refresh)

    def save_qq_login_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.settings_admin_service.save_qq_login_panel(payload)

    def refresh_qq_login_panel(self) -> dict[str, object]:
        return self.service.settings_admin_service.refresh_qq_login_panel()

    def get_qq_login_qrcode_image(self) -> tuple[bytes, str] | None:
        return self.service.settings_admin_service.get_qq_login_qrcode_image()

    def _apply_sidecar_payload(self, payload: dict[str, object]) -> None:
        self.service.settings_admin_service._apply_sidecar_payload(payload)

    def get_other_panel(self) -> dict[str, object]:
        return self.service.settings_admin_service.get_other_panel()

    def save_other_panel(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.settings_admin_service.save_other_panel(payload)
