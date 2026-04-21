from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service, build_file_service, build_runtime_service
from waifu_standalone.config import AppConfig, QQSidecarConfig
from waifu_standalone.contracts import GeneratedImage
from waifu_standalone.gateways.onebot_actions import OneBotHttpOutboundPort
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.memory import FileMemoryStore, InMemoryStore
from waifu_standalone.organs.memories import Memory
from waifu_standalone.services import CapturingOutboundPort
from waifu_standalone.state_store import SqliteRuntimeStateStore
from waifu_standalone.systems.searching import SearchContext, SearchResult


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
    def __init__(self, target_character_id: str, target_launcher_id: str, target_launcher_type: str) -> None:
        super().__init__()
        self._target = (target_character_id, target_launcher_id, target_launcher_type)
        self._gate_lock = threading.Lock()
        self._gated_user_saves_remaining = 2
        self._release = threading.Event()

    def save(self, session):  # type: ignore[no-untyped-def]
        should_gate = False
        with self._gate_lock:
            if (
                (session.character_id, session.launcher_id, session.launcher_type) == self._target
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


class _DelayedCaptureOutbound(CapturingOutboundPort):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def send(self, message) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("sync send should not run for delayed async outbound")

    async def send_async(self, message) -> None:  # type: ignore[no-untyped-def]
        with self._lock:
            self.sent.append(message)


class WaifuServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.outbound = build_default_service()

    @staticmethod
    def _write_group_cards(root: Path) -> None:
        cards_dir = root / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        (cards_dir / "default_group.yaml").write_text(
            "\n".join(
                [
                    "assistant_name: 琉璃",
                    "user_name: 用户",
                    "language: 简体中文",
                    "Profile:",
                    "  - 琉璃",
                ]
            ),
            encoding="utf-8",
        )
        (cards_dir / "aurora_group.yaml").write_text(
            "\n".join(
                [
                    "assistant_name: 极光",
                    "user_name: 用户",
                    "language: 简体中文",
                    "Profile:",
                    "  - 极光",
                ]
            ),
            encoding="utf-8",
        )
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
        prompt = self.service.dispatcher.extract_image_prompt("生图：生成一个晴朗的天空")

        self.assertEqual(prompt, "生成一个晴朗的天空")

    def test_switching_active_character_changes_reply_style_in_same_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_group_cards(root)
            service, _ = build_default_service(
                AppConfig(
                    data_root=str(root),
                    group_reply_requires_mention=False,
                    character="default",
                )
            )

            service.generator.generate_reply = (  # type: ignore[method-assign]
                lambda event, session, emotion, **kwargs: service.cards.load(event.launcher_type, session).assistant_name
            )
            service.cards.set_active_character("default")
            first = service.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="first hello")],
                )
            )

            service.cards.set_active_character("aurora")
            second = service.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="second hello")],
                )
            )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertTrue(first.text.startswith("琉璃"))
            self.assertTrue(second.text.startswith("极光"))

    def test_repair_character_isolation_state_cleans_cross_persona_pollution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cards_dir = root / "cards"
            cards_dir.mkdir(parents=True, exist_ok=True)
            (cards_dir / "default_group.yaml").write_text(
                "assistant_name: Liuli\nuser_name: User\nlanguage: zh\nProfile:\n  - warm\n",
                encoding="utf-8",
            )
            (cards_dir / "aurora_group.yaml").write_text(
                "assistant_name: Aurora\nuser_name: Captain\nlanguage: zh\nProfile:\n  - calm\n",
                encoding="utf-8",
            )
            service, _ = build_default_service(
                AppConfig(
                    data_root=str(root),
                    group_reply_requires_mention=False,
                    character="aurora",
                )
            )
            service.cards.set_active_character("aurora")
            polluted = service.memory.load("612475113", "group", character_id="aurora")
            polluted.history = [
                "tester: who are you",
                "assistant: I am Liuli, your catgirl succubus.",
            ]
            service.memory.store.save(polluted)
            service.state_store.save_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "character_id": "aurora",
                    "qq_nickname": "tester",
                    "preferred_name": "captain",
                    "onboarding_status": "ready",
                    "profile_summary": "Liuli is clingy; Aurora speaks crisply; likes ramen",
                }
            )

            service.migrator.repair_character_isolation("aurora")

            repaired = service.memory.load("612475113", "group", character_id="aurora")
            member = service.state_store.get_member(
                group_id="612475113",
                user_id="783190298",
                character_id="aurora",
            )

            self.assertFalse(any("Liuli" in line for line in repaired.history))
            self.assertIsNotNone(member)
            assert member is not None
            self.assertEqual(member["profile_summary"], "likes ramen")

    def test_handle_event_ignores_cross_persona_history_and_profile_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cards_dir = root / "cards"
            cards_dir.mkdir(parents=True, exist_ok=True)
            (cards_dir / "default_group.yaml").write_text(
                "assistant_name: Liuli\nuser_name: User\nlanguage: zh\nProfile:\n  - warm\n",
                encoding="utf-8",
            )
            (cards_dir / "aurora_group.yaml").write_text(
                "assistant_name: Aurora\nuser_name: Captain\nlanguage: zh\nProfile:\n  - calm\n",
                encoding="utf-8",
            )
            service, _ = build_default_service(
                AppConfig(
                    data_root=str(root),
                    group_reply_requires_mention=False,
                    character="aurora",
                )
            )
            service.cards.set_active_character("aurora")
            polluted = service.memory.load("612475113", "group", character_id="aurora")
            polluted.history = [
                "tester: who are you",
                "assistant: I am Liuli, your catgirl succubus.",
            ]
            service.memory.store.save(polluted)
            service.state_store.save_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "character_id": "aurora",
                    "qq_nickname": "tester",
                    "preferred_name": "captain",
                    "onboarding_status": "ready",
                    "profile_summary": "Liuli is clingy; Aurora speaks crisply; likes ramen",
                }
            )
            captured: dict[str, object] = {}

            def fake_generate_reply(event, session, emotion, **kwargs):  # type: ignore[no-untyped-def]
                captured["conversation_view"] = kwargs.get("conversation_view", "")
                captured["speaker_notes"] = list(kwargs.get("speaker_notes", []))
                return service.cards.load(event.launcher_type, session).assistant_name

            service.generator.generate_reply = fake_generate_reply  # type: ignore[method-assign]

            result = service.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="say it again")],
                )
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.text, "Aurora")
            self.assertNotIn("Liuli", str(captured.get("conversation_view", "")))
            self.assertFalse(any("Liuli" in str(item) for item in captured.get("speaker_notes", [])))

    def test_llm_user_key_includes_character_and_launcher_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_group_cards(root)
            config = AppConfig(
                data_root=str(root),
                group_reply_requires_mention=False,
                character="aurora",
            )
            service, _ = build_default_service(config)
            service.cards.set_active_character("aurora")
            service.config.llm.enabled = True
            service.generator._dify_client.base_url = "https://example.com"
            service.generator._dify_client.api_key = "test-key"
            service.generator._dify_client.model = "test-model"
            service.generator._dify_client.backend = "openai"
            calls: list[str] = []

            def fake_invoke(prompt: str, *, user: str = "waifu-standalone") -> str:
                calls.append(user)
                return "极光"

            service.generator._dify_client.invoke = fake_invoke  # type: ignore[method-assign]

            result = service.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="hello aurora")],
                )
            )

            self.assertIsNotNone(result)
            self.assertTrue(any("aurora:group:612475113:783190298" in call for call in calls))

    def test_reset_directory_member_persona_keeps_shared_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_group_cards(root)
            service, _ = build_default_service(
                AppConfig(
                    data_root=str(root),
                    group_reply_requires_mention=False,
                    character="aurora",
                )
            )
            service.cards.set_active_character("aurora")
            saved = service.save_directory_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "qq_nickname": "tester",
                    "preferred_name": "爸爸",
                    "onboarding_status": "ready",
                    "profile_summary": "Aurora stays calm",
                    "affinity_score": 0.72,
                    "notes_count": 4,
                }
            )

            self.assertEqual(saved["profile_summary"], "Aurora stays calm")
            reset = service.reset_directory_member_persona(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                }
            )

            self.assertIsNotNone(reset)
            assert reset is not None
            self.assertEqual(reset["preferred_name"], "爸爸")
            self.assertEqual(reset["qq_nickname"], "tester")
            self.assertEqual(reset["profile_summary"], "")
            self.assertEqual(float(reset["affinity_score"]), 0.0)
            self.assertEqual(int(reset["notes_count"]), 0)

    def test_delete_knowledge_entry_respects_active_character(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_group_cards(root)
            service, _ = build_default_service(
                AppConfig(
                    data_root=str(root),
                    group_reply_requires_mention=False,
                    character="aurora",
                )
            )
            service.cards.set_active_character("aurora")
            aurora_entry = service.save_knowledge_entry(
                {
                    "scope_type": "group",
                    "scope_id": "612475113",
                    "memory_type": "fact",
                    "summary": "Aurora likes clean prompts",
                }
            )
            service.state_store.save_knowledge(
                {
                    "character_id": "default",
                    "scope_type": "group",
                    "scope_id": "612475113",
                    "memory_type": "fact",
                    "summary": "Liuli likes sugar",
                }
            )

            removed = service.delete_knowledge_entry(int(aurora_entry["id"]))

            self.assertTrue(removed)
            remaining = service.state_store.list_knowledge(limit=10, character_id="aurora")
            self.assertEqual(remaining, [])
            default_entries = service.state_store.list_knowledge(limit=10, character_id="default")
            self.assertEqual(len(default_entries), 1)

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

    def test_group_follow_up_window_survives_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                bot_account_id="3518944354",
                group_follow_up_window_seconds=16.0,
            )
            service1, _ = build_file_service(config)
            service1.state_store.save_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "qq_nickname": "tester",
                    "preferred_name": "爸爸",
                    "onboarding_status": "ready",
                }
            )
            first = service1.handle_event(
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
            service1.close(timeout=5.0)

            service2, outbound2 = build_file_service(config)
            second = service2.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="继续说呀")],
                )
            )
            service2.close(timeout=5.0)

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(len(outbound2.sent), 1)

    def test_pending_search_confirmation_can_reply_without_second_mention(self) -> None:
        config = AppConfig(
            bot_account_id="3518944354",
            group_follow_up_window_seconds=0.0,
        )
        service, _ = build_default_service(config)
        service.state_store.save_member(
            {
                "group_id": "612475113",
                "user_id": "783190298",
                "qq_nickname": "tester",
                "preferred_name": "爸爸",
                "onboarding_status": "ready",
            }
        )

        original_query = "能不能帮我看看现在的小米公司股价是多少？"
        service.search.build_context = lambda event: SearchContext(  # type: ignore[method-assign]
            query=original_query,
            summary="这类问题通常需要联网确认，但这次没有拿到可靠结果。",
            results=[],
            fetched_at=time.time(),
            reason="keyword-hit:no-results",
        )
        service.generator.generate_reply = lambda *args, **kwargs: "爸爸，这种实时股价最好核验一下，要我再帮你查查吗？"  # type: ignore[method-assign]

        first = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=f" {original_query}"),
                ],
            )
        )

        service.search.search_query = lambda query, reason="manual": SearchContext(  # type: ignore[method-assign]
            query=query,
            summary="小米集团当前股价示例为 42 港元。",
            results=[SearchResult(title="小米集团", snippet="当前股价示例为 42 港元。")],
            fetched_at=time.time(),
            reason=reason,
        )

        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="好的，你帮我查查吧")],
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn("小米集团", second.text)
        self.assertIn("42 港元", second.text)

    def test_handle_event_uses_async_search_context_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(search_enabled=True, data_root=tmpdir))
            async_called = threading.Event()

            async def fake_abuild_context(event: InboundEvent) -> SearchContext:
                async_called.set()
                return SearchContext(
                    query="北京天气",
                    summary="北京天气：今天晴，最高温 26 度。",
                    results=[SearchResult(title="北京天气", snippet="今天晴，最高温 26 度。")],
                    fetched_at=time.time(),
                    reason="keyword-hit",
                )

            def fail_build_context(event: InboundEvent) -> SearchContext:
                raise AssertionError("sync build_context should not be used")

            service.search.should_search = lambda event: True  # type: ignore[method-assign]
            service.search.abuild_context = fake_abuild_context  # type: ignore[method-assign]
            service.search.build_context = fail_build_context  # type: ignore[method-assign]

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="今天北京天气怎么样")],
                )
            )

            self.assertTrue(async_called.is_set())
            self.assertIsNotNone(reply)
            session = service.memory.load("783190298", "person")
            last_search = session.metadata.get("last_search", {})
            self.assertEqual(last_search.get("query"), "北京天气")

    def test_pending_search_clarification_extends_query_without_second_mention(self) -> None:
        config = AppConfig(
            bot_account_id="3518944354",
            group_follow_up_window_seconds=0.0,
        )
        service, _ = build_default_service(config)
        service.state_store.save_member(
            {
                "group_id": "612475113",
                "user_id": "783190298",
                "qq_nickname": "tester",
                "preferred_name": "爸爸",
                "onboarding_status": "ready",
            }
        )

        original_query = "能不能帮我查查今天的股价是多少"
        service.search.build_context = lambda event: SearchContext(  # type: ignore[method-assign]
            query=original_query,
            summary="这类问题通常需要联网确认，但这次没有拿到可靠结果。",
            results=[],
            fetched_at=time.time(),
            reason="keyword-hit:no-results",
        )
        service.generator.generate_reply = lambda *args, **kwargs: "爸爸，股价实时变动，最好自己联网核验最新。"  # type: ignore[method-assign]

        first = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=f" {original_query}"),
                ],
            )
        )

        captured_queries: list[str] = []

        def fake_search(query: str, reason: str = "manual") -> SearchContext:
            captured_queries.append(query)
            return SearchContext(
                query=query,
                summary="小米集团当前股价示例为 42 港元。",
                results=[SearchResult(title="小米集团", snippet="当前股价示例为 42 港元。")],
                fetched_at=time.time(),
                reason=reason,
            )

        service.search.search_query = fake_search  # type: ignore[method-assign]

        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="小米公司的哦")],
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertTrue(any("小米公司的哦" in query for query in captured_queries))

    def test_search_link_followup_returns_real_urls_without_generator(self) -> None:
        service, _ = build_default_service()
        session = service.memory.load("783190298", "person")
        session.metadata["last_search"] = SearchContext(
            query="拼多多 罚款 2026",
            summary="拼多多因幽灵外卖系列案被罚。",
            results=[
                SearchResult(
                    title="市场监管总局依法对拼多多等7家平台作出行政处罚",
                    snippet="市场监管总局公布行政处罚结果。",
                    url="https://www.samr.gov.cn/xw/zj/art/2026/art_9714ee2bef9244819134ef4ffdd14017.html",
                ),
                SearchResult(
                    title="腾讯新闻报道",
                    snippet="拼多多被罚 15.22 亿元。",
                    url="https://news.qq.com/rain/a/20260417A07W5G00",
                ),
            ],
            fetched_at=time.time(),
            reason="manual",
        ).as_dict()
        service.memory.store.save(session)

        def fail_generate_reply(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("generator should not run for explicit link follow-up")

        service.generator.generate_reply = fail_generate_reply  # type: ignore[method-assign]

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="把23年罚钱的链接发给我")],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("真实链接", reply.text)
        self.assertIn("https://www.samr.gov.cn/xw/zj/art/2026/art_9714ee2bef9244819134ef4ffdd14017.html", reply.text)
        self.assertIn("https://news.qq.com/rain/a/20260417A07W5G00", reply.text)

    def test_search_link_followup_without_urls_refuses_to_fabricate(self) -> None:
        service, _ = build_default_service()
        session = service.memory.load("783190298", "person")
        session.metadata["last_search"] = SearchContext(
            query="拼多多 罚款 2026",
            summary="拼多多因幽灵外卖系列案被罚。",
            results=[
                SearchResult(
                    title="只拿到摘要",
                    snippet="这里只有摘要，没有稳定来源链接。",
                    url="",
                )
            ],
            fetched_at=time.time(),
            reason="manual",
        ).as_dict()
        service.memory.store.save(session)

        def fail_generate_reply(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("generator should not run for explicit link follow-up")

        service.generator.generate_reply = fail_generate_reply  # type: ignore[method-assign]

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="原文链接呢")],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("不能给你乱编", reply.text)
        self.assertNotIn("http://", reply.text)
        self.assertNotIn("https://", reply.text)

    def test_same_launcher_events_do_not_lose_history_under_concurrency(self) -> None:
        config = AppConfig(group_reply_requires_mention=False)
        service, outbound = build_default_service(config)
        current_character = service._active_character_id()
        store = _CoordinatedUserSaveStore(current_character, "612475113", "group")
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

        session = service.memory.load("612475113", "group", character_id=current_character)
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

    def test_group_response_delay_does_not_block_request_thread_for_real_outbound(self) -> None:
        outbound = _DelayedCaptureOutbound()
        delay_seconds = 0.5
        service, _ = build_default_service(
            AppConfig(
                bot_account_id="3518944354",
                group_reply_requires_mention=False,
                group_response_delay_seconds=delay_seconds,
            )
        )
        service.outbound = outbound

        started = time.monotonic()
        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="hello there")],
            )
        )
        elapsed = time.monotonic() - started

        self.assertIsNotNone(result)
        self.assertLess(elapsed, delay_seconds * 0.5)
        self.assertEqual(len(outbound.sent), 0)

        service.flush_background_tasks(timeout=2.0)
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
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(bot_account_id="3518944354", data_root=tmpdir, character="default")
            service, _ = build_default_service(config)
            service.cards.set_active_character("default")
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

            self.assertEqual(snapshot["assistant_name"], service.cards.load("group", service.memory.load("612475113", "group", character_id="default")).assistant_name)
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
        shared_member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        scoped_member = service.state_store.get_member(
            group_id="612475113",
            user_id="783190298",
            character_id=service.cards.active_character(),
        )
        self.assertIsNotNone(shared_member)
        self.assertIsNotNone(scoped_member)
        assert shared_member is not None and scoped_member is not None
        self.assertAlmostEqual(float(shared_member["affinity_score"]), 0.0, places=4)
        self.assertGreater(float(scoped_member["affinity_score"]), 0.0)
        proactive = service.get_proactive_panel(limit=5)
        self.assertGreaterEqual(len(proactive["candidates"]), 1)

    def test_value_game_updates_active_character_persona_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_group_cards(root)
            service, _ = build_default_service(
                AppConfig(
                    data_root=str(root),
                    character="aurora",
                    group_reply_requires_mention=False,
                )
            )

            service.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="hello aurora")],
                )
            )

            shared = service.state_store.get_member(group_id="612475113", user_id="783190298")
            scoped = service.state_store.get_member(
                group_id="612475113",
                user_id="783190298",
                character_id="aurora",
            )

            self.assertIsNotNone(shared)
            self.assertIsNotNone(scoped)
            assert shared is not None and scoped is not None
            self.assertAlmostEqual(float(shared["affinity_score"]), 0.0, places=4)
            self.assertGreater(float(scoped["affinity_score"]), 0.0)

    def test_save_character_panel_generates_portrait_when_image_provider_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            service.config.image_generation.enabled = True
            service.generator._image_client.enabled = True  # type: ignore[attr-defined]
            service.generator.generate_image = lambda prompt: GeneratedImage(  # type: ignore[method-assign]
                prompt=prompt,
                image_ref="generated://aurora-portrait",
            )
            service.generator.resolve_generated_image = lambda image_ref: (  # type: ignore[method-assign]
                b"fakepngbytes",
                "image/png",
            )

            bundle = service.console.save_character_panel(
                {
                    "character": "aurora",
                    "set_active": False,
                    "shared_fields": {
                        "assistant_name": "极光",
                        "user_name": "主人",
                        "language": "简体中文",
                    },
                    "person_fields": {
                        "profile": ["安静", "锐利"],
                        "skills": ["会接话"],
                        "background": ["你们正在私聊。"],
                        "rules": ["自然回复。"],
                        "prologue": ["屏幕轻轻亮起。"],
                    },
                    "group_fields": {
                        "profile": ["群里反应快"],
                        "skills": ["会接梗"],
                        "background": ["你在一个群聊里。"],
                        "rules": ["不要刷屏。"],
                        "prologue": ["群消息不断刷新。"],
                    },
                    "portrait": {
                        "style": "dream-anime",
                        "prompt_suffix": "blue hair",
                        "auto_generate": True,
                        "generate": True,
                    },
                }
            )

            self.assertTrue(bundle["portrait"]["available"])
            self.assertEqual(bundle["portrait"]["style"], "dream-anime")
            self.assertIn("/api/portraits?character=aurora", bundle["portrait"]["url"])
            stored = service.cards.load_portrait_asset("aurora")
            self.assertIsNotNone(stored)

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
        self.assertEqual(service.config.qq_sidecar.webui_api_prefix, "/api")

    def test_group_first_contact_replies_first_and_soft_asks_once(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)
        service.generator.generate_reply = lambda *args, **kwargs: "normal-reply"  # type: ignore[method-assign]

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
                    MessageSegment(kind="text", text=" 还在吗"),
                ],
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertIn("normal-reply", first.text)
        self.assertIn("叫我 xx", first.text)
        self.assertEqual(second.text, "normal-reply")
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "")
        self.assertEqual(member["onboarding_status"], "asked_once")
        self.assertEqual(len(outbound.sent), 2)

    def test_group_onboarding_accepts_explicit_call_me_phrase(self) -> None:
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
                    MessageSegment(kind="text", text=" 叫我爷爷"),
                ],
            )
        )

        self.assertIsNotNone(first)
        assert first is not None
        self.assertIn("我叫你爷爷", first.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "爷爷")
        self.assertEqual(member["onboarding_status"], "ready")
        self.assertEqual(len(outbound.sent), 1)

    def test_group_onboarding_ignores_unmentioned_self_intro_without_prompt(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="我是马云的狗")],
            )
        )

        self.assertIsNone(result)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNone(member)
        self.assertEqual(len(outbound.sent), 0)

    def test_group_onboarding_accepts_mentioned_self_intro(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 我是阿狗"),
                ],
            )
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("我叫你阿狗", result.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "阿狗")
        self.assertEqual(member["onboarding_status"], "ready")
        self.assertEqual(len(outbound.sent), 1)

    def test_group_onboarding_accepts_passive_name_after_soft_ask(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)
        service.generator.generate_reply = lambda *args, **kwargs: "normal-reply"  # type: ignore[method-assign]

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
        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" luna"),
                ],
            )
        )

        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn("我叫你luna", second.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "luna")
        self.assertEqual(member["onboarding_status"], "ready")
        self.assertEqual(len(outbound.sent), 2)

    def test_group_onboarding_accepts_llm_name_hint_after_soft_ask(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)
        service.generator.generate_reply = lambda *args, **kwargs: "normal-reply"  # type: ignore[method-assign]

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
        service.generator.extract_preferred_name_hint = (  # type: ignore[method-assign]
            lambda *args, **kwargs: {"name": "爸爸", "confidence": 0.92, "is_self_intro": True}
        )
        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 以后就这么叫吧"),
                ],
            )
        )

        self.assertIsNotNone(second)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "爸爸")
        self.assertEqual(member["onboarding_status"], "ready")
        self.assertEqual(len(outbound.sent), 2)

    def test_group_onboarding_rejects_third_party_call_phrase_with_explicit_reply(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 叫他爷爷"),
                ],
            )
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("不能替别人决定", result.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "")
        self.assertEqual(member["assistant_alias"], "")
        self.assertEqual(member["onboarding_status"], "new")
        self.assertEqual(len(outbound.sent), 1)

    def test_group_onboarding_clarifies_non_declarative_naming_question(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 你该叫我什么"),
                ],
            )
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("叫我 xx", result.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "")
        self.assertEqual(member["onboarding_status"], "new")
        self.assertEqual(len(outbound.sent), 1)

    def test_group_assistant_alias_command_persists_for_same_character(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)
        current_character = service._active_character_id()

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 以后就叫你阿璃"),
                ],
            )
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("那就叫我阿璃", result.text)
        alias = service.state_store.get_assistant_alias(character_id=current_character, user_id="783190298")
        self.assertIsNotNone(alias)
        assert alias is not None
        self.assertEqual(alias["assistant_alias"], "阿璃")

        private_result = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="hello there")],
            )
        )

        self.assertIsNotNone(private_result)
        assert private_result is not None
        self.assertIn("阿璃", private_result.text)
        self.assertEqual(len(outbound.sent), 2)

    def test_group_address_command_requires_mention(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="/叫我 爸爸")],
            )
        )

        self.assertIsNone(result)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNone(member)
        self.assertEqual(len(outbound.sent), 0)

    def test_group_onboarding_requires_mention_even_after_soft_ask(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)
        service.generator.generate_reply = lambda *args, **kwargs: "normal-reply"  # type: ignore[method-assign]

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
        assert second is not None
        self.assertEqual(second.text, "normal-reply")
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "")
        self.assertEqual(member["onboarding_status"], "asked_once")
        self.assertEqual(len(outbound.sent), 2)

    def test_group_onboarding_accepts_llm_naming_route_for_vague_follow_up_with_mention(self) -> None:
        config = AppConfig(bot_account_id="3518944354")
        service, outbound = build_default_service(config)
        service.generator.generate_reply = lambda *args, **kwargs: "normal-reply"  # type: ignore[method-assign]

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
        service.naming_router._routing_ready = lambda: True  # type: ignore[method-assign]

        async def fake_route(*args, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "mode": "set_preferred_name",
                "preferred_name": "小满",
                "assistant_alias": "",
                "clarification_text": "",
                "reason": "follow-up naming",
            }

        service.generator.aresolve_naming_intent = fake_route  # type: ignore[method-assign]
        second = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" 那你以后就这么叫我吧"),
                ],
            )
        )

        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn("我叫你小满", second.text)
        member = service.state_store.get_member(group_id="612475113", user_id="783190298")
        self.assertIsNotNone(member)
        assert member is not None
        self.assertEqual(member["preferred_name"], "小满")
        self.assertEqual(member["onboarding_status"], "ready")
        self.assertEqual(len(outbound.sent), 2)

    def test_group_reply_does_not_leak_other_members_name_into_speaker_notes(self) -> None:
        config = AppConfig(group_reply_requires_mention=False)
        service, _ = build_default_service(config)
        service.state_store.save_member(
            {
                "group_id": "612475113",
                "user_id": "783190298",
                "qq_nickname": "tester",
                "preferred_name": "爸爸",
                "onboarding_status": "ready",
            }
        )
        service.state_store.save_member(
            {
                "group_id": "612475113",
                "user_id": "999999999",
                "qq_nickname": "other",
                "preferred_name": "爷爷",
                "profile_summary": "你这太机械了",
                "onboarding_status": "ready",
            }
        )
        captured: dict[str, object] = {}

        def fake_generate_reply(event, session, emotion, **kwargs):  # type: ignore[no-untyped-def]
            captured["speaker_notes"] = list(kwargs.get("speaker_notes", []))
            return "ok"

        service.generator.generate_reply = fake_generate_reply  # type: ignore[method-assign]

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
        notes = [str(item) for item in captured.get("speaker_notes", [])]
        self.assertTrue(any("爸爸" in item for item in notes))
        self.assertFalse(any("爷爷" in item for item in notes))
        self.assertFalse(any("你这太机械了" in item for item in notes))

    def test_preference_message_writes_back_member_knowledge(self) -> None:
        config = AppConfig(group_reply_requires_mention=False, knowledge_auto_extract=True)
        service, _ = build_default_service(config)

        result = service.handle_event(
            InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="我喜欢雨天和猫")],
            )
        )
        service.flush_background_tasks(timeout=5.0)

        self.assertIsNotNone(result)
        knowledge_entries = service.state_store.list_knowledge(
            limit=8,
            character_id=service.cards.active_character(),
        )
        self.assertEqual(len(knowledge_entries), 1)
        self.assertEqual(knowledge_entries[0]["scope_type"], "member")
        self.assertEqual(knowledge_entries[0]["scope_id"], "612475113:783190298")
        self.assertIn("Likes", knowledge_entries[0]["summary"])
        member = service.state_store.get_member(
            group_id="612475113",
            user_id="783190298",
            character_id=service.cards.active_character(),
        )
        self.assertIsNotNone(member)
        assert member is not None
        self.assertGreaterEqual(int(member["notes_count"]), 1)
        self.assertTrue(str(member["profile_summary"] or "").strip())

    def test_group_increase_notice_for_bot_triggers_auto_sync(self) -> None:
        config = AppConfig(bot_account_id="3518944354", member_auto_sync=True)
        service, _ = build_default_service(config)
        calls: list[str] = []

        def fake_sync(current_service, group_id: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
            calls.append(group_id)
            return {"status": "ok", "group_id": group_id, "count": 2}

        with patch.object(type(service), "sync_group_members", fake_sync):
            result = service.handle_notice_payload(
                {
                    "post_type": "notice",
                    "notice_type": "group_increase",
                    "self_id": "3518944354",
                    "group_id": "612475113",
                    "user_id": "3518944354",
                }
            )

        self.assertEqual(calls, ["612475113"])
        self.assertEqual(result["reason"], "bot_joined_group")

    def test_active_character_card_identity_wins_for_person_session(self) -> None:
        service, _ = build_default_service(AppConfig())
        service.cards.save_editor_bundle(
            "aurora",
            shared_fields={
                "assistant_name": "Aurora",
                "user_name": "Captain",
                "language": "zh",
            },
            person_fields={
                "profile": ["calm"],
                "skills": ["keeps continuity"],
                "background": ["private chat"],
                "rules": ["stay concise"],
                "prologue": ["hello"],
            },
            group_fields={
                "profile": ["quick"],
                "skills": [],
                "background": ["group chat"],
                "rules": ["stay concise"],
                "prologue": ["hello"],
            },
        )
        service.cards.set_active_character("aurora")
        session = service.memory.load("783190298", "person", character_id="aurora")
        session.metadata["card"] = {
            "assistant_name": "neko",
            "user_name": "LegacyUser",
        }
        service.memory.store.save(session)
        event = InboundEvent(
            launcher_id="783190298",
            launcher_type="person",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="hello there")],
        )

        address = service._resolve_address(event, session)

        self.assertEqual(address, "Captain")

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

    def test_save_character_panel_switch_resets_character_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(data_root=tmpdir, character="default")
            service, _ = build_file_service(config)
            aurora_sessions = FileMemoryStore(Path(tmpdir) / "sessions" / "aurora", scoped_character_id="aurora")
            polluted = aurora_sessions.load("612475113", "group")
            polluted.history = [
                "tester: stale question",
                "assistant: stale answer",
            ]
            aurora_sessions.save(polluted)

            service.save_character_panel(
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

            switched = service.memory.load("612475113", "group", character_id="aurora")

            self.assertEqual(switched.character_id, "aurora")
            self.assertEqual(switched.history, [])

    def test_save_character_panel_switches_to_character_scoped_runtime_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(data_root=tmpdir, character="default")
            service, _ = build_file_service(config)
            service.state_store.save_member(
                {
                    "group_id": "",
                    "user_id": "783190298",
                    "qq_nickname": "tester",
                    "preferred_name": "default-name",
                    "onboarding_status": "ready",
                }
            )

            service.save_character_panel(
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

            self.assertEqual(
                service.state_store.path,
                Path(tmpdir) / "state" / "characters" / "aurora" / "runtime.sqlite3",
            )
            self.assertIsNone(service.state_store.get_member(group_id="", user_id="783190298"))

            default_store = SqliteRuntimeStateStore(
                Path(tmpdir) / "state" / "characters" / "default" / "runtime.sqlite3"
            )
            persisted = default_store.get_member(group_id="", user_id="783190298")
            default_store.close()

            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted["preferred_name"], "default-name")

    def test_get_behavior_events_filters_by_character(self) -> None:
        service, _ = build_default_service(AppConfig(group_reply_requires_mention=False))
        event = InboundEvent(
            launcher_id="612475113",
            launcher_type="group",
            sender_id="783190298",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="hello there")],
        )

        service._record_behavior_event(
            service.event_engine.capture_inbound(event, text="default hello", character_id="default")
        )
        service._record_behavior_event(
            service.event_engine.capture_inbound(event, text="aurora hello", character_id="aurora")
        )

        default_events = service.get_behavior_events(
            limit=10,
            launcher_type="group",
            launcher_id="612475113",
            character_id="default",
        )
        aurora_events = service.get_behavior_events(
            limit=10,
            launcher_type="group",
            launcher_id="612475113",
            character_id="aurora",
        )

        self.assertEqual(len(default_events), 1)
        self.assertEqual(default_events[0]["character_id"], "default")
        self.assertEqual(default_events[0]["summary"], "default hello")
        self.assertEqual(len(aurora_events), 1)
        self.assertEqual(aurora_events[0]["character_id"], "aurora")
        self.assertEqual(aurora_events[0]["summary"], "aurora hello")

    def test_character_switch_isolates_follow_up_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                character="default",
                bot_account_id="3518944354",
                group_follow_up_window_seconds=30.0,
            )
            service, _ = build_file_service(config)
            event = InboundEvent(
                launcher_id="612475113",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(kind="mention", mention_target="3518944354"),
                    MessageSegment(kind="text", text=" hello"),
                ],
            )

            service.gate.refresh_follow_up_window(event)
            self.assertTrue(service.gate.is_follow_up_window_active("612475113"))

            service.activate_character("aurora", reset_sessions=False)
            self.assertFalse(service.gate.is_follow_up_window_active("612475113"))

            service.activate_character("default", reset_sessions=False)
            self.assertTrue(service.gate.is_follow_up_window_active("612475113"))

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
        current_character = self.service._active_character_id()
        self.service.state_store.save_member(
            {
                "group_id": "",
                "user_id": "783190298",
                "preferred_name": "luna",
                "onboarding_status": "ready",
            }
        )
        self.service.state_store.save_assistant_alias(
            character_id=current_character,
            user_id="783190298",
            assistant_alias="阿璃",
        )
        self.service.memory.save_user_message("783190298", "person", "tester", "hello")

        detail = self.service.get_session_detail("person", "783190298")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["preferred_name"], "luna")
        self.assertEqual(detail["assistant_alias"], "阿璃")

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

        self.service.migrator.migrate_session_state()

        migrated_person = self.service.memory.load("783190298", "person")
        migrated_group = self.service.memory.load("612475113", "group")
        person_member = self.service.state_store.get_member(group_id="", user_id="783190298")
        group_member = self.service.state_store.get_member(
            group_id="612475113",
            user_id="783190298",
            character_id=self.service.cards.active_character(),
        )
        knowledge_entries = self.service.state_store.list_knowledge(
            limit=8,
            character_id=self.service.cards.active_character(),
        )

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

    def test_dashboard_snapshot_exposes_story_mode(self) -> None:
        service, _ = build_default_service(AppConfig(story_mode=True))

        snapshot = service.dashboard_snapshot()

        self.assertTrue(snapshot["story_mode"])
        self.assertTrue(snapshot["narrator_mode"])

    def test_story_mode_fallback_reply_uses_visible_story_format(self) -> None:
        service, outbound = build_default_service(
            AppConfig(
                story_mode=True,
                story_style="cinematic",
                story_detail_level=3,
                group_reply_requires_mention=False,
            )
        )

        result = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="我今天好累")],
            )
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("(", result.text)
        self.assertIn(")", result.text)
        self.assertIn("`", result.text)
        self.assertEqual(len(outbound.sent), 1)

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
