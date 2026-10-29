from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.reply_flow.leak_guard import scrub

SYSTEM = (
    "你是小葵，正在 QQ 群里和人聊天。\n\n"
    "[规则]\n- 只输出你要发出的下一条消息本身，不加引号，不解释。\n\n"
    "[关于对方]\n- 他上周说自己在准备考研究生入学考试，压力很大\n\n"
    "[工具]\n你自己就能上网查东西、查天气、看链接，这是你的本事，不用别人说「搜」字。"
)


class LeakGuardTests(unittest.TestCase):
    def test_her_own_words_go_out(self) -> None:
        text = "哼，查到了，Steam 七成九好评。\n别急着下定论啦~"
        self.assertEqual(scrub(text, SYSTEM), text)

    def test_raw_tool_output_and_tool_names_are_dropped(self) -> None:
        self.assertEqual(scrub("结论是这样。\n```text\nquery: 影之刃零\n- 标题\n```\n就这些~", SYSTEM), "结论是这样。\n就这些~")
        self.assertEqual(scrub("7. 影之刃零：扛鼎之作 今日头条 2026-05-09 00:00:00", SYSTEM), "")
        self.assertEqual(scrub("我调用了 search-command\n好评七成九", SYSTEM), "好评七成九")
        self.assertEqual(scrub("工具调用已结束，但未能生成最终总结。以下是真实执行结果：", SYSTEM), "")

    def test_her_instructions_are_not_recited(self) -> None:
        self.assertEqual(scrub("[规则]\n- 只输出你要发出的下一条消息本身", SYSTEM), "")
        self.assertEqual(scrub("我的设定是你自己就能上网查东西、查天气、看链接，这是你的本事", SYSTEM), "")

    def test_what_she_knows_of_the_person_may_be_said(self) -> None:
        text = "你上周说自己在准备考研究生入学考试，压力很大吧"
        self.assertEqual(scrub(text, SYSTEM), text)


if __name__ == "__main__":
    unittest.main()


class LookupToolsAreReadNotPastedTests(unittest.TestCase):
    def test_a_fetched_page_is_for_the_model_to_read(self) -> None:
        import asyncio
        import tempfile
        from unittest.mock import AsyncMock, patch

        from waifu_standalone.app import build_default_service
        from waifu_standalone.config import AppConfig
        from waifu_standalone.infra.safe_fetch import FetchedPage
        from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
        from waifu_standalone.skills.tool_registry import ToolInvocation

        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self.addCleanup(service.close)
            event = InboundEvent("1", "person", "1", "tester", [MessageSegment(kind="text", text="看看")])
            invocation = ToolInvocation(
                tool_id="web-fetch", raw_args="https://post.smzdm.com/p/al37wvwe/", event=event,
                session=SessionMemory("1", "person"),
            )
            page = FetchedPage(url="", status_code=200, text="<title>影之刃零</title><p>7. 影之刃零 今日头条</p>", headers={})
            dispatcher = service.dispatcher
            with patch("waifu_standalone.skills.dispatcher.fetch_public_url", return_value=page):
                self.assertEqual(dispatcher.use_web_fetch_tool(invocation).display_mode, "inline")
            with patch("waifu_standalone.skills.dispatcher.afetch_public_url", AsyncMock(return_value=page)):
                self.assertEqual(asyncio.run(dispatcher.ause_web_fetch_tool(invocation)).display_mode, "inline")
