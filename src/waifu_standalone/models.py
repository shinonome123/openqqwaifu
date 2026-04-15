from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


LauncherType = Literal["group", "person"]
MessageKind = Literal["text", "image", "mention"]


@dataclass(slots=True)
class MessageSegment:
    kind: MessageKind
    text: str = ""
    image_url: str = ""
    image_base64: str = ""
    mention_target: str = ""
    mention_display: str = ""


@dataclass(slots=True)
class InboundEvent:
    launcher_id: str
    launcher_type: LauncherType
    sender_id: str
    sender_name: str
    segments: list[MessageSegment]
    message_id: str = ""
    raw_message: str = ""

    @property
    def plain_text(self) -> str:
        return "".join(segment.text for segment in self.segments if segment.kind == "text").strip()

    @property
    def image_count(self) -> int:
        return sum(1 for segment in self.segments if segment.kind == "image")

    @property
    def mentioned_targets(self) -> list[str]:
        return [
            segment.mention_target.strip()
            for segment in self.segments
            if segment.kind == "mention" and segment.mention_target.strip()
        ]

    def has_bot_mention(self, bot_account_id: str) -> bool:
        target = str(bot_account_id or "").strip()
        if not target:
            return False
        return target in self.mentioned_targets

    def command_text(self, bot_account_id: str = "") -> str:
        target = str(bot_account_id or "").strip()
        parts: list[str] = []
        for segment in self.segments:
            if segment.kind == "text" and segment.text:
                parts.append(segment.text)
            elif segment.kind == "mention":
                mention_target = segment.mention_target.strip()
                if target and mention_target == target:
                    continue
                display = segment.mention_display.strip() or mention_target
                if display:
                    parts.append(f"@{display}")
        return "".join(parts).strip()

    def to_memory_text(self) -> str:
        has_images = self.image_count > 0
        parts: list[str] = []
        for segment in self.segments:
            if segment.kind == "text" and segment.text:
                parts.append(segment.text)
            elif segment.kind == "image":
                continue
            elif segment.kind == "mention" and not has_images:
                display = segment.mention_display.strip() or segment.mention_target.strip()
                if display:
                    parts.append(f"@{display}")
        text = "".join(parts).strip()
        if not has_images:
            return text
        count = self.image_count
        image_label = "发送了图片" if count == 1 else f"发送了{count}张图片"
        if text:
            return f"{image_label}，并且说：“{text}”。"
        return f"{image_label}。"

    def image_payloads(self) -> list[str]:
        payloads: list[str] = []
        for segment in self.segments:
            if segment.kind != "image":
                continue
            payload = segment.image_base64.strip() or segment.image_url.strip()
            if payload:
                payloads.append(payload)
        return payloads


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
    character_id: str = ""
    history: list[str] = field(default_factory=list)
    preferred_name: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class EmotionState:
    primary: str = "neutral"
    intensity: float = 0.0
