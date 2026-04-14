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
        history=list(session.history),
        preferred_name=session.preferred_name,
        metadata=deepcopy(session.metadata),
    )


class InMemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], SessionMemory] = {}

    def load(self, launcher_id: str, launcher_type: str) -> SessionMemory:
        key = (launcher_id, launcher_type)
        if key not in self._sessions:
            self._sessions[key] = SessionMemory(launcher_id=launcher_id, launcher_type=launcher_type)
        return clone_session(self._sessions[key])

    def save(self, session: SessionMemory) -> SessionMemory:
        key = (session.launcher_id, session.launcher_type)
        self._sessions[key] = clone_session(session)
        return clone_session(self._sessions[key])

    def append(self, launcher_id: str, launcher_type: str, line: str) -> SessionMemory:
        session = self.load(launcher_id, launcher_type)
        session.history.append(line)
        session.history = session.history[-HISTORY_LIMIT:]
        return self.save(session)

    def list_sessions(self) -> list[SessionMemory]:
        return [clone_session(session) for _, session in sorted(self._sessions.items(), key=lambda item: item[0])]


class FileMemoryStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, launcher_id: str, launcher_type: str) -> SessionMemory:
        path = self._session_path(launcher_id, launcher_type)
        if not path.exists():
            return SessionMemory(launcher_id=launcher_id, launcher_type=launcher_type)
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionMemory(
            launcher_id=str(data.get("launcher_id", launcher_id)),
            launcher_type=str(data.get("launcher_type", launcher_type)),
            history=list(data.get("history", [])),
            preferred_name=str(data.get("preferred_name", "")),
            metadata=deepcopy(data.get("metadata", {})),
        )

    def save(self, session: SessionMemory) -> SessionMemory:
        path = self._session_path(session.launcher_id, session.launcher_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(asdict(session), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return self.load(session.launcher_id, session.launcher_type)

    def append(self, launcher_id: str, launcher_type: str, line: str) -> SessionMemory:
        session = self.load(launcher_id, launcher_type)
        session.history.append(line)
        session.history = session.history[-HISTORY_LIMIT:]
        return self.save(session)

    def list_sessions(self) -> list[SessionMemory]:
        sessions: list[SessionMemory] = []
        for path in sorted(self.root.glob("*.json")):
            parts = path.stem.split("_", 1)
            if len(parts) != 2:
                continue
            launcher_type, launcher_id = parts
            try:
                sessions.append(self.load(launcher_id, launcher_type))
            except (ValueError, json.JSONDecodeError, OSError):
                continue
        return sessions

    def session_path(self, launcher_id: str, launcher_type: str) -> Path:
        return self._session_path(launcher_id, launcher_type)

    def _session_path(self, launcher_id: str, launcher_type: str) -> Path:
        safe_launcher_type = self._sanitize_launcher_type(launcher_type)
        safe_launcher_id = self._sanitize_launcher_id(launcher_id)
        path = (self.root / f"{safe_launcher_type}_{safe_launcher_id}.json").resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise ValueError("session path escapes storage root")
        return path

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
