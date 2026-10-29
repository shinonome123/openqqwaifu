from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import GeneratedImage
from ..models import EmotionState, InboundEvent, OutboundMessage, SessionMemory


class RuleBasedEmotionAnalyzer:
    def analyze(self, event: InboundEvent, memory: SessionMemory) -> EmotionState:
        text = event.plain_text.lower()
        if not text:
            return EmotionState(primary="neutral", intensity=0.0)
        if "draw" in text:
            return EmotionState(primary="anticipation", intensity=0.8)
        if "call me" in text:
            return EmotionState(primary="love", intensity=0.6)
        return EmotionState(primary="trust", intensity=0.4)


class StubImageGenerator:
    def generate(self, prompt: str) -> GeneratedImage:
        cleaned = prompt.strip()
        if not cleaned:
            raise ValueError("image prompt is empty")
        return GeneratedImage(prompt=cleaned, image_ref=f"generated://{cleaned}")


@dataclass(slots=True)
class CapturingOutboundPort:
    sent: list[OutboundMessage] = field(default_factory=list)
    # Other OneBot actions (file uploads), as (action, params).
    actions: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def send(self, message: OutboundMessage) -> dict[str, object]:
        self.sent.append(message)
        # As QQ answers a send: the new message's id ("1", "2", … in sending order).
        return {"status": "ok", "retcode": 0, "data": {"message_id": str(len(self.sent))}}

    def call_action(self, action: str, params: dict[str, object]) -> dict[str, object]:
        self.actions.append((action, dict(params)))
        return {"status": "ok", "retcode": 0, "data": {}}

    async def acall_action(self, action: str, params: dict[str, object]) -> dict[str, object]:
        return self.call_action(action, params)
