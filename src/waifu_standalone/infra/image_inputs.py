"""Making inbound pictures readable for vision models.

QQ hands over CDN links, which model providers often fail to download; GIF
stickers, which xAI rejects; and photos of several megabytes. Each picture is
fetched here (public addresses only), its first frame taken, scaled down when
large, and sent inline as PNG, JPEG or WebP.
"""

from __future__ import annotations

import base64
import binascii
import io

from PIL import Image, UnidentifiedImageError

from .http_transport import TransportError
from .safe_fetch import fetch_public_url

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
# Longest side sent to the model: larger pictures cost more and read no better.
MAX_SIDE = 1536
# Pictures up to this size in a format every provider reads go through untouched.
_PASS_THROUGH_BYTES = 4 * 1024 * 1024
_PASS_THROUGH_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImageInputError(ValueError):
    """The picture could not be fetched or decoded."""


def prepare_image_input(reference: str, *, timeout_seconds: float = 15.0) -> str:
    """A data URL for one inbound picture given as a URL, base64:// or data: reference."""
    data = _load(str(reference or "").strip(), timeout_seconds=timeout_seconds)
    try:
        with Image.open(io.BytesIO(data)) as image:
            media_type = _PASS_THROUGH_FORMATS.get(str(image.format or "").upper(), "")
            if (
                media_type
                and not getattr(image, "is_animated", False)
                and len(data) <= _PASS_THROUGH_BYTES
                and max(image.size) <= MAX_SIDE
            ):
                return _data_url(media_type, data)
            image.seek(0)  # the first frame of a GIF or animated WebP
            transparent = image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info
            frame = image.convert("RGBA" if transparent else "RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageInputError(f"unreadable picture: {exc}") from exc
    frame.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    if frame.mode == "RGBA":
        frame.save(buffer, format="PNG", optimize=True)
        return _data_url("image/png", buffer.getvalue())
    frame.save(buffer, format="JPEG", quality=85)
    return _data_url("image/jpeg", buffer.getvalue())


def _load(reference: str, *, timeout_seconds: float) -> bytes:
    if reference.startswith(("https://", "http://")):
        try:
            page = fetch_public_url(reference, timeout_seconds=timeout_seconds, max_bytes=MAX_DOWNLOAD_BYTES)
        except TransportError as exc:
            raise ImageInputError(f"picture download failed: {exc}") from exc
        if page.truncated:
            raise ImageInputError("picture is larger than the download limit")
        data = page.content
    else:
        encoded = reference.removeprefix("base64://")
        if reference.startswith("data:"):
            header, separator, encoded = reference.partition(",")
            if not separator or not header.endswith(";base64"):
                raise ImageInputError("picture data must use base64 encoding")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageInputError("unsupported picture reference") from exc
    if not data:
        raise ImageInputError("empty picture")
    return data


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
