from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.skill_registry import SkillSpec
from waifu_standalone.cells.tool_registry import ToolInvocation, ToolRegistry
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
    skill = SkillSpec(
        skill_id="demo",
        name="Demo",
        description="",
        triggers=["demo"],
        mode="prefix",
        priority=0,
        content="",
        source="test",
        command_tool="demo-tool",
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


if __name__ == "__main__":
    unittest.main()
