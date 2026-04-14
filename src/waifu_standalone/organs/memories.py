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
        self._remember_group_member(session, event.sender_id, event.sender_name, message_text)
        preferred_name = self.extract_preferred_name(message_text)
        if preferred_name:
            session.preferred_name = preferred_name
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
        preferred_name = self.extract_preferred_name(content)
        if preferred_name:
            session.preferred_name = preferred_name
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

    def recall_long_term_memories(
        self,
        launcher_id: str,
        launcher_type: str,
        query: str,
        *,
        limit: int = 3,
    ) -> list[str]:
        session = self.load(launcher_id, launcher_type)
        memories = session.metadata.get("long_term_memory", [])
        if not isinstance(memories, list):
            return []

        terms = self._extract_terms(query)
        scored: list[tuple[float, str]] = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary", "") or "").strip()
            tags = [str(tag).strip() for tag in item.get("tags", []) if str(tag).strip()] if isinstance(item.get("tags"), list) else []
            score = self._score_memory(summary, tags, terms)
            if score <= 0:
                continue
            scored.append((score, summary))

        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [summary for _, summary in scored[:limit]]

    def maybe_archive_history(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        max_history_lines: int,
        batch_size: int,
        summarizer: Callable[[list[str]], tuple[str, list[str]]],
    ) -> SessionMemory | None:
        session = self.load(launcher_id, launcher_type)
        if len(session.history) <= max_history_lines:
            return None

        archive_count = min(max(1, batch_size), max(1, len(session.history) - max_history_lines))
        history_batch = session.history[:archive_count]
        summary, tags = summarizer(history_batch)
        summary = str(summary or "").strip()
        if not summary:
            return None

        raw_long_term = session.metadata.get("long_term_memory", [])
        long_term = list(raw_long_term) if isinstance(raw_long_term, list) else []
        long_term.append(
            {
                "summary": summary,
                "tags": [tag for tag in tags if str(tag).strip()],
                "source": "archived_history",
            }
        )
        session.metadata["long_term_memory"] = long_term
        session.history = session.history[archive_count:]
        return self.store.save(session)

    def group_member_notes(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        active_sender_id: str,
        limit: int = 4,
    ) -> list[str]:
        session = self.load(launcher_id, launcher_type)
        group_members = session.metadata.get("group_members", {})
        if not isinstance(group_members, dict):
            return []

        notes: list[str] = []
        for sender_id, profile in group_members.items():
            if len(notes) >= limit:
                break
            if not isinstance(profile, dict):
                continue
            sender_name = str(profile.get("sender_name", "") or "").strip() or str(sender_id)
            preferred_name = str(profile.get("preferred_name", "") or "").strip()
            if sender_id == active_sender_id and preferred_name:
                notes.append(f"{sender_name}希望被称呼为{preferred_name}。")
            elif sender_id == active_sender_id:
                notes.append(f"当前说话的人是{sender_name}。")
            elif preferred_name:
                notes.append(f"{sender_name}常用称呼是{preferred_name}。")
        return notes

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

    def group_member_hint(self, launcher_id: str, launcher_type: str, sender_id: str) -> str:
        session = self.load(launcher_id, launcher_type)
        group_members = session.metadata.get("group_members", {})
        if not isinstance(group_members, dict):
            return ""
        profile = group_members.get(sender_id, {})
        if not isinstance(profile, dict):
            return ""
        preferred_name = str(profile.get("preferred_name", "") or "").strip()
        if preferred_name:
            return preferred_name
        return str(profile.get("sender_name", "") or "").strip()

    def _remember_group_member(
        self,
        session: SessionMemory,
        sender_id: str,
        sender_name: str,
        content: str,
    ) -> None:
        group_members = session.metadata.setdefault("group_members", {})
        if not isinstance(group_members, dict):
            group_members = {}
            session.metadata["group_members"] = group_members
        profile = group_members.setdefault(sender_id, {})
        if not isinstance(profile, dict):
            profile = {}
            group_members[sender_id] = profile
        profile["sender_name"] = sender_name
        preferred_name = self.extract_preferred_name(content)
        if preferred_name:
            profile["preferred_name"] = preferred_name

    @staticmethod
    def _split_history_line(line: str) -> tuple[str, str]:
        speaker, _, content = str(line or "").partition(": ")
        if _:
            return speaker.strip() or "user", content.strip()
        return "user", str(line or "").strip()

    @staticmethod
    def _extract_terms(text: str) -> list[str]:
        normalized = str(text or "").lower()
        terms = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", normalized)
        unique: list[str] = []
        seen: set[str] = set()
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            unique.append(term)
        return unique

    @staticmethod
    def _score_memory(summary: str, tags: list[str], terms: list[str]) -> float:
        haystack_summary = summary.lower()
        haystack_tags = [tag.lower() for tag in tags]
        score = 0.0
        for term in terms:
            if any(term in tag or tag in term for tag in haystack_tags):
                score += 2.0
                continue
            if term in haystack_summary:
                score += 1.0
        return score
