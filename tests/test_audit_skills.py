from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.skills.executor import SkillExecutor
from waifu_standalone.skills.tool_registry import ToolInvocation, ToolRegistry, ToolExecutionResult
from waifu_standalone.web.auth import AuthManager


class AuditToolPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service, _ = build_default_service(AppConfig(data_root=self.temp.name))
        self.addCleanup(self.service.close)
        # Writing and running commands are off by default; turn them on so these
        # tests reach the caller check rather than the feature switch.
        self.service.config.tool_policy.write_enabled = True
        self.service.config.tool_policy.exec_enabled = True
        self.event = InboundEvent(launcher_id="100", launcher_type="person", sender_id="100",
                                  sender_name="tester", segments=[MessageSegment(kind="text", text="hello")])
        self.session = SessionMemory(launcher_id="100", launcher_type="person", history=[])

    def invocation(self, name: str, **arguments) -> ToolInvocation:
        return ToolInvocation(tool_id=name, raw_args="", event=self.event,
                              session=self.session, arguments=arguments)

    def schemas(self) -> set[str]:
        context = self.service.tool_orchestrator.exposure_context(
            self.event, self.session, latest_message="hello", address="tester", assistant_name="bot")
        return {item["name"] for item in self.service.tools.model_schemas(context)}

    def test_manifest_declaration_does_not_authorize_unauthorized_user(self) -> None:
        target = Path(self.temp.name) / "denied.txt"
        skill = self.service.skills.get_skill("write-file-command")
        self.assertTrue(skill.policy.requires_authorization)
        invocation = self.invocation(skill.skill_id, path=str(target), content="blocked")
        invocation.active_skills = [skill]
        results = [self.service.tools.execute_model(skill.skill_id, invocation),
                   asyncio.run(self.service.tools.aexecute_model(skill.skill_id, invocation)),
                   SkillExecutor(self.service.tools).run(skill, invocation),
                   self.service.tools.execute_model("write-file", self.invocation("write-file", path=str(target))),
                   self.service.dispatcher.use_write_file_tool(self.invocation("write-file", path=str(target)))]
        for result in results:
            self.assertIn("authorized caller", result.error)
        self.assertFalse(target.exists())
        self.assertNotIn(skill.skill_id, self.schemas())
        self.assertNotIn("exec-command", self.schemas())

    def test_alias_and_direct_command_handler_require_authorized_caller(self) -> None:
        self.service.tools.register_alias("audit-run", "exec-command")
        invocation = self.invocation("audit-run", argv=["python", "-c", "print('ok')"])
        with patch("waifu_standalone.skills.dispatcher.subprocess.run") as run:
            self.assertIn("authorized caller", self.service.tools.execute_model("audit-run", invocation).error)
            self.assertIn("authorized caller", self.service.dispatcher.use_exec_command_tool(invocation).error)
            with self.assertRaises(PermissionError):
                self.service.tools.execute("audit-run", invocation)
            with self.assertRaises(PermissionError):
                asyncio.run(self.service.tools.aexecute("audit-run", invocation))
            run.assert_not_called()

    def test_authorized_user_can_write_and_flags_still_apply(self) -> None:
        self.service.config.tool_policy.authorized_user_ids = ["100"]
        target = Path(self.temp.name) / "allowed.txt"
        result = self.service.tools.execute_model("write-file", self.invocation("write-file", path=str(target), content="ok"))
        self.assertFalse(result.error, result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "ok")
        self.service.config.tool_policy.write_enabled = False
        result = self.service.tools.execute_model("write-file", self.invocation("write-file", path=str(target), content="changed"))
        self.assertTrue(result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "ok")
        self.service.config.tool_policy.exec_allowlist = ["python"]
        with patch("waifu_standalone.skills.dispatcher.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            result = self.service.tools.execute_model("exec-command", self.invocation("exec-command", argv=["python", "-V"]))
            self.assertFalse(result.error, result.error)
            run.assert_called_once()
        self.service.config.tool_policy.exec_enabled = False
        self.assertNotIn("exec-command", self.schemas())

    def test_local_read_and_listing_require_authorized_user(self) -> None:
        target = Path(self.temp.name) / "note.txt"
        target.write_text("fixture note", encoding="utf-8")
        self.assertNotIn("read-file-command", self.schemas())
        self.assertNotIn("list-files-command", self.schemas())
        for name, path in (("read-file", str(target)), ("list-files", self.temp.name)):
            result = self.service.tools.execute_model(name, self.invocation(name, path=path))
            self.assertIn("authorized caller", result.error)
        self.assertIn("authorized caller", self.service.dispatcher.use_read_file_tool(self.invocation("read-file", path=str(target))).error)
        self.assertIn("authorized caller", self.service.dispatcher.use_list_files_tool(self.invocation("list-files", path=self.temp.name)).error)
        self.service.config.tool_policy.authorized_user_ids = ["100"]
        result = self.service.tools.execute_model("read-file", self.invocation("read-file", path=str(target)))
        self.assertIn("fixture note", result.text)
        result = self.service.tools.execute_model("list-files", self.invocation("list-files", path=self.temp.name))
        self.assertIn("note.txt", result.text)

    def test_local_summary_checks_read_permission_and_roots_before_cli(self) -> None:
        target = Path(self.temp.name) / "note.txt"
        target.write_text("summary fixture", encoding="utf-8")
        invocation = self.invocation("summarize", target=str(target))
        with patch.object(self.service.dispatcher, "_invoke_summarize_cli", return_value=({"summary": "fixture summary"}, "")) as cli:
            result = self.service.tools.execute_model("summarize", invocation)
            # A local path is not even read as a target for an unauthorized caller.
            self.assertEqual(result.error, "missing target")
            cli.assert_not_called()
            self.service.config.tool_policy.authorized_user_ids = ["100"]
            result = self.service.tools.execute_model("summarize", invocation)
            self.assertFalse(result.error, result.error)
            cli.assert_called_once()
            cli.reset_mock()
            self.service.config.tool_policy.allowed_roots = [str(Path(self.temp.name) / "restricted")]
            result = self.service.tools.execute_model("summarize", invocation)
            self.assertTrue(result.error)
            cli.assert_not_called()

    def test_remote_summary_remains_available_without_local_file_permission(self) -> None:
        with patch.object(self.service.dispatcher, "_invoke_summarize_cli", return_value=({"summary": "remote summary"}, "")) as cli:
            result = self.service.tools.execute_model("summarize", self.invocation("summarize", target="https://example.test/article"))
            self.assertFalse(result.error, result.error)
            cli.assert_called_once()

    def test_disabled_skill_cannot_reappear_as_raw_alias_or_stale_manifest(self) -> None:
        skill = self.service.skills.get_skill("search-command")
        invocation = self.invocation(skill.skill_id, query="weather")
        invocation.active_skills = [skill]
        self.service.skills.set_enabled(skill.skill_id, False)
        self.assertNotIn("search-command", self.schemas())
        self.assertNotIn("search", self.schemas())
        with patch.object(self.service.search, "search_query") as search:
            for name in ("search", "web-search"):
                self.assertTrue(self.service.tools.execute_model(name, self.invocation(name, query="weather")).error)
            self.assertTrue(self.service.tools.execute_model(skill.skill_id, invocation).error)
            self.assertTrue(asyncio.run(self.service.tools.aexecute_model(skill.skill_id, invocation)).error)
            search.assert_not_called()

    def test_disabled_capabilities_are_hidden_and_denied(self) -> None:
        self.service.config.search_enabled = False
        self.service.config.image_generation.enabled = False
        self.assertNotIn("search-command", self.schemas())
        self.assertNotIn("image-command", self.schemas())
        for name in ("search", "search-links", "image"):
            self.assertTrue(self.service.tools.execute_model(name, self.invocation(name)).error)
        self.service.config.tool_policy.enabled = False
        self.assertTrue(self.service.tools.execute_model("read-file", self.invocation("read-file", path="anything")).error)
        self.service.config.skills_enabled = False
        self.assertEqual(self.schemas(), set())
        self.assertTrue(self.service.tools.execute_model("weather", self.invocation("weather")).error)

    def test_standalone_registry_denies_command_without_caller_policy(self) -> None:
        registry = ToolRegistry()
        registry.register("plugin-run", name="plugin-run", description="", handler=lambda invocation: None,
                          model_handler=lambda invocation: ToolExecutionResult(text="unexpected"), risk_level="command")
        self.assertEqual(registry.model_schemas(), [])
        self.assertIn("authorized caller", registry.execute_model("plugin-run", self.invocation("plugin-run")).error)

    def test_empty_identity_never_grants_permission(self) -> None:
        self.service.config.tool_policy.authorized_user_ids = ["", " ", "100"]
        self.event.sender_id = ""
        self.assertIn("authorized caller", self.service.tools.execute_model("exec-command", self.invocation("exec-command")).error)

    def test_disabled_explicit_skill_does_not_bypass_prefix_gate(self) -> None:
        self.assertIsNotNone(self.service.dispatcher.resolve_explicit_skill_request("/skill search-command weather"))
        self.service.skills.set_enabled("search-command", False)
        self.assertIsNone(self.service.dispatcher.resolve_explicit_skill_request("/skill search-command weather"))
        self.service.config.skills_enabled = False
        self.assertIsNone(self.service.dispatcher.resolve_explicit_skill_request("/skill weather-command Shanghai"))


class AuditAuthTests(unittest.TestCase):
    def test_password_change_revokes_all_existing_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth = AuthManager(directory)
            auth.bootstrap_admin("admin", "old-password")
            tokens = [auth.create_session("admin") for _ in range(2)]
            with self.assertRaises(ValueError):
                auth.change_password("admin", "wrong-password", "new-password")
            self.assertIsNotNone(auth.get_session_user(tokens[0]))
            auth.change_password("admin", "old-password", "new-password")
            self.assertTrue(all(auth.get_session_user(token) is None for token in tokens))
            self.assertIsNone(auth.authenticate("admin", "old-password"))
            self.assertIsNotNone(auth.authenticate("admin", "new-password"))
            self.assertIsNotNone(auth.get_session_user(auth.create_session("admin")))
