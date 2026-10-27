from __future__ import annotations

import asyncio
import base64
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from waifu_standalone.config import AppConfig
from waifu_standalone.infra import image_inputs
from waifu_standalone.infra.llm_clients import LLMClientError, LLMToolCall, LLMToolResponse
from waifu_standalone.infra.safe_fetch import FetchedPage
from waifu_standalone.models import EmotionState, InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from waifu_standalone.reply_flow.prompt_assembler import Generator
from waifu_standalone.runtime.container import _build_runtime_outbound
from waifu_standalone.services import CapturingOutboundPort
from waifu_standalone.skills import ToolExecutionResult, ToolRegistry
from waifu_standalone.systems.searching import SearchDecider, SearchResult


# A 1x1 PNG.
PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
UNSEEN = "（对方发来的图片没能加载出来，你看不到它的内容）"


class _ImageClient:
    enabled = False

    def close(self):
        pass


class _ModelClient:
    enabled = True
    supports_tool_calling = True
    supports_images = True

    def __init__(self, *, rounds=0, final_error=False, image_error=False, requested_tool="audit-echo"):
        self.rounds = rounds
        self.final_error = final_error
        # Fail only requests that carry pictures, as a provider rejecting them would.
        self.image_error = image_error
        self.requested_tool = requested_tool
        self.requests = []
        self.plain_calls = 0

    def invoke(self, query, **kwargs):
        self.plain_calls += 1
        return "plain reply"

    async def ainvoke(self, query, **kwargs):
        return self.invoke(query, **kwargs)

    def invoke_with_tools(self, messages, *, tools, **kwargs):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        if len(self.requests) <= self.rounds:
            call = LLMToolCall(f"call_{len(self.requests)}", self.requested_tool, {})
            return LLMToolResponse(tool_calls=[call], assistant_message={
                "role": "assistant", "content": "", "tool_calls": [call.as_assistant_tool_call()],
            })
        if self.final_error or (self.image_error and isinstance(messages[-1]["content"], list)):
            raise LLMClientError("synthetic failure")
        return LLMToolResponse(text="final answer")

    async def ainvoke_with_tools(self, messages, *, tools, **kwargs):
        return self.invoke_with_tools(messages, tools=tools, **kwargs)

    def close(self):
        pass


class ProviderAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = AppConfig(data_root=self.temp.name)
        self.config.llm.enabled = True
        self.event = InboundEvent("audit", "person", "audit", "Audit", [MessageSegment("text", text="hello")])
        self.session = SessionMemory("audit", "person")

    def generator(self, client):
        generator = Generator(self.config, llm_client=client, image_client=_ImageClient())
        self.addCleanup(generator.close)
        return generator

    async def reply(self, generator, asynchronous):
        args = dict(assistant_name="Bot", address_override="Audit", tool_context=None, allow_fallback=False)
        if asynchronous:
            return await generator.agenerate_reply_message(self.event, self.session, EmotionState(), **args)
        return generator.generate_reply_message(self.event, self.session, EmotionState(), **args)

    async def test_tool_limit_synthesizes_with_all_results_and_tools_disabled(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                client = _ModelClient(rounds=3)
                generator = self.generator(client)
                tools = ToolRegistry()
                effects = []

                def execute(invocation):
                    effects.append(len(effects) + 1)
                    return ToolExecutionResult(text=f"RESULT_{effects[-1]}")

                tools.register("audit-echo", name="Audit", description="inert local test", handler=lambda inv: None,
                               model_handler=execute)
                generator.bind_tools(tools)
                result = await self.reply(generator, asynchronous)
                self.assertEqual(result.text, "final answer")
                self.assertEqual(effects, [1, 2, 3])
                self.assertEqual(len(client.requests), 4)
                self.assertEqual(client.requests[-1][1], [])
                history = client.requests[-1][0]
                self.assertEqual(sum(message["role"] == "tool" for message in history), 3)
                for index in (1, 2, 3):
                    self.assertIn(f"RESULT_{index}", str(history))
                self.assertEqual(client.plain_calls, 0)

    async def test_synthesis_failure_preserves_executed_tool_results(self):
        for asynchronous in (False, True):
            for rounds in (1, 3):
                with self.subTest(asynchronous=asynchronous, rounds=rounds):
                    client = _ModelClient(rounds=rounds, final_error=True)
                    generator = self.generator(client)
                    tools = ToolRegistry()
                    tools.register("audit-echo", name="Audit", description="inert", handler=lambda inv: None,
                                   model_handler=lambda inv: ToolExecutionResult(text="TRUE_RESULT"))
                    generator.bind_tools(tools)
                    result = await self.reply(generator, asynchronous)
                    self.assertIn("TRUE_RESULT", result.text)
                    self.assertEqual(client.plain_calls, 0)

    async def test_model_cannot_execute_a_tool_not_in_the_exposed_schema(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                client = _ModelClient(rounds=1, requested_tool="unexposed")
                generator = self.generator(client)
                tools = ToolRegistry()
                tools.register("audit-echo", name="Audit", description="inert", handler=lambda inv: None,
                               model_handler=lambda inv: self.fail("unexposed tool reached handler"))
                generator.bind_tools(tools)
                await self.reply(generator, asynchronous)
                self.assertIn("当前未提供", str(client.requests[-1][0]))

    async def test_images_are_sent_even_without_a_tool_registry(self):
        self.event.segments.append(MessageSegment("image", image_base64=PNG_BASE64))
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                client = _ModelClient()
                generator = self.generator(client)
                result = await self.reply(generator, asynchronous)
                content = client.requests[0][0][1]["content"]
                self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_BASE64}"}})
                self.assertEqual(client.requests[0][1], [])
                self.assertEqual(result.text, "final answer")
                self.assertEqual(client.plain_calls, 0)

    async def test_picture_links_are_downloaded_here_and_sent_inline(self):
        # Providers often fail to fetch QQ's CDN links themselves.
        self.event.segments.append(MessageSegment("image", image_url="https://multimedia.nt.qq.com.cn/download?fileid=1"))
        page = FetchedPage(url="", status_code=200, text="", headers={}, content=base64.b64decode(PNG_BASE64))
        client = _ModelClient()
        with patch.object(image_inputs, "fetch_public_url", return_value=page) as fetch:
            await self.reply(self.generator(client), True)
        self.assertEqual(fetch.call_args.args[0], "https://multimedia.nt.qq.com.cn/download?fileid=1")
        self.assertEqual(client.requests[0][0][1]["content"][1]["image_url"]["url"], f"data:image/png;base64,{PNG_BASE64}")

    async def test_multimodal_switch_and_unsupported_backend_name_the_unseen_picture(self):
        self.event.segments.append(MessageSegment("image", image_base64=PNG_BASE64))
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                client = _ModelClient()
                generator = self.generator(client)
                self.config.multimodal_enabled = False
                await self.reply(generator, asynchronous)
                # With multimodal off the message goes out as text, saying a picture was not seen.
                self.assertEqual(client.requests[-1][0][-1]["content"], f"hello\n{UNSEEN}")
                self.config.multimodal_enabled = True
                client.supports_images = False
                result = await self.reply(generator, asynchronous)
                self.assertEqual(client.requests[-1][0][-1]["content"], f"hello\n{UNSEEN}")
                self.assertEqual(result.text, "final answer")  # the character answers, no system error
                self.assertEqual(client.plain_calls, 0)

    async def test_a_picture_the_model_rejects_is_named_never_hidden(self):
        self.event.segments.append(MessageSegment("image", image_base64=PNG_BASE64))
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                client = _ModelClient(image_error=True)
                result = await self.reply(self.generator(client), asynchronous)
                # The retry tells the model the picture did not load instead of dropping it silently.
                self.assertIsInstance(client.requests[0][0][-1]["content"], list)
                self.assertEqual(client.requests[1][0][-1]["content"], f"hello\n{UNSEEN}")
                self.assertEqual(result.text, "final answer")
                self.assertEqual(client.plain_calls, 0)

                failing = _ModelClient(final_error=True)
                self.assertEqual((await self.reply(self.generator(failing), asynchronous)).text, "")

    async def test_disabled_image_generation_reports_failure(self):
        generator = self.generator(_ModelClient())
        self.config.image_generation.enabled = False
        with self.assertRaisesRegex(ValueError, "未启用"):
            generator.generate_image("cat")
        with self.assertRaisesRegex(ValueError, "未启用"):
            await generator.agenerate_image("cat")

    async def test_disabling_search_blocks_fetches_and_previously_cached_results(self):
        calls = []

        def fetch(query):
            calls.append(query)
            return [SearchResult(title="local", url="https://example.invalid", snippet="cached marker")]

        self.config.search_enabled = True
        search = SearchDecider(self.config, fetcher=fetch)
        self.addCleanup(search.close)
        self.assertTrue(search.search_query("cached query").active)
        self.config.search_enabled = False
        for query in ("cached query", "new query"):
            for result in (search.search_query(query), await search.asearch_query(query)):
                self.assertFalse(result.active)
                self.assertEqual(result.reason, "disabled")
                self.assertNotIn("cached marker", result.summary)
        self.assertEqual(calls, ["cached query"])

    async def test_dry_run_captures_for_http_and_reverse_ws(self):
        self.config.qq_sidecar.dry_run = True
        self.config.qq_sidecar.outbound_base_url = "http://127.0.0.1:9"
        for mode in ("http", "reverse_ws"):
            with self.subTest(mode=mode):
                self.config.qq_sidecar.gateway_mode = mode
                outbound, gateway = _build_runtime_outbound(self.config)
                self.assertIsInstance(outbound, CapturingOutboundPort)
                if mode == "reverse_ws":
                    self.assertIsNotNone(gateway)
                else:
                    self.assertIsNone(gateway)
                message = OutboundMessage("audit", "person", "captured only")
                outbound.send(message)
                self.assertEqual(outbound.sent, [message])


if __name__ == "__main__":
    unittest.main()
