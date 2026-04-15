from __future__ import annotations

import re
from typing import Callable

from ..contracts import MemoryStore
from ..memory import HISTORY_LIMIT
from ..models import InboundEvent, SessionMemory


class Memory:
    """Session memory organ with a persistent store interface."""

    _PREFERRED_NAME_PATTERNS = (
        re.compile(r"(?:叫我|喊我|称呼我)([\u4e00-\u9fffA-Za-z0-9_]{1,12})"),
        re.compile(r"我是([\u4e00-\u9fffA-Za-z0-9_]{1,12})"),
        re.compile(r"call\s+me\s+([A-Za-z0-9_]{1,12})", flags=re.IGNORECASE),
    )
    _MAX_NAME_SCAN_CHARS = 2000

    def __init__(self, store: MemoryStore):
        self.store = store

    def load(self, launcher_id: str, launcher_type: str) -> SessionMemory:
        return self.store.load(launcher_id, launcher_type)

    def save_user_event(self, event: InboundEvent) -> SessionMemory:
        session = self.load(event.launcher_id, event.launcher_type)
        message_text = event.to_memory_text() or "[空消息]"
        session.history.append(f"{event.sender_name}: {message_text}")
        session.history = session.history[-HISTORY_LIMIT:]
        return self.store.save(session)

    def save_user_message(
        self,
        launcher_id: str,
        launcher_type: str,
        sender_name: str,
        content: str,
    ) -> SessionMemory:
        session = self.load(launcher_id, launcher_type)
        session.history.append(f"{sender_name}: {content}")
        session.history = session.history[-HISTORY_LIMIT:]
        return self.store.save(session)

    def save_assistant_message(self, launcher_id: str, launcher_type: str, content: str) -> SessionMemory:
        session = self.load(launcher_id, launcher_type)
        session.history.append(f"assistant: {content}")
        session.history = session.history[-HISTORY_LIMIT:]
        return self.store.save(session)

    def recent_history(self, launcher_id: str, launcher_type: str, limit: int = 6) -> list[str]:
        session = self.load(launcher_id, launcher_type)
        return session.history[-limit:]

    def format_dialogue(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        assistant_name: str,
        limit: int = 8,
    ) -> str:
        session = self.load(launcher_id, launcher_type)
        lines: list[str] = []
        for raw_line in session.history[-limit:]:
            speaker, content = self._split_history_line(raw_line)
            if speaker == "assistant":
                speaker = assistant_name
            lines.append(f"{speaker}：{content}")
        return "\n".join(lines)

    def maybe_archive_history(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        max_history_lines: int,
        batch_size: int,
        summarizer: Callable[[list[str]], tuple[str, list[str]]],
    ) -> dict[str, object] | None:
        session = self.load(launcher_id, launcher_type)
        if len(session.history) <= max_history_lines:
            return None

        archive_count = min(max(1, batch_size), max(1, len(session.history) - max_history_lines))
        history_batch = list(session.history[:archive_count])
        summary, tags = summarizer(history_batch)
        summary = str(summary or "").strip()
        if not summary:
            return None
        current = self.load(launcher_id, launcher_type)
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

    def extract_preferred_name(self, content: str) -> str:
        raw = str(content or "").strip()
        if len(raw) > self._MAX_NAME_SCAN_CHARS:
            return ""
        normalized = raw.replace(" ", "")
        for pattern in self._PREFERRED_NAME_PATTERNS:
            match = pattern.search(raw) or pattern.search(normalized)
            if not match:
                continue
            candidate = match.group(1).strip("，。！？,.!?:： ")
            if candidate and candidate not in {"什么", "名字", "昵称", "称呼"}:
                return candidate
        return ""

    @staticmethod
    def _split_history_line(line: str) -> tuple[str, str]:
        speaker, _, content = str(line or "").partition(": ")
        if _:
            return speaker.strip() or "user", content.strip()
        return "user", str(line or "").strip()
