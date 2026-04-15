from __future__ import annotations

import json
import sys
import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.dify_service import DifyChatClient


class _LLMHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        record = {
            "path": self.path,
            "headers": dict(self.headers.items()),
            "payload": payload,
        }
        self.__class__.requests.append(record)
        if self.path.endswith("/chat-messages"):
            body = {"answer": "dify-ok"}
        elif self.path.endswith("/chat/completions"):
            body = {"choices": [{"message": {"content": "openai-ok"}}]}
        elif self.path.endswith("/messages"):
            body = {"content": [{"type": "text", "text": "claude-ok"}]}
        else:
            body = {"error": "unexpected"}
        raw = json.dumps(body).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        return


class DifyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        _LLMHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LLMHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_dify_request_carries_model_override(self) -> None:
        client = DifyChatClient(
            base_url=self.base_url,
            api_key="secret",
            model="waifu-grok",
            backend="dify",
            app_type="chat",
        )

        answer = client.invoke("hello")

        self.assertEqual(answer, "dify-ok")
        record = _LLMHandler.requests[-1]
        self.assertEqual(record["path"], "/chat-messages")
        self.assertEqual(record["payload"]["model_config"]["model"], "waifu-grok")

    def test_openai_compatible_request_uses_chat_completions(self) -> None:
        client = DifyChatClient(
            base_url=f"{self.base_url}/v1",
            api_key="secret",
            model="grok-3-mini",
            backend="openai",
        )

        answer = client.invoke("hello")

        self.assertEqual(answer, "openai-ok")
        record = _LLMHandler.requests[-1]
        self.assertEqual(record["path"], "/v1/chat/completions")
        self.assertEqual(record["payload"]["model"], "grok-3-mini")
        self.assertEqual(record["headers"]["Authorization"], "Bearer secret")

    def test_claude_request_uses_anthropic_headers(self) -> None:
        client = DifyChatClient(
            base_url=self.base_url,
            api_key="secret",
            model="claude-sonnet-4-0",
            backend="claude",
        )

        answer = client.invoke("hello")

        self.assertEqual(answer, "claude-ok")
        record = _LLMHandler.requests[-1]
        self.assertEqual(record["path"], "/v1/messages")
        self.assertEqual(record["payload"]["model"], "claude-sonnet-4-0")
        headers = {str(key).lower(): value for key, value in record["headers"].items()}
        self.assertEqual(headers["x-api-key"], "secret")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")


if __name__ == "__main__":
    unittest.main()
