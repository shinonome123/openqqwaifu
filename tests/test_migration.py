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

    def test_importer_migrates_history_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            waifu_root = Path(tmpdir) / "waifu"
            data_dir = waifu_root / "data"
            config_dir = data_dir / "config"
            config_dir.mkdir(parents=True)

            (config_dir / "waifu.yaml").write_text(
                "\n".join(
                    [
                        "response_rate: 1",
                        "assistant_name: \"luna\"",
                    ]
                ),
                encoding="utf-8",
            )
            (config_dir / "waifu_612475113.yaml").write_text(
                "group_response_delay: 3\n",
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
            self.assertEqual(loaded.preferred_name, "luna")
            self.assertEqual(loaded.metadata["history_source"], "short_term_memory")
            self.assertEqual(loaded.metadata["waifu_config"]["group_response_delay"], 3)
            self.assertEqual(loaded.metadata["long_term_memory"][0]["summary"], "talked before")
            self.assertEqual(loaded.history[0], "user: [time]hello")


if __name__ == "__main__":
    unittest.main()
