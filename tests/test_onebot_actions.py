from __future__ import annotations

import asyncio
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

from waifu_standalone.gateways.onebot_actions import (
    OneBotActionClient,
    OneBotActionError,
    OneBotHttpOutboundPort,
)
from waifu_standalone.models import OutboundMessage


class _CaptureHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, object]]] = []
    response_body: dict[str, object] = {"status": "ok"}
    auth_headers: list[str] = []

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        self.__class__.requests.append((self.path, payload))
        self.__class__.auth_headers.append(self.headers.get("Authorization", ""))
        body = json.dumps(self.__class__.response_body).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class OneBotActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        _CaptureHandler.requests = []
        _CaptureHandler.response_body = {"status": "ok"}
        _CaptureHandler.auth_headers = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = OneBotActionClient(f"http://{host}:{port}")
        self.port = OneBotHttpOutboundPort(self.client)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_group_message_is_posted_as_onebot_action(self) -> None:
        message = OutboundMessage(
            launcher_id="612475113",
            launcher_type="group",
            text="hello",
            images=["generated://catgirl"],
        )

        self.port.send(message)

        self.assertEqual(_CaptureHandler.requests[0][0], "/send_group_msg")
        payload = _CaptureHandler.requests[0][1]
        self.assertEqual(payload["group_id"], 612475113)
        self.assertEqual(payload["message"][0]["data"]["text"], "hello")
        self.assertEqual(payload["message"][1]["data"]["file"], "generated://catgirl")

    def test_private_message_uses_user_id(self) -> None:
        message = OutboundMessage(
            launcher_id="10001",
            launcher_type="person",
            text="received",
        )

        self.port.send(message)

        self.assertEqual(_CaptureHandler.requests[0][0], "/send_private_msg")
        payload = _CaptureHandler.requests[0][1]
        self.assertEqual(payload["user_id"], 10001)

    def test_async_send_posts_as_onebot_action(self) -> None:
        message = OutboundMessage(
            launcher_id="612475113",
            launcher_type="group",
            text="async hello",
        )

        asyncio.run(self.port.send_async(message))

        self.assertEqual(_CaptureHandler.requests[0][0], "/send_group_msg")
        payload = _CaptureHandler.requests[0][1]
        self.assertEqual(payload["group_id"], 612475113)
        self.assertEqual(payload["message"][0]["data"]["text"], "async hello")

    def test_non_ok_onebot_response_raises(self) -> None:
        _CaptureHandler.response_body = {"status": "failed", "retcode": 1200}
        message = OutboundMessage(
            launcher_id="10001",
            launcher_type="person",
            text="received",
        )

        with self.assertRaises(OneBotActionError):
            self.port.send(message)

    def test_error_message_does_not_echo_response_payload(self) -> None:
        _CaptureHandler.response_body = {
            "status": "failed",
            "retcode": 1200,
            "headers": {"Authorization": "Bearer secret-token"},
        }

        with self.assertRaises(OneBotActionError) as ctx:
            self.port.send(
                OutboundMessage(
                    launcher_id="10001",
                    launcher_type="person",
                    text="received",
                )
            )

        self.assertEqual(str(ctx.exception), "onebot action failed")

    def test_access_token_is_sent_as_bearer_header(self) -> None:
        host, port = self.server.server_address
        client = OneBotActionClient(f"http://{host}:{port}", access_token="secret-token")
        port_adapter = OneBotHttpOutboundPort(client)

        port_adapter.send(
            OutboundMessage(
                launcher_id="10001",
                launcher_type="person",
                text="received",
            )
        )

        self.assertEqual(_CaptureHandler.auth_headers[0], "Bearer secret-token")

    def test_status_helpers_use_expected_action_names(self) -> None:
        self.client.get_version_info()
        self.client.get_login_info()
        self.client.get_status()

        self.assertEqual(
            [item[0] for item in _CaptureHandler.requests],
            ["/get_version_info", "/get_login_info", "/get_status"],
        )

    def test_group_member_helpers_use_expected_payloads(self) -> None:
        self.client.get_group_member_list("612475113")
        self.client.get_group_member_info("612475113", "783190298", no_cache=True)

        self.assertEqual(_CaptureHandler.requests[0][0], "/get_group_member_list")
        self.assertEqual(_CaptureHandler.requests[0][1]["group_id"], 612475113)
        self.assertEqual(_CaptureHandler.requests[1][0], "/get_group_member_info")
        self.assertEqual(_CaptureHandler.requests[1][1]["group_id"], 612475113)
        self.assertEqual(_CaptureHandler.requests[1][1]["user_id"], 783190298)
        self.assertTrue(_CaptureHandler.requests[1][1]["no_cache"])


if __name__ == "__main__":
    unittest.main()
