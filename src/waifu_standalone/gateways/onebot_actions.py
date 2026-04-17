from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any

from ..http_transport import AsyncHttpTransport, SyncHttpTransport, TransportError
from ..models import OutboundMessage
from ..observability import TransportMetricsScope


class OneBotActionError(RuntimeError):
    pass


def ensure_onebot_action_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") not in {None, "ok"} or result.get("retcode") not in {None, 0}:
        raise OneBotActionError("onebot action failed")
    return result


def build_onebot_action(message: OutboundMessage) -> tuple[str, dict[str, object]]:
    action = "send_group_msg" if message.launcher_type == "group" else "send_private_msg"
    target_key = "group_id" if message.launcher_type == "group" else "user_id"
    payload: dict[str, object] = {
        target_key: _coerce_target_id(message.launcher_id),
        "message": build_onebot_message_segments(message),
    }
    return action, payload


def build_onebot_message_segments(message: OutboundMessage) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    if message.text:
        segments.append({"type": "text", "data": {"text": message.text}})
    for image in message.images:
        segments.append({"type": "image", "data": {"file": image}})
    return segments


@dataclass(slots=True)
class OneBotActionClient:
    base_url: str
    timeout: float = 10.0
    access_token: str = ""
    _transport: SyncHttpTransport = field(init=False, repr=False)
    _async_transport: AsyncHttpTransport = field(init=False, repr=False)

    def __post_init__(self) -> None:
        scope = TransportMetricsScope(kind="sidecar", target="onebot_http")
        self._transport = SyncHttpTransport(timeout_seconds=self.timeout, metrics_scope=scope)
        self._async_transport = AsyncHttpTransport(timeout_seconds=self.timeout, metrics_scope=scope)

    def post_action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        base = self.base_url.rstrip("/")
        headers = self._headers()
        try:
            raw = self._transport.request(
                "POST",
                f"{base}/{action}",
                headers=headers,
                json_payload=payload,
            ).text
        except TransportError as exc:
            raise OneBotActionError(str(exc)) from exc
        result = json.loads(raw) if raw else {"status": "ok"}
        return ensure_onebot_action_result(result)

    async def apost_action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        base = self.base_url.rstrip("/")
        headers = self._headers()
        try:
            raw = (
                await self._async_transport.request(
                    "POST",
                    f"{base}/{action}",
                    headers=headers,
                    json_payload=payload,
                )
            ).text
        except TransportError as exc:
            raise OneBotActionError(str(exc)) from exc
        result = json.loads(raw) if raw else {"status": "ok"}
        return ensure_onebot_action_result(result)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def get_status(self) -> dict[str, object]:
        return self.post_action("get_status", {})

    def get_login_info(self) -> dict[str, object]:
        return self.post_action("get_login_info", {})

    def get_version_info(self) -> dict[str, object]:
        return self.post_action("get_version_info", {})

    def get_group_member_list(self, group_id: int | str) -> dict[str, object]:
        return self.post_action("get_group_member_list", {"group_id": _coerce_target_id(group_id)})

    async def aget_group_member_list(self, group_id: int | str) -> dict[str, object]:
        return await self.apost_action("get_group_member_list", {"group_id": _coerce_target_id(group_id)})

    def get_group_member_info(self, group_id: int | str, user_id: int | str, *, no_cache: bool = False) -> dict[str, object]:
        return self.post_action(
            "get_group_member_info",
            {
                "group_id": _coerce_target_id(group_id),
                "user_id": _coerce_target_id(user_id),
                "no_cache": bool(no_cache),
            },
        )

    async def aget_group_member_info(
        self,
        group_id: int | str,
        user_id: int | str,
        *,
        no_cache: bool = False,
    ) -> dict[str, object]:
        return await self.apost_action(
            "get_group_member_info",
            {
                "group_id": _coerce_target_id(group_id),
                "user_id": _coerce_target_id(user_id),
                "no_cache": bool(no_cache),
            },
        )

    def close(self) -> None:
        self._transport.close()
        self._async_transport.close()


class OneBotHttpOutboundPort:
    def __init__(self, client: OneBotActionClient):
        self.client = client

    def send(self, message: OutboundMessage) -> None:
        action, payload = build_onebot_action(message)
        self.client.post_action(action, payload)

    async def send_async(self, message: OutboundMessage) -> None:
        action, payload = build_onebot_action(message)
        await self.client.apost_action(action, payload)

    def _coerce_target_id(self, launcher_id: str) -> int | str:
        return _coerce_target_id(launcher_id)

    def close(self) -> None:
        self.client.close()


def _coerce_target_id(value: int | str) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)
