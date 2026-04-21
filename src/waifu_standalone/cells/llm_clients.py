from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..http_transport import AsyncHttpTransport, SyncHttpTransport, TransportError
from ..observability import TransportMetricsScope


class LLMClientError(RuntimeError):
    pass


@dataclass(slots=True)
class LLMToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)

    def as_assistant_tool_call(self) -> dict[str, object]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.tool_name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class LLMToolResponse:
    text: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    assistant_message: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    base_url: str
    api_key: str
    model: str
    backend: str
    timeout_seconds: float
    app_type: str

    @property
    def supports_tool_calling(self) -> bool:
        ...

    @property
    def enabled(self) -> bool:
        ...

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        ...

    async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        ...

    def invoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        ...

    async def ainvoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
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
        metrics_kind: str = "llm",
        metrics_target: str = "",
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.backend = str(backend or "").strip().lower()
        self.timeout_seconds = float(timeout_seconds or 45.0)
        self.app_type = str(app_type or "chat").strip() or "chat"
        scope = TransportMetricsScope(
            kind=str(metrics_kind or "llm").strip() or "llm",
            target=str(metrics_target or self.backend or "unknown").strip() or "unknown",
        )
        self._transport = SyncHttpTransport(timeout_seconds=self.timeout_seconds, metrics_scope=scope)
        self._async_transport = AsyncHttpTransport(timeout_seconds=self.timeout_seconds, metrics_scope=scope)

    @property
    def supports_tool_calling(self) -> bool:
        return False

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

    @staticmethod
    def _coerce_message_content(content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            blocks: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "") or "").strip()
                if text:
                    blocks.append(text)
            return "\n".join(blocks).strip()
        return str(content or "").strip()

    @staticmethod
    def _parse_tool_arguments(raw_arguments: object) -> dict[str, object]:
        if isinstance(raw_arguments, dict):
            return {str(key): value for key, value in raw_arguments.items()}
        text = str(raw_arguments or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): value for key, value in payload.items()}

    def invoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        raise LLMClientError(f"{self.backend or 'llm'} client does not support tool calling")

    async def ainvoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        raise LLMClientError(f"{self.backend or 'llm'} client does not support tool calling")

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

    @property
    def supports_tool_calling(self) -> bool:
        return self.enabled

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

    def _tool_payload(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str,
    ) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": self._normalize_tool_messages(messages),
            "user": user,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name", "") or "").strip(),
                        "description": str(tool.get("description", "") or "").strip(),
                        "parameters": dict(tool.get("parameters", {}) or {"type": "object", "properties": {}}),
                    },
                }
                for tool in tools
                if str(tool.get("name", "") or "").strip()
            ],
            "tool_choice": "auto",
        }
 
    def _normalize_tool_messages(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        normalized: list[dict[str, object]] = []
        for message in messages:
            role = str(message.get("role", "") or "").strip().lower()
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            payload: dict[str, object] = {"role": role}
            if role == "tool":
                payload["tool_call_id"] = str(message.get("tool_call_id", "") or "").strip()
                name = str(message.get("name", "") or "").strip()
                if name:
                    payload["name"] = name
                payload["content"] = self._coerce_message_content(message.get("content"))
                normalized.append(payload)
                continue
            content = message.get("content", "")
            payload["content"] = self._coerce_message_content(content)
            tool_calls = message.get("tool_calls", [])
            if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
                sanitized_calls: list[dict[str, object]] = []
                for item in tool_calls:
                    if not isinstance(item, dict):
                        continue
                    tool_id = str(item.get("id", "") or "").strip()
                    function_payload = item.get("function", {})
                    function_payload = function_payload if isinstance(function_payload, dict) else {}
                    function_name = str(function_payload.get("name", "") or "").strip()
                    function_arguments = function_payload.get("arguments", "{}")
                    if not tool_id or not function_name:
                        continue
                    sanitized_calls.append(
                        {
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": function_name,
                                "arguments": str(function_arguments or "{}"),
                            },
                        }
                    )
                if sanitized_calls:
                    payload["tool_calls"] = sanitized_calls
            normalized.append(payload)
        return normalized

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

    def invoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        if not self.supports_tool_calling:
            raise LLMClientError("openai-compatible client is not configured")
        raw = self._post_json(
            self._endpoint("chat/completions"),
            self._tool_payload(messages, tools=tools, user=user),
            headers=self._headers(),
        )
        return self._parse_tool_response(raw)

    async def ainvoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        if not self.supports_tool_calling:
            raise LLMClientError("openai-compatible client is not configured")
        raw = await self._apost_json(
            self._endpoint("chat/completions"),
            self._tool_payload(messages, tools=tools, user=user),
            headers=self._headers(),
        )
        return self._parse_tool_response(raw)

    def _parse_tool_response(self, raw: str) -> LLMToolResponse:
        data = json.loads(raw) if raw else {}
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise LLMClientError(f"llm returned no choices: {data}")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        message = message if isinstance(message, dict) else {}
        text = self._extract_content_text(message.get("content"))
        raw_tool_calls = message.get("tool_calls", [])
        tool_calls: list[LLMToolCall] = []
        assistant_tool_calls: list[dict[str, object]] = []
        if isinstance(raw_tool_calls, list):
            for item in raw_tool_calls:
                if not isinstance(item, dict):
                    continue
                call_id = str(item.get("id", "") or "").strip()
                function_payload = item.get("function", {})
                function_payload = function_payload if isinstance(function_payload, dict) else {}
                tool_name = str(function_payload.get("name", "") or "").strip()
                if not call_id or not tool_name:
                    continue
                arguments = self._parse_tool_arguments(function_payload.get("arguments"))
                call = LLMToolCall(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
                tool_calls.append(call)
                assistant_tool_calls.append(call.as_assistant_tool_call())
        assistant_message: dict[str, object] = {"role": "assistant", "content": text}
        if assistant_tool_calls:
            assistant_message["tool_calls"] = assistant_tool_calls
        return LLMToolResponse(
            text=text,
            tool_calls=tool_calls,
            assistant_message=assistant_message,
        )


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

    @property
    def supports_tool_calling(self) -> bool:
        return self.enabled

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

    def _tool_payload(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str,
    ) -> dict[str, object]:
        system_blocks: list[str] = []
        payload_messages: list[dict[str, object]] = []
        for message in messages:
            role = str(message.get("role", "") or "").strip().lower()
            if role == "system":
                text = self._coerce_message_content(message.get("content"))
                if text:
                    system_blocks.append(text)
                continue
            if role == "user":
                payload_messages.append(
                    {
                        "role": "user",
                        "content": self._coerce_message_content(message.get("content")),
                    }
                )
                continue
            if role == "assistant":
                content_blocks: list[dict[str, object]] = []
                text = self._coerce_message_content(message.get("content"))
                if text:
                    content_blocks.append({"type": "text", "text": text})
                raw_tool_calls = message.get("tool_calls", [])
                if isinstance(raw_tool_calls, list):
                    for item in raw_tool_calls:
                        if not isinstance(item, dict):
                            continue
                        tool_id = str(item.get("id", "") or "").strip()
                        function_payload = item.get("function", {})
                        function_payload = function_payload if isinstance(function_payload, dict) else {}
                        tool_name = str(function_payload.get("name", "") or "").strip()
                        if not tool_id or not tool_name:
                            continue
                        content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": self._parse_tool_arguments(function_payload.get("arguments")),
                            }
                        )
                payload_messages.append(
                    {
                        "role": "assistant",
                        "content": content_blocks or [{"type": "text", "text": text}],
                    }
                )
                continue
            if role == "tool":
                tool_call_id = str(message.get("tool_call_id", "") or "").strip()
                if not tool_call_id:
                    continue
                payload_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_call_id,
                                "content": self._coerce_message_content(message.get("content")),
                            }
                        ],
                    }
                )
        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": 1024,
            "metadata": {"user_id": user},
            "messages": payload_messages or [{"role": "user", "content": ""}],
            "tools": [
                {
                    "name": str(tool.get("name", "") or "").strip(),
                    "description": str(tool.get("description", "") or "").strip(),
                    "input_schema": dict(tool.get("parameters", {}) or {"type": "object", "properties": {}}),
                }
                for tool in tools
                if str(tool.get("name", "") or "").strip()
            ],
        }
        if system_blocks:
            payload["system"] = "\n\n".join(block for block in system_blocks if block.strip())
        return payload

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

    def invoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        if not self.supports_tool_calling:
            raise LLMClientError("claude client is not configured")
        raw = self._post_json(
            self._endpoint("messages", add_v1=True),
            self._tool_payload(messages, tools=tools, user=user),
            headers=self._headers(),
        )
        return self._parse_tool_response(raw)

    async def ainvoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        if not self.supports_tool_calling:
            raise LLMClientError("claude client is not configured")
        raw = await self._apost_json(
            self._endpoint("messages", add_v1=True),
            self._tool_payload(messages, tools=tools, user=user),
            headers=self._headers(),
        )
        return self._parse_tool_response(raw)

    def _parse_tool_response(self, raw: str) -> LLMToolResponse:
        data = json.loads(raw) if raw else {}
        content = data.get("content", [])
        content = content if isinstance(content, list) else []
        text_blocks: list[str] = []
        tool_calls: list[LLMToolCall] = []
        assistant_tool_calls: list[dict[str, object]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type", "") or "").strip().lower()
            if block_type == "text":
                text = str(item.get("text", "") or "").strip()
                if text:
                    text_blocks.append(text)
                continue
            if block_type != "tool_use":
                continue
            call_id = str(item.get("id", "") or "").strip()
            tool_name = str(item.get("name", "") or "").strip()
            if not call_id or not tool_name:
                continue
            arguments = item.get("input", {})
            arguments = arguments if isinstance(arguments, dict) else {}
            call = LLMToolCall(
                call_id=call_id,
                tool_name=tool_name,
                arguments={str(key): value for key, value in arguments.items()},
            )
            tool_calls.append(call)
            assistant_tool_calls.append(call.as_assistant_tool_call())
        text = "\n".join(text_blocks).strip()
        assistant_message: dict[str, object] = {"role": "assistant", "content": text}
        if assistant_tool_calls:
            assistant_message["tool_calls"] = assistant_tool_calls
        return LLMToolResponse(
            text=text,
            tool_calls=tool_calls,
            assistant_message=assistant_message,
        )


class UnifiedLLMClient(_BaseHTTPClient):
    @property
    def supports_tool_calling(self) -> bool:
        return build_llm_client_from_values(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            backend=self.backend,
            timeout_seconds=self.timeout_seconds,
            app_type=self.app_type,
        ).supports_tool_calling

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

    def invoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        client = build_llm_client_from_values(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            backend=self.backend,
            timeout_seconds=self.timeout_seconds,
            app_type=self.app_type,
        )
        return client.invoke_with_tools(messages, tools=tools, user=user)

    async def ainvoke_with_tools(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str = "waifu-standalone",
    ) -> LLMToolResponse:
        client = build_llm_client_from_values(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            backend=self.backend,
            timeout_seconds=self.timeout_seconds,
            app_type=self.app_type,
        )
        return await client.ainvoke_with_tools(messages, tools=tools, user=user)

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
