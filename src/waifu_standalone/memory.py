from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .models import SessionMemory

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
        # Disk is the source of truth for this store; the caller's session object
        # already mirrors what we persisted, so returning it directly avoids a
        # per-message deepcopy on the hot path.
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
        target_root = self.root if self._scoped_character_id else (self.root / safe_character_id if safe_character_id else self.root)
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
