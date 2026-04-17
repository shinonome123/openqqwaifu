from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.config import AppConfig
from waifu_standalone.gateways.onebot_ws import OneBotWsGateway
from waifu_standalone.http_api import HttpApi
from waifu_standalone.http_api_async import _build_application
from waifu_standalone.models import InboundEvent


class _ReverseWsService:
    def __init__(self, gateway: OneBotWsGateway) -> None:
        self.config = AppConfig()
        self.reverse_ws_gateway = gateway
        self.events: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self.notices: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def handle_event_async(self, event: InboundEvent):  # type: ignore[no-untyped-def]
        await self.events.put(event)
        return None

    async def handle_notice_payload_async(self, payload: dict[str, object]) -> dict[str, object]:
        await self.notices.put(dict(payload))
        return {"status": "ok"}


class OneBotWsGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.seen_events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.gateway = OneBotWsGateway(event_handler=self.seen_events.put)
        self.app = web.Application()
        self.app.router.add_get("/onebot/v11/ws", self.gateway.handle)
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.close()

    async def test_event_frame_dispatched_to_handler(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        await ws.send_json(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 612475113,
                "user_id": 783190298,
                "sender": {"user_id": 783190298, "nickname": "tester"},
                "raw_message": "hello",
            }
        )

        payload = await asyncio.wait_for(self.seen_events.get(), timeout=1)

        self.assertEqual(payload["post_type"], "message")
        self.assertEqual(payload["group_id"], 612475113)
        await ws.close()

    async def test_action_echo_roundtrip(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        task = asyncio.create_task(self.gateway.send_action("send_group_msg", {"group_id": 1, "message": []}))

        action = await asyncio.wait_for(ws.receive_json(), timeout=1)
        self.assertEqual(action["action"], "send_group_msg")
        self.assertTrue(action["echo"])

        await ws.send_json({"status": "ok", "retcode": 0, "data": {"message_id": 42}, "echo": action["echo"]})
        response = await asyncio.wait_for(task, timeout=1)

        self.assertEqual(response["data"]["message_id"], 42)
        await ws.close()

    async def test_no_connection_raises(self) -> None:
        with self.assertRaises(ConnectionError):
            await self.gateway.send_action("get_status", {})

    async def test_new_connection_supersedes_old(self) -> None:
        ws1 = await self.client.ws_connect("/onebot/v11/ws")
        task = asyncio.create_task(self.gateway.send_action("get_status", {}))
        frame = await asyncio.wait_for(ws1.receive_json(), timeout=1)
        self.assertEqual(frame["action"], "get_status")

        ws2 = await self.client.ws_connect("/onebot/v11/ws")

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        msg = await asyncio.wait_for(ws1.receive(), timeout=1)
        self.assertIn(msg.type, {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED})
        await ws2.close()

    async def test_access_token_is_required_when_configured(self) -> None:
        protected_gateway = OneBotWsGateway(access_token="secret-token", event_handler=self.seen_events.put)
        app = web.Application()
        app.router.add_get("/onebot/v11/ws", protected_gateway.handle)
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            with self.assertRaises(WSServerHandshakeError):
                await client.ws_connect("/onebot/v11/ws")
            ws = await client.ws_connect("/onebot/v11/ws", headers={"Authorization": "Bearer secret-token"})
            await ws.close()
        finally:
            await client.close()
            await server.close()


class OneBotWsHttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.gateway = OneBotWsGateway()
        self.service = _ReverseWsService(self.gateway)
        self.api = HttpApi(self.service)  # type: ignore[arg-type]
        self.server = TestServer(_build_application(self.api))
        await self.server.start_server()
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.close()

    async def test_http_application_route_dispatches_event_to_service(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        await ws.send_json(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 612475113,
                "user_id": 783190298,
                "sender": {"user_id": 783190298, "nickname": "tester"},
                "raw_message": "hello from ws",
            }
        )

        event = await asyncio.wait_for(self.service.events.get(), timeout=1)

        self.assertEqual(event.launcher_type, "group")
        self.assertEqual(event.launcher_id, "612475113")
        self.assertEqual(event.sender_id, "783190298")
        await ws.close()


if __name__ == "__main__":
    unittest.main()
