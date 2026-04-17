from __future__ import annotations

import time
from copy import deepcopy
from typing import TYPE_CHECKING

from .cells.config import serialize_app_config
from .gateways.napcat_login import NapCatLoginError, normalize_webui_settings, qrcode_payload_to_image_source
from .gateways.onebot_actions import OneBotActionClient
from .models import InboundEvent, MessageSegment, SessionMemory

if TYPE_CHECKING:
    from .app import WaifuService


_MASK_SENTINEL = "..."


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "***" if key else ""
    return key[:4] + "..." + key[-4:]


def _is_masked(value: str) -> bool:
    return value == "***" or _MASK_SENTINEL in value


def _safe_int(payload: dict[str, object], key: str, default: int) -> int:
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _safe_float(payload: dict[str, object], key: str, default: float) -> float:
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class ConsolePanels:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def dashboard_snapshot(self) -> dict[str, object]:
        svc = self.service
        current_character = svc._active_character_id()
        sessions = svc.list_sessions(limit=24)
        knowledge_count = svc.state_store.knowledge_count(character_id=current_character)
        member_count = svc.state_store.member_count()
        proactive_candidates = svc.proactive.list_candidates(
            members=svc.state_store.list_members(limit=200, character_id=current_character),
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
            "narrator_mode": svc.config.narrator_mode,
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
                "dry_run": False,
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
        svc = self.service
        target = str(character or svc.cards.active_character()).strip() or "default"
        bundle = svc.cards.get_editor_bundle(target)
        return {
            "current_character": svc.cards.active_character(),
            **bundle,
        }

    def save_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        character = str(payload.get("character") or svc.config.character or "default").strip() or "default"
        set_active = bool(payload.get("set_active", True))
        person = str(payload.get("person_content") or "")
        group = str(payload.get("group_content") or "")
        person_fields = payload.get("person_fields")
        group_fields = payload.get("group_fields")
        shared_fields = payload.get("shared_fields")
        portrait = payload.get("portrait")
        bundle = svc.cards.save_editor_bundle(
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
            svc.activate_character(character, reset_sessions=True)
        svc._persist_config()
        return {
            "current_character": svc.cards.active_character(),
            **bundle,
        }

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.service.cards.load_portrait_asset(character)

    def preview_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
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
        card = svc.cards.build_preview_card(
            shared_fields=shared_fields if isinstance(shared_fields, dict) else None,
            variant_fields=variant_fields if isinstance(variant_fields, dict) else None,
        )

        user_name = str(payload.get("user_name") or card.user_name or "User").strip() or card.user_name or "User"
        history = svc._normalize_preview_history(payload.get("history"), assistant_name=card.assistant_name)
        session = SessionMemory(
            launcher_id=f"preview-{launcher_type}",
            launcher_type=launcher_type,
            history=svc._preview_history_lines(history),
            metadata={},
        )
        event = InboundEvent(
            launcher_id=session.launcher_id,
            launcher_type=launcher_type,
            sender_id="preview-user",
            sender_name=user_name,
            segments=[MessageSegment(kind="text", text=message)],
        )
        emotion = svc.emotions.analyze(event, session)
        conversation_view = svc._preview_conversation_view(
            session.history,
            assistant_name=card.assistant_name,
            limit=svc.config.history_window_messages,
        )
        analysis_hint = svc.generator.generate_analysis(
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
        reply_text = svc.generator.generate_reply(
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
            "llm_ready": svc.generator.llm_ready,
        }

    def _generate_character_portrait(
        self,
        character: str,
        bundle: dict[str, object],
        portrait_payload: dict[str, object],
    ) -> dict[str, object]:
        svc = self.service
        current = dict(bundle.get("portrait", {}) if isinstance(bundle.get("portrait"), dict) else {})
        shared_fields = bundle.get("shared", {}) if isinstance(bundle.get("shared"), dict) else {}
        person = bundle.get("person", {}) if isinstance(bundle.get("person"), dict) else {}
        group = bundle.get("group", {}) if isinstance(bundle.get("group"), dict) else {}
        person_fields = person.get("fields", {}) if isinstance(person.get("fields"), dict) else {}
        group_fields = group.get("fields", {}) if isinstance(group.get("fields"), dict) else {}
        style = str(portrait_payload.get("style") or current.get("style") or "neon-pixel")
        prompt_suffix = str(portrait_payload.get("prompt_suffix") or current.get("prompt_suffix") or "")
        auto_generate = bool(portrait_payload.get("auto_generate", current.get("auto_generate", True)))
        prompt = svc.cards.build_portrait_prompt(
            character,
            shared_fields=shared_fields,
            person_fields=person_fields,
            group_fields=group_fields,
            portrait={"style": style, "prompt_suffix": prompt_suffix},
        )
        if not svc.generator.image_ready:
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

    def get_ai_panel(self) -> dict[str, object]:
        svc = self.service
        panel = {
            "llm": deepcopy(serialize_app_config(svc.config)["llm"]),
            "image_generation": deepcopy(serialize_app_config(svc.config)["image_generation"]),
            "embedding": deepcopy(serialize_app_config(svc.config)["embedding"]),
        }
        for section in ("llm", "image_generation", "embedding"):
            sub = panel.get(section)
            if isinstance(sub, dict) and "api_key" in sub:
                sub["api_key"] = _mask_key(str(sub["api_key"] or ""))
        return panel

    def save_ai_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        llm = payload.get("llm", {})
        image_generation = payload.get("image_generation", {})
        embedding = payload.get("embedding", {})
        if isinstance(llm, dict):
            svc.config.llm.enabled = bool(llm.get("enabled", svc.config.llm.enabled))
            svc.config.llm.backend = str(llm.get("backend", svc.config.llm.backend) or svc.config.llm.backend)
            svc.config.llm.base_url = str(llm.get("base_url", svc.config.llm.base_url) or "")
            _llm_key = str(llm.get("api_key", "") or "")
            if _llm_key and not _is_masked(_llm_key):
                svc.config.llm.api_key = _llm_key
            svc.config.llm.model = str(llm.get("model", svc.config.llm.model) or "")
            svc.config.llm.app_type = str(llm.get("app_type", svc.config.llm.app_type) or svc.config.llm.app_type)
            svc.config.llm.timeout_seconds = _safe_float(llm, "timeout_seconds", svc.config.llm.timeout_seconds)
        if isinstance(image_generation, dict):
            svc.config.image_generation.enabled = bool(image_generation.get("enabled", svc.config.image_generation.enabled))
            svc.config.image_generation.base_url = str(image_generation.get("base_url", svc.config.image_generation.base_url) or "")
            _img_key = str(image_generation.get("api_key", "") or "")
            if _img_key and not _is_masked(_img_key):
                svc.config.image_generation.api_key = _img_key
            svc.config.image_generation.model = str(image_generation.get("model", svc.config.image_generation.model) or svc.config.image_generation.model)
            svc.config.image_generation.timeout_seconds = _safe_float(image_generation, "timeout_seconds", svc.config.image_generation.timeout_seconds)
            svc.config.image_generation.response_format = str(image_generation.get("response_format", svc.config.image_generation.response_format) or svc.config.image_generation.response_format)
            svc.config.image_generation.aspect_ratio = str(image_generation.get("aspect_ratio", svc.config.image_generation.aspect_ratio) or svc.config.image_generation.aspect_ratio)
            svc.config.image_generation.resolution = str(image_generation.get("resolution", svc.config.image_generation.resolution) or "")
        if isinstance(embedding, dict):
            svc.config.embedding.enabled = bool(embedding.get("enabled", svc.config.embedding.enabled))
            svc.config.embedding.backend = str(embedding.get("backend", svc.config.embedding.backend) or svc.config.embedding.backend)
            svc.config.embedding.base_url = str(embedding.get("base_url", svc.config.embedding.base_url) or "")
            _emb_key = str(embedding.get("api_key", "") or "")
            if _emb_key and not _is_masked(_emb_key):
                svc.config.embedding.api_key = _emb_key
            svc.config.embedding.model = str(embedding.get("model", svc.config.embedding.model) or svc.config.embedding.model)
            svc.config.embedding.timeout_seconds = _safe_float(embedding, "timeout_seconds", svc.config.embedding.timeout_seconds)
        svc._refresh_runtime_components(rebuild_generator=True)
        svc._persist_config()
        return self.get_ai_panel()

    def get_memory_panel(self) -> dict[str, object]:
        svc = self.service
        sessions = svc.list_sessions(limit=60)
        current_character = svc._active_character_id()
        knowledge_entries = svc.state_store.list_knowledge(limit=80, character_id=current_character)
        return {
            "character_id": current_character,
            "sessions": sessions,
            "knowledge_entries": knowledge_entries,
            "knowledge_count": svc.state_store.knowledge_count(character_id=current_character),
            "embedded_knowledge_count": svc.state_store.embedded_knowledge_count(character_id=current_character),
            "member_count": svc.state_store.member_count(),
            "behavior_event_count": len(svc.get_behavior_events(limit=200)),
        }

    def save_memory_session(
        self,
        launcher_type: str,
        launcher_id: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        svc = self.service
        current_character = svc._active_character_id()
        session = svc.memory.load(launcher_id, launcher_type, character_id=current_character)
        if not session.history and not svc._sanitize_session_metadata(session.metadata):
            return None
        session.preferred_name = ""
        history_value = payload.get("history", session.history)
        if isinstance(history_value, list):
            session.history = [str(item) for item in history_value]
        elif isinstance(history_value, str):
            session.history = [line for line in history_value.splitlines() if line.strip()]
        metadata_value = payload.get("metadata", session.metadata)
        if isinstance(metadata_value, dict):
            session.metadata = svc._sanitize_session_metadata(metadata_value)
        svc.memory.store.save(session)
        return svc.get_session_detail(launcher_type, launcher_id)

    def get_member_directory_panel(self, *, limit: int = 120) -> dict[str, object]:
        svc = self.service
        current_character = svc._active_character_id()
        return {
            "character_id": current_character,
            "members": svc.state_store.list_members(limit=limit, character_id=current_character),
            "member_count": svc.state_store.member_count(),
            "proactive_enabled": svc.config.proactive_mode,
        }

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        next_payload = dict(payload)
        next_payload["character_id"] = self.service._active_character_id()
        return self.service.state_store.save_member(next_payload)

    def reset_directory_member_persona(self, payload: dict[str, object]) -> dict[str, object] | None:
        svc = self.service
        return svc.state_store.reset_member_persona(
            group_id=payload.get("group_id"),
            user_id=payload.get("user_id"),
            character_id=svc._active_character_id(),
        )

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        next_payload = dict(payload)
        next_payload["character_id"] = self.service._active_character_id()
        return self.service.state_store.save_knowledge(next_payload)

    def delete_knowledge_entry(self, entry_id: int) -> bool:
        svc = self.service
        return bool(
            svc.state_store.delete_knowledge(
                entry_id,
                character_id=svc._active_character_id(),
            )
        )

    def get_abilities_panel(self) -> dict[str, object]:
        svc = self.service
        return {
            "search_enabled": svc.config.search_enabled,
            "search_result_limit": svc.config.search_result_limit,
            "search_timeout_seconds": svc.config.search_timeout_seconds,
            "thinking_mode": svc.config.thinking_mode,
            "conversation_analysis": svc.config.conversation_analysis,
            "summarization_mode": svc.config.summarization_mode,
            "member_auto_sync": svc.config.member_auto_sync,
            "knowledge_auto_extract": svc.config.knowledge_auto_extract,
            "knowledge_auto_extract_limit": svc.config.knowledge_auto_extract_limit,
            "event_mode": svc.config.event_mode,
            "event_buffer_limit": svc.config.event_buffer_limit,
            "narrator_mode": svc.config.narrator_mode,
            "narrator_style": svc.config.narrator_style,
            "narrator_detail_level": svc.config.narrator_detail_level,
            "value_game_mode": svc.config.value_game_mode,
            "value_game_reply_bonus": svc.config.value_game_reply_bonus,
            "memory_graph_mode": svc.config.memory_graph_mode,
            "memory_graph_limit": svc.config.memory_graph_limit,
            "proactive_mode": svc.config.proactive_mode,
            "proactive_inactive_hours": svc.config.proactive_inactive_hours,
            "proactive_candidate_limit": svc.config.proactive_candidate_limit,
            "proactive_min_affinity": svc.config.proactive_min_affinity,
            "max_active_skills": svc.config.max_active_skills,
            "history_window_messages": svc.config.history_window_messages,
            "memory_recall_limit": svc.config.memory_recall_limit,
            "max_thinking_words": svc.config.max_thinking_words,
            "short_term_memory_limit": svc.config.short_term_memory_limit,
            "memory_summary_batch_size": svc.config.memory_summary_batch_size,
            "tools": svc.list_tools(),
            "marketplace": svc.marketplace.describe(),
        }

    def save_abilities_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        svc.config.search_enabled = bool(payload.get("search_enabled", svc.config.search_enabled))
        svc.config.search_result_limit = _safe_int(payload, "search_result_limit", svc.config.search_result_limit)
        svc.config.search_timeout_seconds = _safe_float(payload, "search_timeout_seconds", svc.config.search_timeout_seconds)
        svc.config.thinking_mode = bool(payload.get("thinking_mode", svc.config.thinking_mode))
        svc.config.conversation_analysis = bool(payload.get("conversation_analysis", svc.config.conversation_analysis))
        svc.config.summarization_mode = bool(payload.get("summarization_mode", svc.config.summarization_mode))
        svc.config.member_auto_sync = bool(payload.get("member_auto_sync", svc.config.member_auto_sync))
        svc.config.knowledge_auto_extract = bool(payload.get("knowledge_auto_extract", svc.config.knowledge_auto_extract))
        svc.config.knowledge_auto_extract_limit = _safe_int(
            payload,
            "knowledge_auto_extract_limit",
            svc.config.knowledge_auto_extract_limit,
        )
        svc.config.event_mode = bool(payload.get("event_mode", svc.config.event_mode))
        svc.config.event_buffer_limit = _safe_int(payload, "event_buffer_limit", svc.config.event_buffer_limit)
        svc.config.narrator_mode = bool(payload.get("narrator_mode", svc.config.narrator_mode))
        svc.config.narrator_style = str(payload.get("narrator_style", svc.config.narrator_style) or svc.config.narrator_style)
        svc.config.narrator_detail_level = _safe_int(payload, "narrator_detail_level", svc.config.narrator_detail_level)
        svc.config.value_game_mode = bool(payload.get("value_game_mode", svc.config.value_game_mode))
        svc.config.value_game_reply_bonus = _safe_float(payload, "value_game_reply_bonus", svc.config.value_game_reply_bonus)
        svc.config.memory_graph_mode = bool(payload.get("memory_graph_mode", svc.config.memory_graph_mode))
        svc.config.memory_graph_limit = _safe_int(payload, "memory_graph_limit", svc.config.memory_graph_limit)
        svc.config.proactive_mode = bool(payload.get("proactive_mode", svc.config.proactive_mode))
        svc.config.proactive_inactive_hours = _safe_float(payload, "proactive_inactive_hours", svc.config.proactive_inactive_hours)
        svc.config.proactive_candidate_limit = _safe_int(payload, "proactive_candidate_limit", svc.config.proactive_candidate_limit)
        svc.config.proactive_min_affinity = _safe_float(payload, "proactive_min_affinity", svc.config.proactive_min_affinity)
        svc.config.max_active_skills = _safe_int(payload, "max_active_skills", svc.config.max_active_skills)
        svc.config.history_window_messages = _safe_int(payload, "history_window_messages", svc.config.history_window_messages)
        svc.config.memory_recall_limit = _safe_int(payload, "memory_recall_limit", svc.config.memory_recall_limit)
        svc.config.max_thinking_words = _safe_int(payload, "max_thinking_words", svc.config.max_thinking_words)
        svc.config.short_term_memory_limit = _safe_int(payload, "short_term_memory_limit", svc.config.short_term_memory_limit)
        svc.config.memory_summary_batch_size = _safe_int(payload, "memory_summary_batch_size", svc.config.memory_summary_batch_size)
        svc._refresh_runtime_components()
        svc._persist_config()
        return self.get_abilities_panel()

    def get_proactive_panel(self, *, limit: int = 12) -> dict[str, object]:
        svc = self.service
        current_character = svc._active_character_id()
        members = svc.state_store.list_members(limit=max(120, limit * 8), character_id=current_character)
        candidates = svc.proactive.list_candidates(members=members, limit=limit)
        return {
            "character_id": current_character,
            "enabled": svc.config.proactive_mode,
            "inactive_hours": svc.config.proactive_inactive_hours,
            "candidate_limit": svc.config.proactive_candidate_limit,
            "candidates": candidates,
        }

    def generate_proactive_draft(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        member = svc.state_store.get_member(group_id=group_id, user_id=user_id, character_id=svc._active_character_id())
        if member is None:
            raise ValueError("member not found")
        relationship_hint = str(payload.get("relationship_hint", "") or "").strip()
        draft = svc.proactive.build_draft(
            member=member,
            assistant_name=svc.config.assistant_name,
            relationship_hint=relationship_hint,
        )
        return {"status": "ok", "draft": draft}

    def get_skills_panel(self) -> dict[str, object]:
        svc = self.service
        return {
            "skills": svc.list_skills(),
            "tools": svc.list_tools(),
            "marketplace": svc.marketplace.describe(),
        }

    def search_marketplace(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        return self.service.marketplace.search(query, source_id=source_id, limit=limit)

    def import_marketplace_skill(
        self,
        *,
        source_id: str,
        github_url: str,
    ) -> dict[str, object]:
        svc = self.service
        payload = svc.marketplace.fetch_skill_markdown(source_id, github_url)
        skill = svc.install_skill(payload["markdown"], filename=payload["filename"])
        skill["raw_url"] = payload["raw_url"]
        return skill

    def get_sidecar_panel(self, *, refresh: bool = False) -> dict[str, object]:
        svc = self.service
        status: dict[str, object] = {
            "mode": "offline",
            "adapter_name": svc.config.qq_sidecar.adapter_name,
            "outbound_base_url": svc.config.qq_sidecar.outbound_base_url,
            "access_token": _mask_key(svc.config.qq_sidecar.access_token),
            "inbound_host": svc.config.qq_sidecar.inbound_host,
            "inbound_port": svc.config.qq_sidecar.inbound_port,
            "webui_base_url": svc.config.qq_sidecar.webui_base_url,
            "webui_api_prefix": svc.config.qq_sidecar.webui_api_prefix,
            "webui_timeout_seconds": svc.config.qq_sidecar.webui_timeout_seconds,
            "webui_token": _mask_key(svc.config.qq_sidecar.webui_token),
            "webui_url": svc.napcat_login.webui_url() if svc.napcat_login is not None else "",
            "reverse_ws_url": svc.config.qq_sidecar.reverse_ws_url,
            "outbound_timeout_seconds": svc.config.qq_sidecar.outbound_timeout_seconds,
            "dry_run": False,
            "llm_ready": svc.generator.llm_ready,
            "details": {},
        }
        if svc.config.qq_sidecar.outbound_base_url:
            status["mode"] = "configured"
        if refresh and svc.config.qq_sidecar.outbound_base_url:
            client = OneBotActionClient(
                base_url=svc.config.qq_sidecar.outbound_base_url,
                timeout=svc.config.qq_sidecar.outbound_timeout_seconds,
                access_token=svc.config.qq_sidecar.access_token,
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
        if svc.napcat_login is not None:
            qq_login = svc.napcat_login.panel(refresh=refresh)
            status["qq_login"] = qq_login
            if refresh:
                svc._adopt_login_info(qq_login.get("login_info"))
        return status

    def save_sidecar_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        self._apply_sidecar_payload(payload)
        svc._refresh_runtime_components(rebuild_outbound=True)
        svc._persist_config()
        return self.get_sidecar_panel(refresh=False)

    def get_qq_login_panel(self, *, refresh: bool = False) -> dict[str, object]:
        svc = self.service
        panel = {
            "configured": bool(str(svc.config.qq_sidecar.webui_base_url or "").strip()),
            "token_configured": bool(str(svc.config.qq_sidecar.webui_token or "").strip()),
            "webui_base_url": svc.config.qq_sidecar.webui_base_url,
            "webui_api_prefix": svc.config.qq_sidecar.webui_api_prefix,
            "webui_timeout_seconds": svc.config.qq_sidecar.webui_timeout_seconds,
            "webui_token": _mask_key(svc.config.qq_sidecar.webui_token),
            "webui_url": svc.napcat_login.webui_url() if svc.napcat_login is not None else "",
            "status": {
                "is_login": False,
                "is_offline": False,
                "qrcode_url": "",
                "login_error": "",
            },
            "login_info": {},
        }
        if svc.napcat_login is None:
            return panel
        bridge_panel = svc.napcat_login.panel(refresh=refresh)
        panel.update(bridge_panel)
        if refresh:
            svc._adopt_login_info(bridge_panel.get("login_info"))
        return panel

    def save_qq_login_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        self._apply_sidecar_payload(payload)
        svc._refresh_runtime_components(rebuild_outbound=False)
        svc._persist_config()
        return self.get_qq_login_panel(refresh=False)

    def refresh_qq_login_panel(self) -> dict[str, object]:
        svc = self.service
        if svc.napcat_login is None:
            raise ValueError("NapCat QQ login bridge is unavailable")
        try:
            svc.napcat_login.refresh_qrcode()
        except NapCatLoginError as exc:
            raise ValueError(str(exc)) from exc
        return self.get_qq_login_panel(refresh=True)

    def get_qq_login_qrcode_image(self) -> tuple[bytes, str] | None:
        svc = self.service
        if svc.napcat_login is None:
            return None
        try:
            payload = svc.napcat_login.qrcode_payload()
            content_type, body = qrcode_payload_to_image_source(payload)
        except NapCatLoginError as exc:
            raise ValueError(str(exc)) from exc
        return body, content_type

    def _apply_sidecar_payload(self, payload: dict[str, object]) -> None:
        svc = self.service
        svc.config.qq_sidecar.adapter_name = str(
            payload.get("adapter_name", svc.config.qq_sidecar.adapter_name)
            or svc.config.qq_sidecar.adapter_name
        )
        svc.config.qq_sidecar.outbound_base_url = str(
            payload.get("outbound_base_url", svc.config.qq_sidecar.outbound_base_url) or ""
        )
        svc.config.qq_sidecar.outbound_timeout_seconds = _safe_float(
            payload, "outbound_timeout_seconds", svc.config.qq_sidecar.outbound_timeout_seconds
        )
        _access_token_raw = str(payload.get("access_token", "") or "")
        if _access_token_raw and not _is_masked(_access_token_raw):
            svc.config.qq_sidecar.access_token = _access_token_raw
        svc.config.qq_sidecar.inbound_host = str(
            payload.get("inbound_host", svc.config.qq_sidecar.inbound_host)
            or svc.config.qq_sidecar.inbound_host
        )
        svc.config.qq_sidecar.inbound_port = _safe_int(
            payload, "inbound_port", svc.config.qq_sidecar.inbound_port
        )
        webui_base_raw = str(
            payload.get("webui_base_url", svc.config.qq_sidecar.webui_base_url)
            or ""
        )
        webui_api_prefix = payload.get("webui_api_prefix", svc.config.qq_sidecar.webui_api_prefix)
        next_webui_api_prefix = str(
            svc.config.qq_sidecar.webui_api_prefix if webui_api_prefix is None else webui_api_prefix
        ).strip()
        svc.config.qq_sidecar.webui_api_prefix = next_webui_api_prefix or "/api"
        svc.config.qq_sidecar.webui_timeout_seconds = _safe_float(
            payload, "webui_timeout_seconds", svc.config.qq_sidecar.webui_timeout_seconds
        )
        webui_token_raw = str(payload.get("webui_token", "") or "")
        if _is_masked(webui_token_raw):
            webui_token_raw = svc.config.qq_sidecar.webui_token
        normalized_webui_base, normalized_webui_token = normalize_webui_settings(
            webui_base_raw,
            webui_token_raw,
        )
        svc.config.qq_sidecar.webui_base_url = normalized_webui_base
        svc.config.qq_sidecar.webui_token = normalized_webui_token
        svc.config.qq_sidecar.reverse_ws_url = str(
            payload.get("reverse_ws_url", svc.config.qq_sidecar.reverse_ws_url)
            or svc.config.qq_sidecar.reverse_ws_url
        )
        svc.config.qq_sidecar.dry_run = False

    def get_other_panel(self) -> dict[str, object]:
        svc = self.service
        return {
            "service_name": svc.config.service_name,
            "assistant_name": svc.config.assistant_name,
            "bot_account_id": svc.config.bot_account_id,
            "group_reply_requires_mention": svc.config.group_reply_requires_mention,
            "image_command_prefix": svc.config.image_command_prefix,
            "image_command_aliases": list(svc.config.image_command_aliases),
            "ignore_prefixes": list(svc.config.ignore_prefixes),
            "group_follow_up_window_seconds": svc.config.group_follow_up_window_seconds,
            "group_response_delay_seconds": svc.config.group_response_delay_seconds,
            "repeat_trigger_count": svc.config.repeat_trigger_count,
            "multimodal_enabled": svc.config.multimodal_enabled,
            "data_root": svc.config.data_root,
            "config_path": svc.config.config_path,
            "marketplace": deepcopy(serialize_app_config(svc.config)["marketplace"]),
        }

    def save_other_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        follow_up_raw = payload.get("group_follow_up_window_seconds", svc.config.group_follow_up_window_seconds)
        reply_delay_raw = payload.get("group_response_delay_seconds", svc.config.group_response_delay_seconds)
        repeat_trigger_raw = payload.get("repeat_trigger_count", svc.config.repeat_trigger_count)
        svc.config.service_name = str(payload.get("service_name", svc.config.service_name) or svc.config.service_name)
        svc.config.assistant_name = str(payload.get("assistant_name", svc.config.assistant_name) or svc.config.assistant_name)
        svc.config.bot_account_id = str(payload.get("bot_account_id", svc.config.bot_account_id) or "")
        svc.config.group_reply_requires_mention = bool(payload.get("group_reply_requires_mention", svc.config.group_reply_requires_mention))
        svc.config.image_command_prefix = str(payload.get("image_command_prefix", svc.config.image_command_prefix) or svc.config.image_command_prefix)
        svc.config.image_command_aliases = svc._coerce_list(payload.get("image_command_aliases", svc.config.image_command_aliases))
        svc.config.ignore_prefixes = svc._coerce_list(payload.get("ignore_prefixes", svc.config.ignore_prefixes))
        svc.config.group_follow_up_window_seconds = max(
            0.0,
            float(svc.config.group_follow_up_window_seconds if follow_up_raw is None else follow_up_raw),
        )
        svc.config.group_response_delay_seconds = max(
            0.0,
            float(svc.config.group_response_delay_seconds if reply_delay_raw is None else reply_delay_raw),
        )
        svc.config.repeat_trigger_count = max(
            0,
            int(svc.config.repeat_trigger_count if repeat_trigger_raw is None else repeat_trigger_raw),
        )
        svc.config.multimodal_enabled = bool(payload.get("multimodal_enabled", svc.config.multimodal_enabled))
        marketplace = payload.get("marketplace", {})
        if isinstance(marketplace, dict):
            svc.config.marketplace.enabled = bool(marketplace.get("enabled", svc.config.marketplace.enabled))
            svc.config.marketplace.default_query = str(marketplace.get("default_query", svc.config.marketplace.default_query) or svc.config.marketplace.default_query)
            sources = marketplace.get("sources")
            if isinstance(sources, list):
                next_sources = []
                for item in sources:
                    if not isinstance(item, dict):
                        continue
                    next_sources.append(type(svc.config.marketplace.sources[0])(
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
                    svc.config.marketplace.sources = next_sources
        svc._refresh_runtime_components()
        svc._persist_config()
        return self.get_other_panel()
        try:
            generated = svc.generator.generate_image(prompt)
            image_bytes, content_type = svc.generator.resolve_generated_image(generated.image_ref)
            portrait = svc.cards.save_portrait_asset(
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
