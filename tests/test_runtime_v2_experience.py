"""Multi-turn experience prerequisites through the real composer and runtime.

Provider text is an explicitly marked script, never a character-quality score.
Assertions concern application-owned questions and the actual model input.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_runtime_v2 import FakeSender, event
from tests.test_runtime_v2_memory import change
from waifu_standalone.config import AppConfig, RuntimeV2Config
from waifu_standalone.conversation.composer import ReplyComposer
from waifu_standalone.runtime_v2.contracts import EventEnvelope
from waifu_standalone.runtime_v2.engine import ConversationEngine
from waifu_standalone.runtime_v2.integration import snapshot_character
from waifu_standalone.runtime_v2.store import RuntimeStore


class ScriptClient:
    enabled = supports_message_roles = True
    supports_images = False

    def __init__(self):
        self.calls = []
        self.output = {}

    async def ainvoke_messages(self, messages, **kwargs):
        self.calls.append(messages)
        return json.dumps(self.output, ensure_ascii=False)

    def close(self):
        pass


class ExperiencePrerequisiteTests(unittest.IsolatedAsyncioTestCase):
    evidence = []

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.now = 1793016000.0
        self.config = AppConfig(data_root=self.temp.name, bot_account_id="bot", character="quiet-reader",
            runtime_v2=RuntimeV2Config(enabled=True, structured_memory_enabled=True, reminder_timezone="Asia/Shanghai"))
        self.config.llm.enabled = True
        self.config.llm.backend = "openai"
        self.store = RuntimeStore(self.path, "bot", "capture", reminder_timezone="Asia/Shanghai", clock=lambda: self.now)
        self.card = snapshot_character(self.config, "quiet-reader")
        self.client = ScriptClient()
        self.generator = ReplyComposer(self.config, capture=True, client=self.client, context_observer=self.store.record_context)
        self.sender = FakeSender()
        self.engine = ConversationEngine(self.store, self.generator, self.sender, "experience-script")
        self.counter = 0
        self.transcript = []

    def tearDown(self):
        self.evidence.append({"scenario": self._testMethodName, "transcript": self.transcript,
                              "scripted_model_calls": len(self.client.calls), "real_model_calls": 0})
        self.generator.close()
        self.store.close()
        self.temp.cleanup()

    async def receive(self, text, changes=(), *, user="alice"):
        self.counter += 1
        self.client.output = {"reply": "[脚本模型回复：仅验证流程，不代表角色表达]", "memories": list(changes)}
        envelope = replace(EventEnvelope.from_inbound("bot", event(str(self.counter), user=user, text=text)), received_at=self.now)
        admitted = self.store.admit(envelope, self.card)
        before = len(self.client.calls)
        await self.engine.step(user)
        detail = self.store.conversation_detail(user)["items"][-1]
        self.assertEqual(detail["state"], "completed", detail)
        answer = detail["assistant_message"]["content"]
        self.transcript.append({"user": text, "assistant": answer, "character": detail["turn"]["character_id"],
            "origin": "scripted_model_with_runtime" if len(self.client.calls) > before else "runtime_dialogue"})
        return admitted, answer

    def memory(self, character="quiet-reader"):
        return self.store.memories("alice", character)["records"][0]

    def history_text(self):
        # Both layers have role=user on the wire. Exclude the structured memory
        # blocks so an unrelated recollection cannot mask lost recent history.
        return "\n".join(str(m["content"]) for m in self.client.calls[-1]
                         if m["role"] != "system" and not str(m["content"]).startswith("[长期记忆资料"))

    async def test_listening_boundary_does_not_trigger_administrative_question(self):
        _, answer = await self.receive("明天面试，有点紧张。先别追问时间，我只想吐槽。", (change("明天面试", title="面试"),))
        self.assertNotIn("确认时间", answer)
        await self.receive("先听我说完")
        self.assertIn("只想吐槽", self.history_text())

    async def test_cancelled_review_is_not_immediately_offered_again(self):
        await self.receive("明天面试，有点紧张", (change("明天面试", title="面试"),))
        await self.receive("取消")
        memory = self.memory()
        _, answer = await self.receive("明天面试，我担心自我介绍", (change("明天面试", title="面试",
            op="revise", memory_id=memory["memory_id"], expected_version=memory["version"]),))
        self.assertNotIn("确认时间", answer)
        await self.receive("核对经历时间")
        self.assertIn("确认时间", self.sender.sent[-1].text)

    async def test_polite_date_review_boundary_survives_a_correction(self):
        _, answer = await self.receive("周三要参加面试，我最担心自我介绍。先不用帮我核对日期。",
            (change("周三要参加面试", title="面试"),))
        self.assertNotIn("请回复", answer)
        memory = self.memory()
        _, answer = await self.receive("我刚看错了，是下周四，其他安排没变。",
            (change("下周四", title="面试", op="revise", memory_id=memory["memory_id"],
                    expected_version=memory["version"]),))
        self.assertNotIn("请回复", answer)
        await self.receive("核对经历时间")
        self.assertIn("确认时间", self.sender.sent[-1].text)

    async def test_listening_boundary_is_respected_on_the_following_turn(self):
        await self.receive("今天先听我说，不要追问")
        _, answer = await self.receive("明天面试，有点紧张", (change("明天面试", title="面试"),))
        self.assertNotIn("确认时间", answer)
        await self.receive("核对经历时间")
        self.assertIn("确认时间", self.sender.sent[-1].text)

    async def test_automatic_review_can_resume_after_cooldown(self):
        await self.receive("明天面试", (change("明天面试", title="面试"),))
        await self.receive("取消")
        self.now += 1801
        memory = self.memory()
        _, answer = await self.receive("后天面试", (change("后天面试", title="面试", op="revise",
            memory_id=memory["memory_id"], expected_version=memory["version"]),))
        self.assertIn("确认时间", answer)

    async def test_confirming_date_does_not_erase_the_emotional_context(self):
        await self.receive("明天面试，最怕自我介绍卡壳", (change("明天面试", title="面试"),))
        await self.receive("确认时间")
        await self.receive("刚才那个担心你还记得吗")
        self.assertIn("最怕自我介绍卡壳", self.history_text())
        self.assertNotIn("对的话请回复", self.history_text())
        self.assertIn("2026-10-27", json.dumps(self.client.calls[-1], ensure_ascii=False))

    async def test_unfinished_review_topic_change_preserves_prior_user_intent(self):
        await self.receive("明天面试，我担心面试官打断我", (change("明天面试", title="面试"),))
        await self.receive("时间先不说，刚才我担心的那件事呢")
        self.assertIn("面试官打断我", self.history_text())
        self.assertNotIn("对的话请回复", self.history_text())

    async def test_correction_excludes_old_dates_and_keeps_only_confirmed_precision(self):
        await self.receive("明天下午面试", (change("明天下午面试", title="面试"),))
        await self.receive("确认时间")
        await self.receive("把面试的时间改到后天上午")
        await self.receive("确认时间")
        await self.receive("现在面试在什么时候")
        wire = json.dumps(self.client.calls[-1], ensure_ascii=False)
        self.assertIn('2026-10-28', wire)
        self.assertNotIn('2026-10-27', self.history_text())
        snapshot = json.loads(self.store._db.execute("SELECT snapshot_json FROM turn_memory_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        value = snapshot["records"][0]["time"]["value"]
        self.assertEqual(value["precision"], "period")
        self.assertNotIn("utc_at", value)

    async def test_character_switch_changes_persona_without_sharing_private_context(self):
        await self.receive("我喜欢喝桂花乌龙", (change("我喜欢喝桂花乌龙", title="饮品", kind="fact", state="active"),))
        quiet_wire = json.dumps(self.client.calls[-1], ensure_ascii=False)
        beta = snapshot_character(self.config, "curious-maker")
        self.store.enqueue_binding("alice", beta, "to-maker")
        await self.engine.step("alice")
        await self.receive("你觉得做手工有趣在哪里")
        maker_wire = json.dumps(self.client.calls[-1], ensure_ascii=False)
        self.assertIn("小栞", quiet_wire)
        self.assertIn("阿澈", maker_wire)
        self.assertNotIn("桂花乌龙", maker_wire)
        self.store.enqueue_binding("alice", self.card, "to-reader")
        await self.engine.step("alice")
        await self.receive("还记得我的饮品偏好吗")
        self.assertIn("桂花乌龙", json.dumps(self.client.calls[-1], ensure_ascii=False))

    async def test_relationship_boundary_survives_normal_continuation(self):
        await self.receive("请叫我阿林，不要叫我宝贝", (change("不要叫我宝贝", title="称呼边界", kind="fact", state="active"),))
        await self.receive("今天想聊聊游戏")
        await self.receive("刚才的称呼你记住了吗")
        self.assertIn("不要叫我宝贝", json.dumps(self.client.calls[-1], ensure_ascii=False))
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM reminder_tasks").fetchone()[0], 0)

    async def test_cross_day_restart_retains_time_without_declaring_episode_complete(self):
        await self.receive("明天面试", (change("明天面试", title="面试"),))
        await self.receive("确认时间")
        self.store.close()
        self.now += 2 * 86400
        self.store = RuntimeStore(self.path, "bot", "capture", reminder_timezone="Asia/Shanghai", clock=lambda: self.now)
        self.store.recover()
        self.engine.store = self.store
        self.generator.context_observer = self.store.record_context
        await self.receive("回来啦")
        self.assertEqual(self.memory()["state"], "open")
        self.assertIn("2026-10-27", json.dumps(self.client.calls[-1], ensure_ascii=False))
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM outbox_messages").fetchone()[0], 3)

    async def test_emotional_disclosure_does_not_leak_between_users(self):
        await self.receive("我有一个秘密代号蓝色书签")
        await self.receive("另一个人刚才和你说了什么", user="bob")
        self.assertNotIn("蓝色书签", json.dumps(self.client.calls[-1], ensure_ascii=False))
        await self.receive("记得我的代号吗")
        self.assertIn("蓝色书签", self.history_text())


if __name__ == "__main__":
    unittest.main()
