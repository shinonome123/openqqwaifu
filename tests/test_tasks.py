from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.llm_clients import LLMToolCall, LLMToolResponse
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.skills.tool_registry import ToolInvocation

BOT_ID = "3956638110"
GROUP_ID = "612475113"
FRIEND = "783190298"


def _group(text: str, *, sender: str = FRIEND, name: str = "闵闵", mention: bool = False, message_id: str = "") -> InboundEvent:
    segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
    segments.append(MessageSegment(kind="text", text=text))
    return InboundEvent(GROUP_ID, "group", sender, name, segments, message_id=message_id)


def _private(text: str) -> InboundEvent:
    return InboundEvent(FRIEND, "person", FRIEND, "闵闵", [MessageSegment(kind="text", text=text)])


class _Model:
    """A chat model run by a script: ``plan(messages, tools)`` returns text or a tool call."""

    enabled = True
    supports_tool_calling = True
    supports_images = False

    def __init__(self) -> None:
        self.requests: list[tuple[list[dict[str, object]], list[str]]] = []
        self.plan = lambda messages, tools: "嗯嗯。"
        self.delay = 0.0
        self._calls = 0

    async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        names = [str(tool.get("name")) for tool in tools]
        self.requests.append(([dict(message) for message in messages], names))
        step = self.plan(messages, names)
        if isinstance(step, tuple):
            self._calls += 1
            call = LLMToolCall(f"call_{self._calls}", step[0], step[1])
            return LLMToolResponse(tool_calls=[call], assistant_message={
                "role": "assistant", "content": "", "tool_calls": [call.as_assistant_tool_call()],
            })
        return LLMToolResponse(text=step, assistant_message={"role": "assistant", "content": step})

    def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        return asyncio.run(self.ainvoke_with_tools(messages, tools=tools, user=user))

    def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return ""

    async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return ""

    def close(self) -> None:
        pass


def _tool_results(messages) -> list[str]:  # type: ignore[no-untyped-def]
    return [str(message["content"]) for message in messages if message.get("role") == "tool"]


class TaskTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False)
        self.config.llm.enabled = True
        self.config.listen.enabled = False
        self.model = _Model()
        self.service, self.outbound = build_default_service(self.config)
        self.addCleanup(self.service.close)
        self.service.generator._dify_client = self.model  # type: ignore[assignment]
        self.service.companion.autostart = False

    def _sent(self) -> list[str]:
        return [message.text for message in self.outbound.sent]

    # ------------------------------------------------------------------
    # The chat log
    # ------------------------------------------------------------------
    def test_every_line_is_logged_with_its_time_and_read_back_per_chat(self) -> None:
        self.service.handle_event(_group("我最近在玩影之刃零", sender="1147026356", name="苦力怕"))
        self.service.handle_event(_group("我在玩鸣潮", sender="3246300953", name="大肥鱼"))
        self.service.handle_event(_group("你们都在玩啥", mention=True))
        self.service.handle_event(_private("私聊里的话"))
        event = _group("x")
        session = self.service.memory.load(GROUP_ID, "group")
        result = self.service.tasks.use_chat_log_tool(
            ToolInvocation("chat-log", "", event, session, assistant_name="小葵", arguments={"days": 7})
        )
        self.assertIn("苦力怕：我最近在玩影之刃零", result.text)
        self.assertIn("大肥鱼：我在玩鸣潮", result.text)
        self.assertIn("闵闵：你们都在玩啥", result.text)
        self.assertIn("小葵（你）：嗯嗯。", result.text)  # her own lines too
        self.assertNotIn("私聊里的话", result.text)  # another chat's lines never
        filtered = self.service.tasks.use_chat_log_tool(
            ToolInvocation("chat-log", "", event, session, arguments={"keyword": "鸣潮"})
        )
        self.assertIn("鸣潮", filtered.text)
        self.assertNotIn("影之刃零", filtered.text)
        self.assertEqual(self.service.tools.model_schemas(
            self.service.tool_orchestrator.exposure_context(event, session, latest_message="昨天谁说过鸣潮", address="闵闵", assistant_name="小葵")
        ) and True, True)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------
    def test_a_job_is_handed_off_answered_at_once_and_its_result_sent_later(self) -> None:
        for sender, name, text in (("1147026356", "苦力怕", "影之刃零好难"), ("3246300953", "大肥鱼", "鸣潮新版本不错")):
            self.service.handle_event(_group(text, sender=sender, name=name))

        def plan(messages, tools):  # type: ignore[no-untyped-def]
            system = str(messages[0]["content"])
            results = _tool_results(messages)
            if "[后台任务]" in system:
                self.assertNotIn("start-task", tools)  # a task does not start tasks
                if not results:
                    return ("chat-log", {"days": 7})
                return "弄好啦！这周聊到的游戏：\n1. 影之刃零（苦力怕说难）\n2. 鸣潮（大肥鱼说新版本不错）"
            if not results:
                self.assertIn("start-task", tools)
                return ("start-task", {"action": "start", "goal": "整理这周群里聊过的游戏"})
            return "行，我翻翻记录整理一下~"

        self.model.plan = plan
        reply = self.service.handle_event(_group("把这周群里聊过的游戏整理一下", mention=True, message_id="m42"))
        self.assertTrue(reply.text.startswith("行，我翻翻记录整理一下~"), reply.text)
        self.service.tasks.drain()
        result = self.outbound.sent[-1]
        self.assertTrue(result.text.startswith("弄好啦！这周聊到的游戏"))
        self.assertEqual(result.reply_to, "m42")  # quotes what was asked, long after
        # The run read the log, and its answer is part of the chat now.
        task_request = next(messages for messages, _ in self.model.requests if "[后台任务]" in str(messages[0]["content"]))
        self.assertIn("整理这周群里聊过的游戏", str(task_request[0]["content"]))
        last = self.model.requests[-1][0]
        self.assertIn("影之刃零好难", "".join(_tool_results(last)))
        history = self.service.memory.load(GROUP_ID, "group").history
        self.assertTrue(any("弄好啦" in line for line in history))

    def test_one_task_per_chat_and_a_stuck_one_says_so(self) -> None:
        self.config.tasks.timeout_seconds = 30.0
        event = _private("做个表")
        session = self.service.memory.load(FRIEND, "person")

        async def start_two():  # type: ignore[no-untyped-def]
            self.model.delay = 0.2
            invocation = ToolInvocation("start-task", "", event, session, address="闵闵", arguments={"action": "start", "goal": "做个表"})
            first = self.service.tasks.use_tool(invocation)
            second = self.service.tasks.use_tool(invocation)
            status = self.service.tasks.use_tool(ToolInvocation("start-task", "", event, session, arguments={"action": "status"}))
            return first, second, status

        from waifu_standalone.runtime.service import _ASYNC_RUNTIME
        first, second, status = _ASYNC_RUNTIME.submit(start_two()).result()
        self.assertIn("交给后台", first.text)
        self.assertEqual(second.error, "busy")
        self.assertIn("正在做：做个表", status.text)
        self.service.tasks.drain()
        self.assertEqual(self._sent(), ["嗯嗯。"])

        # A run with nothing to show says it got stuck rather than going quiet.
        self.model.plan = lambda messages, tools: ""
        self.model.delay = 0.0
        _ASYNC_RUNTIME.submit(start_two()).result()
        self.service.tasks.drain()
        self.assertEqual(self._sent()[-1], "刚才那件事没做完，中间卡住了，等会儿再让我试一次吧。")

    def test_tasks_get_more_rounds_than_a_reply(self) -> None:
        def plan(messages, tools):  # type: ignore[no-untyped-def]
            done = len(_tool_results(messages))
            if "[后台任务]" in str(messages[0]["content"]):
                return ("chat-log", {"days": 1}) if done < 6 else f"查了 {done} 次"
            if not done:
                return ("start-task", {"action": "start", "goal": "多查几次"})
            return "好。"

        self.model.plan = plan
        self.service.handle_event(_private("帮我整理一下"))
        self.service.tasks.drain()
        self.assertEqual(self._sent()[-1], "查了 6 次")


if __name__ == "__main__":
    unittest.main()
