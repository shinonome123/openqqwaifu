from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_file_service
from waifu_standalone.character import switcher as switcher_module
from waifu_standalone.config import AppConfig
from waifu_standalone.models import InboundEvent, MessageSegment

BOT_ID = "3956638110"
GROUP_ID = "612475113"
OWNER = "783190298"


def _card(name: str, profile: str) -> str:
    return f"user_name: 主人\nassistant_name: {name}\nlanguage: 简体中文\nProfile:\n  - {profile}\n"


def _group(text: str, *, sender: str = OWNER, mention: bool = True) -> InboundEvent:
    segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
    segments.append(MessageSegment(kind="text", text=text))
    return InboundEvent(GROUP_ID, "group", sender, f"user{sender}", segments)


def _private(text: str, *, sender: str = OWNER) -> InboundEvent:
    return InboundEvent(sender, "person", sender, f"user{sender}", [MessageSegment(kind="text", text=text)])


class CharacterSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cards = Path(temp.name) / "cards"
        cards.mkdir()
        for character, name, profile in (
            ("aurora", "极光", "你是极光，傲娇的小公主，嘴硬心软。"),
            ("xiaokui", "小葵", "你是小葵，元气满满的雌小鬼。"),
            ("wanqing", "晚晴", "你是晚晴，温柔的大姐姐。"),
        ):
            for variant in ("person", "group"):
                (cards / f"{character}_{variant}.yaml").write_text(_card(name, profile), encoding="utf-8")
        (cards / "xiaokui.meta.json").write_text(
            json.dumps({"intro": "爱捉弄人的元气雌小鬼", "aliases": ["雌小鬼"], "greetings": ["小葵来啦~杂鱼们想我没？"]}),
            encoding="utf-8",
        )
        (cards / "wanqing.meta.json").write_text(json.dumps({"aliases": ["温柔大姐姐"]}), encoding="utf-8")
        (cards / "active_character.json").write_text(json.dumps({"character": "aurora"}), encoding="utf-8")
        self.config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, group_follow_up_window_seconds=60.0)
        self.service, self.outbound = build_file_service(self.config)
        self.addCleanup(self.service.close)

    def _texts(self) -> list[str]:
        return [part.text for part in self.outbound.sent]

    def _switch(self, event: InboundEvent) -> None:
        self.assertIsNone(self.service.handle_event(event))
        self.service.character_switcher.pending.result(timeout=10)

    def test_the_list_names_each_card_with_its_intro(self) -> None:
        self.service.handle_event(_group("切换人物卡"))

        listing = self._texts()[-1]
        self.assertIn("1. 极光（现在）：傲娇的小公主，嘴硬心软。", listing)
        self.assertIn("小葵：爱捉弄人的元气雌小鬼", listing)  # the meta intro wins over the profile
        self.assertIn("晚晴：温柔的大姐姐。", listing)
        self.assertIn("@我说「切换+名字」", listing)
        self.assertNotIn("default", listing)  # the bundled template is not offered

    def test_switching_by_name_changes_the_character_and_it_greets(self) -> None:
        self._switch(_group("切换小葵"))

        self.assertEqual(self.service._active_character_id(), "xiaokui")
        self.assertEqual(self._texts()[-1], "小葵来啦~杂鱼们想我没？")
        history = self.service.memory.load(GROUP_ID, "group", character_id="xiaokui").history
        self.assertEqual(history[-1], "assistant: 小葵来啦~杂鱼们想我没？")
        # Later messages are answered by the new card.
        self.assertEqual(self.service.cards.load("group", self.service.memory.load(GROUP_ID, "group")).assistant_name, "小葵")

    def test_aliases_and_wordings_find_the_card(self) -> None:
        self._switch(_private("切换到元气雌小鬼"))
        self.assertEqual(self.service._active_character_id(), "xiaokui")
        self._switch(_private("换成大姐姐"))
        self.assertEqual(self.service._active_character_id(), "wanqing")
        self.assertEqual(self._texts()[-1], "（已切换到「晚晴」）")  # no greetings in its meta
        self._switch(_private("切换回极光"))
        self.assertEqual(self.service._active_character_id(), "aurora")

    def test_ordinary_chat_and_unaddressed_group_messages_are_not_commands(self) -> None:
        self.service.handle_event(_group("在吗"))  # opens the follow-up window
        self.service.handle_event(_group("切换小葵", mention=False))
        self.service.handle_event(_group("切换一下话题"))

        self.assertIsNone(self.service.character_switcher.pending)
        self.assertEqual(self.service._active_character_id(), "aurora")

    def test_the_current_card_and_the_allow_list_answer_without_switching(self) -> None:
        self.service.handle_event(_group("切换极光"))
        self.assertEqual(self._texts()[-1], "现在就是「极光」。")

        self.service.config.character_switch_user_ids = ["10001"]
        self.service.handle_event(_group("切换小葵"))
        self.assertEqual(self._texts()[-1], "人物卡只有指定的人能换。")
        self.assertEqual(self.service._active_character_id(), "aurora")

    def _busy(self) -> Future[None]:
        """A write-back that is still running, as far as the service can tell."""
        busy: Future[None] = Future()
        with self.service._background_lock:
            self.service._background_tasks.add(busy)
        busy.add_done_callback(lambda done: self.service._background_task_done("test-busy", done))
        self.addCleanup(lambda: busy.done() or busy.set_result(None))
        return busy

    def test_a_switch_waits_for_running_work(self) -> None:
        busy = self._busy()
        self.assertIsNone(self.service.handle_event(_group("切换小葵")))
        with self.assertRaises(TimeoutError):
            self.service.character_switcher.pending.result(timeout=1.5)
        self.assertEqual(self.service._active_character_id(), "aurora")
        busy.set_result(None)
        self.service.character_switcher.pending.result(timeout=10)
        self.assertEqual(self.service._active_character_id(), "xiaokui")

    def test_a_switch_gives_up_when_the_bot_stays_busy(self) -> None:
        self._busy()
        with patch.object(switcher_module, "SWITCH_WAIT_SECONDS", 0.5):
            with self.assertLogs("waifu_standalone.character.switcher", level="WARNING"):
                self._switch(_group("切换晚晴"))
        self.assertEqual(self.service._active_character_id(), "aurora")
        self.assertEqual(self._texts()[-1], "现在有点忙，没换成「晚晴」，过会儿再说一次吧。")


if __name__ == "__main__":
    unittest.main()
