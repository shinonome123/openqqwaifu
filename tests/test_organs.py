from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.memory import InMemoryStore
from waifu_standalone.organs.memories import Memory


class MemoryOrganTests(unittest.TestCase):
    def test_recall_long_term_memories_scores_summary_and_tags(self) -> None:
        store = InMemoryStore()
        memory = Memory(store)
        session = memory.load("1", "person")
        session.metadata["long_term_memory"] = [
            {"summary": "之前聊过晴朗的天空和云层。", "tags": ["天空", "天气"]},
            {"summary": "说过喜欢牛奶和甜点。", "tags": ["食物"]},
        ]
        store.save(session)

        result = memory.recall_long_term_memories("1", "person", "再说说天空和天气", limit=2)

        self.assertEqual(result[0], "之前聊过晴朗的天空和云层。")

    def test_format_dialogue_uses_assistant_alias(self) -> None:
        store = InMemoryStore()
        memory = Memory(store)
        session = memory.load("1", "person")
        session.history = ["tester: hello", "assistant: hi there"]
        store.save(session)

        formatted = memory.format_dialogue("1", "person", assistant_name="琉璃", limit=4)

        self.assertIn("tester：hello", formatted)
        self.assertIn("琉璃：hi there", formatted)

    def test_maybe_archive_history_creates_long_term_entry(self) -> None:
        store = InMemoryStore()
        memory = Memory(store)
        session = memory.load("1", "person")
        session.history = [
            "tester: hello",
            "assistant: hi",
            "tester: tell me more",
            "assistant: sure",
            "tester: continue",
        ]
        store.save(session)

        updated = memory.maybe_archive_history(
            "1",
            "person",
            max_history_lines=3,
            batch_size=2,
            summarizer=lambda lines: ("聊过问候和继续聊天。", ["聊天", "问候"]),
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(len(updated.history), 3)
        self.assertEqual(updated.metadata["long_term_memory"][0]["summary"], "聊过问候和继续聊天。")


if __name__ == "__main__":
    unittest.main()
