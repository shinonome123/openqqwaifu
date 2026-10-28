from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .http_transport import AsyncHttpTransport, SyncHttpTransport, TransportError
from .observability import TransportMetricsScope

_LOGGER = logging.getLogger(__name__)


def _worth_one_retry(exc: TransportError) -> bool:
    """A connection that broke off before any answer; not a timeout or an HTTP error."""
    return exc.status_code is None and not exc.timed_out


class LLMClientError(RuntimeError):
    pass


class UnsupportedMessageRoles(LLMClientError):
    pass


class IncompleteModelResponse(LLMClientError):
    pass


def _validate_message_options(max_output_tokens: int, instruction_role: str, output_token_parameter: str):
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError("max_output_tokens must be a positive integer")
    if instruction_role not in {"system", "developer"}:
        raise ValueError("instruction_role must be system or developer")
    if output_token_parameter not in {"max_completion_tokens", "max_tokens"}:
        raise ValueError("unsupported output token parameter")


def _validate_conversation_messages(messages):
    if not messages or messages[-1].get("role") != "user":
        raise ValueError("a conversation request must end with the current user input")
    for message in messages:
        if message.get("role") not in {"system", "user", "assistant"} or message.get("tool_calls"):
            raise ValueError("this conversation interface does not accept tools or unknown roles")


def normalize_image_reference(value: str) -> str:
    """Normalize OneBot image bytes/URLs without fetching remote content."""
    value = str(value or "").strip()
    if value.startswith(("https://", "http://")):
        try:
            if urlsplit(value).netloc:
                return value
        except ValueError as exc:
            raise LLMClientError("invalid image URL") from exc
        raise LLMClientError("invalid image URL")
    media_type = ""
    encoded = value.removeprefix("base64://")
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or not header.endswith(";base64"):
            raise LLMClientError("image data must use base64 encoding")
        media_type = header[5:-7].lower()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LLMClientError("unsupported image reference") from exc
    if not media_type:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            media_type = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            media_type = "image/webp"
    if media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"} or not data:
        raise LLMClientError("unsupported image format")
    return f"data:{media_type};base64,{encoded}"


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
    def supports_message_roles(self) -> bool:
        ...

    @property
    def supports_json_object(self) -> bool:
        ...

    async def ainvoke_messages(self, messages: list[dict[str, object]], *, max_output_tokens: int,
                               user: str = "waifu-standalone", instruction_role: str = "system",
                               output_token_parameter: str = "max_completion_tokens", json_object: bool = False) -> str:
        ...

    @property
    def supports_images(self) -> bool:
        ...

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
    def supports_message_roles(self) -> bool:
        return False

    @property
    def supports_json_object(self) -> bool:
        return False

    async def ainvoke_messages(self, messages: list[dict[str, object]], *, max_output_tokens: int,
                               user: str = "waifu-standalone", instruction_role: str = "system",
                               output_token_parameter: str = "max_completion_tokens", json_object: bool = False) -> str:
        raise UnsupportedMessageRoles(f"{self.backend} cannot preserve conversation roles")

    @property
    def supports_images(self) -> bool:
        return False

    @property
    def supports_tool_calling(self) -> bool:
        return False

    def _post_json(self, url: str, payload: dict[str, object], *, headers: dict[str, str]) -> str:
        for attempt in (1, 2):
            try:
                return self._transport.request(
                    "POST",
                    url,
                    headers=headers,
                    json_payload=payload,
                ).text
            except TransportError as exc:
                if attempt == 1 and _worth_one_retry(exc):
                    _LOGGER.warning("llm request broke off, retrying once: %s", exc)
                    continue
                raise LLMClientError(f"llm request failed: {exc}") from exc
        raise AssertionError("unreachable")

    async def _apost_json(self, url: str, payload: dict[str, object], *, headers: dict[str, str]) -> str:
        for attempt in (1, 2):
            try:
                response = await self._async_transport.request(
                    "POST",
                    url,
                    headers=headers,
                    json_payload=payload,
                )
                return response.text
            except TransportError as exc:
                if attempt == 1 and _worth_one_retry(exc):
                    _LOGGER.warning("llm request broke off, retrying once: %s", exc)
                    continue
                raise LLMClientError(f"llm request failed: {exc}") from exc
        raise AssertionError("unreachable")

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
    def _user_content_blocks(content: object) -> str | list[dict[str, object]]:
        if not isinstance(content, list):
            return _BaseHTTPClient._coerce_message_content(content)
        blocks: list[dict[str, object]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_url":
                image = item.get("image_url", {})
                reference = image.get("url", "") if isinstance(image, dict) else image
                blocks.append({"type": "image_url", "image_url": {"url": normalize_image_reference(str(reference))}})
            elif item.get("type") == "text":
                blocks.append({"type": "text", "text": str(item.get("text", "") or "")})
        return blocks

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
    @property
    def supports_json_object(self) -> bool:
        return True

    @property
    def supports_message_roles(self) -> bool:
        return True

    async def ainvoke_messages(self, messages: list[dict[str, object]], *, max_output_tokens: int,
                               user: str = "waifu-standalone", instruction_role: str = "system",
                               output_token_parameter: str = "max_completion_tokens", json_object: bool = False) -> str:
        _validate_message_options(max_output_tokens, instruction_role, output_token_parameter)
        _validate_conversation_messages(messages)
        if not self.enabled:
            raise LLMClientError("openai-compatible client is not configured")
        payload = self._tool_payload(messages, tools=[], user=user)
        payload[output_token_parameter] = max_output_tokens
        if type(json_object) is not bool:
            raise ValueError("json_object must be a boolean")
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        if instruction_role == "developer":
            for message in payload["messages"]:
                if message["role"] == "system":
                    message["role"] = "developer"
        raw = await self._apost_json(self._endpoint("chat/completions"), payload, headers=self._headers())
        response = self._parse_tool_response(raw)
        data = json.loads(raw)
        reason = data["choices"][0].get("finish_reason")
        if reason in {"length", "content_filter", "tool_calls", "function_call"} or response.tool_calls or not response.text.strip():
            raise IncompleteModelResponse("model did not return a complete plain-text reply")
        return response.text

    @property
    def supports_images(self) -> bool:
        return True

    def __init__(self, *, base_url: str, api_key: str, model: str = "", backend: str = "openai", timeout_seconds: float = 45.0, app_type: str = "chat", reasoning_effort: str = ""):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            backend=backend or "openai",
            timeout_seconds=timeout_seconds,
            app_type=app_type,
        )
        self.reasoning_effort = str(reasoning_effort or "").strip().lower()

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
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [{"role": "user", "content": str(query or "")}],
            "user": user,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def _tool_payload(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        user: str,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
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
        if not tools:
            payload.pop("tools")
            payload.pop("tool_choice")
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload
 
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
            payload["content"] = self._user_content_blocks(content) if role == "user" else self._coerce_message_content(content)
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
        sampling: dict[str, float] | None = None,
    ) -> LLMToolResponse:
        """``sampling`` (temperature, top_p, max_tokens, penalties) goes into the request as it is."""
        if not self.supports_tool_calling:
            raise LLMClientError("openai-compatible client is not configured")
        payload = self._tool_payload(messages, tools=tools, user=user)
        payload.update(sampling or {})
        raw = await self._apost_json(self._endpoint("chat/completions"), payload, headers=self._headers())
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
    @property
    def supports_message_roles(self) -> bool:
        return True

    async def ainvoke_messages(self, messages: list[dict[str, object]], *, max_output_tokens: int,
                               user: str = "waifu-standalone", instruction_role: str = "system",
                               output_token_parameter: str = "max_completion_tokens", json_object: bool = False) -> str:
        _validate_message_options(max_output_tokens, instruction_role, output_token_parameter)
        if json_object:
            raise ValueError("this Claude adapter does not support JSON object response_format")
        _validate_conversation_messages(messages)
        if not self.enabled:
            raise LLMClientError("claude client is not configured")
        payload = self._tool_payload(messages, tools=[], user=user)
        payload["max_tokens"] = max_output_tokens
        raw = await self._apost_json(self._endpoint("messages", add_v1=True), payload, headers=self._headers())
        response = self._parse_tool_response(raw)
        if (json.loads(raw).get("stop_reason") in {"max_tokens", "tool_use", "pause_turn"}
                or response.tool_calls or not response.text.strip()):
            raise IncompleteModelResponse("model did not return a complete plain-text reply")
        return response.text

    @property
    def supports_images(self) -> bool:
        return True

    def _user_content(self, content: object) -> str | list[dict[str, object]]:
        blocks = self._user_content_blocks(content)
        if isinstance(blocks, str):
            return blocks
        converted: list[dict[str, object]] = []
        for block in blocks:
            if block["type"] == "text":
                converted.append(block)
                continue
            reference = str(block["image_url"]["url"])
            if reference.startswith("data:"):
                header, _, data = reference.partition(",")
                source = {"type": "base64", "media_type": header[5:-7], "data": data}
            else:
                source = {"type": "url", "url": reference}
            converted.append({"type": "image", "source": source})
        return converted

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
                        "content": self._user_content(message.get("content")),
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
        if not tools:
            payload.pop("tools")
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
    def supports_message_roles(self) -> bool:
        return str(self.backend or "dify").strip().lower() != "dify"

    @property
    def supports_json_object(self) -> bool:
        return str(self.backend or "dify").strip().lower() not in {"dify", "claude"}

    async def ainvoke_messages(self, messages: list[dict[str, object]], *, max_output_tokens: int,
                               user: str = "waifu-standalone", instruction_role: str = "system",
                               output_token_parameter: str = "max_completion_tokens", json_object: bool = False) -> str:
        client = build_llm_client_from_values(base_url=self.base_url, api_key=self.api_key, model=self.model,
            backend=self.backend, timeout_seconds=self.timeout_seconds, app_type=self.app_type)
        try:
            return await client.ainvoke_messages(messages, max_output_tokens=max_output_tokens, user=user,
                instruction_role=instruction_role, output_token_parameter=output_token_parameter, json_object=json_object)
        finally:
            client.close()

    @property
    def supports_images(self) -> bool:
        return str(self.backend or "dify").strip().lower() != "dify"

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
    reasoning_effort: str = "",
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
            reasoning_effort=reasoning_effort,
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
        reasoning_effort=str(getattr(llm_config, "reasoning_effort", "") or ""),
    )
