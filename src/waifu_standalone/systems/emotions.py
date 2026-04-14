from __future__ import annotations

from ..models import EmotionState, InboundEvent, SessionMemory


class EmotionSensor:
    def analyze(self, event: InboundEvent, memory: SessionMemory) -> EmotionState:
        text = event.plain_text.lower()
        if not text:
            return EmotionState(primary="neutral", intensity=0.0)
        if "draw" in text:
            return EmotionState(primary="anticipation", intensity=0.8)
        if "call me" in text:
            return EmotionState(primary="love", intensity=0.6)
        return EmotionState(primary="trust", intensity=0.4)
