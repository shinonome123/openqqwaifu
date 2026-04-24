from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ..models import InboundEvent, MessageSegment, SessionMemory

if TYPE_CHECKING:
    from ..app import WaifuService


class MemoryAdminService:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def list_sessions(self, limit: int = 24) -> list[dict[str, object]]:
        svc = self.service
        current_character = svc._active_character_id()
        sessions = [
            session
            for session in svc.session_repository.list_sessions()
            if str(getattr(session, "character_id", "") or current_character).strip() == current_character
        ]
        result: list[dict[str, object]] = []
        for session in sessions[-limit:][::-1]:
            history = list(session.history)
            card = svc.cards.load(session.launcher_type, session)
            active_skill_names = self._active_skill_names(session)
            last_search_query = self._last_search_query(session)
            result.append(
                {
                    "character_id": current_character,
                    "launcher_id": session.launcher_id,
                    "launcher_type": session.launcher_type,
                    "preferred_name": svc.member_service.session_preferred_name(session),
                    "assistant_alias": svc.member_service.session_assistant_alias(session),
                    "assistant_name": card.assistant_name,
                    "history_count": len(history),
                    "message_count": len(history),
                    "long_term_count": svc.knowledge._knowledge_count_for_session(session),
                    "active_skill_count": len(active_skill_names),
                    "active_skill_names": active_skill_names,
                    "last_search_query": last_search_query,
                    "last_line": history[-1] if history else "",
                    "group_member_count": self._count_group_members(session),
                }
            )
        return result

    def get_session_detail(self, launcher_type: str, launcher_id: str) -> dict[str, object] | None:
        svc = self.service
        current_character = svc._active_character_id()
        session = svc.memory.load(launcher_id, launcher_type, character_id=current_character)
        clean_metadata = self._sanitize_session_metadata(session.metadata)
        if not session.history and not clean_metadata:
            return None
        return {
            "character_id": str(session.character_id or current_character).strip(),
            "launcher_id": session.launcher_id,
            "launcher_type": session.launcher_type,
            "preferred_name": svc.member_service.session_preferred_name(session),
            "assistant_alias": svc.member_service.session_assistant_alias(session),
            "history": list(session.history),
            "metadata": clean_metadata,
            "memory_graph": self._session_detail_graph(session),
        }

    def get_memory_panel(self) -> dict[str, object]:
        svc = self.service
        current_character = svc._active_character_id()
        return {
            "character_id": current_character,
            "sessions": self.list_sessions(limit=60),
            "knowledge_entries": svc.knowledge_repository.list_knowledge(limit=80, character_id=current_character),
            "knowledge_count": svc.knowledge_repository.knowledge_count(character_id=current_character),
            "embedded_knowledge_count": svc.knowledge_repository.embedded_knowledge_count(character_id=current_character),
            "member_count": svc.member_repository.member_count(),
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
        svc.session_repository.save(session)
        return self.get_session_detail(launcher_type, launcher_id)

    def save_knowledge_entry(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.knowledge_repository.save_knowledge_for_active(payload)

    def delete_knowledge_entry(self, entry_id: int) -> bool:
        return self.service.knowledge_repository.delete_knowledge_for_active(entry_id)

    def _session_detail_graph(self, session: SessionMemory) -> dict[str, Any]:
        svc = self.service
        launcher_id = str(session.launcher_id or "").strip()
        launcher_type = str(session.launcher_type or "").strip()
        character_id = str(session.character_id or svc._active_character_id()).strip()
        if launcher_type == "person":
            member = svc.member_repository.get_member(
                group_id="",
                user_id=launcher_id,
                character_id=character_id,
            )
            sender_id = launcher_id
            sender_name = str((member or {}).get("preferred_name") or (member or {}).get("qq_nickname") or launcher_id)
        else:
            recent_behavior = svc.get_behavior_events(limit=1, launcher_type=launcher_type, launcher_id=launcher_id)
            sender_id = str(recent_behavior[0].get("sender_id", "") or "") if recent_behavior else ""
            member = (
                svc.member_repository.get_member(
                    group_id=launcher_id,
                    user_id=sender_id,
                    character_id=character_id,
                )
                if sender_id
                else None
            )
            sender_name = str((member or {}).get("preferred_name") or (member or {}).get("qq_nickname") or launcher_id)
        detail_event = InboundEvent(
            launcher_id=launcher_id,
            launcher_type=launcher_type if launcher_type in {"group", "person"} else "person",
            sender_id=sender_id,
            sender_name=sender_name,
            segments=[MessageSegment(kind="text", text=session.history[-1] if session.history else "")],
        )
        knowledge_entries = svc.knowledge._detail_knowledge_entries(session)
        behavior_events = svc.get_behavior_events(
            limit=8,
            launcher_type=launcher_type,
            launcher_id=launcher_id,
            character_id=str(session.character_id or svc._active_character_id()).strip(),
        )
        return svc.memory_graph.build(
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

    def _count_group_members(self, session: SessionMemory) -> int:
        if session.launcher_type != "group":
            return 0
        return self.service.member_repository.count_members_in_group(session.launcher_id)
