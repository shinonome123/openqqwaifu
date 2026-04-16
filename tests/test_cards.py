from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.cards import CardManager, parse_card_file
from waifu_standalone.config import AppConfig
from waifu_standalone.models import SessionMemory


class CardTests(unittest.TestCase):
    def test_parse_card_file_supports_scalar_and_list_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "card.yaml"
            path.write_text(
                "\n".join(
                    [
                        "user_name: 爸爸",
                        "assistant_name: neko",
                        "Profile:",
                        "  - 可爱",
                        "  - 机灵",
                        "Rules:",
                        "  - 回答保持简洁",
                    ]
                ),
                encoding="utf-8",
            )

            payload = parse_card_file(path)

            self.assertEqual(payload["user_name"], "爸爸")
            self.assertEqual(payload["assistant_name"], "neko")
            self.assertEqual(payload["Profile"], ["可爱", "机灵"])
            self.assertEqual(payload["Rules"], ["回答保持简洁"])

    def test_card_manager_loads_builtin_default_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir))
            session = SessionMemory(launcher_id="1", launcher_type="person")

            card = manager.load("person", session)

            self.assertEqual(card.assistant_name, "Assistant")
            self.assertTrue(card.profile)
            self.assertIn("QQ", " ".join(card.background))

    def test_missing_non_default_card_uses_character_name_instead_of_default_template_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir, character="aurora"))
            session = SessionMemory(launcher_id="1", launcher_type="group", character_id="aurora")

            card = manager.load("group", session)

            self.assertEqual(card.assistant_name, "aurora")
            self.assertTrue(card.source.endswith("default_group.yaml"))

    def test_editor_bundle_missing_non_default_card_shows_character_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir, character="aurora"))

            bundle = manager.get_editor_bundle("aurora")

            self.assertEqual(bundle["shared"]["assistant_name"], "aurora")
            self.assertTrue(str(bundle["group"]["source_path"]).endswith("default_group.yaml"))

    def test_card_manager_prefers_data_root_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cards_dir = Path(tmpdir) / "cards"
            cards_dir.mkdir(parents=True)
            (cards_dir / "custom_person.yaml").write_text(
                "\n".join(
                    [
                        "user_name: 主人",
                        "assistant_name: neko",
                        "Profile:",
                        "  - 爱撒娇",
                    ]
                ),
                encoding="utf-8",
            )
            manager = CardManager(AppConfig(data_root=tmpdir, character="custom"))
            session = SessionMemory(launcher_id="1", launcher_type="person")

            card = manager.load("person", session)

            self.assertEqual(card.assistant_name, "neko")
            self.assertEqual(card.user_name, "主人")
            self.assertEqual(card.profile, ["爱撒娇"])

    def test_editor_bundle_exposes_structured_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir))

            bundle = manager.get_editor_bundle("default")

            self.assertEqual(bundle["shared"]["assistant_name"], "Assistant")
            self.assertIn("profile", bundle["person"]["fields"])
            self.assertIn("rules", bundle["group"]["fields"])
            self.assertFalse(bundle["portrait"]["available"])

    def test_build_preview_card_uses_editor_fields(self) -> None:
        manager = CardManager(AppConfig())

        card = manager.build_preview_card(
            shared_fields={
                "assistant_name": "Aurora",
                "user_name": "Captain",
                "language": "简体中文",
            },
            variant_fields={
                "profile": ["calm and sharp"],
                "skills": ["keeps continuity"],
                "background": ["chatting in a private thread"],
                "rules": ["stay concise"],
                "prologue": ["the terminal lights up softly"],
            },
        )

        self.assertEqual(card.assistant_name, "Aurora")
        self.assertEqual(card.user_name, "Captain")
        self.assertEqual(card.language, "简体中文")
        self.assertEqual(card.profile, ["calm and sharp"])
        self.assertEqual(card.source, "preview")

    def test_save_editor_bundle_accepts_structured_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir))

            bundle = manager.save_editor_bundle(
                "aurora",
                shared_fields={
                    "assistant_name": "极光",
                    "user_name": "主人",
                    "language": "简体中文",
                },
                person_fields={
                    "profile": ["安静", "会接话"],
                    "skills": ["顺着聊天氛围继续说"],
                    "background": ["你和用户在私聊。"],
                    "rules": ["不超过三句话。"],
                    "prologue": ["屏幕轻轻亮了起来。"],
                },
                group_fields={
                    "profile": ["群里反应快"],
                    "skills": ["会接梗"],
                    "background": ["你在一个活跃群聊里。"],
                    "rules": ["不过度刷屏。"],
                    "prologue": ["群消息刷得很快。"],
                },
                portrait={
                    "style": "dream-anime",
                    "prompt_suffix": "blue hair",
                    "auto_generate": True,
                },
            )

            self.assertEqual(bundle["character"], "aurora")
            self.assertEqual(bundle["shared"]["assistant_name"], "极光")
            self.assertEqual(bundle["person"]["fields"]["profile"], ["安静", "会接话"])
            self.assertEqual(bundle["portrait"]["style"], "dream-anime")
            self.assertTrue((Path(tmpdir) / "cards" / "aurora_person.yaml").exists())
            self.assertTrue((Path(tmpdir) / "cards" / "aurora_group.yaml").exists())

    def test_active_character_is_shared_through_cards_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir, character="default"))

            manager.save_editor_bundle(
                "aurora",
                shared_fields={
                    "assistant_name": "Aurora",
                    "user_name": "Captain",
                    "language": "zh",
                },
                person_fields={"profile": ["calm"]},
                group_fields={"profile": ["quick"]},
            )
            manager.set_active_character("aurora")

            next_manager = CardManager(AppConfig(data_root=tmpdir, character="default"))

            self.assertEqual(next_manager.active_character(), "aurora")
            self.assertEqual(next_manager.get_editor_bundle("")["character"], "aurora")

    def test_portrait_asset_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir))

            portrait = manager.save_portrait_asset(
                "aurora",
                b"fakepngbytes",
                "image/png",
                prompt="portrait prompt",
                style="neon-pixel",
                prompt_suffix="blue jacket",
                auto_generate=True,
            )
            data = manager.load_portrait_asset("aurora")

            self.assertIsNotNone(data)
            assert data is not None
            body, content_type = data
            self.assertEqual(body, b"fakepngbytes")
            self.assertEqual(content_type, "image/png")
            self.assertTrue(portrait["available"])
            self.assertIn("/api/portraits?character=aurora", portrait["url"])

    def test_active_character_wins_over_imported_metadata_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = CardManager(AppConfig(data_root=tmpdir, character="default"))
            cards_root = Path(tmpdir) / "cards"
            cards_root.mkdir(parents=True, exist_ok=True)
            imported_path = cards_root / "imported_person.yaml"
            imported_path.write_text(
                "\n".join(
                    [
                        "user_name: LegacyUser",
                        "assistant_name: legacy",
                    ]
                ),
                encoding="utf-8",
            )
            manager.save_editor_bundle(
                "aurora",
                shared_fields={
                    "assistant_name": "Aurora",
                    "user_name": "Captain",
                    "language": "zh",
                },
                person_fields={"profile": ["calm"]},
                group_fields={"profile": ["quick"]},
            )
            manager.set_active_character("aurora")
            session = SessionMemory(
                launcher_id="783190298",
                launcher_type="person",
                metadata={
                    "card": {
                        "assistant_name": "legacy",
                        "user_name": "LegacyUser",
                        "source": str(imported_path),
                    }
                },
            )

            card = manager.load("person", session)

            self.assertEqual(card.assistant_name, "Aurora")
            self.assertEqual(card.user_name, "Captain")
            self.assertTrue(card.source.endswith("aurora_person.yaml"))

    def test_list_characters_uses_existing_variant_instead_of_default_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cards_root = Path(tmpdir) / "cards"
            cards_root.mkdir(parents=True, exist_ok=True)
            (cards_root / "aurora_group.yaml").write_text(
                "\n".join(
                    [
                        "user_name: Captain",
                        "assistant_name: Aurora",
                        "Profile:",
                        "  - calm",
                    ]
                ),
                encoding="utf-8",
            )
            manager = CardManager(AppConfig(data_root=tmpdir, character="aurora"))

            items = {str(item["character"]): item for item in manager.list_characters()}

            self.assertIn("aurora", items)
            self.assertEqual(items["aurora"]["assistant_name"], "Aurora")
            self.assertFalse(bool(items["aurora"]["has_person"]))
            self.assertTrue(bool(items["aurora"]["has_group"]))


if __name__ == "__main__":
    unittest.main()
