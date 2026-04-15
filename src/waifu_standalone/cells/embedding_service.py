from __future__ import annotations

import json
import math
import urllib.error
import urllib.request


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
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
        self.enabled = bool(enabled)
        self.backend = str(backend or "openai").strip().lower() or "openai"
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds or 30.0))

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.base_url and self.model)

    def embed(self, text: str) -> list[float]:
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            raise EmbeddingError("embedding input is empty")
        if not self.ready:
            raise EmbeddingError("embedding client is not configured")
        payload = json.dumps(
            {
                "model": self.model,
                "input": cleaned,
                "encoding_format": "float",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self._embeddings_url(),
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise EmbeddingError(f"embedding http {exc.code}: {body[:160]}") from exc
        except urllib.error.URLError as exc:
            raise EmbeddingError(f"embedding request failed: {exc.reason}") from exc
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmbeddingError("embedding response is not valid JSON") from exc
        data = decoded.get("data")
        if not isinstance(data, list) or not data:
            raise EmbeddingError("embedding response missing data")
        item = data[0] if isinstance(data[0], dict) else {}
        vector = item.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError("embedding response missing vector")
        normalized = self._normalize_vector(vector)
        if not normalized:
            raise EmbeddingError("embedding vector is empty")
        return normalized

    def _embeddings_url(self) -> str:
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        return f"{self.base_url}/embeddings"

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
