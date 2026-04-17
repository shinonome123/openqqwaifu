from __future__ import annotations

import base64
import json
from typing import Protocol, runtime_checkable

from ..config import AppConfig
from ..http_transport import AsyncHttpTransport, SyncHttpTransport, TransportError
from ..observability import TransportMetricsScope


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
        return self._parse_generation(self._request_generation(prompt))

    async def agenerate(self, prompt: str) -> str:
        if not self.enabled:
            raise ImageClientError("xai image client is not configured")
        return self._parse_generation(await self._arequest_generation(prompt))

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

    def _request_generation(self, prompt: str) -> str:
        try:
            return self._transport.request(
                "POST",
                f"{self.base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json_payload=self._payload(prompt),
            ).text
        except TransportError as exc:
            raise ImageClientError(f"xai image request failed: {exc}") from exc

    async def _arequest_generation(self, prompt: str) -> str:
        try:
            response = await self._async_transport.request(
                "POST",
                f"{self.base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json_payload=self._payload(prompt),
            )
            return response.text
        except TransportError as exc:
            raise ImageClientError(f"xai image request failed: {exc}") from exc

    def _parse_generation(self, raw: str) -> str:
        data = json.loads(raw) if raw else {}
        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            raise ImageClientError(f"xai image response missing data: {data}")

        first = items[0] if isinstance(items[0], dict) else {}
        base64_data = str(first.get("b64_json", "") or "").strip()
        if base64_data:
            return f"base64://{base64_data}"

        image_url = str(first.get("url", "") or "").strip()
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
        return base64.b64decode(payload)

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
