from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from ..http_transport import AsyncHttpTransport, SyncHttpTransport, TransportError


class LLMClientError(RuntimeError):
    pass


@runtime_checkable
class LLMClient(Protocol):
    base_url: str
    api_key: str
    model: str
    backend: str
    timeout_seconds: float
    app_type: str

    @property
    def enabled(self) -> bool:
        ...

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        ...

    async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        ...


class _BaseHTTPClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "",
        backend: str,
        timeout_seconds: float = 45.0,
        app_type: str = "chat",
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.backend = str(backend or "").strip().lower()
        self.timeout_seconds = float(timeout_seconds or 45.0)
        self.app_type = str(app_type or "chat").strip() or "chat"
        self._transport = SyncHttpTransport(timeout_seconds=self.timeout_seconds)
        self._async_transport = AsyncHttpTransport(timeout_seconds=self.timeout_seconds)

    def _post_json(self, url: str, payload: dict[str, object], *, headers: dict[str, str]) -> str:
        try:
            return self._transport.request(
                "POST",
                url,
                headers=headers,
                json_payload=payload,
            ).text
        except TransportError as exc:
            raise LLMClientError(f"llm request failed: {exc}") from exc

    async def _apost_json(self, url: str, payload: dict[str, object], *, headers: dict[str, str]) -> str:
        try:
            response = await self._async_transport.request(
                "POST",
                url,
                headers=headers,
                json_payload=payload,
            )
            return response.text
        except TransportError as exc:
            raise LLMClientError(f"llm request failed: {exc}") from exc

    def _endpoint(self, suffix: str, *, add_v1: bool = False) -> str:
        normalized = str(suffix or "").lstrip("/")
        if not normalized:
            return self.base_url
        if self.base_url.endswith(normalized):
            return self.base_url
        base = self.base_url
        if add_v1 and not base.endswith("/v1") and not base.endswith("/v1/"):
            base = f"{base}/v1"
        return f"{base.rstrip('/')}/{normalized}"

    @staticmethod
    def _extract_content_text(content: object) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            blocks: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = str(item.get("text", "") or "").strip()
                    if text:
                        blocks.append(text)
            return "\n".join(blocks).strip()
        if isinstance(content, dict):
            text = str(content.get("text", "") or "").strip()
            if text:
                return text
        return ""

    def close(self) -> None:
        self._transport.close()
        self._async_transport.close()


class DisabledLLMClient(_BaseHTTPClient):
    def __init__(self, *, base_url: str = "", api_key: str = "", model: str = "", backend: str = "disabled", timeout_seconds: float = 45.0, app_type: str = "chat"):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend=backend,
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )

    @property
    def enabled(self) -> bool:
        return False

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        raise LLMClientError("llm client is not configured")

    async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        raise LLMClientError("llm client is not configured")


class DifyLLMClient(_BaseHTTPClient):
    def __init__(self, *, base_url: str, api_key: str, model: str = "", timeout_seconds: float = 45.0, app_type: str = "chat"):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend="dify",
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.app_type == "chat")

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise LLMClientError("dify chat client is not configured")
        return self._parse_invoke(
            self._post_json(
                self._endpoint("chat-messages"),
                self._payload(query, user=user),
                headers=self._headers(),
            )
        )

    async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise LLMClientError("dify chat client is not configured")
        return self._parse_invoke(
            await self._apost_json(
                self._endpoint("chat-messages"),
                self._payload(query, user=user),
                headers=self._headers(),
            )
        )

    def _payload(self, query: str, *, user: str) -> dict[str, object]:
        return {
            "inputs": {},
            "query": str(query or ""),
            "user": user,
            "response_mode": "blocking",
            "conversation_id": "",
            "files": [],
            "model_config": {"model": self.model} if self.model else {},
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _parse_invoke(self, raw: str) -> str:
        data = json.loads(raw) if raw else {}
        answer = str(data.get("answer", "") or "").strip()
        if not answer:
            raise LLMClientError(f"dify returned no answer: {data}")
        return answer


class OpenAILLMClient(_BaseHTTPClient):
    def __init__(self, *, base_url: str, api_key: str, model: str = "", backend: str = "openai", timeout_seconds: float = 45.0, app_type: str = "chat"):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend=backend or "openai",
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise LLMClientError("openai-compatible client is not configured")
        return self._parse_invoke(
            self._post_json(
                self._endpoint("chat/completions"),
                self._payload(query, user=user),
                headers=self._headers(),
            )
        )

    async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise LLMClientError("openai-compatible client is not configured")
        return self._parse_invoke(
            await self._apost_json(
                self._endpoint("chat/completions"),
                self._payload(query, user=user),
                headers=self._headers(),
            )
        )

    def _payload(self, query: str, *, user: str) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": str(query or "")}],
            "user": user,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _parse_invoke(self, raw: str) -> str:
        data = json.loads(raw) if raw else {}
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise LLMClientError(f"llm returned no choices: {data}")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        answer = self._extract_content_text(message.get("content"))
        if not answer:
            raise LLMClientError(f"llm returned empty content: {data}")
        return answer


class ClaudeLLMClient(_BaseHTTPClient):
    def __init__(self, *, base_url: str, api_key: str, model: str = "", timeout_seconds: float = 45.0, app_type: str = "chat"):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend="claude",
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise LLMClientError("claude client is not configured")
        return self._parse_invoke(
            self._post_json(
                self._endpoint("messages", add_v1=True),
                self._payload(query, user=user),
                headers=self._headers(),
            )
        )

    async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise LLMClientError("claude client is not configured")
        return self._parse_invoke(
            await self._apost_json(
                self._endpoint("messages", add_v1=True),
                self._payload(query, user=user),
                headers=self._headers(),
            )
        )

    def _payload(self, query: str, *, user: str) -> dict[str, object]:
        return {
            "model": self.model,
            "max_tokens": 1024,
            "metadata": {"user_id": user},
            "messages": [{"role": "user", "content": str(query or "")}],
        }

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _parse_invoke(self, raw: str) -> str:
        data = json.loads(raw) if raw else {}
        answer = self._extract_content_text(data.get("content"))
        if not answer:
            raise LLMClientError(f"claude returned empty content: {data}")
        return answer


class UnifiedLLMClient(_BaseHTTPClient):
    @property
    def enabled(self) -> bool:
        return build_llm_client_from_values(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            backend=self.backend,
            timeout_seconds=self.timeout_seconds,
            app_type=self.app_type,
        ).enabled

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        client = build_llm_client_from_values(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            backend=self.backend,
            timeout_seconds=self.timeout_seconds,
            app_type=self.app_type,
        )
        return client.invoke(query, user=user)

    async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        client = build_llm_client_from_values(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            backend=self.backend,
            timeout_seconds=self.timeout_seconds,
            app_type=self.app_type,
        )
        return await client.ainvoke(query, user=user)

    def close(self) -> None:
        super().close()


def build_llm_client_from_values(
    *,
    base_url: str,
    api_key: str,
    model: str = "",
    backend: str = "dify",
    timeout_seconds: float = 45.0,
    app_type: str = "chat",
) -> LLMClient:
    resolved_backend = str(backend or "dify").strip().lower() or "dify"
    if resolved_backend == "dify":
        return DifyLLMClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )
    if resolved_backend == "claude":
        return ClaudeLLMClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )
    if resolved_backend in {"openai", "openai-compatible", "openrouter", "xai", "grok"} or resolved_backend:
        return OpenAILLMClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend=resolved_backend,
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )
    return DisabledLLMClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        backend=resolved_backend or "disabled",
        timeout_seconds=timeout_seconds,
        app_type=app_type,
    )


def build_llm_client(config: object) -> LLMClient:
    llm_config = getattr(config, "llm", config)
    return build_llm_client_from_values(
        base_url=str(getattr(llm_config, "base_url", "") or ""),
        api_key=str(getattr(llm_config, "api_key", "") or ""),
        model=str(getattr(llm_config, "model", "") or ""),
        backend=str(getattr(llm_config, "backend", "dify") or "dify"),
        timeout_seconds=float(getattr(llm_config, "timeout_seconds", 45.0) or 45.0),
        app_type=str(getattr(llm_config, "app_type", "chat") or "chat"),
    )
