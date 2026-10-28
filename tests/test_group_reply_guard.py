from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, ListenConfig, RateLimitConfig
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.settings_admin.config_manager import ConfigManager

BOT_ID = "3956638110"
OTHER_BOT_ID = "3246300953"


def _group_event(sender_id: str, *, group: str = "612475113", mention: bool = True, text: str = "在吗") -> InboundEvent:
    segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
    segments.append(MessageSegment(kind="text", text=text))
    return InboundEvent(
        launcher_id=group,
        launcher_type="group",
        sender_id=sender_id,
        sender_name=f"user{sender_id}",
        segments=segments,
    )


def _private_event(sender_id: str, text: str = "在吗") -> InboundEvent:
    return InboundEvent(
        launcher_id=sender_id,
        launcher_type="person",
        sender_id=sender_id,
        sender_name=f"user{sender_id}",
        segments=[MessageSegment(kind="text", text=text)],
    )


class GroupReplyGuardTests(unittest.TestCase):
    def _service(self, tmpdir: str, **overrides: object):  # type: ignore[no-untyped-def]
        # These tests are about the group-wide follow-up window: the behaviour with listen off.
        overrides.setdefault("listen", ListenConfig(enabled=False))
        service, outbound = build_default_service(AppConfig(data_root=tmpdir, bot_account_id=BOT_ID, **overrides))
        self.addCleanup(service.close)
        return service, outbound

    def test_ignored_sender_never_triggers_a_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, outbound = self._service(tmpdir, ignore_user_ids=[OTHER_BOT_ID])

            self.assertIsNone(service.handle_event(_group_event(OTHER_BOT_ID)))
            self.assertIsNone(service.handle_event(_private_event(OTHER_BOT_ID)))
            self.assertEqual(outbound.sent, [])
            self.assertIsNotNone(service.handle_event(_group_event("783190298")))

    def test_ignored_sender_does_not_ride_the_follow_up_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, outbound = self._service(
                tmpdir, ignore_user_ids=[OTHER_BOT_ID], group_follow_up_window_seconds=60.0
            )

            self.assertIsNotNone(service.handle_event(_group_event("783190298")))
            # The window is open for everyone else, but not for the ignored bot.
            self.assertIsNone(service.handle_event(_group_event(OTHER_BOT_ID, mention=False, text="本鱼更强")))
            self.assertIsNotNone(service.handle_event(_group_event("1147026356", mention=False, text="真的吗")))
            self.assertEqual(len(outbound.sent), 2)

    def test_follow_up_replies_stop_after_the_group_burst(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, outbound = self._service(
                tmpdir,
                group_follow_up_window_seconds=60.0,
                rate_limits=RateLimitConfig(group_reply_burst=2, group_reply_per_hour=0.0),
            )

            # The @ opens the follow-up window; the chatter after it shares the group bucket.
            self.assertIsNotNone(service.handle_event(_group_event("10001")))
            self.assertIsNotNone(service.handle_event(_group_event("10002", mention=False, text="然后呢")))
            self.assertIsNotNone(service.handle_event(_group_event("10003", mention=False, text="还有吗")))
            with self.assertLogs("waifu_standalone.reply_flow.reply_gate", level="WARNING"):
                self.assertIsNone(service.handle_event(_group_event("10004", mention=False, text="再来")))
            # Other groups and private chats have their own budget.
            self.assertIsNotNone(service.handle_event(_group_event("10003", group="700000001")))
            self.assertIsNotNone(service.handle_event(_private_event("10003")))
            self.assertEqual(len(outbound.sent), 5)

    def test_a_person_who_mentions_the_bot_is_answered_after_bots_used_the_group_share(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = self._service(
                tmpdir,
                group_follow_up_window_seconds=60.0,
                rate_limits=RateLimitConfig(
                    group_reply_burst=1, group_reply_per_hour=0.0, mention_reply_burst=2, mention_reply_per_hour=0.0
                ),
            )

            self.assertIsNotNone(service.handle_event(_group_event("10001")))
            # Another bot keeps talking in the window until the group share is gone.
            self.assertIsNotNone(service.handle_event(_group_event(OTHER_BOT_ID, mention=False, text="本鱼更强")))
            with self.assertLogs("waifu_standalone.reply_flow.reply_gate", level="WARNING"):
                self.assertIsNone(service.handle_event(_group_event(OTHER_BOT_ID, mention=False, text="不服")))
            # A person who @s the bot still gets answers, from their own bucket.
            self.assertIsNotNone(service.handle_event(_group_event("783190298", text="今天天气怎么样")))
            self.assertIsNotNone(service.handle_event(_group_event("783190298", text="那明天呢")))
            with self.assertLogs("waifu_standalone.reply_flow.reply_gate", level="WARNING") as logs:
                self.assertIsNone(service.handle_event(_group_event("783190298", text="后天呢")))
            self.assertIn("bucket=group-mention", "\n".join(logs.output))
            # So does someone else.
            self.assertIsNotNone(service.handle_event(_group_event("1147026356", text="在吗")))

    def test_exempt_sender_and_disabled_limits_bypass_the_group_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = self._service(
                tmpdir,
                group_follow_up_window_seconds=60.0,
                rate_limits=RateLimitConfig(
                    group_reply_burst=1, group_reply_per_hour=0.0, exempt_user_ids=["783190298"]
                ),
            )

            self.assertIsNotNone(service.handle_event(_group_event("10001")))
            self.assertIsNotNone(service.handle_event(_group_event("10002", mention=False, text="然后呢")))
            with self.assertLogs("waifu_standalone.reply_flow.reply_gate", level="WARNING"):
                self.assertIsNone(service.handle_event(_group_event("10003", mention=False, text="还有吗")))
            self.assertIsNotNone(service.handle_event(_group_event("783190298", mention=False, text="我也说")))
            service.config.rate_limits.enabled = False
            self.assertIsNotNone(service.handle_event(_group_event("10003", mention=False, text="还有吗")))


class GuardConfigTests(unittest.TestCase):
    def _load(self, payload: dict[str, object], env: dict[str, str]) -> AppConfig:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("waifu_standalone.settings_admin.config_manager._napcat_config_dirs", return_value=[]):
                with patch.dict(os.environ, env):
                    return ConfigManager(path).load()

    def test_config_file_and_environment_set_the_guard(self) -> None:
        payload = {
            "ignore_user_ids": [OTHER_BOT_ID],
            "rate_limits": {"group_reply_burst": 3, "group_reply_per_hour": 12},
        }
        config = self._load(payload, {})
        self.assertEqual(config.ignore_user_ids, [OTHER_BOT_ID])
        self.assertEqual(config.rate_limits.group_reply_burst, 3)
        self.assertEqual(config.rate_limits.group_reply_per_hour, 12.0)
        defaults = self._load({}, {}).rate_limits
        self.assertEqual((defaults.group_reply_burst, defaults.group_reply_per_hour), (8, 60.0))
        self.assertEqual((defaults.mention_reply_burst, defaults.mention_reply_per_hour), (8, 120.0))
        mentions = self._load({"rate_limits": {"mention_reply_burst": 0, "mention_reply_per_hour": "12"}}, {}).rate_limits
        self.assertEqual((mentions.mention_reply_burst, mentions.mention_reply_per_hour), (0, 12.0))

        config = self._load(payload, {"OPENQQWAIFU_IGNORE_USER_IDS": "111,222"})
        self.assertEqual(config.ignore_user_ids, ["111", "222"])

    def test_empty_environment_value_keeps_the_configured_token(self) -> None:
        # compose.napcat.yml passes ${VAR:-} for optional settings: an unset variable
        # arrives as an empty string and must not wipe what the config file says.
        payload = {
            "qq_sidecar": {
                "access_token": "actions-secret",
                "inbound_access_token": "callback-secret",
                "reverse_ws_access_token": "ws-secret",
            }
        }
        env = {
            "OPENQQWAIFU_QQ_SIDECAR_ACCESS_TOKEN": "",
            "OPENQQWAIFU_QQ_SIDECAR_INBOUND_ACCESS_TOKEN": "",
            "OPENQQWAIFU_QQ_SIDECAR_REVERSE_WS_ACCESS_TOKEN": " ",
        }
        sidecar = self._load(payload, env).qq_sidecar
        self.assertEqual(sidecar.access_token, "actions-secret")
        self.assertEqual(sidecar.inbound_access_token, "callback-secret")
        self.assertEqual(sidecar.reverse_ws_access_token, "ws-secret")

        sidecar = self._load(payload, {"OPENQQWAIFU_QQ_SIDECAR_REVERSE_WS_ACCESS_TOKEN": "from-env"}).qq_sidecar
        self.assertEqual(sidecar.reverse_ws_access_token, "from-env")


if __name__ == "__main__":
    unittest.main()
