from __future__ import annotations

import base64
import json
import sys
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.gateways.napcat_login import (
    NapCatLoginBridge,
    NapCatLoginError,
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

    def test_http_qrcode_payload_is_rendered_locally_without_network(self) -> None:
        with patch("urllib.request.urlopen") as mocked_urlopen:
            content_type, payload = qrcode_payload_to_image_source("https://txz.qq.com/p?k=test")

        self.assertEqual(content_type, "image/png")
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        mocked_urlopen.assert_not_called()

    def test_normalize_webui_settings_accepts_full_panel_url(self) -> None:
        base, token = normalize_webui_settings(
            "http://127.0.0.1:6099/webui?token=secret-token",
            "",
        )

        self.assertEqual(base, "http://127.0.0.1:6099")
        self.assertEqual(token, "secret-token")

    def test_panel_auto_refreshes_expired_qrcode(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            if request.full_url == "http://127.0.0.1:6099/auth/login":
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found",
                    hdrs=None,
                    fp=_FakeResponse(b""),
                )
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus"):
                if calls.count("http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus") == 1:
                    return _FakeResponse(
                        json.dumps(
                            {
                                "code": 0,
                                "data": {
                                    "isLogin": False,
                                    "isOffline": False,
                                    "qrcodeurl": "",
                                    "loginError": "二维码已失效，请刷新",
                                },
                            }
                        ).encode("utf-8")
                    )
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": False,
                                "isOffline": False,
                                "qrcodeurl": "https://txz.qq.com/p?k=fresh",
                                "loginError": "",
                            },
                        }
                    ).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/RefreshQRcode"):
                return _FakeResponse(json.dumps({"code": 0, "data": {}}).encode("utf-8"))
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099/webui?token=secret-token",
            api_prefix="/api",
            webui_token="",
            timeout=5.0,
        )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            panel = bridge.panel(refresh=True)

        self.assertEqual(panel["status"]["qrcode_url"], "https://txz.qq.com/p?k=fresh")
        self.assertIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)

    def test_unrelated_token_error_does_not_count_as_unauthorized(self) -> None:
        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus"):
                raise urllib.error.HTTPError(
                    request.full_url,
                    500,
                    "server error",
                    hdrs=None,
                    fp=_FakeResponse(json.dumps({"message": "invalid JSON token"}).encode("utf-8")),
                )
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(NapCatLoginError):
                bridge.fetch_status(force=True)

    def test_bridge_falls_back_to_alternate_api_base_after_404(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            if request.full_url == "http://127.0.0.1:6099/auth/login":
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found",
                    hdrs=None,
                    fp=_FakeResponse(b""),
                )
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/GetQQLoginInfo") or request.full_url.endswith(
                "/QQLogin/GetQQLoginInfo"
            ):
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found",
                    hdrs=None,
                    fp=_FakeResponse(b""),
                )
            if request.full_url == "http://127.0.0.1:6099/QQLogin/RefreshQRcode":
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found",
                    hdrs=None,
                    fp=_FakeResponse(b""),
                )
            if request.full_url == "http://127.0.0.1:6099/api/QQLogin/RefreshQRcode":
                return _FakeResponse(json.dumps({"code": 0, "data": None}).encode("utf-8"))
            if request.full_url == "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus":
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": False,
                                "isOffline": False,
                                "qrcodeurl": "https://txz.qq.com/p?k=fresh",
                                "loginError": "",
                            },
                        }
                    ).encode("utf-8")
                )
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="",
            webui_token="secret-token",
            timeout=5.0,
        )
        bridge._resolved_api_base = "http://127.0.0.1:6099"

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            status = bridge.refresh_qrcode()

        self.assertEqual(status["qrcode_url"], "https://txz.qq.com/p?k=fresh")
        self.assertIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)
        self.assertEqual(bridge._resolved_api_base, "http://127.0.0.1:6099/api")

    def test_refresh_qrcode_treats_already_logged_in_as_success(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/GetQQLoginInfo") or request.full_url.endswith(
                "/QQLogin/GetQQLoginInfo"
            ):
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "uin": "3956638110",
                                "nick": "卧槽",
                                "online": True,
                            },
                        }
                    ).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/RefreshQRcode"):
                return _FakeResponse(
                    json.dumps({"code": -1, "message": "QQ Is Logined", "data": None}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": True,
                                "isOffline": False,
                                "qrcodeurl": "",
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
            status = bridge.refresh_qrcode()

        self.assertTrue(status["is_login"])
        self.assertEqual(status["qrcode_url"], "")
        self.assertNotIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)

    def test_panel_keeps_logged_in_status_when_login_info_fetch_fails(self) -> None:
        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": True,
                                "isOffline": False,
                                "qrcodeurl": "",
                                "loginError": "",
                            },
                        }
                    ).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/GetQQLoginInfo") or request.full_url.endswith(
                "/QQLogin/GetQQLoginInfo"
            ):
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found",
                    hdrs=None,
                    fp=_FakeResponse(b""),
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

        self.assertTrue(panel["status"]["is_login"])
        self.assertEqual(panel["login_info"], {})
        self.assertTrue(panel["login_info_error"])
        self.assertNotIn("error", panel)

    def test_panel_prefers_logged_in_state_when_status_route_404s(self) -> None:
        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus") or request.full_url.endswith(
                "/QQLogin/CheckLoginStatus"
            ):
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "not found",
                    hdrs=None,
                    fp=_FakeResponse(b""),
                )
            if request.full_url.endswith("/api/QQLogin/GetQQLoginInfo") or request.full_url.endswith(
                "/QQLogin/GetQQLoginInfo"
            ):
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "uin": "3956638110",
                                "nick": "卧槽",
                                "online": True,
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

        self.assertTrue(panel["status"]["is_login"])
        self.assertEqual(panel["status"]["qrcode_url"], "")
        self.assertEqual(panel["login_info"]["uin"], "3956638110")
        self.assertNotIn("error", panel)

    def test_logged_in_status_clears_stale_qrcode_payload(self) -> None:
        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": True,
                                "isOffline": False,
                                "qrcodeurl": "https://txz.qq.com/p?k=stale",
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
            status = bridge.fetch_status(force=True)

        self.assertTrue(status["is_login"])
        self.assertEqual(status["qrcode_url"], "")

    def test_refresh_qrcode_returns_logged_in_state_without_hitting_refresh_endpoint(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            calls.append(request.full_url)
            if request.full_url.endswith("/api/auth/login"):
                return _FakeResponse(
                    json.dumps({"code": 0, "data": {"Credential": "cred-123"}}).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": True,
                                "isOffline": False,
                                "qrcodeurl": "https://txz.qq.com/p?k=stale",
                                "loginError": "",
                            },
                        }
                    ).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/GetQQLoginInfo"):
                return _FakeResponse(
                    json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "uin": "3956638110",
                                "nick": "卧槽",
                                "online": True,
                            },
                        }
                    ).encode("utf-8")
                )
            if request.full_url.endswith("/api/QQLogin/RefreshQRcode"):
                raise AssertionError("RefreshQRcode should not be called after login")
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            status = bridge.refresh_qrcode()

        self.assertTrue(status["is_login"])
        self.assertEqual(status["qrcode_url"], "")
        self.assertNotIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)


if __name__ == "__main__":
    unittest.main()
