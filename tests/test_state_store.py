from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.state_store import InMemoryRuntimeStateStore, SqliteRuntimeStateStore


class FakeEmbedder:
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = {key: list(value) for key, value in mapping.items()}
        self.ready = True

    def embed(self, text: str) -> list[float]:
        cleaned = " ".join(str(text or "").split())
        return list(self.mapping.get(cleaned, self.mapping.get("__default__", [])))


class BlockingEmbedder:
    def __init__(self, vector: list[float]) -> None:
        self.vector = list(vector)
        self.ready = True
        self.started = threading.Event()
        self.release = threading.Event()

    def embed(self, text: str) -> list[float]:
        self.started.set()
        self.release.wait(timeout=1)
        return list(self.vector)


class RuntimeStateStoreTests(unittest.TestCase):
    def test_in_memory_store_round_trips_member_and_knowledge(self) -> None:
        store = InMemoryRuntimeStateStore()

        member = store.record_member_seen(group_id="612475113", user_id="783190298", qq_nickname="tester")
        self.assertEqual(member["onboarding_status"], "new")
        self.assertEqual(member["membership_status"], "active")

        saved_member = store.save_member(
            {
                "group_id": "612475113",
                "user_id": "783190298",
                "qq_nickname": "tester",
                "preferred_name": "luna",
                "onboarding_status": "ready",
                "profile_summary": "likes cats",
                "last_sync_at": 123,
            }
        )
        self.assertEqual(saved_member["preferred_name"], "luna")
        self.assertEqual(saved_member["last_sync_at"], 123)
        self.assertEqual(store.member_count(), 1)

        entry = store.add_knowledge(
            scope_type="member",
            scope_id="612475113:783190298",
            memory_type="preference",
            summary="luna likes cats and rainy nights",
            tags=["cats", "rain"],
            confidence=0.9,
        )
        self.assertGreater(entry["id"], 0)
        recalled = store.recall_knowledge(
            scopes=[("member", "612475113:783190298")],
            query="cats",
            limit=3,
        )
        self.assertEqual(recalled, ["luna likes cats and rainy nights"])
        adjusted = store.adjust_member_affinity(group_id="612475113", user_id="783190298", delta=0.18)
        self.assertIsNotNone(adjusted)
        assert adjusted is not None
        self.assertAlmostEqual(float(adjusted["affinity_score"]), 0.18, places=4)

    def test_mark_group_members_missing_updates_membership_status(self) -> None:
        store = InMemoryRuntimeStateStore()
        store.save_member({"group_id": "612475113", "user_id": "1", "qq_nickname": "one", "membership_status": "active"})
        store.save_member({"group_id": "612475113", "user_id": "2", "qq_nickname": "two", "membership_status": "active"})

        changed = store.mark_group_members_missing(
            group_id="612475113",
            active_user_ids=["1"],
            membership_status="left",
            last_sync_at=456,
        )

        self.assertEqual(changed, 1)
        active = store.get_member(group_id="612475113", user_id="1")
        missing = store.get_member(group_id="612475113", user_id="2")
        self.assertIsNotNone(active)
        self.assertIsNotNone(missing)
        assert active is not None and missing is not None
        self.assertEqual(active["membership_status"], "active")
        self.assertEqual(missing["membership_status"], "left")
        self.assertEqual(missing["last_sync_at"], 456)

    def test_sqlite_store_persists_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            store = SqliteRuntimeStateStore(path)
            store.record_member_seen(group_id="612475113", user_id="783190298", qq_nickname="tester")
            store.save_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "preferred_name": "luna",
                    "onboarding_status": "ready",
                }
            )
            store.add_knowledge(
                scope_type="group",
                scope_id="612475113",
                memory_type="fact",
                summary="group 612475113 often talks about games",
                tags=["games"],
                confidence=0.7,
            )

            reopened = SqliteRuntimeStateStore(path)
            member = reopened.get_member(group_id="612475113", user_id="783190298")
            self.assertIsNotNone(member)
            assert member is not None
            self.assertEqual(member["preferred_name"], "luna")
            reopened.adjust_member_affinity(group_id="612475113", user_id="783190298", delta=0.25)
            refreshed = reopened.get_member(group_id="612475113", user_id="783190298")
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertAlmostEqual(float(refreshed["affinity_score"]), 0.25, places=4)
            recalled = reopened.recall_knowledge(
                scopes=[("group", "612475113")],
                query="games",
                limit=3,
            )
            self.assertEqual(recalled, ["group 612475113 often talks about games"])

    def test_persona_state_is_isolated_by_character(self) -> None:
        store = InMemoryRuntimeStateStore()
        store.record_member_seen(group_id="612475113", user_id="783190298", qq_nickname="tester")
        store.save_member(
            {
                "group_id": "612475113",
                "user_id": "783190298",
                "character_id": "default",
                "profile_summary": "likes cats",
                "affinity_score": 0.4,
            }
        )
        store.save_member(
            {
                "group_id": "612475113",
                "user_id": "783190298",
                "character_id": "aurora",
                "profile_summary": "likes rain",
                "affinity_score": -0.2,
            }
        )
        store.add_knowledge(
            scope_type="member",
            scope_id="612475113:783190298",
            memory_type="fact",
            summary="default knows cats",
            character_id="default",
        )
        store.add_knowledge(
            scope_type="member",
            scope_id="612475113:783190298",
            memory_type="fact",
            summary="aurora knows rain",
            character_id="aurora",
        )

        default_member = store.get_member(group_id="612475113", user_id="783190298", character_id="default")
        aurora_member = store.get_member(group_id="612475113", user_id="783190298", character_id="aurora")

        self.assertIsNotNone(default_member)
        self.assertIsNotNone(aurora_member)
        assert default_member is not None and aurora_member is not None
        self.assertEqual(default_member["profile_summary"], "likes cats")
        self.assertEqual(aurora_member["profile_summary"], "likes rain")
        self.assertAlmostEqual(float(default_member["affinity_score"]), 0.4, places=4)
        self.assertAlmostEqual(float(aurora_member["affinity_score"]), -0.2, places=4)
        self.assertEqual(
            store.recall_knowledge(
                scopes=[("member", "612475113:783190298")],
                query="cats",
                character_id="default",
            ),
            ["default knows cats"],
        )
        self.assertEqual(
            store.recall_knowledge(
                scopes=[("member", "612475113:783190298")],
                query="rain",
                character_id="aurora",
            ),
            ["aurora knows rain"],
        )

    def test_sqlite_persona_state_is_isolated_by_character(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            store = SqliteRuntimeStateStore(path)
            store.record_member_seen(group_id="612475113", user_id="783190298", qq_nickname="tester")
            store.save_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "character_id": "default",
                    "profile_summary": "likes cats",
                    "affinity_score": 0.4,
                }
            )
            store.save_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "character_id": "aurora",
                    "profile_summary": "likes rain",
                    "affinity_score": -0.2,
                }
            )
            store.add_knowledge(
                scope_type="member",
                scope_id="612475113:783190298",
                memory_type="fact",
                summary="default knows cats",
                character_id="default",
            )
            store.add_knowledge(
                scope_type="member",
                scope_id="612475113:783190298",
                memory_type="fact",
                summary="aurora knows rain",
                character_id="aurora",
            )

            reopened = SqliteRuntimeStateStore(path)
            default_member = reopened.get_member(group_id="612475113", user_id="783190298", character_id="default")
            aurora_member = reopened.get_member(group_id="612475113", user_id="783190298", character_id="aurora")

            self.assertIsNotNone(default_member)
            self.assertIsNotNone(aurora_member)
            assert default_member is not None and aurora_member is not None
            self.assertEqual(default_member["profile_summary"], "likes cats")
            self.assertEqual(aurora_member["profile_summary"], "likes rain")
            self.assertEqual(reopened.knowledge_count(character_id="default"), 1)
            self.assertEqual(reopened.knowledge_count(character_id="aurora"), 1)

    def test_in_memory_store_persists_assistant_alias_by_character_and_user(self) -> None:
        store = InMemoryRuntimeStateStore()
        store.record_member_seen(group_id="612475113", user_id="783190298", qq_nickname="tester")
        saved = store.save_assistant_alias(
            character_id="aurora",
            user_id="783190298",
            assistant_alias="阿璃",
        )

        self.assertEqual(saved["assistant_alias"], "阿璃")
        aurora_member = store.get_member(
            group_id="612475113",
            user_id="783190298",
            character_id="aurora",
        )
        default_member = store.get_member(
            group_id="612475113",
            user_id="783190298",
            character_id="default",
        )

        self.assertIsNotNone(aurora_member)
        self.assertIsNotNone(default_member)
        assert aurora_member is not None and default_member is not None
        self.assertEqual(aurora_member["assistant_alias"], "阿璃")
        self.assertEqual(default_member["assistant_alias"], "")

    def test_sqlite_store_persists_assistant_alias_by_character_and_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            store = SqliteRuntimeStateStore(path)
            store.record_member_seen(group_id="", user_id="783190298", qq_nickname="tester")
            store.save_assistant_alias(
                character_id="aurora",
                user_id="783190298",
                assistant_alias="阿璃",
            )

            reopened = SqliteRuntimeStateStore(path)
            alias = reopened.get_assistant_alias(character_id="aurora", user_id="783190298")
            member = reopened.get_member(group_id="", user_id="783190298", character_id="aurora")

            self.assertIsNotNone(alias)
            self.assertIsNotNone(member)
            assert alias is not None and member is not None
            self.assertEqual(alias["assistant_alias"], "阿璃")
            self.assertEqual(member["assistant_alias"], "阿璃")

    def test_in_memory_store_can_use_embedding_similarity(self) -> None:
        embedder = FakeEmbedder(
            {
                "晴朗的天空带着薄云\nTags: 天气": [1.0, 0.0],
                "蓝天白云": [1.0, 0.0],
                "__default__": [0.0, 1.0],
            }
        )
        store = InMemoryRuntimeStateStore(embedder=embedder)
        store.add_knowledge(
            scope_type="group",
            scope_id="612475113",
            memory_type="fact",
            summary="晴朗的天空带着薄云",
            tags=["天气"],
            confidence=0.4,
        )

        recalled = store.recall_knowledge(
            scopes=[("group", "612475113")],
            query="蓝天白云",
            limit=3,
        )

        self.assertEqual(recalled, ["晴朗的天空带着薄云"])
        self.assertEqual(store.embedded_knowledge_count(), 1)

    def test_sqlite_store_persists_embedding_vectors(self) -> None:
        embedder = FakeEmbedder(
            {
                "她偏爱深夜聊天和安静的雨声\nTags: 深夜, 雨天": [0.0, 1.0],
                "夜雨": [0.0, 1.0],
                "__default__": [1.0, 0.0],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            store = SqliteRuntimeStateStore(path, embedder=embedder)
            store.add_knowledge(
                scope_type="member",
                scope_id="612475113:783190298",
                memory_type="preference",
                summary="她偏爱深夜聊天和安静的雨声",
                tags=["深夜", "雨天"],
                confidence=0.7,
            )

            reopened = SqliteRuntimeStateStore(path, embedder=embedder)
            recalled = reopened.recall_knowledge(
                scopes=[("member", "612475113:783190298")],
                query="夜雨",
                limit=3,
            )

            self.assertEqual(recalled, ["她偏爱深夜聊天和安静的雨声"])
            self.assertEqual(reopened.embedded_knowledge_count(), 1)


    def test_sqlite_embedding_refresh_does_not_block_other_store_operations(self) -> None:
        embedder = BlockingEmbedder([1.0, 0.0])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            store = SqliteRuntimeStateStore(path, embedder=embedder)
            store.add_knowledge(
                scope_type="group",
                scope_id="612475113",
                memory_type="fact",
                summary="hello world",
                tags=["chat"],
                confidence=0.5,
            )

            refresh_done = threading.Event()

            def run_refresh() -> None:
                try:
                    store.refresh_knowledge_embeddings()
                finally:
                    refresh_done.set()

            refresh_thread = threading.Thread(target=run_refresh)
            refresh_thread.start()
            self.assertTrue(embedder.started.wait(timeout=1), "embedding refresh should start")

            mutation_done = threading.Event()

            def mutate_store() -> None:
                store.record_member_seen(group_id="612475113", user_id="783190298", qq_nickname="tester")
                mutation_done.set()

            mutation_thread = threading.Thread(target=mutate_store)
            mutation_thread.start()

            self.assertTrue(
                mutation_done.wait(timeout=0.3),
                "member writes should not be blocked by embedding network calls",
            )
            embedder.release.set()
            refresh_thread.join(timeout=2)
            mutation_thread.join(timeout=2)

            self.assertTrue(refresh_done.is_set())
            self.assertEqual(store.embedded_knowledge_count(), 1)

if __name__ == "__main__":
    unittest.main()
