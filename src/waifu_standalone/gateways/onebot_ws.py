from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

from ..contracts import OutboundPort
from ..gateways.onebot_actions import build_onebot_action, ensure_onebot_action_result
from ..models import OutboundMessage

_LOGGER = logging.getLogger(__name__)


class OneBotWsGateway:
    def __init__(
        self,
        *,
        event_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        access_token: str = "",
        send_timeout_seconds: float = 15.0,
    ) -> None:
        self._event_handler = event_handler
        self._access_token = str(access_token or "").strip()
        self._send_timeout = float(send_timeout_seconds)
        self._ws_lock = asyncio.Lock()
        self._active_ws: web.WebSocketResponse | None = None
        self._pending_echos: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def set_event_handler(self, event_handler: Callable[[dict[str, Any]], Awaitable[None]] | None) -> None:
        self._event_handler = event_handler

    def configure(self, *, access_token: str | None = None, send_timeout_seconds: float | None = None) -> None:
        if access_token is not None:
            self._access_token = str(access_token or "").strip()
        if send_timeout_seconds is not None:
            self._send_timeout = float(send_timeout_seconds)

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        self._authorize(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await self._activate(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    _LOGGER.warning("invalid reverse-WS payload")
                    continue
                if not isinstance(payload, dict):
                    continue
                if "post_type" in payload:
                    self._spawn_event_task(payload)
                    continue
                echo = str(payload.get("echo") or "").strip()
                if echo:
                    future = self._pending_echos.get(echo)
                    if future is not None and not future.done():
                        future.set_result(payload)
        finally:
            await self._deactivate(ws)
        return ws

    async def send_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        echo = ""
        async with self._ws_lock:
            ws = self._active_ws
            if ws is None or ws.closed:
                raise ConnectionError("no active reverse-WS connection")
            echo = str(uuid.uuid4())
            self._pending_echos[echo] = future
            await ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        try:
            return await asyncio.wait_for(future, timeout=self._send_timeout)
        finally:
            self._pending_echos.pop(echo, None)

    async def close(self) -> None:
        async with self._ws_lock:
            ws = self._active_ws
            self._active_ws = None
            self._cancel_pending()
        if ws is not None and not ws.closed:
            await ws.close(code=1001, message=b"server shutdown")

    def _authorize(self, request: web.Request) -> None:
        if not self._access_token:
            return
        header = str(request.headers.get("Authorization", "") or "").strip()
        expected = f"Bearer {self._access_token}"
        if header != expected:
            raise web.HTTPUnauthorized(
                text=json.dumps({"status": "unauthorized"}),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )

    async def _activate(self, ws: web.WebSocketResponse) -> None:
        async with self._ws_lock:
            previous = self._active_ws
            self._active_ws = ws
            self._cancel_pending()
        if previous is not None and not previous.closed:
            await previous.close(code=1012, message=b"superseded")

    async def _deactivate(self, ws: web.WebSocketResponse) -> None:
        async with self._ws_lock:
            if self._active_ws is ws:
                self._active_ws = None
                self._cancel_pending()

    def _cancel_pending(self) -> None:
        pending = list(self._pending_echos.values())
        self._pending_echos.clear()
        for future in pending:
            if not future.done():
                future.cancel()

    def _spawn_event_task(self, payload: dict[str, Any]) -> None:
        if self._event_handler is None:
            _LOGGER.warning("reverse-WS event received before event handler was configured")
            return
        task = asyncio.create_task(self._event_handler(payload))
        task.add_done_callback(self._log_task_failure)

    @staticmethod
    def _log_task_failure(task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            _LOGGER.exception("reverse-WS event handler failed")


class OneBotWsActionClient:
    def __init__(self, gateway: OneBotWsGateway) -> None:
        self._gateway = gateway

    def get_group_member_list(self, group_id: int | str) -> dict[str, Any]:
        from ..app import _ASYNC_RUNTIME

        return _ASYNC_RUNTIME.submit(self.aget_group_member_list(group_id)).result()

    async def aget_group_member_list(self, group_id: int | str) -> dict[str, Any]:
        response = await self._gateway.send_action("get_group_member_list", {"group_id": _coerce_target_id(group_id)})
        return ensure_onebot_action_result(response)

    def get_group_member_info(self, group_id: int | str, user_id: int | str, *, no_cache: bool = False) -> dict[str, Any]:
        from ..app import _ASYNC_RUNTIME

        return _ASYNC_RUNTIME.submit(
            self.aget_group_member_info(group_id, user_id, no_cache=no_cache)
        ).result()

    async def aget_group_member_info(
        self,
        group_id: int | str,
        user_id: int | str,
        *,
        no_cache: bool = False,
    ) -> dict[str, Any]:
        response = await self._gateway.send_action(
            "get_group_member_info",
            {
                "group_id": _coerce_target_id(group_id),
                "user_id": _coerce_target_id(user_id),
                "no_cache": bool(no_cache),
            },
        )
        return ensure_onebot_action_result(response)


class OneBotWsOutboundPort(OutboundPort):
    def __init__(self, gateway: OneBotWsGateway) -> None:
        self._gateway = gateway

    def send(self, message: OutboundMessage) -> None:
        from ..app import _ASYNC_RUNTIME

        _ASYNC_RUNTIME.submit(self.send_async(message)).result()

    async def send_async(self, message: OutboundMessage) -> None:
        action, params = build_onebot_action(message)
        response = await self._gateway.send_action(action, params)
        ensure_onebot_action_result(response)

    def close(self) -> None:
        from ..app import _ASYNC_RUNTIME

        _ASYNC_RUNTIME.submit(self.aclose()).result()

    async def aclose(self) -> None:
        await self._gateway.close()


def _coerce_target_id(value: int | str) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)
