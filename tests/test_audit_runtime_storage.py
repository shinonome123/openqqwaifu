from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_file_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.session_lock import SessionLock
from waifu_standalone.memory import ROLLING_SUMMARY_KEY, FileMemoryStore
from waifu_standalone.models import InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from waifu_standalone.reply_flow.prompt_assembler import GeneratedReply
from waifu_standalone.repositories.runtime_state import SqliteRuntimeStateStore
from waifu_standalone.skills.telemetry import record_skill_telemetry_event
from waifu_standalone.web.http_api import HttpApi


def event(message_id="", launcher_type="person"):
    return InboundEvent(launcher_id="10001", launcher_type=launcher_type, sender_id="10001",
                        sender_name="tester", message_id=message_id,
                        segments=[MessageSegment(kind="text", text="hello")])


def onebot_payload(message_id):
    return {"post_type": "message", "message_type": "private", "user_id": 10001, "message_id": message_id,
            "sender": {"nickname": "tester"}, "message": [{"type": "text", "data": {"text": "hello"}}]}


def config(root):
    cfg = AppConfig(data_root=root)
    cfg.thinking_mode = False
    cfg.conversation_analysis = False
    cfg.knowledge_auto_extract = False
    cfg.member_auto_sync = False
    return cfg


class AsyncRuntimeRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.svc, self.outbound = build_file_service(config(self.temp.name))

    async def asyncTearDown(self):
        await asyncio.to_thread(self.svc.close, timeout=3)
        self.temp.cleanup()

    async def test_same_session_waiters_do_not_exhaust_worker_pool(self):
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(*args, **kwargs):
            entered.set()
            await release.wait()
            await asyncio.to_thread(lambda: None)

        with patch.object(type(self.svc), "_handle_event_async_locked", side_effect=process):
            first = asyncio.create_task(self.svc.handle_event_async(event()))
            await entered.wait()
            waiters = [asyncio.create_task(self.svc.handle_event_async(event())) for _ in range(5)]
            await asyncio.sleep(0.02)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, *waiters), 2)

    async def test_canceled_waiter_does_not_acquire_or_leak_lock(self):
        lock = SessionLock()
        await lock.acquire()
        waiting = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        lock.release()
        await asyncio.wait_for(lock.acquire(), 1)
        lock.release()
        self.assertFalse(lock.locked())

    async def test_cancel_after_handoff_releases_granted_lock(self):
        lock = SessionLock()
        await lock.acquire()
        waiting = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        lock.release()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(lock.acquire(), 1)
        lock.release()

    async def test_character_switch_rejected_until_reply_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def reply(*args, **kwargs):
            entered.set()
            await release.wait()
            return GeneratedReply(text="old-character-answer")

        with patch.object(self.svc.generator, "agenerate_reply_message", side_effect=reply):
            task = asyncio.create_task(self.svc.handle_event_async(event()))
            await entered.wait()
            with self.assertRaisesRegex(ValueError, "runtime busy"):
                self.svc.activate_character("second")
            self.assertEqual(self.svc._active_character_id(), "default")
            release.set()
            await task
        self.svc.activate_character("second")
        self.assertEqual(self.svc.memory.load("10001", "person").history, [])
        self.svc.activate_character("default")
        self.assertTrue(any("old-character-answer" in line for line in self.svc.memory.load("10001", "person").history))

    async def test_cancel_during_reply_retains_storage_lease_until_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def reply(*args, **kwargs):
            entered.set()
            await release.wait()
            return GeneratedReply(text="completed-before-cancel")

        with patch.object(self.svc.generator, "agenerate_reply_message", side_effect=reply):
            task = asyncio.create_task(self.svc.handle_event_async(event("cancel-id")))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            with self.assertRaisesRegex(ValueError, "runtime busy"):
                self.svc.activate_character("second")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(any("completed-before-cancel" in line for line in self.svc.memory.load("10001", "person").history))
        self.svc.activate_character("second")

    async def test_same_message_id_is_handled_once_but_failed_operation_can_retry(self):
        # Duplicates are dropped by HttpApi's inbound deduplicator, shared with the service.
        api = HttpApi(self.svc)
        callback = AsyncMock(return_value=GeneratedReply(text="reply"))
        with patch.object(self.svc.generator, "agenerate_reply_message", callback):
            await api.handle_json_async(onebot_payload(1))
            _, body = await api.handle_json_async(onebot_payload(1))
            self.assertEqual(body["reason"], "duplicate message")
            self.assertEqual(callback.await_count, 1)
        self.assertEqual(len(self.outbound.sent), 1)
        with patch.object(type(self.svc), "_handle_event_async_locked", new=AsyncMock(side_effect=ConnectionError("offline"))):
            status, _ = await api.handle_json_async(onebot_payload(2))
            self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        callback = AsyncMock(return_value=None)
        with patch.object(type(self.svc), "_handle_event_async_locked", callback):
            await api.handle_json_async(onebot_payload(2))
        self.assertEqual(callback.await_count, 1)

    async def test_delayed_failure_does_not_record_a_delivered_reply(self):
        class FailingOutbound:
            async def send_async(self, message):
                raise ConnectionError("offline")
        self.svc.outbound = FailingOutbound()
        self.svc.config.group_response_delay_seconds = 0.01
        await self.svc.emitter.aemit(event(launcher_type="group"),
            OutboundMessage(launcher_id="10001", launcher_type="group", text="not-delivered"),
            assistant_name="Assistant")
        await asyncio.to_thread(self.svc.flush_background_tasks, timeout=2)
        self.assertEqual(self.svc.memory.load("10001", "group").history, [])
        self.assertEqual(self.svc._recent_outbound, [])

    async def test_cancel_delayed_delivery_waits_for_inflight_storage_worker(self):
        started, release = threading.Event(), threading.Event()
        original = self.svc.memory.save_assistant_message

        def blocked_save(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test timed out")
            return original(*args, **kwargs)

        with patch.object(self.svc.memory, "save_assistant_message", side_effect=blocked_save):
            task = asyncio.create_task(self.svc.emitter._deliver_after_delay(
                event(), OutboundMessage(launcher_id="10001", launcher_type="person", text="delivered"),
                delay_seconds=0, assistant_name="Assistant"))
            self.svc._track_async_task("test-delivery", task)
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                with self.assertRaisesRegex(ValueError, "runtime busy"):
                    self.svc.activate_character("second")
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(any("delivered" in line for line in self.svc.memory.load("10001", "person").history))

    async def test_shutdown_rejects_new_events_and_drains_active_reply(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(*args, **kwargs):
            entered.set()
            await release.wait()

        with patch.object(type(self.svc), "_handle_event_async_locked", side_effect=process):
            task = asyncio.create_task(self.svc.handle_event_async(event()))
            await entered.wait()
            shutdown = asyncio.create_task(self.svc.wait_idle(timeout=2))
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                await self.svc.handle_event_async(event())
            release.set()
            await asyncio.gather(task, shutdown)

    async def test_delivered_message_is_not_resent_when_bookkeeping_fails(self):
        api = HttpApi(self.svc)
        with patch.object(self.svc.generator, "agenerate_reply_message", new=AsyncMock(return_value=GeneratedReply(text="delivered"))):
            with patch.object(self.svc.memory, "asave_assistant_message", new=AsyncMock(side_effect=OSError("disk failure"))):
                status, _ = await api.handle_json_async(onebot_payload(3))
                self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
            _, body = await api.handle_json_async(onebot_payload(3))
            self.assertEqual(body["reason"], "duplicate message")
        self.assertEqual(len(self.outbound.sent), 1)

class StorageRegressionTests(unittest.TestCase):
    def test_torn_jsonl_tail_recovers_and_next_save_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileMemoryStore(tmp)
            session = SessionMemory(launcher_id="1", launcher_type="person", history=["user: saved"])
            store.save(session)
            path = store.session_path("1", "person")
            with path.open("ab") as stream:
                stream.write(b'{"history":["torn\xe4')
            self.assertEqual(store.load("1", "person").history, session.history)
            session.history.append("assistant: next")
            store.save(session)
            self.assertEqual(store.load("1", "person").history, session.history)

    def test_activating_current_character_preserves_history_and_rebinds_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, _ = build_file_service(config(tmp))
            try:
                svc.memory.save_user_event(event())
                history = svc.memory.load("10001", "person").history
                svc.save_character_panel({"character": "default", "set_active": True})
                self.assertEqual(svc.memory.load("10001", "person").history, history)
                previous = svc.state_store
                svc.activate_character("second")
                record_skill_telemetry_event({"skill_id": "new-role", "status": "ok"})
                self.assertEqual(previous.list_skill_telemetry(skill_id="new-role"), [])
                self.assertEqual(len(svc.state_store.list_skill_telemetry(skill_id="new-role")), 1)
            finally:
                svc.close()

    def test_ai_refresh_keeps_tools_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, _ = build_file_service(config(tmp))
            try:
                svc.save_ai_panel({"llm": {"model": "updated-model"}})
                self.assertIs(svc.generator._tools, svc.tools)
            finally:
                svc.close()

    def test_persona_name_substring_does_not_delete_member_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, _ = build_file_service(config(tmp))
            try:
                session = SessionMemory(launcher_id="1", launcher_type="person", character_id="default",
                    history=["Joanna: hello", "assistant: reply"])
                with patch.object(svc.persona, "known_aliases", return_value={"default": {"Default"}, "other": {"Ann"}}):
                    actual = svc.persona.sanitize_session_state(session, assistant_name="Default")
                self.assertEqual(actual.history, ["Joanna: hello", "assistant: reply"])
            finally:
                svc.close()

    def test_failed_summary_save_preserves_history_and_retry_folds_once(self):
        # The rolling summary and the trimmed history are written in one session save,
        # so a failed save leaves both untouched and a retry folds the batch once.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            cfg.summarization_mode = True
            cfg.short_term_memory_limit = cfg.memory_summary_batch_size = 2
            svc, _ = build_file_service(cfg)
            history = ["user: first", "assistant: first", "user: recent", "assistant: recent"]
            svc.memory.store.save(SessionMemory(launcher_id="1", launcher_type="person", character_id="default", history=history))
            try:
                with patch.object(svc.generator, "summarize_rolling", return_value="summary") as summarize:
                    save = svc.memory.store.save
                    def fail_trim(session):
                        if len(session.history) == 2:
                            raise OSError("snapshot failure")
                        return save(session)
                    with patch.object(svc.memory.store, "save", side_effect=fail_trim):
                        with self.assertRaises(OSError):
                            svc._archive_history("1", "person", assistant_name="Assistant", character_id="default")
                    failed = svc.memory.load("1", "person")
                    self.assertEqual(failed.history, history)
                    self.assertNotIn(ROLLING_SUMMARY_KEY, failed.metadata)
                    svc._archive_history("1", "person", assistant_name="Assistant", character_id="default")
                    self.assertEqual(summarize.call_count, 2)
                folded = svc.memory.load("1", "person")
                self.assertEqual(folded.history, history[-2:])
                self.assertEqual(folded.metadata[ROLLING_SUMMARY_KEY], "summary")
                self.assertEqual(svc.state_store.knowledge_count(), 0)
            finally:
                svc.close()

    def test_embedding_wait_does_not_block_unrelated_member_write(self):
        started, release, member_done = threading.Event(), threading.Event(), threading.Event()
        class Embedder:
            ready = True
            def embed(self, text):
                started.set()
                if not release.wait(3):
                    raise TimeoutError("test timeout")
                return [1.0, 0.0]
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRuntimeStateStore(Path(tmp) / "state.sqlite3", embedder=Embedder())
            errors = []
            def knowledge():
                try:
                    store.add_knowledge(scope_type="person", scope_id="1", memory_type="fact", summary="fact", tags=[])
                except Exception as exc:
                    errors.append(exc)
            def member():
                store.record_member_seen(group_id="", user_id="2")
                member_done.set()
            first = threading.Thread(target=knowledge)
            second = threading.Thread(target=member)
            try:
                first.start()
                self.assertTrue(started.wait(2))
                second.start()
                self.assertTrue(member_done.wait(1), "member write held behind embedding")
            finally:
                release.set()
                first.join(3)
                if second.ident:
                    second.join(3)
                store.close()
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
