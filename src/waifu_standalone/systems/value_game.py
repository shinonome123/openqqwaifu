"""How close the character is to each person, and how that shows in her tone.

Every reply to someone moves the score of that person (per character, per chat). A
conversation counts, not each line of it: a reply a minute after the last one to the same
person adds a thirtieth of what one after half an hour does, and gains shrink as the
score nears 1, so a bond takes weeks of talk to become close. Left unanswered for a while it
fades, and calling her names costs her goodwill. The stage of the bond (``bond_stage``)
picks the tone her reply takes (``relationship_note``); a card can word the tones itself
in its meta's ``tone_by_stage``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..models import EmotionState, InboundEvent

# A reply this long after the last one to the same person is a new conversation and gains
# in full; a sooner one gains its share of the half hour, at least a fiftieth.
_FULL_GAP_SECONDS = 1800.0
_MIN_WARMTH = 0.02
# Unanswered for this many days, a bond starts to fade by this share a day.
_FADE_AFTER_DAYS = 3.0
_FADE_PER_DAY = 0.03
_INSULT = re.compile(
    r"傻[逼比]|煞笔|(?<![a-z])sb(?![a-z])|滚(?:开|蛋|吧|远点)|给我滚|闭嘴|去死|"
    r"你(?:个|这个|真是个?|就是个?)?(?:废物|蠢货|白痴|智障|弱智|脑残|垃圾)",
    re.IGNORECASE,
)
_INSULT_DELTA = -0.05
STAGE_LABELS = {
    "cold": "关系很僵",
    "new": "刚认识",
    "warming": "渐渐熟起来",
    "familiar": "熟人",
    "close": "亲近",
    "devoted": "最亲近的人",
}
# The tone of each stage when the card's meta does not word it.
DEFAULT_TONES = {
    "cold": "对方最近对你很不客气：你冷淡、话少，不主动示好，但不会恶语相向。",
    "new": "还不熟：客气、有分寸，带着你自己的性格，不装熟，不用亲昵的称呼。",
    "warming": "开始熟了：可以放松一点，偶尔开个玩笑，记得对方说过的事。",
    "familiar": "已经是熟人：说话随意自然，可以吐槽、逗对方，也会顺口关心几句。",
    "close": "关系亲近：明显更在意对方，语气更软、更护着对方，会主动关心，偶尔流露依赖。",
    "devoted": "对方在你心里很特别：偏心、护短、黏人都可以表现出来，但仍是你自己的性格。",
}


@dataclass(slots=True)
class AffinityOutcome:
    delta: float
    score: float
    stage: str


class ValueGameEngine:
    def __init__(self, config: AppConfig):
        self.config = config

    @staticmethod
    def bond_stage(score: object) -> str:
        try:
            value = float(score)
        except (TypeError, ValueError):
            value = 0.0
        if value < 0:
            return "cold"
        if value < 0.12:
            return "new"
        if value < 0.3:
            return "warming"
        if value < 0.55:
            return "familiar"
        if value < 0.8:
            return "close"
        return "devoted"

    @staticmethod
    def effective_score(score: object, last_addressed_at: object, now: float) -> float:
        """The score after the days since she last answered this person, fading past three."""
        try:
            value, last = float(score or 0.0), float(last_addressed_at or 0.0)
        except (TypeError, ValueError):
            return 0.0
        idle_days = (now - last) / 86400.0 - _FADE_AFTER_DAYS if last else 0.0
        return value * (1.0 - _FADE_PER_DAY) ** idle_days if idle_days > 0 else value

    def relationship_note(self, member: dict[str, Any] | None, *, address: str, tones: object = None) -> str:
        """How close she is to this person and the tone that goes with it, for her prompt."""
        if not self.config.value_game_mode or member is None:
            return ""
        score = self.effective_score(member.get("affinity_score"), member.get("last_addressed_at"), time.time())
        stage = self.bond_stage(score)
        custom = tones.get(stage) if isinstance(tones, dict) else None
        tone = str(custom).strip() if isinstance(custom, str) and custom.strip() else DEFAULT_TONES[stage]
        return f"你和{address}：{STAGE_LABELS[stage]}。{tone}（这只影响你的语气，别把好感、关系阶段这些说出口。）"

    def apply(
        self,
        *,
        state_store: object,
        event: InboundEvent,
        emotion: EmotionState,
        character_id: str = "",
        now: float | None = None,
    ) -> AffinityOutcome | None:
        """Move the bond after a reply; call it before the member is marked as answered."""
        if not self.config.value_game_mode:
            return None
        group_id = event.launcher_id if event.launcher_type == "group" else ""
        get_member = getattr(state_store, "get_member", None)
        adjust = getattr(state_store, "adjust_member_affinity", None)
        if not callable(get_member) or not callable(adjust):
            return None
        member = get_member(group_id=group_id, user_id=event.sender_id, character_id=character_id)
        if member is None:
            return None
        now = time.time() if now is None else now
        stored = float(member.get("affinity_score") or 0.0)
        last = float(member.get("last_addressed_at") or 0.0)
        current = self.effective_score(stored, last, now)
        gap = now - last if last else _FULL_GAP_SECONDS
        target = max(-1.0, min(1.0, current + self._gain(event=event, emotion=emotion, score=current, gap_seconds=gap)))
        delta = target - stored
        if abs(delta) < 1e-6:
            return None
        adjusted = adjust(group_id=group_id, user_id=event.sender_id, delta=delta, character_id=character_id)
        if adjusted is None:
            return None
        score = float(adjusted.get("affinity_score") or 0.0)
        return AffinityOutcome(delta=delta, score=score, stage=self.bond_stage(score))

    def _gain(self, *, event: InboundEvent, emotion: EmotionState, score: float, gap_seconds: float) -> float:
        if _INSULT.search(event.plain_text):
            return _INSULT_DELTA
        gain = float(self.config.value_game_reply_bonus)
        if event.launcher_type == "person":
            gain += 0.01
        if event.has_bot_mention(self.config.bot_account_id):
            gain += 0.005
        emotion_name = str(getattr(emotion, "primary", "") or "").strip().lower()
        if emotion_name in {"joy", "love", "sadness", "anxiety"}:
            gain += 0.005  # shared joy, or comfort when it was needed
        elif emotion_name == "anger":
            gain -= 0.01
        warmth = max(_MIN_WARMTH, min(1.0, gap_seconds / _FULL_GAP_SECONDS))
        return gain * warmth * (1.0 - max(0.0, score)) if gain > 0 else gain
