from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.cells.generator import Generator
from waifu_standalone.cells.llm_clients import LLMToolCall, LLMToolResponse
from waifu_standalone.cells.skill_registry import (
    SkillHandlerSpec,
    SkillManifest,
    SkillPolicySpec,
    SkillRegistry,
    SkillSpec,
    SkillTriggerSpec,
    build_skill_markdown_template,
    parse_skill_file,
)
from waifu_standalone.cells.tool_registry import ToolExecutionResult
from waifu_standalone.config import AppConfig, LLMConfig, ToolPolicyConfig
from waifu_standalone.http_transport import HttpResponse
from waifu_standalone.models import EmotionState, InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.systems.searching import SearchResult


class SkillRegistryTests(unittest.TestCase):
    def _manifest(
        self,
        *,
        skill_id: str,
        name: str,
        description: str = "",
        keywords: list[str] | None = None,
        priority: int = 0,
        content: str = "",
        handler_type: str = "prompt_template",
        handler_target: str = "",
        llm_tool: bool = False,
        source: str = "builtin",
    ) -> SkillManifest:
        return SkillManifest(
            skill_id=skill_id,
            name=name,
            description=description,
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object", "properties": {}},
            trigger=SkillTriggerSpec(command=skill_id, llm_tool=llm_tool, keywords=list(keywords or [])),
            handler=SkillHandlerSpec(type=handler_type, target=handler_target or skill_id),
            policy=SkillPolicySpec(priority=priority),
            default_args={},
            metadata={},
            content=content,
            source=source,
        )

    def _install_tool_client(
        self,
        service,
        *,
        tool_name: str,
        arguments: dict[str, object] | None = None,
        final_text: str = "工具调用完成。",
    ):
        class _SingleToolClient:
            enabled = True
            supports_tool_calling = True
            base_url = "https://example.com/v1"
            api_key = "secret"
            model = "tool-model"
            backend = "openai"
            timeout_seconds = 45.0
            app_type = "chat"

            def __init__(self) -> None:
                self.calls = 0
                self.seen_tools: set[str] = set()
                self.last_tool_message: dict[str, object] = {}

            def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return final_text

            async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return final_text

            def invoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                raise AssertionError("sync tool loop should not run in async event path")

            async def ainvoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                self.calls += 1
                if self.calls == 1:
                    self.seen_tools = {item["name"] for item in tools}
                    return LLMToolResponse(
                        tool_calls=[
                            LLMToolCall(
                                call_id="call_1",
                                tool_name=tool_name,
                                arguments=dict(arguments or {}),
                            )
                        ],
                        assistant_message={
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(dict(arguments or {}), ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                    )
                self.last_tool_message = messages[-1]
                return LLMToolResponse(
                    text=final_text,
                    assistant_message={"role": "assistant", "content": final_text},
                )

        client = _SingleToolClient()
        service.config.llm.enabled = True
        service.generator._dify_client = client  # type: ignore[assignment]
        return client

    def test_parse_skill_file_reads_frontmatter_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_path = Path(tmpdir) / "brief.md"
            skill_path.write_text(
                """---
id: brief-mode
name: 简洁输出
description: 压缩回答长度
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"brief-mode","llm_tool":true,"keywords":["简短点","一句话"]}
handler: {"type":"tool_id","target":"summary","arg_mode":"raw"}
policy: {"priority":6,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
只给结论，不要绕。
""",
                encoding="utf-8",
            )

            skill = parse_skill_file(skill_path)

            self.assertEqual(skill.skill_id, "brief-mode")
            self.assertEqual(skill.name, "简洁输出")
            self.assertEqual(skill.triggers, ["简短点", "一句话"])
            self.assertTrue(skill.dispatches_tool)
            self.assertFalse(skill.disable_model_invocation)
            self.assertEqual(skill.command_tool, "summary")
            self.assertIn("只给结论", skill.content)

    def test_parse_skill_file_supports_multiline_trigger_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_path = Path(tmpdir) / "skill-list.md"
            skill_path.write_text(
                """---
id: skill-list-command
name: 技能列表
description: 列出当前可用技能
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"skill-list-command","llm_tool":true,"keywords":["技能菜单","说说你会的技能"]}
handler: {"type":"tool_id","target":"skill-list","arg_mode":"structured"}
policy: {"priority":6,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
直接展示技能列表。
""",
                encoding="utf-8",
            )

            skill = parse_skill_file(skill_path)

            self.assertEqual(skill.skill_id, "skill-list-command")
            self.assertEqual(skill.triggers, ["技能菜单", "说说你会的技能"])
            self.assertTrue(skill.dispatches_tool)

    def _legacy_registry_loads_builtin_skills(self) -> None:
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

    def test_registry_loads_builtin_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(AppConfig(data_root=tmpdir))

            skill_ids = {skill.skill_id for skill in registry.list_skills()}

            self.assertSetEqual(
                {
                    "freshness-check",
                    "concise-answer",
                    "image-handoff",
                    "search-command",
                    "summary-command",
                    "image-command",
                    "skill-list-command",
                    "summarize-command",
                    "web-fetch-command",
                    "read-file-command",
                    "list-files-command",
                    "search-links-command",
                    "weather-command",
                    "write-file-command",
                    "exec-command",
                },
                skill_ids,
            )

    def test_parse_skill_file_supports_openclaw_metadata_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_path = Path(tmpdir) / "openclaw.md"
            skill_path.write_text(
                """---
name: openclaw_skill
description: OpenClaw-compatible metadata.
homepage: https://example.com/skill
metadata: {"openclaw": {"requires": {"bins": ["uv"], "env": ["OPENQQWAIFU_TEST_KEY"]}, "homepage": "https://fallback.example.com"}}
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"openclaw","llm_tool":false,"keywords":[]}
handler: {"type":"prompt_template","target":"openclaw"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Use tools carefully.
""",
                encoding="utf-8",
            )

            skill = parse_skill_file(skill_path)

            self.assertEqual(skill.skill_id, "openclaw")
            self.assertEqual(skill.homepage, "https://example.com/skill")
            self.assertEqual(
                skill.metadata.get("openclaw", {}).get("requires", {}).get("env"),
                ["OPENQQWAIFU_TEST_KEY"],
            )

    def test_parse_skill_file_uses_name_when_skill_md_has_no_explicit_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "codex-agent"
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text(
                """---
name: codex-agent
description: Bundle style skill without explicit id.
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"codex-agent","llm_tool":false,"keywords":[]}
handler: {"type":"prompt_template","target":"codex-agent"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Use bundle-relative resources.
""",
                encoding="utf-8",
            )

            skill = parse_skill_file(skill_path)

            self.assertEqual(skill.skill_id, "codex-agent")

    def test_parse_skill_file_supports_aliases_and_registry_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "alias.md").write_text(
                """---
id: alias-skill
name: alias_skill
aliases: ["lookup-weather", "weather_lookup"]
description: alias test
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"alias-skill","llm_tool":false,"keywords":["weather alias"]}
handler: {"type":"prompt_template","target":"alias-skill"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Use aliases.
""",
                encoding="utf-8",
            )

            registry = SkillRegistry(AppConfig(data_root=tmpdir))
            self.assertIsNotNone(registry.find_by_name_or_id("lookup-weather"))
            self.assertIsNotNone(registry.find_by_name_or_id("weather_lookup"))

    def test_registry_marks_openclaw_skill_ineligible_when_requirements_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "gated.md").write_text(
                """---
id: gated-skill
name: gated_skill
description: Requires unavailable runtime bits.
metadata: {"openclaw": {"requires": {"bins": ["definitely-missing-openqqwaifu-bin"], "env": ["OPENQQWAIFU_NEVER_SET_ENV"]}}}
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"gated-skill","llm_tool":false,"keywords":["gated"]}
handler: {"type":"prompt_template","target":"gated-skill"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Do gated work.
""",
                encoding="utf-8",
            )

            registry = SkillRegistry(AppConfig(data_root=tmpdir))
            skill = registry.get_skill("gated-skill")

            self.assertIsNotNone(skill)
            assert skill is not None
            self.assertFalse(skill.eligible)
            self.assertIn("missing bin:definitely-missing-openqqwaifu-bin", skill.ineligibility_reasons)
            self.assertNotIn("gated-skill", {skill.skill_id for skill in registry.candidate_manifests("gated")})

    def test_registry_prefers_exact_workspace_id_and_auto_binds_known_tool_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "skill.md").write_text(
                """---
id: summarize
name: summarize
description: Summarize URLs and videos.
metadata: {"nanobot": {"requires": {"bins": ["summarize"]}}}
input_schema: {"type":"object","properties":{"target":{"type":"string"}},"required":["target"]}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"summarize","llm_tool":true,"keywords":["summarize"]}
handler: {"type":"tool_id","target":"summarize","arg_mode":"structured"}
policy: {"priority":10,"user_invocable":true,"risk_level":"safe","timeout_seconds":90,"max_output_chars":10000}
default_args: {}
---
Use summarize for URLs.
""",
                encoding="utf-8",
            )

            registry = SkillRegistry(AppConfig(data_root=tmpdir))
            skill = registry.find_by_name_or_id("summarize")

            self.assertIsNotNone(skill)
            assert skill is not None
            self.assertEqual(skill.skill_id, "summarize")
            self.assertEqual(skill.command_dispatch, "tool")
            self.assertEqual(skill.command_tool, "summarize")
            self.assertIn("summarize", skill.triggers)
            self.assertTrue(skill.eligible)

    def test_service_records_active_skills_in_session_and_snapshot(self) -> None:
        service, _ = build_default_service(AppConfig(data_root=tempfile.mkdtemp()))

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
        client = self._install_tool_client(
            service,
            tool_name="image-command",
            arguments={"prompt": "warm spring sky"},
            final_text="图片生成好了。",
        )

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
        self.assertIn("image-command", client.seen_tools)
        self.assertTrue(names)
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
        client = self._install_tool_client(
            service,
            tool_name="search-command",
            arguments={"query": "北京天气"},
            final_text="我帮你查了一下，北京天气：晴，最高温 26 度。",
        )
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
        self.assertIn("search-command", client.seen_tools)
        session = service.memory.load("783190298", "person")
        tool_calls = session.metadata.get("last_tool_calls", [])
        self.assertEqual(tool_calls[0]["tool_id"], "search-command")
        self.assertEqual(tool_calls[0]["status"], "ok")

    def test_direct_summary_skill_dispatches_tool(self) -> None:
        service, _ = build_default_service()
        self._install_tool_client(
            service,
            tool_name="summary-command",
            arguments={},
            final_text="我先帮你收一下重点：今天想把页面做得更轻一点。",
        )
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

    def test_skill_list_dispatch_accepts_skill_menu_phrases(self) -> None:
        service, _ = build_default_service()
        self._install_tool_client(
            service,
            tool_name="skill-list-command",
            arguments={},
            final_text="我目前掌握的技能都列出来了。",
        )

        for text in ("技能菜单", "你会干什么", "说说你会的技能", "help"):
            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text=text)],
                )
            )

            self.assertIsNotNone(reply, msg=text)
            assert reply is not None
            self.assertTrue(reply.text)
            self.assertIn("掌握的技能", reply.text, msg=text)

    def test_intent_router_dispatches_skill_list_and_keeps_raw_block(self) -> None:
        service, _ = build_default_service(AppConfig(data_root=tempfile.mkdtemp(), llm=LLMConfig(enabled=True)))
        self._install_tool_client(
            service,
            tool_name="skill-list-command",
            arguments={},
            final_text="好呀，我把现在能做的事情摊开给你看。",
        )

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="列出技能")],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("摊开给你看", reply.text)
        session = service.memory.load("783190298", "person")
        tool_calls = session.metadata.get("last_tool_calls", [])
        self.assertEqual(tool_calls[0]["tool_id"], "skill-list-command")

    def test_tool_orchestrator_prefers_summarize_for_url_summary(self) -> None:
        service, _ = build_default_service(AppConfig(data_root=tempfile.mkdtemp(), llm=LLMConfig(enabled=True)))
        self._install_tool_client(
            service,
            tool_name="summarize-command",
            arguments={"target": "https://example.com/post"},
            final_text="我已经替你看过这个网页了，核心意思是：这是网页内容摘要。",
        )
        service.dispatcher._invoke_summarize_cli = lambda target, invocation: (  # type: ignore[method-assign]
            {
                "summary": "这是网页内容摘要。",
                "extracted": {"title": "测试网页标题"},
            },
            "",
        )

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[
                    MessageSegment(
                        kind="text",
                        text="总结一下这个网页：https://example.com/post",
                    )
                ],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("替你看过这个网页", reply.text)
        self.assertIn("这是网页内容摘要", reply.text)
        session = service.memory.load("783190298", "person")
        active_skills = session.metadata.get("active_skills", [])
        self.assertTrue(active_skills)
        self.assertTrue(any(item.get("id") == "summarize-command" for item in active_skills))

    def test_tool_orchestrator_clarifies_ambiguous_skill_request(self) -> None:
        class _FakeToolClient:
            enabled = True
            supports_tool_calling = True
            base_url = "https://example.com/v1"
            api_key = "secret"
            model = "tool-model"
            backend = "openai"
            timeout_seconds = 45.0
            app_type = "chat"

            def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return "你是想让我总结刚才的对话，还是总结一个链接或视频？"

            async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return self.invoke(query, user=user)

            def invoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                raise AssertionError("sync tool loop should not run in async event path")

            async def ainvoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                return LLMToolResponse(
                    text="你是想让我总结刚才的对话，还是总结一个链接或视频？",
                    assistant_message={
                        "role": "assistant",
                        "content": "你是想让我总结刚才的对话，还是总结一个链接或视频？",
                    },
                )

        service, _ = build_default_service(AppConfig(data_root=tempfile.mkdtemp(), llm=LLMConfig(enabled=True)))
        service.generator._dify_client = _FakeToolClient()  # type: ignore[assignment]

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
        self.assertIn("总结刚才的对话", reply.text)
        self.assertIn("链接或视频", reply.text)

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
        visible_skill = self._manifest(
            skill_id="freshness-check",
            name="时效性核验",
            description="提醒时效性话题需要先核验",
            keywords=["今天"],
            priority=8,
            content="先提醒对方这类内容最好核验。",
        )
        hidden_tool_skill = self._manifest(
            skill_id="search-command",
            name="联网搜索",
            description="直接调用搜索工具",
            keywords=["搜一下"],
            priority=12,
            content="直接调用搜索工具。",
            handler_type="tool_id",
            handler_target="search",
            llm_tool=True,
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

    def test_generator_caps_prompt_visible_skill_block_by_config(self) -> None:
        generator = Generator(AppConfig(max_active_skills=1))
        session = SessionMemory(launcher_id="1", launcher_type="person")
        event = InboundEvent(
            launcher_id="1",
            launcher_type="person",
            sender_id="2",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="今天怎么样")],
        )
        high_priority_skill = self._manifest(
            skill_id="freshness-check",
            name="时效性核验",
            description="高优先级能力",
            priority=8,
            content="高优先级内容",
        )
        low_priority_skill = self._manifest(
            skill_id="image-handoff",
            name="生图交付语气",
            description="低优先级能力",
            priority=4,
            content="低优先级内容",
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
            active_skills=[low_priority_skill, high_priority_skill],
        )

        self.assertIn("时效性核验", prompt)
        self.assertIn("高优先级内容", prompt)
        self.assertNotIn("生图交付语气", prompt)
        self.assertNotIn("低优先级内容", prompt)

    def test_story_mode_prompt_pins_visible_reply_language(self) -> None:
        generator = Generator(AppConfig(story_mode=True, story_detail_level=2))
        session = SessionMemory(launcher_id="1", launcher_type="person")
        event = InboundEvent(
            launcher_id="1",
            launcher_type="person",
            sender_id="2",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="你在吗")],
        )
        card = generator._cards.load("person", session)

        prompt = generator._build_chat_query(
            event,
            session,
            EmotionState(),
            card=card,
            assistant_name=card.assistant_name,
            address="tester",
            search_hint="",
            search_context="",
            conversation_view="",
            memory_hints=[],
            speaker_notes=[],
            analysis_hint="",
            latest_message="你在吗",
            active_skills=[],
        )

        self.assertIn("[Story Mode]", prompt)
        self.assertIn("不要把外层叙事写成英文", prompt)
        self.assertIn("这轮可见回复必须使用", prompt)

    def test_explicit_named_summarize_skill_dispatches_real_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "skill.md").write_text(
                """---
name: summarize
description: Summarize URLs and videos.
---
Use summarize for URLs.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="summarize-command",
                arguments={"target": "https://www.youtube.com/watch?v=DmFdxaLgD70"},
                final_text="按 summarize 技能真的跑：这是视频的真实摘要。",
            )
            service.dispatcher._invoke_summarize_cli = lambda target, invocation: (  # type: ignore[method-assign]
                {
                    "summary": "这是视频的真实摘要。",
                    "extracted": {"title": "测试视频标题"},
                },
                "",
            )

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[
                        MessageSegment(
                            kind="text",
                            text="用summarize技能总结一下这个视频在讲什么：https://www.youtube.com/watch?v=DmFdxaLgD70",
                        )
                    ],
                )
            )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("按 summarize 技能真的跑", reply.text)
            self.assertIn("这是视频的真实摘要", reply.text)
            session = service.memory.load("783190298", "person")
            active_skills = session.metadata.get("active_skills", [])
            self.assertTrue(active_skills)
            self.assertTrue(any(item.get("id") == "summarize-command" for item in active_skills))

    def test_explicit_named_summarize_skill_refuses_when_transcript_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "skill.md").write_text(
                """---
name: summarize
description: Summarize URLs and videos.
---
Use summarize for URLs.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="summarize-command",
                arguments={"target": "https://www.youtube.com/watch?v=DmFdxaLgD70"},
                final_text="这次没有拿到这个视频的可用字幕或正文，所以不能假装总结。",
            )
            service.dispatcher._invoke_summarize_cli = lambda target, invocation: (  # type: ignore[method-assign]
                {
                    "summary": "YouTube 是一个全球知名的视频分享平台。",
                    "extracted": {
                        "title": "- YouTube",
                        "siteName": "youtube.com",
                        "transcriptSource": "unavailable",
                        "transcriptMetadata": {"reason": "no_transcript_available"},
                    },
                },
                "",
            )

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[
                        MessageSegment(
                            kind="text",
                            text="用summarize技能总结一下这个视频在讲什么：https://www.youtube.com/watch?v=DmFdxaLgD70",
                        )
                    ],
                )
            )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("没有拿到这个视频的可用字幕或正文", reply.text)
            self.assertIn("不能假装", reply.text)

    def test_service_exposes_registered_tools(self) -> None:
        service, _ = build_default_service()

        tools = service.list_tools()
        tool_ids = {item["id"] for item in tools["items"]}
        aliases_by_id = {item["id"]: set(item.get("aliases", [])) for item in tools["items"]}

        self.assertEqual(tools["count"], 17)
        self.assertEqual(tools["alias_count"], 66)
        self.assertEqual(tools["model_callable_count"], 16)
        self.assertSetEqual(
            tool_ids,
            {
                "image",
                "image-caption",
                "search",
                "search-links",
                "summary",
                "skill-list",
                "summarize",
                "weather",
                "web-fetch",
                "read-file",
                "list-files",
                "write-file",
                "exec-command",
                "set-preferred-name",
                "set-assistant-alias",
                "reject-third-party-naming",
                "clarify-naming-intent",
            },
        )
        self.assertSetEqual(
            aliases_by_id["search"],
            {"web-search", "internet-search", "search-web", "web-lookup", "lookup-web", "browser-search"},
        )
        self.assertSetEqual(
            aliases_by_id["image"],
            {"image-generate", "generate-image", "create-image", "draw-image", "text-to-image"},
        )
        self.assertSetEqual(
            aliases_by_id["image-caption"],
            {"image-handoff", "deliver-image", "image-delivery", "handoff-image"},
        )
        self.assertSetEqual(
            aliases_by_id["read-file"],
            {"read", "file-read", "open-file", "cat-file", "read-text-file"},
        )
        self.assertSetEqual(
            aliases_by_id["web-fetch"],
            {"fetch-url", "fetch", "open-url", "read-url", "fetch-page", "fetch-webpage", "http-get"},
        )
        self.assertSetEqual(
            aliases_by_id["search-links"],
            {"search-sources", "sources", "links", "source-links", "references", "citations", "related-links"},
        )
        self.assertSetEqual(
            aliases_by_id["weather"],
            {"forecast", "weather-lookup", "weather-check", "weather-forecast"},
        )

    def test_skills_panel_exposes_normalized_capabilities_and_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))

            panel = service.get_skills_panel()
            capability_ids = {item["id"] for item in panel["capabilities"]}

            self.assertIn("image", capability_ids)
            self.assertIn("search", capability_ids)
            self.assertIn("image-caption", capability_ids)
            self.assertIn("builtin", panel["skill_groups"])
            self.assertIn("restricted", panel["tool_groups"])
            self.assertIn("exec_allowlist", panel["safety"])
            self.assertIn("resolved_exec_allowed_roots", panel["safety"])
            self.assertGreaterEqual(len(panel["marketplace"]["sources"]), 3)

    def test_slash_skill_command_no_longer_bypasses_tool_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "skill.md").write_text(
                """---
name: summarize
description: Summarize URLs and videos.
---
Use summarize for URLs.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="summarize-command",
                arguments={"target": "https://example.com/post"},
                final_text="slash skill execution completed.",
            )
            service.dispatcher._invoke_summarize_cli = lambda target, invocation: (  # type: ignore[method-assign]
                {
                    "summary": "This came from slash skill execution.",
                    "extracted": {"title": "Slash Skill"},
                },
                "",
            )

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[
                        MessageSegment(
                            kind="text",
                            text="/skill summarize https://example.com/post",
                        )
                    ],
                )
            )

            self.assertIsNone(reply)

    def test_ignored_slash_summarize_does_not_use_legacy_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            document = Path(tmpdir) / "notes.txt"
            document.write_text(
                "OpenQQWaifu can now summarize local files without requiring the external summarize CLI.",
                encoding="utf-8",
            )
            (skills_dir / "skill.md").write_text(
                """---
name: summarize
description: Summarize URLs and files.
---
Use summarize for URLs and files.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="summarize-command",
                arguments={"target": str(document)},
                final_text="按 summarize 技能真的跑：without requiring the external summarize CLI.",
            )
            service.dispatcher._invoke_summarize_cli = lambda target, invocation: ({}, "当前运行时还没安装 summarize CLI。")  # type: ignore[method-assign]

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[
                        MessageSegment(
                            kind="text",
                            text=f"/skill summarize {document}",
                        )
                    ],
                )
            )

            self.assertIsNone(reply)

    def test_explicit_weather_skill_dispatches_real_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "SKILL.md").write_text(
                """---
name: weather
description: Get weather for any location.
---
Use wttr.in to answer weather questions.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="weather-command",
                arguments={"location": "Shanghai"},
                final_text="Shanghai现在 18°C，多云。明天，晴，11 到 22°C。",
            )

            raw = """
{
  "current_condition": [
    {
      "temp_C": "18",
      "FeelsLikeC": "19",
      "humidity": "61",
      "windspeedKmph": "8",
      "weatherDesc": [{"value": "多云"}]
    }
  ],
  "nearest_area": [
    {
      "areaName": [{"value": "Shanghai"}]
    }
  ],
  "weather": [
    {
      "date": "2026-04-21",
      "maxtempC": "20",
      "mintempC": "12",
      "hourly": [
        {
          "chanceofrain": "10",
          "weatherDesc": [{"value": "多云"}]
        }
      ]
    },
    {
      "date": "2026-04-22",
      "maxtempC": "22",
      "mintempC": "11",
      "hourly": [
        {
          "chanceofrain": "0",
          "weatherDesc": [{"value": "晴"}]
        }
      ]
    }
  ]
}
""".strip()

            with patch(
                "waifu_standalone.skill_dispatcher.AsyncHttpTransport.request",
                return_value=HttpResponse(
                    status_code=200,
                    text=raw,
                    content=raw.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
            ):
                reply = service.handle_event(
                    InboundEvent(
                        launcher_id="783190298",
                        launcher_type="person",
                        sender_id="783190298",
                        sender_name="tester",
                        segments=[MessageSegment(kind="text", text="weather Shanghai")],
                    )
                )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("Shanghai现在 18°C", reply.text)
            self.assertIn("明天，晴，11 到 22°C。", reply.text)

            session = service.memory.load("783190298", "person")
            active_skills = session.metadata.get("active_skills", [])
            self.assertTrue(active_skills)
            self.assertTrue(any(item.get("id") == "weather-command" for item in active_skills))

    def test_weather_skill_auto_binds_chinese_trigger_and_extracts_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="weather-command",
                arguments={"location": "上海"},
                final_text="Shanghai现在 23°C，晴。",
            )
            weather = service.get_skill_detail("weather-command")
            assert weather is not None
            self.assertIn("天气", weather["manifest"]["trigger"]["keywords"])

            raw = """
{
  "current_condition": [
    {
      "temp_C": "23",
      "FeelsLikeC": "24",
      "humidity": "58",
      "windspeedKmph": "9",
      "weatherDesc": [{"value": "晴"}]
    }
  ],
  "nearest_area": [
    {
      "areaName": [{"value": "Shanghai"}]
    }
  ],
  "weather": [
    {
      "date": "2026-04-21",
      "maxtempC": "25",
      "mintempC": "18",
      "hourly": [
        {
          "chanceofrain": "0",
          "weatherDesc": [{"value": "晴"}]
        }
      ]
    },
    {
      "date": "2026-04-22",
      "maxtempC": "27",
      "mintempC": "19",
      "hourly": [
        {
          "chanceofrain": "5",
          "weatherDesc": [{"value": "多云"}]
        }
      ]
    }
  ]
}
""".strip()

            with patch(
                "waifu_standalone.skill_dispatcher.AsyncHttpTransport.request",
                return_value=HttpResponse(
                    status_code=200,
                    text=raw,
                    content=raw.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                ),
            ):
                reply = service.handle_event(
                    InboundEvent(
                        launcher_id="weather-zh",
                        launcher_type="person",
                        sender_id="weather-zh",
                        sender_name="tester",
                        segments=[MessageSegment(kind="text", text="上海天气怎么样")],
                    )
                )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("Shanghai现在 23°C", reply.text)

            session = service.memory.load("weather-zh", "person")
            active_skills = session.metadata.get("active_skills", [])
            self.assertTrue(active_skills)
            self.assertTrue(any(item.get("id") == "weather-command" for item in active_skills))

    def test_openclaw_tool_alias_dispatches_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "lookup.md").write_text(
                """---
id: lookup-command
name: lookup
description: OpenClaw-style search tool alias.
input_schema: {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"lookup-command","llm_tool":true,"keywords":["lookup"]}
handler: {"type":"tool_id","target":"web_search","arg_mode":"structured"}
policy: {"priority":9,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Use web_search when the user asks to look something up.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="lookup-command",
                arguments={"query": "Beijing weather"},
                final_text="Beijing Weather: sunny",
            )
            service.search._fetcher = lambda query: [
                SearchResult(
                    title="Beijing Weather",
                    snippet=f"{query}: sunny",
                    url="https://example.com/weather",
                )
            ]

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text="lookup Beijing weather")],
                )
            )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("Beijing Weather", reply.text)

    def test_openclaw_read_alias_dispatches_file_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "notes.txt"
            target.write_text("compatibility layer works", encoding="utf-8")
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "read.md").write_text(
                """---
id: read-command
name: readnote
description: Read a local note.
input_schema: {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"read-command","llm_tool":true,"keywords":["readnote"]}
handler: {"type":"tool_id","target":"read","arg_mode":"structured"}
policy: {"priority":9,"user_invocable":true,"risk_level":"filesystem","timeout_seconds":30,"max_output_chars":6000,"requires_authorization":true}
default_args: {}
---
Read files with the read alias.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="read-command",
                arguments={"path": str(target)},
                final_text="我读到了文件。",
            )

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[MessageSegment(kind="text", text=f"readnote {target}")],
                )
            )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("compatibility layer works", reply.text)

    def test_generator_tool_loop_executes_registered_search_tool(self) -> None:
        class _FakeToolClient:
            enabled = True
            supports_tool_calling = True
            base_url = "https://example.com/v1"
            api_key = "secret"
            model = "tool-model"
            backend = "openai"
            timeout_seconds = 45.0
            app_type = "chat"

            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return "先查天气再回答"

            async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return "先查天气再回答"

            def invoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                raise AssertionError("sync tool loop should not run in async event path")

            async def ainvoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                self.calls += 1
                if self.calls == 1:
                    self.seen_tools = {item["name"] for item in tools}
                    return LLMToolResponse(
                        tool_calls=[LLMToolCall(call_id="call_1", tool_name="search-command", arguments={"query": "北京天气"})],
                        assistant_message={
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search-command",
                                        "arguments": '{"query":"北京天气"}',
                                    },
                                }
                            ],
                        },
                    )
                self.last_tool_message = messages[-1]
                return LLMToolResponse(
                    text="北京今天晴，最高气温 26 度。",
                    assistant_message={"role": "assistant", "content": "北京今天晴，最高气温 26 度。"},
                )

        service, _ = build_default_service()
        service.config.llm.enabled = True
        service.generator._dify_client = _FakeToolClient()  # type: ignore[assignment]
        service.search._fetcher = lambda query: [
            SearchResult(title="北京天气", snippet="北京今天晴，最高气温 26 度", url="https://example.com/weather")
        ]

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="北京天气怎么样")],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("北京今天晴", reply.text)
        self.assertIn("search-command", service.generator._dify_client.seen_tools)  # type: ignore[attr-defined]
        self.assertEqual(service.generator._dify_client.last_tool_message["role"], "tool")  # type: ignore[attr-defined]

    def test_generator_tool_loop_executes_registered_search_tool(self) -> None:
        class _FakeToolClient:
            enabled = True
            supports_tool_calling = True
            base_url = "https://example.com/v1"
            api_key = "secret"
            model = "tool-model"
            backend = "openai"
            timeout_seconds = 45.0
            app_type = "chat"

            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return "先查天气再回答"

            async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return "先查天气再回答"

            def invoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                raise AssertionError("sync tool loop should not run in async event path")

            async def ainvoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                self.calls += 1
                if self.calls == 1:
                    self.seen_tools = {item["name"] for item in tools}
                    return LLMToolResponse(
                        tool_calls=[LLMToolCall(call_id="call_1", tool_name="search-command", arguments={"query": "北京天气"})],
                        assistant_message={
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search-command",
                                        "arguments": '{"query":"北京天气"}',
                                    },
                                }
                            ],
                        },
                    )
                self.last_tool_message = messages[-1]
                return LLMToolResponse(
                    text="北京今天晴，最高气温 26 度。",
                    assistant_message={"role": "assistant", "content": "北京今天晴，最高气温 26 度。"},
                )

        service, _ = build_default_service(AppConfig(data_root=tempfile.mkdtemp()))
        service.config.llm.enabled = True
        service.generator._dify_client = _FakeToolClient()  # type: ignore[assignment]
        service.search._fetcher = lambda query: [
            SearchResult(title="北京天气", snippet="北京今天晴，最高气温 26 度", url="https://example.com/weather")
        ]

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="北京天气怎么样")],
            )
        )

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("北京今天晴", reply.text)
        self.assertIn("search-command", service.generator._dify_client.seen_tools)  # type: ignore[attr-defined]
        self.assertEqual(service.generator._dify_client.last_tool_message["role"], "tool")  # type: ignore[attr-defined]

    def test_skill_dispatch_supports_json_command_arg_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            target = Path(tmpdir) / "payload.txt"
            (skills_dir / "json-write.md").write_text(
                """---
id: json-write
name: json_write
description: write file through json args
input_schema: {"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"json-write","llm_tool":true,"keywords":["jsonwrite"]}
handler: {"type":"tool_id","target":"write-file","arg_mode":"json"}
policy: {"priority":9,"user_invocable":true,"risk_level":"filesystem","timeout_seconds":30,"max_output_chars":6000,"requires_authorization":true}
default_args: {}
---
Write a file using structured arguments.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            self._install_tool_client(
                service,
                tool_name="json-write",
                arguments={"path": target.as_posix(), "content": "json mode works"},
                final_text="已写入文件。",
            )

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[
                        MessageSegment(
                            kind="text",
                            text=f'jsonwrite {{"path":"{target.as_posix()}","content":"json mode works"}}',
                        )
                    ],
                )
            )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("已写入文件", reply.text)
            self.assertEqual(target.read_text(encoding="utf-8"), "json mode works")

    def test_write_file_tool_respects_policy_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "outside.txt"
            data_root = Path(tmpdir) / "runtime-data"
            skills_dir = data_root / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "json-write.md").write_text(
                """---
id: json-write
name: json_write
description: write file through json args
input_schema: {"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"json-write","llm_tool":true,"keywords":["jsonwrite"]}
handler: {"type":"tool_id","target":"write-file","arg_mode":"json"}
policy: {"priority":9,"user_invocable":true,"risk_level":"filesystem","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Write a file using structured arguments.
""",
                encoding="utf-8",
            )
            service, _ = build_default_service(
                AppConfig(
                    data_root=str(data_root),
                    tool_policy=ToolPolicyConfig(
                        allowed_roots=["data"],
                        write_allowed_roots=["data"],
                    ),
                )
            )
            self._install_tool_client(
                service,
                tool_name="json-write",
                arguments={"path": target.as_posix(), "content": "blocked"},
                final_text="路径超出允许范围。",
            )

            reply = service.handle_event(
                InboundEvent(
                    launcher_id="783190298",
                    launcher_type="person",
                    sender_id="783190298",
                    sender_name="tester",
                    segments=[
                        MessageSegment(
                            kind="text",
                            text=f'jsonwrite {{"path":"{target.as_posix()}","content":"blocked"}}',
                        )
                    ],
                )
            )

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("路径超出允许范围", reply.text)
            self.assertFalse(target.exists())

    def test_exec_command_tool_rejects_non_allowlisted_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    tool_policy=ToolPolicyConfig(exec_allowlist=["python"]),
                )
            )

            result = service.dispatcher.use_exec_command_tool(
                service.dispatcher._build_tool_invocation(
                    InboundEvent(
                        launcher_id="783190298",
                        launcher_type="person",
                        sender_id="783190298",
                        sender_name="tester",
                        segments=[MessageSegment(kind="text", text="run blockedcmd")],
                    ),
                    SessionMemory(launcher_id="783190298", launcher_type="person", history=[]),
                    skill=service.skills.get_skill("search-command"),
                    raw_args="blockedcmd --help",
                    address="tester",
                    assistant_name="Assistant",
                    active_skills=[],
                    tool_id="exec-command",
                )
            )

            self.assertIn("allowlisted", result.error)
            self.assertIn("命令未在白名单内", result.text)

    def test_service_can_import_openclaw_compatible_bundle_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_root = Path(tmpdir) / "bundle"
            (bundle_root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
            (bundle_root / ".codex-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
            (bundle_root / "skills" / "weather").mkdir(parents=True, exist_ok=True)
            (bundle_root / "commands").mkdir(parents=True, exist_ok=True)
            (bundle_root / "skills" / "weather" / "SKILL.md").write_text(
                """---
id: weather_bundle
name: weather_bundle
description: Bundle weather skill.
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"weather_bundle","llm_tool":false,"keywords":["weather bundle"]}
handler: {"type":"prompt_template","target":"weather_bundle"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Use tools to answer weather prompts.
""",
                encoding="utf-8",
            )
            (bundle_root / "commands" / "summary.md").write_text(
                """---
id: summary_bundle
name: summary_bundle
description: Bundle summary skill.
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"summary_bundle","llm_tool":false,"keywords":[]}
handler: {"type":"prompt_template","target":"summary_bundle"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Summarize content carefully.
""",
                encoding="utf-8",
            )

            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            result = service.import_skill_bundle(bundle_root)

            self.assertEqual(result["bundle_type"], "codex")
            self.assertEqual(result["imported_count"], 2)
            self.assertIsNotNone(service.get_skill_detail("weather_bundle"))
            self.assertIsNotNone(service.get_skill_detail("summary_bundle"))

    def test_bundle_import_preserves_supporting_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_root = Path(tmpdir) / "codex-agent"
            (bundle_root / "knowledge").mkdir(parents=True, exist_ok=True)
            (bundle_root / "hooks").mkdir(parents=True, exist_ok=True)
            (bundle_root / "SKILL.md").write_text(
                """---
name: codex-agent
description: Preserve bundle resources.
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"codex-agent","llm_tool":false,"keywords":[]}
handler: {"type":"prompt_template","target":"codex-agent"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
See knowledge/features.md and hooks/start_codex.sh.
""",
                encoding="utf-8",
            )
            (bundle_root / "knowledge" / "features.md").write_text("# features\n", encoding="utf-8")
            (bundle_root / "hooks" / "start_codex.sh").write_text("#!/bin/sh\necho start\n", encoding="utf-8")

            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            result = service.import_skill_bundle(bundle_root)

            self.assertEqual(result["imported_count"], 1)
            preserved_files = list((Path(tmpdir) / "skills").rglob("features.md"))
            self.assertTrue(preserved_files)
            self.assertTrue(any(path.parent.name == "knowledge" for path in preserved_files))
            self.assertTrue(any(path.name == "start_codex.sh" for path in (Path(tmpdir) / "skills").rglob("start_codex.sh")))

    def test_bundle_import_marks_known_skill_ready_after_runtime_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_root = Path(tmpdir) / "summarize"
            bundle_root.mkdir(parents=True, exist_ok=True)
            (bundle_root / "SKILL.md").write_text(
                """---
id: summarize
name: summarize
description: Summarize URLs and local files.
metadata: {"nanobot": {"requires": {"bins": ["summarize"]}}}
input_schema: {"type":"object","properties":{"target":{"type":"string"}},"required":["target"]}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"summarize","llm_tool":true,"keywords":["summarize"]}
handler: {"type":"tool_id","target":"summarize","arg_mode":"structured"}
policy: {"priority":10,"user_invocable":true,"risk_level":"safe","timeout_seconds":90,"max_output_chars":10000}
default_args: {}
---
Use summarize for URLs.
""",
                encoding="utf-8",
            )

            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            result = service.import_skill_bundle(bundle_root)
            detail = service.get_skill_detail("summarize")

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["ready_count"], 1)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail["status"], "ready")
            self.assertEqual(detail["manifest"]["handler"]["target"], "summarize")


if __name__ == "__main__":
    unittest.main()
