from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_runtime_v2 import FakeGenerator, FakeSender, character, event
from tests.test_runtime_v2_memory import change
from waifu_standalone.models import OutboundMessage
from waifu_standalone.runtime_v2.contracts import EventEnvelope
from waifu_standalone.runtime_v2.engine import ConversationEngine, RuntimeHost
from waifu_standalone.runtime_v2.store import RuntimeStore
from waifu_standalone.runtime_v2 import privacy


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.path, "bot", "capture", reminders_enabled=True)
        self.generator, self.sender = FakeGenerator(), FakeSender()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "config")
        self.seq = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def incoming(self, text="SECRET user content", *, user="alice", process=True):
        self.seq += 1
        envelope = EventEnvelope.from_inbound("bot", event(str(self.seq), user, text))
        result = self.store.admit(envelope, character())
        if process:
            await self.engine.step(user)
        return envelope, result

    def deletion(self, **overrides):
        return {"conversation_id": "alice", "request_id": "erase-1", "confirmed": True,
                "confirm_conversation_id": "alice", "preview_token": self.store.privacy_status("alice")["preview_token"], **overrides}

    def erase(self, **overrides):
        return self.store.erase_conversation(self.deletion(**overrides), "admin")

    def resume(self, generation=2):
        return self.store.resume_conversation("alice", generation, character(), "admin")

    def reopen(self):
        self.store.close()
        self.store = RuntimeStore(self.path, "bot", "capture", reminders_enabled=True)
        self.store.recover()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "config")

    def assert_no_secret(self):
        dumped = "\n".join(self.store._db.iterdump())
        journal = "\n".join(self.store._journal.db.iterdump())
        self.assertNotIn("SECRET", dumped)
        self.assertNotIn("SECRET", journal)
        self.assertEqual(self.store._db.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_erases_all_characters_and_derived_content_but_not_other_conversation(self):
        envelope, admitted = await self.incoming("面试周三 SECRET")
        self.store.enqueue_memory_change("alice", "alpha", admitted.event_id,
            change(text="SECRET episode", quote="SECRET"), "memory-1", "admin", "SECRET operator note")
        await self.engine.step("alice")
        self.store.reminder_request({"operation": "create", "conversation_id": "alice", "request_id": "task-1",
            "source_event_id": admitted.event_id, "evidence": "SECRET", "text": "SECRET reminder", "when": "+10m", "timezone": "UTC", "allow_quiet": True,
            "authorized": True, "note": "SECRET basis"}, "admin")
        self.store.enqueue_binding("alice", character("beta"), "bind")
        await self.engine.step("alice")
        await self.incoming("SECRET second character")
        await self.incoming("BOB data", user="bob")
        preview = self.store.privacy_status("alice")
        self.assertEqual(preview["counts"]["memory_records"], 1)
        self.assertEqual(preview["counts"]["reminder_tasks"], 1)
        result = self.erase()
        self.assertEqual(result["state"], "erased")
        self.assertEqual(result["data_generation"], 2)
        self.assertEqual(self.store.conversation_detail("alice")["items"], [])
        self.assertIn("BOB data", json.dumps(self.store.conversation_detail("bob")))
        self.assert_no_secret()
        self.assertEqual(self.store.tick_reminders(), 0)
        self.assertTrue(self.store.admit(envelope, character()).duplicate)

    async def test_preview_confirmation_and_idempotency_cannot_erase_new_data(self):
        await self.incoming()
        payload = self.deletion()
        with self.assertRaises(ValueError):
            self.store.erase_conversation({**payload, "confirmed": False}, "admin")
        with self.assertRaises(ValueError):
            self.store.erase_conversation({**payload, "confirm_conversation_id": "bob"}, "admin")
        await self.incoming("NEW pending", process=False)
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            self.store.erase_conversation(payload, "admin")
        payload = self.deletion()
        self.store.erase_conversation(payload, "admin")
        self.resume()
        await self.incoming("new clean session")
        self.assertTrue(self.store.erase_conversation(payload, "admin")["duplicate"])
        self.assertIn("new clean session", json.dumps(self.store.conversation_detail("alice")))
        with self.assertRaises(ValueError):
            self.store.erase_conversation(payload, "other-admin")
        self.assert_no_secret()

    async def test_old_generation_cannot_write_reports_proposals_or_reminders_after_resume(self):
        _, admitted = await self.incoming("/remind +1m | SECRET", process=False)
        work = self.store.next_work("alice", "cfg")
        self.erase()
        self.resume()
        context = work["context"]
        for action in (lambda: self.store.record_context(context, {"status": "SECRET"}),
                       lambda: self.store.save_proposal(context, OutboundMessage("alice", "person", "SECRET reply")),
                       lambda: self.store.reminder_command(context, work["event"]),
                       lambda: self.store.history(context)):
            with self.assertRaises(privacy.StaleWork):
                action()
        self.store.end_generation(admitted.event_id, error_code="SECRET late failure")
        self.assert_no_secret()

    async def test_engine_discards_delayed_generator_result_without_worker_failure(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Delayed(FakeGenerator):
            async def generate(inner, *args):
                entered.set()
                await release.wait()
                return await super().generate(*args)
        self.engine.generator = Delayed()
        await self.incoming(process=False)
        task = asyncio.create_task(self.engine.step("alice"))
        await asyncio.wait_for(entered.wait(), 3)
        self.erase()
        self.resume()
        release.set()
        self.assertTrue(await asyncio.wait_for(task, 3))
        self.assertEqual(self.sender.sent, [])
        self.assert_no_secret()

    async def test_prepared_reply_cannot_send_after_erasure(self):
        await self.incoming(process=False)
        work = self.store.next_work("alice", "cfg")
        box = self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "SECRET draft"))
        self.erase()
        self.assertFalse(self.store.begin_dispatch(box["outbox_id"]))
        self.assert_no_secret()

    async def test_inflight_result_is_recorded_without_recreating_content(self):
        await self.incoming(process=False)
        work = self.store.next_work("alice", "cfg")
        box = self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "SECRET draft"))
        self.store.begin_dispatch(box["outbox_id"])
        result = self.erase()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["pending_deliveries"][0]["state"], "dispatching")
        with self.assertRaises(ValueError):
            self.resume()
        self.assert_no_secret()
        self.store.finish_delivery(box["outbox_id"], "captured")
        self.assertEqual(self.store.delivery_detail(box["outbox_id"])["message"]["text"], "")
        self.assertEqual(self.store.privacy_status("alice")["state"], "erased")
        self.resume()
        await self.incoming("fresh")
        self.assertEqual(len(self.generator.calls[-1][2]), 0)
        self.assert_no_secret()

    async def test_unknown_survives_restart_but_retry_and_new_admission_are_blocked(self):
        self.sender.fail = True
        await self.incoming()
        box = self.store.conversation_detail("alice")["items"][0]["outbox"]
        self.erase()
        self.reopen()
        with self.assertRaises(ValueError):
            await self.incoming("blocked")
        with self.assertRaises(ValueError):
            self.store.resolve_delivery(box["outbox_id"], "retry", "checked")
        self.assertEqual(self.store.privacy_status("alice")["state"], "needs_review")
        self.store.resolve_delivery(box["outbox_id"], "confirm_sent", "SECRET note is not retained")
        self.assert_no_secret()
        self.resume()

    async def test_erased_inflight_crash_recovers_to_unknown_and_discard_unblocks_resume(self):
        await self.incoming(process=False)
        work = self.store.next_work("alice", "cfg")
        box = self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "SECRET"))
        self.store.begin_dispatch(box["outbox_id"])
        self.erase()
        self.reopen()
        self.assertEqual(self.store.delivery_detail(box["outbox_id"])["state"], "unknown")
        self.store.resolve_delivery(box["outbox_id"], "discard", "no result")
        self.resume()
        self.assert_no_secret()

    async def test_known_rejection_of_erased_inflight_send_does_not_block_new_session(self):
        await self.incoming(process=False)
        work = self.store.next_work("alice", "cfg")
        box = self.store.save_proposal(work["context"], OutboundMessage("alice", "person", "SECRET"))
        self.store.begin_dispatch(box["outbox_id"])
        self.erase()
        self.store.finish_delivery(box["outbox_id"], "rejected")
        self.resume()
        await self.incoming("fresh")
        self.assertEqual(self.sender.sent[-1].text, "alpha:fresh")

    async def test_resume_keeps_source_deduplication_and_rejects_unidentifiable_replays(self):
        envelope, admitted = await self.incoming()
        self.erase()
        self.reopen()
        self.resume()
        result = self.store.admit(envelope, character())
        self.assertTrue(result.duplicate)
        self.assertEqual(result.event_id, admitted.event_id)
        with self.assertRaises(ValueError):
            self.store.admit(EventEnvelope.from_inbound("bot", event("", text="SECRET")), character())
        await self.incoming("new data")
        self.assertEqual(self.generator.calls[-1][0].data_generation, 2)
        self.erase(request_id="erase-2")
        with self.assertRaises(ValueError):
            self.resume(2)
        self.resume(3)

    async def test_restoring_old_main_database_replays_latest_deletions_before_access(self):
        await self.incoming()
        backup = Path(self.temp.name) / "old.sqlite3"
        dest = sqlite3.connect(backup)
        self.store._db.backup(dest)
        dest.close()
        self.erase()
        self.store.close()
        shutil.copyfile(backup, self.path)
        with self.assertRaises(privacy.PrivacyUnavailable):
            RuntimeStore.inspect(self.path)
        self.store = RuntimeStore(self.path, "bot", "capture")
        self.assertEqual(self.store.privacy_status("alice")["state"], "erased")
        self.assert_no_secret()

    async def test_crash_after_deletion_intent_fails_closed_and_recovers(self):
        await self.incoming()
        with patch.object(privacy, "apply_erasure", side_effect=RuntimeError("injected crash")):
            with self.assertRaises(RuntimeError):
                self.erase()
        for read in (lambda: self.store.snapshot(), lambda: self.store.conversation_detail("alice"),
                     lambda: self.store.memories("alice", "alpha"), lambda: self.store.tick_reminders()):
            with self.assertRaises(privacy.PrivacyUnavailable):
                read()
        self.reopen()
        self.assert_no_secret()

    async def test_restore_before_conversation_existed_still_preserves_unknown_send(self):
        backup = Path(self.temp.name) / "empty.sqlite3"
        dest = sqlite3.connect(backup)
        self.store._db.backup(dest)
        dest.close()
        self.sender.fail = True
        await self.incoming()
        box = self.store.conversation_detail("alice")["items"][0]["outbox"]
        self.erase()
        self.store.close()
        shutil.copyfile(backup, self.path)
        self.store = RuntimeStore(self.path, "bot", "capture")
        self.store.recover()
        self.assertEqual(self.store.privacy_status("alice")["pending_deliveries"][0]["outbox_id"], box["outbox_id"])
        with self.assertRaises(ValueError):
            self.resume()
        self.assert_no_secret()

    async def test_old_journal_is_rejected_when_main_database_has_newer_deletion(self):
        await self.incoming()
        saved = Path(self.temp.name) / "old-journal.sqlite3"
        dest = sqlite3.connect(saved)
        self.store._journal.db.backup(dest)
        dest.close()
        self.erase()
        self.store.close()
        journal_path = privacy.journal_path(self.path)
        latest = journal_path.with_suffix(".saved")
        shutil.copyfile(journal_path, latest)
        shutil.copyfile(saved, journal_path)
        try:
            with self.assertRaises(privacy.PrivacyUnavailable):
                RuntimeStore.inspect(self.path)
            with self.assertRaises(privacy.PrivacyUnavailable):
                RuntimeStore(self.path, "bot", "capture")
        finally:
            shutil.copyfile(latest, journal_path)
            self.store = RuntimeStore(self.path, "bot", "capture")

    async def test_missing_journal_fails_closed_including_read_only_inspection(self):
        await self.incoming()
        self.erase()
        self.store.close()
        path = privacy.journal_path(self.path)
        saved = path.with_suffix(".saved")
        path.rename(saved)
        try:
            with self.assertRaises(privacy.PrivacyUnavailable):
                RuntimeStore(self.path, "bot", "capture")
            with self.assertRaises(privacy.PrivacyUnavailable):
                RuntimeStore.inspect(self.path)
        finally:
            saved.rename(path)
            self.store = RuntimeStore(self.path, "bot", "capture")

    async def test_deletion_scope_does_not_erase_live_database(self):
        await self.incoming()
        live = RuntimeStore(Path(self.temp.name) / "live.sqlite3", "bot", "live")
        try:
            live.admit(EventEnvelope.from_inbound("bot", event(text="LIVE CONTENT")), character())
            self.erase()
            self.assertIn("LIVE CONTENT", json.dumps(live.conversation_detail("alice")))
        finally:
            live.close()


if __name__ == "__main__":
    unittest.main()
