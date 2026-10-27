from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra import TransportError, safe_fetch
from waifu_standalone.infra.safe_fetch import (
    BlockedUrlError,
    check_public_url,
    fetch_public_url,
    is_public_address,
)
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.skills import ToolInvocation

_PUBLIC_IP = "93.184.215.14"
_FAKE_DNS = {
    "public.example": {_PUBLIC_IP},
    "napcat": {"172.18.0.3"},
    "mixed.example": {_PUBLIC_IP, "10.0.0.7"},
    "metadata.example": {"169.254.169.254"},
}


def _fake_resolve(host: str, port: int) -> set[str]:
    if host in _FAKE_DNS:
        return set(_FAKE_DNS[host])
    try:
        return {str(safe_fetch.ipaddress.ip_address(host))}
    except ValueError:
        raise TransportError(f"无法解析域名：{host}") from None


class SafeFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(safe_fetch, "_resolve_addresses", side_effect=_fake_resolve))

    def test_only_global_addresses_are_public(self) -> None:
        for address in (
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fd00::1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
        ):
            self.assertFalse(is_public_address(address), address)
        for address in (_PUBLIC_IP, "2606:4700:4700::1111", "::ffff:93.184.215.14"):
            self.assertTrue(is_public_address(address), address)

    def test_private_hosts_and_bad_schemes_are_blocked(self) -> None:
        for url in (
            "http://127.0.0.1:6099/api/QQLogin",
            "http://[::1]:8080/",
            "http://napcat:3000/",
            "http://mixed.example/",
            "http://metadata.example/latest/meta-data/",
            "file:///etc/passwd",
            "ftp://public.example/",
            "http:///nohost",
        ):
            with self.assertRaises(BlockedUrlError, msg=url):
                check_public_url(url)
        check_public_url("https://public.example/post")

    def test_redirect_into_private_network_is_blocked_before_it_is_requested(self) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://napcat:6099/api/token"})

        with self.assertRaises(BlockedUrlError):
            fetch_public_url(
                "https://public.example/short",
                timeout_seconds=5,
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(requested, ["https://public.example/short"])

    def test_public_redirects_are_followed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/short":
                return httpx.Response(301, headers={"location": "/article"})
            return httpx.Response(200, text="<html>正文</html>", headers={"content-type": "text/html; charset=utf-8"})

        page = fetch_public_url("https://public.example/short", timeout_seconds=5, transport=httpx.MockTransport(handler))

        self.assertEqual(page.url, "https://public.example/article")
        self.assertEqual(page.text, "<html>正文</html>")
        self.assertFalse(page.truncated)

    def test_body_is_read_only_up_to_the_cap(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"a" * 50_000, headers={"content-type": "text/plain"})

        page = fetch_public_url(
            "https://public.example/big",
            timeout_seconds=5,
            max_bytes=1000,
            transport=httpx.MockTransport(handler),
        )

        self.assertTrue(page.truncated)
        self.assertEqual(page.text, "a" * 1000)

    def test_redirect_loop_gives_up(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://public.example/again"})

        with self.assertRaises(TransportError) as caught:
            fetch_public_url("https://public.example/again", timeout_seconds=5, transport=httpx.MockTransport(handler))
        self.assertNotIsInstance(caught.exception, BlockedUrlError)
        self.assertIn("重定向次数过多", str(caught.exception))

    def test_http_errors_raise_transport_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="missing")

        with self.assertRaises(TransportError) as caught:
            fetch_public_url("https://public.example/gone", timeout_seconds=5, transport=httpx.MockTransport(handler))
        self.assertEqual(caught.exception.status_code, 404)

    def test_tools_refuse_private_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            event = InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="看看这个")],
            )
            session = service.memory.load("783190298", "person")
            invocation = ToolInvocation(
                tool_id="web-fetch",
                raw_args="",
                event=event,
                session=session,
                arguments={"url": "http://127.0.0.1:6099/api/auth"},
            )

            fetched = service.dispatcher.use_web_fetch_tool(invocation)
            self.assertTrue(fetched.error)
            self.assertIn("不能访问内网或本机地址", fetched.text)

            _content, _title, _meta, error = service.dispatcher._load_summarize_source("http://napcat:3000/")
            self.assertIn("不能访问内网或本机地址", error)


if __name__ == "__main__":
    unittest.main()
