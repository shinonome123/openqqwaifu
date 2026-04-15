from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


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


class InMemoryRuntimeStateStore:
    def __init__(self, embedder: Any = None) -> None:
        self._members: dict[tuple[str, str], dict[str, Any]] = {}
        self._knowledge: dict[int, dict[str, Any]] = {}
        self._knowledge_id = 1
        self._lock = threading.Lock()
        self._embedder = embedder

    def set_embedder(self, embedder: Any) -> None:
        self._embedder = embedder

    def record_member_seen(
        self,
        *,
        group_id: object,
        user_id: object,
        qq_nickname: object = "",
        group_card: object = "",
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
                "profile_summary": _normalize_string(existing.get("profile_summary")),
                "affinity_score": _coerce_affinity(existing.get("affinity_score")),
                "notes_count": int(existing.get("notes_count") or 0),
                "last_seen_at": now,
                "last_addressed_at": int(existing.get("last_addressed_at") or 0),
                "created_at": created_at,
                "updated_at": now,
            }
            self._members[key] = member
            return dict(member)

    def save_member(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe_group_id, safe_user_id = _normalize_member(payload.get("group_id"), payload.get("user_id"))
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
                "profile_summary": _normalize_string(payload.get("profile_summary", existing.get("profile_summary"))),
                "affinity_score": _coerce_affinity(payload.get("affinity_score", existing.get("affinity_score"))),
                "notes_count": int(payload.get("notes_count", existing.get("notes_count") or 0) or 0),
                "last_seen_at": int(payload.get("last_seen_at", existing.get("last_seen_at") or 0) or 0),
                "last_addressed_at": int(payload.get("last_addressed_at", existing.get("last_addressed_at") or 0) or 0),
                "created_at": int(existing.get("created_at") or now),
                "updated_at": now,
            }
            self._members[key] = member
            return dict(member)

    def mark_member_addressed(self, *, group_id: object, user_id: object) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        now = _now()
        key = (safe_group_id, safe_user_id)
        with self._lock:
            existing = self._members.get(key)
            if existing is None:
                return None
            member = dict(existing)
            member["last_addressed_at"] = now
            member["updated_at"] = now
            self._members[key] = member
            return dict(member)

    def adjust_member_affinity(self, *, group_id: object, user_id: object, delta: object) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        key = (safe_group_id, safe_user_id)
        now = _now()
        with self._lock:
            existing = self._members.get(key)
            if existing is None:
                return None
            member = dict(existing)
            member["affinity_score"] = _coerce_affinity(float(member.get("affinity_score") or 0.0) + float(delta or 0.0))
            member["updated_at"] = now
            self._members[key] = member
            return dict(member)

    def get_member(self, *, group_id: object, user_id: object) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        with self._lock:
            member = self._members.get((safe_group_id, safe_user_id))
            return dict(member) if member is not None else None

    def list_members(self, *, limit: int = 120) -> list[dict[str, Any]]:
        with self._lock:
            members = [dict(item) for item in self._members.values()]
        members.sort(
            key=lambda item: (
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
            )

    def count_knowledge_for_scope(self, scope_type: str, scope_id: str) -> int:
        st = str(scope_type or "").strip()
        sid = str(scope_id or "").strip()
        with self._lock:
            return sum(
                1
                for entry in self._knowledge.values()
                if str(entry.get("scope_type", "") or "").strip() == st
                and str(entry.get("scope_id", "") or "").strip() == sid
            )

    def count_knowledge_for_scopes(self, scopes: list[tuple[str, str]]) -> int:
        scope_set = {(str(st or "").strip(), str(sid or "").strip()) for st, sid in scopes}
        with self._lock:
            return sum(
                1
                for entry in self._knowledge.values()
                if (
                    str(entry.get("scope_type", "") or "").strip(),
                    str(entry.get("scope_id", "") or "").strip(),
                ) in scope_set
            )

    def save_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope_type, scope_id = _normalize_scope(payload.get("scope_type"), payload.get("scope_id"))
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
    ) -> dict[str, Any]:
        return self.save_knowledge(
            {
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

    def list_knowledge(self, *, limit: int = 80) -> list[dict[str, Any]]:
        with self._lock:
            entries = [dict(item) for item in self._knowledge.values()]
        entries.sort(
            key=lambda item: (
                -int(item.get("updated_at") or 0),
                -int(item.get("id") or 0),
            )
        )
        return entries[: max(1, int(limit))]

    def recall_knowledge(self, *, scopes: list[tuple[str, str]], query: object, limit: int = 3) -> list[str]:
        query_terms = _extract_terms(query)
        query_embedding = self._embed_query(query)
        scope_set = {(_normalize_string(kind).lower(), _normalize_string(identifier)) for kind, identifier in scopes}
        scope_set.add(("global", ""))
        with self._lock:
            entries = [dict(item) for item in self._knowledge.values()]
        scored: list[tuple[float, str]] = []
        for entry in entries:
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

    def knowledge_count(self) -> int:
        with self._lock:
            return len(self._knowledge)

    def embedded_knowledge_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._knowledge.values() if _decode_embedding(item.get("embedding")))

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
        self._lock = threading.Lock()
        self._embedder = embedder
        self._init_schema()

    def set_embedder(self, embedder: Any) -> None:
        self._embedder = embedder

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS members (
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    qq_nickname TEXT NOT NULL DEFAULT '',
                    group_card TEXT NOT NULL DEFAULT '',
                    preferred_name TEXT NOT NULL DEFAULT '',
                    onboarding_status TEXT NOT NULL DEFAULT 'new',
                    profile_summary TEXT NOT NULL DEFAULT '',
                    affinity_score REAL NOT NULL DEFAULT 0,
                    notes_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at INTEGER NOT NULL DEFAULT 0,
                    last_addressed_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS knowledge_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL DEFAULT '',
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    embedding_json TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_members_seen
                ON members (last_seen_at DESC, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_knowledge_scope
                ON knowledge_entries (scope_type, scope_id, updated_at DESC);
                """
            )
            self._ensure_column(connection, "knowledge_entries", "embedding_json", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "members", "affinity_score", "REAL NOT NULL DEFAULT 0")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
        columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row["name"]) for row in columns}
        if column_name in existing:
            return
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def record_member_seen(
        self,
        *,
        group_id: object,
        user_id: object,
        qq_nickname: object = "",
        group_card: object = "",
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
                    profile_summary, affinity_score, notes_count, last_seen_at, last_addressed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    qq_nickname = CASE WHEN excluded.qq_nickname <> '' THEN excluded.qq_nickname ELSE members.qq_nickname END,
                    group_card = CASE WHEN excluded.group_card <> '' THEN excluded.group_card ELSE members.group_card END,
                    last_seen_at = excluded.last_seen_at,
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
                    _coerce_affinity(existing.get("affinity_score")) if existing else 0.0,
                    int(existing.get("notes_count") or 0) if existing else 0,
                    now,
                    int(existing.get("last_addressed_at") or 0) if existing else 0,
                    created_at,
                    now,
                ),
            )
            return self._fetch_member(connection, safe_group_id, safe_user_id) or {}

    def save_member(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe_group_id, safe_user_id = _normalize_member(payload.get("group_id"), payload.get("user_id"))
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
                    profile_summary, affinity_score, notes_count, last_seen_at, last_addressed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    qq_nickname = excluded.qq_nickname,
                    group_card = excluded.group_card,
                    preferred_name = excluded.preferred_name,
                    onboarding_status = excluded.onboarding_status,
                    profile_summary = excluded.profile_summary,
                    affinity_score = excluded.affinity_score,
                    notes_count = excluded.notes_count,
                    last_seen_at = excluded.last_seen_at,
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
                    _normalize_string(payload.get("profile_summary", existing.get("profile_summary"))),
                    _coerce_affinity(payload.get("affinity_score", existing.get("affinity_score"))),
                    int(payload.get("notes_count", existing.get("notes_count") or 0) or 0),
                    int(payload.get("last_seen_at", existing.get("last_seen_at") or 0) or 0),
                    int(payload.get("last_addressed_at", existing.get("last_addressed_at") or 0) or 0),
                    int(existing.get("created_at") or now),
                    now,
                ),
            )
            return self._fetch_member(connection, safe_group_id, safe_user_id) or {}

    def mark_member_addressed(self, *, group_id: object, user_id: object) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        now = _now()
        with self._lock, self._session() as connection:
            connection.execute(
                """
                UPDATE members
                SET last_addressed_at = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (now, now, safe_group_id, safe_user_id),
            )
            return self._fetch_member(connection, safe_group_id, safe_user_id)

    def adjust_member_affinity(self, *, group_id: object, user_id: object, delta: object) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        now = _now()
        with self._lock, self._session() as connection:
            existing = self._fetch_member(connection, safe_group_id, safe_user_id)
            if existing is None:
                return None
            next_affinity = _coerce_affinity(float(existing.get("affinity_score") or 0.0) + float(delta or 0.0))
            connection.execute(
                """
                UPDATE members
                SET affinity_score = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (next_affinity, now, safe_group_id, safe_user_id),
            )
            return self._fetch_member(connection, safe_group_id, safe_user_id)

    def get_member(self, *, group_id: object, user_id: object) -> dict[str, Any] | None:
        safe_group_id, safe_user_id = _normalize_member(group_id, user_id)
        with self._lock, self._session() as connection:
            return self._fetch_member(connection, safe_group_id, safe_user_id)

    def list_members(self, *, limit: int = 120) -> list[dict[str, Any]]:
        with self._lock, self._session() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM members
                ORDER BY last_seen_at DESC, updated_at DESC, group_id ASC, user_id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_row_to_member(row) for row in rows]

    def member_count(self) -> int:
        with self._lock, self._session() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM members").fetchone()
        return int(row["count"] if row is not None else 0)

    def count_members_in_group(self, group_id: str) -> int:
        gid = str(group_id or "").strip()
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM members WHERE group_id = ?",
                (gid,),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def count_knowledge_for_scope(self, scope_type: str, scope_id: str) -> int:
        st = str(scope_type or "").strip()
        sid = str(scope_id or "").strip()
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM knowledge_entries WHERE scope_type = ? AND scope_id = ?",
                (st, sid),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def count_knowledge_for_scopes(self, scopes: list[tuple[str, str]]) -> int:
        if not scopes:
            return 0
        conditions = " OR ".join("(scope_type = ? AND scope_id = ?)" for _ in scopes)
        params: list[str] = []
        for st, sid in scopes:
            params.extend([str(st or "").strip(), str(sid or "").strip()])
        with self._lock, self._session() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM knowledge_entries WHERE {conditions}",
                params,
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def save_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope_type, scope_id = _normalize_scope(payload.get("scope_type"), payload.get("scope_id"))
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
                        scope_type, scope_id, memory_type, summary, tags_json, source_message_ids_json,
                        embedding_json, confidence, archived, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
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
                    SET scope_type = ?, scope_id = ?, memory_type = ?, summary = ?, tags_json = ?,
                        source_message_ids_json = ?, embedding_json = ?, confidence = ?, archived = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
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
    ) -> dict[str, Any]:
        return self.save_knowledge(
            {
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

    def list_knowledge(self, *, limit: int = 80) -> list[dict[str, Any]]:
        with self._lock, self._session() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM knowledge_entries
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_row_to_knowledge(row) for row in rows]

    def recall_knowledge(self, *, scopes: list[tuple[str, str]], query: object, limit: int = 3) -> list[str]:
        query_terms = _extract_terms(query)
        query_embedding = self._embed_query(query)
        scope_set = {(_normalize_string(kind).lower(), _normalize_string(identifier)) for kind, identifier in scopes}
        scope_set.add(("global", ""))
        scope_list = list(scope_set)
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
                WHERE {conditions}
                ORDER BY updated_at DESC, id DESC
            """
        else:
            sql = """
                SELECT *
                FROM knowledge_entries
                ORDER BY updated_at DESC, id DESC
            """
            params = []
        with self._lock, self._session() as connection:
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

    def knowledge_count(self) -> int:
        with self._lock, self._session() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM knowledge_entries").fetchone()
        return int(row["count"] if row is not None else 0)

    def embedded_knowledge_count(self) -> int:
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM knowledge_entries WHERE embedding_json <> ''"
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
        "affinity_score": _coerce_affinity(row["affinity_score"] if "affinity_score" in row.keys() else 0.0),
        "notes_count": int(row["notes_count"] or 0),
        "last_seen_at": int(row["last_seen_at"] or 0),
        "last_addressed_at": int(row["last_addressed_at"] or 0),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def _row_to_knowledge(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        raw_embedding = row.get("embedding_json", row.get("embedding"))
    else:
        raw_embedding = row["embedding_json"] if "embedding_json" in row.keys() else []
    embedding = _decode_embedding(raw_embedding)
    return {
        "id": int(row["id"] or 0),
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
