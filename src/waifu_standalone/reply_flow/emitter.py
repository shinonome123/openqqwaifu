"""Outbound message emission with optional delay.

Extracted from :mod:`waifu_standalone.app` so ``WaifuService`` can
delegate the hand-off from *we have an OutboundMessage* to *it has been
sent, persisted, recorded and its side-effects applied*.

The emitter borrows ``outbound``, ``memory``, ``state_store``,
``event_engine``, ``value_game`` and the follow-up window refresher on
``gate`` from the owning service rather than copying state.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from ..models import EmotionState, InboundEvent, OutboundMessage
from ..runtime.testing import CapturingOutboundPort
from .stickers import strip_sticker_tags

if TYPE_CHECKING:
    from ..app import WaifuService


_SENTENCE_END = re.compile(r"[。！？!?；;…]")
# A chat sentence: up to its end mark (plus closing quotes) or a line break. "…" is
# left alone so a "……" opening stays with the sentence it opens.
_CHAT_SENTENCE = re.compile(r"[^。！？!?~～\n]*(?:[。！？!?~～]+[”’」』）)]*|\n|$)")
# Pieces shorter than this join their neighbour ("在。" is not a message of its own).
_MIN_CHAT_PIECE_CHARS = 4
# A list item ("1. ", "2、", "- ", "• "): a list goes out as one message.
_LIST_LINE = re.compile(r"^\s*(?:[-*•·]\s|\d{1,2}[.、)）]|[（(]\d{1,2}[)）])", re.MULTILINE)


def conversational_chunks(text: str, *, max_parts: int, max_total_chars: int) -> list[str]:
    """Split a short reply into the few messages a person would send.

    Long replies (a story, search results), lists and code blocks stay whole.
    """
    remaining = str(text or "").strip()
    if not remaining:
        return []
    if max_parts < 2 or len(remaining) > max_total_chars or "```" in remaining or _LIST_LINE.search(remaining):
        return [remaining]
    # (sentence, whether a line break ended it): joined pieces keep that line break.
    pieces: list[tuple[str, bool]] = []
    for match in _CHAT_SENTENCE.findall(remaining):
        sentence = match.strip()
        if not sentence:
            continue
        if pieces and min(len(sentence), len(pieces[-1][0])) < _MIN_CHAT_PIECE_CHARS:
            previous, line_break = pieces.pop()
            sentence = previous + ("\n" if line_break else "") + sentence
        pieces.append((sentence, match.endswith("\n")))
    while len(pieces) > max_parts:
        last, last_break = pieces.pop()
        previous, line_break = pieces.pop()
        pieces.append((previous + ("\n" if line_break else "") + last, last_break))
    return [sentence for sentence, _ in pieces] or [remaining]


def split_reply_text(text: str, limit: int) -> list[str]:
    """Split a long reply into chunks of at most ``limit`` chars for separate QQ messages.

    Prefers a blank line, then a line break, then a sentence end, as long as the
    chunk keeps at least half the limit; otherwise cuts hard.
    """
    remaining = str(text or "").strip()
    if limit <= 0 or len(remaining) <= limit:
        return [remaining] if remaining else []
    chunks: list[str] = []
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            ends = [match.end() for match in _SENTENCE_END.finditer(window)]
            cut = ends[-1] if ends else -1
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


class OutboundEmitter:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        # Group id -> id of the newest message seen there, to tell when a reply needs a quote.
        self._latest_group_message: dict[str, str] = {}

    def emit(
        self,
        event: InboundEvent,
        message: OutboundMessage,
        *,
        assistant_name: str,
        emotion: EmotionState | None = None,
        search_used: bool = False,
        behavior_reason: str = "",
        character_id: str = "",
    ) -> OutboundMessage:
        resolved_character_id = str(character_id or self._service._active_character_id()).strip()
        delay = self.delay_seconds(event)
        if delay > 0:
            self._service._submit_background_coro("delayed_outbound_send", self._deliver_after_delay(
                event, message, delay_seconds=delay, assistant_name=assistant_name,
                emotion=emotion, search_used=search_used, behavior_reason=behavior_reason,
                character_id=resolved_character_id,
            ))
            return message
        message = self._with_quote(event, message)
        self.dispatch(message, delay_seconds=delay)
        self._service._mark_inbound_delivered(event)
        self._service._record_outbound(message)
        self._service.memory.save_assistant_message(
            event.launcher_id,
            event.launcher_type,
            message.text,
            character_id=resolved_character_id,
        )
        self._service.member_repository.mark_member_addressed(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=resolved_character_id,
        )
        self._service._schedule_archive(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            character_id=resolved_character_id,
        )
        self._service._record_behavior_event(
            self._service.event_engine.capture_outbound(
                event=event,
                message=message,
                reason=behavior_reason or "reply",
                character_id=resolved_character_id,
            )
        )
        self._service.value_game.apply(
            state_store=self._service.state_store,
            event=event,
            emotion=emotion or EmotionState(),
            reply_text=message.text,
            search_used=search_used,
            character_id=resolved_character_id,
        )
        self._service.gate.refresh_follow_up_window(event)
        self._service.listener.mark_answered(event, message)
        return message

    async def aemit(
        self,
        event: InboundEvent,
        message: OutboundMessage,
        *,
        assistant_name: str,
        emotion: EmotionState | None = None,
        search_used: bool = False,
        behavior_reason: str = "",
        character_id: str = "",
        _skip_delay: bool = False,
    ) -> OutboundMessage:
        resolved_character_id = str(character_id or self._service._active_character_id()).strip()
        delay = 0.0 if _skip_delay else self.delay_seconds(event)
        if delay > 0:
            task = asyncio.create_task(self._deliver_after_delay(
                event, message, delay_seconds=delay, assistant_name=assistant_name,
                emotion=emotion, search_used=search_used, behavior_reason=behavior_reason,
                character_id=resolved_character_id,
            ))
            self._service._track_async_task("delayed_outbound_send", task)
            return message
        message = self._with_quote(event, message)
        await self.adispatch(message, delay_seconds=delay)
        self._service._mark_inbound_delivered(event)
        self._service._record_outbound(message)
        await self._service.memory.asave_assistant_message(
            event.launcher_id,
            event.launcher_type,
            message.text,
            character_id=resolved_character_id,
        )
        await asyncio.to_thread(
            self._service.member_repository.mark_member_addressed,
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=resolved_character_id,
        )
        self._service._schedule_archive(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            character_id=resolved_character_id,
        )
        self._service._record_behavior_event(
            self._service.event_engine.capture_outbound(
                event=event,
                message=message,
                reason=behavior_reason or "reply",
                character_id=resolved_character_id,
            )
        )
        await asyncio.to_thread(
            self._service.value_game.apply,
            state_store=self._service.state_store,
            event=event,
            emotion=emotion or EmotionState(),
            reply_text=message.text,
            search_used=search_used,
            character_id=resolved_character_id,
        )
        self._service.gate.refresh_follow_up_window(event)
        self._service.listener.mark_answered(event, message)
        return message

    async def _deliver_after_delay(self, event: InboundEvent, message: OutboundMessage, *, delay_seconds: float, **kwargs) -> None:
        await asyncio.sleep(delay_seconds)
        lock = self._service._session_lock_for(event)
        await lock.acquire()
        try:
            operation = asyncio.create_task(self.aemit(event, message, _skip_delay=True, **kwargs))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(operation)
                    break
                except asyncio.CancelledError:
                    if operation.cancelled():
                        raise
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        finally:
            lock.release()

    def delay_seconds(self, event: InboundEvent) -> float:
        if event.launcher_type != "group":
            return 0.0
        if type(self._service.outbound) is CapturingOutboundPort:
            return 0.0
        return max(0.0, float(self._service.config.group_response_delay_seconds))

    def message_parts(self, message: OutboundMessage) -> list[OutboundMessage]:
        text = strip_sticker_tags(message.text)
        segmented = self._service.config.segmented_reply
        chunks: list[str] = []
        if segmented.enabled and message.conversational:
            chunks = conversational_chunks(
                text,
                max_parts=int(segmented.max_parts or 0),
                max_total_chars=int(segmented.max_total_chars or 0),
            )
        if len(chunks) <= 1:
            chunks = split_reply_text(text, int(self._service.config.outbound_message_max_chars or 0))
        if not chunks and (message.images or not message.sticker):
            chunks = [""]
        last = len(chunks) - 1
        # Images go with the last chunk, after the text that introduces them; a quote
        # goes on the first message, and a sticker follows on its own.
        parts = [
            replace(
                message,
                text=chunk,
                images=list(message.images) if index == last else [],
                reply_to=message.reply_to if index == 0 else "",
                sticker="",
            )
            for index, chunk in enumerate(chunks)
        ]
        if message.sticker:
            parts.append(
                OutboundMessage(
                    launcher_id=message.launcher_id,
                    launcher_type=message.launcher_type,
                    text="",
                    reply_to="" if parts else message.reply_to,
                    sticker=message.sticker,
                )
            )
        return parts

    def _pause_between_parts(self) -> float:
        """Seconds to wait before the next part; none when replies are only captured."""
        if type(self._service.outbound) is CapturingOutboundPort:
            return 0.0
        segmented = self._service.config.segmented_reply
        low = max(0.0, float(segmented.min_delay_seconds or 0.0))
        high = max(low, float(segmented.max_delay_seconds or 0.0))
        return random.uniform(low, high) if high > 0 else 0.0

    def _send_parts(self, message: OutboundMessage) -> None:
        for index, part in enumerate(self.message_parts(message)):
            if index:
                time.sleep(self._pause_between_parts())
            self._service.outbound.send(part)

    async def _asend_parts(self, message: OutboundMessage) -> None:
        send_async = getattr(self._service.outbound, "send_async", None)
        for index, part in enumerate(self.message_parts(message)):
            if index:
                await asyncio.sleep(self._pause_between_parts())
            if callable(send_async):
                await send_async(part)
            else:
                await asyncio.to_thread(self._service.outbound.send, part)

    def note_inbound(self, event: InboundEvent) -> None:
        """Remember the newest message in a group; called for every inbound message."""
        message_id = str(event.message_id or "").strip()
        if event.launcher_type == "group" and message_id:
            self._latest_group_message[str(event.launcher_id)] = message_id

    def superseded(self, event: InboundEvent) -> bool:
        """Whether a newer message has arrived in this group since ``event``."""
        message_id = str(event.message_id or "").strip()
        newest = self._latest_group_message.get(str(event.launcher_id), "")
        return bool(event.launcher_type == "group" and message_id and newest and newest != message_id)

    def _with_quote(self, event: InboundEvent, message: OutboundMessage) -> OutboundMessage:
        """Quote the message being answered when others have spoken since, as people do."""
        message_id = str(event.message_id or "").strip()
        if (
            event.launcher_type != "group"
            or not self._service.config.group_reply_quote
            or not message_id
            or message.reply_to
            or self._latest_group_message.get(str(event.launcher_id), message_id) == message_id
        ):
            return message
        return replace(message, reply_to=message_id)

    def dispatch(self, message: OutboundMessage, *, delay_seconds: float) -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        self._send_parts(message)

    async def adispatch(self, message: OutboundMessage, *, delay_seconds: float) -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self._asend_parts(message)

    async def adelayed_send(self, message: OutboundMessage, *, delay_seconds: float) -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self._asend_parts(message)
