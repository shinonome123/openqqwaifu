from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..cells.prompt_builder import RelationshipContext
from ..models import EmotionState, InboundEvent
from ..systems.value_game import AffinityOutcome, ValueGameEngine


@dataclass(slots=True)
class RelationshipTracker:
    state_store: object
    value_game: ValueGameEngine
    sanitize_profile_summary: Callable[[str], str] | None = None

    def build_context(
        self,
        event: InboundEvent,
        member: dict[str, Any] | None,
        *,
        address: str = "",
    ) -> RelationshipContext:
        affinity_score = float((member or {}).get("affinity_score") or 0.0)
        profile_summary = str((member or {}).get("profile_summary", "") or "")
        if self.sanitize_profile_summary is not None:
            profile_summary = self.sanitize_profile_summary(profile_summary)
        return RelationshipContext(
            address=str(address or event.sender_name or event.sender_id).strip() or "对方",
            bond_stage=self.value_game.bond_stage(affinity_score),
            affinity_score=affinity_score,
            profile_summary=profile_summary,
        )

    def apply_reply(
        self,
        *,
        event: InboundEvent,
        emotion: EmotionState,
        reply_text: str,
        search_used: bool,
    ) -> AffinityOutcome | None:
        return self.value_game.apply(
            state_store=self.state_store,
            event=event,
            emotion=emotion,
            reply_text=reply_text,
            search_used=search_used,
        )
