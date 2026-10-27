from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.config import AppConfig
from waifu_standalone.gateways.onebot_ws import OneBotWsGateway, OneBotWsActionClient, OneBotWsOutboundPort
from waifu_standalone.http_api import HttpApi
from waifu_standalone.http_api_async import _build_application
from waifu_standalone.models import InboundEvent, OutboundMessage


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

    async def test_sync_apis_use_the_socket_loop_and_receive_immediate_echoes(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        actions = OneBotWsActionClient(self.gateway)
        outbound = OneBotWsOutboundPort(self.gateway)
        calls = [
            (lambda: actions.get_group_member_list("123"), "get_group_member_list"),
            (lambda: actions.get_group_member_info("123", "456", no_cache=True), "get_group_member_info"),
            (lambda: outbound.send(OutboundMessage("123", "group", "hello")), "send_group_msg"),
        ]
        for invoke, action_name in calls:
            with self.subTest(action=action_name):
                task = asyncio.create_task(asyncio.to_thread(invoke))
                action = await asyncio.wait_for(ws.receive_json(), timeout=2)
                self.assertEqual(action["action"], action_name)
                await ws.send_json({"status": "ok", "retcode": 0, "data": {}, "echo": action["echo"]})
                await asyncio.wait_for(task, timeout=2)
                self.assertFalse(self.gateway._pending_echos)
        close_task = asyncio.create_task(asyncio.to_thread(outbound.close))
        await asyncio.wait_for(ws.receive(), timeout=2)
        await asyncio.wait_for(close_task, timeout=2)
        self.assertIsNone(self.gateway._active_ws)

    async def test_async_api_can_be_called_from_a_different_loop(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(self.gateway.send_action("get_status", {}))))
        action = await asyncio.wait_for(ws.receive_json(), timeout=2)
        await ws.send_json({"status": "ok", "retcode": 0, "data": {"online": True}, "echo": action["echo"]})
        response = await asyncio.wait_for(task, timeout=2)
        self.assertTrue(response["data"]["online"])
        await ws.close()

    async def test_sync_api_refuses_to_block_its_own_loop(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        with self.assertRaisesRegex(RuntimeError, "async reverse-WS API"):
            OneBotWsActionClient(self.gateway).get_group_member_list(123)
        await ws.close()

    async def test_failed_send_clears_pending_echo(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        with patch.object(self.gateway._active_ws, "send_str", new=AsyncMock(side_effect=ConnectionError("closed"))):
            with self.assertRaises(ConnectionError):
                await self.gateway.send_action("get_status", {})
        self.assertFalse(self.gateway._pending_echos)
        await ws.close()

    async def test_unconnected_gateway_can_be_closed_synchronously(self) -> None:
        OneBotWsOutboundPort(self.gateway).close()

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

    async def test_replayed_event_frame_is_handled_once(self) -> None:
        ws = await self.client.ws_connect("/onebot/v11/ws")
        frame = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 612475113,
            "user_id": 783190298,
            "message_id": 5001,
            "sender": {"user_id": 783190298, "nickname": "tester"},
            "raw_message": "hello from ws",
        }
        await ws.send_json(frame)
        await ws.send_json(frame)
        await ws.send_json({**frame, "message_id": 5002, "raw_message": "second message"})

        first = await asyncio.wait_for(self.service.events.get(), timeout=1)
        second = await asyncio.wait_for(self.service.events.get(), timeout=1)
        await asyncio.sleep(0.1)

        self.assertEqual({first.message_id, second.message_id}, {"5001", "5002"})
        self.assertTrue(self.service.events.empty())
        await ws.close()


if __name__ == "__main__":
    unittest.main()
