from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.llm_clients import LLMToolResponse
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.tavern.soup import TurtleSoup, _verdict

BOT_ID = "3956638110"
GROUP_ID = "612475113"
HOST = "783190298"
PLAYER = "1147026356"
NAMES = {HOST: "资本狗", PLAYER: "苦力怕"}
RIDDLES = [
    {
        "id": "hiccup", "title": "一杯水", "level": "简单",
        "surface": "男人要了一杯水，服务员却用玩具枪指着他，他道谢走了。",
        "truth": "男人在打嗝，服务员吓了他一跳治好了。",
        "keys": ["男人在打嗝", "被吓一跳治好了"],
        "hints": ["男人有个小毛病。", "有个土办法能治。"],
    },
    {
        "id": "triplets", "title": "不是双胞胎",
        "surface": "两个男孩同一天同一个妈妈生的，却不是双胞胎。",
        "truth": "他们是三胞胎里的两个。",
        "keys": ["三胞胎"],
        "hints": ["不止两个孩子。"],
    },
]


def _group(text: str, *, sender: str = HOST, mention: bool = False) -> InboundEvent:
    segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
    segments.append(MessageSegment(kind="text", text=text))
    return InboundEvent(GROUP_ID, "group", sender, NAMES[sender], segments)


def _private(text: str) -> InboundEvent:
    return InboundEvent(HOST, "person", HOST, NAMES[HOST], [MessageSegment(kind="text", text=text)])


class _Model:
    """Answers the judge from ``verdicts`` (in order) and the character with a plain line."""

    enabled = True
    supports_tool_calling = True
    supports_images = False

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.verdicts: list[str] = []

    def _answer(self, messages):  # type: ignore[no-untyped-def]
        self.requests.append([dict(message) for message in messages])
        judging = str(messages[0]["content"]).startswith("你是 QQ")
        text = self.verdicts.pop(0) if judging and self.verdicts else "嗯嗯。"
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

    def judge_requests(self) -> list[list[dict[str, object]]]:
        return [request for request in self.requests if str(request[0]["content"]).startswith("你是 QQ")]


class TurtleSoupTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        folder = self.root / "turtle_soup"
        folder.mkdir()
        (folder / "mine.json").write_text(json.dumps(RIDDLES, ensure_ascii=False), encoding="utf-8")
        self.config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False)
        self.config.llm.enabled = True
        self.config.listen.enabled = False
        self.model = _Model()
        self.service, self.outbound = build_default_service(self.config)
        self.addCleanup(self.service.close)
        self.service.generator._dify_client = self.model  # type: ignore[assignment]
        self.service.soup.builtin = self.root / "none.json"  # only this test's riddles

    def _say(self, event: InboundEvent, *verdicts: str) -> str:
        self.model.verdicts.extend(verdicts)
        reply = self.service.handle_event(event)
        return reply.text if reply is not None else ""

    def _serve(self) -> str:
        """Serve in the group, and say which riddle came."""
        text = self._say(_group("来碗海龟汤", mention=True))
        self.assertTrue(text.startswith("🐢 海龟汤《"), text)
        return "一杯水" if "《一杯水》" in text else "不是双胞胎"

    def test_a_bowl_is_served_and_questions_are_judged_against_the_truth(self) -> None:
        # In a group it takes an @ (or her name), like the tavern.
        self.assertNotIn("🐢", self._say(_group("海龟汤")))
        self.service.soup._bowls.clear()
        self.service.soup.builtin = self.root / "none.json"
        (self.root / "turtle_soup" / "mine.json").write_text(json.dumps(RIDDLES[:1], ensure_ascii=False), encoding="utf-8")

        text = self._say(_group("来碗海龟汤", mention=True))
        self.assertEqual(
            text,
            "🐢 海龟汤《一杯水》（简单）\n【汤面】男人要了一杯水，服务员却用玩具枪指着他，他道谢走了。\n\n"
            "问我能用「是」或「不是」回答的问题，猜出真相就算赢。说「提示」要提示，「揭晓」看汤底，「换一碗」换题；"
            "括号（…）里的话我不听。",
        )
        self.assertEqual(self._say(_group("他是不是生病了？"), '{"answer": "不是", "solved": false}'), "资本狗：不是。")
        system = str(self.model.judge_requests()[-1][0]["content"])
        self.assertIn("【汤底】男人在打嗝，服务员吓了他一跳治好了。", system)
        self.assertIn("1. 男人在打嗝\n2. 被吓一跳治好了", system)
        self.assertEqual(self.model.judge_requests()[-1][-1]["content"], "他是不是生病了？")

        self.assertEqual(self._say(_group("为什么拿枪", sender=PLAYER), '{"answer": "请问是非题"}'),
                         "苦力怕：这个得换成能用「是」或「不是」回答的问题哦。")

        # Talk that neither asks nor guesses, and table talk, stay with the chat.
        judged = len(self.model.judge_requests())
        self._say(_group("今天好热"))
        self._say(_group("（我去吃个饭？）"))
        self.assertEqual(len(self.model.judge_requests()), judged)

        self.assertEqual(self._say(_group("提示")), "提示 1/2：男人有个小毛病。")
        self.assertEqual(self._say(_group("给个提示")), "提示 2/2：有个土办法能治。")
        self.assertEqual(self._say(_group("提示")), "提示用完了。实在想不出来，可以说「揭晓」看汤底。")
        self.assertEqual(self._say(_group("汤面")), "【汤面】男人要了一杯水，服务员却用玩具枪指着他，他道谢走了。")

        solved = self._say(_group("我猜他在打嗝，被吓一下就好了", sender=PLAYER), '{"answer": "是", "solved": true}')
        self.assertEqual(
            solved,
            "🎉 苦力怕猜中了！（一共问了 3 个问题）\n【汤底】男人在打嗝，服务员吓了他一跳治好了。\n想再来一碗就说「海龟汤」。",
        )
        self.assertFalse(self.service.soup.running("group", GROUP_ID))

    def test_a_chat_gets_riddles_it_has_not_had_and_can_switch_or_reveal(self) -> None:
        first = self._serve()
        switched = self._say(_group("换一碗"))
        self.assertTrue(switched.startswith("上一碗的汤底："), switched)
        second = "一杯水" if "《一杯水》" in switched else "不是双胞胎"
        self.assertNotEqual(first, second)

        revealed = self._say(_group("揭晓"))
        self.assertTrue(revealed.startswith("【汤底】"), revealed)
        self.assertTrue(revealed.endswith("这碗汤一共问了 0 个问题。想再来一碗就说「海龟汤」。"))
        played = json.loads((self.root / "turtle_soup" / "played.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(played[f"group:{GROUP_ID}"]), ["hiccup", "triplets"])

        # Every riddle had: it starts over.
        self._serve()
        self.assertEqual(len(json.loads((self.root / "turtle_soup" / "played.json").read_text(encoding="utf-8"))[f"group:{GROUP_ID}"]), 1)

    def test_in_private_every_line_is_a_question_and_a_muddled_judge_asks_again(self) -> None:
        self.assertTrue(self._say(_private("海龟汤")).startswith("🐢"))
        self.assertEqual(self._say(_private("他很高"), '{"answer": "不重要", "solved": false}'), "不重要。")
        self.assertEqual(self._say(_private("跟天气有关"), "我觉得是吧"), "主持人没听清，再问一次？")
        bowl = self.service.soup._bowls[("person", HOST)]
        self.assertEqual(bowl.questions, 1)  # the muddled one is not counted
        self.assertEqual(self._say(_private("跟天气有关"), "不是。"), "不是。")

    def test_an_untouched_bowl_is_put_away_quietly(self) -> None:
        self.config.turtle_soup.idle_timeout_seconds = 0.05
        self._serve()
        time.sleep(0.1)
        judged = len(self.model.judge_requests())
        self.assertFalse(self._say(_group("他是不是在打嗝？")).startswith("资本狗："))
        self.assertEqual(len(self.model.judge_requests()), judged)
        self.assertFalse(self.service.soup.running("group", GROUP_ID))

    def test_the_tavern_and_a_bowl_do_not_share_a_chat(self) -> None:
        self._serve()
        self.assertEqual(self._say(_group("启动酒馆", mention=True)), "这碗海龟汤还没喝完，说「揭晓」看了汤底再开酒馆。")
        self._say(_group("揭晓"))

        self.assertIn("酒馆开张啦", self._say(_group("启动酒馆", mention=True)))
        self.assertEqual(self._say(_group("来碗海龟汤", mention=True)), "酒馆还开着呢，这一局结束了再来喝海龟汤。")

    def test_the_bundled_riddles_are_complete(self) -> None:
        soup = TurtleSoup(self.service)
        soup.root = self.root / "nowhere"  # the bundled ones only
        riddles = soup.riddles()
        self.assertGreaterEqual(len(riddles), 30)
        self.assertEqual(len({riddle.riddle_id for riddle in riddles}), len(riddles))
        for riddle in riddles:
            self.assertTrue(riddle.keys and riddle.hints and riddle.level, riddle.riddle_id)

    def test_the_verdict_is_read_from_the_judges_line(self) -> None:
        self.assertEqual(_verdict('好的：{"answer": "是也不是", "solved": false}'), ("是也不是", False))
        self.assertEqual(_verdict('{"answer": "是", "solved": true}'), ("是", True))
        self.assertEqual(_verdict('{"answer": "也许"}'), (None, False))
        self.assertEqual(_verdict("不重要。"), ("不重要", False))


if __name__ == "__main__":
    unittest.main()
