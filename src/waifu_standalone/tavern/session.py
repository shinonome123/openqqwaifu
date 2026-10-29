"""A tavern game in one chat, and the file it is kept in.

A game lives in ``<data_root>/tavern/sessions/<chat>.json`` while it runs, so a restart
picks it up again, and moves to ``<data_root>/tavern/archive/`` when it ends. The story
itself is a SillyTavern chat (``chat``), out of the character's own history and memories.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PendingAction:
    """Something the game master has not answered yet: a player's line or a note from the program.

    Notes from the program (someone joined, the game began) have no ``actor_id``.
    """

    actor_id: str
    text: str
    at: float = field(default_factory=time.time)


@dataclass(slots=True)
class TavernSession:
    launcher_type: str
    launcher_id: str
    host_id: str
    host_name: str
    # "menu" until the host picks a card, then "playing".
    state: str = "menu"
    # Card ids (SillyTavern avatars) in the order the menu numbered them, so "选 2" means what the host saw.
    menu: list[str] = field(default_factory=list)
    scenario_id: str = ""
    scenario_title: str = ""
    # QQ id -> name, in the order they joined; the host is the first.
    players: dict[str, str] = field(default_factory=dict)
    # The SillyTavern chat the story is in, and its latest reply (for "2" picking its second option).
    chat: str = ""
    last_reply: str = ""
    pending: list[PendingAction] = field(default_factory=list)
    # People outside the game already told that one is running.
    hinted: list[str] = field(default_factory=list)
    rounds: int = 0
    # Opted in by an authorized adult host, in private or with ``adult_in_groups`` enabled.
    # Host/chat authorization is rechecked for content, and minors are always excluded.
    adult: bool = False
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str]:
        return (self.launcher_type, self.launcher_id)

    def touch(self) -> None:
        self.last_activity_at = time.time()

    def to_dict(self) -> dict[str, object]:
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> TavernSession:
        values = {name: payload[name] for name in cls.__dataclass_fields__ if name in payload}
        values["pending"] = [
            PendingAction(str(item.get("actor_id", "")), str(item.get("text", "")), float(item.get("at") or 0.0))
            for item in payload.get("pending", []) or []
            if isinstance(item, dict)
        ]
        values["players"] = {str(key): str(value) for key, value in dict(payload.get("players") or {}).items()}
        return cls(**values)


class SessionFiles:
    def __init__(self, root: Path) -> None:
        self.sessions_dir = root / "sessions"
        self.archive_dir = root / "archive"

    def path(self, launcher_type: str, launcher_id: str) -> Path:
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(launcher_id)) or "unknown"
        return self.sessions_dir / f"{launcher_type}_{safe_id}.json"

    def load(self, launcher_type: str, launcher_id: str) -> TavernSession | None:
        path = self.path(launcher_type, launcher_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return TavernSession.from_dict(payload) if isinstance(payload, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError):
            # A damaged file must not keep the chat in a game nobody can end.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def save(self, session: TavernSession) -> None:
        path = self.path(session.launcher_type, session.launcher_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
        try:
            # Half an emoji from a model must not leave a game that can no longer be saved.
            text = json.dumps(session.to_dict(), ensure_ascii=False, indent=1)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text.encode("utf-16", "surrogatepass").decode("utf-16", "replace"))
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def archive(self, session: TavernSession) -> Path | None:
        """Keep a finished game under archive/ and forget the running one."""
        path = self.path(session.launcher_type, session.launcher_id)
        if not session.chat:
            path.unlink(missing_ok=True)
            return None
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session.started_at))
        target = self.archive_dir / f"{path.stem}-{stamp}.json"
        text = json.dumps(session.to_dict(), ensure_ascii=False, indent=1)
        target.write_text(text.encode("utf-16", "surrogatepass").decode("utf-16", "replace"), encoding="utf-8")
        path.unlink(missing_ok=True)
        return target

    def saves(self, launcher_type: str, launcher_id: str) -> list[tuple[str, TavernSession]]:
        """This chat's games under archive/ as (file name, game), the last played first.

        A game picked up again is archived to the same name when it ends, so each game is one save.
        """
        stem = self.path(launcher_type, launcher_id).stem
        found: list[tuple[str, TavernSession]] = []
        if not self.archive_dir.is_dir():
            return found
        for path in self.archive_dir.glob(f"{stem}-*.json"):
            session = self._read(path)
            if session is not None and session.key == (launcher_type, str(launcher_id)) and session.chat:
                found.append((path.name, session))
        found.sort(key=lambda item: item[1].last_activity_at, reverse=True)
        return found

    def load_save(self, launcher_type: str, launcher_id: str, name: str) -> TavernSession | None:
        """One of ``saves`` by its file name; ``None`` if it is gone or not this chat's."""
        stem = self.path(launcher_type, launcher_id).stem
        if "/" in name or "\\" in name or not name.startswith(f"{stem}-"):
            return None
        session = self._read(self.archive_dir / name)
        return session if session is not None and session.key == (launcher_type, str(launcher_id)) else None

    @staticmethod
    def _read(path: Path) -> TavernSession | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return TavernSession.from_dict(payload) if isinstance(payload, dict) else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None
