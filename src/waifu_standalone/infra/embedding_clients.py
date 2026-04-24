from __future__ import annotations

import json
import math
from typing import Protocol, runtime_checkable

from ..config import AppConfig
from .llm_clients import _BaseHTTPClient


class EmbeddingClientError(RuntimeError):
    pass


@runtime_checkable
class EmbeddingClient(Protocol):
    enabled: bool
    backend: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float

    @property
    def ready(self) -> bool:
        ...

    def embed(self, text: str) -> list[float]:
        ...

    async def aembed(self, text: str) -> list[float]:
        ...


class DisabledEmbeddingClient(_BaseHTTPClient):
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        backend: str = "disabled",
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend=backend,
            timeout_seconds=timeout_seconds,
            metrics_kind="embedding",
            metrics_target=backend or "disabled",
        )
        self.enabled = False

    @property
    def ready(self) -> bool:
        return False

    def embed(self, text: str) -> list[float]:
        raise EmbeddingClientError("embedding client is not configured")

    async def aembed(self, text: str) -> list[float]:
        raise EmbeddingClientError("embedding client is not configured")

    def close(self) -> None:
        super().close()


class OpenAIEmbeddingClient(_BaseHTTPClient):
    def __init__(
        self,
        *,
        enabled: bool,
        backend: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend=backend or "openai",
            timeout_seconds=timeout_seconds,
            metrics_kind="embedding",
            metrics_target=backend or "openai",
        )
        self.enabled = bool(enabled)

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.base_url and self.model)

    def embed(self, text: str) -> list[float]:
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            raise EmbeddingClientError("embedding input is empty")
        if not self.ready:
            raise EmbeddingClientError("embedding client is not configured")
        return self._parse_embed(
            self._post_json(
                self._endpoint("embeddings"),
                self._payload(cleaned),
                headers=self._headers(),
            )
        )

    async def aembed(self, text: str) -> list[float]:
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            raise EmbeddingClientError("embedding input is empty")
        if not self.ready:
            raise EmbeddingClientError("embedding client is not configured")
        return self._parse_embed(
            await self._apost_json(
                self._endpoint("embeddings"),
                self._payload(cleaned),
                headers=self._headers(),
            )
        )

    def _payload(self, cleaned: str) -> dict[str, object]:
        return {
            "model": self.model,
            "input": cleaned,
            "encoding_format": "float",
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _parse_embed(self, raw: str) -> list[float]:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmbeddingClientError("embedding response is not valid JSON") from exc
        data = decoded.get("data")
        if not isinstance(data, list) or not data:
            raise EmbeddingClientError("embedding response missing data")
        item = data[0] if isinstance(data[0], dict) else {}
        vector = item.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise EmbeddingClientError("embedding response missing vector")
        normalized = self._normalize_vector(vector)
        if not normalized:
            raise EmbeddingClientError("embedding vector is empty")
        return normalized

    @staticmethod
    def _normalize_vector(values: list[object]) -> list[float]:
        vector: list[float] = []
        for value in values:
            try:
                vector.append(float(value))
            except (TypeError, ValueError):
                continue
        if not vector:
            return []
        magnitude = math.sqrt(sum(item * item for item in vector))
        if magnitude <= 0:
            return vector
        return [item / magnitude for item in vector]

    def close(self) -> None:
        super().close()


class UnifiedEmbeddingClient(OpenAIEmbeddingClient):
    pass


def build_embedding_client(config: AppConfig) -> EmbeddingClient:
    embedding = config.embedding
    if not bool(embedding.enabled):
        return DisabledEmbeddingClient(
            base_url=embedding.base_url,
            api_key=embedding.api_key,
            model=embedding.model,
            backend=embedding.backend or "disabled",
            timeout_seconds=embedding.timeout_seconds,
        )
    return OpenAIEmbeddingClient(
        enabled=embedding.enabled,
        backend=embedding.backend,
        base_url=embedding.base_url,
        api_key=embedding.api_key,
        model=embedding.model,
        timeout_seconds=embedding.timeout_seconds,
    )
