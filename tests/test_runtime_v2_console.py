from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_runtime_v2 import FakeGenerator, FakeSender, character, event
from waifu_standalone.models import MessageSegment, OutboundMessage
from waifu_standalone.runtime_v2.contracts import EventEnvelope
from waifu_standalone.runtime_v2.engine import ConversationEngine
from waifu_standalone.runtime_v2.store import RuntimeStore


class ConversationInspectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = RuntimeStore(self.path, "bot", "capture")
        self.sender = FakeSender()
        self.engine = ConversationEngine(self.store, FakeGenerator(), self.sender, "test-config")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def admit(self, incoming=None):
        return self.store.admit(EventEnvelope.from_inbound("bot", incoming or event()), character())

    async def test_only_confirmed_replies_are_history_and_review_does_not_regenerate(self):
        self.sender.fail = True
        self.admit()
        await self.engine.step("alice")
        detail = self.store.conversation_detail("alice")
        item = detail["items"][0]
        self.assertIsNone(item["assistant_message"])
        self.assertEqual(item["outbox"]["draft_text"], "alpha:hello")
        self.assertEqual(item["state"], "needs_review")
        outbox_id = item["outbox"]["outbox_id"]
        self.store.resolve_delivery(outbox_id, "confirm_sent", "checked capture", expected_state="unknown", expected_attempts=1)
        item = self.store.conversation_detail("alice")["items"][0]
        self.assertEqual(item["assistant_message"], {"role": "assistant", "content": "alpha:hello", "delivery_state": "captured"})
        self.assertNotIn("draft_text", item["outbox"])
        audit = self.store.delivery_detail(outbox_id)
        self.assertEqual(audit["reviews"][0]["note"], "checked capture")
        self.assertEqual(audit["meta"]["mode"], "capture")
        self.assertEqual(len(self.engine.generator.calls), 1)
        self.assertEqual(len(self.sender.sent), 1)

    async def test_old_review_cannot_change_a_later_delivery_attempt(self):
        self.sender.fail = True
        self.admit()
        await self.engine.step("alice")
        outbox_id = self.store.conversation_detail("alice")["items"][0]["outbox"]["outbox_id"]
        self.store.resolve_delivery(outbox_id, "retry", "first review", expected_attempts=1, expected_state="unknown")
        await self.engine.step("alice")
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.resolve_delivery(outbox_id, "discard", "stale browser", expected_attempts=1, expected_state="unknown")
        audit = self.store.delivery_detail(outbox_id)
        self.assertEqual(audit["state"], "unknown")
        self.assertEqual(audit["attempts"], 2)
        self.assertEqual([r["action"] for r in audit["reviews"]], ["retry"])

    async def test_discarded_draft_remains_distinct_after_restart(self):
        self.sender.fail = True
        self.admit()
        await self.engine.step("alice")
        outbox_id = self.store.conversation_detail("alice")["items"][0]["outbox"]["outbox_id"]
        self.store.resolve_delivery(outbox_id, "discard", "not sent")
        self.store.close()
        self.store = RuntimeStore(self.path, "bot", "capture")
        self.store.recover()
        item = self.store.conversation_detail("alice")["items"][0]
        self.assertEqual(item["outbox"]["state"], "cancelled")
        self.assertIsNone(item["assistant_message"])
        self.assertEqual(self.store.delivery_detail(outbox_id)["reviews"][0]["action"], "discard")

    async def test_pagination_scope_and_binding_events_preserve_each_turn_version(self):
        self.admit(event("1"))
        await self.engine.step("alice")
        self.store.enqueue_binding("alice", character("beta"), "switch")
        # Pending binding is shown without claiming it already took effect.
        pending = self.store.conversation_detail("alice")
        self.assertEqual(pending["binding"]["character_id"], "alpha")
        self.assertEqual(pending["items"][-1]["state"], "queued")
        await self.engine.step("alice")
        self.admit(event("2"))
        await self.engine.step("alice")
        self.admit(event(user="bob", text="BOB_PRIVATE"))
        await self.engine.step("bob")
        latest = self.store.conversation_detail("alice", limit=1)
        self.assertEqual(latest["binding"]["character_id"], "beta")
        self.assertEqual(latest["items"][0]["turn"]["character_version"], character("beta").version)
        self.admit(event("3"))  # New arrivals cannot shift a sequence cursor.
        older = self.store.conversation_detail("alice", limit=2, before_seq=latest["next_before_seq"])
        self.assertEqual([i["seq"] for i in older["items"]], [1, 2])
        self.assertEqual(older["items"][0]["turn"]["character_id"], "alpha")
        self.assertEqual(older["items"][1]["binding_change"]["character_id"], "beta")
        self.assertIsNone(older["next_before_seq"])
        self.assertNotIn("BOB_PRIVATE", json.dumps(older))
        page = self.store.conversations(limit=1)
        self.assertEqual(page["next_after_id"], "alice")
        next_page = self.store.conversations(limit=1, after_id=page["next_after_id"])
        self.assertEqual(next_page["conversations"][0]["conversation_id"], "bob")
        self.assertIsNone(next_page["next_after_id"])

    async def test_failed_context_and_queued_turns_visible_without_outbox(self):
        self.admit(event(text="too long"))
        work = self.store.next_work("alice", "test-config")
        self.store.record_context(work["context"], {"status": "context_rejected", "error_type": "ContextBudgetExceeded"})
        self.store.end_generation(work["event_id"], error_code="generation_ContextBudgetExceeded")
        self.admit(event("2", text="queued"))
        items = self.store.conversation_detail("alice")["items"]
        self.assertEqual(items[0]["turn"]["context_report"]["status"], "context_rejected")
        self.assertEqual(items[0]["error_code"], "generation_ContextBudgetExceeded")
        self.assertEqual(items[1]["state"], "queued")
        self.assertIsNone(items[1]["turn"])
        self.assertTrue(all(i["outbox"] is None for i in items))

    def test_summary_and_attachment_preview_do_not_leak_payloads(self):
        incoming = event(text="PRIVATE_TEXT")
        incoming.sender_name = "<script>private nickname</script>"
        incoming.segments.extend([MessageSegment("image", image_base64="BASE64_PRIVATE", image_url="https://private.invalid/image"),
                                  MessageSegment("mention", mention_target="42", mention_display="friend")])
        self.admit(incoming)
        summary = json.dumps(self.store.conversations())
        self.assertNotIn("PRIVATE_TEXT", summary)
        self.assertNotIn("private nickname", summary)
        detail = self.store.conversation_detail("alice")
        self.assertNotIn("BASE64_PRIVATE", json.dumps(detail))
        self.assertNotIn("private.invalid", json.dumps(detail))
        user = detail["items"][0]["user_message"]
        self.assertEqual(user["speaker_name"], incoming.sender_name)
        self.assertEqual([p["type"] for p in user["segments"]], ["text", "image", "mention"])

    def test_bad_query_values_are_rejected_without_database_changes(self):
        self.admit()
        for value in (0, -1, 101, True, "1.5", "NaN", "1 OR 1=1"):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                self.store.conversations(limit=value)
        for value in (0, -1, True, "bad", str(2**63)):
            with self.subTest(cursor=value), self.assertRaises(ValueError):
                self.store.conversation_detail("alice", before_seq=value)
        with self.assertRaises(LookupError):
            self.store.conversation_detail("missing")
        self.assertEqual(len(self.store.conversation_detail("alice")["items"]), 1)


if __name__ == "__main__":
    unittest.main()
