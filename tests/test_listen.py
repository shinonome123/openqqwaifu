from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.llm_clients import LLMToolResponse
from waifu_standalone.members.onboarding import MemberOnboarding
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.reply_flow.listener import SILENCE, is_silence
from waifu_standalone.runtime.service import _ASYNC_RUNTIME

BOT_ID = "3956638110"
GROUP_ID = "612475113"
HOST = "783190298"
FRIEND = "1147026356"
OTHER_BOT = "3246300953"
NAMES = {HOST: "小夏", FRIEND: "苦力怕", OTHER_BOT: "大肥鱼"}


def _png() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, "PNG")
    return "base64://" + base64.b64encode(buffer.getvalue()).decode("ascii")


class _Model:
    """A chat model that records each request; ``reply`` decides the answer."""

    enabled = True
    supports_tool_calling = True
    supports_images = True

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.reply = lambda messages: "好呀"

    def _answer(self, messages):  # type: ignore[no-untyped-def]
        self.requests.append([dict(message) for message in messages])
        text = self.reply(messages)
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


class ListenTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cards = Path(temp.name) / "cards"
        cards.mkdir()
        for variant in ("person", "group"):
            (cards / f"aurora_{variant}.yaml").write_text(
                "user_name: 主人\nassistant_name: 极光\nlanguage: 简体中文\nProfile:\n  - 你是极光，傲娇的小公主。\n",
                encoding="utf-8",
            )
        (cards / "aurora.meta.json").write_text(json.dumps({"interests": ["猫", "甜品"]}, ensure_ascii=False), encoding="utf-8")
        (cards / "active_character.json").write_text(json.dumps({"character": "aurora"}), encoding="utf-8")
        self.config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False)
        self.config.llm.enabled = True
        # Short waits keep the tests quick; the order of events is what they check.
        self.config.listen.quiet_seconds = 0.4
        self.config.listen.max_wait_seconds = 3.0
        self.config.listen.group_quiet_seconds = 0.4
        self.service, self.outbound = build_default_service(self.config)
        self.addCleanup(self.service.close)
        self.model = _Model()
        self.service.generator._dify_client = self.model  # type: ignore[assignment]
        self._ids = 0

        async def keep_reply(_self, *args, reply_text: str, **kwargs):  # type: ignore[no-untyped-def]
            return reply_text

        soft_ask = patch.object(MemberOnboarding, "amaybe_append_soft_ask", keep_reply)
        soft_ask.start()
        self.addCleanup(soft_ask.stop)

    def _id(self) -> str:
        self._ids += 1
        return str(self._ids)

    def _private(self, text: str = "", *, image: bool = False) -> InboundEvent:
        segments = [MessageSegment(kind="image", image_base64=_png())] if image else []
        if text:
            segments.append(MessageSegment(kind="text", text=text))
        return InboundEvent(HOST, "person", HOST, NAMES[HOST], segments, message_id=self._id())

    def _group(self, text: str, *, sender: str = HOST, mention: bool = False) -> InboundEvent:
        segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
        segments.append(MessageSegment(kind="text", text=text))
        return InboundEvent(GROUP_ID, "group", sender, NAMES[sender], segments, message_id=self._id())

    def _together(self, *events: InboundEvent) -> list[object]:
        """Send events a moment apart, as a person typing them, and wait for all of them."""
        futures = []
        for event in events:
            futures.append(_ASYNC_RUNTIME.submit(self.service.handle_event_async(event)))
            time.sleep(0.1)
        return [future.result(timeout=20) for future in futures]

    def _chat_requests(self) -> list[list[dict[str, object]]]:
        return [request for request in self.model.requests if str(request[0]["content"]).startswith("你是极光")]

    def test_lines_sent_in_a_row_get_one_reply(self) -> None:
        first, second = self._together(self._private("我跟你说"), self._private("今天面试过了！"))

        self.assertIsNone(first)
        self.assertIsNotNone(second)
        (request,) = self._chat_requests()
        self.assertEqual(request[-1], {"role": "user", "content": "我跟你说\n今天面试过了！"})
        self.assertEqual([part.text for part in self.outbound.sent], ["好呀"])

    def test_a_picture_sent_before_the_question_goes_with_it(self) -> None:
        first, second = self._together(self._private(image=True), self._private("这是什么"))

        self.assertIsNone(first)
        self.assertIsNotNone(second)
        (request,) = self._chat_requests()
        content = request[-1]["content"]
        self.assertIsInstance(content, list)
        self.assertIn("这是什么", content[0]["text"])
        self.assertEqual([part["type"] for part in content], ["text", "image_url"])

    def test_a_quick_second_line_in_a_group_goes_with_the_first(self) -> None:
        first, second = self._together(self._group("帮我看看", mention=True), self._group("这个怎么修"))

        self.assertIsNone(first)
        self.assertIsNotNone(second)
        (request,) = self._chat_requests()
        self.assertEqual(request[-1]["content"], "小夏：帮我看看\n小夏：这个怎么修")

    def test_the_person_she_answered_needs_no_at_but_others_do(self) -> None:
        self.assertIsNotNone(self.service.handle_event(self._group("在吗", mention=True)))
        self.assertIsNotNone(self.service.handle_event(self._group("今天吃什么")))
        self.assertIsNone(self.service.handle_event(self._group("我也想知道", sender=FRIEND)))
        self.service.listener.drain()

        self.assertEqual(len(self._chat_requests()), 2)  # nothing in 苦力怕's line is her business

    def test_talking_with_her_uses_the_persons_budget_not_the_groups(self) -> None:
        self.config.rate_limits.group_reply_burst = 1
        self.config.rate_limits.group_reply_per_hour = 0.0
        self.assertIsNotNone(self.service.handle_event(self._group("在吗", mention=True)))
        for text in ("今天吃什么", "要不吃火锅", "就这么定了"):
            self.assertIsNotNone(self.service.handle_event(self._group(text)), text)

    def test_calling_her_by_name_counts_as_addressing_her(self) -> None:
        meta = Path(self.config.data_root) / "cards" / "aurora.meta.json"
        meta.write_text(json.dumps({"call_names": ["奴隶"]}, ensure_ascii=False), encoding="utf-8")

        self.assertIsNotNone(self.service.handle_event(self._group("极光你觉得呢", sender=FRIEND)))
        self.assertIsNotNone(self.service.handle_event(self._group("奴隶出来干活", sender=HOST)))

    def test_she_joins_talk_about_her_interests_and_may_keep_quiet(self) -> None:
        self.model.reply = lambda messages: SILENCE
        self.assertIsNone(self.service.handle_event(self._group("我家的猫今天把杯子打碎了", sender=FRIEND)))
        self.service.listener.drain()

        (asked,) = self._chat_requests()
        self.assertIn("[这次是你自己想插话]", str(asked[0]["content"]))
        self.assertEqual(asked[-1]["content"], "苦力怕：我家的猫今天把杯子打碎了")
        self.assertEqual(self.outbound.sent, [])  # she chose to say nothing

        self.model.reply = lambda messages: "笨猫！人没受伤吧？"
        self.service.handle_event(self._group("猫还躲到床底下不出来", sender=FRIEND))
        self.service.listener.drain()
        self.assertEqual([part.text for part in self.outbound.sent], ["笨猫！人没受伤吧？"])
        history = self.service.memory.load(GROUP_ID, "group").history
        self.assertEqual(history[-3:], ["苦力怕: 我家的猫今天把杯子打碎了", "苦力怕: 猫还躲到床底下不出来", "assistant: 笨猫！人没受伤吧？"])

        # Having just spoken, she does not join in again so soon; 苦力怕 is now talking to her, though.
        self.service.handle_event(self._group("我家猫最喜欢吃鱼了", sender=HOST))
        self.service.listener.drain()
        self.assertEqual(len(self._chat_requests()), 2)
        self.assertIsNotNone(self.service.handle_event(self._group("它没事", sender=FRIEND)))

    def test_a_card_can_be_chattier_than_the_defaults(self) -> None:
        meta = Path(self.config.data_root) / "cards" / "aurora.meta.json"
        meta.write_text(
            json.dumps({"interests": ["猫"], "chime_in": {"min_gap_seconds": 0, "burst": 5}}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.model.reply = lambda messages: "笨猫！"
        for sender, text in ((FRIEND, "我家的猫今天把杯子打碎了"), (HOST, "我家猫也天天捣乱")):
            self.service.handle_event(self._group(text, sender=sender))
            self.service.listener.drain()

        self.assertEqual([part.text for part in self.outbound.sent], ["笨猫！", "笨猫！"])
        self.assertEqual(self.service.listener.limits().burst, 5)
        self.assertEqual(self.service.listener.limits().threshold, self.config.listen.chime_in_threshold)

    def test_commands_bots_and_bare_laughter_never_make_her_join_in(self) -> None:
        self.config.ignore_user_ids = [OTHER_BOT]
        cases = (
            self._group("我家的猫今天又闯祸了", sender=OTHER_BOT),
            self._group("/skill weather 猫城", sender=FRIEND),
            self._group("生图 一只猫", sender=FRIEND),
            self._group("猫猫猫猫！！", sender=FRIEND),  # her interest, but nothing to answer
        )
        # Each in a group of its own: the talk a check weighs is everything since she last spoke there.
        for number, event in enumerate(cases):
            event.launcher_id = f"{GROUP_ID}{number}"
            self.assertIsNone(self.service.handle_event(event))
            self.service.listener.drain()
        self.assertEqual(self._chat_requests(), [])

    def test_with_listen_off_every_line_is_answered(self) -> None:
        self.config.listen.enabled = False
        first, second = self._together(self._private("我跟你说"), self._private("今天面试过了！"))

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(self._chat_requests()), 2)

    def test_silence_is_recognised_in_its_usual_spellings(self) -> None:
        for text in ("[不说话]", "【不说话】", " （不说话） ", "不说话。", ""):
            self.assertTrue(is_silence(text), text)
        for text in ("我不说话了，你们聊", "猫没事吧"):
            self.assertFalse(is_silence(text), text)


if __name__ == "__main__":
    unittest.main()
