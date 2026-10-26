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
    def test_format_dialogue_uses_assistant_alias(self) -> None:
        store = InMemoryStore()
        memory = Memory(store)
        session = memory.load("1", "person")
        session.history = ["tester: hello", "assistant: hi there"]
        store.save(session)

        formatted = memory.format_dialogue("1", "person", assistant_name="Ruri", limit=4)

        self.assertIn("tester", formatted)
        self.assertIn("Ruri", formatted)

    def test_format_dialogue_exclude_last_keeps_full_window(self) -> None:
        store = InMemoryStore()
        memory = Memory(store)
        session = memory.load("1", "person")
        session.history = [f"tester: line-{index}" for index in range(6)]
        store.save(session)

        formatted = memory.format_dialogue("1", "person", assistant_name="Ruri", limit=3, exclude_last=True)

        self.assertEqual(formatted.splitlines(), ["tester: line-2", "tester: line-3", "tester: line-4"])

    def test_format_dialogue_truncates_long_line_without_dropping_context(self) -> None:
        store = InMemoryStore()
        memory = Memory(store)
        session = memory.load("1", "person")
        session.history = ["tester: earlier", "tester: " + "x" * 2000, "assistant: short reply"]
        store.save(session)

        formatted = memory.format_dialogue("1", "person", assistant_name="Ruri", limit=10, max_chars=2500)

        lines = formatted.splitlines()
        self.assertEqual(lines[0], "tester: earlier")
        self.assertTrue(lines[1].endswith("…"))
        self.assertLess(len(lines[1]), 2000)
        self.assertEqual(lines[2], "Ruri: short reply")

    def test_maybe_archive_history_returns_summary_payload(self) -> None:
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

        archived = memory.maybe_archive_history(
            "1",
            "person",
            max_history_lines=3,
            batch_size=2,
            summarizer=lambda lines: ("chat summary", ["chat", "follow-up"]),
        )

        self.assertIsNotNone(archived)
        assert archived is not None
        updated_session = memory.load("1", "person")
        self.assertEqual(len(updated_session.history), 3)
        self.assertEqual(archived["summary"], "chat summary")
        self.assertEqual(archived["tags"], ["chat", "follow-up"])
        self.assertEqual(archived["archive_count"], 2)

    def test_maybe_archive_history_preserves_newer_messages_written_during_summary(self) -> None:
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

        def summarizer(lines: list[str]) -> tuple[str, list[str]]:
            current = store.load("1", "person")
            current.history.append("tester: newer message")
            store.save(current)
            return ("chat summary", ["chat"])

        archived = memory.maybe_archive_history(
            "1",
            "person",
            max_history_lines=3,
            batch_size=2,
            summarizer=summarizer,
        )

        self.assertIsNotNone(archived)
        updated_session = memory.load("1", "person")
        self.assertEqual(
            updated_session.history,
            [
                "tester: tell me more",
                "assistant: sure",
                "tester: continue",
                "tester: newer message",
            ],
        )

    def test_maybe_archive_history_skips_stale_trim_when_prefix_changes(self) -> None:
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

        def summarizer(lines: list[str]) -> tuple[str, list[str]]:
            current = store.load("1", "person")
            current.history = current.history[2:]
            store.save(current)
            return ("chat summary", ["chat"])

        archived = memory.maybe_archive_history(
            "1",
            "person",
            max_history_lines=3,
            batch_size=2,
            summarizer=summarizer,
        )

        self.assertIsNone(archived)
        updated_session = memory.load("1", "person")
        self.assertEqual(
            updated_session.history,
            [
                "tester: tell me more",
                "assistant: sure",
                "tester: continue",
            ],
        )

    def test_extract_preferred_name_skips_overlong_messages(self) -> None:
        store = InMemoryStore()
        memory = Memory(store)

        result = memory.extract_preferred_name("a" * 3000)

        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
