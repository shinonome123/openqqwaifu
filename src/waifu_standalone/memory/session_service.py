from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from ..contracts import MemoryStore
from ..models import InboundEvent, SessionMemory

HISTORY_LIMIT = 120
_SAFE_LAUNCHER_ID_CHARS = frozenset("-_")
_ALLOWED_LAUNCHER_TYPES = frozenset({"group", "person"})
_SESSION_LOG_SUFFIX = ".jsonl"
_LEGACY_SESSION_SUFFIX = ".json"
_COMPACT_THRESHOLD_BYTES = 256 * 1024


def clone_session(session: SessionMemory) -> SessionMemory:
    return SessionMemory(
        launcher_id=session.launcher_id,
        launcher_type=session.launcher_type,
        character_id=session.character_id,
        history=list(session.history),
        preferred_name=session.preferred_name,
        metadata=deepcopy(session.metadata),
    )


class InMemoryStore:
    def __init__(self, scoped_character_id: str = "") -> None:
        self._sessions: dict[tuple[str, str, str], SessionMemory] = {}
        self._scoped_character_id = self._sanitize_character_id(scoped_character_id)

    def load(self, launcher_id: str, launcher_type: str, character_id: str = "") -> SessionMemory:
        safe_character_id = self._resolve_character_id(character_id)
        key = (safe_character_id, launcher_id, launcher_type)
        if key not in self._sessions:
            self._sessions[key] = SessionMemory(
                launcher_id=launcher_id,
                launcher_type=launcher_type,
                character_id=safe_character_id,
            )
        return clone_session(self._sessions[key])

    def save(self, session: SessionMemory) -> SessionMemory:
        session.character_id = self._resolve_character_id(session.character_id)
        key = (session.character_id, session.launcher_id, session.launcher_type)
        self._sessions[key] = clone_session(session)
        return clone_session(self._sessions[key])

    def append(self, launcher_id: str, launcher_type: str, line: str, character_id: str = "") -> SessionMemory:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        session.history.append(line)
        session.history = session.history[-HISTORY_LIMIT:]
        return self.save(session)

    def list_sessions(self) -> list[SessionMemory]:
        return [clone_session(session) for _, session in sorted(self._sessions.items(), key=lambda item: item[0])]

    def clear(self) -> None:
        self._sessions.clear()

    def _resolve_character_id(self, character_id: str) -> str:
        if self._scoped_character_id:
            return self._scoped_character_id
        return self._sanitize_character_id(character_id)

    @staticmethod
    def _sanitize_character_id(character_id: str) -> str:
        return "".join(
            char for char in str(character_id or "") if char.isalnum() or char in _SAFE_LAUNCHER_ID_CHARS
        )


class FileMemoryStore:
    def __init__(self, root: str | Path, scoped_character_id: str = ""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._scoped_character_id = self._sanitize_character_id(scoped_character_id)

    def load(self, launcher_id: str, launcher_type: str, character_id: str = "") -> SessionMemory:
        safe_character_id = self._resolve_character_id(character_id)
        log_path = self._session_path(launcher_id, launcher_type, character_id=safe_character_id)
        loaded = self._load_latest_snapshot(
            log_path,
            launcher_id=launcher_id,
            launcher_type=launcher_type,
            character_id=safe_character_id,
        )
        if loaded is not None:
            return loaded

        legacy_path = self._legacy_session_path(launcher_id, launcher_type, character_id=safe_character_id)
        if legacy_path.exists():
            legacy_session = self._load_legacy_snapshot(
                legacy_path,
                launcher_id=launcher_id,
                launcher_type=launcher_type,
                character_id=safe_character_id,
            )
            self._append_snapshot(log_path, legacy_session)
            return legacy_session

        return SessionMemory(
            launcher_id=launcher_id,
            launcher_type=launcher_type,
            character_id=safe_character_id,
        )

    def save(self, session: SessionMemory) -> SessionMemory:
        session.character_id = self._resolve_character_id(session.character_id)
        log_path = self._session_path(session.launcher_id, session.launcher_type, character_id=session.character_id)
        self._append_snapshot(log_path, session)
        self._maybe_compact(log_path, session)
        return session

    def append(self, launcher_id: str, launcher_type: str, line: str, character_id: str = "") -> SessionMemory:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        session.history.append(line)
        session.history = session.history[-HISTORY_LIMIT:]
        return self.save(session)

    def list_sessions(self) -> list[SessionMemory]:
        sessions: dict[tuple[str, str, str], SessionMemory] = {}
        for suffix in (_SESSION_LOG_SUFFIX, _LEGACY_SESSION_SUFFIX):
            for path in self._iter_session_paths(suffix):
                parsed = self._parse_session_path(path)
                if parsed is None:
                    continue
                if parsed in sessions:
                    continue
                character_id, launcher_type, launcher_id = parsed
                try:
                    sessions[parsed] = self.load(launcher_id, launcher_type, character_id=character_id)
                except (ValueError, json.JSONDecodeError, OSError):
                    continue
        return [sessions[key] for key in sorted(sessions)]

    def session_path(self, launcher_id: str, launcher_type: str, character_id: str = "") -> Path:
        return self._session_path(launcher_id, launcher_type, character_id=character_id)

    def clear(self) -> None:
        for suffix in (_SESSION_LOG_SUFFIX, _LEGACY_SESSION_SUFFIX):
            for path in self._iter_session_paths(suffix):
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue

    def _session_path(self, launcher_id: str, launcher_type: str, character_id: str = "") -> Path:
        return self._build_session_path(
            launcher_id,
            launcher_type,
            character_id=character_id,
            suffix=_SESSION_LOG_SUFFIX,
        )

    def _legacy_session_path(self, launcher_id: str, launcher_type: str, character_id: str = "") -> Path:
        return self._build_session_path(
            launcher_id,
            launcher_type,
            character_id=character_id,
            suffix=_LEGACY_SESSION_SUFFIX,
        )

    def _build_session_path(
        self,
        launcher_id: str,
        launcher_type: str,
        *,
        character_id: str = "",
        suffix: str,
    ) -> Path:
        safe_launcher_type = self._sanitize_launcher_type(launcher_type)
        safe_launcher_id = self._sanitize_launcher_id(launcher_id)
        safe_character_id = self._resolve_character_id(character_id)
        target_root = (
            self.root
            if self._scoped_character_id
            else (self.root / safe_character_id if safe_character_id else self.root)
        )
        path = (target_root / f"{safe_launcher_type}_{safe_launcher_id}{suffix}").resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise ValueError("session path escapes storage root")
        return path

    def _parse_session_path(self, path: Path) -> tuple[str, str, str] | None:
        root = self.root.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            return None
        relative_parts = resolved.relative_to(root).parts
        if not relative_parts:
            return None
        filename = Path(relative_parts[-1])
        if filename.suffix not in {_SESSION_LOG_SUFFIX, _LEGACY_SESSION_SUFFIX}:
            return None
        parts = filename.stem.split("_", 1)
        if len(parts) != 2:
            return None
        launcher_type, launcher_id = parts
        if len(relative_parts) > 2:
            return None
        if self._scoped_character_id:
            character_id = self._scoped_character_id
        else:
            character_id = relative_parts[0] if len(relative_parts) == 2 else ""
        return character_id, launcher_type, launcher_id

    def _iter_session_paths(self, suffix: str) -> list[Path]:
        paths = sorted(self.root.glob(f"*{suffix}"))
        for directory in sorted(path for path in self.root.iterdir() if path.is_dir()):
            paths.extend(sorted(directory.glob(f"*{suffix}")))
        return paths

    def _load_latest_snapshot(
        self,
        path: Path,
        *,
        launcher_id: str,
        launcher_type: str,
        character_id: str,
    ) -> SessionMemory | None:
        if not path.exists():
            return None
        for raw_line in reversed(path.read_text(encoding="utf-8").splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            data = json.loads(line)
            return self._session_from_payload(
                data,
                launcher_id=launcher_id,
                launcher_type=launcher_type,
                character_id=character_id,
            )
        return None

    def _load_legacy_snapshot(
        self,
        path: Path,
        *,
        launcher_id: str,
        launcher_type: str,
        character_id: str,
    ) -> SessionMemory:
        data = json.loads(path.read_text(encoding="utf-8"))
        return self._session_from_payload(
            data,
            launcher_id=launcher_id,
            launcher_type=launcher_type,
            character_id=character_id,
        )

    def _session_from_payload(
        self,
        data: object,
        *,
        launcher_id: str,
        launcher_type: str,
        character_id: str,
    ) -> SessionMemory:
        if not isinstance(data, dict):
            raise json.JSONDecodeError("session payload must be an object", doc=str(data), pos=0)
        resolved_character_id = str(data.get("character_id", character_id)).strip() or character_id
        raw_history = data.get("history", [])
        history = [str(item) for item in raw_history] if isinstance(raw_history, list) else []
        raw_metadata = data.get("metadata", {})
        metadata: dict[str, object] = raw_metadata if isinstance(raw_metadata, dict) else {}
        return SessionMemory(
            launcher_id=str(data.get("launcher_id", launcher_id)),
            launcher_type=str(data.get("launcher_type", launcher_type)),
            character_id=resolved_character_id,
            history=history,
            preferred_name=str(data.get("preferred_name", "")),
            metadata=metadata,
        )

    def _append_snapshot(self, path: Path, session: SessionMemory) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(session), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    def _maybe_compact(self, path: Path, session: SessionMemory) -> None:
        try:
            if path.stat().st_size <= _COMPACT_THRESHOLD_BYTES:
                return
        except OSError:
            return
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(asdict(session), ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)

    @staticmethod
    def _sanitize_launcher_id(launcher_id: str) -> str:
        safe_launcher_id = "".join(
            char for char in str(launcher_id or "") if char.isalnum() or char in _SAFE_LAUNCHER_ID_CHARS
        )
        if not safe_launcher_id:
            raise ValueError("launcher_id must contain at least one safe character")
        return safe_launcher_id

    @staticmethod
    def _sanitize_launcher_type(launcher_type: str) -> str:
        resolved = str(launcher_type or "").strip().lower()
        if resolved not in _ALLOWED_LAUNCHER_TYPES:
            raise ValueError("launcher_type must be 'group' or 'person'")
        return resolved

    @staticmethod
    def _sanitize_character_id(character_id: str) -> str:
        return "".join(
            char for char in str(character_id or "") if char.isalnum() or char in _SAFE_LAUNCHER_ID_CHARS
        )

    def _resolve_character_id(self, character_id: str) -> str:
        if self._scoped_character_id:
            return self._scoped_character_id
        return self._sanitize_character_id(character_id)


class Memory:
    """Session memory organ backed by a persistent store."""

    _PREFERRED_NAME_PATTERNS = (
        re.compile(
            r"(?:叫我|喊我|称呼我|请叫我|可以叫我|你可以叫我|你就叫我)\s*([^\s,，。！？!?:：]{1,12})"
        ),
        re.compile(r"(?:我的名字是|我叫|我是)\s*([^\s,，。！？!?:：]{1,12})"),
        re.compile(r"(?:call\s+me|my\s+name\s+is|i\s*am|i'm)\s+([A-Za-z0-9_]{1,12})", flags=re.IGNORECASE),
    )
    _PREFERRED_NAME_BLACKLIST = {"我", "你", "名字", "姓名", "称呼", "昵称", "一下", "一个"}
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
