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

    def test_person_event_updates_preferred_name(self) -> None:
        event = InboundEvent(
            launcher_id="783190298",
            launcher_type="person",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="call me luna")],
        )

        result = self.service.handle_event(event)

        self.assertIsNotNone(result)
        self.assertIn("luna", result.text)
        self.assertEqual(len(self.outbound.sent), 1)

    def test_image_request_uses_generator(self) -> None:
        event = InboundEvent(
            launcher_id="1101040950",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="draw: catgirl under sunlight")],
        )

        result = self.service.handle_event(event)

        self.assertIsNotNone(result)
        self.assertEqual(result.images, ["generated://catgirl under sunlight"])
        self.assertIn("图片生成好了", result.text)
        self.assertEqual(len(self.outbound.sent), 1)

    def test_group_message_requires_mention_when_bot_account_is_configured(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)
        ignored = InboundEvent(
            launcher_id="612475113",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="hello")],
        )
        mentioned = InboundEvent(
            launcher_id="612475113",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[
                MessageSegment(kind="mention", mention_target="3518944354"),
                MessageSegment(kind="text", text=" what should you call me"),
            ],
        )

        ignored_result = service.handle_event(ignored)
        mentioned_result = service.handle_event(mentioned)

        self.assertIsNone(ignored_result)
        self.assertIsNotNone(mentioned_result)
        self.assertEqual(len(outbound.sent), 1)

    def test_group_follow_up_window_keeps_replying_after_mention(self) -> None:
        config = AppConfig(bot_account_id="3518944354", group_follow_up_window_seconds=5.0)
        service, outbound = build_default_service(config)
        mentioned = InboundEvent(
            launcher_id="612475113",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[
                MessageSegment(kind="mention", mention_target="3518944354"),
                MessageSegment(kind="text", text=" hello"),
            ],
        )
        follow_up = InboundEvent(
            launcher_id="612475113",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="keep talking")],
        )

        first = service.handle_event(mentioned)
        second = service.handle_event(follow_up)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(outbound.sent), 2)

    def test_dashboard_snapshot_surfaces_runtime_state(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, _ = build_default_service(config)
        service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" hello"),
                ],
            )
        )

        snapshot = service.dashboard_snapshot()

        self.assertEqual(snapshot["assistant_name"], "琉璃")
        self.assertEqual(snapshot["character"], "default")
        self.assertEqual(snapshot["thinking_mode"], True)
        self.assertEqual(snapshot["summarization_mode"], False)
        self.assertEqual(snapshot["session_count"], 1)
        self.assertEqual(snapshot["recent_outbound_count"], 1)
        self.assertIn("612475113", snapshot["active_follow_up_launchers"])

    def test_imported_card_identity_is_used_for_person_session(self) -> None:
        service, _ = build_default_service(AppConfig())
        session = service.memory.load("783190298", "person")
        session.metadata["card"] = {
            "assistant_name": "neko",
            "user_name": "爸爸",
        }
        service.memory.store.save(session)

        result = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="hello there")],
            )
        )

        self.assertIsNotNone(result)
        self.assertIn("爸爸", result.text)

    def test_save_character_panel_can_edit_without_switching_active_character(self) -> None:
        service, _ = build_default_service(AppConfig())

        saved = service.save_character_panel(
            {
                "character": "aurora",
                "set_active": False,
                "shared_fields": {
                    "assistant_name": "极光",
                    "user_name": "主人",
                    "language": "简体中文",
                },
                "person_fields": {
                    "profile": ["安静"],
                    "skills": ["会顺着接话"],
                    "background": ["你们正在私聊。"],
                    "rules": ["不超过三句话。"],
                    "prologue": ["屏幕亮了一下。"],
                },
                "group_fields": {
                    "profile": ["群里说话利落"],
                    "skills": ["知道什么时候插话"],
                    "background": ["你在一个群聊中。"],
                    "rules": ["不要刷屏。"],
                    "prologue": ["群消息不断刷新。"],
                },
            }
        )

        self.assertEqual(service.config.character, "default")
        self.assertEqual(saved["character"], "aurora")
        self.assertEqual(saved["shared"]["assistant_name"], "极光")

    def test_summarization_mode_archives_older_history(self) -> None:
        config = AppConfig(
            summarization_mode=True,
            short_term_memory_limit=4,
            memory_summary_batch_size=2,
        )
        service, _ = build_default_service(config)

        for index in range(3):
            service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text=f"message-{index}")],
                )
            )

        session = service.memory.load("783190298", "person")
        long_term = session.metadata.get("long_term_memory", [])

        self.assertTrue(isinstance(long_term, list) and long_term)
        self.assertLessEqual(len(session.history), 4)

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
