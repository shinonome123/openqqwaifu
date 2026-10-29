from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.clock import resolve_timezone
from waifu_standalone.infra.llm_clients import LLMToolResponse
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.reply_flow.companion import Companion
from waifu_standalone.runtime.service import _ASYNC_RUNTIME
from waifu_standalone.skills.tool_registry import ToolInvocation
from waifu_standalone.systems.searching import SearchContext, SearchResult

BOT_ID = "3956638110"
GROUP_ID = "612475113"
FRIEND = "783190298"
HOUR = 3600.0


def _private(text: str) -> InboundEvent:
    return InboundEvent(FRIEND, "person", FRIEND, "闵闵", [MessageSegment(kind="text", text=text)])


def _group(text: str) -> InboundEvent:
    return InboundEvent(GROUP_ID, "group", FRIEND, "闵闵", [MessageSegment(kind="text", text=text)])


class _Always:
    def random(self) -> float:
        return 0.0


class _Model:
    enabled = True
    supports_tool_calling = True
    supports_images = False

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.answer = "嗯嗯。"
        # Answers for the watch judge, in order.
        self.verdicts: list[str] = []

    def _answer(self, messages):  # type: ignore[no-untyped-def]
        self.requests.append([dict(message) for message in messages])
        judging = str(messages[0]["content"]).startswith("你在帮一个人长期盯着")
        text = (self.verdicts.pop(0) if self.verdicts else "{}") if judging else self.answer
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


class CompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False)
        self.config.llm.enabled = True
        self.config.listen.enabled = False
        self.config.companion.greeting_hours = [0, 24]
        self.model = _Model()
        self.service, self.outbound = build_default_service(self.config)
        self.addCleanup(self.service.close)
        self.service.generator._dify_client = self.model  # type: ignore[assignment]
        self.service.companion.autostart = False
        self.service.companion._rng = _Always()  # type: ignore[assignment]

    def _tick(self, now: float) -> None:
        _ASYNC_RUNTIME.submit(self.service.companion.tick(now)).result()

    def _use(self, event: InboundEvent, **arguments: object):  # type: ignore[no-untyped-def]
        session = self.service.memory.load(event.launcher_id, event.launcher_type)
        invocation = ToolInvocation("reminder", "", event, session, address="闵闵", arguments=dict(arguments))
        return self.service.companion.use_tool(invocation)

    def _watch(self, event: InboundEvent, **arguments: object):  # type: ignore[no-untyped-def]
        session = self.service.memory.load(event.launcher_id, event.launcher_type)
        invocation = ToolInvocation("watch", "", event, session, address="闵闵", arguments=dict(arguments))
        return self.service.companion.use_watch_tool(invocation)

    def _sent(self) -> list[str]:
        return [message.text for message in self.outbound.sent]

    def _at(self, hour: int) -> float:
        """Tomorrow at ``hour`` local time."""
        zone = resolve_timezone(self.config.timezone)
        today = datetime.fromtimestamp(time.time() + 86400.0, zone)
        return today.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------
    def test_the_reminder_tool_is_offered_whenever_someone_talks_to_her(self) -> None:
        event = _private("明早九点叫我起床")
        session = self.service.memory.load(FRIEND, "person")
        context = self.service.tool_orchestrator.exposure_context(
            event, session, latest_message="明早九点叫我起床", address="闵闵", assistant_name="小葵"
        )
        self.assertIn("reminder", context.allowed_tool_ids)
        names = [schema["name"] for schema in self.service.tools.model_schemas(context)]
        self.assertIn("reminder", names)

    def test_a_reminder_is_said_in_her_words_when_it_is_due_and_only_once(self) -> None:
        result = self._use(_private("半小时后提醒我收衣服"), action="add", in_minutes=30, note="提醒闵闵收衣服")
        self.assertFalse(result.error, result.text)
        self.assertIn("记下了", result.text)
        self.assertIn("提醒闵闵收衣服", self._use(_private("记着啥"), action="list").text)
        # The same thing asked again (a retried request) is kept once.
        again = self._use(_private("半小时后提醒我收衣服"), action="add", in_minutes=30, note="提醒闵闵收衣服")
        self.assertIn("已经记着了", again.text)
        self.assertEqual(len(self.service.companion.reminders()), 1)

        self._tick(time.time() + 10 * 60)
        self.assertEqual(self._sent(), [])  # not yet

        self.model.answer = "闵闵，衣服该收啦！"
        self._tick(time.time() + 31 * 60)
        self.assertEqual(self._sent(), ["闵闵，衣服该收啦！"])
        system = str(self.model.requests[-1][0]["content"])
        self.assertIn("[到点了]", system)
        self.assertIn("提醒闵闵收衣服", system)
        self.assertIn("半小时后提醒我收衣服", system)  # what they said when they asked
        self.assertEqual(self.model.requests[-1][-1]["content"], "（对方这会儿没有说话。）")
        # What she said is part of the chat; the cue is not.
        history = self.service.memory.load(FRIEND, "person").history
        self.assertTrue(any("衣服该收啦" in line for line in history))
        self.assertFalse(any("没有说话" in line for line in history))

        self._tick(time.time() + 40 * 60)
        self.assertEqual(len(self._sent()), 1)
        self.assertEqual(self.service.companion.reminders(), [])

    def test_reminders_take_a_local_time_and_refuse_nonsense(self) -> None:
        zone = resolve_timezone(self.config.timezone)
        tomorrow = datetime.fromtimestamp(time.time() + 86400.0, zone).strftime("%Y-%m-%d") + " 09:00"
        result = self._use(_private("明早九点叫我"), action="add", at=tomorrow, note="叫闵闵起床")
        self.assertIn(tomorrow, result.text)
        reminder = self.service.companion.reminders()[0]
        self.assertEqual(datetime.fromtimestamp(reminder.due_at, zone).strftime("%Y-%m-%d %H:%M"), tomorrow)

        self.assertEqual(self._use(_private("x"), action="add", at="明天", note="x").error, "bad time")
        self.assertEqual(self._use(_private("x"), action="add", at="2020-01-01 09:00", note="x").error, "past")
        self.assertEqual(self._use(_private("x"), action="add", in_minutes=5, note=" ").error, "missing note")
        # Another chat sees none of them, and cannot cancel them.
        other = InboundEvent("42", "person", "42", "路人", [MessageSegment(kind="text", text="x")])
        self.assertIn("没有记着的事", self._use(other, action="list").text)
        self.assertEqual(self._use(other, action="cancel", id=reminder.id).error, "not found")
        self.assertIn("取消了", self._use(_private("算了"), action="cancel", id=reminder.id).text)
        self.assertEqual(self.service.companion.reminders(), [])

    def test_reminders_survive_a_restart_and_long_overdue_ones_are_dropped(self) -> None:
        self._use(_private("x"), action="add", in_minutes=10, note="喝水")
        self._use(_private("x"), action="add", in_minutes=60 * 24 * 3, note="考试")
        self.service.companion.close()
        again = Companion(self.service)
        self.assertEqual(sorted(item.note for item in again.reminders()), ["喝水", "考试"])
        self.service.companion = again
        again.autostart = False
        self._tick(time.time() + 20 * HOUR)  # "喝水" is 20 hours late: dropped
        self.assertEqual(self._sent(), [])
        self.assertEqual([item.note for item in again.reminders()], ["考试"])

    def test_a_reminder_in_a_group_goes_to_the_group(self) -> None:
        self._use(_group("十分钟后提醒我开会"), action="add", in_minutes=10, note="提醒闵闵开会")
        self.model.answer = "闵闵，开会时间到了。"
        self._tick(time.time() + 11 * 60)
        self.assertEqual([(m.launcher_type, m.launcher_id, m.text) for m in self.outbound.sent],
                         [("group", GROUP_ID, "闵闵，开会时间到了。")])

    # ------------------------------------------------------------------
    # Greetings
    # ------------------------------------------------------------------
    def _befriend(self, delta: float = 0.4) -> None:
        self.service.handle_event(_private("在吗"))
        self.service.state_store.adjust_member_affinity(group_id="", user_id=FRIEND, delta=delta, character_id=self.service._active_character_id())
        self.outbound.sent.clear()

    def test_no_greetings_unless_turned_on(self) -> None:
        self._befriend()
        self._tick(time.time() + 30 * HOUR)
        self.assertEqual(self._sent(), [])

    def test_she_opens_a_quiet_private_chat_once_a_day_and_backs_off_when_ignored(self) -> None:
        self.config.companion.greetings_enabled = True
        self._befriend()
        start = time.time()
        self._tick(start + 10 * HOUR)
        self.assertEqual(self._sent(), [])  # not quiet long enough

        self.model.answer = "你那个面试后来怎么样了？"
        self._tick(start + 21 * HOUR)
        self.assertEqual(self._sent(), ["你那个面试后来怎么样了？"])
        self.assertIn("[你主动开口]", str(self.model.requests[-1][0]["content"]))
        self._tick(start + 22 * HOUR)
        self.assertEqual(len(self._sent()), 1)  # once a day

        # Unanswered: the next waits three days, and two unanswered in a row are the last.
        self._tick(start + 50 * HOUR)
        self.assertEqual(len(self._sent()), 1)
        self._tick(start + 95 * HOUR)
        self.assertEqual(len(self._sent()), 2)
        self._tick(start + 400 * HOUR)
        self.assertEqual(len(self._sent()), 2)

        # They speak: she may open again, after the quiet spell.
        self.service.handle_event(_private("回来了"))
        count = len(self._sent())
        self._tick(start + 130 * HOUR)  # the clock here ran ahead: past the last opening's day
        self.assertEqual(len(self._sent()), count + 1)

    def test_not_with_strangers_not_at_night_and_not_when_she_would_rather_not(self) -> None:
        self.config.companion.greetings_enabled = True
        self.service.handle_event(_private("在吗"))  # barely acquainted
        self.outbound.sent.clear()
        self._tick(time.time() + 30 * HOUR)
        self.assertEqual(self._sent(), [])

        self.service.state_store.adjust_member_affinity(group_id="", user_id=FRIEND, delta=0.4, character_id=self.service._active_character_id())
        self.config.companion.greeting_hours = [10, 22]
        self._tick(self._at(3))
        self.assertEqual(self._sent(), [])  # 3 a.m.

        self.config.companion.greeting_hours = [0, 24]
        self.model.answer = "[不说话]"
        self._tick(time.time() + 30 * HOUR)
        self.assertEqual(self._sent(), [])

    def test_a_quiet_group_gets_a_topic_within_its_daily_limit(self) -> None:
        self.config.companion.greetings_enabled = True
        self.config.companion.group_daily_limit = 1
        self.service.handle_event(_group("今天好热"))
        start = time.time()
        self._tick(start + 2 * HOUR)
        self.assertEqual(self._sent(), [])
        self.model.answer = "你们最近有在玩什么新游戏吗？"
        self._tick(start + 4 * HOUR)
        self.assertEqual(self._sent(), ["你们最近有在玩什么新游戏吗？"])
        self.service.handle_event(_group("在玩影之刃零"))  # answered
        self._tick(time.time() + 4 * HOUR)
        self.assertEqual(len(self._sent()), 1)  # one a day here

        self.config.companion.greet_groups = False
        self._tick(time.time() + 30 * HOUR)
        self.assertEqual(len(self._sent()), 1)

    def test_greeting_state_survives_a_restart(self) -> None:
        self.config.companion.greetings_enabled = True
        self._befriend()
        self._tick(time.time() + 21 * HOUR)
        self.assertEqual(len(self._sent()), 1)
        self.service.companion.close()
        again = Companion(self.service, rng=_Always())  # type: ignore[arg-type]
        again.autostart = False
        self.service.companion = again
        self._tick(time.time() + 22 * HOUR)
        self.assertEqual(len(self._sent()), 1)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def test_drawing_is_offered_in_private_but_in_groups_only_when_asked(self) -> None:
        def offered(event: InboundEvent, text: str, *, spontaneous: bool = False) -> set[str]:
            session = self.service.memory.load(event.launcher_id, event.launcher_type)
            return self.service.tool_orchestrator.exposure_context(
                event, session, latest_message=text, address="闵闵", assistant_name="小葵", spontaneous=spontaneous
            ).allowed_tool_ids

        self.assertIn("image", offered(_private("这场景要是能看看就好了"), "这场景要是能看看就好了"))
        self.assertNotIn("image", offered(_group("这场景要是能看看就好了"), "这场景要是能看看就好了"))
        self.assertIn("image", offered(_group("帮我画张猫"), "帮我画张猫"))
        self.assertNotIn("image", offered(_private("今天好热"), "今天好热", spontaneous=True))

    # ------------------------------------------------------------------
    # Watches
    # ------------------------------------------------------------------
    def _searching(self, *snippets: str) -> list[str]:
        queries: list[str] = []
        answers = list(snippets)

        async def search(query: str, *, reason: str = "manual") -> SearchContext:
            queries.append(query)
            snippet = answers.pop(0) if answers else "没有新消息"
            return SearchContext(query=query, results=[SearchResult(title="新闻", url="https://example.com/a", snippet=snippet)])

        self.service.search.asearch_query = search  # type: ignore[method-assign]
        return queries

    def test_a_watch_learns_first_then_tells_only_news(self) -> None:
        queries = self._searching("预计明年发售", "还是预计明年", "官宣 2027 年 3 月 1 日发售")
        result = self._watch(_private("帮我盯着影之刃零的发售日"), action="add", topic="影之刃零的发售日", query="影之刃零 发售日")
        self.assertIn("开始盯了", result.text)
        self.assertIn("已经在盯了", self._watch(_private("x"), action="add", topic="影之刃零的发售日", query="影之刃零 发售日").text)
        self.assertEqual(len(self.service.companion.watches()), 1)
        start = time.time()

        # The first search only learns what is out there, even if the judge calls it news.
        self.model.verdicts.append('{"news": true, "what": "预计明年发售", "known": "预计明年发售"}')
        self._tick(start + 6 * 60)
        self.assertEqual(self._sent(), [])
        self.assertEqual(self.service.companion.watches()[0].known, "预计明年发售")

        self.model.verdicts.append('{"news": false, "what": "", "known": "预计明年发售"}')
        self._tick(start + 13 * HOUR)
        self.assertEqual(self._sent(), [])

        self.model.verdicts.append('{"news": true, "what": "官宣 2027 年 3 月 1 日发售", "source": "https://example.com/a", "known": "2027-03-01 发售"}')
        self.model.answer = "影之刃零定档了，2027 年 3 月 1 日！"
        self._tick(start + 26 * HOUR)
        self.assertEqual(self._sent(), ["影之刃零定档了，2027 年 3 月 1 日！"])
        system = str(self.model.requests[-1][0]["content"])
        self.assertIn("[盯着的事有消息了]", system)
        self.assertIn("https://example.com/a", system)
        judge = [r for r in self.model.requests if str(r[0]["content"]).startswith("你在帮一个人长期盯着")][-1]
        self.assertIn("之前知道的情况：预计明年发售", str(judge[-1]["content"]))
        self.assertEqual(queries, ["影之刃零 发售日"] * 3)

        # Checked no sooner than every_hours, and not at all at night.
        self._tick(start + 27 * HOUR)
        self.assertEqual(len(queries), 3)
        self.config.companion.greeting_hours = [10, 22]
        self.service.companion.watches()[0].next_at = 0.0
        self._tick(self._at(3))
        self.assertEqual(len(queries), 3)

    def test_watches_are_per_chat_bounded_and_end(self) -> None:
        self._searching()
        result = self._watch(_private("盯着 RTX 6090 价格"), action="add", topic="RTX 6090 价格", every_hours=1, days=999)
        watch = self.service.companion.watches()[0]
        self.assertEqual(watch.every_seconds, 6 * HOUR)  # at least watch_min_hours
        self.assertLessEqual(watch.until - time.time(), 60 * 86400.0 + 5)
        self.assertIn(watch.id, self._watch(_private("在盯啥"), action="list").text)
        other = InboundEvent("42", "person", "42", "路人", [MessageSegment(kind="text", text="x")])
        self.assertEqual(self._watch(other, action="cancel", id=watch.id).error, "not found")
        self.assertEqual(self._watch(_private("x"), action="add", topic=" ").error, "missing topic")

        self.service.companion.close()
        again = Companion(self.service)
        again.autostart = False
        self.service.companion = again
        self.assertEqual([item.topic for item in again.watches()], ["RTX 6090 价格"])
        self._tick(watch.until + 1)
        self.assertEqual(again.watches(), [])


if __name__ == "__main__":
    unittest.main()
