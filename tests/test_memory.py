from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.memory import FileMemoryStore
from waifu_standalone.models import SessionMemory


class FileMemoryStoreTests(unittest.TestCase):
    def test_round_trip_persists_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)
            session = SessionMemory(
                launcher_id="612475113",
                launcher_type="group",
                history=["user: hello"],
                preferred_name="luna",
                metadata={"source": "test"},
            )

            store.save(session)
            loaded = store.load("612475113", "group")

            self.assertEqual(loaded.history, ["user: hello"])
            self.assertEqual(loaded.preferred_name, "luna")
            self.assertEqual(loaded.metadata["source"], "test")

    def test_append_limits_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)

            for index in range(140):
                store.append("1", "group", f"line-{index}")

            loaded = store.load("1", "group")
            self.assertEqual(len(loaded.history), 120)
            self.assertEqual(loaded.history[0], "line-20")
            self.assertEqual(loaded.history[-1], "line-139")

    def test_invalid_launcher_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)

            with self.assertRaises(ValueError):
                store.save(SessionMemory(launcher_id="../", launcher_type="group"))

    def test_character_specific_sessions_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)
            first = SessionMemory(
                launcher_id="612475113",
                launcher_type="group",
                character_id="default",
                history=["user: default line"],
            )
            second = SessionMemory(
                launcher_id="612475113",
                launcher_type="group",
                character_id="aurora",
                history=["user: aurora line"],
            )

            store.save(first)
            store.save(second)

            loaded_default = store.load("612475113", "group", character_id="default")
            loaded_aurora = store.load("612475113", "group", character_id="aurora")

            self.assertEqual(loaded_default.history, ["user: default line"])
            self.assertEqual(loaded_aurora.history, ["user: aurora line"])
            self.assertEqual(loaded_default.character_id, "default")
            self.assertEqual(loaded_aurora.character_id, "aurora")

    def test_character_load_does_not_inherit_legacy_root_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)
            legacy = SessionMemory(
                launcher_id="612475113",
                launcher_type="group",
                history=["user: legacy line"],
            )
            store.save(legacy)

            loaded = store.load("612475113", "group", character_id="default")

            self.assertEqual(loaded.history, [])
            self.assertEqual(loaded.character_id, "default")


if __name__ == "__main__":
    unittest.main()
