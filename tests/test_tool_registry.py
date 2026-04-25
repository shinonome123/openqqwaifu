from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.skill_registry import SkillHandlerSpec, SkillManifest, SkillPolicySpec, SkillTriggerSpec
from waifu_standalone.cells.tool_registry import ToolExecutionResult, ToolInvocation, ToolRegistry
from waifu_standalone.models import InboundEvent, MessageSegment, OutboundMessage, SessionMemory


def _build_invocation() -> ToolInvocation:
    event = InboundEvent(
        launcher_id="group-1",
        launcher_type="group",
        sender_id="user-1",
        sender_name="tester",
        segments=[MessageSegment(kind="text", text="hello")],
    )
    session = SessionMemory(
        launcher_id="group-1",
        launcher_type="group",
        history=[],
    )
    skill = SkillManifest(
        skill_id="demo",
        name="Demo",
        description="",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        trigger=SkillTriggerSpec(command="demo", llm_tool=True, keywords=["demo"]),
        handler=SkillHandlerSpec(type="tool_id", target="demo-tool"),
        policy=SkillPolicySpec(priority=0),
        default_args={},
        metadata={},
        content="",
        source="test",
    )
    return ToolInvocation(
        tool_id="demo-tool",
        raw_args="hello",
        event=event,
        session=session,
        skill=skill,
        address="tester",
        assistant_name="waifu",
        active_skills=[skill],
    )


class ToolRegistryTests(unittest.TestCase):
    def test_duplicate_registration_warns_and_keeps_first_tool(self) -> None:
        registry = ToolRegistry()

        def first_handler(invocation: ToolInvocation) -> OutboundMessage | None:
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text="first",
            )

        def second_handler(invocation: ToolInvocation) -> OutboundMessage | None:
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text="second",
            )

        registry.register(
            "demo-tool",
            name="Demo",
            description="",
            handler=first_handler,
        )
        with self.assertLogs("waifu_standalone.cells.tool_registry", level="WARNING") as captured:
            registry.register(
                "demo-tool",
                name="Demo Again",
                description="duplicate",
                handler=second_handler,
            )

        result = registry.execute("demo-tool", _build_invocation())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "first")
        self.assertIn("already registered", captured.output[0])

    def test_aexecute_prefers_async_handler(self) -> None:
        registry = ToolRegistry()
        calls: list[str] = []

        def sync_handler(invocation: ToolInvocation) -> OutboundMessage | None:
            calls.append("sync")
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text="sync",
            )

        async def async_handler(invocation: ToolInvocation) -> OutboundMessage | None:
            calls.append("async")
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text="async",
            )

        registry.register(
            "demo-tool",
            name="Demo",
            description="",
            handler=sync_handler,
            async_handler=async_handler,
        )

        result = asyncio.run(registry.aexecute("demo-tool", _build_invocation()))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "async")
        self.assertEqual(calls, ["async"])

    def test_aexecute_falls_back_to_sync_handler(self) -> None:
        registry = ToolRegistry()
        calls: list[str] = []

        def sync_handler(invocation: ToolInvocation) -> OutboundMessage | None:
            calls.append("sync")
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text=invocation.raw_args,
            )

        registry.register(
            "demo-tool",
            name="Demo",
            description="",
            handler=sync_handler,
        )

        result = asyncio.run(registry.aexecute("demo-tool", _build_invocation()))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "hello")
        self.assertEqual(calls, ["sync"])

    def test_model_schemas_only_expose_model_callable_tools(self) -> None:
        registry = ToolRegistry()

        def handler(invocation: ToolInvocation) -> OutboundMessage | None:
            return OutboundMessage(
                launcher_id=invocation.event.launcher_id,
                launcher_type=invocation.event.launcher_type,
                text="ok",
            )

        registry.register(
            "demo-tool",
            name="Demo",
            description="callable from model",
            handler=handler,
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            model_handler=lambda invocation: ToolExecutionResult(text="demo"),
        )
        registry.register(
            "write-tool",
            name="Write",
            description="direct only",
            handler=handler,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
            model_handler=lambda invocation: ToolExecutionResult(text="write"),
            model_callable=False,
        )

        schemas = registry.model_schemas()
        described = registry.describe()
        items_by_id = {item["id"]: item for item in described["items"]}

        self.assertEqual([item["name"] for item in schemas], ["demo-tool"])
        self.assertEqual(described["model_callable_count"], 1)
        self.assertTrue(items_by_id["demo-tool"]["model_callable"])
        self.assertFalse(items_by_id["write-tool"]["model_callable"])


if __name__ == "__main__":
    unittest.main()
