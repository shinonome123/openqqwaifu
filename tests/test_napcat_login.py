from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.gateways.napcat_login import (
    NapCatLoginBridge,
    _hash_webui_token,
    normalize_webui_settings,
    qrcode_payload_to_image_source,
)


class _FakeResponse:
    def __init__(self, payload: bytes, content_type: str = "application/json") -> None:
        self._payload = payload
        self._content_type = content_type
        self.headers = self

    def read(self) -> bytes:
        return self._payload

    def get_content_type(self) -> str:
        return self._content_type

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class NapCatLoginBridgeTests(unittest.TestCase):
    def test_hash_matches_official_napcat_rule(self) -> None:
        self.assertEqual(
            _hash_webui_token("napcat-token"),
            "9b42dc7052599a31694fa5f1169adb4f0713580913a9a597ae055810327811e3",
        )

    def test_bridge_logs_in_and_fetches_qr_status(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            body = json.loads(request.data.decode("utf-8"))
            headers = {key.lower(): value for key, value in request.header_items()}
            if request.full_url.endswith("/api/auth/login"):
                self.assertEqual(body["hash"], _hash_webui_token("secret-token"))
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus"):
                self.assertEqual(headers.get("authorization"), "Bearer cred-123")
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": False,
                                "isOffline": False,
                                "qrcodeurl": "napcat://scan-me",
                                "loginError": "",
                            },
                        }
                    ).encode("utf-8")
                )
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            panel = bridge.panel(refresh=True)

        self.assertTrue(panel["configured"])
        self.assertTrue(panel["token_configured"])
        self.assertEqual(panel["status"]["qrcode_url"], "napcat://scan-me")
        self.assertEqual(calls, [
            "http://127.0.0.1:6099/api/auth/login",
            "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus",
        ])

    def test_qrcode_payload_can_decode_data_urls_without_network(self) -> None:
        content_type, payload = qrcode_payload_to_image_source(
            "data:image/svg+xml;base64," + base64.b64encode(b"<svg></svg>").decode("ascii")
        )

        self.assertEqual(content_type, "image/svg+xml")
        self.assertEqual(payload, b"<svg></svg>")

    def test_http_qrcode_payload_is_rendered_as_qr_image(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            return _FakeResponse(b"PNGDATA", content_type="image/png")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            content_type, payload = qrcode_payload_to_image_source("https://txz.qq.com/p?k=test")

        self.assertEqual(content_type, "image/png")
        self.assertEqual(payload, b"PNGDATA")
        self.assertEqual(
            calls,
            ["https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=https%3A%2F%2Ftxz.qq.com%2Fp%3Fk%3Dtest"],
        )

    def test_normalize_webui_settings_accepts_full_panel_url(self) -> None:
        base, token = normalize_webui_settings(
            "http://127.0.0.1:6099/webui?token=secret-token",
            "",
        )

        self.assertEqual(base, "http://127.0.0.1:6099")
        self.assertEqual(token, "secret-token")


if __name__ == "__main__":
    unittest.main()
