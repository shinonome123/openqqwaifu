"""Dice for tavern games, rolled by the program so the model cannot invent results.

".r 2d6+1 撬锁", "/roll d20", "掷骰 3d6", "。r" (the Chinese IME full stop) all roll;
a bare ".r" or "掷骰" rolls one d20.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

_DOT = r"[.。/!！]\s*r(?:oll)?"
_WORD = r"(?:roll|掷骰子?|扔骰子?|投骰子?|掷)"
# Numbers are matched generously and range-checked afterwards, so "100d6" is refused
# instead of being read as a d20 roll for "100d6". ".r撬锁" rolls, but "掷出飞刀" and
# ".reset" are not rolls: a word command needs a space before its reason.
_ROLL = re.compile(
    rf"^(?:{_DOT}|{_WORD})\s*(?P<count>\d{{0,4}})\s*[dD](?P<sides>\d{{1,6}})\s*(?P<modifier>[+-]\s*\d{{1,6}})?(?P<reason>.*)$"
    rf"|^{_DOT}(?P<dot_reason>(?![A-Za-z]).*)$"
    rf"|^{_WORD}(?P<bare_reason>(?:\s.*)?)$",
    re.IGNORECASE | re.DOTALL,
)
MAX_DICE = 20
MAX_SIDES = 1000
MAX_MODIFIER = 1000
_RANDOM = random.SystemRandom()


@dataclass(slots=True)
class DiceRoll:
    count: int
    sides: int
    modifier: int
    reason: str
    results: list[int]

    @property
    def total(self) -> int:
        return sum(self.results) + self.modifier

    @property
    def expression(self) -> str:
        count = "" if self.count == 1 else str(self.count)
        modifier = f"{self.modifier:+d}" if self.modifier else ""
        return f"{count}d{self.sides}{modifier}"

    def announcement(self, player: str) -> str:
        """The line posted to the chat: "🎲 苦力怕为「撬锁」掷 2d6+1：3+5+1 = 9"."""
        purpose = f"为「{self.reason}」" if self.reason else ""
        if self.count == 1 and not self.modifier:
            return f"🎲 {player}{purpose}掷 {self.expression}：{self.total}"
        parts = "+".join(str(value) for value in self.results)
        if self.modifier:
            parts += f"{self.modifier:+d}"
        return f"🎲 {player}{purpose}掷 {self.expression}：{parts} = {self.total}"

    def story_line(self, player: str) -> str:
        """How the roll reaches the game master, among the players' actions."""
        purpose = f"为「{self.reason}」" if self.reason else ""
        return f"（{player}{purpose}掷了 {self.expression}，结果 {self.total}）"


class DiceError(ValueError):
    """A roll command with numbers out of range; the message says what is allowed."""


def parse_roll(text: str) -> DiceRoll | None:
    """Roll the dice a command asks for; ``None`` when ``text`` is not a roll command."""
    match = _ROLL.match(str(text or "").strip())
    if match is None:
        return None
    if match.group("sides") is None:
        count, sides, modifier = 1, 20, 0
        reason = (match.group("dot_reason") or match.group("bare_reason") or "").strip()
    else:
        count = int(match.group("count") or 1)
        sides = int(match.group("sides"))
        modifier = int((match.group("modifier") or "0").replace(" ", ""))
        reason = (match.group("reason") or "").strip()
    if not 1 <= count <= MAX_DICE or not 2 <= sides <= MAX_SIDES or abs(modifier) > MAX_MODIFIER:
        raise DiceError(f"骰子最多 {MAX_DICE} 个、每个 2 到 {MAX_SIDES} 面，加减值不超过 {MAX_MODIFIER}。")
    reason = reason.strip(" ，,：:")[:20]
    return DiceRoll(count, sides, modifier, reason, [_RANDOM.randint(1, sides) for _ in range(count)])
