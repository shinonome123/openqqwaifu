from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
import zipfile
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
    def _client(self, sources: list[MarketplaceSourceConfig] | None = None) -> MarketplaceClient:
        return MarketplaceClient(
            MarketplaceConfig(
                sources=sources
                or [
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

    @staticmethod
    def _zip_payload(files: dict[str, str]) -> bytes:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return stream.getvalue()

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

    def test_fetch_skill_markdown_accepts_clawhub_skill_url(self) -> None:
        client = self._client(
            [
                MarketplaceSourceConfig(
                    source_id="clawhub",
                    name="ClawHub",
                    kind="clawhub",
                    enabled=True,
                    base_url="https://clawhub.ai",
                    search_path="/api/v1/search",
                )
            ]
        )

        def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://clawhub.ai/api/v1/skills/skill-hunter/file?path=SKILL.md")
            return HttpResponse(
                status_code=200,
                text="---\nname: skill-hunter\n---\nbody\n",
                content=b"---\nname: skill-hunter\n---\nbody\n",
                headers={},
            )

        with patch.object(client._transport, "request", side_effect=fake_request):
            result = client.fetch_skill_markdown("clawhub", "https://clawhub.ai/skills/skill-hunter")

        self.assertEqual(result["filename"], "SKILL.md")
        self.assertIn("skill-hunter", result["markdown"])

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

    def test_search_without_source_id_merges_enabled_sources(self) -> None:
        client = self._client(
            [
                MarketplaceSourceConfig(
                    source_id="skillsmp",
                    name="SkillsMP",
                    kind="skillsmp",
                    enabled=True,
                    base_url="https://skillsmp.com",
                ),
                MarketplaceSourceConfig(
                    source_id="clawhub",
                    name="ClawHub",
                    kind="clawhub",
                    enabled=True,
                    base_url="https://clawhub.ai",
                    search_path="/api/v1/search",
                ),
            ]
        )

        def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            if url.startswith("https://skillsmp.com/api/v1/skills/search?"):
                payload = json.dumps(
                    {
                        "data": {
                            "skills": [
                                {
                                    "id": "codex-agent",
                                    "name": "codex-agent",
                                    "author": "tester",
                                    "description": "skillsmp result",
                                    "githubUrl": "https://github.com/example/codex-agent",
                                    "skillUrl": "https://skillsmp.com/skills/example",
                                }
                            ]
                        }
                    }
                )
                return HttpResponse(status_code=200, text=payload, content=payload.encode("utf-8"), headers={})
            if url.startswith("https://clawhub.ai/api/v1/search?"):
                payload = json.dumps(
                    {
                        "results": [
                            {
                                "slug": "skill-hunter",
                                "displayName": "Skill Hunter",
                                "summary": "clawhub result",
                                "score": 3.5,
                            }
                        ]
                    }
                )
                return HttpResponse(status_code=200, text=payload, content=payload.encode("utf-8"), headers={})
            raise AssertionError(url)

        with patch.object(client._transport, "request", side_effect=fake_request):
            result = client.search("codex", limit=4)

        self.assertTrue(result["aggregated"])
        self.assertEqual([item["source_id"] for item in result["items"]], ["skillsmp", "clawhub"])
        self.assertEqual(result["items"][0]["install_url"], "https://github.com/example/codex-agent")
        self.assertEqual(result["items"][1]["install_url"], "https://clawhub.ai/skills/skill-hunter")

    def test_prepare_skill_bundle_materializes_github_subdirectory(self) -> None:
        client = self._client()
        payload = self._zip_payload(
            {
                "example-demo-abcdef/skills/demo/SKILL.md": "---\nid: demo\n---\nbody\n",
                "example-demo-abcdef/skills/demo/resources/guide.md": "# guide\n",
            }
        )

        def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://api.github.com/repos/example/demo/zipball/main")
            return HttpResponse(status_code=200, text="", content=payload, headers={})

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(client._transport, "request", side_effect=fake_request):
                prepared = client.prepare_skill_bundle(
                    "skillsmp",
                    "https://github.com/example/demo/tree/main/skills/demo",
                    tmpdir,
                )

            bundle_path = Path(prepared["path"])
            self.assertTrue(bundle_path.is_dir())
            self.assertTrue((bundle_path / "SKILL.md").is_file())
            self.assertTrue((bundle_path / "resources" / "guide.md").is_file())
            self.assertEqual(prepared["page_url"], "https://github.com/example/demo")

    def test_prepare_skill_bundle_downloads_clawhub_zip_bundle(self) -> None:
        client = self._client(
            [
                MarketplaceSourceConfig(
                    source_id="clawhub",
                    name="ClawHub",
                    kind="clawhub",
                    enabled=True,
                    base_url="https://clawhub.ai",
                    search_path="/api/v1/search",
                )
            ]
        )
        payload = self._zip_payload({"SKILL.md": "---\nname: skill-hunter\n---\n"})

        def fake_request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            if url == "https://clawhub.ai/api/v1/skills/skill-hunter":
                body = json.dumps({"owner": {"handle": "kenoodl-synthesis"}})
                return HttpResponse(status_code=200, text=body, content=body.encode("utf-8"), headers={})
            if url == "https://clawhub.ai/kenoodl-synthesis/skill-hunter":
                html_body = '<a href="https://downloads.example/api/v1/download?slug=skill-hunter">Download</a>'
                return HttpResponse(status_code=200, text=html_body, content=html_body.encode("utf-8"), headers={})
            if url == "https://downloads.example/api/v1/download?slug=skill-hunter":
                return HttpResponse(
                    status_code=200,
                    text="",
                    content=payload,
                    headers={"content-disposition": 'attachment; filename="skill-hunter-1.0.6.zip"'},
                )
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(client._transport, "request", side_effect=fake_request):
                prepared = client.prepare_skill_bundle(
                    "clawhub",
                    "https://clawhub.ai/skills/skill-hunter",
                    tmpdir,
                )

            bundle_path = Path(prepared["path"])
            self.assertTrue(bundle_path.is_file())
            self.assertEqual(bundle_path.name, "skill-hunter-1.0.6.zip")
            self.assertEqual(prepared["page_url"], "https://clawhub.ai/kenoodl-synthesis/skill-hunter")
            self.assertEqual(prepared["bundle_url"], "https://downloads.example/api/v1/download?slug=skill-hunter")


if __name__ == "__main__":
    unittest.main()
