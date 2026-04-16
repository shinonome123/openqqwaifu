from __future__ import annotations

import asyncio
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
from waifu_standalone.http_transport import HttpResponse


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

        def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            return HttpResponse(
                status_code=200,
                text="---\nid: demo\n---\nbody\n",
                content=b"---\nid: demo\n---\nbody\n",
                headers={},
            )

        with patch.object(client._transport, "request", side_effect=fake_request):
            result = client.fetch_skill_markdown(
                "skillsmp",
                "https://github.com/example/demo/tree/main/skills/demo",
            )

        self.assertEqual(result["raw_url"], "https://raw.githubusercontent.com/example/demo/main/skills/demo/SKILL.md")
        self.assertIn("id: demo", result["markdown"])

    def test_fetch_skill_markdown_accepts_github_repo_root_url(self) -> None:
        client = self._client()

        def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            if url == "https://api.github.com/repos/example/demo":
                payload = json.dumps({"default_branch": "main"})
                return HttpResponse(status_code=200, text=payload, content=payload.encode("utf-8"), headers={})
            if url == "https://raw.githubusercontent.com/example/demo/main/SKILL.md":
                return HttpResponse(
                    status_code=200,
                    text="---\nid: demo-root\n---\nbody\n",
                    content=b"---\nid: demo-root\n---\nbody\n",
                    headers={},
                )
            raise AssertionError(url)

        with patch.object(client._transport, "request", side_effect=fake_request):
            result = client.fetch_skill_markdown(
                "skillsmp",
                "https://github.com/example/demo",
            )

        self.assertEqual(result["raw_url"], "https://raw.githubusercontent.com/example/demo/main/SKILL.md")
        self.assertIn("id: demo-root", result["markdown"])

    def test_fetch_skill_markdown_accepts_github_blob_url(self) -> None:
        client = self._client()

        def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            return HttpResponse(
                status_code=200,
                text="---\nid: blob-demo\n---\nbody\n",
                content=b"---\nid: blob-demo\n---\nbody\n",
                headers={},
            )

        with patch.object(client._transport, "request", side_effect=fake_request):
            result = client.fetch_skill_markdown(
                "skillsmp",
                "https://github.com/example/demo/blob/main/skills/demo/SKILL.md",
            )

        self.assertEqual(result["raw_url"], "https://raw.githubusercontent.com/example/demo/main/skills/demo/SKILL.md")
        self.assertIn("id: blob-demo", result["markdown"])

    def test_async_search_uses_async_transport(self) -> None:
        client = self._client()

        async def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            payload = json.dumps(
                {
                    "data": {
                        "skills": [
                            {
                                "id": "demo",
                                "name": "Demo Skill",
                                "author": "tester",
                                "description": "demo",
                                "githubUrl": "https://github.com/example/demo",
                                "skillUrl": "https://example.com/skill",
                            }
                        ]
                    }
                }
            )
            return HttpResponse(status_code=200, text=payload, content=payload.encode("utf-8"), headers={})

        with patch.object(client._async_transport, "request", side_effect=fake_request):
            result = asyncio.run(client.asearch("demo"))

        self.assertTrue(result["enabled"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "demo")


if __name__ == "__main__":
    unittest.main()
