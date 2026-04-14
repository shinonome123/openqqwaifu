from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.state_store import InMemoryRuntimeStateStore, SqliteRuntimeStateStore


class RuntimeStateStoreTests(unittest.TestCase):
    def test_in_memory_store_round_trips_member_and_knowledge(self) -> None:
        store = InMemoryRuntimeStateStore()

        member = store.record_member_seen(group_id="612475113", user_id="783190298", qq_nickname="tester")
        self.assertEqual(member["onboarding_status"], "new")

        saved_member = store.save_member(
            {
                "group_id": "612475113",
                "user_id": "783190298",
                "qq_nickname": "tester",
                "preferred_name": "luna",
                "onboarding_status": "ready",
                "profile_summary": "likes cats",
            }
        )
        self.assertEqual(saved_member["preferred_name"], "luna")
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
            recalled = reopened.recall_knowledge(
                scopes=[("group", "612475113")],
                query="games",
                limit=3,
            )
            self.assertEqual(recalled, ["group 612475113 often talks about games"])


if __name__ == "__main__":
    unittest.main()
