from __future__ import annotations

import sys
import tempfile
import threading
import time
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
from waifu_standalone.memory import InMemoryStore
from waifu_standalone.organs.memories import Memory
from waifu_standalone.services import CapturingOutboundPort


class _FakeNapCatLoginBridge:
    def __init__(self) -> None:
        self.refreshed = False

    def webui_url(self) -> str:
        return "http://127.0.0.1:6099/webui"

    def panel(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
        return {
            "configured": True,
            "token_configured": True,
            "webui_url": self.webui_url(),
            "status": {
                "is_login": self.refreshed,
                "is_offline": False,
                "qrcode_url": "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
                "login_error": "",
            },
            "login_info": {
                "uin": "3518944354",
                "nickname": "琉璃",
                "avatar_url": "",
                "online": self.refreshed,
            },
        }

    def refresh_qrcode(self):  # type: ignore[no-untyped-def]
        self.refreshed = True
        return self.panel(refresh=True)

    def qrcode_payload(self) -> str:
        return "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="


class _CoordinatedUserSaveStore(InMemoryStore):
    def __init__(self, target_launcher_id: str, target_launcher_type: str) -> None:
        super().__init__()
        self._target = (target_launcher_id, target_launcher_type)
        self._gate_lock = threading.Lock()
        self._gated_user_saves_remaining = 2
        self._release = threading.Event()

    def save(self, session):  # type: ignore[no-untyped-def]
        should_gate = False
        with self._gate_lock:
            if (
                (session.launcher_id, session.launcher_type) == self._target
                and session.history
                and not str(session.history[-1]).startswith("assistant: ")
                and self._gated_user_saves_remaining > 0
            ):
                self._gated_user_saves_remaining -= 1
                should_gate = True
                if self._gated_user_saves_remaining == 0:
                    self._release.set()
        if should_gate:
            self._release.wait(timeout=0.3)
        return super().save(session)


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
        member = self.service.state_store.get_member(group_id="", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "luna")

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

    def test_extract_image_prompt_accepts_fullwidth_colon(self) -> None:
        prompt = self.service._extract_image_prompt("生图：生成一个晴朗的天空")

        self.assertEqual(prompt, "生成一个晴朗的天空")

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

    def test_same_launcher_events_do_not_lose_history_under_concurrency(self) -> None:
        config = AppConfig(group_reply_requires_mention=False)
        service, outbound = build_default_service(config)
        store = _CoordinatedUserSaveStore("612475113", "group")
        service.memory = Memory(store)
        events = [
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="first message")],
            ),
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="second message")],
            ),
        ]
        results: list[object] = [None, None]
        threads = [
            threading.Thread(
                target=lambda idx=index, evt=event: results.__setitem__(idx, service.handle_event(evt)),
                name=f"worker-{index}",
            )
            for index, event in enumerate(events)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        session = service.memory.load("612475113", "group")
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outbound.sent), 2)
        self.assertEqual(sum(1 for line in session.history if line == "tester: first message"), 1)
        self.assertEqual(sum(1 for line in session.history if line == "tester: second message"), 1)
        self.assertEqual(sum(1 for line in session.history if line.startswith("assistant: ")), 2)
        self.assertTrue(all(result is not None for result in results))

    def test_different_launchers_can_progress_concurrently(self) -> None:
        config = AppConfig(group_reply_requires_mention=False)
        service, outbound = build_default_service(config)
        lock = threading.Lock()
        ready = threading.Event()
        release = threading.Event()
        active = 0
        max_active = 0

        def slow_generate(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                if active >= 2:
                    ready.set()
            ready.wait(timeout=1)
            release.wait(timeout=1)
            time.sleep(0.02)
            with lock:
                active -= 1
            return "concurrent reply"

        service.generator.generate_reply = slow_generate  # type: ignore[method-assign]
        events = [
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="hello from group one")],
            ),
            InboundEvent(
                launcher_id="1101040950",
                launcher_type="group",
                sender_id="783190299",
                sender_name="tester-two",
                segments=[MessageSegment(kind="text", text="hello from group two")],
            ),
        ]
        threads = [threading.Thread(target=service.handle_event, args=(event,)) for event in events]

        for thread in threads:
            thread.start()
        self.assertTrue(ready.wait(timeout=1), "different launchers should not serialize each other")
        release.set()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(len(outbound.sent), 2)

    def test_group_reply_can_be_opened_without_mentions(self) -> None:
        config = AppConfig(
            bot_account_id="3518944354",
            group_reply_requires_mention=False,
        )
        service, outbound = build_default_service(config)

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="hello there")],
            )
        )

        self.assertIsNotNone(result)
        self.assertEqual(len(outbound.sent), 1)

    def test_repeat_trigger_replies_when_same_group_line_is_repeated(self) -> None:
        config = AppConfig(repeat_trigger_count=2, group_reply_requires_mention=False)
        service, outbound = build_default_service(config)
        event = InboundEvent(
            launcher_id="612475113",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="hello")],
        )

        first = service.handle_event(event)
        second = service.handle_event(event)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn("重复2次", second.text)
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
        self.assertTrue(snapshot["group_reply_requires_mention"])
        self.assertEqual(snapshot["message_behavior"]["follow_up_window_seconds"], 5.0)
        self.assertEqual(snapshot["knowledge_count"], 0)
        self.assertEqual(snapshot["member_count"], 1)

    def test_behavior_graph_and_value_game_update_after_reply(self) -> None:
        config = AppConfig(
            bot_account_id="3518944354",
            group_reply_requires_mention=False,
            proactive_mode=True,
            proactive_inactive_hours=0.0,
            proactive_min_affinity=0.0,
        )
        service, _ = build_default_service(config)
        service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="hello there")],
            )
        )

        detail = service.get_session_detail("group", "612475113")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertTrue(detail["memory_graph"]["enabled"])
        self.assertGreaterEqual(len(service.get_behavior_events(limit=10)), 2)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertGreater(float(member["affinity_score"]), 0.0)
        proactive = service.get_proactive_panel(limit=5)
        self.assertGreaterEqual(len(proactive["candidates"]), 1)

    def test_qq_login_panel_and_qrcode_image_are_available(self) -> None:
        self.service.napcat_login = _FakeNapCatLoginBridge()

        panel = self.service.get_qq_login_panel(refresh=True)
        self.assertTrue(panel["configured"])
        self.assertTrue(panel["token_configured"])
        self.assertFalse(panel["status"]["is_login"])

        refreshed = self.service.refresh_qq_login_panel()
        self.assertTrue(refreshed["status"]["is_login"])

        asset = self.service.get_qq_login_qrcode_image()
        self.assertIsNotNone(asset)
        assert asset is not None
        body, content_type = asset
        self.assertEqual(content_type, "image/svg+xml")
        self.assertEqual(body, b"<svg></svg>")

    def test_qq_login_refresh_adopts_bot_account_id(self) -> None:
        self.service.napcat_login = _FakeNapCatLoginBridge()
        self.service.napcat_login.refreshed = True
        self.assertEqual(self.service.config.bot_account_id, "")

        self.service.get_qq_login_panel(refresh=True)

        self.assertEqual(self.service.config.bot_account_id, "3518944354")

    def test_save_qq_login_panel_normalizes_full_webui_url(self) -> None:
        service, _ = build_default_service(AppConfig())

        panel = service.save_qq_login_panel(
            {
                "webui_base_url": "http://127.0.0.1:6099/webui?token=secret-token",
                "webui_api_prefix": "/api",
                "webui_timeout_seconds": 10,
                "webui_token": "",
            }
        )

        self.assertEqual(service.config.qq_sidecar.webui_base_url, "http://127.0.0.1:6099")
        self.assertEqual(service.config.qq_sidecar.webui_token, "secret-token")
        self.assertEqual(panel["webui_base_url"], "http://127.0.0.1:6099")
        self.assertTrue(panel["token_configured"])

    def test_group_onboarding_prompts_and_saves_preferred_name(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        first = service.handle_event(
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
        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="luna")],
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None
        self.assertIn("称呼", first.text)
        self.assertIn("luna", second.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "luna")
        self.assertEqual(member["onboarding_status"], "ready")
        self.assertEqual(len(outbound.sent), 2)

    def test_group_onboarding_does_not_store_freeform_complaint_as_name(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        first = service.handle_event(
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
        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 什么玩意儿"),
                ],
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn("称呼", second.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "")
        self.assertEqual(member["onboarding_status"], "pending_name")
        self.assertEqual(len(outbound.sent), 2)

    def test_group_onboarding_does_not_store_greeting_as_name(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        first = service.handle_event(
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
        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 你好"),
                ],
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn("称呼", second.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "")
        self.assertEqual(member["onboarding_status"], "pending_name")
        self.assertEqual(len(outbound.sent), 2)

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

    def test_save_character_panel_persists_shared_active_character(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(data_root=tmpdir, character="default")
            service, _ = build_default_service(config)

            saved = service.save_character_panel(
                {
                    "character": "aurora",
                    "set_active": True,
                    "shared_fields": {
                        "assistant_name": "Aurora",
                        "user_name": "Captain",
                        "language": "zh",
                    },
                    "person_fields": {"profile": ["calm"]},
                    "group_fields": {"profile": ["quick"]},
                }
            )

            reloaded, _ = build_default_service(AppConfig(data_root=tmpdir, character="default"))

            self.assertEqual(saved["current_character"], "aurora")
            self.assertEqual(reloaded.get_character_panel()["current_character"], "aurora")

    def test_preview_character_panel_uses_unsaved_editor_fields(self) -> None:
        preview = self.service.preview_character_panel(
            {
                "launcher_type": "group",
                "user_name": "Captain",
                "message": "你现在应该叫我什么？",
                "shared_fields": {
                    "assistant_name": "Aurora",
                    "user_name": "Captain",
                    "language": "简体中文",
                },
                "person_fields": {
                    "profile": ["soft"],
                    "skills": ["keeps continuity"],
                    "background": ["private chat"],
                    "rules": ["stay concise"],
                    "prologue": ["lights on"],
                },
                "group_fields": {
                    "profile": ["playful in groups"],
                    "skills": ["tracks speakers"],
                    "background": ["group chat"],
                    "rules": ["keep it short"],
                    "prologue": ["new notification"],
                },
            }
        )

        self.assertEqual(preview["launcher_type"], "group")
        self.assertEqual(preview["assistant_name"], "Aurora")
        self.assertEqual(preview["user_name"], "Captain")
        self.assertTrue(preview["reply_text"])
        self.assertTrue(preview["analysis_hint"])
        self.assertEqual(len(preview["transcript"]), 2)
        self.assertEqual(preview["transcript"][0]["role"], "user")
        self.assertEqual(preview["transcript"][1]["role"], "assistant")

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
        knowledge_entries = service.state_store.list_knowledge(limit=8)

        self.assertLessEqual(len(session.history), 4)
        self.assertNotIn("long_term_memory", session.metadata)
        self.assertEqual(service.state_store.knowledge_count(), 1)
        self.assertEqual(knowledge_entries[0]["memory_type"], "summary")

    def test_session_preferred_name_is_resolved_from_directory(self) -> None:
        self.service.state_store.save_member(
            {
                "group_id": "",
                "user_id": "783190298",
                "preferred_name": "luna",
                "onboarding_status": "ready",
            }
        )
        self.service.memory.save_user_message("783190298", "person", "tester", "hello")

        detail = self.service.get_session_detail("person", "783190298")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["preferred_name"], "luna")

    def test_migrate_legacy_session_state_moves_names_and_summaries(self) -> None:
        person_session = self.service.memory.load("783190298", "person")
        person_session.preferred_name = "luna"
        person_session.metadata["long_term_memory"] = [
            {
                "summary": "tester likes rainy nights",
                "tags": ["rain", "night"],
            }
        ]
        self.service.memory.store.save(person_session)

        group_session = self.service.memory.load("612475113", "group")
        group_session.metadata["group_members"] = {
            "783190298": {
                "sender_id": "783190298",
                "sender_name": "tester",
                "preferred_name": "luna",
                "profile_summary": "likes cats",
            }
        }
        self.service.memory.store.save(group_session)

        self.service._migrate_legacy_session_state()

        migrated_person = self.service.memory.load("783190298", "person")
        migrated_group = self.service.memory.load("612475113", "group")
        person_member = self.service.state_store.get_member(group_id="", user_id="783190298")
        group_member = self.service.state_store.get_member(group_id="612475113", user_id="783190298")
        knowledge_entries = self.service.state_store.list_knowledge(limit=8)

        self.assertEqual(migrated_person.preferred_name, "")
        self.assertNotIn("long_term_memory", migrated_person.metadata)
        self.assertNotIn("group_members", migrated_group.metadata)
        self.assertIsNotNone(person_member)
        self.assertIsNotNone(group_member)
        assert person_member is not None
        assert group_member is not None
        self.assertEqual(person_member["preferred_name"], "luna")
        self.assertEqual(group_member["preferred_name"], "luna")
        self.assertEqual(group_member["profile_summary"], "likes cats")
        self.assertEqual(len(knowledge_entries), 1)
        self.assertEqual(knowledge_entries[0]["summary"], "tester likes rainy nights")

    def test_save_other_panel_round_trips_group_runtime_settings(self) -> None:
        service, _ = build_default_service(AppConfig())

        panel = service.save_other_panel(
            {
                "service_name": "openqqwaifu",
                "assistant_name": "琉璃",
                "bot_account_id": "3518944354",
                "group_reply_requires_mention": False,
                "image_command_prefix": "生图",
                "image_command_aliases": ["生图", "draw"],
                "ignore_prefixes": ["!", "/"],
                "group_follow_up_window_seconds": 9,
                "group_response_delay_seconds": 1.5,
                "repeat_trigger_count": 3,
                "multimodal_enabled": False,
            }
        )

        self.assertFalse(panel["group_reply_requires_mention"])
        self.assertEqual(panel["group_response_delay_seconds"], 1.5)
        self.assertEqual(panel["repeat_trigger_count"], 3)
        self.assertFalse(panel["multimodal_enabled"])

    def test_save_ai_panel_round_trips_provider_models(self) -> None:
        service, _ = build_default_service(AppConfig())

        panel = service.save_ai_panel(
            {
                "llm": {
                    "enabled": True,
                    "backend": "openai",
                    "base_url": "https://api.x.ai/v1",
                    "api_key": "secret",
                    "model": "grok-3-mini",
                    "timeout_seconds": 33,
                },
                "embedding": {
                    "enabled": True,
                    "backend": "openai",
                    "base_url": "https://api.example.com/v1",
                    "api_key": "secret",
                    "model": "text-embedding-3-small",
                    "timeout_seconds": 22,
                }
            }
        )

        llm = panel["llm"]
        embedding = panel["embedding"]
        self.assertTrue(llm["enabled"])
        self.assertEqual(llm["backend"], "openai")
        self.assertEqual(llm["base_url"], "https://api.x.ai/v1")
        self.assertEqual(llm["model"], "grok-3-mini")
        self.assertEqual(llm["timeout_seconds"], 33.0)
        self.assertTrue(embedding["enabled"])
        self.assertEqual(embedding["base_url"], "https://api.example.com/v1")
        self.assertEqual(embedding["model"], "text-embedding-3-small")
        self.assertEqual(embedding["timeout_seconds"], 22.0)

    def test_runtime_service_uses_capture_outbound_when_sidecar_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                qq_sidecar=QQSidecarConfig(outbound_base_url=""),
            )

            _, outbound = build_runtime_service(config)

            self.assertIsInstance(outbound, CapturingOutboundPort)

    def test_runtime_service_uses_onebot_outbound_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                qq_sidecar=QQSidecarConfig(
                    outbound_base_url="http://127.0.0.1:3000",
                ),
            )

            _, outbound = build_runtime_service(config)

            self.assertIsInstance(outbound, OneBotHttpOutboundPort)

    def test_live_runtime_service_requires_llm_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                qq_sidecar=QQSidecarConfig(outbound_base_url="http://127.0.0.1:3000"),
            )
            service, _ = build_runtime_service(config)

            result = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="hello")],
                )
            )

            self.assertIsNone(result)

    def test_live_runtime_service_suppresses_local_fallback_on_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                qq_sidecar=QQSidecarConfig(outbound_base_url="http://127.0.0.1:3000"),
            )
            config.llm.enabled = True
            config.llm.backend = "openai"
            config.llm.base_url = "http://127.0.0.1:1"
            config.llm.api_key = "test-key"
            config.llm.model = "test-model"
            service, _ = build_runtime_service(config)

            result = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="hello")],
                )
            )

            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
