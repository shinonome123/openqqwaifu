from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.http_api import HttpApi, parse_onebot_event


class _BrokenService:
    def handle_event(self, event):  # type: ignore[no-untyped-def]
        raise RuntimeError("sidecar unavailable")


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
                "raw_message": "中文测试123",
            }
        )

        self.assertEqual(event.plain_text, "中文测试123")

    def test_parse_event_falls_back_to_sender_id_for_suspicious_nickname(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "person",
                "user_id": 2,
                "sender": {"user_id": 2, "nickname": "闂颴椤"},
                "raw_message": "hello",
            }
        )

        self.assertEqual(event.sender_name, "user_2")


if __name__ == "__main__":
    unittest.main()
