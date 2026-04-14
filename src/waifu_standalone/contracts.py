from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import EmotionState, InboundEvent, OutboundMessage, SessionMemory


class MemoryStore(Protocol):
    def load(self, launcher_id: str, launcher_type: str) -> SessionMemory:
        ...

    def save(self, session: SessionMemory) -> SessionMemory:
        ...

    def append(self, launcher_id: str, launcher_type: str, line: str) -> SessionMemory:
        ...


class EmotionAnalyzer(Protocol):
    def analyze(self, event: InboundEvent, memory: SessionMemory) -> EmotionState:
        ...


@dataclass(slots=True)
class GeneratedImage:
    prompt: str
    image_ref: str


class ImageGenerator(Protocol):
    def generate(self, prompt: str) -> GeneratedImage:
        ...


class OutboundPort(Protocol):
    def send(self, message: OutboundMessage) -> None:
        ...
