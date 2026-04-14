from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.cells.generator import Generator
from waifu_standalone.config import AppConfig
from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.organs.thoughts import Thoughts


class ThoughtsTests(unittest.TestCase):
    def test_fallback_analysis_mentions_direct_addressing(self) -> None:
        config = AppConfig(bot_account_id="3518944354", thinking_mode=True)
        thoughts = Thoughts(config, Generator(config))
        session = SessionMemory(launcher_id="1", launcher_type="group")
        event = InboundEvent(
            launcher_id="1",
            launcher_type="group",
            sender_id="2",
            sender_name="tester",
            segments=[
                MessageSegment(kind="mention", mention_target="3518944354"),
                MessageSegment(kind="text", text=" hello"),
            ],
        )

        analysis = thoughts.analyze(
            event,
            session,
            assistant_name="琉璃",
            conversation_view="tester：hello",
            memory_hints=[],
            speaker_notes=[],
        )

        self.assertIn("直接点你", analysis)

    def test_disabled_thinking_mode_returns_empty_analysis(self) -> None:
        config = AppConfig(thinking_mode=False)
        thoughts = Thoughts(config, Generator(config))

        analysis = thoughts.analyze(
            InboundEvent(
                launcher_id="1",
                launcher_type="person",
                sender_id="2",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="hello")],
            ),
            SessionMemory(launcher_id="1", launcher_type="person"),
            assistant_name="琉璃",
            conversation_view="tester：hello",
            memory_hints=[],
            speaker_notes=[],
        )

        self.assertEqual(analysis, "")


if __name__ == "__main__":
    unittest.main()
