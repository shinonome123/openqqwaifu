from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http import cookiejar
from codecs import BOM_UTF8
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_runtime_service
from waifu_standalone.cells.skill_registry import build_skill_markdown_template
from waifu_standalone.config import AppConfig, QQSidecarConfig
from waifu_standalone.http_api import HttpApi, run_server
from waifu_standalone.models import InboundEvent, MessageSegment


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

    def _build_auth_opener(self, host: str, port: int) -> urllib.request.OpenerDirector:
        jar = cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(
            f"http://{host}:{port}/api/auth/bootstrap",
            data=json.dumps({"username": "admin", "password": "password123"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["user"]["username"], "admin")
        return opener

    def _open_json(
        self,
        opener: urllib.request.OpenerDirector,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
    ) -> tuple[HTTPStatus, dict[str, object]]:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with opener.open(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        return HTTPStatus(response.status), body

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

    def test_dashboard_page_is_served(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as response:
                    body = response.read().decode("utf-8")

                self.assertIn("AI Girlfriend Console", body)
                self.assertIn("/assets/js/main.js", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_dashboard_api_returns_runtime_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            service.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="call me luna")],
                )
            )
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                opener = self._build_auth_opener(host, port)
                _, body = self._open_json(opener, f"http://{host}:{port}/api/dashboard")

                self.assertEqual(body["assistant_name"], "琉璃")
                self.assertEqual(body["session_count"], 1)
                self.assertEqual(body["recent_outbound_count"], 1)
                self.assertEqual(body["tools"]["count"], 3)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_skills_api_returns_skill_list_and_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                opener = self._build_auth_opener(host, port)
                _, list_body = self._open_json(opener, f"http://{host}:{port}/api/skills")

                self.assertGreaterEqual(list_body["count"], 6)

                _, toggle_body = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/skills/search-command/toggle",
                    method="POST",
                    payload={"enabled": False},
                )

                self.assertEqual(toggle_body["status"], "ok")
                self.assertFalse(toggle_body["skill"]["enabled"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_console_panel_endpoints_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                opener = self._build_auth_opener(host, port)
                _, console_body = self._open_json(opener, f"http://{host}:{port}/api/console")
                self.assertIn("character", console_body)
                self.assertIn("skills", console_body)

                _, character_body = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/panels/character?character=default",
                )
                self.assertEqual(character_body["character"], "default")

                _, sidecar_body = self._open_json(opener, f"http://{host}:{port}/api/panels/sidecar")
                self.assertIn("inbound_host", sidecar_body)
                self.assertIn("reverse_ws_url", sidecar_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_skills_reload_endpoint_returns_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                opener = self._build_auth_opener(host, port)
                _, body = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/skills/reload",
                    method="POST",
                    payload={},
                )

                self.assertGreaterEqual(body["reload_count"], 1)
                self.assertGreaterEqual(body["count"], 6)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_tool_and_skill_crud_endpoints_work_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                opener = self._build_auth_opener(host, port)
                _, tools = self._open_json(opener, f"http://{host}:{port}/api/tools")
                self.assertEqual(tools["count"], 3)

                _, pack_template = self._open_json(opener, f"http://{host}:{port}/api/skill-packs/template")
                self.assertEqual(pack_template["format"], "waifu-skill-pack")

                _, template = self._open_json(opener, f"http://{host}:{port}/api/skills/template")
                self.assertIn("markdown", template)

                markdown = build_skill_markdown_template(
                    skill_id="radar-http",
                    name="Radar HTTP",
                    description="http install test",
                    triggers=["radar http"],
                    mode="prefix",
                    priority=7,
                    body="Use this skill from HTTP tests.",
                )
                _, installed = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/skills/install",
                    method="POST",
                    payload={"markdown": markdown},
                )
                self.assertEqual(installed["skill"]["id"], "radar-http")

                _, detail = self._open_json(opener, f"http://{host}:{port}/api/skills/radar-http")
                self.assertEqual(detail["id"], "radar-http")

                _, saved = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/skills/radar-http/save",
                    method="POST",
                    payload={"markdown": markdown.replace("http install test", "edited http install test")},
                )
                self.assertIn("edited http install test", saved["skill"]["markdown"])

                _, pack_export = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/skill-packs/export",
                    method="POST",
                    payload={"skill_ids": ["radar-http"], "include_builtin": False, "name": "radar-pack"},
                )
                self.assertEqual(pack_export["name"], "radar-pack")
                self.assertEqual(pack_export["skill_count"], 1)

                _, pack_import = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/skill-packs/import",
                    method="POST",
                    payload={"bundle": pack_export, "overwrite": True},
                )
                self.assertEqual(pack_import["status"], "ok")
                self.assertEqual(pack_import["pack"]["imported_count"], 1)

                _, deleted = self._open_json(
                    opener,
                    f"http://{host}:{port}/api/skills/radar-http",
                    method="DELETE",
                )
                self.assertEqual(deleted["deleted"], "radar-http")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_session_detail_endpoint_returns_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            service.handle_event(
                InboundEvent(
                    launcher_id="612475113",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="call me luna")],
                )
            )
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                opener = self._build_auth_opener(host, port)
                _, body = self._open_json(opener, f"http://{host}:{port}/api/sessions/group/612475113")

                self.assertEqual(body["launcher_id"], "612475113")
                self.assertTrue(body["history"])
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

    def test_http_server_rejects_large_content_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.putrequest("POST", "/onebot/events")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(10 * 1024 * 1024 + 1))
                connection.endheaders()
                response = connection.getresponse()
                body = json.loads(response.read().decode("utf-8"))
                connection.close()

                self.assertEqual(response.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                self.assertEqual(body["status"], "payload_too_large")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_server_rejects_invalid_skill_route_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_runtime_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                opener = self._build_auth_opener(host, port)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    opener.open(f"http://{host}:{port}/api/skills/%2E%2E", timeout=5)

                self.assertEqual(ctx.exception.code, HTTPStatus.BAD_REQUEST)
                body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertEqual(body["status"], "bad_request")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
