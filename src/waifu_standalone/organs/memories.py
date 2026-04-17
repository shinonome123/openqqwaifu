from __future__ import annotations

import asyncio
import re
from typing import Callable

from ..contracts import MemoryStore
from ..memory import HISTORY_LIMIT
from ..models import InboundEvent, SessionMemory


class Memory:
    """Session memory organ backed by a persistent store."""

    _PREFERRED_NAME_PATTERNS = (
        re.compile(
            r"(?:叫我|喊我|称呼我|请叫我|可以叫我|你可以叫我|你就叫我)\s*([^\s,，。！？!?:：]{1,12})"
        ),
        re.compile(r"(?:我的名字是|我叫|我是)\s*([^\s,，。！？!?:：]{1,12})"),
        re.compile(r"(?:call\s+me|my\s+name\s+is|i\s*am|i'm)\s+([A-Za-z0-9_]{1,12})", flags=re.IGNORECASE),
    )
    _PREFERRED_NAME_BLACKLIST = {
        "我",
        "你",
        "名字",
        "姓名",
        "称呼",
        "昵称",
        "一下",
        "一个",
    }
    _MAX_NAME_SCAN_CHARS = 2000

    def __init__(self, store: MemoryStore, character_resolver: Callable[[], str] | None = None):
        self.store = store
        self._character_resolver = character_resolver

    def set_character_resolver(self, resolver: Callable[[], str] | None) -> None:
        self._character_resolver = resolver

    def load(self, launcher_id: str, launcher_type: str, *, character_id: str = "") -> SessionMemory:
        resolved_character_id = self._resolve_character_id(character_id)
        session = self.store.load(launcher_id, launcher_type, character_id=resolved_character_id)
        if not session.character_id:
            session.character_id = resolved_character_id
        return session

    async def aload(self, launcher_id: str, launcher_type: str, *, character_id: str = "") -> SessionMemory:
        return await asyncio.to_thread(self.load, launcher_id, launcher_type, character_id=character_id)

    def save_user_event(self, event: InboundEvent, *, character_id: str = "") -> SessionMemory:
        session = self.load(event.launcher_id, event.launcher_type, character_id=character_id)
        message_text = event.to_memory_text() or "[non-text event]"
        session.history.append(f"{event.sender_name}: {message_text}")
        session.history = session.history[-HISTORY_LIMIT:]
        return self.store.save(session)

    async def asave_user_event(self, event: InboundEvent, *, character_id: str = "") -> SessionMemory:
        return await asyncio.to_thread(self.save_user_event, event, character_id=character_id)

    def save_user_message(
        self,
        launcher_id: str,
        launcher_type: str,
        sender_name: str,
        content: str,
        *,
        character_id: str = "",
    ) -> SessionMemory:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        session.history.append(f"{sender_name}: {content}")
        session.history = session.history[-HISTORY_LIMIT:]
        return self.store.save(session)

    def save_assistant_message(
        self,
        launcher_id: str,
        launcher_type: str,
        content: str,
        *,
        character_id: str = "",
    ) -> SessionMemory:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        session.history.append(f"assistant: {content}")
        session.history = session.history[-HISTORY_LIMIT:]
        return self.store.save(session)

    async def asave_assistant_message(
        self,
        launcher_id: str,
        launcher_type: str,
        content: str,
        *,
        character_id: str = "",
    ) -> SessionMemory:
        return await asyncio.to_thread(
            self.save_assistant_message,
            launcher_id,
            launcher_type,
            content,
            character_id=character_id,
        )

    def recent_history(
        self,
        launcher_id: str,
        launcher_type: str,
        limit: int = 6,
        *,
        character_id: str = "",
    ) -> list[str]:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        return session.history[-limit:]

    def format_dialogue(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        assistant_name: str,
        limit: int = 8,
        character_id: str = "",
    ) -> str:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        lines: list[str] = []
        for raw_line in session.history[-limit:]:
            speaker, content = self._split_history_line(raw_line)
            if speaker == "assistant":
                speaker = assistant_name
            lines.append(f"{speaker}: {content}")
        return "\n".join(lines)

    async def aformat_dialogue(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        assistant_name: str,
        limit: int = 8,
        character_id: str = "",
    ) -> str:
        return await asyncio.to_thread(
            self.format_dialogue,
            launcher_id,
            launcher_type,
            assistant_name=assistant_name,
            limit=limit,
            character_id=character_id,
        )

    def maybe_archive_history(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        max_history_lines: int,
        batch_size: int,
        summarizer: Callable[[list[str]], tuple[str, list[str]]],
        character_id: str = "",
    ) -> dict[str, object] | None:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        if len(session.history) <= max_history_lines:
            return None

        archive_count = min(max(1, batch_size), max(1, len(session.history) - max_history_lines))
        history_batch = list(session.history[:archive_count])
        summary, tags = summarizer(history_batch)
        summary = str(summary or "").strip()
        if not summary:
            return None

        current = self.load(launcher_id, launcher_type, character_id=character_id)
        if len(current.history) < archive_count:
            return None
        if list(current.history[:archive_count]) != history_batch:
            return None

        current.history = current.history[archive_count:]
        self.store.save(current)
        return {
            "summary": summary,
            "tags": [tag for tag in tags if str(tag).strip()],
            "archive_count": archive_count,
            "source": "archived_history",
        }

    async def amaybe_archive_history(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        max_history_lines: int,
        batch_size: int,
        summarizer: Callable[[list[str]], tuple[str, list[str]]],
        character_id: str = "",
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(
            self.maybe_archive_history,
            launcher_id,
            launcher_type,
            max_history_lines=max_history_lines,
            batch_size=batch_size,
            summarizer=summarizer,
            character_id=character_id,
        )

    def extract_preferred_name(self, content: str) -> str:
        raw = str(content or "").strip()
        if len(raw) > self._MAX_NAME_SCAN_CHARS:
            return ""
        normalized = raw.replace(" ", "")
        for pattern in self._PREFERRED_NAME_PATTERNS:
            match = pattern.search(raw) or pattern.search(normalized)
            if not match:
                continue
            candidate = match.group(1).strip(" \t\r\n,.!?，。！？:：\"'`()[]{}<>")
            if candidate and candidate not in self._PREFERRED_NAME_BLACKLIST:
                return candidate
        return ""

    @staticmethod
    def _split_history_line(line: str) -> tuple[str, str]:
        speaker, _, content = str(line or "").partition(": ")
        if _:
            return speaker.strip() or "user", content.strip()
        return "user", str(line or "").strip()

    def _resolve_character_id(self, explicit: str = "") -> str:
        resolved = str(explicit or "").strip()
        if resolved:
            return resolved
        if self._character_resolver is None:
            return ""
        return str(self._character_resolver() or "").strip()
