from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.skill_registry import SkillRegistry
from waifu_standalone.cells.tool_registry import ToolInvocation, ToolRegistry
from waifu_standalone.config import AppConfig
from waifu_standalone.models import InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from waifu_standalone.observability import MetricsRegistry
from waifu_standalone.plugin_api import PluginContext, load_tool_plugins


class _FakeEntryPoint:
    def __init__(self, name: str, register) -> None:  # type: ignore[no-untyped-def]
        self.name = name
        self._register = register

    def load(self):  # type: ignore[no-untyped-def]
        return self._register


def _build_context() -> PluginContext:
    app_config = AppConfig()
    return PluginContext(
        app_config=app_config,
        tool_registry=ToolRegistry(),
        skill_registry=SkillRegistry(app_config),
        logger=logging.getLogger("waifu.plugins"),
        metrics=MetricsRegistry(service_name="test-waifu"),
    )


def _build_invocation() -> ToolInvocation:
    return ToolInvocation(
        tool_id="demo-tool",
        raw_args="hello",
        event=InboundEvent(
            launcher_id="group-1",
            launcher_type="group",
            sender_id="user-1",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="hello")],
        ),
        session=SessionMemory(launcher_id="group-1", launcher_type="group", history=[]),
    )


class PluginApiTests(unittest.TestCase):
    def test_loader_calls_register_with_context(self) -> None:
        ctx = _build_context()
        seen: list[PluginContext] = []

        def register(plugin_ctx: PluginContext) -> None:
            seen.append(plugin_ctx)

        loaded = load_tool_plugins(ctx, entry_points=[_FakeEntryPoint("demo", register)])

        self.assertEqual(loaded, ["demo"])
        self.assertEqual(seen, [ctx])

    def test_failing_plugin_does_not_abort_others(self) -> None:
        ctx = _build_context()
        seen: list[str] = []

        def broken(plugin_ctx: PluginContext) -> None:
            seen.append("broken")
            raise RuntimeError("boom")

        def healthy(plugin_ctx: PluginContext) -> None:
            seen.append("healthy")

        with self.assertLogs("waifu_standalone.plugin_api", level="ERROR") as captured:
            loaded = load_tool_plugins(
                ctx,
                entry_points=[
                    _FakeEntryPoint("broken", broken),
                    _FakeEntryPoint("healthy", healthy),
                ],
            )

        self.assertEqual(loaded, ["healthy"])
        self.assertEqual(seen, ["broken", "healthy"])
        self.assertIn("plugin broken failed to load", captured.output[0])

    def test_disabled_name_is_skipped(self) -> None:
        ctx = _build_context()
        seen: list[str] = []

        def noisy(plugin_ctx: PluginContext) -> None:
            seen.append("noisy")

        def quiet(plugin_ctx: PluginContext) -> None:
            seen.append("quiet")

        loaded = load_tool_plugins(
            ctx,
            disabled={"noisy"},
            entry_points=[
                _FakeEntryPoint("noisy", noisy),
                _FakeEntryPoint("quiet", quiet),
            ],
        )

        self.assertEqual(loaded, ["quiet"])
        self.assertEqual(seen, ["quiet"])

    def test_plugin_can_register_tool_through_registry(self) -> None:
        ctx = _build_context()

        def handler(invocation: ToolInvocation) -> OutboundMessage | None:
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=f"plugin:{invocation.raw_args}",
            )

        def register(plugin_ctx: PluginContext) -> None:
            plugin_ctx.tool_registry.register(
                "plugin-tool",
                name="Plugin Tool",
                description="registered from plugin",
                handler=handler,
            )

        loaded = load_tool_plugins(ctx, entry_points=[_FakeEntryPoint("plugin-tool", register)])
        result = ctx.tool_registry.execute("plugin-tool", _build_invocation())

        self.assertEqual(loaded, ["plugin-tool"])
        self.assertTrue(ctx.tool_registry.has("plugin-tool"))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "plugin:hello")

    def test_plugin_can_register_runtime_skill_through_registry(self) -> None:
        ctx = _build_context()

        def register(plugin_ctx: PluginContext) -> None:
            plugin_ctx.skill_registry.register_runtime_skill(
                """---
id: plugin-skill
name: plugin_skill
aliases: ["plugin-skill-alias"]
description: plugin registered skill
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{}}
trigger: {"command":"plugin-skill","llm_tool":false,"keywords":["plugin skill"]}
handler: {"type":"prompt_template","target":"plugin-skill"}
policy: {"priority":0,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
---
Use plugin tools carefully.
""",
                source_name="plugin://demo/plugin-skill",
            )

        loaded = load_tool_plugins(ctx, entry_points=[_FakeEntryPoint("plugin-skill", register)])
        by_alias = ctx.skill_registry.find_by_name_or_id("plugin-skill-alias")

        self.assertEqual(loaded, ["plugin-skill"])
        self.assertIsNotNone(by_alias)
        assert by_alias is not None
        self.assertEqual(by_alias.skill_id, "plugin-skill")
        self.assertEqual(by_alias.source_kind, "plugin")

    def test_plugin_can_register_skill_tool_in_one_step(self) -> None:
        ctx = _build_context()

        def handler(invocation: ToolInvocation) -> OutboundMessage | None:
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=f"weather:{invocation.raw_args}",
            )

        registration = ctx.register_skill_tool(
            "weather-tool",
            name="Weather Tool",
            description="fetches weather",
            handler=handler,
            triggers=["天气"],
            aliases=["weather_lookup"],
            priority=8,
            body="Call the weather tool directly.",
        )

        result = ctx.tool_registry.execute("weather-tool", _build_invocation())
        skill = ctx.skill_registry.get_skill("weather-tool")
        by_alias = ctx.skill_registry.find_by_name_or_id("weather_lookup")

        self.assertEqual(registration.tool.tool_id, "weather-tool")
        self.assertEqual(registration.skill.skill_id, "weather-tool")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "weather:hello")
        self.assertIsNotNone(skill)
        assert skill is not None
        self.assertTrue(skill.dispatches_tool)
        self.assertEqual(skill.command_tool, "weather-tool")
        self.assertEqual(skill.triggers, ["天气"])
        self.assertIsNotNone(by_alias)


if __name__ == "__main__":
    unittest.main()
