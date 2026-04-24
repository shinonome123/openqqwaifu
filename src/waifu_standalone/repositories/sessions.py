from __future__ import annotations

from typing import Any

from ..models import SessionMemory


class SessionRepository:
    def __init__(self, store: Any) -> None:
        self._store = store

    def bind(self, store: Any) -> None:
        self._store = store

    def save(self, session: SessionMemory) -> SessionMemory:
        return self._store.save(session)

    def list_sessions(self) -> list[SessionMemory]:
        list_sessions = getattr(self._store, "list_sessions", None)
        if not callable(list_sessions):
            return []
        return list(list_sessions())

    def clear(self) -> None:
        clear = getattr(self._store, "clear", None)
        if callable(clear):
            clear()
