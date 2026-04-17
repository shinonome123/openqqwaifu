"""Assistant-persona bookkeeping and member-profile sanitization.

Extracted from :mod:`waifu_standalone.app`. ``PersonaGuard`` owns the
data flow around *who is the AI called right now* and *what residual
persona text must be stripped from stored member summaries and session
history*. It is called during normal event handling (to filter history
from other characters) and from ``LegacyMigrator`` (to repair saved
state after a character switch).

The class stays thin - it borrows ``cards``, ``config``, ``memory``,
``state_store`` from the owning service rather than copying state.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .models import InboundEvent, SessionMemory

if TYPE_CHECKING:
    from .app import WaifuService


class PersonaGuard:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    def known_aliases(self) -> dict[str, set[str]]:
        aliases: dict[str, set[str]] = {}
        try:
            for item in self._service.cards.list_characters():
                character_id = str(item.get("character", "") or "").strip()
                if not character_id:
                    continue
                names = aliases.setdefault(character_id, set())
                if bool(item.get("has_person")):
                    try:
                        names.add(
                            str(
                                self._service.cards.load(
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
                                self._service.cards.load(
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
        current_character = self._service._active_character_id()
        current_names = aliases.setdefault(current_character, set())
        for launcher_type in ("person", "group"):
            try:
                current_names.add(
                    str(
                        self._service.cards.load(
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
        fallback = str(self._service.config.assistant_name or "").strip()
        if fallback:
            current_names.add(fallback)
        return {
            character_id: {name for name in names if name}
            for character_id, names in aliases.items()
            if any(name for name in names)
        }

    def known_names(self) -> dict[str, str]:
        aliases = self.known_aliases()
        return {
            character_id: sorted(names, key=lambda item: (len(item), item.casefold()))[0]
            for character_id, names in aliases.items()
            if names
        }

    @staticmethod
    def mentions_any_name(text: str, names: set[str]) -> bool:
        lowered = str(text or "").casefold()
        return any(name and name.casefold() in lowered for name in names)

    def sanitize_profile_summary(self, summary: str) -> str:
        fragments: list[str] = []
        seen: set[str] = set()
        assistant_names: set[str] = set()
        for names in self.known_aliases().values():
            assistant_names.update(names)
        for fragment in str(summary or "").split(";"):
            cleaned = " ".join(fragment.split()).strip().strip(",")
            if not cleaned:
                continue
            if assistant_names and self.mentions_any_name(cleaned, assistant_names):
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            fragments.append(cleaned)
        return "; ".join(fragments[:3])

    def sanitize_session_state(
        self,
        session: SessionMemory,
        *,
        assistant_name: str,
    ) -> SessionMemory:
        known_aliases = self.known_aliases()
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
            speaker, content = self._service._split_history_line(raw_line)
            speaker_is_assistant = speaker == "assistant" or self.mentions_any_name(speaker, other_names)
            if speaker_is_assistant and (
                self.mentions_any_name(speaker, other_names)
                or self.mentions_any_name(content, other_names)
            ):
                changed = True
                continue
            filtered_history.append(raw_line)
        if not changed:
            return session
        session.history = filtered_history
        self._service.memory.store.save(session)
        return session

    async def asanitize_session_state(
        self,
        session: SessionMemory,
        *,
        assistant_name: str,
    ) -> SessionMemory:
        return await asyncio.to_thread(self.sanitize_session_state, session, assistant_name=assistant_name)

    def sanitize_member_state(
        self,
        *,
        group_id: str,
        user_id: str,
        character_id: str,
    ) -> dict[str, object] | None:
        member = self._service.state_store.get_member(
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )
        if member is None:
            return None
        cleaned_summary = self.sanitize_profile_summary(str(member.get("profile_summary", "") or ""))
        if cleaned_summary == str(member.get("profile_summary", "") or ""):
            return member
        return self._service.state_store.save_member(
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

    async def asanitize_member_state(
        self,
        *,
        group_id: str,
        user_id: str,
        character_id: str,
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(
            self.sanitize_member_state,
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )

    def update_member_profile_summary(
        self,
        event: InboundEvent,
        *,
        extra_summary: str = "",
    ) -> None:
        group_id = event.launcher_id if event.launcher_type == "group" else ""
        current_character = self._service._active_character_id()
        member = self.sanitize_member_state(
            group_id=group_id,
            user_id=event.sender_id,
            character_id=current_character,
        )
        if member is None:
            return
        scope_id = f"{event.launcher_id}:{event.sender_id}" if event.launcher_type == "group" else event.sender_id
        notes_count = 0
        count_scope = getattr(self._service.state_store, "count_knowledge_for_scope", None)
        if callable(count_scope):
            notes_count = int(count_scope("member", scope_id, character_id=current_character) or 0)
        merged_summary = self.merge_profile_summary(
            self.sanitize_profile_summary(str(member.get("profile_summary", "") or "")),
            self.sanitize_profile_summary(extra_summary),
        )
        self._service.state_store.save_member(
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
    def merge_profile_summary(existing: str, addition: str) -> str:
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
