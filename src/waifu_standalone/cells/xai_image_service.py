from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request


class XAIImageError(RuntimeError):
    pass


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

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def generate(self, prompt: str) -> str:
        if not self.enabled:
            raise XAIImageError("xai image client is not configured")

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

        request = urllib.request.Request(
            f"{self.base_url}/images/generations",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise XAIImageError(f"xai image request failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise XAIImageError(f"xai image request failed: {exc.reason}") from exc

        data = json.loads(raw) if raw else {}
        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            raise XAIImageError(f"xai image response missing data: {data}")

        first = items[0] if isinstance(items[0], dict) else {}
        base64_data = str(first.get("b64_json", "") or "").strip()
        if base64_data:
            return f"base64://{base64_data}"

        image_url = str(first.get("url", "") or "").strip()
        if image_url:
            return image_url

        raise XAIImageError(f"xai image response missing payload: {data}")

    def resolve_image(self, image_ref: str) -> tuple[bytes, str]:
        payload = str(image_ref or "").strip()
        if not payload:
            raise XAIImageError("image reference is empty")
        if payload.startswith("base64://"):
            image_bytes = self.decode_base64_image(payload)
            return image_bytes, _detect_content_type(image_bytes)

        request = urllib.request.Request(payload, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                image_bytes = response.read()
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise XAIImageError(f"image download failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise XAIImageError(f"image download failed: {exc.reason}") from exc

        return image_bytes, content_type or _detect_content_type(image_bytes)

    @staticmethod
    def decode_base64_image(image_ref: str) -> bytes:
        payload = str(image_ref or "")
        if payload.startswith("base64://"):
            payload = payload[len("base64://") :]
        return base64.b64decode(payload)


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
