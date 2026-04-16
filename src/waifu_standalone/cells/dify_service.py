from __future__ import annotations

from .llm_clients import LLMClientError, UnifiedLLMClient

DifyChatError = LLMClientError
DifyChatClient = UnifiedLLMClient

__all__ = ["DifyChatClient", "DifyChatError"]
