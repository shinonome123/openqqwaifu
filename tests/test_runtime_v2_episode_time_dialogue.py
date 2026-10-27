from __future__ import annotations

import json
import shutil
import sqlite3
import unittest

from tests import test_runtime_v2_episode_times as time_tests
from tests.test_runtime_v2 import FakeGenerator, character, event
from tests.test_runtime_v2_memory import change
from tests.test_conversation_context import builder
from waifu_standalone.models import MessageSegment, OutboundMessage
from waifu_standalone.runtime_v2 import episode_time_dialogue, episode_times, privacy
from waifu_standalone.runtime_v2.contracts import EventEnvelope, ReplyProposal
from waifu_standalone.runtime_v2.store import RuntimeStore


class EpisodeTimeDialogueTests(unittest.IsolatedAsyncioTestCase):
    setUp = time_tests.EpisodeTimeTests.setUp
    tearDown = time_tests.EpisodeTimeTests.tearDown
    open_store = time_tests.EpisodeTimeTests.open_store
    incoming = time_tests.EpisodeTimeTests.incoming
    detail = time_tests.EpisodeTimeTests.detail
    request = time_tests.EpisodeTimeTests.request
    reopen = time_tests.EpisodeTimeTests.reopen
    episode = time_tests.EpisodeTimeTests.episode
    candidates = time_tests.EpisodeTimeTests.candidates
    candidate = time_tests.EpisodeTimeTests.candidate
    schedule = time_tests.EpisodeTimeTests.schedule
    tick = time_tests.EpisodeTimeTests.tick
    timing = time_tests.EpisodeTimeTests.timing
    confirm = time_tests.EpisodeTimeTests.confirm
    time_payload = time_tests.EpisodeTimeTests.time_payload
    draft_request = time_tests.EpisodeTimeTests.draft_request

    def answer(self):
        return self.sender.sent[-1].text

    def pending(self, user="alice"):
        row = self.store._db.execute("SELECT state_json FROM episode_time_dialogues WHERE conversation_id=?", (user,)).fetchone()
        return json.loads(row[0]) if row else None

    async def test_user_can_clarify_and_confirm_without_proactive_permission_or_model(self):
        memory = await self.episode("过几天面试", title="面试")
        calls = len(self.generator.calls)
        await self.incoming("核对经历时间")
        self.assertIn("具体是哪一天", self.answer())
        source = await self.incoming("明天下午")
        self.assertIn("2026-10-27 下午", self.answer())
        self.assertIn("Asia/Shanghai", self.answer())
        self.assertEqual(self.timing(memory)["state"], "proposed")
        approval = await self.incoming("确认时间")
        confirmed = self.timing(memory)["confirmed"]
        self.assertEqual((confirmed["date"], confirmed["precision"]), ("2026-10-27", "period"))
        self.assertEqual(confirmed["confirmation_source_event_id"], source.event_id)
        self.assertEqual(confirmed["approval_event_id"], approval.event_id)
        self.assertNotIn("utc_at", confirmed)
        self.assertEqual(len(self.generator.calls), calls)
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])
        self.assertIsNone(self.candidates()[0]["due_at"])
        self.assertIsNone(self.pending())
        audit = episode_times.audit(self.store._db, "alice", memory["memory_id"])
        self.assertEqual(audit[0]["actor"], "user:alice")
        await self.incoming("面试准备", process=False)
        work = self.store.next_work("alice", "cfg")
        bundle = builder().build(work["context"], work["event"], self.store.history(work["context"]))
        evidence = next(m for m in bundle.messages if m.layer == "memory_evidence")
        self.assertEqual(evidence.role, "user")
        self.assertIn(approval.event_id, evidence.source_ids)
        self.assertIn("user_confirmed_in_dialogue", evidence.content_json)
        self.assertNotIn("2026-10-27", json.dumps(bundle.report()))

    async def test_single_model_episode_asks_in_same_reply_and_survives_restart(self):
        class Extractor(FakeGenerator):
            async def generate(inner, context, incoming, history):
                inner.calls.append((context, incoming, history))
                return ReplyProposal(OutboundMessage("alice", "person", "希望面试顺利。"),
                                     (change("明天下午面试", title="面试"),))
        self.engine.generator = self.generator = Extractor()
        await self.incoming("明天下午面试")
        memory = self.store.memories("alice", "alpha")["records"][0]
        self.assertIn("希望面试顺利", self.answer())
        self.assertIn("确认时间", self.answer())
        self.reopen()
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["state"], "confirmed")
        self.assertEqual(len(self.generator.calls), 1)

    async def test_multiple_extracted_episodes_do_not_implicitly_choose_one(self):
        await self.incoming("明天面试，后天考试", process=False)
        work = self.store.next_work("alice", "cfg")
        out = self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "知道了"),
            (change("明天面试", title="面试"), change("后天考试", title="考试")))
        self.assertIsNone(self.pending())
        self.assertEqual(json.loads(out["payload_json"])["text"], "知道了")

    async def test_ambiguous_weekday_requires_choice_then_separate_confirmation(self):
        memory = await self.episode("周三面试", title="面试")
        self.now += 86400  # Original quote retains its Monday anchor.
        await self.incoming("核对面试的时间")
        self.assertIn("2026-10-28", self.answer())
        self.assertIn("2026-11-04", self.answer())
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["state"], "proposed")
        selected = await self.incoming("2")
        self.assertIn("2026-11-04", self.answer())
        self.assertEqual(self.timing(memory)["state"], "proposed")
        self.reopen()
        await self.incoming("确认时间")
        value = self.timing(memory)["confirmed"]
        self.assertEqual(value["date"], "2026-11-04")
        self.assertEqual(value["selection_event_id"], selected.event_id)
        self.assertEqual(value["confirmation_source_event_id"], memory["source_event_id"])
        self.assertNotIn("utc_at", value)

    async def test_clarification_relative_date_uses_its_own_receipt(self):
        memory = await self.episode("过几天面试")
        self.now += 86400
        await self.incoming("核对经历时间")
        source = await self.incoming("改成明天上午九点半")
        await self.incoming("确认时间")
        value = self.timing(memory)["confirmed"]
        self.assertEqual((value["date"], value["clock"]), ("2026-10-28", "09:30"))
        self.assertEqual(value["confirmation_source_event_id"], source.event_id)

    async def test_multiple_episodes_are_selected_by_number_and_same_title_is_ambiguous(self):
        first = await self.episode("明天面试", title="面试")
        second = await self.episode("后天面试", title="面试")
        await self.incoming("核对面试的时间")
        self.assertEqual(self.pending()["stage"], "select")
        await self.incoming("2")
        await self.incoming("确认时间")
        self.assertEqual(self.timing(first)["state"], "confirmed")
        self.assertEqual(self.timing(second)["state"], "proposed")

    async def test_casual_acknowledgement_or_topic_change_never_confirms(self):
        memory = await self.episode("明天面试")
        for text in ("好的", "嗯", "我明天去吃饭", "他说确认时间", "确认时间然后提醒我", "明天？天气怎么样", "确认时间？", "确认时间?"):
            with self.subTest(text=text):
                await self.incoming("核对经历时间")
                calls = len(self.generator.calls)
                await self.incoming(text)
                self.assertEqual(len(self.generator.calls), calls + 1)
                self.assertIsNone(self.pending())
                self.assertEqual(self.timing(memory)["state"], "proposed")
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["state"], "proposed")

    async def test_mixed_segments_and_forwarded_confirmation_are_not_authority(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        incoming = event("mixed", text="确认时间")
        incoming.segments.append(MessageSegment("reply", text="old question"))
        self.store.admit(EventEnvelope.from_inbound("bot", incoming), character())
        await self.engine.step("alice")
        self.assertIsNone(self.pending())
        self.assertEqual(self.timing(memory)["state"], "proposed")

    async def test_cancel_and_negative_allow_correction_without_early_write(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        await self.incoming("不对")
        self.assertEqual(self.pending()["stage"], "time")
        await self.incoming("后天下午")
        await self.incoming("取消")
        self.assertEqual(self.timing(memory)["state"], "proposed")
        await self.incoming("核对经历时间")
        await self.incoming("改成后天")
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["confirmed"]["date"], "2026-10-28")

    async def test_vague_or_conditional_original_never_becomes_confirmable_guess(self):
        for text in ("可能明天下午面试", "明天9:00到11:00面试", "过几天面试"):
            memory = await self.episode(text, title=text)
            await self.incoming(f"核对{text}的时间")
            self.assertEqual(self.pending()["stage"], "time")
            await self.incoming("确认时间")
            self.assertEqual(self.timing(memory)["state"], "proposed")
            await self.incoming("取消")

    async def test_no_timezone_requires_explicit_zone_without_enabling_contact(self):
        self.store.close()
        self.store = RuntimeStore(self.path, "bot", "capture", clock=lambda: self.now)
        self.engine.store = self.store
        await self.episode("明天面试")
        await self.incoming("核对经历时间")
        self.assertIn("/contact timezone", self.answer())
        self.assertIsNone(self.pending())
        await self.incoming("/contact timezone Asia/Shanghai")
        await self.incoming("核对经历时间")
        self.assertEqual(self.pending()["stage"], "confirm")
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])

    async def test_dst_ambiguity_is_explicit_and_nonexistent_clock_is_rejected(self):
        memory = await self.episode("过几天面试")
        self.request({"timezone": "America/New_York"})
        await self.incoming("核对经历时间")
        await self.incoming("2026-03-08 02:30")
        self.assertEqual(self.pending()["options"], [])
        await self.incoming("2026-11-01 01:30")
        self.assertIn("第一次", self.answer())
        self.assertIn("第二次", self.answer())
        await self.incoming("2")
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["confirmed"]["fold"], 1)

    async def test_expired_question_cannot_be_confirmed_after_restart(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        self.now += episode_time_dialogue.TTL + 1
        self.reopen()
        await self.incoming("确认时间")
        self.assertIn("已失效", self.answer())
        self.assertEqual(self.timing(memory)["state"], "proposed")

    async def test_prequeued_answer_does_not_approve_question_sent_later(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间", process=False)
        await self.incoming("确认时间", process=False)
        await self.engine.step("alice")
        await self.engine.step("alice")
        self.assertIn("已失效", self.answer())
        self.assertEqual(self.timing(memory)["state"], "proposed")

    async def test_unknown_send_keeps_barrier_and_queued_confirmation_cannot_cross_review(self):
        memory = await self.episode("明天面试")
        self.sender.fail = True
        await self.incoming("核对经历时间")
        await self.incoming("确认时间", process=False)
        self.assertFalse(await self.engine.step("alice"))
        box = self.store._db.execute("SELECT * FROM outbox_messages WHERE state='unknown'").fetchone()
        self.store.resolve_delivery(box["outbox_id"], "confirm_sent", "externally checked")
        self.sender.fail = False
        await self.engine.step("alice")
        self.assertEqual(self.timing(memory)["state"], "proposed")
        self.assertIn("已失效", self.answer())

    async def test_commit_before_reply_crash_is_idempotent(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        admitted = await self.incoming("确认时间", process=False)
        work = self.store.next_work("alice", "cfg")
        message = self.store.episode_time_command(work["context"], work["event"])
        version = self.timing(memory)["version"]
        self.reopen()
        await self.engine.step("alice")
        self.assertEqual(self.answer(), message.text)
        self.assertEqual(self.timing(memory)["version"], version)
        self.assertEqual(len(episode_times.audit(self.store._db, "alice", memory["memory_id"])), 1)
        duplicate = self.store.admit(EventEnvelope.from_inbound("bot", event(str(self.counter), text="确认时间")), character())
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.event_id, admitted.event_id)
        self.assertFalse(await self.engine.step("alice"))

    async def test_newer_pending_message_prevents_confirmation_of_old_turn(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        await self.incoming("确认时间", process=False)
        await self.incoming("不对，后天", process=False)
        await self.engine.step("alice")
        self.assertEqual(self.timing(memory)["state"], "proposed")
        self.assertIn("已失效", self.answer())

    async def test_admin_update_or_policy_change_invalidates_pending_confirmation(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        self.confirm(memory, date="2026-11-05")
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["confirmed"]["date"], "2026-11-05")
        self.assertIn("已失效", self.answer())
        await self.incoming("核对经历时间")
        await self.incoming("后天")
        self.request({"timezone": "UTC"})
        self.request({"timezone": "Asia/Shanghai"})
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["state"], "proposed")
        self.assertIn("已失效", self.answer())

    async def test_memory_revision_invalidates_target_in_list_and_confirmation(self):
        memory = await self.episode("明天面试", title="面试")
        await self.incoming("核对经历时间")
        self.store.enqueue_memory_change("alice", "alpha", memory["source_event_id"],
            change("明天面试", op="revise", memory_id=memory["memory_id"], expected_version=1, title="面试"),
            "revise", "admin", "review")
        await self.engine.step("alice")
        await self.incoming("确认时间")
        current = self.store.memories("alice", "alpha")["records"][0]
        self.assertEqual(current["version"], 2)
        self.assertEqual(self.timing(current)["state"], "proposed")
        self.assertIn("已失效", self.answer())

    async def test_other_user_and_role_switch_cannot_approve_old_question(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        await self.incoming("确认时间", user="bob")
        self.assertEqual(self.timing(memory)["state"], "proposed")
        for name in ("beta", "alpha"):
            self.store.enqueue_binding("alice", character(name), "switch:" + name)
            await self.engine.step("alice")
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["state"], "proposed")
        self.assertIn("已失效", self.answer())

    async def test_user_correction_invalidates_schedules_drafts_and_old_dialogue_history(self):
        memory = await self.episode("明天面试")
        self.request({"proactive_enabled": True})
        await self.incoming("核对经历时间")
        await self.incoming("确认时间")
        self.draft_request(self.candidates()[0])
        self.schedule()
        await self.incoming("修改经历时间")
        await self.incoming("后天下午")
        await self.incoming("确认时间")
        self.assertEqual(self.candidates()[0]["state"], "candidate")
        self.assertIsNone(self.candidates()[0]["due_at"])
        self.assertEqual(self.timing(memory)["confirmed"]["date"], "2026-10-28")
        job = self.store._db.execute("SELECT state FROM followup_drafts").fetchone()
        self.assertNotIn(job[0], ("queued", "ready", "generating"))
        await self.incoming("面试准备", process=False)
        work = self.store.next_work("alice", "cfg")
        bundle = builder().build(work["context"], work["event"], self.store.history(work["context"]))
        historical = " ".join(m.content_json for m in bundle.messages if m.layer == "history")
        self.assertNotIn("2026-10-27", historical)

    async def test_clear_requires_own_confirmation_and_does_not_delete_episode(self):
        memory = await self.episode("明天面试")
        self.confirm(memory)
        await self.incoming("清除经历时间")
        await self.incoming("取消")
        self.assertEqual(self.timing(memory)["state"], "confirmed")
        await self.incoming("清除经历时间")
        await self.incoming("确认清除")
        self.assertEqual(self.timing(memory)["state"], "cleared")
        self.assertEqual(self.store.memories("alice", "alpha")["records"][0]["state"], "open")

    async def test_named_natural_correction_requires_explicit_final_confirmation(self):
        memory = await self.episode("明天面试", title="面试")
        self.confirm(memory)
        source = await self.incoming("把面试的时间改到后天下午")
        self.assertIn("2026-10-28 下午", self.answer())
        self.assertEqual(self.timing(memory)["confirmed"]["date"], "2026-10-27")
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["confirmed"]["date"], "2026-10-28")
        self.assertEqual(self.timing(memory)["confirmed"]["confirmation_source_event_id"], source.event_id)

    async def test_bare_confirmation_without_question_has_explicit_no_change_response(self):
        await self.incoming("确认时间")
        self.assertIn("没有保存", self.answer())
        self.assertEqual(self.generator.calls, [])
        self.assertIsNone(self.pending())

    async def test_other_commands_still_work_and_end_time_review(self):
        memory = await self.episode("明天面试")
        await self.incoming("核对经历时间")
        await self.incoming("/contact status")
        self.assertIn("时区", self.answer())
        self.assertIsNone(self.pending())
        await self.incoming("确认时间")
        self.assertEqual(self.timing(memory)["state"], "proposed")

    async def test_erasure_restore_and_stale_worker_cannot_revive_dialogue(self):
        await self.episode("明天 SECRET 面试", title="SECRET 面试")
        await self.incoming("核对经历时间")
        await self.incoming("确认时间", process=False)
        work = self.store.next_work("alice", "cfg")
        backup = self.path.with_suffix(".backup")
        self.store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copyfile(self.path, backup)
        preview = self.store.privacy_status("alice")
        self.assertEqual(preview["counts"]["episode_time_dialogues"], 1)
        self.store.erase_conversation({"conversation_id": "alice", "request_id": "erase", "confirmed": True,
            "confirm_conversation_id": "alice", "preview_token": preview["preview_token"]}, "admin")
        with self.assertRaises(privacy.StaleWork):
            self.store.episode_time_command(work["context"], work["event"])
        self.store.close()
        shutil.copyfile(backup, self.path)
        self.store = self.open_store()
        self.store.recover()
        self.engine.store = self.store
        self.assertNotIn("SECRET", "\n".join(self.store._db.iterdump()))
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM episode_time_dialogue_turns").fetchone()[0], 0)
        self.store.resume_conversation("alice", 2, character(), "admin")
        await self.incoming("确认时间")
        self.assertIsNone(self.pending())
        self.assertEqual(self.store._db.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_schema_nine_upgrade_preserves_confirmed_times_without_starting_dialogue(self):
        memory = await self.episode("明天面试")
        self.confirm(memory)
        previous = self.timing(memory)
        self.store.close()
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE episode_time_dialogues")
        db.execute("DROP TABLE episode_time_dialogue_turns")
        db.execute("PRAGMA user_version=9")
        db.commit()
        db.close()
        self.store = self.open_store()
        self.assertEqual(self.store.snapshot()["schema_version"], 10)
        self.assertEqual(self.timing(memory), previous)
        self.assertIsNone(self.pending())


if __name__ == "__main__":
    unittest.main()
