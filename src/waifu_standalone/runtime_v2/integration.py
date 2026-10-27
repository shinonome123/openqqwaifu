from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from pathlib import Path

from ..character.cards import CardManager
from ..conversation.composer import ReplyComposer
from ..config import AppConfig
from ..gateways.onebot_actions import OneBotActionRejected
from ..models import InboundEvent, OutboundMessage, SessionMemory
from ..runtime.testing import CapturingOutboundPort
from .contracts import CharacterSnapshot, DeliveryReceipt, DeliveryRejected, EventEnvelope, json_text
from .engine import ConversationEngine, RuntimeHost
from .store import RuntimeStore


def database_path(config: AppConfig, *, capture: bool) -> Path:
    filename = "runtime-v2-preview.sqlite3" if capture else "runtime-v2.sqlite3"
    return Path(config.data_root) / filename


def snapshot_character(config: AppConfig, character_id: str) -> CharacterSnapshot:
    manager = CardManager(config)
    if character_id not in {str(item["character"]) for item in manager.list_characters()}:
        raise ValueError("unknown character")
    session = SessionMemory(launcher_id="", launcher_type="person", character_id=character_id)
    return CharacterSnapshot.create(character_id, asdict(manager.load("person", session)))


class OutboundAdapter:
    def __init__(self, outbound, config: AppConfig, *, capture: bool):
        self.outbound, self.config, self.capture = outbound, config, capture
        self.account_id = config.bot_account_id
        self.initial_dry_run = config.qq_sidecar.dry_run

    def is_ready(self):
        check = getattr(self.outbound, "is_ready", None)
        return not callable(check) or check()

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        # A runtime restart applies connection settings. A live edit must never
        # turn a captured queue into a live send or bypass a new dry-run setting.
        if self.config.bot_account_id != self.account_id or self.config.qq_sidecar.dry_run != self.initial_dry_run:
            raise DeliveryRejected("connection identity/mode changed; restart required")
        send_async = getattr(self.outbound, "send_async", None)
        try:
            result = await send_async(message) if callable(send_async) else await asyncio.to_thread(self.outbound.send, message)
        except OneBotActionRejected as exc:
            raise DeliveryRejected("platform rejected the action") from exc
        if self.capture:
            return DeliveryReceipt("captured")
        if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode") != 0:
            raise RuntimeError("send result does not confirm platform acceptance")
        data = result.get("data")
        platform_id = str(data.get("message_id", "")) if isinstance(data, dict) else ""
        return DeliveryReceipt("accepted", platform_id)


class PrivateRuntime:
    def __init__(self, config: AppConfig, outbound, *, generator=None):
        self.config = config
        self.account_id = str(config.bot_account_id).strip()
        self.users = frozenset(config.runtime_v2.private_user_ids)
        self.structured_memory_enabled = config.runtime_v2.structured_memory_enabled
        if not self.account_id or not self.users or any(not isinstance(user, str) or not user.strip() for user in self.users):
            raise ValueError("runtime_v2 requires bot_account_id and nonempty private_user_ids")
        capture = isinstance(outbound, CapturingOutboundPort)
        self.store = RuntimeStore(database_path(config, capture=capture), self.account_id,
                                  "capture" if capture else "live", max_pending=config.runtime_v2.max_pending_events,
                                  reminders_enabled=config.runtime_v2.reminders_enabled,
                                  reminder_timezone=config.runtime_v2.reminder_timezone,
                                  reminder_grace_seconds=config.runtime_v2.reminder_grace_seconds,
                                  reminder_gate=lambda conversation_id: (config.runtime_v2.enabled is True
                                      and config.runtime_v2.reminders_enabled is True
                                      and config.bot_account_id == self.account_id
                                      and (conversation_id is None or conversation_id in config.runtime_v2.private_user_ids)),
                                  followup_gate=lambda conversation_id: (config.runtime_v2.enabled is True
                                      and config.bot_account_id == self.account_id
                                      and (conversation_id is None or conversation_id in config.runtime_v2.private_user_ids)))
        try:
            existing = {row["conversation_id"] for row in self.store.snapshot()["conversations"]}
            if not existing <= self.users:
                raise ValueError("removing v2 conversations requires an explicit data migration; keep their private_user_ids")
            self.initial_character = snapshot_character(config, CardManager(config).active_character())
            adapter = generator or ReplyComposer(config, capture=capture, context_observer=self.store.record_context)
            # The digest is an identifier only; credentials/config text are never
            # written to the runtime database or diagnostics.
            version = hashlib.sha256(json_text(asdict(config)).encode()).hexdigest()
            self.host = RuntimeHost(ConversationEngine(self.store, adapter,
                OutboundAdapter(outbound, config, capture=capture), version,
                history_limit=config.history_window_messages), workers=config.runtime_v2.workers)
        except BaseException:
            if "adapter" in locals():
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()
            self.store.close()
            raise

    def routes(self, event: InboundEvent) -> bool:
        return event.launcher_type == "person" and event.launcher_id in self.users

    def accept(self, event: InboundEvent):
        if not self.routes(event) or self.config.bot_account_id != self.account_id:
            raise ValueError("event does not belong to this runtime/account")
        envelope = EventEnvelope.from_inbound(self.account_id, event)
        return self.host.mutate(self.store.admit, envelope, self.initial_character)

    def bind(self, conversation_id: str, character_id: str, request_id: str):
        if conversation_id not in self.users:
            raise ValueError("conversation is not routed to runtime v2")
        character = snapshot_character(self.config, character_id)
        return self.host.mutate(self.store.enqueue_binding, conversation_id, character, request_id)

    def resolve(self, outbox_id: str, action: str, note: str, *, expected_attempts=None, expected_state=None):
        return self.host.mutate(self.store.resolve_delivery, outbox_id, action, note,
                                expected_attempts=expected_attempts, expected_state=expected_state)

    def change_memory(self, payload, actor):
        conversation_id = payload.get("conversation_id", "")
        if not isinstance(conversation_id, str) or conversation_id not in self.users:
            raise ValueError("conversation is not routed to runtime v2")
        return self.host.mutate(self.store.enqueue_memory_change, conversation_id, payload.get("character_id", ""),
            payload.get("source_event_id", ""), payload.get("change"), payload.get("request_id", ""), actor, payload.get("note", ""))

    def change_reminder(self, payload, actor):
        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, str) or conversation_id not in self.users:
            raise ValueError("conversation is not routed to runtime v2")
        return self.host.mutate(self.store.reminder_request, payload, actor)

    def change_privacy(self, payload, actor):
        cid = payload.get("conversation_id")
        if not isinstance(cid, str) or cid not in self.users:
            raise ValueError("conversation is not routed to runtime v2")
        if payload.get("operation") == "erase":
            return self.host.mutate(self.store.erase_conversation, payload, actor)
        if payload.get("operation") == "resume":
            character = snapshot_character(self.config, payload.get("character_id", ""))
            return self.host.mutate(self.store.resume_conversation, cid, payload.get("expected_generation"), character, actor)
        raise ValueError("unsupported privacy operation")

    def change_contact_policy(self, payload, actor):
        cid = payload.get("conversation_id")
        if not isinstance(cid, str) or cid not in self.users:
            raise ValueError("conversation is not routed to runtime v2")
        return self.host.mutate(self.store.contact_request, payload, actor)

    def change_followup(self, payload, actor):
        cid = payload.get("conversation_id")
        if not isinstance(cid, str) or cid not in self.users:
            raise ValueError("conversation is not routed to runtime v2")
        return self.host.mutate(self.store.followup_request, payload, actor)

    def change_episode_time(self, payload, actor):
        cid = payload.get("conversation_id")
        if not isinstance(cid, str) or cid not in self.users:
            raise ValueError("conversation is not routed to runtime v2")
        return self.host.mutate(self.store.episode_time_request, payload, actor)

    def close(self, timeout: float = 60):
        self.host.close(timeout)


def build_private_runtime(config: AppConfig, outbound):
    if config.runtime_v2.enabled:
        return PrivateRuntime(config, outbound)
    # Fail closed rather than silently sending an already-owned conversation
    # through the old engine after toggling the feature off.
    capture = isinstance(outbound, CapturingOutboundPort)
    existing = RuntimeStore.inspect(database_path(config, capture=capture))
    if existing["conversations"]:
        raise ValueError("runtime v2 owns existing conversations; re-enable it or migrate them before using legacy")
    return None
