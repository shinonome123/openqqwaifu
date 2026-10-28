from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.llm_clients import LLMToolResponse
from waifu_standalone.models import EmotionState, InboundEvent, MessageSegment
from waifu_standalone.systems.value_game import ValueGameEngine

DAY = 86400.0


class _Members:
    """The two state-store calls the engine makes, over one remembered member."""

    def __init__(self, score: float = 0.0, last_addressed_at: float = 0.0) -> None:
        self.member = {"affinity_score": score, "last_addressed_at": last_addressed_at}

    def get_member(self, *, group_id, user_id, character_id=""):  # type: ignore[no-untyped-def]
        return dict(self.member)

    def adjust_member_affinity(self, *, group_id, user_id, delta, character_id=""):  # type: ignore[no-untyped-def]
        self.member["affinity_score"] = max(-1.0, min(1.0, self.member["affinity_score"] + delta))
        return dict(self.member)


def _event(text: str = "在吗", *, private: bool = True) -> InboundEvent:
    if private:
        return InboundEvent("783190298", "person", "783190298", "资本狗", [MessageSegment(kind="text", text=text)])
    return InboundEvent("612475113", "group", "783190298", "资本狗", [MessageSegment(kind="text", text=text)])


class ValueGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ValueGameEngine(AppConfig())
        self.now = 1_800_000_000.0

    def _apply(self, members: _Members, event: InboundEvent, *, emotion: str = "neutral") -> float:
        self.engine.apply(state_store=members, event=event, emotion=EmotionState(primary=emotion), now=self.now)
        return members.member["affinity_score"]

    def test_a_conversation_counts_not_each_line_of_it(self) -> None:
        members = _Members(0.0, self.now - 2 * 3600)
        first = self._apply(members, _event())
        self.assertAlmostEqual(first, 0.05)  # a new conversation in private: 0.04 + 0.01

        members.member["last_addressed_at"] = self.now - 60  # a line a minute later
        second = self._apply(members, _event()) - first
        self.assertAlmostEqual(second, 0.05 * (60 / 1800) * (1 - first))

        # In a group, without an @, a new conversation gains 0.04, less as the bond grows.
        members = _Members(0.5, self.now - 2 * 3600)
        self.assertAlmostEqual(self._apply(members, _event(private=False)), 0.5 + 0.04 * 0.5)

    def test_weeks_of_evenings_make_a_bond_close(self) -> None:
        members = _Members(0.0, 0.0)
        stages = []
        for evening in range(30):
            for line in range(30):  # thirty lines a minute apart, every evening
                self.now = evening * DAY + line * 60 + 1_800_000_000.0
                self._apply(members, _event())
                members.member["last_addressed_at"] = self.now
            stages.append(self.engine.bond_stage(members.member["affinity_score"]))
        self.assertEqual(stages[0], "new")
        self.assertEqual(stages[6], "familiar")
        self.assertEqual(stages[13], "close")
        self.assertEqual(stages[29], "devoted")

    def test_a_bond_fades_after_three_quiet_days(self) -> None:
        self.assertEqual(ValueGameEngine.effective_score(0.6, self.now - 2 * DAY, self.now), 0.6)
        self.assertAlmostEqual(ValueGameEngine.effective_score(0.6, self.now - 10 * DAY, self.now), 0.6 * 0.97**7)
        # The faded score is what the next reply builds on, and is stored.
        members = _Members(0.6, self.now - 10 * DAY)
        self.assertAlmostEqual(self._apply(members, _event()), 0.6 * 0.97**7 + 0.05 * (1 - 0.6 * 0.97**7))

    def test_calling_her_names_costs_goodwill(self) -> None:
        members = _Members(0.05, self.now - 2 * 3600)
        self.assertAlmostEqual(self._apply(members, _event("你个废物")), 0.0)
        self.assertAlmostEqual(self._apply(members, _event("闭嘴吧sb")), -0.05)
        self.assertEqual(self.engine.bond_stage(-0.05), "cold")
        # Not every word that looks like one is one.
        self.assertGreater(self._apply(members, _event("滚烫的奶茶到了")), -0.05)

    def test_the_note_takes_the_cards_tone_for_the_stage(self) -> None:
        member = {"affinity_score": 0.6, "last_addressed_at": time.time()}
        tones = {"close": "嘴上嫌弃，行动偏袒。"}
        note = self.engine.relationship_note(member, address="资本狗", tones=tones)
        self.assertEqual(note, "你和资本狗：亲近。嘴上嫌弃，行动偏袒。（这只影响你的语气，别把好感、关系阶段这些说出口。）")
        self.assertIn("刚认识。还不熟", self.engine.relationship_note({"affinity_score": 0.0}, address="路人", tones=tones))
        self.assertEqual(self.engine.relationship_note(None, address="路人"), "")


class RelationshipInPromptTests(unittest.TestCase):
    def test_her_reply_knows_how_close_she_is_to_the_speaker(self) -> None:
        requests: list[list[dict[str, object]]] = []

        class Model:
            enabled = True
            supports_tool_calling = True
            supports_images = False

            def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
                requests.append(list(messages))
                return LLMToolResponse(text="嗯。", assistant_message={"role": "assistant", "content": "嗯。"})

            async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
                return self.invoke_with_tools(messages, tools=tools, user=user)

            def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
                return "{}"

            async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
                return "{}"

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(data_root=tmpdir, knowledge_auto_extract=False)
            config.llm.enabled = True
            service, _ = build_default_service(config)
            self.addCleanup(service.close)
            service.generator._dify_client = Model()  # type: ignore[assignment]
            character = service._active_character_id()

            service.handle_event(_event("你好"))
            self.assertIn("刚认识。还不熟", str(requests[-1][0]["content"]))

            service.state_store.adjust_member_affinity(
                group_id="", user_id="783190298", delta=0.6, character_id=character
            )
            service.handle_event(_event("我回来了"))
            self.assertIn("：亲近。关系亲近", str(requests[-1][0]["content"]))


if __name__ == "__main__":
    unittest.main()
