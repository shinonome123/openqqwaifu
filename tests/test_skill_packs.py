from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.skill_pack import (
    build_skill_pack_template,
    export_skill_pack,
    import_skill_pack,
    parse_skill_pack,
)
from waifu_standalone.cells.skill_registry import SkillRegistry, build_skill_markdown_template
from waifu_standalone.config import AppConfig


class SkillPackTests(unittest.TestCase):
    def test_template_contains_one_skill(self) -> None:
        template = build_skill_pack_template()

        self.assertEqual(template["format"], "waifu-skill-pack")
        self.assertEqual(template["skill_count"], 1)
        self.assertTrue(template["skills"][0]["markdown"])

    def test_parse_skill_pack_validates_required_fields(self) -> None:
        with self.assertRaises(ValueError):
            parse_skill_pack({"format": "waifu-skill-pack", "skills": []})

    def test_export_and_import_skill_pack_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(AppConfig(data_root=tmpdir))
            registry.install_workspace_skill(
                build_skill_markdown_template(
                    skill_id="weather-lens",
                    name="Weather Lens",
                    description="workspace export test",
                    triggers=["weather lens"],
                    mode="prefix",
                    priority=7,
                    body="Use weather lens.",
                )
            )
            registry.install_workspace_skill(
                build_skill_markdown_template(
                    skill_id="sky-note",
                    name="Sky Note",
                    description="second workspace skill",
                    triggers=["sky note"],
                    mode="contains",
                    priority=4,
                    body="Use sky note.",
                )
            )

            bundle = export_skill_pack(registry, include_builtin=False, name="workspace-pack")
            self.assertEqual(bundle["name"], "workspace-pack")
            self.assertEqual(bundle["skill_count"], 2)

            with tempfile.TemporaryDirectory() as other_tmpdir:
                imported_registry = SkillRegistry(AppConfig(data_root=other_tmpdir))
                result = import_skill_pack(imported_registry, bundle, overwrite=True)

                self.assertEqual(result["imported_count"], 2)
                self.assertEqual(imported_registry.get_skill("weather-lens").source_kind, "workspace")  # type: ignore[union-attr]
                self.assertEqual(imported_registry.get_skill("sky-note").source_kind, "workspace")  # type: ignore[union-attr]

    def test_import_without_overwrite_skips_existing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(AppConfig(data_root=tmpdir))
            original = build_skill_markdown_template(
                skill_id="bundle-probe",
                name="Bundle Probe",
                description="original version",
                triggers=["bundle probe"],
                mode="prefix",
                priority=3,
                body="original body",
            )
            registry.install_workspace_skill(original)

            incoming = {
                "format": "waifu-skill-pack",
                "version": 1,
                "name": "probe-pack",
                "skills": [
                    {
                        "id": "bundle-probe",
                        "filename": "bundle-probe.md",
                        "markdown": original.replace("original version", "new version"),
                    }
                ],
            }
            result = import_skill_pack(registry, incoming, overwrite=False)

            self.assertEqual(result["imported_count"], 0)
            self.assertEqual(result["skipped_count"], 1)
            self.assertIn("original version", registry.get_skill_markdown("bundle-probe") or "")


if __name__ == "__main__":
    unittest.main()
