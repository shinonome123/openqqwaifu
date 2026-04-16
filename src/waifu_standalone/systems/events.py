from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig
from ..models import InboundEvent, OutboundMessage


@dataclass(slots=True)
class BehaviorEvent:
    character_id: str
    kind: str
    launcher_type: str
    launcher_id: str
    sender_id: str
    summary: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "kind": self.kind,
            "launcher_type": self.launcher_type,
            "launcher_id": self.launcher_id,
            "sender_id": self.sender_id,
            "summary": self.summary,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


class BehaviorEventEngine:
    def __init__(self, config: AppConfig):
        self.config = config

    def capture_inbound(self, event: InboundEvent, *, text: str, character_id: str = "") -> BehaviorEvent | None:
        if not self.config.event_mode:
            return None
        normalized = str(text or "").strip() or event.to_memory_text()
        event_kind = "message"
        if event.image_count > 0:
            event_kind = "image_share"
        elif event.has_bot_mention(self.config.bot_account_id):
            event_kind = "mention"
        elif self._looks_like_question(normalized):
            event_kind = "question"
        return BehaviorEvent(
            character_id=str(character_id or "").strip(),
            kind=event_kind,
            launcher_type=event.launcher_type,
            launcher_id=event.launcher_id,
            sender_id=event.sender_id,
            summary=self._clip(normalized),
            metadata={
                "image_count": event.image_count,
                "mentioned_bot": event.has_bot_mention(self.config.bot_account_id),
                "message_id": event.message_id,
            },
        )

    def capture_outbound(
        self,
        *,
        event: InboundEvent,
        message: OutboundMessage,
        reason: str = "reply",
        character_id: str = "",
    ) -> BehaviorEvent | None:
        if not self.config.event_mode:
            return None
        kind = "image_delivery" if message.images else "reply"
        if reason:
            kind = reason if kind == "reply" else kind
        return BehaviorEvent(
            character_id=str(character_id or "").strip(),
            kind=kind,
            launcher_type=message.launcher_type,
            launcher_id=message.launcher_id,
            sender_id=event.sender_id,
            summary=self._clip(message.text),
            metadata={"image_count": len(message.images)},
        )

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        lowered = str(text or "").lower()
        if not lowered:
            return False
        return any(token in lowered for token in ("?", "？", "什么", "怎么", "为何", "why", "what", "how"))

    @staticmethod
    def _clip(text: str, limit: int = 96) -> str:
        compact = " ".join(str(text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: max(12, limit - 1)].rstrip() + "…"
