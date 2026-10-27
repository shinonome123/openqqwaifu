from __future__ import annotations

import asyncio
import json
import http.client
import sqlite3
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.character.cards import CharacterCard
from waifu_standalone.config import AppConfig, RuntimeV2Config
from waifu_standalone.models import InboundEvent, MessageSegment, OutboundMessage
from waifu_standalone.runtime.testing import CapturingOutboundPort
from waifu_standalone.runtime_v2.contracts import CharacterSnapshot, DeliveryReceipt, EventEnvelope
from waifu_standalone.runtime_v2.engine import ConversationEngine, RuntimeHost
from waifu_standalone.runtime_v2.integration import OutboundAdapter, database_path
from waifu_standalone.runtime_v2.store import RuntimeBusy, RuntimeStore
from waifu_standalone.settings_admin.config_manager import ConfigManager
from waifu_standalone.web.http_api import HttpApi, make_handler, run_server


def event(message_id="1", user="alice", text="hello"):
    return InboundEvent(user, "person", user, user, [MessageSegment("text", text=text)],
                        message_id=message_id, source_account_id="bot", occurred_at=1234.0)


def character(name="alpha"):
    return CharacterSnapshot.create(name, asdict(CharacterCard(assistant_name=name)))


class FakeGenerator:
    def __init__(self):
        self.calls = []

    async def generate(self, context, incoming, history):
        self.calls.append((context, incoming, history))
        return OutboundMessage(incoming.launcher_id, "person", context.binding.character.character_id + ":" + incoming.plain_text)


class FakeSender:
    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, message):
        self.sent.append(message)
        if self.fail:
            raise TimeoutError("a remote send may have completed")
        return DeliveryReceipt("captured")


class DurableRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.path, "bot", "capture")
        self.generator, self.sender = FakeGenerator(), FakeSender()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "config-v1")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def admit(self, incoming=None, card=None):
        return self.store.admit(EventEnvelope.from_inbound("bot", incoming or event()), card or character())

    def restart(self):
        self.store.close()
        self.store = RuntimeStore(self.path, "bot", "capture")
        self.store.recover()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "config-v2")

    async def test_duplicate_after_restart_does_not_generate_or_send_again(self):
        admitted = self.admit()
        await self.engine.step("alice")
        self.restart()
        duplicate = self.admit()
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.event_id, admitted.event_id)
        self.assertFalse(await self.engine.step("alice"))
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(len(self.generator.calls), 1)

    async def test_interrupted_generation_recovers_snapshot_and_single_input_history(self):
        self.admit()
        interrupted = self.store.next_work("alice", "config-v1")
        self.restart()
        await self.engine.step("alice")
        context = self.generator.calls[0][0]
        self.assertEqual(context.turn_id, interrupted["context"].turn_id)
        self.assertEqual(context.binding.character.version, character().version)
        self.assertEqual(context.config_version, "config-v2")
        self.admit(event("2"))
        work = self.store.next_work("alice", "config-v2")
        history = self.store.history(work["context"])
        self.assertEqual([m.role for m in history], ["user", "assistant"])

    async def test_committed_proposal_is_sent_after_restart_without_regeneration(self):
        self.admit()
        work = self.store.next_work("alice", "v1")
        self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "saved answer"))
        self.restart()
        await self.engine.step("alice")
        self.assertEqual(self.sender.sent[0].text, "saved answer")
        self.assertEqual(self.generator.calls, [])

    async def test_crash_during_dispatch_requires_review_and_blocks_only_its_conversation(self):
        self.admit()
        work = self.store.next_work("alice", "v1")
        outbox = self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "maybe sent"))
        self.store.begin_dispatch(outbox["outbox_id"])
        self.admit(event("2"))
        self.admit(event("1", "bob"))
        self.restart()
        self.assertFalse(await self.engine.step("alice"))
        self.assertEqual(self.store.ready_conversations(), ["bob"])
        await self.engine.step("bob")
        self.assertEqual([m.launcher_id for m in self.sender.sent], ["bob"])
        self.store.resolve_delivery(outbox["outbox_id"], "confirm_sent", "checked platform history")
        await self.engine.step("alice")
        self.assertEqual(len(self.sender.sent), 2)
        self.assertIn("maybe sent", [m.content for m in self.generator.calls[-1][2]])

    async def test_ack_then_local_commit_failure_never_blindly_repeats_send(self):
        self.admit()
        with patch.object(self.store, "finish_delivery", side_effect=sqlite3.OperationalError("disk unavailable")):
            with self.assertRaises(sqlite3.OperationalError):
                await self.engine.step("alice")
        self.restart()
        self.assertFalse(await self.engine.step("alice"))
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.store.snapshot()["outbox"][0]["state"], "unknown")

    async def test_commit_succeeded_before_exception_still_prevents_resend(self):
        self.admit()
        original = self.store.finish_delivery

        def commit_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("subsequent failure")

        with patch.object(self.store, "finish_delivery", side_effect=commit_then_fail):
            with self.assertRaises(RuntimeError):
                await self.engine.step("alice")
        self.restart()
        self.assertFalse(await self.engine.step("alice"))
        self.assertEqual(len(self.sender.sent), 1)

    async def test_timeout_needs_explicit_review_retry_and_reuses_proposal(self):
        self.sender.fail = True
        self.admit()
        await self.engine.step("alice")
        outbox = self.store.snapshot()["outbox"][0]
        self.assertEqual(outbox["state"], "unknown")
        self.assertFalse(await self.engine.step("alice"))
        self.sender.fail = False
        self.store.resolve_delivery(outbox["outbox_id"], "retry", "verified not received")
        await self.engine.step("alice")
        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(len(self.sender.sent), 2)
        self.assertEqual(self.store.snapshot()["outbox"][0]["attempts"], 2)

    async def test_discarded_reply_is_not_added_to_history(self):
        self.sender.fail = True
        self.admit()
        await self.engine.step("alice")
        outbox = self.store.snapshot()["outbox"][0]
        self.store.resolve_delivery(outbox["outbox_id"], "discard", "abandon ambiguous send")
        self.sender.fail = False
        self.admit(event("2", text="next"))
        await self.engine.step("alice")
        self.assertEqual([m.role for m in self.generator.calls[-1][2]], ["user"])

    async def test_binding_barrier_and_history_follow_character_not_global_default(self):
        self.admit(event("1", text="alpha-only"))
        switch = self.store.enqueue_binding("alice", character("beta"), "switch-beta")
        self.admit(event("2", text="beta-only"), character("unrelated-global"))
        await self.engine.step("alice")
        await self.engine.step("alice")
        await self.engine.step("alice")
        self.assertEqual([c[0].binding.character.character_id for c in self.generator.calls], ["alpha", "beta"])
        self.assertEqual(self.generator.calls[1][2], [])
        self.assertEqual(self.generator.calls[1][0].binding.generation, 2)
        duplicate = self.store.enqueue_binding("alice", character("beta"), "switch-beta")
        self.assertEqual(duplicate.event_id, switch.event_id)
        with self.assertRaises(ValueError):
            self.store.enqueue_binding("alice", character("alpha"), "switch-beta")
        self.store.enqueue_binding("alice", character(), "switch-back")
        self.admit(event("3", text="welcome back"))
        await self.engine.step("alice")
        await self.engine.step("alice")
        self.assertEqual([m.content for m in self.generator.calls[-1][2]], ["alpha-only", "alpha:alpha-only"])

    async def test_no_platform_id_does_not_collapse_identical_human_messages(self):
        first, second = self.admit(event("")), self.admit(event(""))
        self.assertNotEqual(first.event_id, second.event_id)
        await self.engine.step("alice")
        await self.engine.step("alice")
        self.assertEqual(len(self.sender.sent), 2)

    async def test_capacity_failure_rolls_back_admission_and_sequence(self):
        self.store.max_pending = 1
        first = self.admit()
        with self.assertRaises(RuntimeBusy):
            self.admit(event("2"))
        self.assertTrue(self.admit().duplicate)
        await self.engine.step("alice")
        second = self.admit(event("2"))
        self.assertEqual(second.conversation_seq, first.conversation_seq + 1)

    async def test_generation_failure_is_visible_and_never_becomes_assistant_memory(self):
        self.admit()
        with patch.object(self.generator, "generate", new=AsyncMock(side_effect=TimeoutError)):
            await self.engine.step("alice")
        self.assertEqual(self.store.snapshot()["failures"][0]["error_code"], "generation_TimeoutError")
        self.assertEqual(self.sender.sent, [])

    async def test_offline_sender_keeps_pending_outbox_without_dispatch_attempt(self):
        self.admit()
        self.sender.is_ready = lambda: False
        self.assertFalse(await self.engine.step("alice"))
        row = self.store.snapshot()["outbox"][0]
        self.assertEqual((row["state"], row["attempts"]), ("pending", 0))
        self.sender.is_ready = lambda: True
        await self.engine.step("alice")
        self.assertEqual(len(self.generator.calls), 1)

    def test_writer_lock_and_mode_or_account_guard(self):
        with self.assertRaises(RuntimeBusy):
            RuntimeStore(self.path, "bot", "capture")
        self.store.close()
        for account, mode in [("other", "capture"), ("bot", "live")]:
            with self.assertRaisesRegex(ValueError, "mismatch"):
                RuntimeStore(self.path, account, mode)
        self.store = RuntimeStore(self.path, "bot", "capture")

    def test_wrong_source_account_rejected_before_storage(self):
        incoming = event()
        incoming.source_account_id = "other"
        with self.assertRaisesRegex(ValueError, "source account"):
            self.admit(incoming)
        self.assertEqual(self.store.snapshot()["counts"], {})

    async def test_capture_receipt_cannot_complete_live_delivery(self):
        self.admit()
        work = self.store.next_work("alice", "v1")
        outbox = self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "reply"))
        self.store.begin_dispatch(outbox["outbox_id"])
        with self.assertRaisesRegex(ValueError, "mode mismatch"):
            self.store.finish_delivery(outbox["outbox_id"], "accepted")


class HostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.host = None

    def tearDown(self):
        if self.host is not None:
            self.host.close()
        self.temp.cleanup()

    def test_cross_conversation_concurrency_and_same_conversation_order(self):
        entered = {name: threading.Event() for name in ("alice", "bob")}
        release = threading.Event()

        class BlockingGenerator(FakeGenerator):
            async def generate(inner, context, incoming, history):
                entered[incoming.launcher_id].set()
                if not await asyncio.to_thread(release.wait, 5):
                    raise TimeoutError("test did not release generation")
                return await super().generate(context, incoming, history)

        store = RuntimeStore(self.path, "bot", "capture")
        generator, sender = BlockingGenerator(), FakeSender()
        self.host = RuntimeHost(ConversationEngine(store, generator, sender, "v1"), workers=2)
        try:
            for incoming in [event("1"), event("2"), event("1", "bob")]:
                self.host.mutate(store.admit, EventEnvelope.from_inbound("bot", incoming), character())
            self.assertTrue(entered["alice"].wait(2))
            self.assertTrue(entered["bob"].wait(2))
            self.assertEqual(self.host.status()["active_conversations"], ["alice", "bob"])
        finally:
            release.set()
        self.host.wait_idle(5)
        self.assertEqual([c[1].message_id for c in generator.calls if c[1].launcher_id == "alice"], ["1", "2"])
        self.assertEqual(len(sender.sent), 3)

    def test_shutdown_timeout_retains_storage_owner_until_inflight_work_finishes(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingGenerator(FakeGenerator):
            async def generate(inner, context, incoming, history):
                entered.set()
                await asyncio.to_thread(release.wait, 5)
                return await super().generate(context, incoming, history)

        store = RuntimeStore(self.path, "bot", "capture")
        self.host = RuntimeHost(ConversationEngine(store, BlockingGenerator(), FakeSender(), "v1"))
        self.host.mutate(store.admit, EventEnvelope.from_inbound("bot", event()), character())
        self.assertTrue(entered.wait(2))
        self.host.mutate(store.admit, EventEnvelope.from_inbound("bot", event("2")), character())
        try:
            with self.assertRaises(TimeoutError):
                self.host.close(timeout=0.01)
            with self.assertRaises(RuntimeBusy):
                RuntimeStore(self.path, "bot", "capture")
            with self.assertRaises(RuntimeBusy):
                self.host.mutate(store.admit, EventEnvelope.from_inbound("bot", event("3")), character())
        finally:
            release.set()
        self.host.close(timeout=5)
        self.host = None
        reopened = RuntimeStore(self.path, "bot", "capture")
        try:
            reopened.recover()
            self.assertEqual(reopened.snapshot()["counts"], {"completed": 1, "queued": 1})
        finally:
            reopened.close()

    def test_service_acks_persisted_event_before_model_finishes_and_routes_only_allowlist(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingGenerator(FakeGenerator):
            async def generate(inner, context, incoming, history):
                entered.set()
                await asyncio.to_thread(release.wait, 5)
                return await super().generate(context, incoming, history)

        config = AppConfig(data_root=self.temp.name, bot_account_id="bot",
                           runtime_v2=RuntimeV2Config(enabled=True, private_user_ids=["alice"], max_pending_events=1))
        with patch("waifu_standalone.runtime_v2.integration.ReplyComposer", return_value=BlockingGenerator()):
            service, outbound = build_default_service(config)
        try:
            api = HttpApi(service)
            payload = {"post_type": "message", "message_type": "private", "self_id": "bot",
                       "user_id": "alice", "message_id": "1", "message": "hello"}
            status, result = api.handle_json(payload)
            self.assertEqual(status, 202)
            self.assertEqual(result["status"], "queued")
            self.assertTrue(entered.wait(2))
            self.assertEqual(outbound.sent, [])
            self.assertEqual(service.memory.load("alice", "person").history, [])
            self.assertTrue(api.handle_json(payload)[1]["duplicate"])
            self.assertEqual(api.handle_json({**payload, "message_id": "capacity-rejected"})[0], 503)
            self.assertFalse(service.private_runtime.routes(event(user="bob")))
            incoming = event()
            incoming.launcher_type = "group"
            self.assertFalse(service.private_runtime.routes(incoming))
        finally:
            release.set()
            service.private_runtime.host.wait_idle(5)
            service.close()
        self.assertEqual(len(outbound.sent), 1)
        self.assertTrue(database_path(config, capture=True).is_file())
        self.assertFalse(database_path(config, capture=False).exists())
        config.runtime_v2.enabled = False
        with self.assertRaisesRegex(ValueError, "owns existing conversations"):
            build_default_service(config)
        config.runtime_v2.enabled = True
        config.runtime_v2.private_user_ids = ["bob"]
        with self.assertRaisesRegex(ValueError, "explicit data migration"):
            build_default_service(config)

    def test_config_roundtrip_and_environment_allowlist(self):
        config = AppConfig(runtime_v2=RuntimeV2Config(enabled=True, private_user_ids=["alice"], workers=2))
        path = Path(self.temp.name) / "config.json"
        ConfigManager().save(config, path)
        loaded = ConfigManager(path).load()
        self.assertEqual(loaded.runtime_v2, config.runtime_v2)
        with patch.dict("os.environ", {"OPENQQWAIFU_RUNTIME_V2_PRIVATE_USER_IDS": "bob,carol"}):
            self.assertEqual(ConfigManager(path).load().runtime_v2.private_user_ids, ["bob", "carol"])


class OutboundAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_onebot_async_acceptance_is_not_a_definite_rejection(self):
        from waifu_standalone.gateways.onebot_actions import OneBotActionError, OneBotActionRejected, ensure_onebot_action_result
        with self.assertRaises(OneBotActionError) as raised:
            ensure_onebot_action_result({"status": "async", "retcode": 1})
        self.assertNotIsInstance(raised.exception, OneBotActionRejected)

    async def test_explicit_platform_rejection_remains_distinct_from_timeout(self):
        from waifu_standalone.gateways.onebot_actions import OneBotActionRejected
        from waifu_standalone.runtime_v2.contracts import DeliveryRejected
        outbound = type("Port", (), {"send_async": AsyncMock(side_effect=OneBotActionRejected("rejected"))})()
        adapter = OutboundAdapter(outbound, AppConfig(bot_account_id="bot"), capture=False)
        with self.assertRaises(DeliveryRejected):
            await adapter.send(OutboundMessage("alice", "person", "hello"))

    async def test_unconfirmed_response_is_not_reported_as_accepted(self):
        config = AppConfig(bot_account_id="bot")
        outbound = type("Port", (), {"send_async": AsyncMock(return_value=None)})()
        adapter = OutboundAdapter(outbound, config, capture=False)
        with self.assertRaises(RuntimeError):
            await adapter.send(OutboundMessage("alice", "person", "hello"))

    async def test_platform_receipt_and_live_mode_change(self):
        from waifu_standalone.runtime_v2.contracts import DeliveryRejected
        config = AppConfig(bot_account_id="bot")
        config.qq_sidecar.dry_run = False
        outbound = type("Port", (), {"send_async": AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 123}})})()
        adapter = OutboundAdapter(outbound, config, capture=False)
        receipt = await adapter.send(OutboundMessage("alice", "person", "hello"))
        self.assertEqual(receipt, DeliveryReceipt("accepted", "123"))
        config.qq_sidecar.dry_run = True
        with self.assertRaises(DeliveryRejected):
            await adapter.send(OutboundMessage("alice", "person", "hello"))
        self.assertEqual(outbound.send_async.await_count, 1)


class RuntimeHttpTests(unittest.TestCase):
    def test_admin_commands_and_private_callback_on_both_servers(self):
        def request(address, path, *, method="GET", payload=None, headers=None):
            connection = http.client.HTTPConnection(*address, timeout=5)
            try:
                connection.request(method, path, body=json.dumps(payload).encode() if payload is not None else None,
                                   headers={"Content-Type": "application/json", **(headers or {})})
                response = connection.getresponse()
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
            finally:
                connection.close()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory() as root:
                config = AppConfig(data_root=root, bot_account_id="bot",
                                   runtime_v2=RuntimeV2Config(enabled=True, private_user_ids=["alice"], reminders_enabled=True))
                config.qq_sidecar.inbound_access_token = "test-inbound"
                with patch("waifu_standalone.runtime_v2.integration.ReplyComposer", return_value=FakeGenerator()):
                    service, _ = build_default_service(config)
                service.private_runtime.host.engine.sender = FakeSender(fail=True)
                api = HttpApi(service)
                api.auth.bootstrap_admin("admin", "password123")
                with api.auth._lock:
                    api.auth._users["viewer"] = api.auth._build_user_record("viewer", password="password123", role="viewer")
                    api.auth._save_locked()
                _, admin_cookie = api.login({"username": "admin", "password": "password123"})
                _, viewer_cookie = api.login({"username": "viewer", "password": "password123"})
                admin = {"Cookie": admin_cookie.split(";", 1)[0]}
                viewer = {"Cookie": viewer_cookie.split(";", 1)[0]}
                server = run_server(api, "127.0.0.1", 0) if asynchronous else ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = server.server_address
                try:
                    payload = {"post_type": "message", "message_type": "private", "user_id": "alice",
                               "self_id": "bot", "message_id": "http-1", "message": "hello"}
                    status, _ = request(address, "/onebot/events", method="POST", payload=payload,
                                        headers={"Authorization": "Bearer test-inbound"})
                    self.assertEqual(status, 204)
                    service.private_runtime.host.wait_idle(5)
                    self.assertEqual(request(address, "/api/v2/runtime")[0], 401)
                    self.assertEqual(request(address, "/api/v2/runtime", headers=viewer)[0], 403)
                    status, result = request(address, "/api/v2/runtime", headers=admin)
                    self.assertEqual(status, 200)
                    self.assertEqual(result["outbox"][0]["state"], "unknown")
                    outbox_id = result["outbox"][0]["outbox_id"]
                    detail_path = "/api/v2/runtime/delivery?outbox_id=" + outbox_id
                    self.assertEqual(request(address, detail_path, headers=viewer)[0], 403)
                    detail_status, detail = request(address, detail_path, headers=admin)
                    self.assertEqual(detail_status, 200)
                    self.assertEqual(detail["message"]["launcher_id"], "alice")
                    self.assertEqual(detail["delivery_attempts"][0]["state"], "unknown")
                    list_path = "/api/v2/runtime/conversations"
                    conversation_path = "/api/v2/runtime/conversation?conversation_id=alice"
                    for path in (list_path, conversation_path):
                        self.assertEqual(request(address, path)[0], 401)
                        self.assertEqual(request(address, path, headers=viewer)[0], 403)
                        self.assertEqual(request(address, path, headers=admin)[0], 200)
                    _, listing = request(address, list_path, headers=admin)
                    self.assertEqual(listing["conversations"][0]["conversation_id"], "alice")
                    self.assertNotIn("hello", json.dumps(listing))
                    _, conversation = request(address, conversation_path, headers=admin)
                    self.assertIsNone(conversation["items"][0]["assistant_message"])
                    self.assertIn("hello", conversation["items"][0]["outbox"]["draft_text"])
                    for bad_path in (list_path + "?limit=101", list_path + "?limit=bad",
                                     conversation_path + "&before_seq=-1", conversation_path + "&before_seq=" + str(2**63)):
                        self.assertEqual(request(address, bad_path, headers=admin)[0], 400)
                    self.assertEqual(request(address, "/api/v2/runtime/conversation?conversation_id=missing", headers=admin)[0], 404)
                    command = {"action": "bind", "conversation_id": "alice", "character_id": "default", "request_id": "bind-http"}
                    self.assertEqual(request(address, "/api/v2/runtime/commands", method="POST", payload=command, headers=viewer)[0], 403)
                    self.assertEqual(request(address, "/api/v2/runtime/commands", method="POST", payload=command, headers=admin)[0], 200)
                    self.assertEqual(request(address, "/api/v2/runtime/commands", method="POST",
                        payload={"action": "retry", "outbox_id": outbox_id, "note": "not checked"}, headers=admin)[0], 409)
                    self.assertEqual(request(address, "/api/v2/runtime/commands", method="POST",
                        payload={"action": "discard", "outbox_id": outbox_id, "note": "stale",
                                 "expected_attempts": 2, "expected_state": "unknown"}, headers=admin)[0], 409)
                    self.assertEqual(request(address, "/api/v2/runtime/commands", method="POST",
                        payload={"action": "discard", "outbox_id": outbox_id, "note": "reviewed"}, headers=admin)[0], 200)
                    service.private_runtime.host.wait_idle(5)
                    self.assertEqual(service.private_runtime.host.status()["conversations"][0]["generation"], 2)
                    _, reviewed = request(address, detail_path, headers=admin)
                    self.assertEqual(reviewed["reviews"][0]["action"], "discard")
                    self.assertEqual(reviewed["reviews"][0]["note"], "reviewed")
                    memory_path = "/api/v2/runtime/memories?conversation_id=alice&character_id=default"
                    self.assertEqual(request(address, memory_path)[0], 401)
                    self.assertEqual(request(address, memory_path, headers=viewer)[0], 403)
                    self.assertEqual(request(address, memory_path + "&limit=-1", headers=admin)[0], 400)
                    memory_command = {"action": "memory", "conversation_id": "alice", "character_id": "default",
                        "source_event_id": result["outbox"][0]["event_id"], "request_id": "memory-http", "note": "operator reviewed",
                        "actor": "forged-other-user", "change": {"op": "create", "memory_id": "", "expected_version": 0,
                            "kind": "fact", "title": "Greeting", "text": "User greeted", "quote": "hello", "state": "active"}}
                    self.assertEqual(request(address, "/api/v2/runtime/commands", method="POST", payload=memory_command, headers=viewer)[0], 403)
                    status, admitted = request(address, "/api/v2/runtime/commands", method="POST", payload=memory_command, headers=admin)
                    self.assertEqual(status, 200)
                    service.private_runtime.host.wait_idle(5)
                    _, memories = request(address, memory_path, headers=admin)
                    self.assertEqual(len(memories["records"]), 1)
                    memory_id = memories["records"][0]["memory_id"]
                    _, revision = request(address, memory_path + "&memory_id=" + memory_id, headers=admin)
                    self.assertEqual(revision["revisions"][0]["actor"], "admin")
                    self.assertEqual(request(address, memory_path.replace("alice", "bob") + "&memory_id=" + memory_id, headers=admin)[0], 404)
                    _, duplicate = request(address, "/api/v2/runtime/commands", method="POST", payload=memory_command, headers=admin)
                    self.assertTrue(duplicate["duplicate"])
                    self.assertEqual(duplicate["event_id"], admitted["event_id"])
                    reminder_path = "/api/v2/runtime/reminders?conversation_id=alice"
                    self.assertEqual(request(address, reminder_path)[0], 401)
                    self.assertEqual(request(address, reminder_path, headers=viewer)[0], 403)
                    self.assertEqual(request(address, reminder_path + "&limit=-1", headers=admin)[0], 400)
                    reminder = {"action": "reminder", "operation": "create", "conversation_id": "alice",
                        "character_id": "default", "source_event_id": result["outbox"][0]["event_id"], "evidence": "hello",
                        "text": "Test only", "when": "+10m", "timezone": "UTC", "allow_quiet": True, "authorized": True,
                        "note": "operator verified permission", "request_id": "reminder-http", "actor": "forged"}
                    def reminder_command(body, headers=admin):
                        return request(address, "/api/v2/runtime/commands", method="POST", payload=body, headers=headers)
                    self.assertEqual(reminder_command(reminder, {})[0], 401)
                    self.assertEqual(reminder_command(reminder, viewer)[0], 403)
                    self.assertEqual(reminder_command({**reminder, "authorized": False})[0], 409)
                    self.assertEqual(reminder_command({**reminder, "evidence": "not in source"})[0], 409)
                    status, created = reminder_command(reminder)
                    self.assertEqual(status, 200, created)
                    task = created["task"]
                    self.assertEqual(task["actor"], "admin")
                    self.assertEqual(task["state"], "scheduled")
                    self.assertTrue(reminder_command(reminder)[1]["duplicate"])
                    self.assertEqual(request(address, reminder_path + "&task_id=" + task["task_id"], headers=admin)[1]["requests"][0]["actor"], "admin")
                    self.assertEqual(request(address, reminder_path.replace("alice", "bob") + "&task_id=" + task["task_id"], headers=admin)[0], 404)
                    update = {"action": "reminder", "operation": "postpone", "conversation_id": "alice", "task_id": task["task_id"],
                        "expected_version": 1, "when": "+20m", "timezone": "UTC", "allow_quiet": True, "authorized": True, "note": "reschedule", "request_id": "postpone-http"}
                    self.assertEqual(reminder_command(update)[1]["task"]["version"], 2)
                    self.assertEqual(reminder_command({**update, "request_id": "stale"})[0], 409)
                    cancelled = {**update, "operation": "cancel", "expected_version": 2, "request_id": "cancel-http"}
                    self.assertEqual(reminder_command(cancelled)[1]["task"]["state"], "cancelled")
                    self.assertTrue(reminder_command(cancelled)[1]["duplicate"])
                    self.assertEqual(request(address, "/api/memory/purge", method="POST", payload={}, headers=admin)[0], 409)
                    contact_path = "/api/v2/runtime/contact-policy?conversation_id=alice"
                    self.assertEqual(request(address, contact_path)[0], 401)
                    self.assertEqual(request(address, contact_path, headers=viewer)[0], 403)
                    self.assertEqual(request(address, contact_path.replace("alice", "missing"), headers=admin)[0], 404)
                    status, preferences = request(address, contact_path, headers=admin)
                    self.assertEqual(status, 200, preferences)
                    self.assertFalse(preferences["policy"]["proactive_enabled"])
                    contact = {"action": "contact_policy", "operation": "update", "conversation_id": "alice",
                        "request_id": "contact-http", "expected_version": 1, "expected_data_generation": 1, "actor": "forged",
                        "changes": {"proactive_enabled": True, "timezone": "UTC", "daily_limit": 5, "quiet_enabled": False},
                        "authorized": True, "note": "User confirmed preferences"}
                    self.assertEqual(reminder_command(contact, {})[0], 401)
                    self.assertEqual(reminder_command(contact, viewer)[0], 403)
                    self.assertEqual(reminder_command({**contact, "authorized": False})[0], 409)
                    self.assertEqual(reminder_command({**contact, "conversation_id": "bob"})[0], 409)
                    self.assertEqual(reminder_command(contact)[0], 200)
                    self.assertTrue(reminder_command(contact)[1]["duplicate"])
                    self.assertEqual(reminder_command({**contact, "request_id": "stale-contact"})[0], 409)
                    _, preferences = request(address, contact_path, headers=admin)
                    self.assertEqual(preferences["audit"][0]["actor"], "admin")
                    followup_path = "/api/v2/runtime/followups?conversation_id=alice"
                    self.assertEqual(request(address, followup_path)[0], 401)
                    self.assertEqual(request(address, followup_path, headers=viewer)[0], 403)
                    self.assertEqual(request(address, followup_path.replace("alice", "missing"), headers=admin)[0], 404)
                    self.assertEqual(request(address, followup_path + "&limit=101", headers=admin)[0], 409)
                    episode = {**memory_command, "request_id": "episode-http", "change": {**memory_command["change"],
                        "kind": "episode", "state": "open", "title": "Greeting to revisit"}}
                    self.assertEqual(reminder_command(episode)[0], 200)
                    service.private_runtime.host.wait_idle(5)
                    _, candidates = request(address, followup_path, headers=admin)
                    candidate = candidates["candidates"][0]
                    self.assertEqual(candidate["state"], "candidate")
                    self.assertIsNone(candidate["due_at"])
                    time_command = {"action": "episode_time", "operation": "confirm", "conversation_id": "alice",
                        "memory_id": candidate["memory_id"], "request_id": "episode-time-http", "expected_memory_version": candidate["memory_version"],
                        "expected_timing_version": candidate["timing"]["version"], "expected_data_generation": candidates["data_generation"],
                        "expected_policy_version": candidates["policy_version"], "expected_binding_generation": candidates["binding_generation"],
                        "expected_next_seq": candidates["next_seq"], "precision": "date", "date": "2026-10-27", "period": "", "clock": "", "fold": None,
                        "confirmation_source_event_id": candidate["source_event_id"], "confirmation_quote": candidate["evidence"],
                        "authorized": True, "note": "Synthetic administrator time confirmation", "actor": "forged"}
                    self.assertEqual(reminder_command(time_command, {})[0], 401)
                    self.assertEqual(reminder_command(time_command, viewer)[0], 403)
                    self.assertEqual(reminder_command({**time_command, "authorized": False})[0], 409)
                    self.assertEqual(reminder_command({**time_command, "confirmation_quote": "not in user source"})[0], 409)
                    status, confirmed_time = reminder_command(time_command)
                    self.assertEqual(status, 200, confirmed_time)
                    self.assertEqual(confirmed_time["timing"]["confirmed"]["precision"], "date")
                    self.assertNotIn("utc_at", confirmed_time["timing"]["confirmed"])
                    self.assertTrue(reminder_command(time_command)[1]["duplicate"])
                    self.assertEqual(reminder_command({**time_command, "request_id": "stale-time"})[0], 409)
                    _, time_detail = request(address, followup_path + "&candidate_id=" + candidate["candidate_id"], headers=admin)
                    self.assertEqual(time_detail["time_audit"][0]["actor"], "admin")
                    candidate = time_detail["candidate"]
                    self.assertIsNone(candidate["due_at"])
                    followup = {"action": "followup", "operation": "schedule", "conversation_id": "alice", "request_id": "followup-http",
                        "candidate_id": candidate["candidate_id"], "expected_version": candidate["version"], "expected_data_generation": 1,
                        "expected_policy_version": candidates["policy_version"], "when": "+31m", "text": "Reviewed follow-up", "authorized": True,
                        "note": "Reviewed source and timing", "actor": "forged"}
                    from waifu_standalone.runtime_v2.followup_drafts import DraftResult
                    async def fake_draft(context, before_call):
                        await before_call({"builder_version": "http-test", "status": "prepared"})
                        return DraftResult(text="Generated for review only")
                    draft_call = AsyncMock(side_effect=fake_draft)
                    service.private_runtime.host.engine.generator.generate_followup = draft_call
                    draft_command = {**followup, "operation": "generate", "request_id": "draft-http"}
                    self.assertEqual(reminder_command(draft_command, {})[0], 401)
                    self.assertEqual(reminder_command(draft_command, viewer)[0], 403)
                    self.assertEqual(reminder_command({**draft_command, "authorized": False})[0], 409)
                    status, draft = reminder_command(draft_command)
                    self.assertEqual(status, 200, draft)
                    self.assertEqual(draft["draft"]["actor"], "admin")
                    service.private_runtime.host.wait_idle(5)
                    self.assertTrue(reminder_command(draft_command)[1]["duplicate"])
                    self.assertEqual(draft_call.await_count, 1)
                    _, review = request(address, followup_path + "&candidate_id=" + candidate["candidate_id"], headers=admin)
                    self.assertEqual(review["drafts"][0]["state"], "ready")
                    self.assertIsNone(review["candidate"]["due_at"])
                    followup["draft_job_id"] = draft["draft"]["job_id"]
                    self.assertEqual(reminder_command(followup, {})[0], 401)
                    self.assertEqual(reminder_command(followup, viewer)[0], 403)
                    self.assertEqual(reminder_command({**followup, "authorized": False})[0], 409)
                    self.assertEqual(reminder_command(followup)[0], 200)
                    self.assertTrue(reminder_command(followup)[1]["duplicate"])
                    self.assertEqual(reminder_command({**followup, "request_id": "stale-followup"})[0], 409)
                    status, review = request(address, followup_path + "&candidate_id=" + candidate["candidate_id"], headers=admin)
                    self.assertEqual(status, 200)
                    self.assertEqual(review["requests"][0]["actor"], "admin")
                    self.assertEqual(request(address, followup_path.replace("alice", "bob") + "&candidate_id=" + candidate["candidate_id"], headers=admin)[0], 404)
                    pause = {**contact, "operation": "pause", "changes": {}, "request_id": "pause-http"}
                    self.assertTrue(reminder_command(pause)[1]["policy"]["paused"])  # Emergency stop accepts stale version.
                    self.assertEqual(reminder_command({**pause, "operation": "resume", "request_id": "stale-resume"})[0], 409)
                    privacy_path = "/api/v2/runtime/privacy?conversation_id=alice"
                    self.assertEqual(request(address, privacy_path)[0], 401)
                    self.assertEqual(request(address, privacy_path, headers=viewer)[0], 403)
                    self.assertEqual(request(address, privacy_path.replace("alice", "missing"), headers=admin)[0], 404)
                    _, before = request(address, privacy_path, headers=admin)
                    erasure = {"action": "privacy", "operation": "erase", "conversation_id": "alice", "request_id": "erase-http",
                        "preview_token": before["preview_token"], "confirmed": True, "confirm_conversation_id": "alice", "actor": "forged"}
                    self.assertEqual(reminder_command(erasure, {})[0], 401)
                    self.assertEqual(reminder_command(erasure, viewer)[0], 403)
                    self.assertEqual(reminder_command({**erasure, "confirmed": False})[0], 409)
                    self.assertEqual(reminder_command({**erasure, "preview_token": "stale"})[0], 409)
                    status, erased = reminder_command(erasure)
                    self.assertEqual(status, 200, erased)
                    self.assertEqual(erased["state"], "erased")
                    self.assertEqual(erased["deletions"][0]["actor"], "admin")
                    self.assertEqual(request(address, conversation_path, headers=admin)[1]["items"], [])
                    self.assertEqual(request(address, memory_path, headers=admin)[1]["records"], [])
                    self.assertEqual(request(address, reminder_path, headers=admin)[1]["tasks"], [])
                    self.assertEqual(request(address, followup_path, headers=admin)[1]["candidates"], [])
                    self.assertEqual(request(address, contact_path, headers=admin)[0], 409)
                    paused_message = {**payload, "message_id": "paused", "message": "DO NOT STORE PAUSED MESSAGE"}
                    self.assertIn(request(address, "/onebot/events", method="POST", payload=paused_message,
                        headers={"Authorization": "Bearer test-inbound"})[0], (202, 204))
                    service.private_runtime.host.wait_idle(5)
                    self.assertEqual(request(address, conversation_path, headers=admin)[1]["items"], [])
                    resume = {"action": "privacy", "operation": "resume", "conversation_id": "alice", "character_id": "default", "expected_generation": 2}
                    self.assertEqual(reminder_command({**resume, "expected_generation": 1})[0], 409)
                    self.assertEqual(reminder_command(resume)[1]["state"], "active")
                    self.assertFalse(request(address, contact_path, headers=admin)[1]["policy"]["proactive_enabled"])
                    self.assertEqual(reminder_command(followup)[0], 409)
                    self.assertEqual(reminder_command(time_command)[0], 409)
                    self.assertEqual(reminder_command(contact)[0], 409)  # Old authorization cannot cross data generations.
                    self.assertTrue(reminder_command(erasure)[1]["duplicate"])
                    self.assertIn(request(address, "/onebot/events", method="POST", payload=paused_message,
                        headers={"Authorization": "Bearer test-inbound"})[0], (202, 204))
                    self.assertEqual(request(address, conversation_path, headers=admin)[1]["items"], [])
                finally:
                    server.shutdown()
                    thread.join(5)
                    server.server_close()
                    service.close()


if __name__ == "__main__":
    unittest.main()
