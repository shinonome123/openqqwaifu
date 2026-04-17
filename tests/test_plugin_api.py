from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

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
    return PluginContext(
        app_config=AppConfig(),
        tool_registry=ToolRegistry(),
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


if __name__ == "__main__":
    unittest.main()
