from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, ToolPolicyConfig
from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.skills import ToolInvocation

OWNER = "783190298"


def _event(sender: str) -> InboundEvent:
    return InboundEvent(sender, "person", sender, "tester", [MessageSegment(kind="text", text="你会什么")])


def _invocation(tool_id: str, sender: str = OWNER, *, allowed: set[str] | None = None, **arguments: object) -> ToolInvocation:
    return ToolInvocation(
        tool_id=tool_id,
        raw_args="",
        event=_event(sender),
        session=SessionMemory(sender, "person"),
        arguments=dict(arguments),
        allowed_tool_ids=allowed,
    )


class SkillListTests(unittest.TestCase):
    def test_the_list_names_only_what_this_person_can_ask_for_and_how(self) -> None:
        service, _ = build_default_service(AppConfig(tool_policy=ToolPolicyConfig(authorized_user_ids=[OWNER])))
        stranger = service.tool_orchestrator._allowed_tool_ids(_event("10001"))
        owner = service.tool_orchestrator._allowed_tool_ids(_event(OWNER))

        for_stranger = service.dispatcher.use_skill_list_tool(_invocation("skill-list", "10001", allowed=stranger))
        for_owner = service.dispatcher.use_skill_list_tool(_invocation("skill-list", allowed=owner))

        self.assertEqual(for_stranger.display_mode, "inline")  # the character says it, no code block
        self.assertIn("- 天气查询：说「天气…」，或 /skill weather", for_stranger.text)
        for hidden in ("读取文件", "写入文件", "执行命令", "抓取网页", "简洁回答", "keywords=", "tool="):
            self.assertNotIn(hidden, for_stranger.text)
        self.assertIn("- 读取文件：", for_owner.text)


class SharedFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.files = self.data / "files"
        self.files.mkdir()
        (self.data / "docker-compose-config.json").write_text('{"llm": {"api_key": "sk-secret"}}', encoding="utf-8")
        shared = [str(self.files)]
        self.service, _ = build_default_service(
            AppConfig(
                data_root=temp.name,
                tool_policy=ToolPolicyConfig(
                    allowed_roots=shared, write_allowed_roots=shared, write_enabled=True, authorized_user_ids=[OWNER]
                ),
            )
        )

    def test_relative_paths_land_in_the_shared_folder(self) -> None:
        dispatcher = self.service.dispatcher

        written = dispatcher.use_write_file_tool(_invocation("write-file", path="notes/todo.txt", content="买牛奶"))
        self.assertFalse(written.error, written.text)
        self.assertEqual((self.files / "notes" / "todo.txt").read_text(encoding="utf-8"), "买牛奶")

        listed = dispatcher.use_list_files_tool(_invocation("list-files", recursive=True))
        self.assertIn("todo.txt", listed.text)
        read = dispatcher.use_read_file_tool(_invocation("read-file", path="notes/todo.txt"))
        self.assertIn("买牛奶", read.text)

    def test_the_model_gets_the_file_tools_only_for_the_owner(self) -> None:
        self.service.config.llm.enabled = True

        def offered(sender: str, text: str) -> list[str]:
            event = InboundEvent(sender, "person", sender, "tester", [MessageSegment(kind="text", text=text)])
            context = self.service.tool_orchestrator.exposure_context(
                event, SessionMemory(sender, "person"), latest_message=text, address="tester", assistant_name="极光"
            )
            return sorted(schema["name"] for schema in self.service.tools.model_schemas(context))

        # No file skill's own keyword appears in these; the wording alone has to bring the tools.
        for text in ("看看文件夹里现在有什么", "把刚才那个笔记读给我听"):
            self.assertEqual(
                offered(OWNER, text),
                ["list-files-command", "read-file-command", "send-file-command", "write-file-command"],
            )
            self.assertEqual(offered("10001", text), [])

    def test_nothing_outside_the_shared_folder_is_reachable(self) -> None:
        dispatcher = self.service.dispatcher
        for path in ("../docker-compose-config.json", str(self.data / "docker-compose-config.json")):
            result = dispatcher.use_read_file_tool(_invocation("read-file", path=path))
            self.assertIn("路径超出允许范围", result.text)
            self.assertNotIn("sk-secret", result.text)
        listed = dispatcher.use_list_files_tool(_invocation("list-files", path=".."))
        self.assertIn("路径超出允许范围", listed.text)


if __name__ == "__main__":
    unittest.main()
