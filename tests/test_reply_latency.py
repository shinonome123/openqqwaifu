from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, ListenConfig, LLMConfig, ToolPolicyConfig
from waifu_standalone.infra.http_transport import HttpResponse, TransportError, _request_failure
from waifu_standalone.infra.llm_clients import LLMClientError, OpenAILLMClient, build_llm_client
from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.settings_admin.config_manager import ConfigManager
from waifu_standalone.skills import ToolInvocation

BOT_ID = "3956638110"
GROUP_ID = "612475113"


def _group(text: str, *, message_id: str, sender: str = "10001", mention: bool = False) -> InboundEvent:
    segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
    segments.append(MessageSegment(kind="text", text=text))
    return InboundEvent(GROUP_ID, "group", sender, f"user{sender}", segments, message_id=message_id)


class ReasoningEffortTests(unittest.TestCase):
    def test_a_configured_effort_goes_with_every_chat_request(self) -> None:
        client = OpenAILLMClient(base_url="https://api.x.ai/v1", api_key="k", model="grok-4.7", reasoning_effort="LOW")
        plain = OpenAILLMClient(base_url="https://api.x.ai/v1", api_key="k", model="grok-4.7")

        self.assertEqual(client._tool_payload([{"role": "user", "content": "在吗"}], tools=[], user="u")["reasoning_effort"], "low")
        self.assertEqual(client._payload("在吗", user="u")["reasoning_effort"], "low")
        self.assertNotIn("reasoning_effort", plain._tool_payload([{"role": "user", "content": "在吗"}], tools=[], user="u"))
        self.assertNotIn("reasoning_effort", plain._payload("在吗", user="u"))

    def test_the_effort_is_read_from_the_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps({"llm": {"enabled": True, "backend": "xai", "base_url": "https://api.x.ai/v1",
                                                "api_key": "k", "model": "grok-4.7", "reasoning_effort": "low"}}), encoding="utf-8")
            with patch("waifu_standalone.settings_admin.config_manager._napcat_config_dirs", return_value=[]):
                config = ConfigManager(path).load()
        self.assertEqual(config.llm.reasoning_effort, "low")
        self.assertEqual(build_llm_client(config).reasoning_effort, "low")
        self.assertEqual(LLMConfig().reasoning_effort, "")


class _FlakyTransport:
    """Fails with the given errors, then answers."""

    def __init__(self, *errors: TransportError) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def request(self, method, url, *, headers=None, content=None, json_payload=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return HttpResponse(status_code=200, text='{"choices": [{"message": {"content": "在的"}}]}', content=b"", headers={})


class RetryTests(unittest.TestCase):
    def _client(self, transport: _FlakyTransport) -> OpenAILLMClient:
        client = OpenAILLMClient(base_url="https://api.x.ai/v1", api_key="k", model="grok-4.7")
        client._async_transport = transport  # type: ignore[assignment]
        return client

    def test_a_connection_that_broke_off_is_tried_once_more(self) -> None:
        transport = _FlakyTransport(TransportError("request failed: RemoteProtocolError"))
        with self.assertLogs("waifu_standalone.infra.llm_clients", level="WARNING"):
            self.assertEqual(asyncio.run(self._client(transport).ainvoke("在吗")), "在的")
        self.assertEqual(transport.calls, 2)

    def test_timeouts_http_errors_and_a_second_failure_are_not_retried(self) -> None:
        with self.assertLogs("waifu_standalone.infra.llm_clients", level="WARNING"):
            for errors in (
                [TransportError("request failed: ReadTimeout", timed_out=True)],
                [TransportError("http 400: bad", status_code=400)],
                [TransportError("request failed: ConnectError"), TransportError("request failed: ConnectError")],
            ):
                transport = _FlakyTransport(*errors)
                with self.assertRaises(LLMClientError):
                    asyncio.run(self._client(transport).ainvoke("在吗"))
                self.assertEqual(transport.calls, len(errors))

    def test_an_error_without_a_message_still_names_its_type(self) -> None:
        silent = _request_failure(httpx.RemoteProtocolError(""))
        self.assertEqual(str(silent), "request failed: RemoteProtocolError")
        self.assertFalse(silent.timed_out)
        self.assertTrue(_request_failure(httpx.ReadTimeout("slow")).timed_out)


class BacklogTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.service, self.outbound = build_default_service(
            # The group-wide follow-up window and its backlog: the behaviour with listen off.
            AppConfig(
                data_root=temp.name,
                bot_account_id=BOT_ID,
                group_follow_up_window_seconds=60.0,
                listen=ListenConfig(enabled=False),
            )
        )
        self.addCleanup(self.service.close)
        self.service.handle_event(_group("在吗", message_id="1", mention=True))  # opens the follow-up window

    def test_only_the_newest_unaddressed_line_is_answered(self) -> None:
        # Two lines arrive while the bot is still busy; then each is handled in turn.
        older, newer = _group("今天吃啥", message_id="2"), _group("火锅吧", message_id="3", sender="10002")
        for event in (older, newer):
            self.service.emitter.note_inbound(event)
        sent = len(self.outbound.sent)

        self.assertIsNone(self.service._handle_event_locked(older))  # left to the newer line
        self.assertIsNotNone(self.service._handle_event_locked(newer))
        self.assertEqual(len(self.outbound.sent), sent + 1)
        # The skipped line is still context for the answer.
        history = self.service.memory.load(GROUP_ID, "group").history
        self.assertTrue(any(line.endswith("今天吃啥") for line in history))

    def test_a_line_that_ats_the_bot_is_always_answered(self) -> None:
        asked, later = _group("天气怎么样", message_id="4", mention=True), _group("哈哈", message_id="5", sender="10002")
        for event in (asked, later):
            self.service.emitter.note_inbound(event)

        self.assertIsNotNone(self.service._handle_event_locked(asked))


class SmallFixTests(unittest.TestCase):
    def test_bare_skill_commands_list_the_skills(self) -> None:
        service, _ = build_default_service()
        for text in ("/skill", "/skills", "/Skills"):
            self.assertEqual(service.dispatcher.resolve_explicit_skill_request(text)[0].skill_id, "skill-list-command")
        skill, args = service.dispatcher.resolve_explicit_skill_request("/skills weather 上海")
        self.assertEqual((skill.skill_id, args), ("weather-command", "上海"))

    def test_an_empty_shared_folder_is_not_called_a_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shared = Path(tmpdir) / "files"
            shared.mkdir()
            service, _ = build_default_service(AppConfig(
                data_root=tmpdir, tool_policy=ToolPolicyConfig(allowed_roots=[str(shared)], authorized_user_ids=["783190298"])
            ))
            event = InboundEvent("783190298", "person", "783190298", "tester", [MessageSegment(kind="text", text="看看")])
            result = service.dispatcher.use_list_files_tool(
                ToolInvocation(tool_id="list-files", raw_args="", event=event, session=SessionMemory("783190298", "person"))
            )
        self.assertIn("不是电脑桌面", result.text)
        self.assertNotIn(tmpdir, result.text)


if __name__ == "__main__":
    unittest.main()
