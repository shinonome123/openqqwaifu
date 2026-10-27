from __future__ import annotations

import json
import sqlite3
import threading
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from dataclasses import asdict, replace

from tests.test_runtime_v2 import FakeGenerator, FakeSender, character, event
from tests.test_runtime_v2_memory import change
from tests.test_conversation_context import builder
from waifu_standalone.runtime_v2.contracts import EventEnvelope
from waifu_standalone.runtime_v2.engine import ConversationEngine, RuntimeHost
from waifu_standalone.config import AppConfig, RuntimeV2Config
from waifu_standalone.settings_admin.config_manager import ConfigManager
from waifu_standalone.runtime_v2.reminder_time import parse_command, resolve_time
from waifu_standalone.runtime_v2.store import RuntimeStore, _SCHEMA_V1, _SCHEMA_V2
from waifu_standalone.runtime_v2.memories import SCHEMA_V3


START = datetime(2026, 10, 26, 12, tzinfo=timezone.utc).timestamp()


class ReminderClockTests(unittest.TestCase):
    def test_absolute_time_requires_zone_and_dst_ambiguity_is_explicit(self):
        with self.assertRaises(ValueError):
            resolve_time("2026-11-01T01:30", "", START)
        with self.assertRaisesRegex(ValueError, "两次"):
            resolve_time("2026-11-01T01:30", "America/New_York", START)
        first = resolve_time("2026-11-01T01:30", "America/New_York", START, 0)
        second = resolve_time("2026-11-01T01:30", "America/New_York", START, 1)
        self.assertEqual(second[0] - first[0], 3600)
        self.assertTrue(first[2].endswith("-04:00"))
        self.assertTrue(second[2].endswith("-05:00"))
        with self.assertRaisesRegex(ValueError, "不存在"):
            resolve_time("2027-03-14T02:30", "America/New_York", START)

    def test_relative_days_are_elapsed_time_not_host_timezone(self):
        due, zone, local = resolve_time("+1d", "", START)
        self.assertEqual(due - START, 86400)
        self.assertEqual(zone, "UTC")
        self.assertTrue(local.endswith("+00:00"))
        for value in ("+0m", "+999999d", "yesterday", "2020-01-01T09:00", "2026-12-01T09:00+08:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_time(value, "Asia/Shanghai", START)

    def test_parser_does_not_turn_plans_or_quotes_into_authorization(self):
        self.assertIsNone(parse_command("明天面试"))
        self.assertIsNone(parse_command("他说：10分钟后提醒我喝水"))
        self.assertEqual(parse_command("明天上午提醒我喝水")["operation"], "help")
        self.assertEqual(parse_command("10分钟后提醒我喝水")["when"], "+10m")


class DurableReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.now = START
        self.allowed = True
        self.store = self.open_store()
        self.sender, self.generator = FakeSender(), FakeGenerator()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "cfg")
        self.counter = 0

    def open_store(self):
        return RuntimeStore(self.path, "bot", "capture", reminders_enabled=True,
            reminder_timezone="Asia/Shanghai", reminder_gate=lambda _: self.allowed, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def incoming(self, text="请提醒我面试", *, user="alice", process=True):
        self.counter += 1
        envelope = replace(EventEnvelope.from_inbound("bot", event(str(self.counter), user=user, text=text)), received_at=self.now)
        admission = self.store.admit(envelope, character())
        if process:
            await self.engine.step(user)
        return admission

    def task(self, task_id):
        return self.store.reminder_list("alice", task_id=task_id)["task"]

    async def create(self, **kwargs):
        source = await self.incoming()
        payload = {"operation": "create", "conversation_id": "alice", "character_id": "alpha", "source_event_id": source.event_id,
                   "evidence": "请提醒我面试", "text": "准备面试", "when": "+1m", "timezone": "Asia/Shanghai", "authorized": True,
                   "note": "管理员确认用户要求", "request_id": "create-" + source.event_id, **kwargs}
        result = self.store.reminder_request(payload, "admin")
        return result["task"], payload

    def update(self, task_id, operation, **kwargs):
        task = self.task(task_id)
        return self.store.reminder_request({"operation": operation, "conversation_id": "alice", "task_id": task_id,
            "expected_version": task["version"], "authorized": True, "note": "人工核对", "request_id": f"{operation}-{task_id}-{task['version']}", **kwargs}, "admin")

    def due(self, seconds=60):
        self.now += seconds
        return self.store.tick_reminders()

    async def test_user_creation_uses_no_model_and_only_reply_body_is_sent(self):
        await self.incoming("1分钟后提醒我喝水")
        self.assertEqual(self.generator.calls, [])
        tasks = self.store.reminder_list("alice")["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["authorization_kind"], "user_command")
        self.assertIn(tasks[0]["task_id"], self.sender.sent[0].text)
        self.due()
        await self.engine.step("alice")
        self.assertEqual(self.sender.sent[-1].text, "提醒：喝水")
        self.assertEqual(self.generator.calls, [])
        self.assertEqual(self.task(tasks[0]["task_id"])["state"], "completed")

    async def test_due_event_is_unique_across_restart_and_not_fake_user_history(self):
        task, _ = await self.create()
        self.store.close()
        self.store = self.open_store()
        self.store.recover()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "restart")
        self.assertEqual(self.due(), 1)
        self.assertEqual(self.store.tick_reminders(), 0)
        await self.engine.step("alice")
        self.assertEqual(self.store.tick_reminders(), 0)
        self.assertEqual(len(self.sender.sent), 2)
        detail = self.store.conversation_detail("alice")["items"][-1]
        self.assertEqual(detail["kind"], "reminder_due")
        self.assertNotIn("user_message", detail)
        self.assertEqual(detail["assistant_message"]["delivery_state"], "captured")
        await self.incoming("谢谢提醒", process=False)
        work = self.store.next_work("alice", "cfg")
        bundle = builder().build(work["context"], work["event"], self.store.history(work["context"]))
        self.assertTrue(any(m.role == "assistant" and "提醒：准备面试" in m.content_json for m in bundle.messages))
        self.assertEqual(self.task(task["task_id"])["state"], "completed")

    async def test_cancel_prepared_outbox_wins_before_dispatch(self):
        task, _ = await self.create()
        self.due()
        work = self.store.next_work("alice", "cfg")
        result = self.update(task["task_id"], "cancel")
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(self.store.begin_dispatch(work["outbox"]["outbox_id"]))
        self.assertFalse(await self.engine.step("alice"))
        self.assertEqual(len(self.sender.sent), 1)

    async def test_prepared_outbox_is_reused_after_restart_without_generation(self):
        task, _ = await self.create()
        self.due()
        original = self.store.next_work("alice", "cfg")["outbox"]
        self.store.close()
        self.store = self.open_store()
        self.store.recover()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "restart")
        self.assertEqual(self.store.next_work("alice", "restart")["outbox"]["outbox_id"], original["outbox_id"])
        await self.engine.step("alice")
        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(self.task(task["task_id"])["state"], "completed")

    async def test_review_retry_is_allowed_only_inside_original_authorization_window(self):
        task, _ = await self.create()
        self.due()
        self.sender.fail = True
        await self.engine.step("alice")
        outbox_id = self.store.reminder_list("alice", task_id=task["task_id"])["occurrences"][0]["outbox_id"]
        self.now += 60  # A retry also obeys the contact interval.
        self.store.resolve_delivery(outbox_id, "retry", "test authorized retry")
        await self.engine.step("alice")
        self.assertEqual(self.store.delivery_detail(outbox_id)["attempts"], 2)
        self.now += 301
        with self.assertRaises(ValueError):
            self.store.resolve_delivery(outbox_id, "retry", "too late")
        self.store.resolve_delivery(outbox_id, "discard", "window exceeded")
        self.assertEqual(self.task(task["task_id"])["state"], "cancelled")

    async def test_default_disabled_refuses_creation_but_allows_cancellation(self):
        task, payload = await self.create()
        self.store.close()
        self.store = RuntimeStore(self.path, "bot", "capture", clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, "尚未启用"):
            self.store.reminder_request({**payload, "request_id": "disabled"}, "admin")
        self.assertFalse(self.store.reminder_list("alice")["enabled"])
        self.update(task["task_id"], "cancel")
        self.assertEqual(self.due(), 0)

    async def test_relative_request_uses_receipt_time_despite_queue_delay(self):
        await self.incoming("/remind +10m | 喝水", process=False)
        self.now += 180
        await self.engine.step("alice")
        task = self.store.reminder_list("alice")["tasks"][0]
        self.assertEqual(task["due_at"], START + 600)
        self.assertEqual(self.due(420), 1)
        await self.engine.step("alice")
        self.assertEqual(self.sender.sent[-1].text, "提醒：喝水")

    async def test_delayed_command_past_grace_does_not_silently_move_deadline(self):
        await self.incoming("/remind +1m | 喝水", process=False)
        self.now += 361
        await self.engine.step("alice")
        self.assertEqual(self.store.reminder_list("alice")["tasks"], [])
        self.assertIn("超过补发窗口", self.sender.sent[-1].text)

    async def test_user_cancellation_is_applied_at_admission_while_due_turn_is_waiting(self):
        task, _ = await self.create()
        self.due()
        work = self.store.next_work("alice", "cfg")
        await self.incoming("/remind cancel " + task["task_id"], process=False)
        self.assertEqual(self.task(task["task_id"])["state"], "cancelled")
        self.assertFalse(self.store.begin_dispatch(work["outbox"]["outbox_id"]))
        await self.engine.step("alice")
        self.assertIn("已取消", self.sender.sent[-1].text)
        self.assertEqual(len(self.generator.calls), 1)

    async def test_cancel_after_dispatch_reports_inflight_and_does_not_claim_recall(self):
        task, _ = await self.create()
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.assertTrue(self.store.begin_dispatch(outbox["outbox_id"]))
        result = self.update(task["task_id"], "cancel")
        self.assertEqual(result["status"], "in_flight")
        self.assertEqual(result["task"]["state"], "dispatching")
        self.store.finish_delivery(outbox["outbox_id"], "captured")
        self.assertEqual(self.task(task["task_id"])["state"], "completed")
        self.assertEqual(self.task(task["task_id"])["cancel_requested"], 1)

    async def test_unknown_send_recovery_never_retries_and_cancel_keeps_review_barrier(self):
        task, _ = await self.create()
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.store.begin_dispatch(outbox["outbox_id"])
        self.store.close()
        self.store = self.open_store()
        self.store.recover()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "cfg")
        self.assertEqual(self.task(task["task_id"])["state"], "needs_review")
        self.assertFalse(await self.engine.step("alice"))
        self.assertEqual(self.update(task["task_id"], "cancel")["status"], "needs_review")
        with self.assertRaises(ValueError):
            self.store.resolve_delivery(outbox["outbox_id"], "retry", "不能重复发")
        self.store.resolve_delivery(outbox["outbox_id"], "discard", "核对后放弃")
        self.assertEqual(self.task(task["task_id"])["state"], "cancelled")

    async def test_postpone_invalidates_old_occurrence_and_sends_new_version_only(self):
        task, _ = await self.create()
        self.due()
        old = self.store.next_work("alice", "cfg")["outbox"]
        await self.incoming(f"/remind postpone {task['task_id']} +5m", process=False)
        self.assertFalse(self.store.begin_dispatch(old["outbox_id"]))
        await self.engine.step("alice")
        self.assertIn("提醒已保存", self.sender.sent[-1].text)
        self.assertEqual(self.due(299), 0)
        self.assertEqual(self.due(1), 1)
        await self.engine.step("alice")
        self.assertEqual([m.text for m in self.sender.sent].count("提醒：准备面试"), 1)
        self.assertEqual(len(self.store.reminder_list("alice", task_id=task["task_id"])["occurrences"]), 2)

    async def test_stale_operator_or_duplicate_creation_cannot_resurrect_cancelled_task(self):
        task, payload = await self.create()
        self.update(task["task_id"], "cancel")
        duplicate = self.store.reminder_request(payload, "admin")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.task(task["task_id"])["state"], "cancelled")
        with self.assertRaises(ValueError):
            self.update(task["task_id"], "postpone", expected_version=1, when="+5m")

    async def test_binding_change_pauses_and_switching_back_does_not_auto_resume(self):
        task, _ = await self.create()
        self.store.enqueue_binding("alice", character("beta"), "beta")
        await self.engine.step("alice")
        self.due()
        self.assertEqual(self.task(task["task_id"])["reason"], "binding_changed")
        self.store.enqueue_binding("alice", character(), "alpha")
        await self.engine.step("alice")
        self.assertEqual(self.store.tick_reminders(), 0)
        self.update(task["task_id"], "postpone", when="+1m")
        self.due()
        await self.engine.step("alice")
        self.assertEqual(self.task(task["task_id"])["state"], "completed")

    async def test_pending_linked_episode_correction_blocks_already_prepared_send(self):
        source = await self.incoming("面试周三")
        self.store.enqueue_memory_change("alice", "alpha", source.event_id, change(), "memory", "admin", "确认")
        await self.engine.step("alice")
        memory = self.store.memories("alice", "alpha")["records"][0]
        task, _ = await self.create(linked_memory_id=memory["memory_id"])
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.store.enqueue_memory_change("alice", "alpha", source.event_id,
            change(op="revise", memory_id=memory["memory_id"], expected_version=1, state="completed"), "complete", "admin", "事件已结束")
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertEqual(self.task(task["task_id"])["reason"], "linked_episode_change_pending")
        await self.engine.step("alice")
        with self.assertRaises(ValueError):
            self.update(task["task_id"], "postpone", when="+5m")

    async def test_disable_between_preparation_and_send_blocks_delivery(self):
        task, _ = await self.create()
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.allowed = False
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertEqual(self.task(task["task_id"])["reason"], "reminders_disabled")

    async def test_expiration_and_spacing_prevent_restart_burst(self):
        first, _ = await self.create()
        second, _ = await self.create()
        self.assertEqual(self.due(180), 1)
        await self.engine.step("alice")
        self.assertTrue(self.sender.sent[-1].text.startswith("提醒（已延迟 2 分钟）"))
        self.assertEqual(self.store.tick_reminders(), 0)
        self.assertEqual(self.due(181), 0)
        self.assertEqual(self.task(second["task_id"])["state"], "expired")
        self.assertEqual(self.task(first["task_id"])["state"], "completed")

    async def test_prepared_outbox_expiration_and_scope_auth_are_rechecked(self):
        task, payload = await self.create()
        with self.assertRaises(ValueError):
            self.store.reminder_request({**payload, "request_id": "no-grant", "authorized": False}, "admin")
        await self.incoming(user="bob")
        with self.assertRaises(LookupError):
            self.store.reminder_list("bob", task_id=task["task_id"])
        with self.assertRaises(ValueError):
            self.store.reminder_request({**payload, "conversation_id": "bob", "request_id": "foreign"}, "admin")
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.now += 301
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertEqual(self.task(task["task_id"])["state"], "expired")

    async def test_ambiguous_command_requests_details_without_model_call_or_task(self):
        await self.incoming("明天上午提醒我面试")
        self.assertEqual(self.generator.calls, [])
        self.assertEqual(self.store.reminder_list("alice")["tasks"], [])
        self.assertIn("时区", self.sender.sent[-1].text)


class ReminderMigrationTests(unittest.TestCase):
    def test_default_and_config_roundtrip_and_environment_overlay(self):
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {}, clear=True):
            path = Path(root) / "config.json"
            config = AppConfig(runtime_v2=RuntimeV2Config())
            self.assertFalse(config.runtime_v2.reminders_enabled)
            config.runtime_v2.reminder_grace_seconds = 120
            path.write_text(json.dumps(asdict(config)), encoding="utf-8")
            loaded = ConfigManager(path).load().runtime_v2
            self.assertEqual(loaded, config.runtime_v2)
            with patch.dict("os.environ", {"OPENQQWAIFU_RUNTIME_V2_REMINDERS_ENABLED": "true",
                                           "OPENQQWAIFU_RUNTIME_V2_REMINDER_TIMEZONE": "Asia/Shanghai"}):
                loaded = ConfigManager(path).load().runtime_v2
                self.assertTrue(loaded.reminders_enabled)
                self.assertEqual(loaded.reminder_timezone, "Asia/Shanghai")

    def test_host_wakes_for_due_reminder_without_incoming_message(self):
        with tempfile.TemporaryDirectory() as root:
            now = [START]
            sent = threading.Event()
            class SignallingSender(FakeSender):
                async def send(self, message):
                    result = await super().send(message)
                    if message.text.startswith("提醒："):
                        sent.set()
                    return result
            store = RuntimeStore(Path(root) / "host.sqlite3", "bot", "capture", reminders_enabled=True, reminder_timezone="UTC", clock=lambda: now[0])
            generator, sender = FakeGenerator(), SignallingSender()
            host = RuntimeHost(ConversationEngine(store, generator, sender, "cfg"))
            try:
                envelope = replace(EventEnvelope.from_inbound("bot", event(text="/remind +1m | 喝水")), received_at=now[0])
                host.mutate(store.admit, envelope, character())
                host.wait_idle(3)
                self.assertFalse(sent.is_set())
                now[0] += 60
                self.assertTrue(sent.wait(3))
                host.wait_idle(3)
                self.assertEqual(generator.calls, [])
                self.assertEqual(store.reminder_list("alice")["tasks"][0]["state"], "completed")
            finally:
                host.close()

    def test_schema_three_upgrade_preserves_existing_tables(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "v3.sqlite3"
            db = sqlite3.connect(path)
            for statement in (*_SCHEMA_V1, *_SCHEMA_V2, *SCHEMA_V3):
                db.execute(statement)
            db.executemany("INSERT INTO runtime_meta VALUES(?,?)", [("account_id", "bot"), ("mode", "capture")])
            db.execute("PRAGMA user_version=3")
            db.commit()
            db.close()
            store = RuntimeStore(path, "bot", "capture")
            try:
                self.assertEqual(store.snapshot()["schema_version"], 10)
                self.assertEqual(store._db.execute("SELECT count(*) FROM memory_records").fetchone()[0], 0)
                self.assertEqual(store._db.execute("SELECT count(*) FROM reminder_tasks").fetchone()[0], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
