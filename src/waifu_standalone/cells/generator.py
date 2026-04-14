from __future__ import annotations

from ..contracts import GeneratedImage
from ..models import EmotionState, InboundEvent


class Generator:
    """Minimal generator cell for text and image responses."""

    def generate_reply(self, event: InboundEvent, emotion: EmotionState) -> str:
        if "call me" in event.plain_text.lower():
            return f"{event.sender_name}, you can decide the nickname."
        return f"received. detected emotion: {emotion.primary}"

    def generate_image(self, prompt: str) -> GeneratedImage:
        cleaned = str(prompt or "").strip()
        if not cleaned:
            raise ValueError("image prompt is empty")
        return GeneratedImage(prompt=cleaned, image_ref=f"generated://{cleaned}")

    def generate_image_caption(self, prompt: str) -> str:
        return f"image ready: {prompt}"
