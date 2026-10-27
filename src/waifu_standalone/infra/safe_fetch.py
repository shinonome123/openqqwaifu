"""Fetching user-supplied URLs without reaching the bot's own network.

The web-fetch and summarize tools fetch URLs chosen by QQ users. Unguarded, they
can read the NapCat WebUI, this console, Docker-internal services or cloud
metadata (169.254.169.254). Every hop, redirects included, must resolve only to
public addresses, and the body is read up to a size cap.

Remaining gap: the address is checked before httpx connects and httpx resolves
the name again, so a DNS-rebinding host can still slip through between the two.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from .http_transport import TransportError

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class BlockedUrlError(TransportError):
    """The URL, or a redirect it leads to, points at a non-public address."""


@dataclass(slots=True)
class FetchedPage:
    url: str
    status_code: int
    text: str
    headers: dict[str, str]
    truncated: bool = False
    # The raw body, for callers that want bytes (pictures) rather than text.
    content: bytes = b""


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # is_global is False for private, loopback, link-local, CGNAT, unspecified and reserved ranges.
    return address.is_global and not address.is_multicast


def _resolve_addresses(host: str, port: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise TransportError(f"无法解析域名：{host}") from exc
    return {str(info[4][0]) for info in infos}


def check_public_url(url: str) -> None:
    """Raise ``BlockedUrlError`` unless ``url`` is http(s) and its host resolves only to public addresses."""
    parts = urlsplit(str(url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise BlockedUrlError(f"只支持 http 或 https 链接：{url}")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedUrlError(f"链接里没有主机名：{url}")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise BlockedUrlError(f"链接端口无效：{url}") from exc
    # Resolving (rather than pattern-matching the host) also catches 127.1,
    # 2130706433, ::ffff:127.0.0.1 and internal names such as "napcat".
    addresses = _resolve_addresses(host, port)
    if not addresses or not all(is_public_address(address) for address in addresses):
        raise BlockedUrlError(f"不能访问内网或本机地址：{host}")


def fetch_public_url(
    url: str,
    *,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    transport: httpx.BaseTransport | None = None,
) -> FetchedPage:
    """GET ``url``, re-checking every redirect hop and reading at most ``max_bytes`` of body.

    ``transport`` is for tests (``httpx.MockTransport``).
    """
    current = str(url or "").strip()
    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        transport=transport,
    ) as client:
        for _ in range(max(0, int(max_redirects)) + 1):
            check_public_url(current)
            try:
                with client.stream("GET", current, headers=headers) as response:
                    location = response.headers.get("location", "")
                    if response.status_code in _REDIRECT_STATUSES and location:
                        current = urljoin(str(response.url), location)
                        continue
                    if response.status_code >= 400:
                        raise TransportError(
                            f"http {response.status_code}",
                            status_code=response.status_code,
                        )
                    body, truncated = _read_capped(response, max_bytes)
                    return FetchedPage(
                        url=str(response.url),
                        status_code=response.status_code,
                        text=_decode(body, response.charset_encoding),
                        headers={str(key): str(value) for key, value in response.headers.items()},
                        truncated=truncated,
                        content=body,
                    )
            except httpx.HTTPError as exc:
                raise TransportError(f"request failed: {exc}") from exc
    raise TransportError(f"重定向次数过多：{url}")


async def afetch_public_url(
    url: str,
    *,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> FetchedPage:
    # Name resolution is blocking; run the whole guarded fetch off the event loop.
    return await asyncio.to_thread(
        fetch_public_url,
        url,
        timeout_seconds=timeout_seconds,
        headers=headers,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
    )


def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    limit = max(1, int(max_bytes))
    chunks: list[bytes] = []
    size = 0
    # iter_bytes decompresses as it goes, so a gzip bomb also stops at the cap.
    for chunk in response.iter_bytes():
        if size + len(chunk) > limit:
            chunks.append(chunk[: limit - size])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


def _decode(body: bytes, encoding: str | None) -> str:
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
