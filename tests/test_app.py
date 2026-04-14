from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service, build_runtime_service
from waifu_standalone.config import AppConfig, QQSidecarConfig
from waifu_standalone.gateways.onebot_actions import OneBotHttpOutboundPort
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.services import CapturingOutboundPort


class WaifuServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.outbound = build_default_service()

    def test_text_event_produces_reply(self) -> None:
        event = InboundEvent(
            launcher_id="612475113",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="call me luna")],
        )

        result = self.service.handle_event(event)

        self.assertIsNotNone(result)
        self.assertEqual(result.text, "tester, you can decide the nickname.")
        self.assertEqual(len(self.outbound.sent), 1)

    def test_image_request_uses_generator(self) -> None:
        event = InboundEvent(
            launcher_id="1101040950",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="draw: a catgirl in sunlight")],
        )

        result = self.service.handle_event(event)

        self.assertEqual(result.images, ["generated://a catgirl in sunlight"])
        self.assertIn("image ready", result.text)
        self.assertEqual(len(self.outbound.sent), 1)

    def test_runtime_service_uses_capture_outbound_in_dry_run_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(data_root=tmpdir)

            _, outbound = build_runtime_service(config)

            self.assertIsInstance(outbound, CapturingOutboundPort)

    def test_runtime_service_uses_onebot_outbound_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                qq_sidecar=QQSidecarConfig(
                    dry_run=False,
                    outbound_base_url="http://127.0.0.1:3000",
                ),
            )

            _, outbound = build_runtime_service(config)

            self.assertIsInstance(outbound, OneBotHttpOutboundPort)


if __name__ == "__main__":
    unittest.main()
