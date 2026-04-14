from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


LauncherType = Literal["group", "person"]
MessageKind = Literal["text", "image"]


@dataclass(slots=True)
class MessageSegment:
    kind: MessageKind
    text: str = ""
    image_url: str = ""


@dataclass(slots=True)
class InboundEvent:
    launcher_id: str
    launcher_type: LauncherType
    sender_id: str
    sender_name: str
    segments: list[MessageSegment]

    @property
    def plain_text(self) -> str:
        return "".join(segment.text for segment in self.segments if segment.kind == "text").strip()

    @property
    def image_count(self) -> int:
        return sum(1 for segment in self.segments if segment.kind == "image")


@dataclass(slots=True)
class OutboundMessage:
    launcher_id: str
    launcher_type: LauncherType
    text: str
    images: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionMemory:
    launcher_id: str
    launcher_type: LauncherType
    history: list[str] = field(default_factory=list)
    preferred_name: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class EmotionState:
    primary: str = "neutral"
    intensity: float = 0.0
