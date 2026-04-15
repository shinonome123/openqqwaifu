from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.marketplace import MarketplaceClient
from waifu_standalone.config import MarketplaceConfig, MarketplaceSourceConfig


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class MarketplaceClientTests(unittest.TestCase):
    def _client(self) -> MarketplaceClient:
        return MarketplaceClient(
            MarketplaceConfig(
                sources=[
                    MarketplaceSourceConfig(
                        source_id="skillsmp",
                        name="SkillsMP",
                        kind="skillsmp",
                        enabled=True,
                        base_url="https://skillsmp.com",
                    )
                ]
            )
        )

    def test_fetch_skill_markdown_accepts_github_tree_url(self) -> None:
        client = self._client()

        def fake_urlopen(request, timeout=0):
            return _FakeResponse(b"---\nid: demo\n---\nbody\n")

        with patch("waifu_standalone.cells.marketplace.urlopen", side_effect=fake_urlopen):
            result = client.fetch_skill_markdown(
                "skillsmp",
                "https://github.com/example/demo/tree/main/skills/demo",
            )

        self.assertEqual(result["raw_url"], "https://raw.githubusercontent.com/example/demo/main/skills/demo/SKILL.md")
        self.assertIn("id: demo", result["markdown"])

    def test_fetch_skill_markdown_accepts_github_repo_root_url(self) -> None:
        client = self._client()

        def fake_urlopen(request, timeout=0):
            url = request.full_url
            if url == "https://api.github.com/repos/example/demo":
                return _FakeResponse(json.dumps({"default_branch": "main"}).encode("utf-8"))
            if url == "https://raw.githubusercontent.com/example/demo/main/SKILL.md":
                return _FakeResponse(b"---\nid: demo-root\n---\nbody\n")
            raise AssertionError(url)

        with patch("waifu_standalone.cells.marketplace.urlopen", side_effect=fake_urlopen):
            result = client.fetch_skill_markdown(
                "skillsmp",
                "https://github.com/example/demo",
            )

        self.assertEqual(result["raw_url"], "https://raw.githubusercontent.com/example/demo/main/SKILL.md")
        self.assertIn("id: demo-root", result["markdown"])

    def test_fetch_skill_markdown_accepts_github_blob_url(self) -> None:
        client = self._client()

        def fake_urlopen(request, timeout=0):
            return _FakeResponse(b"---\nid: blob-demo\n---\nbody\n")

        with patch("waifu_standalone.cells.marketplace.urlopen", side_effect=fake_urlopen):
            result = client.fetch_skill_markdown(
                "skillsmp",
                "https://github.com/example/demo/blob/main/skills/demo/SKILL.md",
            )

        self.assertEqual(result["raw_url"], "https://raw.githubusercontent.com/example/demo/main/skills/demo/SKILL.md")
        self.assertIn("id: blob-demo", result["markdown"])


if __name__ == "__main__":
    unittest.main()
