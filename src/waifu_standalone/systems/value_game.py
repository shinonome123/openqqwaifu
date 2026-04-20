from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig
from ..models import EmotionState, InboundEvent


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
        if value < 0.12:
            return "new"
        if value < 0.3:
            return "warming"
        if value < 0.55:
            return "familiar"
        if value < 0.8:
            return "close"
        return "devoted"

    def apply(
        self,
        *,
        state_store: object,
        event: InboundEvent,
        emotion: EmotionState,
        reply_text: str,
        search_used: bool,
        character_id: str = "",
    ) -> AffinityOutcome | None:
        if not self.config.value_game_mode:
            return None
        delta = self._compute_delta(
            event=event,
            emotion=emotion,
            reply_text=reply_text,
            search_used=search_used,
        )
        if abs(delta) < 1e-6:
            return None
        group_id = event.launcher_id if event.launcher_type == "group" else ""
        adjusted = None
        adjust = getattr(state_store, "adjust_member_affinity", None)
        if callable(adjust):
            adjusted = adjust(
                group_id=group_id,
                user_id=event.sender_id,
                delta=delta,
                character_id=character_id,
            )
        if adjusted is None:
            return None
        score = float(adjusted.get("affinity_score") or 0.0)
        return AffinityOutcome(delta=delta, score=score, stage=self.bond_stage(score))

    def _compute_delta(
        self,
        *,
        event: InboundEvent,
        emotion: EmotionState,
        reply_text: str,
        search_used: bool,
    ) -> float:
        delta = float(self.config.value_game_reply_bonus)
        if event.launcher_type == "person":
            delta += 0.05
        if event.has_bot_mention(self.config.bot_account_id):
            delta += 0.03
        if event.image_count > 0:
            delta += 0.02
        if search_used:
            delta += 0.015
        if len(str(reply_text or "").strip()) > 32:
            delta += 0.01
        emotion_name = str(getattr(emotion, "primary", "") or "").strip().lower()
        if emotion_name in {"joy", "love", "anticipation"}:
            delta += 0.025
        elif emotion_name in {"sadness", "anxiety"}:
            delta += 0.015
        elif emotion_name == "anger":
            delta -= 0.02
        return max(-0.25, min(0.25, delta))
