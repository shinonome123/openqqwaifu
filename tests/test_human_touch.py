from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, SegmentedReplyConfig, StickerConfig
from waifu_standalone.gateways.onebot_actions import build_onebot_message_segments
from waifu_standalone.infra.llm_clients import LLMToolResponse
from waifu_standalone.members.onboarding import MemberOnboarding
from waifu_standalone.models import InboundEvent, MessageSegment, OutboundMessage
from waifu_standalone.reply_flow import emitter as emitter_module
from waifu_standalone.reply_flow import stickers as stickers_module
from waifu_standalone.reply_flow.emitter import conversational_chunks
from waifu_standalone.reply_flow.stickers import strip_sticker_tags
from waifu_standalone.settings_admin.config_manager import ConfigManager

BOT_ID = "3956638110"
GROUP_ID = "612475113"
# A 1x1 PNG; the library only reads the bytes.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _group(text: str, *, message_id: str, sender: str = "783190298", mention: bool = True) -> InboundEvent:
    segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
    segments.append(MessageSegment(kind="text", text=text))
    return InboundEvent(
        launcher_id=GROUP_ID,
        launcher_type="group",
        sender_id=sender,
        sender_name=f"user{sender}",
        segments=segments,
        message_id=message_id,
    )


def _private(text: str, *, sender: str = "783190298", message_id: str = "") -> InboundEvent:
    return InboundEvent(
        launcher_id=sender,
        launcher_type="person",
        sender_id=sender,
        sender_name="闵闵",
        segments=[MessageSegment(kind="text", text=text)],
        message_id=message_id,
    )


class _ChatClient:
    """Answers from a script; ``during_reply`` runs while the "model" is thinking."""

    enabled = True
    supports_tool_calling = True
    supports_images = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["好呀"]
        self.requests: list[list[dict[str, object]]] = []
        self.during_reply = None

    def _answer(self, messages):  # type: ignore[no-untyped-def]
        self.requests.append([dict(message) for message in messages])
        if self.during_reply is not None:
            self.during_reply()
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return LLMToolResponse(text=text, assistant_message={"role": "assistant", "content": text})

    def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        return self._answer(messages)

    async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        return self._answer(messages)

    def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return "{}"

    async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return "{}"

    def close(self) -> None:
        pass


class _ServiceCase(unittest.TestCase):
    def _service(self, **overrides: object):  # type: ignore[no-untyped-def]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_root = Path(temp.name)
        config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False, **overrides)
        config.llm.enabled = True
        service, outbound = build_default_service(config)
        self.addCleanup(service.close)
        return service, outbound

    def _chat(self, service, *replies: str) -> _ChatClient:  # type: ignore[no-untyped-def]
        # The one-time "what should I call you" question is not what these tests look at.
        async def keep_reply(_self, *args, reply_text: str, **kwargs):  # type: ignore[no-untyped-def]
            return reply_text

        soft_ask = patch.object(MemberOnboarding, "amaybe_append_soft_ask", keep_reply)
        soft_ask.start()
        self.addCleanup(soft_ask.stop)
        client = _ChatClient(*replies)
        service.generator._dify_client = client  # type: ignore[assignment]
        return client

    def _add_stickers(self, category: str, *names: str) -> Path:
        folder = self.data_root / "stickers" / category
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(PNG)
        return folder


class ConversationalChunkTests(unittest.TestCase):
    def chunks(self, text: str) -> list[str]:
        return conversational_chunks(text, max_parts=3, max_total_chars=150)

    def test_a_short_reply_goes_out_sentence_by_sentence(self) -> None:
        self.assertEqual(
            self.chunks("大牛真会挑时候使唤人。苦力怕为啥谈不了恋爱？靠太近就炸，手都牵不成。"),
            ["大牛真会挑时候使唤人。", "苦力怕为啥谈不了恋爱？", "靠太近就炸，手都牵不成。"],
        )

    def test_tiny_pieces_join_their_neighbour_and_keep_the_line_break(self) -> None:
        self.assertEqual(self.chunks("在。\n想换个称呼的话直接说。"), ["在。想换个称呼的话直接说。"])
        self.assertEqual(self.chunks("哈哈哈\n你也太逗了"), ["哈哈哈\n你也太逗了"])
        self.assertEqual(self.chunks("好呀~今天去哪玩？"), ["好呀~今天去哪玩？"])

    def test_a_quoted_sentence_stays_with_its_closing_mark(self) -> None:
        self.assertEqual(
            self.chunks("她说：“走吧。”然后就走了。我愣在原地。还有一句。"),
            ["她说：“走吧。”", "然后就走了。", "我愣在原地。还有一句。"],
        )

    def test_long_replies_lists_and_code_stay_whole(self) -> None:
        long_reply = "这是一句话。" * 30
        listing = "推荐三部：\n1. 千与千寻\n2. 你的名字\n3. 铃芽之旅"
        code = "试试这个。\n```\nprint(1)\n```"
        for text in (long_reply, listing, code):
            self.assertEqual(self.chunks(text), [text])
        self.assertEqual(conversational_chunks("在吗？在的。", max_parts=1, max_total_chars=150), ["在吗？在的。"])


class MessagePartTests(_ServiceCase):
    def test_only_a_model_chat_reply_is_split(self) -> None:
        service, _ = self._service()
        text = "今天好冷啊。你多穿点衣服！"
        canned = OutboundMessage(launcher_id=GROUP_ID, launcher_type="group", text=text)
        chat = OutboundMessage(launcher_id=GROUP_ID, launcher_type="group", text=text, conversational=True)

        self.assertEqual([part.text for part in service.emitter.message_parts(canned)], [text])
        self.assertEqual(
            [part.text for part in service.emitter.message_parts(chat)],
            ["今天好冷啊。", "你多穿点衣服！"],
        )

    def test_quote_on_the_first_part_images_on_the_last_and_the_sticker_on_its_own(self) -> None:
        service, _ = self._service()
        message = OutboundMessage(
            launcher_id=GROUP_ID,
            launcher_type="group",
            text="画好啦。你看看喜不喜欢！\n[表情:开心]",
            images=["https://example.com/cat.png"],
            reply_to="101",
            sticker="base64://c3RpY2tlcg==",
            conversational=True,
        )

        parts = service.emitter.message_parts(message)

        self.assertEqual(
            [(part.text, part.images, part.reply_to, part.sticker) for part in parts],
            [
                ("画好啦。", [], "101", ""),
                ("你看看喜不喜欢！", ["https://example.com/cat.png"], "", ""),
                ("", [], "", "base64://c3RpY2tlcg=="),
            ],
        )

    def test_a_sticker_alone_carries_the_quote_and_tags_are_never_sent(self) -> None:
        service, _ = self._service()
        only_sticker = OutboundMessage(
            launcher_id=GROUP_ID, launcher_type="group", text="[表情:开心]", reply_to="7", sticker="base64://eA=="
        )
        stray_tag = OutboundMessage(launcher_id=GROUP_ID, launcher_type="group", text="好的[表情：没这个]")

        self.assertEqual(
            [(part.text, part.reply_to, part.sticker) for part in service.emitter.message_parts(only_sticker)],
            [("", "7", "base64://eA==")],
        )
        self.assertEqual([part.text for part in service.emitter.message_parts(stray_tag)], ["好的"])

    def test_segmenting_can_be_switched_off(self) -> None:
        service, _ = self._service(segmented_reply=SegmentedReplyConfig(enabled=False))
        message = OutboundMessage(launcher_id="1", launcher_type="person", text="一句。两句！", conversational=True)

        self.assertEqual([part.text for part in service.emitter.message_parts(message)], ["一句。两句！"])

    def test_parts_pause_only_when_really_sending(self) -> None:
        service, _ = self._service(segmented_reply=SegmentedReplyConfig(min_delay_seconds=1.0, max_delay_seconds=2.5))
        self.assertEqual(service.emitter._pause_between_parts(), 0.0)  # captured replies do not wait

        service.outbound = object()  # stands in for a live sidecar
        with patch.object(emitter_module.random, "uniform", return_value=1.7) as uniform:
            self.assertEqual(service.emitter._pause_between_parts(), 1.7)
        uniform.assert_called_once_with(1.0, 2.5)


class QuoteTests(_ServiceCase):
    def test_a_group_reply_quotes_the_message_when_others_spoke_since(self) -> None:
        service, outbound = self._service()
        client = self._chat(service, "确实有道理。我也这么觉得！")
        # Someone else speaks while the reply is being written.
        client.during_reply = lambda: service.emitter.note_inbound(
            _group("今天吃啥", message_id="102", sender="1147026356", mention=False)
        )

        service.handle_event(_group("你觉得呢", message_id="101"))

        self.assertEqual([(part.text, part.reply_to) for part in outbound.sent], [("确实有道理。", "101"), ("我也这么觉得！", "")])

    def test_a_reply_right_below_the_message_is_not_quoted(self) -> None:
        service, outbound = self._service()
        self._chat(service, "确实有道理。我也这么觉得！")

        service.handle_event(_group("你觉得呢", message_id="101"))

        self.assertEqual([part.reply_to for part in outbound.sent], ["", ""])

    def test_private_chats_and_the_switch_never_quote(self) -> None:
        for overrides, event in (
            ({}, _private("在吗", message_id="55")),
            ({"group_reply_quote": False}, _group("在吗", message_id="101")),
        ):
            service, outbound = self._service(**overrides)
            client = self._chat(service, "在的")
            client.during_reply = lambda service=service: service.emitter.note_inbound(
                _group("插一句", message_id="999", sender="1147026356", mention=False)
            )

            service.handle_event(event)

            self.assertEqual([part.reply_to for part in outbound.sent], [""])


class OneBotSegmentTests(unittest.TestCase):
    def test_quote_comes_first_and_a_sticker_is_marked_as_one(self) -> None:
        quoted = OutboundMessage(launcher_id=GROUP_ID, launcher_type="group", text="在的", reply_to="101")
        sticker = OutboundMessage(launcher_id=GROUP_ID, launcher_type="group", text="", sticker="base64://eA==")

        self.assertEqual(
            build_onebot_message_segments(quoted),
            [{"type": "reply", "data": {"id": "101"}}, {"type": "text", "data": {"text": "在的"}}],
        )
        self.assertEqual(
            build_onebot_message_segments(sticker),
            [{"type": "image", "data": {"file": "base64://eA==", "sub_type": 1, "summary": "[动画表情]"}}],
        )


class StickerLibraryTests(_ServiceCase):
    def test_categories_hold_only_usable_images(self) -> None:
        service, _ = self._service()
        happy = self._add_stickers("开心", "a.png", "b.gif")
        (happy / "notes.txt").write_text("not a sticker", encoding="utf-8")
        (happy / "empty.png").write_bytes(b"")
        self._add_stickers("委屈")  # no images: not offered
        self._add_stickers("生气", "big.png")

        with patch.object(stickers_module, "_MAX_STICKER_BYTES", len(PNG)):
            (self.data_root / "stickers" / "生气" / "big.png").write_bytes(PNG + b"0")
            categories = service.stickers.categories()

        self.assertEqual({name: [path.name for path in files] for name, files in categories.items()}, {"开心": ["a.png", "b.gif"]})

    def test_the_prompt_offers_the_categories_only_when_a_sticker_may_be_sent(self) -> None:
        service, _ = self._service()
        event = _private("在吗")
        self.assertEqual(service.stickers.prompt_section(event), "")  # nothing in the library yet

        self._add_stickers("开心", "a.png")
        self._add_stickers("摸摸头", "b.png")
        section = service.stickers.prompt_section(event)
        self.assertIn("[表情:分类]", section)
        self.assertIn("开心、摸摸头", section)

        service.config.stickers.enabled = False
        self.assertEqual(service.stickers.prompt_section(event), "")

    def test_a_tag_becomes_a_sticker_from_that_category(self) -> None:
        service, _ = self._service()
        self._add_stickers("开心", "a.png")

        text, sticker = service.stickers.take("嘿嘿，被你发现了\n[表情:开心]", _private("在吗"))

        self.assertEqual(text, "嘿嘿，被你发现了\n[表情:开心]")  # the history keeps the tag
        self.assertEqual(sticker, "base64://" + base64.b64encode(PNG).decode("ascii"))
        self.assertEqual(strip_sticker_tags(text), "嘿嘿，被你发现了")

    def test_an_unknown_category_sends_nothing_and_drops_the_tag(self) -> None:
        service, _ = self._service()
        self._add_stickers("开心", "a.png")

        self.assertEqual(service.stickers.take("好吧[表情：无语]", _private("在吗")), ("好吧", ""))

    def test_a_near_miss_category_name_still_finds_its_stickers(self) -> None:
        service, _ = self._service()
        self._add_stickers("开心", "a.png")

        text, sticker = service.stickers.take("嘿嘿[表情:很开心]", _private("在吗"))

        self.assertEqual(text, "嘿嘿\n[表情:开心]")
        self.assertTrue(sticker)

    def test_a_chat_waits_a_few_replies_between_stickers(self) -> None:
        service, _ = self._service(stickers=StickerConfig(min_replies_between=2))
        self._add_stickers("开心", "a.png")
        here, elsewhere = _private("在吗"), _private("在吗", sender="10001")

        sent = [bool(service.stickers.take("好呀[表情:开心]", here)[1]) for _ in range(4)]
        self.assertEqual(sent, [True, False, False, True])
        # The wait is per chat, and while it lasts the prompt does not offer stickers.
        self.assertTrue(service.stickers.take("好呀[表情:开心]", elsewhere)[1])
        self.assertEqual(service.stickers.prompt_section(elsewhere), "")


class StickerReplyFlowTests(_ServiceCase):
    def test_the_model_picks_a_sticker_and_it_follows_the_text(self) -> None:
        service, outbound = self._service()
        self._add_stickers("开心", "a.png")
        client = self._chat(service, "哈哈，被你发现了。我才没有偷吃呢！\n[表情:开心]", "那就好。")
        event = _private("你是不是偷吃了")

        service.handle_event(event)

        system = str(client.requests[0][0]["content"])
        self.assertIn("[表情包]", system)
        self.assertIn("开心", system)
        self.assertEqual(
            [(part.text, bool(part.sticker)) for part in outbound.sent],
            [("哈哈，被你发现了。", False), ("我才没有偷吃呢！", False), ("", True)],
        )
        history = service.memory.load(event.launcher_id, event.launcher_type).history
        self.assertEqual(history[-1], "assistant: 哈哈，被你发现了。我才没有偷吃呢！\n[表情:开心]")

        # Right after a sticker the next reply is not offered one.
        service.handle_event(_private("真的吗"))
        self.assertNotIn("[表情包]", str(client.requests[1][0]["content"]))

    def test_a_reply_that_was_only_an_unusable_tag_sends_nothing(self) -> None:
        service, outbound = self._service()
        self._chat(service, "[表情:不存在]")

        self.assertIsNone(service.handle_event(_private("在吗")))
        self.assertEqual(outbound.sent, [])


class HumanTouchConfigTests(unittest.TestCase):
    def test_settings_load_from_the_config_file_with_safe_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "segmented_reply": {"enabled": False, "max_parts": "4", "min_delay_seconds": -1, "max_delay_seconds": "x"},
                        "stickers": {"directory": "memes", "min_replies_between": 5},
                        "group_reply_quote": False,
                    }
                ),
                encoding="utf-8",
            )
            config = ConfigManager(path).load()

        self.assertEqual(
            config.segmented_reply,
            SegmentedReplyConfig(enabled=False, max_parts=4, min_delay_seconds=0.0, max_delay_seconds=2.5),
        )
        self.assertEqual(config.stickers, StickerConfig(enabled=True, directory="memes", min_replies_between=5))
        self.assertFalse(config.group_reply_quote)

    def test_defaults_turn_the_human_touches_on(self) -> None:
        config = AppConfig()
        self.assertTrue(config.segmented_reply.enabled)
        self.assertTrue(config.stickers.enabled)
        self.assertTrue(config.group_reply_quote)


if __name__ == "__main__":
    unittest.main()
