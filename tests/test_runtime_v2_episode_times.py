from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
from datetime import datetime, timezone

from tests import test_runtime_v2_followups as followup_tests
from tests.test_conversation_context import builder
from tests.test_runtime_v2 import character
from waifu_standalone.config import RuntimeV2Config
from waifu_standalone.conversation.followup_draft import build_context
from waifu_standalone.runtime_v2 import episode_times
from waifu_standalone.runtime_v2.episode_time_parser import extract, confirm_value
from waifu_standalone.runtime_v2.followup_drafts import DraftResult


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class TimeParserTests(unittest.TestCase):
    def parse(self, text, at="2026-10-26T12:00", zone="Asia/Shanghai"):
        return extract(text, instant(at), zone)

    def test_relative_dates_use_source_local_date_across_midnight_and_year(self):
        result = self.parse("明天下午有面试", "2026-12-31T16:30")
        self.assertEqual(result["source_local_date"], "2027-01-01")
        self.assertEqual(result["options"], [{"date": "2027-01-02", "precision": "period", "period": "afternoon", "clock": "", "fold": None}])
        result = self.parse("明天面试", "2026-12-31T16:30", "America/New_York")
        self.assertEqual(result["options"][0]["date"], "2027-01-01")
        self.assertTrue(result["requires_confirmation"])
        self.assertNotIn("utc_at", result["options"][0])

    def test_weekday_without_week_is_not_silently_rolled_forward(self):
        result = self.parse("周三面试", "2026-10-30T12:00")
        self.assertEqual([o["date"] for o in result["options"]], ["2026-10-28", "2026-11-04"])
        self.assertIn("weekday_ambiguous", result["reasons"])
        for text, day in (("本周三", "2026-10-28"), ("下周三", "2026-11-04"), ("下下周三", "2026-11-11"), ("上周日", "2026-10-25")):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text, "2026-10-30T12:00")["options"][0]["date"], day)

    def test_clock_precision_ambiguity_chinese_numbers_and_relative_days(self):
        for text, clock in (("明天下午三点半", "15:30"), ("后天上午九点十五", "09:15"),
                            ("明天下午三点一刻", "15:15"), ("明天15:45", "15:45"), ("三天后14:30", "14:30")):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual(result["options"][0]["clock"], clock)
                self.assertEqual(result["options"][0]["precision"], "minute")
        result = self.parse("明天三点面试")
        self.assertEqual([o["clock"] for o in result["options"]], ["03:00", "15:00"])
        self.assertIn("clock_ambiguous", result["reasons"])

    def test_unknown_zone_yearless_dates_and_vague_expressions_remain_unconfirmed(self):
        result = self.parse("明天下午", zone="")
        self.assertEqual(result["options"], [])
        self.assertIn("missing_timezone", result["reasons"])
        self.assertEqual([o["date"] for o in self.parse("1月2日面试")["options"]], ["2026-01-02", "2027-01-02"])
        for text in ("有空再说", "过几天面试", "周末再聊", "最近要参加活动", "下午三点"):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual(result["options"], [])
                self.assertTrue(result["reasons"])

    def test_conflicts_ranges_negation_and_other_zones_do_not_pick_one(self):
        for text in ("不是明天，是后天", "明天9:00到11:00面试", "明天上午到下午", "2026-10-28 至 2026-10-29"):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertIn("multiple_mentions", result["reasons"])
                self.assertEqual(result["options"], [])
        self.assertIn("uncertain_language", self.parse("可能明天下午面试")["reasons"])
        self.assertEqual(self.parse("北京时间明天上午九点", zone="UTC")["options"], [])

    def test_invalid_calendar_and_daylight_saving_times(self):
        for text in ("2026-02-30", "2026-13-01", "2月30日"):
            with self.subTest(text=text):
                self.assertIn("invalid_date", self.parse(text)["reasons"])
        self.assertIn("invalid_clock", self.parse("明天25:00")["reasons"])
        result = self.parse("2026-03-08 02:30", zone="America/New_York")
        self.assertIn("dst_gap", result["reasons"])
        self.assertEqual(result["options"], [])
        result = self.parse("2026-11-01 01:30", zone="America/New_York")
        self.assertEqual([o["fold"] for o in result["options"]], [0, 1])
        first = confirm_value(result["options"][0], "America/New_York")
        second = confirm_value(result["options"][1], "America/New_York")
        self.assertEqual(second["utc_at"] - first["utc_at"], 3600)
        with self.assertRaises(ValueError):
            confirm_value({"precision": "minute", "date": "2026-11-01", "clock": "01:30"}, "America/New_York")

    def test_date_and_period_confirmation_never_invent_a_clock(self):
        for precision, period in (("date", ""), ("period", "afternoon")):
            result = confirm_value({"precision": precision, "date": "2026-01-01", "period": period}, "UTC")
            self.assertNotIn("utc_at", result)
            self.assertNotIn("local_time", result)
            with self.assertRaises(ValueError):
                confirm_value({"precision": precision, "date": "2026-01-01", "period": period, "clock": "15:00"}, "UTC")
        self.assertLess(confirm_value({"precision": "minute", "date": "2026-01-01", "clock": "15:00"}, "UTC")["utc_at"], instant("2026-10-26"))


class EpisodeTimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = followup_tests.FollowupTests.setUp
    tearDown = followup_tests.FollowupTests.tearDown
    open_store = followup_tests.FollowupTests.open_store
    incoming = followup_tests.FollowupTests.incoming
    detail = followup_tests.FollowupTests.detail
    request = followup_tests.FollowupTests.request
    reopen = followup_tests.FollowupTests.reopen
    episode = followup_tests.FollowupTests.episode
    candidates = followup_tests.FollowupTests.candidates
    candidate = followup_tests.FollowupTests.candidate
    schedule = followup_tests.FollowupTests.schedule
    revise = followup_tests.FollowupTests.revise
    tick = followup_tests.FollowupTests.tick

    def time_payload(self, memory, **overrides):
        detail = self.store.followup_list("alice")
        timing = episode_times.current(self.store._db, memory["memory_id"], memory["version"])
        return {"conversation_id": "alice", "operation": "confirm", "memory_id": memory["memory_id"],
            "expected_memory_version": memory["version"], "expected_timing_version": timing["version"],
            "expected_data_generation": detail["data_generation"], "expected_policy_version": detail["policy_version"],
            "expected_binding_generation": detail["binding_generation"], "expected_next_seq": detail["next_seq"],
            "precision": "period", "date": "2026-10-27", "period": "afternoon", "clock": "", "fold": None,
            "confirmation_source_event_id": memory["source_event_id"], "confirmation_quote": memory["quote"],
            "authorized": True, "note": "与用户核对了具体日期和时段", "request_id": f"time:{memory['memory_id']}:{memory['version']}:{timing['version']}", **overrides}

    def confirm(self, memory, **overrides):
        payload = self.time_payload(memory, **overrides)
        return self.store.episode_time_request(payload, "admin"), payload

    def timing(self, memory):
        return episode_times.public(episode_times.current(self.store._db, memory["memory_id"], memory["version"]))

    def draft_request(self, item):
        detail = self.store.followup_list("alice")
        return self.store.followup_request({"conversation_id": "alice", "operation": "generate", "candidate_id": item["candidate_id"],
            "expected_version": item["version"], "expected_data_generation": detail["data_generation"], "expected_policy_version": detail["policy_version"],
            "request_id": "draft:" + str(item["version"]), "authorized": True, "note": "已核对来源"}, "admin")

    async def test_extraction_is_source_only_stable_on_restart_and_never_schedules(self):
        memory = await self.episode("明天下午面试", title="2040-12-31 伪造摘要日期")
        timing = self.timing(memory)
        self.assertEqual(timing["proposal"]["options"][0]["date"], "2026-10-27")
        self.assertEqual(timing["state"], "proposed")
        self.assertEqual(len(self.generator.calls), 1)
        self.now += 2 * 86400
        self.reopen()
        self.tick()
        self.assertEqual(self.timing(memory)["proposal"], timing["proposal"])
        self.assertEqual(self.candidates()[0]["state"], "candidate")
        self.assertIsNone(self.candidates()[0]["due_at"])

    async def test_confirmation_preserves_precision_and_enters_user_context_only(self):
        memory = await self.episode("明天下午面试")
        self.confirm(memory)
        self.assertFalse(self.detail()["policy"]["proactive_enabled"])
        self.assertEqual(self.candidates()[0]["state"], "candidate")
        self.assertIsNone(self.candidates()[0]["due_at"])
        self.assertNotIn("utc_at", self.timing(memory)["confirmed"])
        await self.incoming("我在准备", process=False)
        work = self.store.next_work("alice", "cfg")
        bundle = builder().build(work["context"], work["event"], self.store.history(work["context"]))
        evidence = [m for m in bundle.messages if m.layer == "memory_evidence"][0]
        self.assertEqual(evidence.role, "user")
        self.assertIn("2026-10-27", evidence.content_json)
        self.assertNotIn("15:00", evidence.content_json)
        self.assertEqual(bundle.report()["episode_time_references"][0]["state"], "confirmed")
        self.assertNotIn("2026-10-27", json.dumps(bundle.report()))

    async def test_unconfirmed_interpretations_are_excluded_from_draft_context(self):
        memory = await self.episode("周三面试")
        self.request({"proactive_enabled": True})
        self.draft_request(self.candidates()[0])
        context = self.store.claim_followup_draft()
        episode = json.loads(context.episode_json)
        self.assertEqual(episode["time"]["state"], "proposed")
        self.assertNotIn("options", context.episode_json)
        self.assertNotIn("2026-10-28", json.dumps(build_context(context, RuntimeV2Config()).wire_messages()))

    async def test_later_clarification_uses_new_user_source_and_suppresses_old_round(self):
        memory = await self.episode("周三面试")
        source = await self.incoming("是下周三下午，11月4日")
        self.confirm(memory, date="2026-11-04", confirmation_source_event_id=source.event_id, confirmation_quote="是下周三下午，11月4日")
        self.request({"proactive_enabled": True})
        self.draft_request(self.candidates()[0])
        context = self.store.claim_followup_draft()
        bundle = build_context(context, RuntimeV2Config())
        evidence = next(m for m in bundle.messages if m.layer == "episode_evidence")
        self.assertIn(source.event_id, evidence.source_ids)
        self.assertIn(memory["source_event_id"], bundle.omitted_history_ids)
        self.assertIn("2026-11-04", evidence.content_json)

    async def test_time_revision_invalidates_pending_model_result_and_scheduled_outbox(self):
        memory = await self.episode("明天下午面试")
        self.request({"proactive_enabled": True})
        self.draft_request(self.candidates()[0])
        context = self.store.claim_followup_draft()
        self.store.authorize_draft_call(context.job_id, {"status": "prepared"})
        self.confirm(memory)
        self.assertFalse(self.store.finish_followup_draft(context.job_id, DraftResult(text="STALE TIME")))
        item, payload = self.schedule()
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.confirm(memory, date="2026-10-28")
        self.assertFalse(self.store.begin_dispatch(outbox["outbox_id"]))
        self.assertEqual(self.candidates()[0]["state"], "candidate")
        self.assertIsNone(self.candidates()[0]["due_at"])
        self.assertEqual(self.store.delivery_detail(outbox["outbox_id"])["state"], "cancelled")
        self.assertEqual(self.tick(3600), 0)
        self.assertNotIn("STALE TIME", "\n".join(self.store._db.iterdump()))

    async def test_time_change_during_dispatch_keeps_unknown_barrier(self):
        memory = await self.episode("明天下午面试")
        self.request({"proactive_enabled": True})
        self.confirm(memory)
        self.schedule()
        self.tick(31 * 60)
        outbox = self.store.next_work("alice", "cfg")["outbox"]
        self.store.begin_dispatch(outbox["outbox_id"])
        self.confirm(memory, date="2026-10-28")
        self.assertTrue(self.candidates()[0]["cancel_requested"])
        self.store.finish_delivery(outbox["outbox_id"], "unknown")
        self.reopen()
        with self.assertRaises(ValueError):
            self.store.resolve_delivery(outbox["outbox_id"], "retry", "old time")
        self.assertIsNone(self.store.next_work("alice", "cfg"))

    async def test_time_correction_suppresses_old_sent_question_from_context(self):
        memory = await self.episode("明天下午面试")
        self.request({"proactive_enabled": True})
        self.confirm(memory)
        self.schedule(text="OLD-TIME follow-up")
        self.tick(31 * 60)
        await self.engine.step("alice")
        self.confirm(memory, date="2026-10-28")
        await self.incoming("继续聊吧", process=False)
        work = self.store.next_work("alice", "cfg")
        bundle = builder().build(work["context"], work["event"], self.store.history(work["context"]))
        self.assertNotIn("OLD-TIME", json.dumps(bundle.wire_messages()))
        self.assertIn("2026-10-28", json.dumps(bundle.wire_messages()))

    async def test_clear_confirmation_invalidates_ready_draft_and_preserves_audit(self):
        memory = await self.episode("明天下午面试")
        self.request({"proactive_enabled": True})
        self.confirm(memory)
        self.draft_request(self.candidates()[0])
        context = self.store.claim_followup_draft()
        self.store.authorize_draft_call(context.job_id, {"status": "prepared"})
        self.store.finish_followup_draft(context.job_id, DraftResult(text="时间已确认的草稿"))
        self.confirm(memory, operation="clear")
        self.assertEqual(self.timing(memory)["state"], "cleared")
        detail = self.store.followup_list("alice", candidate_id=self.candidates()[0]["candidate_id"])
        self.assertEqual(detail["drafts"][0]["state"], "invalidated")
        self.assertEqual(len(detail["time_audit"]), 2)
        self.assertEqual(detail["time_audit"][1]["timing"]["confirmed"]["date"], "2026-10-27")

    async def test_timezone_change_reinterprets_source_and_requires_fresh_confirmation(self):
        self.now = instant("2026-10-26T17:00")
        memory = await self.episode("明天下午面试")
        self.assertEqual(self.timing(memory)["proposal"]["options"][0]["date"], "2026-10-28")
        self.confirm(memory, date="2026-10-28")
        self.request({"timezone": "America/New_York"})
        timing = self.timing(memory)
        self.assertEqual(timing["state"], "proposed")
        self.assertEqual(timing["confirmed"], {})
        self.assertEqual(timing["proposal"]["options"][0]["date"], "2026-10-27")
        self.request({"timezone": "Asia/Shanghai"})
        self.assertEqual(self.timing(memory)["state"], "proposed")

    async def test_episode_revision_and_closure_invalidate_time_without_reuse(self):
        memory = await self.episode("明天下午面试")
        self.confirm(memory)
        await self.revise(memory, text="改到下周四")
        self.assertEqual(self.timing(memory)["state"], "invalidated")
        current = self.store.memories("alice", "alpha")["records"][0]
        self.assertEqual(self.timing(current)["state"], "proposed")
        self.confirm(current, date="2026-11-05")
        await self.revise(current, text="面试取消了", state="cancelled")
        self.assertEqual(self.timing(current)["state"], "invalidated")

    async def test_stale_forms_idempotency_source_scope_and_pending_turn_checks(self):
        memory = await self.episode("明天下午面试")
        result, payload = self.confirm(memory)
        self.assertTrue(self.store.episode_time_request(payload, "admin")["duplicate"])
        with self.assertRaises(ValueError):
            self.store.episode_time_request(payload, "forged")
        for overrides in ({"expected_timing_version": 1}, {"expected_memory_version": 0}, {"expected_next_seq": 0},
                          {"expected_data_generation": True}, {"expected_binding_generation": 0}, {"expected_policy_version": 0},
                          {"authorized": False}, {"confirmation_quote": "不在原文"}, {"precision": "minute", "clock": "25:00", "period": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.confirm(memory, **overrides)
        other = await self.incoming("明天有事", user="bob")
        with self.assertRaises(ValueError):
            self.confirm(memory, confirmation_source_event_id=other.event_id, confirmation_quote="明天有事")
        stale = self.time_payload(memory)
        await self.incoming("新情况", process=False)
        with self.assertRaises(ValueError):
            self.store.episode_time_request(stale, "admin")
        with self.assertRaises(ValueError):
            self.confirm(memory)  # Even a fresh version cannot change an active turn.
        await self.engine.step("alice")
        self.store.enqueue_binding("alice", character("beta"), "switch")
        await self.engine.step("alice")
        with self.assertRaises(ValueError):
            self.confirm(memory)

    async def test_erasure_and_restore_remove_times_audits_and_stale_confirmation(self):
        memory = await self.episode("明天下午 SECRET 面试")
        _, payload = self.confirm(memory)
        backup = self.path.with_suffix(".backup")
        self.store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copyfile(self.path, backup)
        preview = self.store.privacy_status("alice")
        self.assertEqual(preview["counts"]["episode_times"], 1)
        self.assertEqual(preview["counts"]["episode_time_requests"], 1)
        self.store.erase_conversation({"conversation_id": "alice", "request_id": "erase", "confirmed": True,
            "confirm_conversation_id": "alice", "preview_token": preview["preview_token"]}, "admin")
        self.store.close()
        shutil.copyfile(backup, self.path)
        self.store = self.open_store()
        self.store.recover()
        self.assertNotIn("SECRET", "\n".join(self.store._db.iterdump()))
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM episode_times").fetchone()[0], 0)
        self.store.resume_conversation("alice", 2, character(), "admin")
        with self.assertRaises(ValueError):
            self.store.episode_time_request(payload, "admin")
        self.assertEqual(self.store._db.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_schema_eight_upgrade_only_proposes_times(self):
        memory = await self.episode("明天下午面试")
        self.store.close()
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE episode_time_dialogues")
        db.execute("DROP TABLE episode_time_dialogue_turns")
        db.execute("DROP TABLE episode_time_requests")
        db.execute("DROP TABLE episode_times")
        db.execute("PRAGMA user_version=8")
        db.commit()
        db.close()
        self.store = self.open_store()
        self.tick()
        self.assertEqual(self.store.snapshot()["schema_version"], 10)
        self.assertEqual(self.timing(memory)["state"], "proposed")
        self.assertIsNone(self.candidates()[0]["due_at"])


if __name__ == "__main__":
    unittest.main()
