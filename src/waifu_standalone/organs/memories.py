from __future__ import annotations

from ..contracts import MemoryStore
from ..models import SessionMemory


class Memory:
    """Session memory organ with a persistent store interface."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def load(self, launcher_id: str, launcher_type: str) -> SessionMemory:
        return self.store.load(launcher_id, launcher_type)

    def save_user_message(self, launcher_id: str, launcher_type: str, sender_name: str, content: str) -> SessionMemory:
        return self.store.append(launcher_id, launcher_type, f"{sender_name}: {content}")

    def save_assistant_message(self, launcher_id: str, launcher_type: str, content: str) -> SessionMemory:
        return self.store.append(launcher_id, launcher_type, f"assistant: {content}")
