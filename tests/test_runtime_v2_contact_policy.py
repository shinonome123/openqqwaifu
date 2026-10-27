from __future__ import annotations

import shutil
import sqlite3
import unittest
from datetime import datetime, timezone

from tests import test_runtime_v2_reminders as reminder_tests
from tests.test_runtime_v2 import character
from waifu_standalone.runtime_v2 import contact_policy
from waifu_standalone.runtime_v2.engine import ConversationEngine
from waifu_standalone.runtime_v2.store import RuntimeStore

START = reminder_tests.START


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class ContactPolicyTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the deterministic clock and fake transports without inheriting the
    # unrelated test methods (and rerunning that suite).
    setUp = reminder_tests.DurableReminderTests.setUp
    tearDown = reminder_tests.DurableReminderTests.tearDown
    open_store = reminder_tests.DurableReminderTests.open_store
    incoming = reminder_tests.DurableReminderTests.incoming
    create = reminder_tests.DurableReminderTests.create
    task = reminder_tests.DurableReminderTests.task
    update = reminder_tests.DurableReminderTests.update
    due = reminder_tests.DurableReminderTests.due

    def detail(self, user="alice"):
        return self.store.contact_policy_detail(user)

    def request(self, changes=None, operation="update", **overrides):
        d = self.detail()
        payload = {"conversation_id": "alice", "request_id": f"policy-{d['policy']['version']}", "operation": operation,
            "expected_version": d["policy"]["version"], "expected_data_generation": d["data_generation"],
            "authorized": True, "note": "User confirmed these preferences", "changes": changes or {}, **overrides}
        return self.store.contact_request(payload, "admin"), payload

    def reopen(self):
        self.store.close()
        self.store = self.open_store()
        self.store.recover()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "restarted")

    async def test_default_opt_out_still_allows_explicit_reminders(self):
        task, _ = await self.create()
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])
        self.assertEqual(self.detail()["proactive_decision"]["reason"], "proactive_disabled")
        self.due()
        await self.engine.step("alice")
        self.assertEqual(self.task(task["task_id"])["state"], "completed")
        self.assertEqual(self.detail()["attempts_24h"], 1)
        self.assertEqual(self.detail()["remaining_24h"], 19)

    async def test_pause_at_admission_retires_prepared_occurrence_and_frees_queue(self):
        task, _ = await self.create()
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        await self.incoming("/contact pause", process=False)
        self.assertTrue(self.detail()["policy"]["paused"])
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertEqual(self.task(task["task_id"])["reason"], "contact_paused")
        version = self.task(task["task_id"])["version"]
        for _ in range(4):
            self.store.tick_reminders()
        self.assertEqual(self.task(task["task_id"])["version"], version)
        await self.engine.step("alice")
        await self.incoming("normal chat")
        self.assertIn("normal chat", self.sender.sent[-1].text)
        await self.incoming("/contact resume")
        self.assertEqual(self.store.tick_reminders(), 1)
        await self.engine.step("alice")
        self.assertEqual(self.task(task["task_id"])["state"], "completed")
        self.assertEqual(len(self.generator.calls), 2)  # Source and ordinary chat only.

    async def test_pause_during_inflight_retains_unknown_barrier_and_blocks_retry(self):
        task, _ = await self.create()
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.assertTrue(self.store.begin_dispatch(outbox["outbox_id"]))
        await self.incoming("/contact pause", process=False)
        self.store.finish_delivery(outbox["outbox_id"], "unknown")
        self.now += 60
        with self.assertRaisesRegex(ValueError, "contact_paused"):
            self.store.resolve_delivery(outbox["outbox_id"], "retry", "reviewed")
        self.assertEqual(self.task(task["task_id"])["state"], "needs_review")
        self.assertIsNone(self.store.next_work("alice", "cfg"))
        self.assertEqual(self.detail()["attempts_24h"], 1)
        self.store.resolve_delivery(outbox["outbox_id"], "discard", "not sending")
        await self.engine.step("alice")
        self.assertIn("已暂停", self.sender.sent[-1].text)

    async def test_saturated_queue_cannot_block_emergency_pause(self):
        task, _ = await self.create()
        self.store.max_pending = 1
        self.due()
        self.assertEqual(self.store.snapshot()["counts"]["queued"], 1)
        await self.incoming("/contact pause", process=False)
        self.assertTrue(self.detail()["policy"]["paused"])
        self.assertEqual(self.task(task["task_id"])["state"], "scheduled")
        self.assertEqual(self.store.ready_conversations(), [])
        self.assertEqual(self.detail()["audit"][0]["operation"], "pause")
        self.reopen()
        self.assertTrue(self.detail()["policy"]["paused"])

    async def test_invalid_reminder_zone_type_and_missing_policy_mutation_fail_cleanly(self):
        with self.assertRaises(ValueError):
            await self.create(timezone=False)
        with self.assertRaises(ValueError):
            self.store.contact_request({"conversation_id": "missing", "request_id": "invalid", "operation": "pause"}, "admin")

    async def test_commands_are_isolated_deterministic_and_idempotent(self):
        await self.incoming("/contact on Asia/Shanghai")
        await self.incoming("/contact pause", user="bob")
        self.assertEqual(self.generator.calls, [])
        self.assertTrue(self.detail()["policy"]["proactive_enabled"])
        self.assertFalse(self.detail()["policy"]["paused"])
        self.assertFalse(self.detail("bob")["policy"]["proactive_enabled"])
        self.assertTrue(self.detail("bob")["policy"]["paused"])
        admission = await self.incoming("/contact off", process=False)
        await self.incoming("/contact on", process=False)
        version = self.detail()["policy"]["version"]
        await self.engine.step("alice")  # Old 'off' response must not undo new 'on'.
        self.assertTrue(self.detail()["policy"]["proactive_enabled"])
        self.assertEqual(self.detail()["policy"]["version"], version)
        await self.engine.step("alice")
        await self.incoming("他说：/contact pause")
        self.assertFalse(self.detail()["policy"]["paused"])
        self.assertEqual(len(self.generator.calls), 1)
        self.assertTrue(admission.event_id)

    async def test_validation_and_stale_writes_cannot_override_pause(self):
        await self.incoming()
        result, payload = self.request({"daily_limit": 5, "min_interval_minutes": 30})
        self.assertTrue(self.store.contact_request(payload, "admin")["duplicate"])
        with self.assertRaises(ValueError):
            self.store.contact_request(payload, "someone_else")
        for changes in ({"daily_limit": True}, {"daily_limit": 101}, {"min_interval_minutes": 0},
                        {"quiet_start": "9:00"}, {"quiet_start": "08:00"}, {"timezone": "bad/zone"},
                        {"proactive_enabled": 1}, {"paused": False}, {"quiet_enabled": "false"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.request(changes)
        paused, _ = self.request(operation="pause", expected_version=0)
        self.assertTrue(paused["policy"]["paused"])
        with self.assertRaises(ValueError):
            self.request(operation="resume", expected_version=result["policy"]["version"])
        with self.assertRaises(ValueError):
            self.request(operation="pause", expected_data_generation=0)
        with self.assertRaises(ValueError):
            self.request(operation=[])

    async def test_missing_zone_fails_closed_and_failed_command_is_not_reapplied(self):
        self.store.close()
        self.store = RuntimeStore(self.path, "bot", "capture", reminders_enabled=True, clock=lambda: self.now)
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "cfg")
        await self.incoming("/contact on", process=False)
        await self.incoming("/contact timezone UTC", process=False)
        await self.engine.step("alice")
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])
        self.assertIn("未更改", self.sender.sent[-1].text)
        await self.engine.step("alice")
        await self.incoming("/remind +1m | water", user="bob")
        self.assertEqual(self.store.reminder_list("bob")["tasks"], [])
        await self.incoming("/remind +1m UTC | water", user="bob")
        self.assertEqual(len(self.store.reminder_list("bob")["tasks"]), 1)
        await self.incoming("/contact on")
        self.assertTrue(self.detail()["policy"]["proactive_enabled"])
        self.assertEqual(self.generator.calls, [])

    async def test_per_conversation_zone_is_used_by_new_relative_reminders(self):
        await self.incoming("/contact timezone America/New_York")
        await self.incoming("/remind +1m | water")
        self.assertEqual(self.store.reminder_list("alice")["tasks"][0]["timezone"], "America/New_York")

    async def test_quiet_exception_is_explicit_per_time_and_never_bypasses_pause_or_limit(self):
        self.now = instant("2026-10-26T15:00:00")  # 23:00 Shanghai
        with self.assertRaisesRegex(ValueError, "allow-quiet"):
            await self.create()
        task, _ = await self.create(allow_quiet=True)
        with self.assertRaisesRegex(ValueError, "allow-quiet"):
            self.update(task["task_id"], "postpone", when="+2m")
        self.request({"daily_limit": 1})
        self.request(operation="pause")
        self.assertEqual(self.due(), 0)
        self.request(operation="resume")
        self.assertEqual(self.store.tick_reminders(), 1)
        await self.engine.step("alice")
        second, _ = await self.create(allow_quiet=True)
        self.assertEqual(self.due(), 0)
        self.assertEqual(self.task(second["task_id"])["reason"], "contact_daily_limit")

    async def test_quiet_policy_change_defers_and_releases_without_changing_deadline(self):
        self.now = instant("2026-10-26T23:57:00")  # 07:57 Shanghai
        await self.incoming("/contact quiet off")
        task, _ = await self.create()
        self.due()
        old = self.store.next_work("alice", "cfg")["outbox"]
        self.request({"quiet_enabled": True})
        self.assertFalse(self.store.begin_dispatch(old["outbox_id"]))
        waiting = self.task(task["task_id"])
        self.assertEqual(waiting["due_at"], task["due_at"])
        self.assertEqual(waiting["expires_at"], task["expires_at"])
        self.assertEqual(waiting["deferred_until"], instant("2026-10-27T00:00:00"))
        self.assertEqual(waiting["reason"], "contact_quiet")
        self.now += 120
        self.assertEqual(self.store.tick_reminders(), 1)
        await self.engine.step("alice")
        self.assertEqual(self.task(task["task_id"])["state"], "completed")

    async def test_paused_tasks_expire_and_resume_does_not_revive_other_invalid_tasks(self):
        task, _ = await self.create()
        cancelled, _ = await self.create()
        self.update(cancelled["task_id"], "cancel")
        self.request(operation="pause")
        self.due(361)
        self.assertEqual(self.task(task["task_id"])["state"], "expired")
        self.request(operation="resume")
        self.assertEqual(self.store.tick_reminders(), 0)
        self.assertEqual(self.task(cancelled["task_id"])["state"], "cancelled")

    async def test_quota_survives_restart_retry_and_rolling_window(self):
        task, _ = await self.create(allow_quiet=True)
        self.request({"daily_limit": 2})
        self.due()
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.assertTrue(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.store.finish_delivery(outbox["outbox_id"], "rejected")
        with self.assertRaisesRegex(ValueError, "contact_interval"):
            self.store.resolve_delivery(outbox["outbox_id"], "retry", "check")
        self.now += 60
        self.store.resolve_delivery(outbox["outbox_id"], "retry", "check")
        self.sender.fail = True
        await self.engine.step("alice")
        self.reopen()
        self.now += 60
        with self.assertRaisesRegex(ValueError, "contact_daily_limit"):
            self.store.resolve_delivery(outbox["outbox_id"], "retry", "check")
        self.assertEqual(self.detail()["attempts_24h"], 2)
        self.request({"timezone": "America/New_York"})
        self.assertEqual(self.detail()["remaining_24h"], 0)
        self.now = START + 60 + 86400
        self.assertEqual(self.detail()["attempts_24h"], 1)
        self.now += 60
        self.assertEqual(self.detail()["attempts_24h"], 0)

    async def test_dispatch_rechecks_policy_after_approved_retry(self):
        task, _ = await self.create()
        self.due()
        self.sender.fail = True
        await self.engine.step("alice")
        box = self.store.reminder_list("alice", task_id=task["task_id"])["occurrences"][0]["outbox_id"]
        self.now += 60
        self.store.resolve_delivery(box, "retry", "reviewed")
        self.request({"min_interval_minutes": 10})
        self.assertFalse(self.store.begin_dispatch(box))
        self.assertEqual(self.task(task["task_id"])["reason"], "contact_interval")
        self.assertEqual(self.detail()["attempts_24h"], 1)

    async def test_delete_restore_replays_policy_erasure_and_resume_defaults(self):
        task, _ = await self.create()
        self.request({"proactive_enabled": True})
        _, old_payload = self.request({"daily_limit": 5})
        self.due()
        await self.engine.step("alice")
        backup = self.path.with_suffix(".backup")
        self.store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copyfile(self.path, backup)
        preview = self.store.privacy_status("alice")
        self.assertEqual(preview["counts"]["contact_attempts"], 1)
        self.store.erase_conversation({"conversation_id": "alice", "request_id": "erase", "confirmed": True,
            "confirm_conversation_id": "alice", "preview_token": preview["preview_token"]}, "admin")
        self.store.close()
        shutil.copyfile(backup, self.path)
        self.store = self.open_store()
        for table in ("contact_policies", "contact_requests", "contact_attempts"):
            self.assertEqual(self.store._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.store.resume_conversation("alice", 2, character(), "admin")
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])
        self.assertEqual(self.detail()["policy"]["daily_limit"], 20)
        self.assertEqual(self.detail()["audit"], [])
        with self.assertRaisesRegex(ValueError, "代次"):
            self.store.contact_request(old_payload, "admin")
        self.assertEqual(self.store._db.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_schema_five_upgrade_is_conservative_and_imports_attempts(self):
        task, _ = await self.create()
        self.due()
        await self.engine.step("alice")
        waiting, _ = await self.create(when="+2h", allow_quiet=True)
        self.store.close()
        db = sqlite3.connect(self.path)
        for table in ("episode_time_dialogues", "episode_time_dialogue_turns", "episode_time_requests", "episode_times", "followup_drafts", "followup_requests", "followup_occurrences", "followup_candidates", "contact_attempts", "contact_requests", "contact_policies"):
            db.execute(f"DROP TABLE {table}")
        for column in ("allow_quiet", "timezone_confirmed", "deferred_until"):
            db.execute(f"ALTER TABLE reminder_tasks DROP COLUMN {column}")
        db.execute("PRAGMA user_version=5")
        db.commit()
        db.close()
        self.store = self.open_store()
        self.assertEqual(self.store.snapshot()["schema_version"], 10)
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])
        self.assertFalse(self.task(waiting["task_id"])["allow_quiet"])
        self.assertEqual(self.detail()["attempts_24h"], 1)
        self.now = waiting["due_at"]
        self.assertEqual(self.store.tick_reminders(), 0)
        self.assertEqual(self.task(waiting["task_id"])["reason"], "contact_quiet")


class ContactTimeTests(unittest.TestCase):
    def test_overnight_boundaries_and_short_daytime_quiet(self):
        for stamp, expected in (("2026-10-26T13:59:59", False), ("2026-10-26T14:00:00", True),
                                ("2026-10-26T23:59:59", True), ("2026-10-27T00:00:00", False)):
            self.assertEqual(contact_policy.in_quiet("Asia/Shanghai", "22:00", "08:00", instant(stamp)), expected)
        self.assertTrue(contact_policy.in_quiet("UTC", "12:00", "13:00", START))

    def test_dst_repeated_and_skipped_hours_use_real_instants(self):
        zone = "America/New_York"
        for stamp in ("2026-11-01T05:15:00", "2026-11-01T06:15:00"):
            self.assertTrue(contact_policy.in_quiet(zone, "01:00", "01:30", instant(stamp)))
            end = contact_policy.next_open(zone, "01:00", "01:30", int(instant(stamp) // 60))
            self.assertEqual(end, instant(stamp) + 15 * 60)
        end = contact_policy.next_open(zone, "22:00", "02:30", int(instant("2027-03-14T06:59:00") // 60))
        self.assertEqual(end, instant("2027-03-14T07:00:00"))

    def test_quotes_and_long_commands_are_not_pause_authorization(self):
        self.assertIsNone(contact_policy.parse_command("我说‘不要主动联系我’只是举例"))
        self.assertIsNone(contact_policy.parse_command("不要主动联系我好吗"))
        self.assertEqual(contact_policy.parse_command("不要主动联系我"), {"operation": "pause"})
        self.assertEqual(contact_policy.parse_command("/contact pause please"), {"operation": "help"})


if __name__ == "__main__":
    unittest.main()
