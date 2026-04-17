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
import time
from typing import TYPE_CHECKING

from .models import EmotionState, InboundEvent, OutboundMessage
from .services import CapturingOutboundPort

if TYPE_CHECKING:
    from .app import WaifuService


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
        self.dispatch(message, delay_seconds=delay)
        self._service._record_outbound(message)
        self._service.memory.save_assistant_message(
            event.launcher_id,
            event.launcher_type,
            message.text,
            character_id=resolved_character_id,
        )
        self._service.state_store.mark_member_addressed(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=resolved_character_id,
        )
        self._service._archive_if_needed(
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
        )
        self._service.gate.refresh_follow_up_window(event)
        return message

    def delay_seconds(self, event: InboundEvent) -> float:
        if event.launcher_type != "group":
            return 0.0
        if type(self._service.outbound) is CapturingOutboundPort:
            return 0.0
        return max(0.0, float(self._service.config.group_response_delay_seconds))

    def dispatch(self, message: OutboundMessage, *, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            self._service.outbound.send(message)
            return
        send_async = getattr(self._service.outbound, "send_async", None)
        if callable(send_async):
            self._service._submit_background_coro(
                "delayed_outbound_send",
                self.adelayed_send(message, delay_seconds=delay_seconds),
            )
            return

        def delayed_send() -> None:
            time.sleep(delay_seconds)
            self._service.outbound.send(message)

        self._service._submit_background_task("delayed_outbound_send", delayed_send)

    async def adelayed_send(self, message: OutboundMessage, *, delay_seconds: float) -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        send_async = getattr(self._service.outbound, "send_async")
        await send_async(message)
