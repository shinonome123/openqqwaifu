"""Reply-gating, follow-up window, pending-search and repeat detection.

Extracted from :mod:`waifu_standalone.app` so :class:`WaifuService` can stay
focused on orchestration. ``ReplyGate`` answers *"should we reply to this
inbound event, and if so in what mode?"* — it owns the group follow-up
window map, tracks pending-search confirmations via session metadata, and
detects repeated phrases that should trigger a short acknowledgement.

The gate shares ``_state_lock`` with the service: methods documented as
``*_locked`` assume the caller already holds it; all others acquire it
themselves.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from .models import InboundEvent, OutboundMessage, SessionMemory

if TYPE_CHECKING:
    from .app import WaifuService


FOLLOW_UP_METADATA_KEY = "follow_up_until"
PENDING_SEARCH_METADATA_KEY = "pending_search"
PENDING_SEARCH_TTL_SECONDS = 1800.0
LAST_SEARCH_TTL_SECONDS = 1800.0


def _safe_float(payload: dict[str, object], key: str, default: float) -> float:
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class ReplyGate:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        self._group_follow_up_until: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # Entry point: should we reply?
    # ------------------------------------------------------------------
    def should_reply(self, event: InboundEvent) -> bool:
        service = self._service
        if event.launcher_type != "group":
            return True
        bot_account_id = str(service.config.bot_account_id or "").strip()
        latest_message = (
            event.command_text(service.config.bot_account_id).strip()
            or event.to_memory_text()
        )
        if bot_account_id and event.has_bot_mention(bot_account_id):
            self.refresh_follow_up_window(event)
            return True
        if not service.config.group_reply_requires_mention:
            return True
        if not bot_account_id:
            return True
        if self.is_follow_up_window_active(event.launcher_id):
            return True
        if self._has_recent_search_link_follow_up(event):
            return True
        return self._has_pending_search_follow_up(event)

    # ------------------------------------------------------------------
    # Group follow-up window
    # ------------------------------------------------------------------
    def is_follow_up_window_active(self, launcher_id: str) -> bool:
        service = self._service
        key = (service._active_character_id(), launcher_id)
        with service._state_lock:
            deadline = self._group_follow_up_until.get(key, 0.0)
        if time.monotonic() <= deadline:
            return True
        session = service.memory.load(
            launcher_id,
            "group",
            character_id=service._active_character_id(),
        )
        return self._session_follow_up_until(session) >= time.time()

    def refresh_follow_up_window(self, event: InboundEvent) -> None:
        service = self._service
        if event.launcher_type != "group":
            return
        window = max(0.0, float(service.config.group_follow_up_window_seconds))
        if window <= 0:
            return
        deadline_monotonic = time.monotonic() + window
        deadline_wall = time.time() + window
        key = (service._active_character_id(), event.launcher_id)
        with service._state_lock:
            self._group_follow_up_until[key] = deadline_monotonic
        self._persist_follow_up_window(
            event.launcher_id,
            event.launcher_type,
            deadline_wall=deadline_wall,
            character_id=service._active_character_id(),
        )

    def active_followup_count_locked(
        self, character_id: str, *, now_monotonic: float
    ) -> int:
        """Count in-memory windows for ``character_id``. Caller holds ``_state_lock``."""
        return sum(
            1
            for (cid, _), until in self._group_follow_up_until.items()
            if cid == character_id and until > now_monotonic
        )

    def active_launcher_ids_locked(
        self, character_id: str, *, now_monotonic: float
    ) -> list[str]:
        """Sorted list of launcher IDs with live windows. Caller holds ``_state_lock``."""
        return sorted(
            launcher_id
            for (cid, launcher_id), until in self._group_follow_up_until.items()
            if cid == character_id and until > now_monotonic
        )

    def clear_all_windows_locked(self) -> None:
        """Drop every in-memory follow-up window. Caller holds ``_state_lock``."""
        self._group_follow_up_until.clear()

    def _persist_follow_up_window(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        deadline_wall: float,
        character_id: str,
    ) -> None:
        if launcher_type != "group":
            return
        session = self._service.memory.load(
            launcher_id, launcher_type, character_id=character_id
        )
        session.metadata[FOLLOW_UP_METADATA_KEY] = float(deadline_wall)
        self._service.memory.store.save(session)

    def _session_follow_up_until(self, session: SessionMemory) -> float:
        raw_value = session.metadata.get(FOLLOW_UP_METADATA_KEY)
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Pending-search follow-up
    # ------------------------------------------------------------------
    def pending_search_query_for_message(
        self, session: SessionMemory, latest_message: str
    ) -> str:
        pending = self._pending_search_payload(session)
        if not pending:
            return ""
        expires_at = _safe_float(pending, "expires_at", 0.0)
        if expires_at and expires_at < time.time():
            self.clear_pending_search(session)
            return ""
        if not self.looks_like_search_confirmation(latest_message):
            if not self.looks_like_search_clarification(latest_message):
                return ""
            base_query = str(pending.get("query", "") or "").strip()
            clarification = " ".join(str(latest_message or "").split()).strip()
            return " ".join(
                part for part in (base_query, clarification) if part
            ).strip()
        return str(pending.get("query", "") or "").strip()

    def recent_search_payload_for_message(
        self, session: SessionMemory, latest_message: str
    ) -> dict[str, object]:
        if not self.looks_like_search_link_request(latest_message):
            return {}
        return self._recent_search_payload(session)

    def store_pending_search(self, session: SessionMemory, *, query: str) -> None:
        cleaned_query = " ".join(str(query or "").split()).strip()
        if not cleaned_query:
            self.clear_pending_search(session)
            return
        session.metadata[PENDING_SEARCH_METADATA_KEY] = {
            "query": cleaned_query,
            "created_at": time.time(),
            "expires_at": time.time() + PENDING_SEARCH_TTL_SECONDS,
        }

    def clear_pending_search(self, session: SessionMemory) -> None:
        if PENDING_SEARCH_METADATA_KEY in session.metadata:
            session.metadata.pop(PENDING_SEARCH_METADATA_KEY, None)
            self._service.memory.store.save(session)

    def _has_pending_search_follow_up(self, event: InboundEvent) -> bool:
        service = self._service
        if event.launcher_type != "group":
            return False
        latest_message = (
            event.command_text(service.config.bot_account_id).strip()
            or event.to_memory_text()
        )
        session = service.memory.load(
            event.launcher_id,
            event.launcher_type,
            character_id=service._active_character_id(),
        )
        return bool(self.pending_search_query_for_message(session, latest_message))

    def _has_recent_search_link_follow_up(self, event: InboundEvent) -> bool:
        service = self._service
        if event.launcher_type != "group":
            return False
        latest_message = (
            event.command_text(service.config.bot_account_id).strip()
            or event.to_memory_text()
        )
        session = service.memory.load(
            event.launcher_id,
            event.launcher_type,
            character_id=service._active_character_id(),
        )
        return bool(self.recent_search_payload_for_message(session, latest_message))

    def _pending_search_payload(self, session: SessionMemory) -> dict[str, object]:
        raw_value = session.metadata.get(PENDING_SEARCH_METADATA_KEY, {})
        if isinstance(raw_value, dict) and str(raw_value.get("query", "") or "").strip():
            return raw_value
        legacy_search = session.metadata.get("last_search", {})
        if not isinstance(legacy_search, dict):
            return {}
        query = str(legacy_search.get("query", "") or "").strip()
        results = legacy_search.get("results", [])
        fetched_at = _safe_float(legacy_search, "fetched_at", 0.0)
        if not query or results:
            return {}
        if fetched_at and time.time() - fetched_at > PENDING_SEARCH_TTL_SECONDS:
            return {}
        return {
            "query": query,
            "created_at": fetched_at or time.time(),
            "expires_at": (fetched_at or time.time()) + PENDING_SEARCH_TTL_SECONDS,
        }

    def _recent_search_payload(self, session: SessionMemory) -> dict[str, object]:
        raw_value = session.metadata.get("last_search", {})
        if not isinstance(raw_value, dict):
            return {}
        query = str(raw_value.get("query", "") or "").strip()
        fetched_at = _safe_float(raw_value, "fetched_at", 0.0)
        if not query:
            return {}
        if fetched_at and time.time() - fetched_at > LAST_SEARCH_TTL_SECONDS:
            return {}
        return raw_value

    @staticmethod
    def looks_like_search_confirmation(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not compact:
            return False
        patterns = (
            "好的你帮我查查吧",
            "好你帮我查查吧",
            "帮我查查吧",
            "你帮我查查吧",
            "帮我查一下吧",
            "你帮我查一下吧",
            "那你帮我查查吧",
            "那你帮我查一下吧",
            "查查吧",
            "查一下吧",
            "去查查吧",
            "去查一下吧",
            "帮我查查",
            "帮我查一下",
        )
        if any(pattern in compact for pattern in patterns):
            return True
        short_affirmations = {
            "好的",
            "好",
            "行",
            "行吧",
            "嗯",
            "嗯嗯",
            "可以",
            "好呀",
            "好哦",
        }
        return compact in short_affirmations

    @staticmethod
    def looks_like_search_clarification(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not compact or len(compact) > 24:
            return False
        blockers = ("怎么", "为什么", "查不了", "不查", "不用查", "别查", "不是", "我不是")
        if any(blocker in compact for blocker in blockers):
            return False
        markers = ("公司", "集团", "股价", "股票", "港股", "美股", "a股", "hk", "us", "sz", "sh")
        if any(marker in compact for marker in markers):
            return True
        if re.search(r"[a-z]{2,}|\d{3,}", compact):
            return True
        cjk_chars = [char for char in compact if "\u4e00" <= char <= "\u9fff"]
        return 2 <= len(cjk_chars) <= 8

    @staticmethod
    def looks_like_search_link_request(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not compact or len(compact) > 48:
            return False
        if any(token in compact for token in ("url", "source")):
            return True
        markers = ("链接", "原文", "来源", "出处", "网址", "官网", "公告")
        if not any(marker in compact for marker in markers):
            return False
        explicit_patterns = (
            "把链接发给我",
            "把链接发我",
            "把原文发给我",
            "把原文发我",
            "把来源发给我",
            "把来源发我",
            "把网址发给我",
            "把网址发我",
            "链接发给我",
            "链接发我",
            "给我链接",
            "给下链接",
            "发下链接",
            "发我链接",
            "原文发给我",
            "原文发我",
            "给我原文",
            "来源发给我",
            "来源发我",
            "给我来源",
            "原文呢",
            "来源呢",
            "链接呢",
            "网址呢",
            "官网链接",
            "公告链接",
        )
        if any(pattern in compact for pattern in explicit_patterns):
            return True
        if compact in {"链接", "原文", "来源", "出处", "网址", "官网", "公告"}:
            return True
        return any(marker in compact for marker in markers) and any(
            verb in compact for verb in ("发", "给", "贴", "甩", "来")
        )

    # ------------------------------------------------------------------
    # Repeat detection
    # ------------------------------------------------------------------
    def build_repeat_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        address: str,
    ) -> OutboundMessage | None:
        service = self._service
        threshold = max(0, int(service.config.repeat_trigger_count))
        if threshold <= 0 or event.launcher_type != "group":
            return None
        normalized = service._normalize_repeat_text(
            event.command_text(service.config.bot_account_id)
        )
        if not normalized:
            return None
        repeat_count = self._recent_repeat_count(
            session, normalized, sender_name=event.sender_name
        )
        if repeat_count != threshold:
            return None
        return OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=f"嗯，{address}，这句话你已经重复{repeat_count}次了，我听到了。",
        )

    def _recent_repeat_count(
        self, session: SessionMemory, target: str, *, sender_name: str
    ) -> int:
        service = self._service
        count = 0
        for raw_line in reversed(session.history):
            speaker, content = service._split_history_line(raw_line)
            if speaker == "assistant":
                continue
            if speaker != sender_name:
                break
            normalized = service._normalize_repeat_text(content)
            if not normalized:
                if count:
                    break
                continue
            if normalized != target:
                break
            count += 1
        return count
