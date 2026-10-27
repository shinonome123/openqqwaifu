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
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from ..models import EmotionState, InboundEvent, OutboundMessage
from ..runtime.testing import CapturingOutboundPort

if TYPE_CHECKING:
    from ..app import WaifuService


_SENTENCE_END = re.compile(r"[。！？!?；;…]")


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
        chunks = split_reply_text(message.text, int(self._service.config.outbound_message_max_chars or 0))
        if len(chunks) <= 1:
            return [message]
        # Images go with the last chunk, after the text that introduces them.
        return [
            replace(message, text=chunk, images=list(message.images) if index == len(chunks) - 1 else [])
            for index, chunk in enumerate(chunks)
        ]

    def _send_parts(self, message: OutboundMessage) -> None:
        for part in self.message_parts(message):
            self._service.outbound.send(part)

    async def _asend_parts(self, message: OutboundMessage) -> None:
        send_async = getattr(self._service.outbound, "send_async", None)
        for part in self.message_parts(message):
            if callable(send_async):
                await send_async(part)
            else:
                await asyncio.to_thread(self._service.outbound.send, part)

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
