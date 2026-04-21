from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, ClawRuntimeConfig
from waifu_standalone.models import InboundEvent, MessageSegment


class ClawRuntimeTests(unittest.TestCase):
    @staticmethod
    def _write_mcp_echo_server(path: Path) -> None:
        path.write_text(
            r"""
const fs = require("node:fs");

function send(payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  process.stdout.write(`Content-Length: ${body.length}\r\n\r\n`);
  process.stdout.write(body);
}

let buffer = Buffer.alloc(0);
process.stdin.on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  while (true) {
    const headerEnd = buffer.indexOf("\r\n\r\n");
    if (headerEnd < 0) {
      return;
    }
    const header = buffer.slice(0, headerEnd).toString("utf8");
    const match = /content-length:\s*(\d+)/i.exec(header);
    if (!match) {
      throw new Error("missing content length");
    }
    const length = Number.parseInt(match[1], 10);
    const bodyStart = headerEnd + 4;
    if (buffer.length < bodyStart + length) {
      return;
    }
    const message = JSON.parse(buffer.slice(bodyStart, bodyStart + length).toString("utf8"));
    buffer = buffer.slice(bodyStart + length);
    if (message.method === "initialize") {
      send({
        jsonrpc: "2.0",
        id: message.id,
        result: { capabilities: { tools: {} }, serverInfo: { name: "echo", version: "1.0.0" } },
      });
      continue;
    }
    if (message.method === "tools/list") {
      send({
        jsonrpc: "2.0",
        id: message.id,
        result: {
          tools: [
            {
              name: "echo_text",
              description: "Echo input text.",
              inputSchema: {
                type: "object",
                properties: { input: { type: "string" } },
              },
            },
          ],
        },
      });
      continue;
    }
    if (message.method === "tools/call") {
      const args = message.params?.arguments || {};
      const input = String(args.input || args.raw_args || "");
      send({
        jsonrpc: "2.0",
        id: message.id,
        result: {
          content: [{ type: "text", text: `echo:${input}` }],
          structuredContent: { echoed: input },
        },
      });
      continue;
    }
    if (message.id !== undefined) {
      send({ jsonrpc: "2.0", id: message.id, result: {} });
    }
  }
});
""".strip(),
            encoding="utf-8",
        )

    @staticmethod
    def _write_acp_echo_harness(path: Path) -> None:
        path.write_text(
            r"""
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  const text = String(chunk || "").trim();
  if (!text) {
    return;
  }
  process.stdout.write(`acp:${text}\n`);
});
process.stdin.resume();
""".strip(),
            encoding="utf-8",
        )

    def test_managed_runtime_installs_and_inspects_bundle_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_root = Path(tmpdir) / "weather-bundle"
            (bundle_root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
            (bundle_root / "skills" / "weather").mkdir(parents=True, exist_ok=True)
            (bundle_root / "commands").mkdir(parents=True, exist_ok=True)
            (bundle_root / "hooks" / "announce").mkdir(parents=True, exist_ok=True)
            (bundle_root / "agents").mkdir(parents=True, exist_ok=True)
            (bundle_root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"name": "Weather Bundle"}),
                encoding="utf-8",
            )
            (bundle_root / "skills" / "weather" / "SKILL.md").write_text(
                """---
id: weather_bundle
name: weather_bundle
description: Weather bundle skill
triggers: ["weather bundle"]
mode: prefix
---
Use tools carefully.
""",
                encoding="utf-8",
            )
            (bundle_root / "commands" / "summary.md").write_text(
                """---
id: summary_bundle
name: summary_bundle
description: Claude command bundle
---
Summarize carefully.
""",
                encoding="utf-8",
            )
            (bundle_root / "hooks" / "announce" / "HOOK.md").write_text("# hook\n", encoding="utf-8")
            (bundle_root / "hooks" / "announce" / "handler.js").write_text(
                "process.stdin.resume(); process.stdin.on('end', () => process.stdout.write('ok'));",
                encoding="utf-8",
            )
            (bundle_root / ".mcp.json").write_text(
                json.dumps({"servers": {"weather-server": {"command": "python", "args": ["-m", "weather"]}}}),
                encoding="utf-8",
            )
            (bundle_root / "settings.json").write_text(
                json.dumps({"shell": {"command": "/bin/bash", "args": ["-lc"]}}),
                encoding="utf-8",
            )
            (bundle_root / ".lsp.json").write_text(
                json.dumps({"python": {"command": "pylsp"}}),
                encoding="utf-8",
            )
            (bundle_root / "agents" / "writer.md").write_text("# agent\n", encoding="utf-8")

            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    claw_runtime=ClawRuntimeConfig(enabled=True, mode="managed", routing_mode="shadow"),
                )
            )
            try:
                result = service.import_skill_bundle(bundle_root)
                self.assertEqual(result["claw_runtime"]["status"], "ok")

                plugin = service.inspect_claw_plugin("weather-bundle")
                self.assertIsNotNone(plugin)
                assert plugin is not None
                self.assertEqual(plugin["format"], "bundle")
                self.assertEqual(plugin["bundle_type"], "codex")
                self.assertEqual(plugin["owner_routing"], "python")
                statuses = {item["kind"]: item["status"] for item in plugin["capabilities"]}
                self.assertEqual(statuses["skills"], "wired")
                self.assertEqual(statuses["claude_commands"], "wired")
                self.assertEqual(statuses["hook_pack"], "wired")
                self.assertEqual(statuses["mcp"], "detect_only")
                self.assertEqual(statuses["claude_settings"], "wired")
                self.assertEqual(statuses["lsp"], "wired")
                self.assertEqual(statuses["claude_agents"], "detect_only")
            finally:
                service.close()

    def test_runtime_lists_and_invokes_mcp_tools_when_bridge_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            node_path = shutil.which("node") or "node"
            bundle_root = Path(tmpdir) / "echo-bundle"
            (bundle_root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
            (bundle_root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"name": "Echo Bundle"}),
                encoding="utf-8",
            )
            (bundle_root / "SKILL.md").write_text(
                """---
name: echo-bundle
description: Minimal skill root for MCP bundle installation.
---
Expose MCP tools through the ClawRuntime bridge.
""",
                encoding="utf-8",
            )
            server_script = Path(tmpdir) / "mcp_echo.js"
            self._write_mcp_echo_server(server_script)
            (bundle_root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "servers": {
                            "echo server": {
                                "command": node_path,
                                "args": [str(server_script)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    claw_runtime=ClawRuntimeConfig(
                        enabled=True,
                        mode="managed",
                        routing_mode="hybrid",
                        plugin_tools_mcp_bridge=True,
                    ),
                )
            )
            try:
                result = service.import_skill_bundle(bundle_root)
                self.assertEqual(result["claw_runtime"]["status"], "ok")

                plugin = service.inspect_claw_plugin("echo-bundle")
                self.assertIsNotNone(plugin)
                assert plugin is not None
                statuses = {item["kind"]: item["status"] for item in plugin["capabilities"]}
                self.assertEqual(statuses["mcp"], "wired")

                tools = service.claw_runtime.list_tools()["items"]
                tool_ids = {item["id"] for item in tools}
                self.assertIn("echo-server__echo_text", tool_ids)

                invocation = service.claw_runtime.invoke_tool(
                    "echo-server__echo_text",
                    {"input": "hello"},
                )
                self.assertEqual(invocation["status"], "ok")
                self.assertIn("echo:hello", invocation["text"])
            finally:
                service.close()

    def test_hybrid_runtime_dispatch_executes_runtime_tool_when_local_tool_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            node_path = shutil.which("node") or "node"
            bundle_root = Path(tmpdir) / "runtime-echo"
            (bundle_root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
            (bundle_root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"name": "Runtime Echo"}),
                encoding="utf-8",
            )
            (bundle_root / "skills" / "echo").mkdir(parents=True, exist_ok=True)
            (bundle_root / "skills" / "echo" / "SKILL.md").write_text(
                """---
id: runtime-echo
name: runtime-echo
description: Dispatch through the ClawRuntime MCP bridge.
triggers: ["runtimeecho"]
mode: prefix
disable-model-invocation: true
command-dispatch: tool
command-tool: echo-server__echo_text
---
Use the runtime echo tool.
""",
                encoding="utf-8",
            )
            server_script = Path(tmpdir) / "mcp_echo.js"
            self._write_mcp_echo_server(server_script)
            (bundle_root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "servers": {
                            "echo server": {
                                "command": node_path,
                                "args": [str(server_script)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    claw_runtime=ClawRuntimeConfig(
                        enabled=True,
                        mode="managed",
                        routing_mode="hybrid",
                        plugin_tools_mcp_bridge=True,
                    ),
                )
            )
            try:
                result = service.import_skill_bundle(bundle_root)
                self.assertEqual(result["claw_runtime"]["status"], "ok")

                reply = service.handle_event(
                    InboundEvent(
                        launcher_id="runtime-echo",
                        launcher_type="person",
                        sender_id="runtime-echo",
                        sender_name="tester",
                        segments=[MessageSegment(kind="text", text="runtimeecho hello runtime")],
                    )
                )

                self.assertIsNotNone(reply)
                assert reply is not None
                self.assertIn("echo:hello runtime", reply.text)
            finally:
                service.close()

    def test_native_manifest_takes_precedence_over_bundle_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_root = Path(tmpdir) / "native-plugin"
            bundle_root.mkdir(parents=True, exist_ok=True)
            (bundle_root / "openclaw.plugin.json").write_text(
                json.dumps({"id": "native-plugin", "name": "Native Plugin"}),
                encoding="utf-8",
            )
            (bundle_root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
            (bundle_root / ".codex-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
            (bundle_root / "SKILL.md").write_text(
                """---
name: native-plugin
description: native manifest should win
---
Use native plugin runtime.
""",
                encoding="utf-8",
            )

            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    claw_runtime=ClawRuntimeConfig(enabled=True, mode="managed", routing_mode="shadow"),
                )
            )
            try:
                service.import_skill_bundle(bundle_root)
                plugin = service.inspect_claw_plugin("native-plugin")
                self.assertIsNotNone(plugin)
                assert plugin is not None
                self.assertEqual(plugin["format"], "native")
                self.assertEqual(plugin["bundle_type"], "none")
            finally:
                service.close()

    def test_runtime_check_and_acp_session_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            node_path = shutil.which("node") or "node"
            harness_script = Path(tmpdir) / "acp_echo.js"
            self._write_acp_echo_harness(harness_script)
            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    claw_runtime=ClawRuntimeConfig(
                        enabled=True,
                        mode="managed",
                        routing_mode="shadow",
                        acp_enabled=True,
                        codex_harness_command=node_path,
                        codex_harness_args=[str(harness_script)],
                    ),
                )
            )
            try:
                runtime = service.get_claw_runtime_panel(refresh=True)
                self.assertTrue(runtime["healthy"])
                self.assertTrue(runtime["codex_harness_configured"])

                session = service.claw_runtime.start_acp_session({"agent": "codex"})
                self.assertTrue(session["supported"])
                self.assertEqual(session["harness_kind"], "codex")
                session_id = str(session["session_id"])
                response = service.claw_runtime.send_acp_input(session_id, {"text": "hello"})
                self.assertEqual(response["status"], "ok")
                self.assertIn("acp:hello", response["text"])
                self.assertTrue(response["running"])
                closed = service.claw_runtime.close_acp_session(session_id)
                self.assertEqual(closed["status"], "ok")
                self.assertFalse(closed["running"])
            finally:
                service.close()

    def test_acp_session_reports_unsupported_when_no_harness_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    claw_runtime=ClawRuntimeConfig(
                        enabled=True,
                        mode="managed",
                        routing_mode="shadow",
                        acp_enabled=True,
                    ),
                )
            )
            try:
                session = service.claw_runtime.start_acp_session({"agent": "codex"})
                self.assertFalse(session["supported"])
                self.assertEqual(session["status"], "unsupported")
                self.assertIn("No ACP harness command is configured", session["reason"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
