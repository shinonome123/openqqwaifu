from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from ..models import OutboundMessage


class OneBotActionError(RuntimeError):
    pass


@dataclass(slots=True)
class OneBotActionClient:
    base_url: str
    timeout: float = 10.0
    access_token: str = ""

    def post_action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        base = self.base_url.rstrip("/")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(
            f"{base}/{action}",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8")
        result = json.loads(raw) if raw else {"status": "ok"}
        if result.get("status") not in {None, "ok"} or result.get("retcode") not in {None, 0}:
            raise OneBotActionError("onebot action failed")
        return result

    def get_status(self) -> dict[str, object]:
        return self.post_action("get_status", {})

    def get_login_info(self) -> dict[str, object]:
        return self.post_action("get_login_info", {})

    def get_version_info(self) -> dict[str, object]:
        return self.post_action("get_version_info", {})


class OneBotHttpOutboundPort:
    def __init__(self, client: OneBotActionClient):
        self.client = client

    def send(self, message: OutboundMessage) -> None:
        action = "send_group_msg" if message.launcher_type == "group" else "send_private_msg"
        target_key = "group_id" if message.launcher_type == "group" else "user_id"
        payload: dict[str, object] = {
            target_key: self._coerce_target_id(message.launcher_id),
            "message": self._build_message_segments(message),
        }
        self.client.post_action(action, payload)

    def _build_message_segments(self, message: OutboundMessage) -> list[dict[str, object]]:
        segments: list[dict[str, object]] = []
        if message.text:
            segments.append({"type": "text", "data": {"text": message.text}})
        for image in message.images:
            segments.append({"type": "image", "data": {"file": image}})
        return segments

    def _coerce_target_id(self, launcher_id: str) -> int | str:
        try:
            return int(launcher_id)
        except ValueError:
            return launcher_id
