from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.cells.generator import Generator
from waifu_standalone.cells.skill_registry import (
    SkillRegistry,
    SkillSpec,
    build_skill_markdown_template,
    parse_skill_file,
)
from waifu_standalone.config import AppConfig
from waifu_standalone.models import EmotionState, InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.systems.searching import SearchResult


class SkillRegistryTests(unittest.TestCase):
    def test_parse_skill_file_reads_frontmatter_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_path = Path(tmpdir) / "brief.md"
            skill_path.write_text(
                """---
id: brief-mode
name: 简洁输出
description: 压缩回答长度
triggers: ["简短点", "一句话"]
mode: prefix
priority: 6
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: summary
command-arg-mode: raw
---
只给结论，不要绕。
""",
                encoding="utf-8",
            )

            skill = parse_skill_file(skill_path)

            self.assertEqual(skill.skill_id, "brief-mode")
            self.assertEqual(skill.name, "简洁输出")
            self.assertEqual(skill.triggers, ["简短点", "一句话"])
            self.assertEqual(skill.mode, "prefix")
            self.assertTrue(skill.dispatches_tool)
            self.assertTrue(skill.disable_model_invocation)
            self.assertEqual(skill.command_tool, "summary")
            self.assertIn("只给结论", skill.content)

    def test_registry_loads_builtin_skills(self) -> None:
        registry = SkillRegistry(AppConfig())

        names = [skill.name for skill in registry.list_skills()]

        self.assertIn("时效性核验", names)
        self.assertIn("简洁回答", names)
        self.assertIn("生图交付语气", names)
        self.assertIn("联网检索", names)
        self.assertIn("会话总结", names)
        self.assertIn("直接生图", names)

    def test_registry_toggle_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(AppConfig(data_root=tmpdir))

            updated = registry.set_enabled("search-command", False)
            assert updated is not None
            reloaded = SkillRegistry(AppConfig(data_root=tmpdir))
            skill = reloaded.get_skill("search-command")

            self.assertIsNotNone(skill)
            assert skill is not None
            self.assertFalse(skill.enabled)

    def test_registry_can_install_save_and_delete_workspace_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(AppConfig(data_root=tmpdir))
            markdown = build_skill_markdown_template(
                skill_id="weather-radar",
                name="Weather Radar",
                description="workspace search helper",
                triggers=["weather radar"],
                mode="prefix",
                priority=9,
                body="Use web search before answering.",
            )

            installed = registry.install_workspace_skill(markdown)
            self.assertEqual(installed.skill_id, "weather-radar")
            self.assertEqual(installed.source_kind, "workspace")
            self.assertIn("Use web search", registry.get_skill_markdown("weather-radar") or "")

            saved = registry.save_workspace_skill(
                "weather-radar",
                markdown.replace("weather radar", "forecast radar").replace("search helper", "edited helper"),
            )
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved.skill_id, "weather-radar")
            self.assertIn("forecast radar", registry.get_skill_markdown("weather-radar") or "")

            deleted = registry.delete_workspace_skill("weather-radar")
            self.assertTrue(deleted)
            self.assertIsNone(registry.get_skill("weather-radar"))

    def test_service_records_active_skills_in_session_and_snapshot(self) -> None:
        service, _ = build_default_service()

        service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="简短点，直接说结论")],
            )
        )

        session = service.memory.load("783190298", "person")
        active_skills = session.metadata.get("active_skills", [])
        snapshot = service.dashboard_snapshot()
        listed_session = snapshot["sessions"][0]

        self.assertTrue(active_skills)
        self.assertEqual(snapshot["skills"]["enabled"], True)
        self.assertGreaterEqual(snapshot["skills"]["count"], 6)
        self.assertGreaterEqual(listed_session["active_skill_count"], 1)

    def test_image_request_keeps_skill_match(self) -> None:
        service, _ = build_default_service()

        result = service.handle_event(
            InboundEvent(
                launcher_id="1101040950",
                launcher_type="group",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="draw: warm spring sky")],
            )
        )

        session = service.memory.load("1101040950", "group")
        active_skills = session.metadata.get("active_skills", [])
        names = [item.get("name") for item in active_skills if isinstance(item, dict)]

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.images, ["generated://warm spring sky"])
        self.assertIn("生图交付语气", names)
        self.assertIn("直接生图", names)

    def test_disabling_image_command_stops_direct_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            service.set_skill_enabled("image-command", False)

            result = service.handle_event(
                InboundEvent(
                    launcher_id="1101040950",
                    launcher_type="group",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="draw: warm spring sky")],
                )
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.images, [])

    def test_direct_search_skill_dispatches_tool(self) -> None:
        service, _ = build_default_service()
        service.search._fetcher = lambda query: [
            SearchResult(title="北京天气", snippet=f"{query}：晴，最高温 26 度", url="https://example.com/weather")
        ]

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="搜一下 北京天气")],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("我帮你查了一下", reply.text)
        self.assertIn("北京天气", reply.text)

    def test_direct_summary_skill_dispatches_tool(self) -> None:
        service, _ = build_default_service()
        service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="今天我想把页面做得更轻一点")],
            )
        )

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="总结一下")],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("我先帮你收一下重点", reply.text)

    def test_generator_injects_only_prompt_visible_skills(self) -> None:
        generator = Generator(AppConfig())
        session = SessionMemory(launcher_id="1", launcher_type="person")
        event = InboundEvent(
            launcher_id="1",
            launcher_type="person",
            sender_id="2",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="今天怎么样")],
        )
        visible_skill = SkillSpec(
            skill_id="freshness-check",
            name="时效性核验",
            description="提醒时效性话题需要先核验",
            triggers=["今天"],
            mode="contains",
            priority=8,
            content="先提醒对方这类内容最好核验。",
            source="builtin",
        )
        hidden_tool_skill = SkillSpec(
            skill_id="search-command",
            name="联网搜索",
            description="直接调用搜索工具",
            triggers=["搜一下"],
            mode="prefix",
            priority=12,
            content="直接调用搜索工具。",
            source="builtin",
            disable_model_invocation=True,
            command_dispatch="tool",
            command_tool="search",
        )

        prompt = generator._build_chat_query(
            event,
            session,
            EmotionState(),
            card=generator._cards.load("person", session),
            assistant_name="琉璃",
            address="tester",
            search_hint="",
            search_context="",
            conversation_view="",
            memory_hints=[],
            speaker_notes=[],
            analysis_hint="",
            latest_message="今天怎么样",
            active_skills=[visible_skill, hidden_tool_skill],
        )

        self.assertIn("[Active Skills]", prompt)
        self.assertIn("时效性核验", prompt)
        self.assertIn("先提醒对方这类内容最好核验。", prompt)
        self.assertNotIn("联网搜索", prompt)

    def test_service_exposes_registered_tools(self) -> None:
        service, _ = build_default_service()

        tools = service.list_tools()
        tool_ids = {item["id"] for item in tools["items"]}

        self.assertEqual(tools["count"], 3)
        self.assertSetEqual(tool_ids, {"image", "search", "summary"})


if __name__ == "__main__":
    unittest.main()
