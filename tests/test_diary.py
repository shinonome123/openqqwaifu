from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.clock import resolve_timezone
from waifu_standalone.infra.llm_clients import LLMClientError, LLMToolResponse
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.reply_flow.tasks import ASSISTANT_SPEAKER
from waifu_standalone.runtime.service import _ASYNC_RUNTIME
from waifu_standalone.skills.tool_registry import ToolInvocation

GROUP_ID = "612475113"
FRIEND = "783190298"


class _Model:
    enabled = True
    supports_tool_calling = True
    supports_images = False

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.fail = False

    async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        if self.fail:
            raise LLMClientError("down")
        self.requests.append([dict(message) for message in messages])
        day = str(messages[-1]["content"])[:10]
        text = f"- {day}：苦力怕说影之刃零太难，大肥鱼抽卡歪了"
        return LLMToolResponse(text=text, assistant_message={"role": "assistant", "content": text})

    def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        raise AssertionError("sync path not used")

    def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return ""

    async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return ""

    def close(self) -> None:
        pass


class DiaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = AppConfig(data_root=temp.name, knowledge_auto_extract=False)
        self.config.llm.enabled = True
        self.config.listen.enabled = False
        self.model = _Model()
        self.service, self.outbound = build_default_service(self.config)
        self.addCleanup(self.service.close)
        self.service.generator._dify_client = self.model  # type: ignore[assignment]
        self.service.companion.autostart = False
        self.zone = resolve_timezone(self.config.timezone)
        self.character = self.service._active_character_id()

    def _day(self, days_ago: int, hour: int = 20) -> float:
        moment = datetime.now(self.zone).replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
        return moment.timestamp()

    def _say(self, launcher_type: str, launcher_id: str, speaker: str, text: str, at: float) -> None:
        self.service.chat_log.append(
            character_id=self.character, launcher_type=launcher_type, launcher_id=launcher_id, speaker=speaker, text=text, at=at
        )

    def _busy_day(self, days_ago: int) -> str:
        at = self._day(days_ago)
        for offset, (who, text) in enumerate(
            [("苦力怕", "影之刃零的boss太难了"), ("大肥鱼", "鸣潮抽卡又歪了"), (ASSISTANT_SPEAKER, "你们都好惨"), ("苦力怕", "明天再打")]
        ):
            self._say("group", GROUP_ID, who, text, at + offset * 60)
        return datetime.fromtimestamp(at, self.zone).strftime("%Y-%m-%d")

    def _catch_up(self, limit: int = 10) -> int:
        return _ASYNC_RUNTIME.submit(self.service.diary.catch_up(limit=limit)).result()

    def _recall(self, launcher_type: str = "group", launcher_id: str = GROUP_ID, **arguments: object) -> str:
        event = InboundEvent(launcher_id, launcher_type, FRIEND, "闵闵", [MessageSegment(kind="text", text="x")])
        session = self.service.memory.load(launcher_id, launcher_type)
        return self.service.diary.use_tool(ToolInvocation("recall", "", event, session, arguments=dict(arguments))).text

    def test_days_that_are_over_are_written_once_and_today_waits(self) -> None:
        two_days_ago = self._busy_day(2)
        yesterday = self._busy_day(1)
        self._busy_day(0)
        self.assertEqual(self._catch_up(), 2)
        self.assertEqual(self._catch_up(), 0)  # nothing new
        entries = self.service.diary.entries(character_id=self.character, launcher_type="group", launcher_id=GROUP_ID)
        self.assertEqual(sorted(entries), [two_days_ago, yesterday])
        prompt = self.model.requests[0]
        self.assertIn("影之刃零的boss太难了", str(prompt[-1]["content"]))
        self.assertIn("：你们都好惨", str(prompt[-1]["content"]))  # her own lines, by her name
        self.assertNotIn(ASSISTANT_SPEAKER, str(prompt[-1]["content"]))

    def test_a_quiet_day_is_kept_as_said_without_asking_the_model(self) -> None:
        at = self._day(1)
        self._say("person", FRIEND, "闵闵", "晚安", at)
        self.assertEqual(self._catch_up(), 1)
        self.assertEqual(self.model.requests, [])
        self.assertIn("闵闵：晚安", self._recall("person", FRIEND))

    def test_recall_reads_by_date_and_keyword_within_this_chat_only(self) -> None:
        old = self._busy_day(40)
        recent = self._busy_day(3)
        self._catch_up()
        self.assertIn(recent, self._recall())  # the last 30 days by default
        self.assertNotIn(old, self._recall())
        self.assertIn(old, self._recall(from_date=old))
        self.assertIn("影之刃零", self._recall(from_date=old, to_date=recent, keyword="影之刃零"))
        self.assertIn("没有找到", self._recall(from_date=old, to_date=recent, keyword="原神"))
        self.assertIn("没有找到", self._recall("person", "42"))
        # The diary outlives the log it was written from.
        for path in self.service.chat_log.root.glob("*/*/*.jsonl"):
            path.unlink()
        self.assertIn(old, self._recall(from_date=old))

    def test_a_model_that_fails_is_tried_again_another_day_not_every_minute(self) -> None:
        self._busy_day(1)
        self.model.fail = True
        self.assertEqual(self._catch_up(), 0)
        self.model.fail = False
        self.assertEqual(self._catch_up(), 0)  # not again today
        self.service.diary._done_on.clear()  # the next day
        self.assertEqual(self._catch_up(), 1)

    def test_recall_is_offered_whenever_someone_talks_to_her(self) -> None:
        event = InboundEvent(FRIEND, "person", FRIEND, "闵闵", [MessageSegment(kind="text", text="上个月我们聊过什么")])
        session = self.service.memory.load(FRIEND, "person")
        context = self.service.tool_orchestrator.exposure_context(
            event, session, latest_message="上个月我们聊过什么", address="闵闵", assistant_name="小葵"
        )
        self.assertIn("recall", context.allowed_tool_ids)


if __name__ == "__main__":
    unittest.main()
