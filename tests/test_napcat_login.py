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
    NapCatLoginError,
    _hash_webui_token,
    normalize_webui_settings,
    qrcode_payload_to_image_source,
)
from waifu_standalone.http_transport import HttpResponse, TransportError


def _json_response(payload: object) -> HttpResponse:
    raw = json.dumps(payload, ensure_ascii=False)
    return HttpResponse(
        status_code=200,
        text=raw,
        content=raw.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _http_error(status_code: int, body: object = "") -> TransportError:
    if isinstance(body, (dict, list)):
        raw = json.dumps(body, ensure_ascii=False)
    else:
        raw = str(body or "")
    return TransportError(
        f"http {status_code}: {raw[:160]}",
        status_code=status_code,
        body=raw,
    )


class NapCatLoginBridgeTests(unittest.TestCase):
    def test_hash_matches_official_napcat_rule(self) -> None:
        self.assertEqual(
            _hash_webui_token("napcat-token"),
            "9b42dc7052599a31694fa5f1169adb4f0713580913a9a597ae055810327811e3",
        )

    def test_bridge_logs_in_and_fetches_qr_status(self) -> None:
        calls: list[str] = []

        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(url)
            headers = {str(key).lower(): value for key, value in (kwargs.get("headers") or {}).items()}
            body = dict(kwargs.get("json_payload") or {})
            if url.endswith("/api/auth/login"):
                self.assertEqual(method, "POST")
                self.assertEqual(body["hash"], _hash_webui_token("secret-token"))
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/CheckLoginStatus"):
                self.assertEqual(headers.get("authorization"), "Bearer cred-123")
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": False,
                            "isOffline": False,
                            "qrcodeurl": "napcat://scan-me",
                            "loginError": "",
                        },
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            panel = bridge.panel(refresh=True)

        self.assertTrue(panel["configured"])
        self.assertTrue(panel["token_configured"])
        self.assertEqual(panel["status"]["qrcode_url"], "napcat://scan-me")
        self.assertEqual(
            calls,
            [
                "http://127.0.0.1:6099/api/auth/login",
                "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus",
            ],
        )

    def test_qrcode_payload_can_decode_data_urls_without_network(self) -> None:
        content_type, payload = qrcode_payload_to_image_source(
            "data:image/svg+xml;base64," + base64.b64encode(b"<svg></svg>").decode("ascii")
        )

        self.assertEqual(content_type, "image/svg+xml")
        self.assertEqual(payload, b"<svg></svg>")

    def test_http_qrcode_payload_is_rendered_locally_without_network(self) -> None:
        content_type, payload = qrcode_payload_to_image_source("https://txz.qq.com/p?k=test")

        self.assertEqual(content_type, "image/png")
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_normalize_webui_settings_accepts_full_panel_url(self) -> None:
        base, token = normalize_webui_settings(
            "http://127.0.0.1:6099/webui?token=secret-token",
            "",
        )

        self.assertEqual(base, "http://127.0.0.1:6099")
        self.assertEqual(token, "secret-token")

    def test_panel_auto_refreshes_expired_qrcode(self) -> None:
        calls: list[str] = []

        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(url)
            if url == "http://127.0.0.1:6099/auth/login":
                raise _http_error(404)
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/CheckLoginStatus"):
                if calls.count("http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus") == 1:
                    return _json_response(
                        {
                            "code": 0,
                            "data": {
                                "isLogin": False,
                                "isOffline": False,
                                "qrcodeurl": "",
                                "loginError": "浜岀淮鐮佸凡澶辨晥锛岃鍒锋柊",
                            },
                        }
                    )
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": False,
                            "isOffline": False,
                            "qrcodeurl": "https://txz.qq.com/p?k=fresh",
                            "loginError": "",
                        },
                    }
                )
            if url.endswith("/api/QQLogin/RefreshQRcode"):
                return _json_response({"code": 0, "data": {}})
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099/webui?token=secret-token",
            api_prefix="/api",
            webui_token="",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            panel = bridge.panel(refresh=True)

        self.assertEqual(panel["status"]["qrcode_url"], "https://txz.qq.com/p?k=fresh")
        self.assertIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)

    def test_unrelated_token_error_does_not_count_as_unauthorized(self) -> None:
        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/CheckLoginStatus"):
                raise _http_error(500, {"message": "invalid JSON token"})
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            with self.assertRaises(NapCatLoginError):
                bridge.fetch_status(force=True)

    def test_bridge_falls_back_to_alternate_api_base_after_404(self) -> None:
        calls: list[str] = []

        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(url)
            if url == "http://127.0.0.1:6099/auth/login":
                raise _http_error(404)
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/GetQQLoginInfo") or url.endswith("/QQLogin/GetQQLoginInfo"):
                raise _http_error(404)
            if url == "http://127.0.0.1:6099/QQLogin/RefreshQRcode":
                raise _http_error(404)
            if url == "http://127.0.0.1:6099/api/QQLogin/RefreshQRcode":
                return _json_response({"code": 0, "data": None})
            if url == "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus":
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": False,
                            "isOffline": False,
                            "qrcodeurl": "https://txz.qq.com/p?k=fresh",
                            "loginError": "",
                        },
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="",
            webui_token="secret-token",
            timeout=5.0,
        )
        bridge._resolved_api_base = "http://127.0.0.1:6099"

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            status = bridge.refresh_qrcode()

        self.assertEqual(status["qrcode_url"], "https://txz.qq.com/p?k=fresh")
        self.assertIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)
        self.assertEqual(bridge._resolved_api_base, "http://127.0.0.1:6099/api")

    def test_force_auth_reuses_last_successful_auth_base(self) -> None:
        calls: list[str] = []

        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(url)
            if url == "http://127.0.0.1:6099/api/auth/login":
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url == "http://127.0.0.1:6099/auth/login":
                raise AssertionError("root auth endpoint should not be probed once /api succeeded")
            if url == "http://127.0.0.1:6099/QQLogin/CheckLoginStatus":
                raise _http_error(404)
            if url == "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus":
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": False,
                            "isOffline": False,
                            "qrcodeurl": "https://txz.qq.com/p?k=fresh",
                            "loginError": "",
                        },
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )
        bridge._resolved_auth_base = "http://127.0.0.1:6099/api"
        bridge._resolved_api_base = "http://127.0.0.1:6099"

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            status = bridge.fetch_status(force=True)

        self.assertEqual(status["qrcode_url"], "https://txz.qq.com/p?k=fresh")
        self.assertEqual(calls[0], "http://127.0.0.1:6099/api/auth/login")
        self.assertNotIn("http://127.0.0.1:6099/auth/login", calls)

    def test_refresh_qrcode_treats_already_logged_in_as_success(self) -> None:
        calls: list[str] = []

        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(url)
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/GetQQLoginInfo") or url.endswith("/QQLogin/GetQQLoginInfo"):
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "uin": "3956638110",
                            "nick": "鍗фЫ",
                            "online": True,
                        },
                    }
                )
            if url.endswith("/api/QQLogin/RefreshQRcode"):
                return _json_response({"code": -1, "message": "QQ Is Logined", "data": None})
            if url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": True,
                            "isOffline": False,
                            "qrcodeurl": "",
                            "loginError": "",
                        },
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            status = bridge.refresh_qrcode()

        self.assertTrue(status["is_login"])
        self.assertEqual(status["qrcode_url"], "")
        self.assertNotIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)

    def test_panel_keeps_logged_in_status_when_login_info_fetch_fails(self) -> None:
        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": True,
                            "isOffline": False,
                            "qrcodeurl": "",
                            "loginError": "",
                        },
                    }
                )
            if url.endswith("/api/QQLogin/GetQQLoginInfo") or url.endswith("/QQLogin/GetQQLoginInfo"):
                raise _http_error(404)
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            panel = bridge.panel(refresh=True)

        self.assertTrue(panel["status"]["is_login"])
        self.assertEqual(panel["login_info"], {})
        self.assertTrue(panel["login_info_error"])
        self.assertNotIn("error", panel)

    def test_panel_prefers_logged_in_state_when_status_route_404s(self) -> None:
        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/CheckLoginStatus") or url.endswith("/QQLogin/CheckLoginStatus"):
                raise _http_error(404)
            if url.endswith("/api/QQLogin/GetQQLoginInfo") or url.endswith("/QQLogin/GetQQLoginInfo"):
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "uin": "3956638110",
                            "nick": "鍗фЫ",
                            "online": True,
                        },
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            panel = bridge.panel(refresh=True)

        self.assertTrue(panel["status"]["is_login"])
        self.assertEqual(panel["status"]["qrcode_url"], "")
        self.assertEqual(panel["login_info"]["uin"], "3956638110")
        self.assertNotIn("error", panel)

    def test_logged_in_status_clears_stale_qrcode_payload(self) -> None:
        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": True,
                            "isOffline": False,
                            "qrcodeurl": "https://txz.qq.com/p?k=stale",
                            "loginError": "",
                        },
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            status = bridge.fetch_status(force=True)

        self.assertTrue(status["is_login"])
        self.assertEqual(status["qrcode_url"], "")

    def test_refresh_qrcode_returns_logged_in_state_without_hitting_refresh_endpoint(self) -> None:
        calls: list[str] = []

        def fake_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(url)
            if url.endswith("/api/auth/login"):
                return _json_response({"code": 0, "data": {"Credential": "cred-123"}})
            if url.endswith("/api/QQLogin/CheckLoginStatus"):
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "isLogin": True,
                            "isOffline": False,
                            "qrcodeurl": "https://txz.qq.com/p?k=stale",
                            "loginError": "",
                        },
                    }
                )
            if url.endswith("/api/QQLogin/GetQQLoginInfo"):
                return _json_response(
                    {
                        "code": 0,
                        "data": {
                            "uin": "3956638110",
                            "nick": "鍗фЫ",
                            "online": True,
                        },
                    }
                )
            if url.endswith("/api/QQLogin/RefreshQRcode"):
                raise AssertionError("RefreshQRcode should not be called after login")
            raise AssertionError(f"Unexpected URL: {url}")

        bridge = NapCatLoginBridge(
            base_url="http://127.0.0.1:6099",
            api_prefix="/api",
            webui_token="secret-token",
            timeout=5.0,
        )

        with patch("waifu_standalone.gateways.napcat_login.SyncHttpTransport.request", side_effect=fake_request):
            status = bridge.refresh_qrcode()

        self.assertTrue(status["is_login"])
        self.assertEqual(status["qrcode_url"], "")
        self.assertNotIn("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", calls)


if __name__ == "__main__":
    unittest.main()
