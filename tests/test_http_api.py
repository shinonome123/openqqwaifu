from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.cells.skill_registry import build_skill_markdown_template
from waifu_standalone.config import AppConfig
from waifu_standalone.http_api import HttpApi, RequestTooLarge, _read_chunked_body, parse_onebot_event


class _BrokenService:
    def handle_event(self, event):  # type: ignore[no-untyped-def]
        raise RuntimeError("sidecar unavailable")


class _FakeChunkedHandler:
    def __init__(self, raw: bytes):
        self.rfile = io.BytesIO(raw)


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.outbound = build_default_service()
        self.api = HttpApi(self.service)

    def test_message_payload_is_accepted(self) -> None:
        payload = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 612475113,
            "user_id": 783190298,
            "sender": {"user_id": 783190298, "nickname": "tester"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["reply"]["launcher_id"], "612475113")

    def test_string_message_payload_is_accepted(self) -> None:
        payload = {
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "sender": {"nickname": "tester"},
            "message": "draw: catgirl",
        }

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["reply"]["images"], ["generated://catgirl"])

    def test_non_message_payload_is_ignored(self) -> None:
        payload = {"post_type": "notice"}

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "ignored")

    def test_delivery_failures_are_mapped_to_502(self) -> None:
        api = HttpApi(_BrokenService())  # type: ignore[arg-type]

        status, body = api.handle_json({"message": "hello"})

        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "delivery_failed")

    def test_reply_body_is_json_serializable(self) -> None:
        payload = {
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "sender": {"nickname": "tester"},
            "message": [{"type": "text", "data": {"text": "draw: catgirl"}}],
        }

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 200)
        json.dumps(body, ensure_ascii=False)

    def test_parse_event_prefers_raw_message_for_pure_text_payloads(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "person",
                "user_id": 2,
                "sender": {"user_id": 2, "nickname": "tester"},
                "message": [{"type": "text", "data": {"text": "garbled"}}],
                "raw_message": "call me luna",
            }
        )

        self.assertEqual(event.plain_text, "call me luna")

    def test_parse_event_falls_back_to_sender_id_for_suspicious_nickname(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "person",
                "user_id": 2,
                "sender": {"user_id": 2, "nickname": "闂傚倸鍊块埛瀣渻"},
                "raw_message": "hello",
            }
        )

        self.assertEqual(event.sender_name, "user_2")

    def test_parse_event_keeps_mentions_and_images(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "group",
                "group_id": 612475113,
                "user_id": 783190298,
                "sender": {"user_id": 783190298, "nickname": "tester"},
                "message": [
                    {"type": "at", "data": {"qq": "3518944354"}},
                    {"type": "text", "data": {"text": " draw: sunny sky"}},
                    {"type": "image", "data": {"base64": "data:image/png;base64,aaaa"}},
                ],
            }
        )

        self.assertTrue(event.has_bot_mention("3518944354"))
        self.assertEqual(event.command_text("3518944354"), "draw: sunny sky")
        self.assertEqual(event.image_count, 1)
        self.assertEqual(event.image_payloads(), ["data:image/png;base64,aaaa"])
        self.assertEqual(event.to_memory_text(), "发送了图片，并且说：“draw: sunny sky”。")

    def test_skill_listing_is_available(self) -> None:
        skills = self.api.list_skills()

        self.assertTrue(skills["enabled"])
        self.assertGreaterEqual(skills["count"], 6)

    def test_tool_listing_is_available(self) -> None:
        tools = self.api.list_tools()

        self.assertEqual(tools["count"], 3)

    def test_skill_pack_template_is_available(self) -> None:
        pack = self.api.skill_pack_template()

        self.assertEqual(pack["format"], "waifu-skill-pack")
        self.assertGreaterEqual(pack["skill_count"], 1)

    def test_skill_detail_is_available(self) -> None:
        detail = self.api.get_skill_detail("search-command")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["command_tool"], "search")
        self.assertIn("markdown", detail)

    def test_skill_template_is_available(self) -> None:
        template = self.api.new_skill_template()

        self.assertIn("markdown", template)
        self.assertIn("custom-skill", template["markdown"])

    def test_skill_install_save_and_delete_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            markdown = build_skill_markdown_template(
                skill_id="api-skill",
                name="API Skill",
                description="api path test",
                triggers=["api skill"],
                mode="prefix",
                priority=5,
                body="Use this from the API test.",
            )

            installed = api.install_skill(markdown)
            self.assertEqual(installed["id"], "api-skill")

            saved = api.save_skill("api-skill", markdown.replace("api path test", "updated api path test"))
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertIn("updated api path test", saved["markdown"])

            deleted = api.delete_skill("api-skill")
            self.assertTrue(deleted)

    def test_skill_pack_export_and_import_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            markdown = build_skill_markdown_template(
                skill_id="pack-api-skill",
                name="Pack API Skill",
                description="pack api test",
                triggers=["pack api"],
                mode="prefix",
                priority=5,
                body="Use this from pack API test.",
            )
            api.install_skill(markdown)

            bundle = api.export_skill_pack(skill_ids=["pack-api-skill"], include_builtin=False, name="single-pack")
            self.assertEqual(bundle["skill_count"], 1)

            imported = api.import_skill_pack(bundle, overwrite=True)
            self.assertEqual(imported["imported_count"], 1)

    def test_chunked_reader_rejects_oversized_payload(self) -> None:
        chunk_size = 10 * 1024 * 1024 + 1
        handler = _FakeChunkedHandler(f"{chunk_size:X}\r\n".encode("ascii"))

        with self.assertRaises(RequestTooLarge):
            _read_chunked_body(handler)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
