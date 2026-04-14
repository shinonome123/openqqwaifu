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

            for index in range(25):
                store.append("1", "group", f"line-{index}")

            loaded = store.load("1", "group")
            self.assertEqual(len(loaded.history), 20)
            self.assertEqual(loaded.history[0], "line-5")
            self.assertEqual(loaded.history[-1], "line-24")


if __name__ == "__main__":
    unittest.main()
