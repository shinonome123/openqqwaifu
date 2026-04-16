from __future__ import annotations

import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
