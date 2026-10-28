"""A tavern game in one chat, and the file it is kept in.

A game lives in ``<data_root>/tavern/sessions/<chat>.json`` while it runs, so a restart
picks it up again, and moves to ``<data_root>/tavern/archive/`` when it ends. The story
stays out of the character's own chat history and memories.
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
    # "menu" until the host picks a scenario, then "playing".
    state: str = "menu"
    # Scenario ids in the order the menu numbered them, so "选 2" means what the host saw.
    menu: list[str] = field(default_factory=list)
    scenario_id: str = ""
    scenario_title: str = ""
    # QQ id -> name, in the order they joined; the host is the first.
    players: dict[str, str] = field(default_factory=dict)
    # The story so far as chat turns: players' rounds are "user", the game master "assistant".
    turns: list[dict[str, str]] = field(default_factory=list)
    # Older rounds folded into a recap once the story outgrows its budget.
    summary: str = ""
    pending: list[PendingAction] = field(default_factory=list)
    # People outside the game already told that one is running.
    hinted: list[str] = field(default_factory=list)
    rounds: int = 0
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
        values["turns"] = [
            {"role": str(turn.get("role")), "content": str(turn.get("content", ""))}
            for turn in payload.get("turns", []) or []
            if isinstance(turn, dict) and turn.get("role") in {"user", "assistant"}
        ]
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
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(session.to_dict(), stream, ensure_ascii=False, indent=1)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def archive(self, session: TavernSession) -> Path | None:
        """Keep a finished game under archive/ and forget the running one."""
        path = self.path(session.launcher_type, session.launcher_id)
        if not session.turns:
            path.unlink(missing_ok=True)
            return None
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session.started_at))
        target = self.archive_dir / f"{path.stem}-{stamp}.json"
        target.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        path.unlink(missing_ok=True)
        return target
