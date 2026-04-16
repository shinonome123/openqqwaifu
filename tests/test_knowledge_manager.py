from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.config import AppConfig
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.organs.knowledge_manager import KnowledgeManager


class _FakeStateStore:
    def __init__(self) -> None:
        self.last_recall: dict[str, object] = {}
        self.last_list_limit = 0

    def recall_knowledge(
        self,
        *,
        scopes: list[tuple[str, str]],
        query: object,
        limit: int = 3,
        character_id: object = "",
    ) -> list[str]:
        self.last_recall = {
            "scopes": list(scopes),
            "query": query,
            "limit": limit,
            "character_id": character_id,
        }
        return ["喜欢火锅", "是程序员", "喜欢火锅"]

    def list_knowledge(self, *, limit: int = 80, character_id: object | None = None) -> list[dict[str, object]]:
        self.last_list_limit = limit
        return [
            {
                "id": 3,
                "scope_type": "member",
                "scope_id": "group-1:user-1",
                "summary": "是程序员",
                "tags": ["程序员"],
                "confidence": 0.9,
                "updated_at": 30,
            },
            {
                "id": 2,
                "scope_type": "group",
                "scope_id": "group-1",
                "summary": "昨天加班到很晚",
                "tags": ["加班"],
                "confidence": 0.7,
                "updated_at": 20,
            },
            {
                "id": 1,
                "scope_type": "member",
                "scope_id": "group-1:user-1",
                "summary": "喜欢火锅",
                "tags": ["火锅"],
                "confidence": 0.8,
                "updated_at": 10,
            },
        ]


class KnowledgeManagerTests(unittest.TestCase):
    def test_recall_merges_and_deduplicates_hits(self) -> None:
        store = _FakeStateStore()
        manager = KnowledgeManager(
            config=AppConfig(memory_graph_limit=1),
            generator=object(),
            state_store=store,
            current_character_id=lambda: "default",
            member_record=lambda event: None,
            extract_directory_preferred_name=lambda text: "",
            extract_image_prompt=lambda text: None,
            update_member_profile_summary=lambda *args, **kwargs: None,
        )
        event = InboundEvent(
            launcher_id="group-1",
            launcher_type="group",
            sender_id="user-1",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="火锅 程序员")],
        )

        recalled = manager.recall(event, query="火锅 程序员", limit=3)

        self.assertEqual(recalled, ["喜欢火锅", "是程序员", "昨天加班到很晚"])
        self.assertEqual(store.last_recall["limit"], 3)
        self.assertEqual(store.last_list_limit, 80)
        self.assertEqual(store.last_recall["character_id"], "default")


if __name__ == "__main__":
    unittest.main()
