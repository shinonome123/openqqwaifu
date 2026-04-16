from __future__ import annotations

from dataclasses import dataclass
import asyncio
import threading
from typing import Any

import httpx


@dataclass(slots=True)
class HttpResponse:
    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]


class TransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SyncHttpTransport:
    def __init__(self, *, timeout_seconds: float, max_connections: int = 32, max_keepalive_connections: int = 16):
        timeout = httpx.Timeout(timeout_seconds)
        limits = httpx.Limits(
            max_connections=max(1, int(max_connections)),
            max_keepalive_connections=max(1, int(max_keepalive_connections)),
        )
        self._client = httpx.Client(timeout=timeout, limits=limits, follow_redirects=True)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> HttpResponse:
        try:
            response = self._client.request(
                method=method,
                url=url,
                headers=headers,
                content=content,
                json=json_payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:160]
            raise TransportError(
                f"http {exc.response.status_code}: {body}",
                status_code=exc.response.status_code,
                body=body,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"request failed: {exc}") from exc
        return HttpResponse(
            status_code=response.status_code,
            text=response.text,
            content=response.content,
            headers={str(key): str(value) for key, value in response.headers.items()},
        )

    def close(self) -> None:
        self._client.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class AsyncHttpTransport:
    def __init__(self, *, timeout_seconds: float, max_connections: int = 32, max_keepalive_connections: int = 16):
        timeout = httpx.Timeout(timeout_seconds)
        limits = httpx.Limits(
            max_connections=max(1, int(max_connections)),
            max_keepalive_connections=max(1, int(max_keepalive_connections)),
        )
        self._client = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> HttpResponse:
        try:
            response = await self._client.request(
                method=method,
                url=url,
                headers=headers,
                content=content,
                json=json_payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:160]
            raise TransportError(
                f"http {exc.response.status_code}: {body}",
                status_code=exc.response.status_code,
                body=body,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"request failed: {exc}") from exc
        return HttpResponse(
            status_code=response.status_code,
            text=response.text,
            content=response.content,
            headers={str(key): str(value) for key, value in response.headers.items()},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def close(self) -> None:
        if self._client.is_closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._client.aclose())
            return
        loop.create_task(self._client.aclose())

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class AsyncRuntime:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="openqqwaifu-async", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)
