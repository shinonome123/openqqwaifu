from __future__ import annotations

import base64
import json
from json import JSONDecodeError
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from ..config import AppConfig
from .http_transport import AsyncHttpTransport, HttpResponse, SyncHttpTransport, TransportError
from .observability import TransportMetricsScope


class ImageClientError(RuntimeError):
    pass


@runtime_checkable
class ImageClient(Protocol):
    enabled: bool

    def generate(self, prompt: str) -> str:
        ...

    async def agenerate(self, prompt: str) -> str:
        ...

    def resolve_image(self, image_ref: str) -> tuple[bytes, str]:
        ...

    async def aresolve_image(self, image_ref: str) -> tuple[bytes, str]:
        ...


class DisabledImageClient:
    enabled = False

    def generate(self, prompt: str) -> str:
        raise ImageClientError("image client is not configured")

    async def agenerate(self, prompt: str) -> str:
        raise ImageClientError("image client is not configured")

    def resolve_image(self, image_ref: str) -> tuple[bytes, str]:
        raise ImageClientError("image client is not configured")

    async def aresolve_image(self, image_ref: str) -> tuple[bytes, str]:
        raise ImageClientError("image client is not configured")

    def close(self) -> None:
        return None


class XAIImageClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 180.0,
        response_format: str = "b64_json",
        aspect_ratio: str = "1:1",
        resolution: str = "",
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout_seconds = float(timeout_seconds or 180.0)
        self.response_format = str(response_format or "b64_json").strip()
        self.aspect_ratio = str(aspect_ratio or "").strip()
        self.resolution = str(resolution or "").strip()
        scope = TransportMetricsScope(kind="image", target="xai")
        self._transport = SyncHttpTransport(timeout_seconds=self.timeout_seconds, metrics_scope=scope)
        self._async_transport = AsyncHttpTransport(timeout_seconds=self.timeout_seconds, metrics_scope=scope)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def generate(self, prompt: str) -> str:
        if not self.enabled:
            raise ImageClientError("xai image client is not configured")
        return self._generate_with_candidates(prompt)

    async def agenerate(self, prompt: str) -> str:
        if not self.enabled:
            raise ImageClientError("xai image client is not configured")
        return await self._agenerate_with_candidates(prompt)

    def _payload(self, prompt: str) -> dict[str, object]:
        payload = {
            "model": self.model,
            "prompt": str(prompt or "").strip(),
            "n": 1,
            "response_format": self.response_format or "b64_json",
        }
        if self.aspect_ratio:
            payload["aspect_ratio"] = self.aspect_ratio
        if self.resolution:
            payload["resolution"] = self.resolution
        return payload

    def _generate_with_candidates(self, prompt: str) -> str:
        last_error: ImageClientError | None = None
        candidates = self._api_base_candidates()
        for index, candidate in enumerate(candidates):
            response = self._request_generation(prompt, api_base=candidate)
            try:
                return self._parse_generation(
                    response.text,
                    content_type=str(response.headers.get("Content-Type") or ""),
                    request_url=f"{candidate}/images/generations",
                )
            except ImageClientError as exc:
                last_error = exc
                if index + 1 < len(candidates) and self._looks_like_html_response(response):
                    continue
                raise
        raise last_error or ImageClientError("xai image generation failed")

    async def _agenerate_with_candidates(self, prompt: str) -> str:
        last_error: ImageClientError | None = None
        candidates = self._api_base_candidates()
        for index, candidate in enumerate(candidates):
            response = await self._arequest_generation(prompt, api_base=candidate)
            try:
                return self._parse_generation(
                    response.text,
                    content_type=str(response.headers.get("Content-Type") or ""),
                    request_url=f"{candidate}/images/generations",
                )
            except ImageClientError as exc:
                last_error = exc
                if index + 1 < len(candidates) and self._looks_like_html_response(response):
                    continue
                raise
        raise last_error or ImageClientError("xai image generation failed")

    def _request_generation(self, prompt: str, *, api_base: str) -> HttpResponse:
        try:
            return self._transport.request(
                "POST",
                f"{api_base}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json_payload=self._payload(prompt),
            )
        except TransportError as exc:
            raise ImageClientError(f"xai image request failed: {exc}") from exc

    async def _arequest_generation(self, prompt: str, *, api_base: str) -> HttpResponse:
        try:
            return await self._async_transport.request(
                "POST",
                f"{api_base}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json_payload=self._payload(prompt),
            )
        except TransportError as exc:
            raise ImageClientError(f"xai image request failed: {exc}") from exc

    def _parse_generation(
        self,
        raw: str,
        *,
        content_type: str = "",
        request_url: str = "",
    ) -> str:
        body = str(raw or "")
        if self._looks_like_html_text(body, content_type=content_type):
            suggested = self._suggest_v1_base_url()
            hint = f"；这个服务看起来要求使用 {suggested}" if suggested else ""
            raise ImageClientError(
                f"image endpoint returned HTML instead of JSON: {request_url or self.base_url}/images/generations{hint}"
            )
        try:
            data = json.loads(body) if body else {}
        except JSONDecodeError as exc:
            snippet = body[:160].replace("\n", " ").strip()
            raise ImageClientError(
                f"image response is not valid JSON from {request_url or self.base_url}/images/generations: {snippet}"
            ) from exc
        items = data.get("data", []) if isinstance(data, dict) else []
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("code") or "").strip()
                if message:
                    raise ImageClientError(f"xai image response error: {message}")
            elif error:
                raise ImageClientError(f"xai image response error: {error}")
        if not items:
            raise ImageClientError(f"xai image response missing data: {data}")

        first = items[0] if isinstance(items[0], dict) else {}
        base64_data = self._normalize_base64_payload(
            first.get("b64_json")
            or first.get("base64")
            or first.get("image_base64")
            or first.get("image")
        )
        if base64_data:
            return f"base64://{base64_data}"

        image_url = str(first.get("url") or first.get("image_url") or first.get("uri") or "").strip()
        if image_url:
            return image_url

        raise ImageClientError(f"xai image response missing payload: {data}")

    def resolve_image(self, image_ref: str) -> tuple[bytes, str]:
        return self._resolve_image(image_ref)

    async def aresolve_image(self, image_ref: str) -> tuple[bytes, str]:
        return await self._aresolve_image(image_ref)

    def _resolve_image(self, image_ref: str) -> tuple[bytes, str]:
        payload = str(image_ref or "").strip()
        if not payload:
            raise ImageClientError("image reference is empty")
        if payload.startswith("base64://"):
            image_bytes = self.decode_base64_image(payload)
            return image_bytes, _detect_content_type(image_bytes)

        try:
            response = self._transport.request("GET", payload)
            image_bytes = response.content
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        except TransportError as exc:
            raise ImageClientError(f"image download failed: {exc}") from exc

        return image_bytes, content_type or _detect_content_type(image_bytes)

    async def _aresolve_image(self, image_ref: str) -> tuple[bytes, str]:
        payload = str(image_ref or "").strip()
        if not payload:
            raise ImageClientError("image reference is empty")
        if payload.startswith("base64://"):
            image_bytes = self.decode_base64_image(payload)
            return image_bytes, _detect_content_type(image_bytes)

        try:
            response = await self._async_transport.request("GET", payload)
            image_bytes = response.content
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        except TransportError as exc:
            raise ImageClientError(f"image download failed: {exc}") from exc

        return image_bytes, content_type or _detect_content_type(image_bytes)

    @staticmethod
    def decode_base64_image(image_ref: str) -> bytes:
        payload = str(image_ref or "")
        if payload.startswith("base64://"):
            payload = payload[len("base64://") :]
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]
        return base64.b64decode(payload)

    def _api_base_candidates(self) -> list[str]:
        base = self.base_url.rstrip("/")
        candidates = [base]
        suggested = self._suggest_v1_base_url()
        if suggested and suggested not in candidates:
            candidates.append(suggested)
        return candidates

    def _suggest_v1_base_url(self) -> str:
        base = self.base_url.rstrip("/")
        if not base:
            return ""
        parts = urlsplit(base)
        path = parts.path.rstrip("/")
        if path.endswith("/v1"):
            return ""
        if path:
            return ""
        return f"{base}/v1"

    @staticmethod
    def _looks_like_html_response(response: HttpResponse) -> bool:
        return XAIImageClient._looks_like_html_text(
            response.text,
            content_type=str(response.headers.get("Content-Type") or ""),
        )

    @staticmethod
    def _looks_like_html_text(raw: str, *, content_type: str = "") -> bool:
        compact = str(raw or "").lstrip().lower()
        ctype = str(content_type or "").lower()
        return "text/html" in ctype or compact.startswith("<!doctype html") or compact.startswith("<html")

    @staticmethod
    def _normalize_base64_payload(value: object) -> str:
        payload = str(value or "").strip()
        if not payload:
            return ""
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1].strip()
        return payload

    def close(self) -> None:
        self._transport.close()
        self._async_transport.close()


def build_image_client(config: AppConfig) -> ImageClient:
    image = config.image_generation
    if not bool(image.enabled):
        return DisabledImageClient()
    return XAIImageClient(
        base_url=image.base_url,
        api_key=image.api_key,
        model=image.model,
        timeout_seconds=image.timeout_seconds,
        response_format=image.response_format,
        aspect_ratio=image.aspect_ratio,
        resolution=image.resolution,
    )


def _detect_content_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/png"
