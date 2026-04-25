"""Schema-version driven migrations for the runtime sqlite database.

Replaces the ad-hoc ``_ensure_column`` calls in ``state_store.py``.
Each migration is a module-level ``Migration`` with an integer version,
a short description and an ``apply(conn)`` callback that runs inside a
single transaction. The runner records the highest applied version in a
``schema_version`` table and fast-skips on subsequent boots.

Adding a new migration: append to ``MIGRATIONS``. Never mutate a
migration that has already shipped -- that breaks every existing deploy.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _column_names(rows: Iterable[sqlite3.Row | tuple[object, ...]]) -> set[str]:
    names: set[str] = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            names.add(str(row["name"]))
        elif len(row) > 1:
            names.add(str(row[1]))
    return names


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if column_name in _column_names(columns):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _migration_001_baseline(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS members (
            group_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            qq_nickname TEXT NOT NULL DEFAULT '',
            group_card TEXT NOT NULL DEFAULT '',
            preferred_name TEXT NOT NULL DEFAULT '',
            onboarding_status TEXT NOT NULL DEFAULT 'new',
            profile_summary TEXT NOT NULL DEFAULT '',
            membership_status TEXT NOT NULL DEFAULT 'active',
            affinity_score REAL NOT NULL DEFAULT 0,
            notes_count INTEGER NOT NULL DEFAULT 0,
            last_seen_at INTEGER NOT NULL DEFAULT 0,
            last_sync_at INTEGER NOT NULL DEFAULT 0,
            last_addressed_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (group_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_persona_state (
            character_id TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            profile_summary TEXT NOT NULL DEFAULT '',
            affinity_score REAL NOT NULL DEFAULT 0,
            notes_count INTEGER NOT NULL DEFAULT 0,
            last_addressed_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (character_id, group_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL DEFAULT '',
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
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_members_seen
        ON members (last_seen_at DESC, updated_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_member_persona_updated
        ON member_persona_state (character_id, updated_at DESC)
        """
    )
    _ensure_column(conn, "knowledge_entries", "embedding_json", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "knowledge_entries", "character_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "members", "affinity_score", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "members", "membership_status", "TEXT NOT NULL DEFAULT 'active'")
    _ensure_column(conn, "members", "last_sync_at", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_scope
        ON knowledge_entries (character_id, scope_type, scope_id, updated_at DESC)
        """
    )


def _migration_002_assistant_aliases(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_aliases (
            character_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            assistant_alias TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (character_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_assistant_aliases_updated
        ON assistant_aliases (character_id, updated_at DESC)
        """
    )


def _migration_003_skill_telemetry(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL DEFAULT '',
            skill_id TEXT NOT NULL DEFAULT '',
            tool_id TEXT NOT NULL DEFAULT '',
            trigger_source TEXT NOT NULL DEFAULT '',
            caller TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            arg_summary TEXT NOT NULL DEFAULT '',
            result_summary TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_skill_telemetry_skill_created
        ON skill_telemetry (skill_id, created_at DESC, id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_skill_telemetry_status
        ON skill_telemetry (status, created_at DESC)
        """
    )


MIGRATIONS: list[Migration] = [
    Migration(version=1, description="baseline schema", apply=_migration_001_baseline),
    Migration(version=2, description="assistant alias state", apply=_migration_002_assistant_aliases),
    Migration(version=3, description="skill telemetry events", apply=_migration_003_skill_telemetry),
]


def ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    if row is None:
        return 0
    if isinstance(row, sqlite3.Row):
        value = row[0]
    else:
        value = row[0]
    return int(value or 0)


def _validated_migrations(migrations: list[Migration]) -> list[Migration]:
    ordered = sorted(migrations, key=lambda migration: migration.version)
    versions = [migration.version for migration in ordered]
    if any(version <= 0 for version in versions):
        raise ValueError("migration versions must be positive integers")
    if len(set(versions)) != len(versions):
        raise ValueError("migration versions must be unique")
    return ordered


def run_migrations(conn: sqlite3.Connection, *, migrations: list[Migration] | None = None) -> int:
    """Apply pending migrations inside transactions.

    Returns the highest applied version.
    """

    ensure_schema_version_table(conn)
    conn.commit()
    current = current_version(conn)
    ordered = _validated_migrations(MIGRATIONS if migrations is None else migrations)
    pending = [migration for migration in ordered if migration.version > current]
    if not pending:
        return current
    for migration in pending:
        _LOGGER.info("applying schema migration %d: %s", migration.version, migration.description)
        try:
            conn.execute("BEGIN")
            migration.apply(conn)
            conn.execute(
                """
                INSERT INTO schema_version(version, description, applied_at)
                VALUES (?, ?, CAST(strftime('%s', 'now') AS INTEGER))
                """,
                (migration.version, migration.description),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            _LOGGER.exception("schema migration %d failed", migration.version)
            raise
    return ordered[-1].version if ordered else current
