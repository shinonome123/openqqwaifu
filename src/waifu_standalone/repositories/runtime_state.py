from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .schema_migrations import run_migrations


def _now() -> int:
    return int(time.time())


def _normalize_string(value: object) -> str:
    return str(value or "").strip()


def _normalize_scope(scope_type: object, scope_id: object) -> tuple[str, str]:
    resolved_type = _normalize_string(scope_type).lower() or "global"
    resolved_id = _normalize_string(scope_id)
    if resolved_type == "global":
        return "global", ""
    if resolved_type not in {"group", "person", "member"}:
        raise ValueError("scope_type must be one of: global, group, person, member")
    if not resolved_id:
        raise ValueError("scope_id is required for non-global knowledge entries")
    return resolved_type, resolved_id


def _normalize_member(group_id: object, user_id: object) -> tuple[str, str]:
    resolved_group = _normalize_string(group_id)
    resolved_user = _normalize_string(user_id)
    if not resolved_user:
        raise ValueError("user_id is required")
    return resolved_group, resolved_user


def _normalize_character_id(value: object) -> str:
    return _normalize_string(value)


def _normalize_assistant_alias_key(character_id: object, user_id: object) -> tuple[str, str]:
    resolved_character_id = _normalize_character_id(character_id)
    resolved_user_id = _normalize_string(user_id)
    if not resolved_character_id:
        raise ValueError("character_id is required")
    if not resolved_user_id:
        raise ValueError("user_id is required")
    return resolved_character_id, resolved_user_id


def _normalize_membership_status(value: object) -> str:
    resolved = _normalize_string(value).lower()
    if resolved in {"active", "left", "removed"}:
        return resolved
    return "active"


def _normalize_tags(value: object) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = _normalize_string(item)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result


def _decode_json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return _normalize_tags(value)
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return _normalize_tags(decoded)


def _extract_terms(text: object) -> list[str]:
    raw = _normalize_string(text).lower()
    if not raw:
        return []
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in raw)
    unique: list[str] = []
    seen: set[str] = set()
    for part in cleaned.split():
        if len(part) < 2 or part in seen:
            continue
        seen.add(part)
        unique.append(part)
    return unique


def _score_knowledge(summary: str, tags: list[str], query_terms: list[str]) -> float:
    if not query_terms:
        return 0.2
    haystack = summary.lower()
    lowered_tags = [tag.lower() for tag in tags]
    score = 0.0
    for term in query_terms:
        if any(term in tag or tag in term for tag in lowered_tags):
            score += 2.5
        elif term in haystack:
            score += 1.2
    return score


def _normalize_vector(values: object) -> list[float]:
    if not isinstance(values, list):
        return []
    vector: list[float] = []
    for value in values:
        try:
            vector.append(float(value))
        except (TypeError, ValueError):
            continue
    if not vector:
        return []
    magnitude = math.sqrt(sum(item * item for item in vector))
    if magnitude <= 0:
        return vector
    return [item / magnitude for item in vector]


def _decode_embedding(value: object) -> list[float]:
    if isinstance(value, list):
        return _normalize_vector(value)
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return _normalize_vector(decoded)


def _encode_embedding(vector: list[float]) -> str:
    if not vector:
        return ""
    return json.dumps(vector, ensure_ascii=False, separators=(",", ":"))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def _embedding_input(summary: str, tags: list[str]) -> str:
    tag_block = " ".join(tag for tag in tags if tag)
    if tag_block:
        return f"{summary}\nTags: {tag_block}"
    return summary


def _coerce_confidence(value: object) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, resolved))


def _coerce_affinity(value: object) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-1.0, min(1.0, resolved))


def _persona_member_defaults(shared: dict[str, Any], *, character_id: str = "") -> dict[str, Any]:
    return {
        "character_id": _normalize_character_id(character_id),
        "profile_summary": "",
        "affinity_score": 0.0,
        "notes_count": 0,
        "last_addressed_at": 0,
        "persona_created_at": int(shared.get("created_at") or 0),
        "persona_updated_at": int(shared.get("updated_at") or 0),
    }


def _merge_member_payload(
    shared: dict[str, Any],
    persona: dict[str, Any] | None = None,
    *,
    character_id: str = "",
    assistant_alias: str = "",
) -> dict[str, Any]:
    merged = dict(shared)
    if character_id or persona is not None:
        merged.update(_persona_member_defaults(shared, character_id=character_id))
    else:
        merged.update(
            {
                "character_id": "",
                "profile_summary": _normalize_string(shared.get("profile_summary")),
                "affinity_score": _coerce_affinity(shared.get("affinity_score")),
                "notes_count": int(shared.get("notes_count") or 0),
                "last_addressed_at": int(shared.get("last_addressed_at") or 0),
                "persona_created_at": int(shared.get("created_at") or shared.get("persona_created_at") or 0),
                "persona_updated_at": int(shared.get("updated_at") or shared.get("persona_updated_at") or 0),
            }
        )
    if persona:
        merged.update(
            {
                "character_id": _normalize_character_id(persona.get("character_id", character_id)),
                "profile_summary": _normalize_string(persona.get("profile_summary")),
                "affinity_score": _coerce_affinity(persona.get("affinity_score")),
                "notes_count": int(persona.get("notes_count") or 0),
                "last_addressed_at": int(persona.get("last_addressed_at") or 0),
                "persona_created_at": int(persona.get("created_at") or persona.get("persona_created_at") or 0),
                "persona_updated_at": int(persona.get("updated_at") or persona.get("persona_updated_at") or 0),
            }
        )
    merged["assistant_alias"] = _normalize_string(assistant_alias)
    return merged


class InMemoryRuntimeStateStore:
    def __init__(self, embedder: Any = None) -> None:
        self._members: dict[tuple[str, str], dict[str, Any]] = {}
        self._persona_members: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._assistant_aliases: dict[tuple[str, str], dict[str, Any]] = {}
        self._skill_telemetry: list[dict[str, Any]] = []
        self._knowledge: dict[int, dict[str, Any]] = {}
        self._knowledge_id = 1
        self._lock = threading.Lock()
        self._embedder = embedder
        self._default_character_id = ""

    def set_embedder(self, embedder: Any) -> None:
        self._embedder = embedder

    def close(self) -> None:
        close = getattr(self._embedder, "close", None)
        if callable(close):
            close()

    def set_default_character(self, character_id: object) -> None:
        self._default_character_id = _normalize_character_id(character_id)

    def adopt_legacy_character(self, character_id: object) -> int:
        safe_character_id = _normalize_character_id(character_id)
        if not safe_character_id:
            return 0
        migrated = 0
        with self._lock:
            for key, shared in list(self._members.items()):
                persona_key = (safe_character_id, key[0], key[1])
                if persona_key in self._persona_members:
                    continue
                if not any(
                    [
                        _normalize_string(shared.get("profile_summary")),
                        int(shared.get("notes_count") or 0),
                        int(shared.get("last_addressed_at") or 0),
                        abs(float(shared.get("affinity_score") or 0.0)) > 1e-6,
                    ]
                ):
                    continue
                self._persona_members[persona_key] = {
                    "character_id": safe_character_id,
                    "group_id": key[0],
                    "user_id": key[1],
                    "profile_summary": _normalize_string(shared.get("profile_summary")),
                    "affinity_score": _coerce_affinity(shared.get("affinity_score")),
                    "notes_count": int(shared.get("notes_count") or 0),
                    "last_addressed_at": int(shared.get("last_addressed_at") or 0),
                    "created_at": int(shared.get("created_at") or 0),
                    "updated_at": int(shared.get("updated_at") or 0),
                }
                shared["profile_summary"] = ""
                shared["affinity_score"] = 0.0
                shared["notes_count"] = 0
                shared["last_addressed_at"] = 0
                self._members[key] = shared
                migrated += 1
            for entry in self._knowledge.values():
                if not _normalize_character_id(entry.get("character_id")):
                    entry["character_id"] = safe_character_id
        return migrated

    def record_skill_telemetry(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = _normalize_skill_telemetry(payload)
        with self._lock:
            self._skill_telemetry.append(event)
            self._skill_telemetry = self._skill_telemetry[-5000:]
        return dict(event)

    def list_skill_telemetry(self, *, skill_id: object = "", limit: int = 200) -> list[dict[str, Any]]:
        safe_skill_id = _normalize_string(skill_id)
        resolved_limit = max(1, min(1000, int(limit or 200)))
        with self._lock:
            rows = [
                dict(item)
                for item in self._skill_telemetry
                if not safe_skill_id or _normalize_string(item.get("skill_id")) == safe_skill_id
            ]
        rows.sort(key=lambda item: (int(item.get("created_at") or 0), int(item.get("id") or 0)), reverse=True)
        return rows[:resolved_limit]

    def skill_telemetry_summary(self, skill_id: object) -> dict[str, Any]:
        rows = self.list_skill_telemetry(skill_id=skill_id, limit=1000)
        return _skill_telemetry_summary_from_rows(rows)

    def save_assistant_alias(
        self,
        *,
        character_id: object,
        user_id: object,
        assistant_alias: object,
    ) -> dict[str, Any]:
        safe_character_id, safe_user_id = _normalize_assistant_alias_key(character_id, user_id)
        cleaned_alias = _normalize_string(assistant_alias)
        now = _now()
        key = (safe_character_id, safe_user_id)
        with self._lock:
            existing = dict(self._assistant_aliases.get(key, {}))
            if not cleaned_alias:
                self._assistant_aliases.pop(key, None)
                return {
                    "character_id": safe_character_id,
                    "user_id": safe_user_id,
                    "assistant_alias": "",
                    "created_at": int(existing.get("created_at") or 0),
                    "updated_at": now,
                }
            payload = {
                "character_id": safe_character_id,
                "user_id": safe_user_id,
                "assistant_alias": cleaned_alias,
                "created_at": int(existing.get("created_at") or now),
                "updated_at": now,
            }
            self._assistant_aliases[key] = payload
            return dict(payload)

    def get_assistant_alias(
        self,
        *,
        character_id: object,
        user_id: object,
    ) -> dict[str, Any] | None:
        safe_character_id, safe_user_id = _normalize_assistant_alias_key(character_id, user_id)
        with self._lock:
            record = self._assistant_aliases.get((safe_character_id, safe_user_id))
            return dict(record) if record is not None else None

    def record_member_seen(
        self,
        *,
        group_id: object,
        user_id: object,
        qq_nickname: object = "",
        group_card: object = "",
        membership_status: object = "active",
        last_sync_at: object = 0,
    ) -> dict[str, Any]:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        now = _now()
        key = (safe_group_id, safe_user_id)
        with self._lock:
            existing = dict(self._members.get(key, {}))
            created_at = int(existing.get("created_at") or now)
            member = {
                "group_id": safe_group_id,
                "user_id": safe_user_id,
                "qq_nickname": _normalize_string(qq_nickname) or _normalize_string(existing.get("qq_nickname")),
                "group_card": _normalize_string(group_card) or _normalize_string(existing.get("group_card")),
                "preferred_name": _normalize_string(existing.get("preferred_name")),
                "onboarding_status": _normalize_string(existing.get("onboarding_status")) or "new",
                "membership_status": _normalize_membership_status(
                    membership_status if membership_status is not None else existing.get("membership_status")
                ),
                "last_seen_at": now,
                "last_sync_at": int(last_sync_at or existing.get("last_sync_at") or 0),
                "profile_summary": _normalize_string(existing.get("profile_summary")),
                "affinity_score": _coerce_affinity(existing.get("affinity_score")),
                "notes_count": int(existing.get("notes_count") or 0),
                "last_addressed_at": int(existing.get("last_addressed_at") or 0),
                "created_at": created_at,
                "updated_at": now,
            }
            self._members[key] = member
            alias_record = self._assistant_aliases.get((self._default_character_id, safe_user_id))
            return _merge_member_payload(
                member,
                character_id=self._default_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def save_member(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe_group_id, safe_user_id = _normalize_member(payload.get("group_id"), payload.get("user_id"))
        safe_character_id = _normalize_character_id(payload.get("character_id"))
        now = _now()
        key = (safe_group_id, safe_user_id)
        with self._lock:
            existing = dict(self._members.get(key, {}))
            preferred_name = _normalize_string(payload.get("preferred_name", existing.get("preferred_name")))
            onboarding_status = _normalize_string(payload.get("onboarding_status", existing.get("onboarding_status")))
            if preferred_name and not onboarding_status:
                onboarding_status = "ready"
            member = {
                "group_id": safe_group_id,
                "user_id": safe_user_id,
                "qq_nickname": _normalize_string(payload.get("qq_nickname", existing.get("qq_nickname"))),
                "group_card": _normalize_string(payload.get("group_card", existing.get("group_card"))),
                "preferred_name": preferred_name,
                "onboarding_status": onboarding_status or "new",
                "membership_status": _normalize_membership_status(
                    payload.get("membership_status", existing.get("membership_status"))
                ),
                "last_seen_at": int(payload.get("last_seen_at", existing.get("last_seen_at") or 0) or 0),
                "last_sync_at": int(payload.get("last_sync_at", existing.get("last_sync_at") or 0) or 0),
                "profile_summary": _normalize_string(existing.get("profile_summary")),
                "affinity_score": _coerce_affinity(existing.get("affinity_score")),
                "notes_count": int(existing.get("notes_count") or 0),
                "last_addressed_at": int(existing.get("last_addressed_at") or 0),
                "created_at": int(existing.get("created_at") or now),
                "updated_at": now,
            }
            self._members[key] = member
            if safe_character_id:
                persona_key = (safe_character_id, safe_group_id, safe_user_id)
                persona_existing = dict(self._persona_members.get(persona_key, {}))
                persona = {
                    "character_id": safe_character_id,
                    "group_id": safe_group_id,
                    "user_id": safe_user_id,
                    "profile_summary": _normalize_string(
                        payload.get("profile_summary", persona_existing.get("profile_summary"))
                    ),
                    "affinity_score": _coerce_affinity(
                        payload.get("affinity_score", persona_existing.get("affinity_score"))
                    ),
                    "notes_count": int(payload.get("notes_count", persona_existing.get("notes_count") or 0) or 0),
                    "last_addressed_at": int(
                        payload.get("last_addressed_at", persona_existing.get("last_addressed_at") or 0) or 0
                    ),
                    "created_at": int(persona_existing.get("created_at") or now),
                    "updated_at": now,
                }
                self._persona_members[persona_key] = persona
                alias_record = self._assistant_aliases.get((safe_character_id, safe_user_id))
                return _merge_member_payload(
                    member,
                    persona,
                    character_id=safe_character_id,
                    assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
                )
            member["profile_summary"] = _normalize_string(payload.get("profile_summary", member.get("profile_summary")))
            member["affinity_score"] = _coerce_affinity(payload.get("affinity_score", member.get("affinity_score")))
            member["notes_count"] = int(payload.get("notes_count", member.get("notes_count") or 0) or 0)
            member["last_addressed_at"] = int(
                payload.get("last_addressed_at", member.get("last_addressed_at") or 0) or 0
            )
            self._members[key] = member
            return _merge_member_payload(member, character_id="", assistant_alias="")

    def mark_member_membership(
        self,
        *,
        group_id: object,
        user_id: object,
        membership_status: object,
        last_sync_at: object = 0,
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        key = (safe_group_id, safe_user_id)
        now = _now()
        with self._lock:
            existing = self._members.get(key)
            if existing is None:
                return None
            member = dict(existing)
            member["membership_status"] = _normalize_membership_status(membership_status)
            if last_sync_at:
                member["last_sync_at"] = int(last_sync_at)
            member["updated_at"] = now
            self._members[key] = member
            return dict(member)

    def mark_group_members_missing(
        self,
        *,
        group_id: object,
        active_user_ids: list[str],
        membership_status: object = "left",
        last_sync_at: object = 0,
    ) -> int:
        safe_group_id = _normalize_string(group_id)
        active_ids = {str(item or "").strip() for item in active_user_ids if str(item or "").strip()}
        now = _now()
        changed = 0
        with self._lock:
            for key, existing in list(self._members.items()):
                member_group_id, member_user_id = key
                if member_group_id != safe_group_id or member_user_id in active_ids:
                    continue
                member = dict(existing)
                member["membership_status"] = _normalize_membership_status(membership_status)
                if last_sync_at:
                    member["last_sync_at"] = int(last_sync_at)
                member["updated_at"] = now
                self._members[key] = member
                changed += 1
        return changed

    def mark_member_addressed(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        now = _now()
        key = (safe_group_id, safe_user_id)
        with self._lock:
            existing = self._members.get(key)
            if existing is None:
                return None
            member = dict(existing)
            if safe_character_id:
                persona_key = (safe_character_id, safe_group_id, safe_user_id)
                persona_existing = dict(self._persona_members.get(persona_key, {}))
                persona_existing.update(
                    {
                        "character_id": safe_character_id,
                        "group_id": safe_group_id,
                        "user_id": safe_user_id,
                        "profile_summary": _normalize_string(persona_existing.get("profile_summary")),
                        "affinity_score": _coerce_affinity(persona_existing.get("affinity_score")),
                        "notes_count": int(persona_existing.get("notes_count") or 0),
                        "last_addressed_at": now,
                        "created_at": int(persona_existing.get("created_at") or member.get("created_at") or now),
                        "updated_at": now,
                    }
                )
                self._persona_members[persona_key] = persona_existing
                alias_record = self._assistant_aliases.get((safe_character_id, safe_user_id))
                return _merge_member_payload(
                    member,
                    persona_existing,
                    character_id=safe_character_id,
                    assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
                )
            member["last_addressed_at"] = now
            member["updated_at"] = now
            self._members[key] = member
            return _merge_member_payload(member, character_id="")

    def adjust_member_affinity(
        self,
        *,
        group_id: object,
        user_id: object,
        delta: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        key = (safe_group_id, safe_user_id)
        now = _now()
        with self._lock:
            existing = self._members.get(key)
            if existing is None:
                return None
            member = dict(existing)
            if safe_character_id:
                persona_key = (safe_character_id, safe_group_id, safe_user_id)
                persona_existing = dict(self._persona_members.get(persona_key, {}))
                next_affinity = _coerce_affinity(
                    float(persona_existing.get("affinity_score") or 0.0) + float(delta or 0.0)
                )
                persona_existing.update(
                    {
                        "character_id": safe_character_id,
                        "group_id": safe_group_id,
                        "user_id": safe_user_id,
                        "profile_summary": _normalize_string(persona_existing.get("profile_summary")),
                        "affinity_score": next_affinity,
                        "notes_count": int(persona_existing.get("notes_count") or 0),
                        "last_addressed_at": int(persona_existing.get("last_addressed_at") or 0),
                        "created_at": int(persona_existing.get("created_at") or member.get("created_at") or now),
                        "updated_at": now,
                    }
                )
                self._persona_members[persona_key] = persona_existing
                alias_record = self._assistant_aliases.get((safe_character_id, safe_user_id))
                return _merge_member_payload(
                    member,
                    persona_existing,
                    character_id=safe_character_id,
                    assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
                )
            member["affinity_score"] = _coerce_affinity(float(member.get("affinity_score") or 0.0) + float(delta or 0.0))
            member["updated_at"] = now
            self._members[key] = member
            return _merge_member_payload(member, character_id="")

    def get_member(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        with self._lock:
            member = self._members.get((safe_group_id, safe_user_id))
            if member is None:
                return None
            persona = None
            if safe_character_id:
                persona = self._persona_members.get((safe_character_id, safe_group_id, safe_user_id))
            alias_record = self._assistant_aliases.get((safe_character_id, safe_user_id)) if safe_character_id else None
            return _merge_member_payload(
                dict(member),
                dict(persona) if persona is not None else None,
                character_id=safe_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def reset_member_persona(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object,
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        if not safe_character_id:
            raise ValueError("character_id is required")
        with self._lock:
            member = self._members.get((safe_group_id, safe_user_id))
            if member is None:
                return None
            self._persona_members.pop((safe_character_id, safe_group_id, safe_user_id), None)
            alias_record = self._assistant_aliases.get((safe_character_id, safe_user_id))
            return _merge_member_payload(
                dict(member),
                None,
                character_id=safe_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def list_members(self, *, limit: int = 120, character_id: object = "") -> list[dict[str, Any]]:
        safe_character_id = _normalize_character_id(character_id)
        with self._lock:
            members = []
            for key, item in self._members.items():
                persona = None
                if safe_character_id:
                    persona = self._persona_members.get((safe_character_id, key[0], key[1]))
                members.append(
                    _merge_member_payload(
                        dict(item),
                        dict(persona) if persona is not None else None,
                        character_id=safe_character_id,
                        assistant_alias=str(
                            (
                                self._assistant_aliases.get((safe_character_id, str(item.get("user_id", "") or "")))
                                or {}
                            ).get("assistant_alias", "")
                            or ""
                        ),
                    )
                )
        members.sort(
            key=lambda item: (
                0 if str(item.get("membership_status", "") or "").strip() == "active" else 1,
                -(int(item.get("last_seen_at") or 0)),
                -(int(item.get("updated_at") or 0)),
                str(item.get("group_id") or ""),
                str(item.get("user_id") or ""),
            )
        )
        return members[: max(1, int(limit))]

    def member_count(self) -> int:
        with self._lock:
            return len(self._members)

    def count_members_in_group(self, group_id: str) -> int:
        gid = str(group_id or "").strip()
        with self._lock:
            return sum(
                1
                for m in self._members.values()
                if str(m.get("group_id", "") or "").strip() == gid
                and str(m.get("membership_status", "active") or "").strip() == "active"
            )

    def count_knowledge_for_scope(self, scope_type: str, scope_id: str, *, character_id: object = "") -> int:
        st = str(scope_type or "").strip()
        sid = str(scope_id or "").strip()
        safe_character_id = _normalize_character_id(character_id)
        with self._lock:
            return sum(
                1
                for entry in self._knowledge.values()
                if str(entry.get("scope_type", "") or "").strip() == st
                and str(entry.get("scope_id", "") or "").strip() == sid
                and _normalize_character_id(entry.get("character_id")) == safe_character_id
            )

    def count_knowledge_for_scopes(self, scopes: list[tuple[str, str]], *, character_id: object = "") -> int:
        scope_set = {(str(st or "").strip(), str(sid or "").strip()) for st, sid in scopes}
        safe_character_id = _normalize_character_id(character_id)
        with self._lock:
            return sum(
                1
                for entry in self._knowledge.values()
                if (
                    str(entry.get("scope_type", "") or "").strip(),
                    str(entry.get("scope_id", "") or "").strip(),
                ) in scope_set
                and _normalize_character_id(entry.get("character_id")) == safe_character_id
            )

    def save_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope_type, scope_id = _normalize_scope(payload.get("scope_type"), payload.get("scope_id"))
        safe_character_id = _normalize_character_id(payload.get("character_id"))
        summary = _normalize_string(payload.get("summary"))
        if not summary:
            raise ValueError("summary is required")
        now = _now()
        with self._lock:
            raw_id = payload.get("id")
            try:
                entry_id = int(raw_id or 0)
            except (TypeError, ValueError):
                entry_id = 0
            existing = dict(self._knowledge.get(entry_id, {})) if entry_id else {}
            if entry_id <= 0:
                entry_id = self._knowledge_id
                self._knowledge_id += 1
            entry = {
                "id": entry_id,
                "character_id": safe_character_id or _normalize_character_id(existing.get("character_id")),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "memory_type": _normalize_string(payload.get("memory_type", existing.get("memory_type"))) or "fact",
                "summary": summary,
                "tags": _normalize_tags(payload.get("tags", existing.get("tags"))),
                "source_message_ids": _normalize_tags(
                    payload.get("source_message_ids", existing.get("source_message_ids"))
                ),
                "confidence": _coerce_confidence(payload.get("confidence", existing.get("confidence", 0.5))),
                "archived": bool(payload.get("archived", existing.get("archived", False))),
                "created_at": int(existing.get("created_at") or now),
                "updated_at": now,
            }
            entry["embedding"] = self._resolve_embedding(
                summary=summary,
                tags=entry["tags"],
                existing=existing.get("embedding"),
                previous_summary=_normalize_string(existing.get("summary")),
                previous_tags=_normalize_tags(existing.get("tags")),
            )
            self._knowledge[entry_id] = entry
            return dict(entry)

    def delete_knowledge(self, entry_id: object, *, character_id: object = "") -> bool:
        try:
            safe_entry_id = int(entry_id or 0)
        except (TypeError, ValueError):
            return False
        if safe_entry_id <= 0:
            return False
        safe_character_id = _normalize_character_id(character_id)
        with self._lock:
            existing = dict(self._knowledge.get(safe_entry_id, {}))
            if not existing:
                return False
            if safe_character_id and _normalize_character_id(existing.get("character_id")) != safe_character_id:
                return False
            self._knowledge.pop(safe_entry_id, None)
        return True

    def add_knowledge(
        self,
        *,
        scope_type: object,
        scope_id: object,
        memory_type: object,
        summary: object,
        tags: list[str] | None = None,
        source_message_ids: list[str] | None = None,
        confidence: float = 0.5,
        archived: bool = False,
        character_id: object = "",
    ) -> dict[str, Any]:
        return self.save_knowledge(
            {
                "character_id": character_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "memory_type": memory_type,
                "summary": summary,
                "tags": tags or [],
                "source_message_ids": source_message_ids or [],
                "confidence": confidence,
                "archived": archived,
            }
        )

    def list_knowledge(self, *, limit: int = 80, character_id: object | None = None) -> list[dict[str, Any]]:
        safe_character_id = None if character_id is None else _normalize_character_id(character_id)
        with self._lock:
            entries = [
                dict(item)
                for item in self._knowledge.values()
                if safe_character_id is None or _normalize_character_id(item.get("character_id")) == safe_character_id
            ]
        entries.sort(
            key=lambda item: (
                -int(item.get("updated_at") or 0),
                -int(item.get("id") or 0),
            )
        )
        return entries[: max(1, int(limit))]

    def recall_knowledge(
        self,
        *,
        scopes: list[tuple[str, str]],
        query: object,
        limit: int = 3,
        character_id: object = "",
    ) -> list[str]:
        query_terms = _extract_terms(query)
        query_embedding = self._embed_query(query)
        safe_character_id = _normalize_character_id(character_id)
        scope_set = {(_normalize_string(kind).lower(), _normalize_string(identifier)) for kind, identifier in scopes}
        scope_set.add(("global", ""))
        with self._lock:
            entries = [dict(item) for item in self._knowledge.values()]
        scored: list[tuple[float, str]] = []
        for entry in entries:
            if _normalize_character_id(entry.get("character_id")) != safe_character_id:
                continue
            entry_scope = (
                _normalize_string(entry.get("scope_type")).lower(),
                _normalize_string(entry.get("scope_id")),
            )
            if entry_scope not in scope_set:
                continue
            summary = _normalize_string(entry.get("summary"))
            if not summary:
                continue
            tags = _normalize_tags(entry.get("tags"))
            score = self._score_entry(
                summary=summary,
                tags=tags,
                entry_embedding=_decode_embedding(entry.get("embedding")),
                query_terms=query_terms,
                query_embedding=query_embedding,
            )
            score += _coerce_confidence(entry.get("confidence")) * 0.18
            if entry.get("archived"):
                score -= 0.15
            scored.append((score, summary))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [summary for _, summary in scored[: max(1, int(limit))]]

    def knowledge_count(self, *, character_id: object | None = None) -> int:
        safe_character_id = None if character_id is None else _normalize_character_id(character_id)
        with self._lock:
            if safe_character_id is None:
                return len(self._knowledge)
            return sum(1 for item in self._knowledge.values() if _normalize_character_id(item.get("character_id")) == safe_character_id)

    def embedded_knowledge_count(self, *, character_id: object | None = None) -> int:
        safe_character_id = None if character_id is None else _normalize_character_id(character_id)
        with self._lock:
            return sum(
                1
                for item in self._knowledge.values()
                if _decode_embedding(item.get("embedding"))
                and (safe_character_id is None or _normalize_character_id(item.get("character_id")) == safe_character_id)
            )

    def refresh_knowledge_embeddings(self) -> int:
        with self._lock:
            entries = [(entry_id, dict(entry)) for entry_id, entry in self._knowledge.items()]
        updated = 0
        for entry_id, entry in entries:
            if _decode_embedding(entry.get("embedding")):
                continue
            summary = _normalize_string(entry.get("summary"))
            if not summary:
                continue
            vector = self._embed_text(_embedding_input(summary, _normalize_tags(entry.get("tags"))))
            if not vector:
                continue
            with self._lock:
                current = dict(self._knowledge.get(entry_id, {}))
                if not current:
                    continue
                current["embedding"] = vector
                self._knowledge[entry_id] = current
            updated += 1
        return updated

    def _resolve_embedding(
        self,
        *,
        summary: str,
        tags: list[str],
        existing: object,
        previous_summary: str,
        previous_tags: list[str],
    ) -> list[float]:
        current = _decode_embedding(existing)
        if current and summary == previous_summary and tags == previous_tags:
            return current
        return self._embed_text(_embedding_input(summary, tags)) or current

    def _embed_query(self, query: object) -> list[float]:
        return self._embed_text(_normalize_string(query))

    def _embed_text(self, text: str) -> list[float]:
        if self._embedder is None or not getattr(self._embedder, "ready", False):
            return []
        try:
            return _normalize_vector(self._embedder.embed(text))
        except Exception:
            return []

    @staticmethod
    def _score_entry(
        *,
        summary: str,
        tags: list[str],
        entry_embedding: list[float],
        query_terms: list[str],
        query_embedding: list[float],
    ) -> float:
        lexical_score = _score_knowledge(summary, tags, query_terms)
        if entry_embedding and query_embedding:
            semantic_score = _cosine_similarity(entry_embedding, query_embedding)
            return max(semantic_score * 4.0, lexical_score)
        return lexical_score


class SqliteRuntimeStateStore:
    def __init__(self, path: str | Path, embedder: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # _write_lock serializes writers at the Python level so that BEGIN IMMEDIATE
        # retries don't thrash. WAL mode already allows readers to run concurrently
        # with a single writer, so read-only hot paths (``recall_knowledge``,
        # ``list_members`` …) deliberately skip this lock.
        self._write_lock = threading.Lock()
        # Back-compat alias: some call sites still reference ``self._lock``.
        self._lock = self._write_lock
        self._embedder = embedder
        self._default_character_id = ""
        self._init_schema()

    def set_embedder(self, embedder: Any) -> None:
        self._embedder = embedder

    def close(self) -> None:
        close = getattr(self._embedder, "close", None)
        if callable(close):
            close()

    def set_default_character(self, character_id: object) -> None:
        self._default_character_id = _normalize_character_id(character_id)

    def adopt_legacy_character(self, character_id: object) -> int:
        safe_character_id = _normalize_character_id(character_id)
        if not safe_character_id:
            return 0
        now = _now()
        migrated = 0
        with self._lock, self._session() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM members
                WHERE profile_summary <> '' OR affinity_score <> 0 OR notes_count <> 0 OR last_addressed_at <> 0
                """
            ).fetchall()
            for row in rows:
                existing = connection.execute(
                    """
                    SELECT 1
                    FROM member_persona_state
                    WHERE character_id = ? AND group_id = ? AND user_id = ?
                    """,
                    (safe_character_id, row["group_id"], row["user_id"]),
                ).fetchone()
                if existing is not None:
                    continue
                connection.execute(
                    """
                    INSERT INTO member_persona_state (
                        character_id, group_id, user_id, profile_summary, affinity_score, notes_count,
                        last_addressed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        safe_character_id,
                        row["group_id"],
                        row["user_id"],
                        row["profile_summary"],
                        row["affinity_score"],
                        row["notes_count"],
                        row["last_addressed_at"],
                        int(row["created_at"] or now),
                        int(row["updated_at"] or now),
                    ),
                )
                connection.execute(
                    """
                    UPDATE members
                    SET profile_summary = '', affinity_score = 0, notes_count = 0, last_addressed_at = 0, updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (now, row["group_id"], row["user_id"]),
                )
                migrated += 1
            connection.execute(
                """
                UPDATE knowledge_entries
                SET character_id = ?
                WHERE character_id = ''
                """,
                (safe_character_id,),
            )
        return migrated

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        # Give writers a grace period so that transient contention retries
        # transparently instead of raising ``OperationalError: database is locked``.
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _read_session(self):
        """Open a short-lived read-only connection outside the write lock.

        WAL mode lets reads run concurrently with a single writer, so hot-path
        readers don't need to serialize on ``self._write_lock``. The connection
        is still closed eagerly so file handles don't linger (important on
        Windows where open handles block ``tempfile.TemporaryDirectory`` cleanup).
        """
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                run_migrations(connection)
            finally:
                connection.close()

    def record_skill_telemetry(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = _normalize_skill_telemetry(payload)
        with self._lock, self._session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO skill_telemetry (
                    trace_id, skill_id, tool_id, trigger_source, caller, status,
                    error_code, error, latency_ms, arg_summary, result_summary,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["trace_id"],
                    event["skill_id"],
                    event["tool_id"],
                    event["trigger_source"],
                    event["caller"],
                    event["status"],
                    event["error_code"],
                    event["error"],
                    int(event["latency_ms"]),
                    event["arg_summary"],
                    event["result_summary"],
                    event["details_json"],
                    int(event["created_at"]),
                ),
            )
            event["id"] = int(cursor.lastrowid or 0)
        return dict(event)

    def list_skill_telemetry(self, *, skill_id: object = "", limit: int = 200) -> list[dict[str, Any]]:
        safe_skill_id = _normalize_string(skill_id)
        resolved_limit = max(1, min(1000, int(limit or 200)))
        with self._read_session() as connection:
            if safe_skill_id:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM skill_telemetry
                    WHERE skill_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_skill_id, resolved_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM skill_telemetry
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (resolved_limit,),
                ).fetchall()
        return [_row_to_skill_telemetry(row) for row in rows]

    def skill_telemetry_summary(self, skill_id: object) -> dict[str, Any]:
        safe_skill_id = _normalize_string(skill_id)
        if not safe_skill_id:
            return _skill_telemetry_summary_from_rows([])
        with self._read_session() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS calls,
                    SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS success
                FROM skill_telemetry
                WHERE skill_id = ?
                """,
                (safe_skill_id,),
            ).fetchone()
            last_row = connection.execute(
                """
                SELECT *
                FROM skill_telemetry
                WHERE skill_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (safe_skill_id,),
            ).fetchone()
        calls = int(row["calls"] or 0) if row is not None else 0
        success = int(row["success"] or 0) if row is not None else 0
        failure = max(0, calls - success)
        return {
            "calls": calls,
            "success": success,
            "failure": failure,
            "success_rate": round(success / calls, 4) if calls else 0.0,
            "last": _row_to_skill_telemetry(last_row) if last_row is not None else {},
            "never_succeeded": calls > 0 and success == 0,
        }

    def save_assistant_alias(
        self,
        *,
        character_id: object,
        user_id: object,
        assistant_alias: object,
    ) -> dict[str, Any]:
        safe_character_id, safe_user_id = _normalize_assistant_alias_key(character_id, user_id)
        cleaned_alias = _normalize_string(assistant_alias)
        now = _now()
        with self._lock, self._session() as connection:
            existing = self._fetch_assistant_alias(connection, safe_character_id, safe_user_id) or {}
            if not cleaned_alias:
                connection.execute(
                    """
                    DELETE FROM assistant_aliases
                    WHERE character_id = ? AND user_id = ?
                    """,
                    (safe_character_id, safe_user_id),
                )
                return {
                    "character_id": safe_character_id,
                    "user_id": safe_user_id,
                    "assistant_alias": "",
                    "created_at": int(existing.get("created_at") or 0),
                    "updated_at": now,
                }
            connection.execute(
                """
                INSERT INTO assistant_aliases (
                    character_id, user_id, assistant_alias, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(character_id, user_id) DO UPDATE SET
                    assistant_alias = excluded.assistant_alias,
                    updated_at = excluded.updated_at
                """,
                (
                    safe_character_id,
                    safe_user_id,
                    cleaned_alias,
                    int(existing.get("created_at") or now),
                    now,
                ),
            )
            return self._fetch_assistant_alias(connection, safe_character_id, safe_user_id) or {}

    def get_assistant_alias(
        self,
        *,
        character_id: object,
        user_id: object,
    ) -> dict[str, Any] | None:
        safe_character_id, safe_user_id = _normalize_assistant_alias_key(character_id, user_id)
        with self._read_session() as connection:
            return self._fetch_assistant_alias(connection, safe_character_id, safe_user_id)

    def record_member_seen(
        self,
        *,
        group_id: object,
        user_id: object,
        qq_nickname: object = "",
        group_card: object = "",
        membership_status: object = "active",
        last_sync_at: object = 0,
    ) -> dict[str, Any]:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        now = _now()
        with self._lock, self._session() as connection:
            existing = self._fetch_member(connection, safe_group_id, safe_user_id)
            created_at = int(existing.get("created_at") or now) if existing else now
            connection.execute(
                """
                INSERT INTO members (
                    group_id, user_id, qq_nickname, group_card, preferred_name, onboarding_status,
                    profile_summary, membership_status, affinity_score, notes_count, last_seen_at, last_sync_at,
                    last_addressed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    qq_nickname = CASE WHEN excluded.qq_nickname <> '' THEN excluded.qq_nickname ELSE members.qq_nickname END,
                    group_card = CASE WHEN excluded.group_card <> '' THEN excluded.group_card ELSE members.group_card END,
                    membership_status = excluded.membership_status,
                    last_seen_at = excluded.last_seen_at,
                    last_sync_at = CASE WHEN excluded.last_sync_at > 0 THEN excluded.last_sync_at ELSE members.last_sync_at END,
                    updated_at = excluded.updated_at
                """,
                (
                    safe_group_id,
                    safe_user_id,
                    _normalize_string(qq_nickname),
                    _normalize_string(group_card),
                    _normalize_string(existing.get("preferred_name")) if existing else "",
                    _normalize_string(existing.get("onboarding_status")) if existing else "new",
                    _normalize_string(existing.get("profile_summary")) if existing else "",
                    _normalize_membership_status(
                        membership_status if membership_status is not None else (existing.get("membership_status") if existing else "active")
                    ),
                    _coerce_affinity(existing.get("affinity_score")) if existing else 0.0,
                    int(existing.get("notes_count") or 0) if existing else 0,
                    now,
                    int(last_sync_at or (existing.get("last_sync_at") if existing else 0) or 0),
                    int(existing.get("last_addressed_at") or 0) if existing else 0,
                    created_at,
                    now,
                ),
            )
            shared = self._fetch_member(connection, safe_group_id, safe_user_id)
            if shared is None:
                return {}
            alias_record = None
            if self._default_character_id:
                alias_record = self._fetch_assistant_alias(connection, self._default_character_id, safe_user_id)
            return _merge_member_payload(
                shared,
                character_id=self._default_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def save_member(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe_group_id, safe_user_id = _normalize_member(payload.get("group_id"), payload.get("user_id"))
        safe_character_id = _normalize_character_id(payload.get("character_id"))
        now = _now()
        with self._lock, self._session() as connection:
            existing = self._fetch_member(connection, safe_group_id, safe_user_id) or {}
            preferred_name = _normalize_string(payload.get("preferred_name", existing.get("preferred_name")))
            onboarding_status = _normalize_string(payload.get("onboarding_status", existing.get("onboarding_status")))
            if preferred_name and not onboarding_status:
                onboarding_status = "ready"
            connection.execute(
                """
                INSERT INTO members (
                    group_id, user_id, qq_nickname, group_card, preferred_name, onboarding_status,
                    profile_summary, membership_status, affinity_score, notes_count, last_seen_at, last_sync_at,
                    last_addressed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    qq_nickname = excluded.qq_nickname,
                    group_card = excluded.group_card,
                    preferred_name = excluded.preferred_name,
                    onboarding_status = excluded.onboarding_status,
                    profile_summary = excluded.profile_summary,
                    membership_status = excluded.membership_status,
                    affinity_score = excluded.affinity_score,
                    notes_count = excluded.notes_count,
                    last_seen_at = excluded.last_seen_at,
                    last_sync_at = excluded.last_sync_at,
                    last_addressed_at = excluded.last_addressed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    safe_group_id,
                    safe_user_id,
                    _normalize_string(payload.get("qq_nickname", existing.get("qq_nickname"))),
                    _normalize_string(payload.get("group_card", existing.get("group_card"))),
                    preferred_name,
                    onboarding_status or "new",
                    _normalize_string(existing.get("profile_summary")),
                    _normalize_membership_status(payload.get("membership_status", existing.get("membership_status"))),
                    _coerce_affinity(existing.get("affinity_score")),
                    int(existing.get("notes_count") or 0),
                    int(payload.get("last_seen_at", existing.get("last_seen_at") or 0) or 0),
                    int(payload.get("last_sync_at", existing.get("last_sync_at") or 0) or 0),
                    int(existing.get("last_addressed_at") or 0),
                    int(existing.get("created_at") or now),
                    now,
                ),
            )
            if safe_character_id:
                persona_existing = self._fetch_member_persona(connection, safe_character_id, safe_group_id, safe_user_id) or {}
                connection.execute(
                    """
                    INSERT INTO member_persona_state (
                        character_id, group_id, user_id, profile_summary, affinity_score, notes_count,
                        last_addressed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(character_id, group_id, user_id) DO UPDATE SET
                        profile_summary = excluded.profile_summary,
                        affinity_score = excluded.affinity_score,
                        notes_count = excluded.notes_count,
                        last_addressed_at = excluded.last_addressed_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        safe_character_id,
                        safe_group_id,
                        safe_user_id,
                        _normalize_string(payload.get("profile_summary", persona_existing.get("profile_summary"))),
                        _coerce_affinity(payload.get("affinity_score", persona_existing.get("affinity_score"))),
                        int(payload.get("notes_count", persona_existing.get("notes_count") or 0) or 0),
                        int(payload.get("last_addressed_at", persona_existing.get("last_addressed_at") or 0) or 0),
                        int(persona_existing.get("created_at") or now),
                        now,
                    ),
                )
            shared = self._fetch_member(connection, safe_group_id, safe_user_id)
            if shared is None:
                return {}
            persona = None
            if safe_character_id:
                persona = self._fetch_member_persona(connection, safe_character_id, safe_group_id, safe_user_id)
            alias_record = self._fetch_assistant_alias(connection, safe_character_id, safe_user_id) if safe_character_id else None
            return _merge_member_payload(
                shared,
                persona,
                character_id=safe_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def reset_member_persona(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object,
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        if not safe_character_id:
            raise ValueError("character_id is required")
        with self._lock, self._session() as connection:
            shared = self._fetch_member(connection, safe_group_id, safe_user_id)
            if shared is None:
                return None
            connection.execute(
                """
                DELETE FROM member_persona_state
                WHERE character_id = ? AND group_id = ? AND user_id = ?
                """,
                (safe_character_id, safe_group_id, safe_user_id),
            )
            alias_record = self._fetch_assistant_alias(connection, safe_character_id, safe_user_id)
            return _merge_member_payload(
                shared,
                None,
                character_id=safe_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def mark_member_membership(
        self,
        *,
        group_id: object,
        user_id: object,
        membership_status: object,
        last_sync_at: object = 0,
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        now = _now()
        with self._lock, self._session() as connection:
            connection.execute(
                """
                UPDATE members
                SET membership_status = ?, last_sync_at = CASE WHEN ? > 0 THEN ? ELSE last_sync_at END, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (
                    _normalize_membership_status(membership_status),
                    int(last_sync_at or 0),
                    int(last_sync_at or 0),
                    now,
                    safe_group_id,
                    safe_user_id,
                ),
            )
            return self._fetch_member(connection, safe_group_id, safe_user_id)

    def mark_group_members_missing(
        self,
        *,
        group_id: object,
        active_user_ids: list[str],
        membership_status: object = "left",
        last_sync_at: object = 0,
    ) -> int:
        safe_group_id = _normalize_string(group_id)
        active_ids = [str(item or "").strip() for item in active_user_ids if str(item or "").strip()]
        now = _now()
        with self._lock, self._session() as connection:
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                cursor = connection.execute(
                    f"""
                    UPDATE members
                    SET membership_status = ?, last_sync_at = CASE WHEN ? > 0 THEN ? ELSE last_sync_at END, updated_at = ?
                    WHERE group_id = ? AND user_id NOT IN ({placeholders})
                    """,
                    [
                        _normalize_membership_status(membership_status),
                        int(last_sync_at or 0),
                        int(last_sync_at or 0),
                        now,
                        safe_group_id,
                        *active_ids,
                    ],
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE members
                    SET membership_status = ?, last_sync_at = CASE WHEN ? > 0 THEN ? ELSE last_sync_at END, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (
                        _normalize_membership_status(membership_status),
                        int(last_sync_at or 0),
                        int(last_sync_at or 0),
                        now,
                        safe_group_id,
                    ),
                )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def mark_member_addressed(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        now = _now()
        with self._lock, self._session() as connection:
            if safe_character_id:
                existing = self._fetch_member_persona(connection, safe_character_id, safe_group_id, safe_user_id) or {}
                connection.execute(
                    """
                    INSERT INTO member_persona_state (
                        character_id, group_id, user_id, profile_summary, affinity_score, notes_count,
                        last_addressed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(character_id, group_id, user_id) DO UPDATE SET
                        last_addressed_at = excluded.last_addressed_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        safe_character_id,
                        safe_group_id,
                        safe_user_id,
                        _normalize_string(existing.get("profile_summary")),
                        _coerce_affinity(existing.get("affinity_score")),
                        int(existing.get("notes_count") or 0),
                        now,
                        int(existing.get("created_at") or now),
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE members
                    SET last_addressed_at = ?, updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (now, now, safe_group_id, safe_user_id),
                )
            shared = self._fetch_member(connection, safe_group_id, safe_user_id)
            if shared is None:
                return None
            persona = None
            if safe_character_id:
                persona = self._fetch_member_persona(connection, safe_character_id, safe_group_id, safe_user_id)
            alias_record = self._fetch_assistant_alias(connection, safe_character_id, safe_user_id) if safe_character_id else None
            return _merge_member_payload(
                shared,
                persona,
                character_id=safe_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def adjust_member_affinity(
        self,
        *,
        group_id: object,
        user_id: object,
        delta: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        now = _now()
        with self._lock, self._session() as connection:
            shared = self._fetch_member(connection, safe_group_id, safe_user_id)
            if shared is None:
                return None
            if safe_character_id:
                existing = self._fetch_member_persona(connection, safe_character_id, safe_group_id, safe_user_id) or {}
                next_affinity = _coerce_affinity(float(existing.get("affinity_score") or 0.0) + float(delta or 0.0))
                connection.execute(
                    """
                    INSERT INTO member_persona_state (
                        character_id, group_id, user_id, profile_summary, affinity_score, notes_count,
                        last_addressed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(character_id, group_id, user_id) DO UPDATE SET
                        affinity_score = excluded.affinity_score,
                        updated_at = excluded.updated_at
                    """,
                    (
                        safe_character_id,
                        safe_group_id,
                        safe_user_id,
                        _normalize_string(existing.get("profile_summary")),
                        next_affinity,
                        int(existing.get("notes_count") or 0),
                        int(existing.get("last_addressed_at") or 0),
                        int(existing.get("created_at") or shared.get("created_at") or now),
                        now,
                    ),
                )
            else:
                next_affinity = _coerce_affinity(float(shared.get("affinity_score") or 0.0) + float(delta or 0.0))
                connection.execute(
                    """
                    UPDATE members
                    SET affinity_score = ?, updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (next_affinity, now, safe_group_id, safe_user_id),
                )
            shared = self._fetch_member(connection, safe_group_id, safe_user_id)
            if shared is None:
                return None
            persona = None
            if safe_character_id:
                persona = self._fetch_member_persona(connection, safe_character_id, safe_group_id, safe_user_id)
            alias_record = self._fetch_assistant_alias(connection, safe_character_id, safe_user_id) if safe_character_id else None
            return _merge_member_payload(
                shared,
                persona,
                character_id=safe_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def get_member(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        safe_character_id = _normalize_character_id(character_id)
        with self._read_session() as connection:
            shared = self._fetch_member(connection, safe_group_id, safe_user_id)
            if shared is None:
                return None
            persona = None
            if safe_character_id:
                persona = self._fetch_member_persona(connection, safe_character_id, safe_group_id, safe_user_id)
            alias_record = self._fetch_assistant_alias(connection, safe_character_id, safe_user_id) if safe_character_id else None
            return _merge_member_payload(
                shared,
                persona,
                character_id=safe_character_id,
                assistant_alias=str((alias_record or {}).get("assistant_alias", "") or ""),
            )

    def list_members(self, *, limit: int = 120, character_id: object = "") -> list[dict[str, Any]]:
        safe_character_id = _normalize_character_id(character_id)
        with self._read_session() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM members
                ORDER BY CASE WHEN membership_status = 'active' THEN 0 ELSE 1 END ASC,
                         last_seen_at DESC, updated_at DESC, group_id ASC, user_id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            persona_rows = {
                (
                    _normalize_string(row["group_id"]),
                    _normalize_string(row["user_id"]),
                ): dict(row)
                for row in (
                    connection.execute(
                        """
                        SELECT *
                        FROM member_persona_state
                        WHERE character_id = ?
                        """,
                        (safe_character_id,),
                    ).fetchall()
                    if safe_character_id
                    else []
                )
            }
            alias_rows = {
                _normalize_string(row["user_id"]): dict(row)
                for row in (
                    connection.execute(
                        """
                        SELECT *
                        FROM assistant_aliases
                        WHERE character_id = ?
                        """,
                        (safe_character_id,),
                    ).fetchall()
                    if safe_character_id
                    else []
                )
            }
        return [
            _merge_member_payload(
                _row_to_member(row),
                persona_rows.get((_normalize_string(row["group_id"]), _normalize_string(row["user_id"]))),
                character_id=safe_character_id,
                assistant_alias=str(
                    (
                        alias_rows.get(_normalize_string(row["user_id"]))
                        or {}
                    ).get("assistant_alias", "")
                    or ""
                ),
            )
            for row in rows
        ]

    def member_count(self) -> int:
        with self._read_session() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM members").fetchone()
        return int(row["count"] if row is not None else 0)

    def count_members_in_group(self, group_id: str) -> int:
        gid = str(group_id or "").strip()
        with self._read_session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM members WHERE group_id = ? AND membership_status = 'active'",
                (gid,),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def count_knowledge_for_scope(self, scope_type: str, scope_id: str, *, character_id: object = "") -> int:
        st = str(scope_type or "").strip()
        sid = str(scope_id or "").strip()
        safe_character_id = _normalize_character_id(character_id)
        with self._read_session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM knowledge_entries
                WHERE character_id = ? AND scope_type = ? AND scope_id = ?
                """,
                (safe_character_id, st, sid),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def count_knowledge_for_scopes(self, scopes: list[tuple[str, str]], *, character_id: object = "") -> int:
        if not scopes:
            return 0
        conditions = " OR ".join("(scope_type = ? AND scope_id = ?)" for _ in scopes)
        params: list[str] = []
        for st, sid in scopes:
            params.extend([str(st or "").strip(), str(sid or "").strip()])
        safe_character_id = _normalize_character_id(character_id)
        with self._read_session() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM knowledge_entries WHERE character_id = ? AND ({conditions})",
                [safe_character_id, *params],
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def save_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope_type, scope_id = _normalize_scope(payload.get("scope_type"), payload.get("scope_id"))
        safe_character_id = _normalize_character_id(payload.get("character_id"))
        summary = _normalize_string(payload.get("summary"))
        if not summary:
            raise ValueError("summary is required")
        now = _now()
        try:
            entry_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            entry_id = 0
        with self._lock, self._session() as connection:
            existing = self._fetch_knowledge(connection, entry_id) if entry_id > 0 else None
            tags = _normalize_tags(payload.get("tags", existing.get("tags") if existing else None))
            embedding_json = _encode_embedding(
                self._resolve_embedding(
                    summary=summary,
                    tags=tags,
                    existing=existing.get("embedding") if existing else None,
                    previous_summary=_normalize_string(existing.get("summary")) if existing else "",
                    previous_tags=_normalize_tags(existing.get("tags")) if existing else [],
                )
            )
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO knowledge_entries (
                        character_id, scope_type, scope_id, memory_type, summary, tags_json, source_message_ids_json,
                        embedding_json, confidence, archived, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        safe_character_id,
                        scope_type,
                        scope_id,
                        _normalize_string(payload.get("memory_type")) or "fact",
                        summary,
                        json.dumps(tags, ensure_ascii=False),
                        json.dumps(_normalize_tags(payload.get("source_message_ids")), ensure_ascii=False),
                        embedding_json,
                        _coerce_confidence(payload.get("confidence")),
                        1 if bool(payload.get("archived", False)) else 0,
                        now,
                        now,
                    ),
                )
                entry_id = int(cursor.lastrowid)
            else:
                connection.execute(
                    """
                    UPDATE knowledge_entries
                    SET character_id = ?, scope_type = ?, scope_id = ?, memory_type = ?, summary = ?, tags_json = ?,
                        source_message_ids_json = ?, embedding_json = ?, confidence = ?, archived = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        safe_character_id or _normalize_character_id(existing.get("character_id")),
                        scope_type,
                        scope_id,
                        _normalize_string(payload.get("memory_type", existing.get("memory_type"))) or "fact",
                        summary,
                        json.dumps(tags, ensure_ascii=False),
                        json.dumps(
                            _normalize_tags(payload.get("source_message_ids", existing.get("source_message_ids"))),
                            ensure_ascii=False,
                        ),
                        embedding_json,
                        _coerce_confidence(payload.get("confidence", existing.get("confidence", 0.5))),
                        1 if bool(payload.get("archived", existing.get("archived", False))) else 0,
                        now,
                        entry_id,
                    ),
                )
            result = self._fetch_knowledge(connection, entry_id)
        return result or {}

    def delete_knowledge(self, entry_id: object, *, character_id: object = "") -> bool:
        try:
            safe_entry_id = int(entry_id or 0)
        except (TypeError, ValueError):
            return False
        if safe_entry_id <= 0:
            return False
        safe_character_id = _normalize_character_id(character_id)
        with self._lock, self._session() as connection:
            existing = self._fetch_knowledge(connection, safe_entry_id)
            if existing is None:
                return False
            if safe_character_id and _normalize_character_id(existing.get("character_id")) != safe_character_id:
                return False
            cursor = connection.execute("DELETE FROM knowledge_entries WHERE id = ?", (safe_entry_id,))
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def add_knowledge(
        self,
        *,
        scope_type: object,
        scope_id: object,
        memory_type: object,
        summary: object,
        tags: list[str] | None = None,
        source_message_ids: list[str] | None = None,
        confidence: float = 0.5,
        archived: bool = False,
        character_id: object = "",
    ) -> dict[str, Any]:
        return self.save_knowledge(
            {
                "character_id": character_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "memory_type": memory_type,
                "summary": summary,
                "tags": tags or [],
                "source_message_ids": source_message_ids or [],
                "confidence": confidence,
                "archived": archived,
            }
        )

    def list_knowledge(self, *, limit: int = 80, character_id: object | None = None) -> list[dict[str, Any]]:
        safe_character_id = None if character_id is None else _normalize_character_id(character_id)
        sql = """
            SELECT *
            FROM knowledge_entries
        """
        params: list[object] = []
        if safe_character_id is not None:
            sql += " WHERE character_id = ?"
            params.append(safe_character_id)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._read_session() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_knowledge(row) for row in rows]

    def recall_knowledge(
        self,
        *,
        scopes: list[tuple[str, str]],
        query: object,
        limit: int = 3,
        character_id: object = "",
    ) -> list[str]:
        query_terms = _extract_terms(query)
        query_embedding = self._embed_query(query)
        safe_character_id = _normalize_character_id(character_id)
        scope_set = {(_normalize_string(kind).lower(), _normalize_string(identifier)) for kind, identifier in scopes}
        scope_set.add(("global", ""))
        scope_list = list(scope_set)
        # Cap the candidate pool to keep scoring O(1) in DB size. A few hundred
        # of the most recent in-scope entries is more than enough for the
        # embedding + keyword rerank to pick the final top-N.
        candidate_limit = max(int(limit) * 20, 200)
        if scope_list:
            conditions = " OR ".join(
                "(LOWER(scope_type) = ? AND scope_id = ?)" for _ in scope_list
            )
            params: list[object] = []
            for kind, identifier in scope_list:
                params.extend([kind, identifier])
            sql = f"""
                SELECT *
                FROM knowledge_entries
                WHERE character_id = ? AND ({conditions})
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
            """
            params = [safe_character_id, *params, candidate_limit]
        else:
            sql = """
                SELECT *
                FROM knowledge_entries
                WHERE character_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
            """
            params = [safe_character_id, candidate_limit]
        with self._read_session() as connection:
            rows = connection.execute(sql, params).fetchall()
        scored: list[tuple[float, str]] = []
        for row in rows:
            entry = _row_to_knowledge(row)
            summary = _normalize_string(entry.get("summary"))
            if not summary:
                continue
            score = self._score_entry(
                summary=summary,
                tags=_normalize_tags(entry.get("tags")),
                entry_embedding=_decode_embedding(entry.get("embedding")),
                query_terms=query_terms,
                query_embedding=query_embedding,
            )
            score += _coerce_confidence(entry.get("confidence")) * 0.18
            if entry.get("archived"):
                score -= 0.15
            scored.append((score, summary))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [summary for _, summary in scored[: max(1, int(limit))]]

    def knowledge_count(self, *, character_id: object | None = None) -> int:
        safe_character_id = None if character_id is None else _normalize_character_id(character_id)
        with self._read_session() as connection:
            if safe_character_id is None:
                row = connection.execute("SELECT COUNT(*) AS count FROM knowledge_entries").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM knowledge_entries WHERE character_id = ?",
                    (safe_character_id,),
                ).fetchone()
        return int(row["count"] if row is not None else 0)

    def embedded_knowledge_count(self, *, character_id: object | None = None) -> int:
        safe_character_id = None if character_id is None else _normalize_character_id(character_id)
        with self._read_session() as connection:
            if safe_character_id is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM knowledge_entries WHERE embedding_json <> ''"
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM knowledge_entries
                    WHERE character_id = ? AND embedding_json <> ''
                    """,
                    (safe_character_id,),
                ).fetchone()
        return int(row["count"] if row is not None else 0)

    def refresh_knowledge_embeddings(self) -> int:
        with self._lock, self._session() as connection:
            rows = [
                {
                    "id": int(row["id"] or 0),
                    "summary": row["summary"],
                    "tags_json": row["tags_json"],
                    "embedding_json": row["embedding_json"],
                }
                for row in connection.execute(
                    """
                    SELECT id, summary, tags_json, embedding_json
                    FROM knowledge_entries
                    ORDER BY updated_at DESC, id DESC
                    """
                ).fetchall()
            ]
        pending_updates: list[tuple[int, str]] = []
        for row in rows:
            if _decode_embedding(row["embedding_json"]):
                continue
            summary = _normalize_string(row["summary"])
            tags = _decode_json_list(row["tags_json"])
            vector = self._embed_text(_embedding_input(summary, tags))
            if not vector:
                continue
            pending_updates.append((int(row["id"] or 0), _encode_embedding(vector)))
        updated = 0
        if not pending_updates:
            return updated
        with self._lock, self._session() as connection:
            for entry_id, encoded_vector in pending_updates:
                current = connection.execute(
                    "SELECT embedding_json FROM knowledge_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if current is None or _decode_embedding(current["embedding_json"]):
                    continue
                connection.execute(
                    "UPDATE knowledge_entries SET embedding_json = ? WHERE id = ?",
                    (encoded_vector, entry_id),
                )
                updated += 1
        return updated

    def _fetch_member(
        self,
        connection: sqlite3.Connection,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT *
            FROM members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        ).fetchone()
        return _row_to_member(row) if row is not None else None

    def _fetch_member_persona(
        self,
        connection: sqlite3.Connection,
        character_id: str,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT *
            FROM member_persona_state
            WHERE character_id = ? AND group_id = ? AND user_id = ?
            """,
            (character_id, group_id, user_id),
        ).fetchone()
        return _row_to_member_persona(row) if row is not None else None

    def _fetch_assistant_alias(
        self,
        connection: sqlite3.Connection,
        character_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT *
            FROM assistant_aliases
            WHERE character_id = ? AND user_id = ?
            """,
            (character_id, user_id),
        ).fetchone()
        return _row_to_assistant_alias(row) if row is not None else None

    def _fetch_knowledge(self, connection: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT *
            FROM knowledge_entries
            WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
        return _row_to_knowledge(row) if row is not None else None

    def _resolve_embedding(
        self,
        *,
        summary: str,
        tags: list[str],
        existing: object,
        previous_summary: str,
        previous_tags: list[str],
    ) -> list[float]:
        current = _decode_embedding(existing)
        if current and summary == previous_summary and tags == previous_tags:
            return current
        return self._embed_text(_embedding_input(summary, tags)) or current

    def _embed_query(self, query: object) -> list[float]:
        return self._embed_text(_normalize_string(query))

    def _embed_text(self, text: str) -> list[float]:
        if self._embedder is None or not getattr(self._embedder, "ready", False):
            return []
        try:
            return _normalize_vector(self._embedder.embed(text))
        except Exception:
            return []

    @staticmethod
    def _score_entry(
        *,
        summary: str,
        tags: list[str],
        entry_embedding: list[float],
        query_terms: list[str],
        query_embedding: list[float],
    ) -> float:
        lexical_score = _score_knowledge(summary, tags, query_terms)
        if entry_embedding and query_embedding:
            semantic_score = _cosine_similarity(entry_embedding, query_embedding)
            return max(semantic_score * 4.0, lexical_score)
        return lexical_score


def _row_to_member(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": _normalize_string(row["group_id"]),
        "user_id": _normalize_string(row["user_id"]),
        "qq_nickname": _normalize_string(row["qq_nickname"]),
        "group_card": _normalize_string(row["group_card"]),
        "preferred_name": _normalize_string(row["preferred_name"]),
        "onboarding_status": _normalize_string(row["onboarding_status"]) or "new",
        "profile_summary": _normalize_string(row["profile_summary"]),
        "membership_status": _normalize_membership_status(row["membership_status"] if "membership_status" in row.keys() else "active"),
        "affinity_score": _coerce_affinity(row["affinity_score"] if "affinity_score" in row.keys() else 0.0),
        "notes_count": int(row["notes_count"] or 0),
        "last_seen_at": int(row["last_seen_at"] or 0),
        "last_sync_at": int(row["last_sync_at"] if "last_sync_at" in row.keys() else 0),
        "last_addressed_at": int(row["last_addressed_at"] or 0),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def _row_to_member_persona(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "character_id": _normalize_character_id(row["character_id"] if "character_id" in row.keys() else ""),
        "group_id": _normalize_string(row["group_id"]),
        "user_id": _normalize_string(row["user_id"]),
        "profile_summary": _normalize_string(row["profile_summary"]),
        "affinity_score": _coerce_affinity(row["affinity_score"]),
        "notes_count": int(row["notes_count"] or 0),
        "last_addressed_at": int(row["last_addressed_at"] or 0),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def _row_to_assistant_alias(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "character_id": _normalize_character_id(row["character_id"] if "character_id" in row.keys() else ""),
        "user_id": _normalize_string(row["user_id"]),
        "assistant_alias": _normalize_string(row["assistant_alias"]),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def _normalize_skill_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    details = payload.get("details")
    if isinstance(details, dict):
        details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        details_json = _normalize_string(payload.get("details_json")) or "{}"
    created_at = payload.get("created_at", payload.get("timestamp", 0))
    try:
        resolved_created_at = int(float(created_at or 0))
    except (TypeError, ValueError):
        resolved_created_at = _now()
    if resolved_created_at <= 0:
        resolved_created_at = _now()
    return {
        "id": int(payload.get("id") or 0),
        "trace_id": _normalize_string(payload.get("trace_id")),
        "skill_id": _normalize_string(payload.get("skill_id")) or "__unknown__",
        "tool_id": _normalize_string(payload.get("tool_id")),
        "trigger_source": _normalize_string(payload.get("trigger_source")) or "system",
        "caller": _normalize_string(payload.get("caller")),
        "status": _normalize_string(payload.get("status")) or "error",
        "error_code": _normalize_string(payload.get("error_code")),
        "error": _normalize_string(payload.get("error"))[:1000],
        "latency_ms": int(payload.get("latency_ms") or 0),
        "arg_summary": _normalize_string(payload.get("arg_summary"))[:1000],
        "result_summary": _normalize_string(payload.get("result_summary"))[:1000],
        "details_json": details_json,
        "created_at": resolved_created_at,
    }


def _row_to_skill_telemetry(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if row is None:
        return {}
    payload = {
        "id": row["id"] if "id" in row.keys() else 0,
        "trace_id": row["trace_id"] if "trace_id" in row.keys() else "",
        "skill_id": row["skill_id"] if "skill_id" in row.keys() else "",
        "tool_id": row["tool_id"] if "tool_id" in row.keys() else "",
        "trigger_source": row["trigger_source"] if "trigger_source" in row.keys() else "",
        "caller": row["caller"] if "caller" in row.keys() else "",
        "status": row["status"] if "status" in row.keys() else "",
        "error_code": row["error_code"] if "error_code" in row.keys() else "",
        "error": row["error"] if "error" in row.keys() else "",
        "latency_ms": row["latency_ms"] if "latency_ms" in row.keys() else 0,
        "arg_summary": row["arg_summary"] if "arg_summary" in row.keys() else "",
        "result_summary": row["result_summary"] if "result_summary" in row.keys() else "",
        "details_json": row["details_json"] if "details_json" in row.keys() else "{}",
        "created_at": row["created_at"] if "created_at" in row.keys() else 0,
    }
    normalized = _normalize_skill_telemetry(payload)
    try:
        normalized["details"] = json.loads(str(normalized.get("details_json") or "{}"))
    except json.JSONDecodeError:
        normalized["details"] = {}
    return normalized


def _skill_telemetry_summary_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    calls = len(rows)
    success = sum(1 for row in rows if row.get("status") == "ok")
    failure = max(0, calls - success)
    ordered = sorted(rows, key=lambda row: (int(row.get("created_at") or 0), int(row.get("id") or 0)))
    return {
        "calls": calls,
        "success": success,
        "failure": failure,
        "success_rate": round(success / calls, 4) if calls else 0.0,
        "last": dict(ordered[-1]) if ordered else {},
        "never_succeeded": calls > 0 and success == 0,
    }


def _row_to_knowledge(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        raw_embedding = row.get("embedding_json", row.get("embedding"))
    else:
        raw_embedding = row["embedding_json"] if "embedding_json" in row.keys() else []
    embedding = _decode_embedding(raw_embedding)
    return {
        "id": int(row["id"] or 0),
        "character_id": _normalize_character_id(row["character_id"] if "character_id" in row.keys() else ""),
        "scope_type": _normalize_string(row["scope_type"]) or "global",
        "scope_id": _normalize_string(row["scope_id"]),
        "memory_type": _normalize_string(row["memory_type"]) or "fact",
        "summary": _normalize_string(row["summary"]),
        "tags": _decode_json_list(row["tags_json"] if "tags_json" in row.keys() else row["tags"]),
        "source_message_ids": _decode_json_list(
            row["source_message_ids_json"] if "source_message_ids_json" in row.keys() else row["source_message_ids"]
        ),
        "embedding": embedding,
        "has_embedding": bool(embedding),
        "embedding_dimensions": len(embedding),
        "confidence": _coerce_confidence(row["confidence"]),
        "archived": bool(row["archived"]),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }
