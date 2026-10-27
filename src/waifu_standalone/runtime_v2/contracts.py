from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Protocol

from ..models import InboundEvent, MessageSegment, OutboundMessage


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class CharacterSnapshot:
    character_id: str
    version: str
    card_json: str

    @classmethod
    def create(cls, character_id: str, card: dict) -> CharacterSnapshot:
        if not character_id.strip():
            raise ValueError("character_id is required")
        serialized = json_text(card)
        return cls(character_id, hashlib.sha256(serialized.encode()).hexdigest(), serialized)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    bot_account_id: str
    conversation_id: str
    source_event_id: str
    received_at: float
    payload_json: str

    @classmethod
    def from_inbound(cls, account_id: str, event: InboundEvent) -> EventEnvelope:
        if event.launcher_type != "person":
            raise ValueError("runtime v2 currently supports private conversations only")
        if not account_id or not event.launcher_id or event.launcher_id != event.sender_id:
            raise ValueError("private conversation must identify its account and sender")
        if event.source_account_id and event.source_account_id != account_id:
            raise ValueError("OneBot source account does not match runtime account")
        return cls(uuid.uuid4().hex, account_id, event.launcher_id, str(event.message_id or "").strip(),
                   time.time(), json_text(asdict(event)))

    def inbound(self) -> InboundEvent:
        payload = json.loads(self.payload_json)
        payload["segments"] = [MessageSegment(**part) for part in payload["segments"]]
        return InboundEvent(**payload)


@dataclass(frozen=True, slots=True)
class ConversationBinding:
    bot_account_id: str
    conversation_id: str
    character: CharacterSnapshot
    generation: int


@dataclass(frozen=True, slots=True)
class TurnContext:
    turn_id: str
    event_id: str
    conversation_seq: int
    binding: ConversationBinding
    config_version: str
    memory_snapshot_json: str = "{}"
    data_generation: int = 1


@dataclass(frozen=True, slots=True)
class ReplyProposal:
    message: OutboundMessage
    memory_changes: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    role: str
    content: str
    event_id: str
    conversation_seq: int
    speaker_id: str = ""
    speaker_name: str = ""
    source_message_id: str = ""
    occurred_at: float | None = None
    received_at: float | None = None
    segments_json: str = "[]"
    conversation_id: str = ""
    character_id: str = ""
    delivery_state: str = ""
    event_kind: str = "message"


@dataclass(frozen=True, slots=True)
class Admission:
    event_id: str
    conversation_seq: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    status: str  # accepted (platform), or captured (simulation)
    platform_message_id: str = ""


class DeliveryRejected(RuntimeError):
    """Known to have failed BEFORE any external send, or explicitly rejected."""


class ReplyGenerator(Protocol):
    async def generate(self, context: TurnContext, event: InboundEvent,
                       history: list[HistoryMessage]) -> OutboundMessage | ReplyProposal | None: ...


class Sender(Protocol):
    async def send(self, message: OutboundMessage) -> DeliveryReceipt: ...
