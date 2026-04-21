from __future__ import annotations

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


class LlmClientsTests(unittest.TestCase):
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
