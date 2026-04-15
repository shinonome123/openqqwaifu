from __future__ import annotations

from ..config import AppConfig
from ..models import EmotionState, InboundEvent


class Narrator:
    def __init__(self, config: AppConfig):
        self.config = config

    def build_hint(
        self,
        *,
        event: InboundEvent,
        latest_message: str,
        address: str,
        emotion: EmotionState,
        memory_graph: dict[str, object] | None = None,
    ) -> str:
        if not self.config.narrator_mode:
            return ""
        space = "private chat" if event.launcher_type == "person" else "group chat"
        mood = self._mood_phrase(str(getattr(emotion, "primary", "") or "").strip().lower())
        detail = "concise" if int(self.config.narrator_detail_level) <= 1 else "textured"
        graph_summary = ""
        if isinstance(memory_graph, dict):
            highlights = memory_graph.get("highlights", [])
            if isinstance(highlights, list) and highlights:
                graph_summary = f" Recent context: {highlights[0]}"
        prompt = str(latest_message or "").strip()
        if len(prompt) > 42:
            prompt = prompt[:41].rstrip() + "…"
        return (
            f"Narrator cue ({self.config.narrator_style}, {detail}): the {space} feels {mood}; "
            f"anchor the reply to {address} and the latest thread “{prompt}”.{graph_summary}"
        )

    @staticmethod
    def _mood_phrase(emotion: str) -> str:
        mapping = {
            "joy": "light and bright",
            "love": "intimate and openly affectionate",
            "sadness": "soft and careful",
            "anger": "tense, so de-escalate",
            "anxiety": "hesitant and reassurance-seeking",
            "anticipation": "expectant and lively",
        }
        return mapping.get(emotion, "steady and attentive")
