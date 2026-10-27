"""Short-term de-duplication of inbound OneBot message events.

NapCat (or a proxy in front of us) may deliver the same message twice: the HTTP
callback is retried after a timeout while the first delivery is still waiting on
the model, or a reverse-WS reconnect replays recent events. Each copy would get
its own reply. Both entry points go through ``HttpApi.handle_json[_async]``,
which asks this class before handing the event to the service.

- With a ``message_id``: a key per (chat, message_id), kept for ``id_ttl_seconds``.
- Without one: a key per (chat, sender, content), kept for
  ``content_window_seconds``. When the payload carries OneBot's ``time`` it is part
  of the key, so a retry matches for the longer id TTL while the same text sent
  again a moment later does not.

A claim is released when handling fails, so the retry is answered. Once the
reply has gone out (``mark_delivered``) the claim stays even if later
bookkeeping fails, so a retry does not send a second reply.

State is in memory only; a restart forgets it.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Callable

from ..models import InboundEvent

DEFAULT_ID_TTL_SECONDS = 600.0
DEFAULT_CONTENT_WINDOW_SECONDS = 10.0
DEFAULT_MAX_ENTRIES = 4096


class InboundDeduplicator:
    def __init__(
        self,
        *,
        id_ttl_seconds: float = DEFAULT_ID_TTL_SECONDS,
        content_window_seconds: float = DEFAULT_CONTENT_WINDOW_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._id_ttl = float(id_ttl_seconds)
        self._content_window = float(content_window_seconds)
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._expires_at: OrderedDict[str, float] = OrderedDict()
        self._delivered: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, event: InboundEvent, *, event_time: object = None) -> str | None:
        """Record the event and return its key, or ``None`` if it was seen recently."""
        key, ttl = self._key_for(event, event_time)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            expires_at = self._expires_at.get(key)
            if expires_at is not None and expires_at > now:
                return None
            self._expires_at[key] = now + ttl
            self._expires_at.move_to_end(key)
            while len(self._expires_at) > self._max_entries:
                evicted, _ = self._expires_at.popitem(last=False)
                self._delivered.discard(evicted)
        return key

    def mark_delivered(self, event: InboundEvent, *, event_time: object = None) -> None:
        """Note that a reply to this event was sent, so ``release`` keeps its claim."""
        key, _ = self._key_for(event, event_time)
        with self._lock:
            if key in self._expires_at:
                self._delivered.add(key)

    def release(self, key: str | None) -> None:
        """Forget a claimed event, so a retry after a failed delivery is handled."""
        if not key:
            return
        with self._lock:
            if key in self._delivered:
                return
            self._expires_at.pop(key, None)

    def _key_for(self, event: InboundEvent, event_time: object) -> tuple[str, float]:
        chat = f"{event.launcher_type}:{event.launcher_id}"
        message_id = str(event.message_id or "").strip()
        if message_id:
            return f"id|{chat}|{message_id}", self._id_ttl
        content = str(event.raw_message or "").strip() or "|".join(
            f"{segment.kind}:{segment.text}:{segment.image_url}:{segment.mention_target}"
            for segment in event.segments
        )
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        if event_time is None:
            event_time = event.occurred_at
        stamp = str(event_time or "").strip()
        ttl = self._id_ttl if stamp else self._content_window
        return f"content|{chat}|{event.sender_id}|{stamp}|{digest}", ttl

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, expires_at in self._expires_at.items() if expires_at <= now]
        for key in expired:
            del self._expires_at[key]
            self._delivered.discard(key)
