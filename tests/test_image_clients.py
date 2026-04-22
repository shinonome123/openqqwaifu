from __future__ import annotations

import base64
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

from waifu_standalone.cells.image_clients import build_image_client
from waifu_standalone.config import AppConfig

_PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepng"


class _ImageHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    generation_path = "/images/generations"
    generation_body = None

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
        if self.path != self.__class__.generation_path:
            self.send_response(HTTPStatus.OK)
            body = b"<!doctype html><html><body>homepage</body></html>"
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        image_payload = self.__class__.generation_body
        if image_payload is None:
            image_payload = base64.b64encode(_PNG_BYTES).decode("ascii")
        raw = json.dumps({"data": [{"b64_json": image_payload}]}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path != "/image.png":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(_PNG_BYTES)))
        self.end_headers()
        self.wfile.write(_PNG_BYTES)

    def log_message(self, format: str, *args: object) -> None:
        return


class ImageClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _ImageHandler.requests = []
        _ImageHandler.generation_path = "/images/generations"
        _ImageHandler.generation_body = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_build_image_client_posts_generation_request(self) -> None:
        config = AppConfig()
        config.image_generation.enabled = True
        config.image_generation.base_url = self.base_url
        config.image_generation.api_key = "secret"
        config.image_generation.model = "grok-imagine-image-pro"
        config.image_generation.aspect_ratio = "16:9"

        client = build_image_client(config)
        image_ref = client.generate("sunset")

        self.assertTrue(image_ref.startswith("base64://"))
        record = _ImageHandler.requests[-1]
        self.assertEqual(record["path"], "/images/generations")
        self.assertEqual(record["payload"]["model"], "grok-imagine-image-pro")
        self.assertEqual(record["payload"]["aspect_ratio"], "16:9")
        self.assertEqual(record["headers"]["Authorization"], "Bearer secret")

    def test_build_image_client_retries_v1_when_root_returns_html(self) -> None:
        _ImageHandler.generation_path = "/v1/images/generations"
        _ImageHandler.generation_body = f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode('ascii')}"
        config = AppConfig()
        config.image_generation.enabled = True
        config.image_generation.base_url = self.base_url
        config.image_generation.api_key = "secret"
        config.image_generation.model = "gpt-image-2"

        client = build_image_client(config)
        image_ref = client.generate("sunset")
        image_bytes, content_type = client.resolve_image(image_ref)

        self.assertTrue(image_ref.startswith("base64://"))
        self.assertNotIn("data:image", image_ref)
        self.assertEqual(image_bytes, _PNG_BYTES)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(_ImageHandler.requests[0]["path"], "/images/generations")
        self.assertEqual(_ImageHandler.requests[-1]["path"], "/v1/images/generations")

    def test_image_client_resolves_remote_images(self) -> None:
        config = AppConfig()
        config.image_generation.enabled = True
        config.image_generation.base_url = self.base_url
        config.image_generation.api_key = "secret"
        config.image_generation.model = "grok-imagine-image-pro"

        client = build_image_client(config)
        image_bytes, content_type = client.resolve_image(f"{self.base_url}/image.png")

        self.assertEqual(image_bytes, _PNG_BYTES)
        self.assertEqual(content_type, "image/png")


if __name__ == "__main__":
    unittest.main()
