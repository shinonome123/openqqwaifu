from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.schema_migrations import MIGRATIONS, Migration, current_version, run_migrations


class SchemaMigrationTests(unittest.TestCase):
    def test_baseline_on_fresh_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            conn = sqlite3.connect(path)
            try:
                version = run_migrations(conn)
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertEqual(version, 4)
                self.assertIn("schema_version", tables)
                self.assertIn("members", tables)
                self.assertIn("member_persona_state", tables)
                self.assertIn("assistant_aliases", tables)
                self.assertIn("skill_telemetry", tables)
                self.assertIn("knowledge_entries", tables)
                row = conn.execute(
                    "SELECT version, description FROM schema_version ORDER BY version DESC"
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row[0], 4)
                self.assertEqual(row[1], "halve affinity for per-conversation bonds")
            finally:
                conn.close()

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            conn = sqlite3.connect(path)
            try:
                first = run_migrations(conn)
                second = run_migrations(conn)
                version_rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
                self.assertEqual(first, 4)
                self.assertEqual(second, 4)
                self.assertIsNotNone(version_rows)
                assert version_rows is not None
                self.assertEqual(version_rows[0], 4)
            finally:
                conn.close()

    def test_affinity_counts_for_half_once_bonds_grow_per_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = sqlite3.connect(Path(tmpdir) / "runtime.sqlite3")
            try:
                run_migrations(conn, migrations=MIGRATIONS[:3])
                conn.execute("INSERT INTO members (group_id, user_id, affinity_score) VALUES ('1', '2', 1.0)")
                conn.execute(
                    "INSERT INTO member_persona_state (character_id, group_id, user_id, affinity_score) "
                    "VALUES ('aurora', '1', '2', 0.6)"
                )
                conn.commit()
                run_migrations(conn)
                run_migrations(conn)  # once only
                self.assertEqual(conn.execute("SELECT affinity_score FROM members").fetchone()[0], 0.5)
                self.assertEqual(conn.execute("SELECT affinity_score FROM member_persona_state").fetchone()[0], 0.3)
            finally:
                conn.close()

    def test_transaction_rollback_on_failure(self) -> None:
        def fail_after_create(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE doomed_table (value TEXT NOT NULL)")
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            conn = sqlite3.connect(path)
            try:
                with self.assertRaises(RuntimeError):
                    run_migrations(
                        conn,
                        migrations=[Migration(version=1, description="broken migration", apply=fail_after_create)],
                    )
                self.assertEqual(current_version(conn), 0)
                doomed = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'doomed_table'"
                ).fetchone()
                self.assertIsNone(doomed)
            finally:
                conn.close()

    def test_existing_database_gets_version_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    """
                    CREATE TABLE members (
                        group_id TEXT NOT NULL DEFAULT '',
                        user_id TEXT NOT NULL,
                        qq_nickname TEXT NOT NULL DEFAULT '',
                        group_card TEXT NOT NULL DEFAULT '',
                        preferred_name TEXT NOT NULL DEFAULT '',
                        onboarding_status TEXT NOT NULL DEFAULT 'new',
                        profile_summary TEXT NOT NULL DEFAULT '',
                        notes_count INTEGER NOT NULL DEFAULT 0,
                        last_seen_at INTEGER NOT NULL DEFAULT 0,
                        last_addressed_at INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (group_id, user_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE member_persona_state (
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
                    CREATE TABLE knowledge_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope_type TEXT NOT NULL,
                        scope_id TEXT NOT NULL DEFAULT '',
                        memory_type TEXT NOT NULL DEFAULT 'fact',
                        summary TEXT NOT NULL,
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        source_message_ids_json TEXT NOT NULL DEFAULT '[]',
                        confidence REAL NOT NULL DEFAULT 0.5,
                        archived INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.commit()

                version = run_migrations(conn)

                self.assertEqual(version, 4)
                self.assertEqual(current_version(conn), 4)
                member_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(members)").fetchall()
                }
                knowledge_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(knowledge_entries)").fetchall()
                }
                self.assertIn("affinity_score", member_columns)
                self.assertIn("membership_status", member_columns)
                self.assertIn("last_sync_at", member_columns)
                self.assertIn("character_id", knowledge_columns)
                self.assertIn("embedding_json", knowledge_columns)
                alias_tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'assistant_aliases'"
                    ).fetchall()
                }
                self.assertIn("assistant_aliases", alias_tables)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
