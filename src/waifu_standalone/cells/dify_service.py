from __future__ import annotations

import json
import urllib.error
import urllib.request


class DifyChatError(RuntimeError):
    pass


class DifyChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "",
        backend: str = "dify",
        timeout_seconds: float = 45.0,
        app_type: str = "chat",
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.backend = str(backend or "dify").strip().lower() or "dify"
        self.timeout_seconds = float(timeout_seconds or 45.0)
        self.app_type = str(app_type or "chat").strip() or "chat"

    @property
    def enabled(self) -> bool:
        if not (self.base_url and self.api_key):
            return False
        if self.backend == "dify":
            return self.app_type == "chat"
        return bool(self.model)

    def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
        if not self.enabled:
            raise DifyChatError("dify chat client is not configured")

        if self.backend == "dify":
            return self._invoke_dify(query, user=user)
        if self.backend == "claude":
            return self._invoke_claude(query, user=user)
        return self._invoke_openai_compatible(query, user=user)

    def _invoke_dify(self, query: str, *, user: str) -> str:
        payload = {
            "inputs": {},
            "query": str(query or ""),
            "user": user,
            "response_mode": "blocking",
            "conversation_id": "",
            "files": [],
            "model_config": {"model": self.model} if self.model else {},
        }
        raw = self._post_json(
            self._endpoint("chat-messages"),
            payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        data = json.loads(raw) if raw else {}
        answer = str(data.get("answer", "") or "").strip()
        if not answer:
            raise DifyChatError(f"dify returned no answer: {data}")
        return answer

    def _invoke_openai_compatible(self, query: str, *, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": str(query or ""),
                }
            ],
            "user": user,
        }
        raw = self._post_json(
            self._endpoint("chat/completions"),
            payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        data = json.loads(raw) if raw else {}
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise DifyChatError(f"llm returned no choices: {data}")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        answer = self._extract_content_text(message.get("content"))
        if not answer:
            raise DifyChatError(f"llm returned empty content: {data}")
        return answer

    def _invoke_claude(self, query: str, *, user: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "metadata": {"user_id": user},
            "messages": [
                {
                    "role": "user",
                    "content": str(query or ""),
                }
            ],
        }
        raw = self._post_json(
            self._endpoint("messages", add_v1=True),
            payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        data = json.loads(raw) if raw else {}
        answer = self._extract_content_text(data.get("content"))
        if not answer:
            raise DifyChatError(f"claude returned empty content: {data}")
        return answer

    def _post_json(self, url: str, payload: dict[str, object], *, headers: dict[str, str]) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise DifyChatError(f"llm request failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise DifyChatError(f"llm request failed: {exc.reason}") from exc

    def _endpoint(self, suffix: str, *, add_v1: bool = False) -> str:
        normalized = str(suffix or "").lstrip("/")
        if not normalized:
            return self.base_url
        if self.base_url.endswith(normalized):
            return self.base_url
        base = self.base_url
        if add_v1 and not base.endswith("/v1") and not base.endswith("/v1/"):
            base = f"{base}/v1"
        return f"{base.rstrip('/')}/{normalized}"

    @staticmethod
    def _extract_content_text(content: object) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            blocks: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = str(item.get("text", "") or "").strip()
                    if text:
                        blocks.append(text)
            return "\n".join(blocks).strip()
        if isinstance(content, dict):
            text = str(content.get("text", "") or "").strip()
            if text:
                return text
        return ""
