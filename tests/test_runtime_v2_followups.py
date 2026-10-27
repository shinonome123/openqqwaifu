from __future__ import annotations

import json
import shutil
import sqlite3
import unittest

from tests import test_runtime_v2_contact_policy as contact_tests
from tests.test_runtime_v2 import character
from tests.test_runtime_v2_memory import change
from tests.test_conversation_context import builder
from waifu_standalone.runtime_v2 import followups
from waifu_standalone.runtime_v2.contracts import ReplyProposal
from waifu_standalone.models import OutboundMessage


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    setUp = contact_tests.ContactPolicyTests.setUp
    tearDown = contact_tests.ContactPolicyTests.tearDown
    open_store = contact_tests.ContactPolicyTests.open_store
    incoming = contact_tests.ContactPolicyTests.incoming
    detail = contact_tests.ContactPolicyTests.detail
    request = contact_tests.ContactPolicyTests.request
    reopen = contact_tests.ContactPolicyTests.reopen

    async def episode(self, text="面试周三", user="alice", **changes):
        source = await self.incoming(text, user=user)
        self.store.enqueue_memory_change(user, "alpha", source.event_id, change(text, **changes), "m:" + source.event_id, "admin", "source checked")
        await self.engine.step(user)
        return self.store.memories(user, "alpha")["records"][0]

    def candidates(self, user="alice"):
        return self.store.followup_list(user)["candidates"]

    def candidate(self, candidate_id):
        return self.store.followup_list("alice", candidate_id=candidate_id)["candidate"]

    def schedule(self, candidate_id=None, **overrides):
        item = self.candidate(candidate_id) if candidate_id else self.candidates()[0]
        payload = {"conversation_id": "alice", "request_id": f"schedule:{item['candidate_id']}:{item['version']}",
            "candidate_id": item["candidate_id"], "expected_version": item["version"], "expected_data_generation": self.detail()["data_generation"],
            "expected_policy_version": self.detail()["policy"]["version"],
            "operation": "schedule", "when": "+31m", "text": "上次聊到的面试，最近有什么进展吗？", "authorized": True,
            "note": "User enabled follow-ups; reviewed source, wording and time", **overrides}
        return self.store.followup_request(payload, "admin"), payload

    async def approved(self, **overrides):
        await self.episode()
        self.request({"proactive_enabled": True})
        result, payload = self.schedule(**overrides)
        return result["candidate"], payload

    def tick(self, seconds=0):
        self.now += seconds
        return self.store.tick_reminders()

    async def revise(self, memory, *, text="改到周四", state="open", process=True, op="revise"):
        source = await self.incoming(text)
        self.store.enqueue_memory_change("alice", "alpha", source.event_id,
            change(text, op=op, state=state, memory_id=memory["memory_id"], expected_version=memory["version"]),
            "revision:" + source.event_id, "admin", "corrected")
        if process:
            await self.engine.step("alice")

    async def test_open_episodes_form_unique_unscheduled_candidates_without_permission(self):
        record = await self.episode()
        original = self.candidates()[0]
        self.assertEqual(original["source_event_id"], record["source_event_id"])
        self.assertEqual(original["evidence"], "面试周三")
        self.assertIsNone(original["due_at"])
        self.assertEqual(original["state"], "candidate")
        for _ in range(4):
            self.tick()
        self.reopen()
        self.tick()
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(self.candidates()[0]["candidate_id"], original["candidate_id"])
        with self.assertRaises(ValueError):
            self.schedule()
        self.tick(3600)
        self.assertFalse(await self.engine.step("alice"))
        self.assertEqual(len(self.sender.sent), 1)

    async def test_facts_closed_episodes_and_old_sources_do_not_create_candidates(self):
        await self.episode("喜欢茶", kind="fact", state="active")
        await self.episode("面试已结束", state="completed")
        self.assertEqual(self.candidates(), [])
        record = await self.episode("尚未结束")
        self.tick(31 * 86400)
        self.assertEqual(self.candidates()[0]["state"], "expired")
        with self.assertRaises(ValueError):
            self.schedule()

    async def test_model_memory_proposal_creates_candidate_but_not_scheduled_work(self):
        await self.incoming("面试周三", process=False)
        work = self.store.next_work("alice", "cfg")
        self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "希望顺利"), (change(),))
        item = self.candidates()[0]
        self.assertEqual(item["state"], "candidate")
        self.assertIsNone(item["due_at"])
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])

    async def test_reviewed_draft_sends_once_after_restart_without_model_or_fake_user(self):
        item, _ = await self.approved()
        self.reopen()
        self.assertEqual(self.tick(31 * 60), 1)
        original = self.store.next_work("alice", "cfg")["outbox"]
        self.reopen()
        self.assertEqual(self.store.next_work("alice", "cfg")["outbox"]["outbox_id"], original["outbox_id"])
        await self.engine.step("alice")
        self.assertEqual(self.sender.sent[-1].text, item["text"])
        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "completed")
        detail = self.store.conversation_detail("alice")["items"][-1]
        self.assertEqual(detail["kind"], "followup_due")
        self.assertNotIn("user_message", detail)
        await self.incoming("谢谢", process=False)
        work = self.store.next_work("alice", "cfg")
        bundle = builder().build(work["context"], work["event"], self.store.history(work["context"]))
        self.assertTrue(any(m.role == "assistant" and item["text"] in m.content_json for m in bundle.messages))

    async def test_corrected_episode_suppresses_sent_followup_in_future_model_context(self):
        item, _ = await self.approved()
        memory = self.store.memories("alice", "alpha")["records"][0]
        self.tick(31 * 60)
        await self.engine.step("alice")
        await self.revise(memory, text="面试取消了", state="cancelled")
        await self.incoming("说说新的安排", process=False)
        work = self.store.next_work("alice", "cfg")
        bundle = builder().build(work["context"], work["event"], self.store.history(work["context"]))
        self.assertNotIn(item["text"], json.dumps(bundle.wire_messages(), ensure_ascii=False))
        self.assertIn("面试取消了", json.dumps(bundle.wire_messages(), ensure_ascii=False))

    async def test_dismiss_during_uncertain_send_still_suppresses_future_revisions(self):
        item, _ = await self.approved()
        memory = self.store.memories("alice", "alpha")["records"][0]
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.store.begin_dispatch(outbox["outbox_id"])
        self.store.finish_delivery(outbox["outbox_id"], "unknown")
        result, _ = self.schedule(item["candidate_id"], operation="dismiss")
        self.assertEqual(result["status"], "needs_review")
        self.store.resolve_delivery(outbox["outbox_id"], "discard", "stop")
        await self.revise(memory)
        self.assertEqual(len(self.candidates()), 1)

    async def test_runtime_gate_and_pending_binding_are_checked_before_dispatch(self):
        item, _ = await self.approved()
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.store.followup_gate = lambda _: False
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertEqual(self.candidate(item["candidate_id"])["reason"], "runtime_disabled")
        self.store.followup_gate = None
        second, _ = await self.approved()
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.store.enqueue_binding("alice", character("beta"), "pending-bind")
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        await self.engine.step("alice")
        self.tick()
        self.assertEqual(self.candidate(second["candidate_id"])["reason"], "binding_changed")

    async def test_new_input_after_preparation_defers_and_frees_queue(self):
        item, _ = await self.approved()
        self.tick(31 * 60)
        old = self.store.next_work("alice", "cfg")["outbox"]
        await self.incoming("我有新情况", process=False)
        self.assertFalse(self.store.begin_dispatch(old["outbox_id"]))
        self.assertEqual(self.candidate(item["candidate_id"])["reason"], "conversation_busy")
        await self.engine.step("alice")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.candidate(item["candidate_id"])["reason"], "recent_interaction")
        self.assertEqual(self.tick(30 * 60), 1)
        await self.engine.step("alice")
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "completed")

    async def test_revision_invalidates_old_draft_and_creates_unapproved_replacement(self):
        item, _ = await self.approved()
        memory = self.store.memories("alice", "alpha")["records"][0]
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        await self.revise(memory)
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "invalidated")
        current = self.candidates()[0]
        self.assertEqual(current["memory_version"], 2)
        self.assertEqual(current["state"], "candidate")
        self.assertEqual(current["evidence"], "改到周四")
        self.assertEqual(self.tick(3600), 0)

    async def test_completed_cancelled_and_withdrawn_episodes_stop_followups(self):
        for state in ("completed", "cancelled", "withdrawn"):
            with self.subTest(state=state):
                item, _ = await self.approved()
                memory = self.store.memories("alice", "alpha")["records"][0]
                await self.revise(memory, state=state, op="withdraw" if state == "withdrawn" else "revise")
                self.assertEqual(self.candidate(item["candidate_id"])["reason"], "episode_closed")
                self.assertEqual(self.tick(31 * 60), 0)

    async def test_pause_waits_but_opt_out_cancels_and_reenabling_does_not_revive(self):
        item, _ = await self.approved()
        self.tick(31 * 60)
        old = self.store.next_work("alice", "cfg")["outbox"]
        await self.incoming("/contact pause")
        self.assertFalse(self.store.begin_dispatch(old["outbox_id"]))
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "scheduled")
        await self.incoming("/contact resume")
        self.tick(30 * 60)
        await self.incoming("/contact off")
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "cancelled")
        await self.incoming("/contact on")
        self.assertEqual(self.tick(31 * 60), 0)

    async def test_quiet_changes_delay_without_extending_window_or_blocking_chat(self):
        item, _ = await self.approved()
        self.request({"quiet_start": "20:00", "quiet_end": "21:00"})
        self.assertEqual(self.tick(31 * 60), 0)
        waiting = self.candidate(item["candidate_id"])
        self.assertEqual(waiting["reason"], "contact_quiet")
        self.assertEqual(waiting["expires_at"], item["expires_at"])
        self.assertEqual(self.tick(29 * 60), 1)
        await self.engine.step("alice")
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "completed")

    async def test_missed_window_does_not_recover_as_a_burst(self):
        item, _ = await self.approved()
        self.request(operation="pause")
        self.assertEqual(self.tick(31 * 60 + followups.WINDOW_SECONDS + 1), 0)
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "expired")
        self.request(operation="resume")
        self.assertEqual(self.tick(), 0)

    async def test_daily_cap_and_two_unanswered_followups_wait_for_new_input(self):
        # Three episodes from one conversation, all reviewed before any send.
        await self.episode("经历一")
        await self.episode("经历二")
        await self.episode("经历三")
        self.request({"proactive_enabled": True, "quiet_enabled": False})
        items = list(reversed(self.candidates()))
        for index, item in enumerate(items):
            self.schedule(item["candidate_id"], when=f"+{31 + index * 1440}m")
        self.tick(31 * 60)
        await self.engine.step("alice")
        self.assertEqual(followups.decision(self.store._db, "alice", self.now)["reason"], "contact_interval")
        self.tick(60)
        self.assertEqual(followups.decision(self.store._db, "alice", self.now)["reason"], "followup_daily_limit")
        self.tick(86400 - 60)
        await self.engine.step("alice")
        self.reopen()
        self.assertEqual(self.tick(86400), 0)
        self.assertEqual(self.candidate(items[2]["candidate_id"])["reason"], "awaiting_interaction")
        await self.incoming("我回来了")
        self.assertEqual(self.tick(30 * 60), 1)
        await self.engine.step("alice")
        self.assertEqual(self.candidate(items[2]["candidate_id"])["state"], "completed")

    async def test_shared_budget_counts_followup_and_reminder_attempts_together(self):
        item, _ = await self.approved()
        self.request({"daily_limit": 1})
        source = await self.incoming("请提醒我喝水")
        reminder = self.store.reminder_request({"conversation_id": "alice", "request_id": "r1", "operation": "create",
            "source_event_id": source.event_id, "evidence": "请提醒我喝水", "text": "喝水", "when": "+31m", "authorized": True, "note": "requested"}, "admin")
        self.tick(31 * 60)
        await self.engine.step("alice")
        self.assertEqual(self.store.reminder_list("alice", task_id=reminder["task"]["task_id"])["task"]["state"], "completed")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.candidate(item["candidate_id"])["reason"], "contact_daily_limit")
        self.assertEqual(self.detail()["attempts_24h"], 1)

    async def test_unknown_send_restart_and_revocation_keep_review_barrier(self):
        item, _ = await self.approved()
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.assertTrue(self.store.begin_dispatch(outbox["outbox_id"]))
        self.reopen()
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "needs_review")
        self.assertEqual(self.tick(), 0)
        await self.incoming("/contact off", process=False)
        self.assertTrue(self.candidate(item["candidate_id"])["cancel_requested"])
        self.assertIsNone(self.store.next_work("alice", "cfg"))
        with self.assertRaises(ValueError):
            self.store.resolve_delivery(outbox["outbox_id"], "retry", "reviewed")
        self.store.resolve_delivery(outbox["outbox_id"], "discard", "uncertain, do not retry")
        await self.engine.step("alice")

    async def test_cancel_before_dispatch_and_dismissed_episode_never_reproposed(self):
        item, _ = await self.approved()
        memory = self.store.memories("alice", "alpha")["records"][0]
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.schedule(item["candidate_id"], operation="dismiss")
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        await self.revise(memory)
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "dismissed")

    async def test_binding_switch_invalidates_and_switching_back_does_not_revive(self):
        item, _ = await self.approved()
        self.store.enqueue_binding("alice", character("beta"), "bind")
        await self.engine.step("alice")
        self.tick()
        self.assertEqual(self.candidate(item["candidate_id"])["reason"], "binding_changed")
        self.store.enqueue_binding("alice", character(), "back")
        await self.engine.step("alice")
        self.assertEqual(self.tick(31 * 60), 0)

    async def test_review_idempotency_scope_version_time_and_authorization_validation(self):
        item, payload = await self.approved()
        self.assertTrue(self.store.followup_request(payload, "admin")["duplicate"])
        with self.assertRaises(ValueError):
            self.store.followup_request(payload, "forged")
        for overrides in ({"authorized": False}, {"operation": []}, {"expected_version": 1}, {"text": ""},
                          {"expected_data_generation": 0}, {"expected_policy_version": 0}, {"when": "+8d"}, {"when": "+3h"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.schedule(**overrides)
        await self.episode("另一位的经历", user="bob")
        bob = self.candidates("bob")[0]
        with self.assertRaises(ValueError):
            self.store.followup_request({**payload, "candidate_id": bob["candidate_id"], "request_id": "wrong-scope"}, "admin")
        self.schedule(item["candidate_id"], operation="cancel")
        self.assertTrue(self.store.followup_request(payload, "admin")["duplicate"])
        self.assertEqual(self.candidate(item["candidate_id"])["state"], "cancelled")

    async def test_erasure_and_old_database_restore_clear_all_followup_content(self):
        item, payload = await self.approved(text="SECRET approved draft")
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.assertTrue(self.store.begin_dispatch(outbox["outbox_id"]))
        backup = self.path.with_suffix(".backup")
        self.store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copyfile(self.path, backup)
        preview = self.store.privacy_status("alice")
        self.assertEqual(preview["counts"]["followup_candidates"], 1)
        self.store.erase_conversation({"conversation_id": "alice", "request_id": "erase", "confirmed": True,
            "confirm_conversation_id": "alice", "preview_token": preview["preview_token"]}, "admin")
        self.store.close()
        shutil.copyfile(backup, self.path)
        self.store = self.open_store()
        self.store.recover()
        self.assertNotIn("SECRET", "\n".join(self.store._db.iterdump()))
        self.assertEqual(self.store.followup_list("alice")["candidates"], [])
        self.assertEqual(self.store.delivery_detail(outbox["outbox_id"])["state"], "unknown")
        with self.assertRaises(ValueError):
            self.store.resolve_delivery(outbox["outbox_id"], "retry", "must not")
        self.store.resolve_delivery(outbox["outbox_id"], "discard", "closed")
        self.store.resume_conversation("alice", 2, character(), "admin")
        with self.assertRaises(ValueError):
            self.store.followup_request(payload, "admin")
        self.assertEqual(self.store._db.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_schema_six_upgrade_preserves_opt_in_without_scheduling_old_memories(self):
        await self.episode()
        self.request({"proactive_enabled": True})
        self.store.close()
        db = sqlite3.connect(self.path)
        for table in ("episode_time_dialogues", "episode_time_dialogue_turns", "episode_time_requests", "episode_times", "followup_drafts", "followup_requests", "followup_occurrences", "followup_candidates"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=6")
        db.commit()
        db.close()
        self.store = self.open_store()
        self.assertEqual(self.store.snapshot()["schema_version"], 10)
        self.assertTrue(self.detail()["policy"]["proactive_enabled"])
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.candidates()[0]["state"], "candidate")
        self.assertIsNone(self.candidates()[0]["due_at"])


if __name__ == "__main__":
    unittest.main()
