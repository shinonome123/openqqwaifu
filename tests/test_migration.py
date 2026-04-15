from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.memory import FileMemoryStore
from waifu_standalone.migration import WaifuDataImporter, parse_simple_yaml


class MigrationTests(unittest.TestCase):
    def test_parse_simple_yaml_supports_scalars_and_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "waifu.yaml"
            path.write_text(
                "\n".join(
                    [
                        "response_rate: 1",
                        "group_response_delay: 0",
                        "summarization_mode: true",
                        "ignore_prefix: [\"/\", \"!\"]",
                        "character: \"default\" # comment",
                    ]
                ),
                encoding="utf-8",
            )

            payload = parse_simple_yaml(path)

            self.assertEqual(payload["response_rate"], 1)
            self.assertEqual(payload["group_response_delay"], 0)
            self.assertEqual(payload["summarization_mode"], True)
            self.assertEqual(payload["ignore_prefix"], ["/", "!"])
            self.assertEqual(payload["character"], "default")

    def test_importer_migrates_history_and_card_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            waifu_root = Path(tmpdir) / "waifu"
            data_dir = waifu_root / "data"
            config_dir = data_dir / "config"
            cards_dir = waifu_root / "cards"
            config_dir.mkdir(parents=True)
            cards_dir.mkdir(parents=True)

            (config_dir / "waifu.yaml").write_text(
                "\n".join(
                    [
                        "response_rate: 1",
                        "character: \"default\"",
                    ]
                ),
                encoding="utf-8",
            )
            (config_dir / "waifu_612475113.yaml").write_text(
                "group_response_delay: 3\n",
                encoding="utf-8",
            )
            (cards_dir / "default_group.yaml").write_text(
                "\n".join(
                    [
                        "assistant_name: neko",
                        "user_name: 爸爸",
                    ]
                ),
                encoding="utf-8",
            )
            (data_dir / "short_term_memory_612475113.json").write_text(
                json.dumps(
                    [
                        {"role": "user", "content": "[time]hello"},
                        {"role": "assistant", "content": "[time]received"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (data_dir / "memories_612475113.json").write_text(
                json.dumps(
                    {"long_term": [{"summary": "talked before", "tags": ["chat", "group"]}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            store = FileMemoryStore(Path(tmpdir) / "sessions")
            importer = WaifuDataImporter(store, waifu_root)

            result = importer.import_launcher("612475113", "group")
            loaded = store.load("612475113", "group")

            self.assertTrue(result.imported)
            self.assertEqual(loaded.preferred_name, "")
            self.assertEqual(loaded.metadata["history_source"], "short_term_memory")
            self.assertEqual(loaded.metadata["waifu_config"]["group_response_delay"], 3)
            self.assertEqual(loaded.metadata["long_term_memory"][0]["summary"], "talked before")
            self.assertEqual(loaded.metadata["card"]["assistant_name"], "neko")
            self.assertEqual(loaded.metadata["card"]["user_name"], "爸爸")
            self.assertEqual(loaded.history[0], "user: [time]hello")

    def test_importer_rejects_invalid_launcher_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            waifu_root = Path(tmpdir) / "waifu"
            waifu_root.mkdir(parents=True)
            store = FileMemoryStore(Path(tmpdir) / "sessions")
            importer = WaifuDataImporter(store, waifu_root)

            with self.assertRaises(ValueError):
                importer.import_launcher("612475113", "weird")

    def test_importer_handles_malformed_json_memory_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            waifu_root = Path(tmpdir) / "waifu"
            data_dir = waifu_root / "data"
            config_dir = data_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "waifu.yaml").write_text("character: \"default\"\n", encoding="utf-8")
            (data_dir / "short_term_memory_612475113.json").write_text("{broken", encoding="utf-8")
            (data_dir / "memories_612475113.json").write_text("{broken", encoding="utf-8")

            store = FileMemoryStore(Path(tmpdir) / "sessions")
            importer = WaifuDataImporter(store, waifu_root)

            result = importer.import_launcher("612475113", "group")

            self.assertTrue(result.imported)
            loaded = store.load("612475113", "group")
            self.assertEqual(loaded.history, [])
            self.assertEqual(loaded.metadata.get("long_term_memory", []), [])

    def test_importer_handles_unexpected_json_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            waifu_root = Path(tmpdir) / "waifu"
            data_dir = waifu_root / "data"
            config_dir = data_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "waifu.yaml").write_text("character: \"default\"\n", encoding="utf-8")
            (data_dir / "short_term_memory_612475113.json").write_text(
                json.dumps({"history": ["bad shape"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (data_dir / "memories_612475113.json").write_text(
                json.dumps(["bad shape"], ensure_ascii=False),
                encoding="utf-8",
            )

            store = FileMemoryStore(Path(tmpdir) / "sessions")
            importer = WaifuDataImporter(store, waifu_root)

            result = importer.import_launcher("612475113", "group")

            self.assertTrue(result.imported)
            loaded = store.load("612475113", "group")
            self.assertEqual(loaded.history, [])
            self.assertEqual(loaded.metadata.get("long_term_memory", []), [])


if __name__ == "__main__":
    unittest.main()
