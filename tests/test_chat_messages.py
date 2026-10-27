from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.infra.llm_clients import LLMToolResponse
from waifu_standalone.members.onboarding import MemberOnboarding
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.reply_flow.prompt_assembler import _repetition_evidence
from waifu_standalone.skills.tool_intent import relevant_tool_ids

BOT_ID = "3956638110"


class _ChatClient:
    """Records role-tagged requests and answers from a script."""

    enabled = True
    supports_tool_calling = True
    supports_images = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["好呀"]
        self.requests: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []
        self.plain_prompts: list[str] = []

    def _answer(self, messages, tools):  # type: ignore[no-untyped-def]
        self.requests.append(([dict(message) for message in messages], list(tools)))
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return LLMToolResponse(text=text, assistant_message={"role": "assistant", "content": text})

    def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        return self._answer(messages, tools)

    async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        return self._answer(messages, tools)

    def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        self.plain_prompts.append(query)
        return "{}"

    async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return self.invoke(query, user=user)

    def close(self) -> None:
        pass


def _private(text: str, sender: str = "783190298") -> InboundEvent:
    return InboundEvent(
        launcher_id=sender,
        launcher_type="person",
        sender_id=sender,
        sender_name="闵闵",
        segments=[MessageSegment(kind="text", text=text)],
    )


def _group(text: str, sender: str = "783190298", name: str = "小夏") -> InboundEvent:
    return InboundEvent(
        launcher_id="612475113",
        launcher_type="group",
        sender_id=sender,
        sender_name=name,
        segments=[MessageSegment(kind="mention", mention_target=BOT_ID), MessageSegment(kind="text", text=text)],
    )


class ChatMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = AppConfig(data_root=self.temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False)
        config.llm.enabled = True
        self.service, self.outbound = build_default_service(config)
        self.addCleanup(self.service.close)
        # The one-time "what should I call you" question is not what these tests look at.
        async def keep_reply(_self, *args, reply_text: str, **kwargs):  # type: ignore[no-untyped-def]
            return reply_text
        soft_ask = patch.object(MemberOnboarding, "amaybe_append_soft_ask", keep_reply)
        soft_ask.start()
        self.addCleanup(soft_ask.stop)
        self.card_name = self.service.generator.resolve_assistant_name("person", self.service.memory.load("x", "person"))

    def _use(self, client: _ChatClient) -> _ChatClient:
        self.service.generator._dify_client = client  # type: ignore[assignment]
        return client

    def test_persona_is_the_system_message_and_history_is_chat_turns(self) -> None:
        client = self._use(_ChatClient("你好呀", "今天也还行", "嗯"))
        for text in ("你好", "今天怎么样", "给我讲个故事吧"):
            self.service.handle_event(_private(text))

        messages, tools = client.requests[-1]
        self.assertEqual(messages[0]["role"], "system")
        system = str(messages[0]["content"])
        self.assertTrue(system.startswith(f"你是{self.card_name}"))
        self.assertIn("[规则]", system)
        self.assertNotIn("[工具]", system)  # ordinary chat carries no tools
        self.assertEqual(tools, [])
        self.assertEqual(
            [(message["role"], message["content"]) for message in messages[1:]],
            [("user", "你好"), ("assistant", "你好呀"), ("user", "今天怎么样"),
             ("assistant", "今天也还行"), ("user", "给我讲个故事吧")],
        )
        self.assertEqual(client.plain_prompts, [])  # one call, no separate inner-thought prompt

    def test_group_turns_carry_the_speaker_name(self) -> None:
        client = self._use(_ChatClient("在的"))
        self.service.handle_event(_group("在吗"))
        self.service.handle_event(_group("刚才说到哪了", sender="1147026356", name="苦力怕"))

        messages = client.requests[-1][0]
        self.assertEqual(messages[1], {"role": "user", "content": "小夏：在吗"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "在的"})
        self.assertEqual(messages[-1], {"role": "user", "content": "苦力怕：刚才说到哪了"})

    def test_an_alias_does_not_replace_the_bots_name(self) -> None:
        client = self._use(_ChatClient("嗯"))
        self.service.member_repository.save_assistant_alias(
            character_id=self.service._active_character_id(), user_id="783190298", assistant_alias="大母牛"
        )
        self.service.handle_event(_private("你是谁"))

        system = str(client.requests[-1][0][0]["content"])
        self.assertTrue(system.startswith(f"你是{self.card_name}"))
        self.assertIn("管你叫「大母牛」", system)
        self.assertNotIn("你是大母牛", system)

    def test_tools_and_tool_rules_appear_only_when_the_message_needs_them(self) -> None:
        client = self._use(_ChatClient("带吧"))
        self.service.handle_event(_private("出门要带伞吗"))

        messages, tools = client.requests[-1]
        self.assertEqual([tool["name"] for tool in tools], ["weather-command"])
        self.assertIn("[工具]", str(messages[0]["content"]))

    def test_a_copied_reply_is_written_again_once(self) -> None:
        client = self._use(_ChatClient("问这个干嘛，又不是给你讲故事的。", "问这个干嘛，又不是给你讲故事的。", "好吧，我叫极光。"))
        self.service.handle_event(_private("你是谁"))
        reply = self.service.handle_event(_private("你到底是谁"))

        assert reply is not None
        self.assertEqual(reply.text, "好吧，我叫极光。")
        retry = client.requests[-1][0]
        self.assertIn("[别重复]", str(retry[0]["content"]))
        self.assertEqual(len(client.requests), 3)

    def test_extraction_prompt_keeps_the_persona_out(self) -> None:
        event = _private("我周四要面试")
        session = self.service.memory.save_user_event(event, character_id=self.service._active_character_id())
        query = self.service.generator._build_knowledge_query(
            event,
            session,
            assistant_name=self.card_name,
            latest_message="我周四要面试",
            conversation_view="闵闵: 我周四要面试",
            address="主人",
            max_entries=2,
        )
        self.assertNotIn("Persona profile", query)
        self.assertIn("Speaker: 闵闵 (QQ 783190298)", query)
        self.assertIn(f"Never describe {self.card_name}", query)

    def test_profile_summary_drops_fragments_about_the_bots_alias(self) -> None:
        cleaned = self.service.persona.sanitize_profile_summary(
            "喜欢猫; 大母牛 interacts with 主人 in a tsundere manner", extra_names={"大母牛"}
        )
        self.assertEqual(cleaned, "喜欢猫")


class RepetitionEvidenceTests(unittest.TestCase):
    def test_near_copies_and_long_shared_phrases_count(self) -> None:
        previous = ["……哼，问这个干嘛，又不是给你讲故事的。随便什么模型都行。"]
        self.assertTrue(_repetition_evidence("记得啊，闵闵。问这个干嘛，又不是给你讲故事的。", previous))
        self.assertTrue(_repetition_evidence("……哼，问这个干嘛，又不是给你讲故事的。随便什么模型都行！", previous))
        self.assertEqual(_repetition_evidence("今天天气不错，出去走走？", previous), [])

    def test_the_same_opening_four_times_in_a_row_counts(self) -> None:
        previous = ["……你好。", "……我叫极光。", "……别乱叫。"]
        self.assertTrue(_repetition_evidence("……行吧。", previous))
        self.assertEqual(_repetition_evidence("行吧。", previous), [])

    def test_short_replies_may_repeat(self) -> None:
        self.assertEqual(_repetition_evidence("嗯嗯", ["嗯嗯"]), [])


class ToolIntentTests(unittest.TestCase):
    def test_wording_selects_tool_groups(self) -> None:
        self.assertEqual(relevant_tool_ids("给我讲个故事吧"), set())
        self.assertEqual(relevant_tool_ids("今天要带伞吗"), {"weather"})
        self.assertIn("set-preferred-name", relevant_tool_ids("以后叫我主人"))
        self.assertIn("set-preferred-name", relevant_tool_ids("我是阿狗"))
        self.assertEqual(relevant_tool_ids("https://example.com 讲了啥"), {"summarize", "web-fetch"})
        self.assertIn("image-caption", relevant_tool_ids("看看这个", has_images=True))


class DialogueTurnTests(unittest.TestCase):
    def test_turns_follow_the_window_and_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self.addCleanup(service.close)
            session = service.memory.load("1", "person")
            session.history = ["甲: 一", "assistant: 二", "甲: 三", "assistant: 四", "甲: 当前"]
            service.memory.store.save(session)

            turns = service.memory.dialogue_turns("1", "person", limit=3, exclude_last=True)
            self.assertEqual(turns, [("assistant", "二"), ("甲", "三"), ("assistant", "四")])
            budgeted = service.memory.dialogue_turns("1", "person", limit=10, max_chars=20, exclude_last=True)
            self.assertEqual(budgeted[-1], ("assistant", "四"))
            self.assertLess(len(budgeted), 4)


if __name__ == "__main__":
    unittest.main()
