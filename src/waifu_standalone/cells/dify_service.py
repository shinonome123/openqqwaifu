from __future__ import annotations

import json
import urllib.error
import urllib.request


class DifyChatError(RuntimeError):
    pass


class DifyChatClient:
    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 45.0, app_type: str = "chat"):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = float(timeout_seconds or 45.0)
        self.app_type = str(app_type or "chat").strip() or "chat"

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.app_type == "chat")

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise DifyChatError("dify chat client is not configured")

        payload = {
            "inputs": {},
            "query": str(query or ""),
            "user": user,
            "response_mode": "blocking",
            "conversation_id": "",
            "files": [],
            "model_config": {},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat-messages",
            data=body,
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
            raise DifyChatError(f"dify request failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise DifyChatError(f"dify request failed: {exc.reason}") from exc

        data = json.loads(raw) if raw else {}
        answer = str(data.get("answer", "") or "").strip()
        if not answer:
            raise DifyChatError(f"dify returned no answer: {data}")
        return answer
