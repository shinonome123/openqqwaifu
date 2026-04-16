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

from waifu_standalone.cells.embedding_clients import build_embedding_client
from waifu_standalone.config import AppConfig


class _EmbeddingHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers.items()),
                "payload": payload,
            }
        )
        raw = json.dumps({"data": [{"embedding": [3.0, 4.0]}]}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        return


class EmbeddingClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _EmbeddingHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_build_embedding_client_posts_to_embeddings_endpoint(self) -> None:
        config = AppConfig()
        config.embedding.enabled = True
        config.embedding.base_url = self.base_url
        config.embedding.api_key = "secret"
        config.embedding.model = "text-embedding-3-small"

        client = build_embedding_client(config)
        vector = client.embed("hello world")

        self.assertAlmostEqual(vector[0], 0.6)
        self.assertAlmostEqual(vector[1], 0.8)
        record = _EmbeddingHandler.requests[-1]
        self.assertEqual(record["path"], "/v1/embeddings")
        self.assertEqual(record["payload"]["model"], "text-embedding-3-small")
        self.assertEqual(record["headers"]["Authorization"], "Bearer secret")

    def test_disabled_embedding_client_is_not_ready(self) -> None:
        client = build_embedding_client(AppConfig())

        self.assertFalse(client.ready)


if __name__ == "__main__":
    unittest.main()
