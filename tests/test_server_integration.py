from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from codecs import BOM_UTF8
import http.client
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_runtime_service
from waifu_standalone.config import AppConfig, QQSidecarConfig
from waifu_standalone.http_api import HttpApi, run_server


class _ActionCaptureHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, object]]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        self.__class__.requests.append((self.path, payload))
        body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _ActionCaptureHandler.requests = []
        self.action_server = ThreadingHTTPServer(("127.0.0.1", 0), _ActionCaptureHandler)
        self.action_thread = threading.Thread(target=self.action_server.serve_forever, daemon=True)
        self.action_thread.start()

    def tearDown(self) -> None:
        self.action_server.shutdown()
        self.action_server.server_close()
        self.action_thread.join(timeout=2)

    def test_http_server_accepts_event_and_posts_outbound_action(self) -> None:
        host, port = self.action_server.server_address
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(
                data_root=tmpdir,
                qq_sidecar=QQSidecarConfig(
                    dry_run=False,
                    outbound_base_url=f"http://{host}:{port}",
                ),
            )
            service, _ = build_runtime_service(config)
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request_host, request_port = server.server_address
                payload = json.dumps(
                    {
                        "message_type": "group",
                        "group_id": 612475113,
                        "user_id": 783190298,
                        "sender": {"nickname": "tester"},
                        "message": [{"type": "text", "data": {"text": "hello"}}],
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://{request_host}:{request_port}/onebot/events",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read()

                self.assertEqual(response.status, HTTPStatus.NO_CONTENT)
                self.assertEqual(body, b"")
                self.assertEqual(_ActionCaptureHandler.requests[0][0], "/send_group_msg")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_health_endpoint_returns_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))

                self.assertEqual(body["status"], "ok")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_server_accepts_utf8_bom_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                payload = BOM_UTF8 + json.dumps(
                    {
                        "message_type": "group",
                        "group_id": 612475113,
                        "user_id": 783190298,
                        "sender": {"nickname": "tester"},
                        "message": [{"type": "text", "data": {"text": "hello"}}],
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://{host}:{port}/onebot/events",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read()

                self.assertEqual(response.status, HTTPStatus.NO_CONTENT)
                self.assertEqual(body, b"")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_server_accepts_chunked_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                payload = json.dumps(
                    {
                        "message_type": "group",
                        "group_id": 612475113,
                        "user_id": 783190298,
                        "sender": {"nickname": "tester"},
                        "message": [{"type": "text", "data": {"text": "hello"}}],
                    }
                ).encode("utf-8")
                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.putrequest("POST", "/onebot/events")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Transfer-Encoding", "chunked")
                connection.endheaders()
                midpoint = len(payload) // 2
                for chunk in (payload[:midpoint], payload[midpoint:]):
                    connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
                    connection.send(chunk)
                    connection.send(b"\r\n")
                connection.send(b"0\r\n\r\n")
                response = connection.getresponse()
                body = response.read()
                connection.close()

                self.assertEqual(response.status, HTTPStatus.NO_CONTENT)
                self.assertEqual(body, b"")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
