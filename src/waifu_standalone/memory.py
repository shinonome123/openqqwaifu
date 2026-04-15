from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .models import SessionMemory

HISTORY_LIMIT = 120
_SAFE_LAUNCHER_ID_CHARS = frozenset("-_")
_ALLOWED_LAUNCHER_TYPES = frozenset({"group", "person"})


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
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str], SessionMemory] = {}

    def load(self, launcher_id: str, launcher_type: str, character_id: str = "") -> SessionMemory:
        safe_character_id = self._sanitize_character_id(character_id)
        key = (safe_character_id, launcher_id, launcher_type)
        if key not in self._sessions:
            self._sessions[key] = SessionMemory(
                launcher_id=launcher_id,
                launcher_type=launcher_type,
                character_id=safe_character_id,
            )
        return clone_session(self._sessions[key])

    def save(self, session: SessionMemory) -> SessionMemory:
        session.character_id = self._sanitize_character_id(session.character_id)
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

    @staticmethod
    def _sanitize_character_id(character_id: str) -> str:
        return "".join(
            char for char in str(character_id or "") if char.isalnum() or char in _SAFE_LAUNCHER_ID_CHARS
        )


class FileMemoryStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, launcher_id: str, launcher_type: str, character_id: str = "") -> SessionMemory:
        safe_character_id = self._sanitize_character_id(character_id)
        path = self._session_path(launcher_id, launcher_type, character_id=safe_character_id)
        if not path.exists():
            return SessionMemory(
                launcher_id=launcher_id,
                launcher_type=launcher_type,
                character_id=safe_character_id,
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        resolved_character_id = str(data.get("character_id", safe_character_id)).strip() or safe_character_id
        return SessionMemory(
            launcher_id=str(data.get("launcher_id", launcher_id)),
            launcher_type=str(data.get("launcher_type", launcher_type)),
            character_id=resolved_character_id,
            history=list(data.get("history", [])),
            preferred_name=str(data.get("preferred_name", "")),
            metadata=deepcopy(data.get("metadata", {})),
        )

    def save(self, session: SessionMemory) -> SessionMemory:
        session.character_id = self._sanitize_character_id(session.character_id)
        path = self._session_path(session.launcher_id, session.launcher_type, character_id=session.character_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(asdict(session), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return clone_session(session)

    def append(self, launcher_id: str, launcher_type: str, line: str, character_id: str = "") -> SessionMemory:
        session = self.load(launcher_id, launcher_type, character_id=character_id)
        session.history.append(line)
        session.history = session.history[-HISTORY_LIMIT:]
        return self.save(session)

    def list_sessions(self) -> list[SessionMemory]:
        sessions: list[SessionMemory] = []
        for path in sorted(self.root.glob("*.json")):
            parsed = self._parse_session_path(path)
            if parsed is None:
                continue
            character_id, launcher_type, launcher_id = parsed
            try:
                sessions.append(self.load(launcher_id, launcher_type, character_id=character_id))
            except (ValueError, json.JSONDecodeError, OSError):
                continue
        for directory in sorted(path for path in self.root.iterdir() if path.is_dir()):
            for path in sorted(directory.glob("*.json")):
                parsed = self._parse_session_path(path)
                if parsed is None:
                    continue
                character_id, launcher_type, launcher_id = parsed
                try:
                    sessions.append(self.load(launcher_id, launcher_type, character_id=character_id))
                except (ValueError, json.JSONDecodeError, OSError):
                    continue
        return sessions

    def session_path(self, launcher_id: str, launcher_type: str, character_id: str = "") -> Path:
        return self._session_path(launcher_id, launcher_type, character_id=character_id)

    def _session_path(self, launcher_id: str, launcher_type: str, character_id: str = "") -> Path:
        safe_launcher_type = self._sanitize_launcher_type(launcher_type)
        safe_launcher_id = self._sanitize_launcher_id(launcher_id)
        safe_character_id = self._sanitize_character_id(character_id)
        target_root = self.root / safe_character_id if safe_character_id else self.root
        path = (target_root / f"{safe_launcher_type}_{safe_launcher_id}.json").resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise ValueError("session path escapes storage root")
        return path

    def _legacy_session_path(self, launcher_id: str, launcher_type: str) -> Path:
        safe_launcher_type = self._sanitize_launcher_type(launcher_type)
        safe_launcher_id = self._sanitize_launcher_id(launcher_id)
        path = (self.root / f"{safe_launcher_type}_{safe_launcher_id}.json").resolve()
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
        filename = Path(relative_parts[-1]).stem
        parts = filename.split("_", 1)
        if len(parts) != 2:
            return None
        launcher_type, launcher_id = parts
        if len(relative_parts) > 2:
            return None
        character_id = relative_parts[0] if len(relative_parts) == 2 else ""
        return character_id, launcher_type, launcher_id

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
