from __future__ import annotations

import sys
import tempfile
import unittest
import json
import hashlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.config import AppConfig
from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.repositories.runtime_state import SqliteRuntimeStateStore
from waifu_standalone.skills import (
    SkillExecutor,
    SkillHandlerSpec,
    SkillManifest,
    SkillPolicySpec,
    SkillRegistry,
    SkillTriggerSpec,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    get_skill_telemetry_summary,
    record_skill_telemetry_event,
    set_skill_telemetry_store,
)
from waifu_standalone.skills import registry as registry_module
from waifu_standalone.skills.registry import parse_skill_markdown
from waifu_standalone.skills.tool_aliases import ToolBindingError


class SkillManifestContractTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_skill_telemetry_store(None)

    def test_builtin_manifests_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(AppConfig(data_root=tmpdir))

            invalid = [
                (skill.skill_id, skill.validation_errors)
                for skill in registry.list_skills()
                if skill.source_kind == "builtin" and skill.validation_errors
            ]

            self.assertEqual(invalid, [])

    def test_executor_records_success_telemetry(self) -> None:
        set_skill_telemetry_store(None)
        tools = ToolRegistry()
        tools.register(
            "echo-tool",
            name="Echo",
            description="Echo test.",
            handler=lambda invocation: None,
            model_handler=lambda invocation: ToolExecutionResult(text=str(invocation.argument("message"))),
        )
        manifest = _manifest(
            skill_id="contract-echo",
            tool_id="echo-tool",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        )
        invocation = _invocation(manifest, arguments={"message": "hello"})

        result = SkillExecutor(tools).run(manifest, invocation)
        summary = get_skill_telemetry_summary("contract-echo")

        self.assertEqual(result.text, "hello")
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["success"], 1)
        self.assertIn("trace_id", summary["last"])

    def test_executor_records_schema_failure(self) -> None:
        set_skill_telemetry_store(None)
        tools = ToolRegistry()
        tools.register(
            "echo-tool",
            name="Echo",
            description="Echo test.",
            handler=lambda invocation: None,
            model_handler=lambda invocation: ToolExecutionResult(text="should not run"),
        )
        manifest = _manifest(
            skill_id="contract-schema-fail",
            tool_id="echo-tool",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        )

        result = SkillExecutor(tools).run(manifest, _invocation(manifest, arguments={}))
        summary = get_skill_telemetry_summary("contract-schema-fail")

        self.assertIn("missing required field", result.error)
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["failure"], 1)
        self.assertEqual(summary["last"]["error_code"], "invalid_arguments")

    def test_sqlite_telemetry_persists_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime-state.db"
            store = SqliteRuntimeStateStore(db_path)
            set_skill_telemetry_store(store)

            record_skill_telemetry_event(
                {
                    "skill_id": "persistent-skill",
                    "tool_id": "summary",
                    "trigger_source": "command",
                    "status": "ok",
                    "trace_id": "trace-persist",
                    "caller": "test",
                    "latency_ms": 7,
                    "created_at": 1,
                }
            )
            reloaded = SqliteRuntimeStateStore(db_path)

            summary = reloaded.skill_telemetry_summary("persistent-skill")

            self.assertEqual(summary["calls"], 1)
            self.assertEqual(summary["success"], 1)
            self.assertEqual(summary["last"]["trace_id"], "trace-persist")

    def test_manifest_conflicts_are_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir(parents=True)
            (skills_dir / "left.md").write_text(
                _skill_markdown("left-skill", command="same-command", tool_id="summary", keywords=["same keyword"]),
                encoding="utf-8",
            )
            (skills_dir / "right.md").write_text(
                _skill_markdown("right-skill", command="same-command", tool_id="search", keywords=["same keyword"]),
                encoding="utf-8",
            )
            registry = SkillRegistry(AppConfig(data_root=tmpdir))

            errors = {
                skill.skill_id: {item["code"] for item in skill.validation_errors}
                for skill in registry.list_skills()
                if skill.skill_id in {"left-skill", "right-skill"}
            }

            self.assertIn("command_conflict", errors["left-skill"])
            self.assertIn("keyword_conflict", errors["right-skill"])

    def test_dangerous_tool_requires_explicit_authorization(self) -> None:
        skill = parse_skill_markdown(
            _skill_markdown("unsafe-write", command="unsafe-write", tool_id="write-file", keywords=["write unsafe"]),
            source="unsafe-write.md",
        )
        registry = SkillRegistry(AppConfig(data_root=tempfile.mkdtemp()))

        registry._apply_conflict_validation([skill])

        self.assertIn("dangerous_tool_requires_authorization", {item["code"] for item in skill.validation_errors})

    def test_legacy_format_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "legacy skill fields"):
            parse_skill_markdown(
                """---
name: legacy
triggers: ["legacy"]
---
Legacy body.
""",
                source="legacy.md",
            )

    def test_reload_reports_tool_binding_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(AppConfig(data_root=tmpdir))
            with patch.object(
                registry_module,
                "reload_tool_bindings",
                side_effect=ToolBindingError("tool_bindings_alias_conflict", "alias conflict"),
            ):
                payload = registry.reload()

            self.assertEqual(payload["reload_status"], "error")
            self.assertEqual(payload["tool_binding_errors"][0]["code"], "tool_bindings_alias_conflict")

    def test_signature_field_validates_body_digest(self) -> None:
        body = "Signed contract fixture."
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        skill = parse_skill_markdown(
            f"""---
id: signed-skill
name: signed-skill
description: Signed fixture.
signature: sha256:{digest}
input_schema: {{"type":"object","properties":{{}}}}
output_schema: {{"type":"object","properties":{{}}}}
trigger: {{"command":"signed-skill","llm_tool":false,"keywords":[]}}
handler: {{"type":"prompt_template","target":"signed-skill"}}
policy: {{"priority":1,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}}
default_args: {{}}
---
{body}
""",
            source="signed-skill.md",
        )

        self.assertTrue(skill.metadata["signature_verified"])
        self.assertEqual(skill.validation_errors, [])


def _manifest(*, skill_id: str, tool_id: str, input_schema: dict[str, object]) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        name=skill_id,
        description="contract skill",
        input_schema=input_schema,
        output_schema={"type": "object", "properties": {}},
        trigger=SkillTriggerSpec(command=skill_id, llm_tool=True, keywords=[]),
        handler=SkillHandlerSpec(type="tool_id", target=tool_id, arg_mode="structured"),
        policy=SkillPolicySpec(priority=1, user_invocable=True),
        default_args={},
        metadata={},
        content="contract skill",
        source="contract",
    )


def _invocation(manifest: SkillManifest, *, arguments: dict[str, object]) -> ToolInvocation:
    event = InboundEvent(
        launcher_id="launcher",
        launcher_type="person",
        sender_id="sender",
        sender_name="tester",
        segments=[MessageSegment(kind="text", text="contract")],
    )
    return ToolInvocation(
        tool_id=manifest.skill_id,
        raw_args="",
        event=event,
        session=SessionMemory(launcher_id="launcher", launcher_type="person", history=[]),
        skill=manifest,
        active_skills=[manifest],
        arguments=arguments,
    )


def _skill_markdown(skill_id: str, *, command: str, tool_id: str, keywords: list[str]) -> str:
    trigger = json.dumps({"command": command, "llm_tool": True, "keywords": keywords}, ensure_ascii=False)
    return f"""---
id: {skill_id}
name: {skill_id}
description: Contract fixture.
input_schema: {{"type":"object","properties":{{}}}}
output_schema: {{"type":"object","properties":{{}}}}
trigger: {trigger}
handler: {{"type":"tool_id","target":"{tool_id}","arg_mode":"structured"}}
policy: {{"priority":1,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}}
default_args: {{}}
---
Contract fixture.
"""


if __name__ == "__main__":
    unittest.main()
