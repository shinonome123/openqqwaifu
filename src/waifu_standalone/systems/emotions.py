from __future__ import annotations

from ..models import EmotionState, InboundEvent, SessionMemory


class EmotionSensor:
    """Cheap local emotion estimate used for relationship scoring only."""

    def quick_estimate(self, text: str, *, history_size: int = 0) -> EmotionState:
        lowered = str(text or "").lower()
        if not lowered:
            return EmotionState(primary="neutral", intensity=0.0)

        strong_rules = (
            (("爱你", "喜欢你", "想你", "抱抱"), EmotionState(primary="love", intensity=0.75)),
            (("开心", "高兴", "快乐", "嘿嘿"), EmotionState(primary="joy", intensity=0.7)),
            (("难过", "伤心", "委屈", "呜呜"), EmotionState(primary="sadness", intensity=0.7)),
            (("生气", "愤怒", "烦死了", "火大"), EmotionState(primary="anger", intensity=0.75)),
            (("害怕", "紧张", "焦虑", "担心"), EmotionState(primary="anxiety", intensity=0.7)),
            (("draw", "生图", "画图"), EmotionState(primary="anticipation", intensity=0.65)),
        )
        for keywords, state in strong_rules:
            if any(keyword in lowered for keyword in keywords):
                return state

        if history_size >= 4:
            return EmotionState(primary="trust", intensity=0.55)
        return EmotionState(primary="neutral", intensity=0.25)

    def analyze(self, event: InboundEvent, memory: SessionMemory) -> EmotionState:
        return self.quick_estimate(event.plain_text, history_size=len(memory.history))
