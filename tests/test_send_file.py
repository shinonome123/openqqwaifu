from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, ToolPolicyConfig
from waifu_standalone.gateways.onebot_actions import OneBotHttpOutboundPort
from waifu_standalone.gateways.onebot_ws import OneBotWsOutboundPort
from waifu_standalone.infra.llm_clients import LLMToolCall, LLMToolResponse
from waifu_standalone.members.onboarding import MemberOnboarding
from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.skills import ToolInvocation
from waifu_standalone.skills import dispatcher as dispatcher_module

OWNER = "783190298"
GROUP_ID = "612475113"
BOT_ID = "3956638110"


def _event(sender: str = OWNER, *, group: bool = True, text: str = "发一下") -> InboundEvent:
    if group:
        segments = [MessageSegment(kind="mention", mention_target=BOT_ID), MessageSegment(kind="text", text=text)]
        return InboundEvent(GROUP_ID, "group", sender, "tester", segments)
    return InboundEvent(sender, "person", sender, "tester", [MessageSegment(kind="text", text=text)])


class SendFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.shared = self.data / "files"
        self.shared.mkdir()
        (self.shared / "xiaohua.txt").write_text("为什么数学书很忧郁？因为它有太多问题。", encoding="utf-8")
        (self.data / "secret.txt").write_text("sk-secret", encoding="utf-8")
        roots = [str(self.shared)]
        self.service, self.outbound = build_default_service(AppConfig(
            data_root=temp.name,
            bot_account_id=BOT_ID,
            tool_policy=ToolPolicyConfig(allowed_roots=roots, write_allowed_roots=roots, write_enabled=True,
                                         authorized_user_ids=[OWNER]),
        ))
        self.addCleanup(self.service.close)

    def _send(self, event: InboundEvent, **arguments: object):  # type: ignore[no-untyped-def]
        invocation = ToolInvocation(tool_id="send-file", raw_args="", event=event,
                                    session=SessionMemory(event.launcher_id, event.launcher_type), arguments=dict(arguments))
        return asyncio.run(self.service.dispatcher.ause_send_file_tool(invocation))

    def test_a_file_in_the_shared_folder_goes_out_as_a_group_file(self) -> None:
        result = self._send(_event(), path="xiaohua.txt")

        self.assertFalse(result.error, result.text)
        self.assertEqual(result.text, "已把「xiaohua.txt」作为群文件发出去了。")
        content = base64.b64encode((self.shared / "xiaohua.txt").read_bytes()).decode()
        self.assertEqual(
            self.outbound.actions,
            [("upload_group_file", {"group_id": int(GROUP_ID), "file": f"base64://{content}", "name": "xiaohua.txt"})],
        )

    def test_in_a_private_chat_it_is_a_private_file_and_the_name_can_change(self) -> None:
        result = self._send(_event(group=False), path="xiaohua.txt", name="笑话.txt")

        self.assertEqual(result.text, "已把「笑话.txt」作为私聊文件发出去了。")
        action, params = self.outbound.actions[-1]
        self.assertEqual((action, params["user_id"], params["name"]), ("upload_private_file", int(OWNER), "笑话.txt"))

    def test_strangers_outside_paths_and_large_files_are_refused(self) -> None:
        self.assertIn("permission denied", self._send(_event("10001"), path="xiaohua.txt").text)
        for path in ("../secret.txt", str(self.data / "secret.txt")):
            self.assertIn("路径超出允许范围", self._send(_event(), path=path).text)
        self.assertIn("路径不存在", self._send(_event(), path="nope.txt").text)
        with patch.object(dispatcher_module, "_SEND_FILE_MAX_BYTES", 10):
            self.assertIn("文件太大", self._send(_event(), path="xiaohua.txt").text)
        self.assertEqual(self.outbound.actions, [])

    def test_a_free_skill_request_offers_every_tool_the_sender_may_use(self) -> None:
        orchestrator = self.service.tool_orchestrator
        self.service.config.llm.enabled = True

        def offered(sender: str) -> set[str]:
            event = _event(sender, text="/skill 写一个小笑话保存为xiaohua.txt，并把它发到群里")
            context = orchestrator.exposure_context(event, SessionMemory(GROUP_ID, "group"), latest_message="写个笑话",
                                                    address="tester", assistant_name="极光", all_tools=True)
            return {schema["name"] for schema in self.service.tools.model_schemas(context)}

        owner, stranger = offered(OWNER), offered("10001")
        self.assertTrue({"write-file-command", "send-file-command", "weather-command"} <= owner)
        self.assertIn("weather-command", stranger)
        self.assertFalse({"write-file-command", "send-file-command"} & stranger)


class FreeSkillRequestTests(unittest.TestCase):
    def test_skill_followed_by_a_sentence_reaches_the_model_with_tools(self) -> None:
        service, _ = build_default_service(AppConfig(bot_account_id=BOT_ID))
        self.addCleanup(service.close)
        service.config.llm.enabled = True
        offered: list[list[str]] = []

        class _Client:
            enabled = True
            supports_tool_calling = True
            supports_images = True

            async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
                offered.append(sorted(tool["name"] for tool in tools))
                return LLMToolResponse(text="好嘞", assistant_message={"role": "assistant", "content": "好嘞"})

            def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
                raise AssertionError("async path only")

            async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
                return "{}"

            def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
                return "{}"

            def close(self) -> None:
                pass

        async def keep_reply(_self, *args, reply_text: str, **kwargs):  # type: ignore[no-untyped-def]
            return reply_text

        service.generator._dify_client = _Client()  # type: ignore[assignment]
        with patch.object(MemberOnboarding, "amaybe_append_soft_ask", keep_reply):
            reply = service.handle_event(_event(text="/skill 讲个冷笑话然后查一下上海天气"))

        self.assertIsNotNone(reply)
        self.assertIn("weather-command", offered[0])  # no weather wording needed: "/skill" offers everything allowed
        self.assertEqual(service.dispatcher.free_skill_request("/skill weather 上海"), "weather 上海")
        self.assertEqual(service.dispatcher.free_skill_request("/skills 写个笑话"), "写个笑话")
        self.assertEqual(service.dispatcher.free_skill_request("/help 写个笑话"), "")


class _PlanningClient:
    """First only describes the plan, then writes the file when nudged, then sums up."""

    enabled = True
    supports_tool_calling = True
    supports_images = True

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []

    async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        self.requests.append([dict(message) for message in messages])
        if len(self.requests) == 1:
            return LLMToolResponse(text="我先写个笑话，写好再存。", assistant_message={"role": "assistant", "content": "我先写个笑话，写好再存。"})
        if len(self.requests) == 2:
            call = LLMToolCall("call_1", "write-file-command", {"path": "a.txt", "content": "冷笑话"})
            return LLMToolResponse(tool_calls=[call], assistant_message={
                "role": "assistant", "content": "", "tool_calls": [call.as_assistant_tool_call()]})
        return LLMToolResponse(text="存好了。", assistant_message={"role": "assistant", "content": "存好了。"})

    def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        raise AssertionError("async path only")

    async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return "{}"

    def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return "{}"

    def close(self) -> None:
        pass


class ExplicitRequestTests(unittest.TestCase):
    def _service(self):  # type: ignore[no-untyped-def]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        shared = Path(temp.name) / "files"
        shared.mkdir()
        service, _ = build_default_service(AppConfig(
            data_root=temp.name,
            tool_policy=ToolPolicyConfig(allowed_roots=[str(shared)], write_allowed_roots=[str(shared)],
                                         write_enabled=True, authorized_user_ids=[OWNER]),
        ))
        self.addCleanup(service.close)
        service.config.llm.enabled = True
        client = _PlanningClient()
        service.generator._dify_client = client  # type: ignore[assignment]

        async def keep_reply(_self, *args, reply_text: str, **kwargs):  # type: ignore[no-untyped-def]
            return reply_text

        soft_ask = patch.object(MemberOnboarding, "amaybe_append_soft_ask", keep_reply)
        soft_ask.start()
        self.addCleanup(soft_ask.stop)
        return service, client, shared

    def test_a_skill_request_that_only_plans_is_asked_once_to_act(self) -> None:
        service, client, shared = self._service()

        reply = service.handle_event(_event(group=False, text="/skill 写个冷笑话保存为a.txt"))

        self.assertEqual(reply.text, "存好了。")
        self.assertEqual((shared / "a.txt").read_text(encoding="utf-8"), "冷笑话")
        self.assertIn("这条消息是对方用 /skill 发起的", str(client.requests[0][0]["content"]))
        self.assertEqual(client.requests[1][-2]["content"], "我先写个笑话，写好再存。")
        self.assertIn("请直接调用工具", str(client.requests[1][-1]["content"]))

    def test_an_ordinary_message_is_not_nudged(self) -> None:
        service, client, _ = self._service()

        reply = service.handle_event(_event(group=False, text="帮我写个冷笑话保存为a.txt"))

        self.assertEqual(reply.text, "我先写个笑话，写好再存。")
        self.assertEqual(len(client.requests), 1)
        self.assertNotIn("这条消息是对方用 /skill 发起的", str(client.requests[0][0]["content"]))


class ActionPortTests(unittest.TestCase):
    def test_the_live_ports_pass_other_actions_through(self) -> None:
        class _Gateway:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def send_action(self, action, params):  # type: ignore[no-untyped-def]
                self.calls.append((action, params))
                return {"status": "ok", "retcode": 0, "data": {"file_id": "f1"}}

        gateway = _Gateway()
        result = asyncio.run(OneBotWsOutboundPort(gateway).acall_action("upload_group_file", {"group_id": 1}))  # type: ignore[arg-type]
        self.assertEqual((gateway.calls, result["data"]), ([("upload_group_file", {"group_id": 1})], {"file_id": "f1"}))

        class _Client:
            def post_action(self, action, payload):  # type: ignore[no-untyped-def]
                return {"status": "ok", "action": action, "payload": payload}

        port = OneBotHttpOutboundPort(_Client())  # type: ignore[arg-type]
        self.assertEqual(port.call_action("upload_private_file", {"user_id": 2})["action"], "upload_private_file")


if __name__ == "__main__":
    unittest.main()
