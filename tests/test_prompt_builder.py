from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.cards import CharacterCard
from waifu_standalone.cells.prompt_builder import PromptBuilder, RelationshipContext


class PromptBuilderTests(unittest.TestCase):
    def test_build_reply_prompt_unifies_persona_relationship_and_latest_message(self) -> None:
        builder = PromptBuilder()
        prompt = builder.build_reply_prompt(
            card=CharacterCard(
                assistant_name="琉璃",
                language="zh-CN",
                profile=["温柔体贴的 AI 伙伴"],
                skills=["会自然接住对方情绪"],
                background=["喜欢轻松口语聊天"],
                rules=["对亲近的人会主动表达关心"],
                prologue=["先轻轻接住对方的情绪。"],
            ),
            launcher_type="person",
            assistant_name="琉璃",
            relationship=RelationshipContext(
                address="小明",
                bond_stage="familiar",
                affinity_score=0.47,
                profile_summary="小明是程序员。",
            ),
            conversation_view="小明: 昨天加班到很晚\n琉璃: 辛苦了，别太勉强自己",
            memories=["小明喜欢吃火锅"],
            latest_message="我今天好累啊",
            opening_lines=["语气柔和一点。"],
            search_context="[联网参考]\n查询：北京天气\n- 北京天气：晴 26 度",
            active_skills="[当前生效的技能]\n- concise-answer",
        )

        self.assertIn("你是琉璃。", prompt)
        self.assertIn("[人设]", prompt)
        self.assertIn("[回复原则]", prompt)
        self.assertIn("[你和小明的关系]", prompt)
        self.assertIn("关系阶段：familiar（好感度 0.47）", prompt)
        self.assertIn("小明喜欢吃火锅", prompt)
        self.assertIn("[小明刚刚说]", prompt)
        self.assertIn("我今天好累啊", prompt)
        self.assertNotIn("[Inner Thought]", prompt)
        self.assertNotIn("[Speaker Notes]", prompt)
        self.assertNotIn("[State]", prompt)

    def test_build_onboarding_prompt_reuses_base_sections_and_adds_onboarding_block(self) -> None:
        builder = PromptBuilder()
        prompt = builder.build_onboarding_prompt(
            card=CharacterCard(
                assistant_name="琉璃",
                language="zh-CN",
                profile=["温柔体贴"],
                skills=["会记住称呼"],
                background=["喜欢自然聊天"],
                rules=["先接话再提问"],
                prologue=["轻一点开场。"],
            ),
            launcher_type="person",
            assistant_name="琉璃",
            relationship=RelationshipContext(address="小明"),
            conversation_view="小明: 你好",
            latest_message="我今天好累啊",
            stage="ask_name",
            display_name="小明同学",
            candidate_name="",
            opening_lines=["先接住对方情绪。"],
        )

        self.assertIn("[人设]", prompt)
        self.assertIn("[回复原则]", prompt)
        self.assertIn("[你和小明的关系]", prompt)
        self.assertIn("[最近对话]", prompt)
        self.assertIn("[小明刚刚说]", prompt)
        self.assertIn("[Onboarding]", prompt)
        self.assertIn("小明同学", prompt)
        self.assertNotIn("[Profile]", prompt)
        self.assertNotIn("[State]", prompt)
        self.assertNotIn("[Speaker Notes]", prompt)


if __name__ == "__main__":
    unittest.main()
