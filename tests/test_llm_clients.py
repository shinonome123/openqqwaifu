from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.llm_clients import (
    ClaudeLLMClient,
    DifyLLMClient,
    OpenAILLMClient,
    UnifiedLLMClient,
    build_llm_client_from_values,
)
from waifu_standalone.infra.llm_clients import LLMClientError, normalize_image_reference


class LlmClientsTests(unittest.TestCase):
    def test_multimodal_payload_preserves_urls_and_base64_for_supported_providers(self) -> None:
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nlocal-test").decode()
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "describe both images"},
            {"type": "image_url", "image_url": {"url": "https://example.invalid/photo.jpg"}},
            {"type": "image_url", "image_url": {"url": "base64://" + encoded}},
        ]}]
        openai = OpenAILLMClient(base_url="https://example.invalid/v1", api_key="test", model="test", backend="openai")
        claude = ClaudeLLMClient(base_url="https://example.invalid", api_key="test", model="test")
        self.addCleanup(openai.close)
        self.addCleanup(claude.close)
        first = openai._tool_payload(messages, tools=[], user="test")
        self.assertNotIn("tools", first)
        self.assertNotIn("tool_choice", first)
        self.assertEqual(first["messages"][0]["content"][1]["image_url"]["url"], "https://example.invalid/photo.jpg")
        self.assertEqual(first["messages"][0]["content"][2]["image_url"]["url"], "data:image/png;base64," + encoded)
        second = claude._tool_payload(messages, tools=[], user="test")
        self.assertNotIn("tools", second)
        blocks = second["messages"][0]["content"]
        self.assertEqual(blocks[1], {"type": "image", "source": {"type": "url", "url": "https://example.invalid/photo.jpg"}})
        self.assertEqual(blocks[2], {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}})

    def test_invalid_image_reference_is_not_silently_discarded(self) -> None:
        for reference in ("file:///secret.png", "http://[", "https:///empty-host", "base64://not base64", "data:text/plain;base64,aGVsbG8="):
            with self.subTest(reference=reference), self.assertRaises(LLMClientError):
                normalize_image_reference(reference)

    def test_final_synthesis_keeps_tool_history_without_exposing_tools(self) -> None:
        messages = [
            {"role": "user", "content": "look up a fact"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "FACT_MARKER"},
        ]
        clients = [OpenAILLMClient(base_url="https://example.invalid/v1", api_key="test", model="test", backend="openai"),
                   ClaudeLLMClient(base_url="https://example.invalid", api_key="test", model="test")]
        for client in clients:
            self.addCleanup(client.close)
            payload = client._tool_payload(messages, tools=[], user="test")
            self.assertNotIn("tools", payload)
            self.assertIn("FACT_MARKER", str(payload["messages"]))
            self.assertIn("call_1", str(payload["messages"]))

    def test_a_reply_streamed_though_not_asked_for_is_read_whole(self) -> None:
        # DZMM streams long tavern replies even with stream off; every round failed to parse.
        from waifu_standalone.infra.llm_clients import OpenAILLMClient

        client = OpenAILLMClient(base_url="http://example.invalid/v1", api_key="k", model="m")
        streamed = (
            'data:  {"choices":[{"delta":{"content":"<think>线索：茶杯</think>"}}]}\n\n'
            'data:  {"choices":[{"delta":{"content":"雨还在下，"}}]}\n\n'
            'data:  {"choices":[{"delta":{"content":"书房的门闩着。"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n"
        )
        self.assertEqual(client._parse_tool_response(streamed).text, "<think>线索：茶杯</think>雨还在下，书房的门闩着。")
        self.assertEqual(client._parse_invoke(streamed), "<think>线索：茶杯</think>雨还在下，书房的门闩着。")

        calls = (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search-command","arguments":"{\\"q"}}]}}]}\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"uery\\": \\"雨\\"}"}}]}}]}\n'
            "data: [DONE]\n"
        )
        (call,) = client._parse_tool_response(calls).tool_calls
        self.assertEqual((call.call_id, call.tool_name, call.arguments), ("c1", "search-command", {"query": "雨"}))

        # An emoji whose escape is split across two chunks comes back whole.
        split = 'data: {"choices":[{"delta":{"content":"笑\\ud83d"}}]}\ndata: {"choices":[{"delta":{"content":"\\ude00了"}}]}\n'
        text = client._parse_tool_response(split).text
        self.assertEqual(text, "笑😀了")
        text.encode("utf-8")

        plain = '{"choices":[{"message":{"role":"assistant","content":"好"}}]}'
        self.assertEqual(client._parse_tool_response(plain).text, "好")

    def test_build_llm_client_returns_strategy_for_backend(self) -> None:
        self.assertIsInstance(
            build_llm_client_from_values(
                base_url="https://example.com",
                api_key="secret",
                model="waifu-grok",
                backend="dify",
            ),
            DifyLLMClient,
        )
        self.assertIsInstance(
            build_llm_client_from_values(
                base_url="https://example.com/v1",
                api_key="secret",
                model="grok-3-mini",
                backend="openai",
            ),
            OpenAILLMClient,
        )
        self.assertIsInstance(
            build_llm_client_from_values(
                base_url="https://example.com",
                api_key="secret",
                model="claude-sonnet-4-0",
                backend="claude",
            ),
            ClaudeLLMClient,
        )

    def test_unified_client_keeps_legacy_mutable_backend_behavior(self) -> None:
        client = UnifiedLLMClient(
            base_url="https://example.com",
            api_key="secret",
            model="waifu-grok",
            backend="dify",
        )
        self.assertTrue(client.enabled)
        client.backend = "openai"
        client.model = "grok-3-mini"
        self.assertTrue(client.enabled)

    def test_openai_tool_call_parsing_round_trip(self) -> None:
        client = OpenAILLMClient(
            base_url="https://example.com/v1",
            api_key="secret",
            model="grok-3-mini",
            backend="openai",
        )

        with patch.object(
            client,
            "_post_json",
            return_value='{"choices":[{"message":{"content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"search","arguments":"{\\"query\\":\\"北京天气\\"}"}}]}}]}',
        ):
            response = client.invoke_with_tools(
                [{"role": "user", "content": "查天气"}],
                tools=[
                    {
                        "name": "search",
                        "description": "search web",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ],
            )

        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].tool_name, "search")
        self.assertEqual(response.tool_calls[0].arguments, {"query": "北京天气"})
        self.assertEqual(response.assistant_message["role"], "assistant")

    def test_claude_tool_call_parsing_round_trip(self) -> None:
        client = ClaudeLLMClient(
            base_url="https://example.com",
            api_key="secret",
            model="claude-sonnet-4-0",
        )

        with patch.object(
            client,
            "_post_json",
            return_value='{"content":[{"type":"tool_use","id":"toolu_1","name":"read-file","input":{"path":"C:/tmp/demo.txt"}}]}',
        ):
            response = client.invoke_with_tools(
                [{"role": "user", "content": "读文件"}],
                tools=[
                    {
                        "name": "read-file",
                        "description": "read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ],
            )

        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].tool_name, "read-file")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "C:/tmp/demo.txt"})
        self.assertEqual(response.assistant_message["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
