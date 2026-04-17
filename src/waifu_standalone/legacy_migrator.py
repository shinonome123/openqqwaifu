"""Legacy session-state migration and character-isolation repair.

Extracted from :mod:`waifu_standalone.app`. Handles one-time data
migrations: legacy per-session ``preferred_name``, ``group_members`` and
``long_term_memory`` fields are lifted into the current
``state_store`` schema, and persona state is sanitized when a character
is re-bound.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import WaifuService


class LegacyMigrator:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    def migrate_session_state(self) -> None:
        store = self._service.memory.store
        if not hasattr(store, "list_sessions"):
            return
        sessions = store.list_sessions()  # type: ignore[no-any-return]
        active_character = self._service._active_character_id()
        existing_keys = {
            self.knowledge_entry_key(entry)
            for entry in self._service.state_store.list_knowledge(
                limit=max(1, self._service.state_store.knowledge_count(character_id=active_character)),
                character_id=active_character,
            )
        }
        for session in sessions:
            changed = False
            member_user_id = session.launcher_id if session.launcher_type == "person" else ""
            legacy_preferred_name = str(session.preferred_name or "").strip()
            if legacy_preferred_name and session.launcher_type == "person":
                self._service.state_store.record_member_seen(
                    group_id="",
                    user_id=session.launcher_id,
                    qq_nickname=member_user_id,
                )
                self._service.state_store.save_member(
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
                for payload in self.iter_legacy_group_members(legacy_members):
                    user_id = str(payload.get("user_id", "") or "").strip()
                    if not user_id:
                        continue
                    self._service.state_store.record_member_seen(
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
                            "character_id": str(session.character_id or self._service._active_character_id()).strip(),
                            "qq_nickname": payload.get("qq_nickname", ""),
                            "group_card": payload.get("group_card", ""),
                            "preferred_name": preferred_name,
                            "profile_summary": payload.get("profile_summary", ""),
                            "onboarding_status": payload.get("onboarding_status", "ready" if preferred_name else "new"),
                        }
                        self._service.state_store.save_member(member_payload)
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
                    key = self.knowledge_entry_key(entry)
                    if key in existing_keys:
                        continue
                    self._service.state_store.add_knowledge(
                        character_id=str(session.character_id or self._service._active_character_id()).strip(),
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

    def repair_character_isolation(self, character_id: str = "") -> None:
        known = self._service.persona.known_names()
        target_ids = [str(character_id or "").strip()] if str(character_id or "").strip() else list(known.keys())
        target_ids = [item for item in target_ids if item]
        if not target_ids:
            return
        store = self._service.memory.store
        if hasattr(store, "list_sessions"):
            for session in store.list_sessions():  # type: ignore[no-any-return]
                session_character = str(getattr(session, "character_id", "") or "").strip()
                if session_character not in target_ids:
                    continue
                assistant_name = known.get(session_character, "")
                if not assistant_name:
                    try:
                        assistant_name = self._service.cards.load(session.launcher_type, session).assistant_name
                    except Exception:
                        assistant_name = ""
                if assistant_name:
                    self._service.persona.sanitize_session_state(session, assistant_name=assistant_name)
        list_members = getattr(self._service.state_store, "list_members", None)
        if callable(list_members):
            for session_character in target_ids:
                for member in list_members(limit=5000, character_id=session_character):
                    self._service.persona.sanitize_member_state(
                        group_id=str(member.get("group_id", "") or ""),
                        user_id=str(member.get("user_id", "") or ""),
                        character_id=session_character,
                    )

    @staticmethod
    def iter_legacy_group_members(raw_members: object) -> list[dict[str, object]]:
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
    def knowledge_entry_key(entry: dict[str, object]) -> tuple[str, str, str, str, tuple[str, ...]]:
        tags = (
            tuple(
                sorted(
                    str(tag).strip()
                    for tag in entry.get("tags", [])
                    if str(tag).strip()
                )
            )
            if isinstance(entry.get("tags"), list)
            else ()
        )
        return (
            str(entry.get("scope_type", "") or "").strip(),
            str(entry.get("scope_id", "") or "").strip(),
            str(entry.get("memory_type", "") or "").strip(),
            str(entry.get("summary", "") or "").strip(),
            tags,
        )
