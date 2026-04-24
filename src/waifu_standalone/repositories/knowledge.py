from __future__ import annotations

from typing import Any, Callable


class KnowledgeRepository:
    def __init__(
        self,
        state_store: Any,
        *,
        character_resolver: Callable[[], str] | None = None,
    ) -> None:
        self._state_store = state_store
        self._character_resolver = character_resolver

    def bind(self, state_store: Any) -> None:
        self._state_store = state_store

    def active_character_id(self) -> str:
        if self._character_resolver is None:
            return ""
        return str(self._character_resolver() or "").strip()

    def count_knowledge_for_scope(
        self,
        scope_type: str,
        scope_id: str,
        *,
        character_id: object = "",
    ) -> int:
        return int(
            self._state_store.count_knowledge_for_scope(
                scope_type,
                scope_id,
                character_id=character_id,
            )
            or 0
        )

    def count_knowledge_for_scopes(
        self,
        scopes: list[tuple[str, str]],
        *,
        character_id: object = "",
    ) -> int:
        return int(self._state_store.count_knowledge_for_scopes(scopes, character_id=character_id) or 0)

    def save_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._state_store.save_knowledge(payload)

    def save_knowledge_for_active(
        self,
        payload: dict[str, Any],
        *,
        character_id: object = "",
    ) -> dict[str, Any]:
        next_payload = dict(payload)
        next_payload["character_id"] = str(character_id or self.active_character_id()).strip()
        return self.save_knowledge(next_payload)

    def delete_knowledge(self, entry_id: object, *, character_id: object = "") -> bool:
        return bool(self._state_store.delete_knowledge(entry_id, character_id=character_id))

    def delete_knowledge_for_active(self, entry_id: object, *, character_id: object = "") -> bool:
        return self.delete_knowledge(entry_id, character_id=str(character_id or self.active_character_id()).strip())

    def add_knowledge(self, **kwargs: Any) -> dict[str, Any]:
        return self._state_store.add_knowledge(**kwargs)

    def add_knowledge_for_active(self, **kwargs: Any) -> dict[str, Any]:
        next_kwargs = dict(kwargs)
        next_kwargs["character_id"] = str(next_kwargs.get("character_id") or self.active_character_id()).strip()
        return self.add_knowledge(**next_kwargs)

    def list_knowledge(self, *, limit: int = 80, character_id: object | None = None) -> list[dict[str, Any]]:
        return self._state_store.list_knowledge(limit=limit, character_id=character_id)

    def list_knowledge_for_active(
        self,
        *,
        limit: int = 80,
        character_id: object | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_knowledge(limit=limit, character_id=str(character_id or self.active_character_id()).strip())

    def recall_knowledge(
        self,
        *,
        scopes: list[tuple[str, str]],
        query: str,
        limit: int,
        character_id: object = "",
    ) -> list[str]:
        return self._state_store.recall_knowledge(
            scopes=scopes,
            query=query,
            limit=limit,
            character_id=character_id,
        )

    def recall_knowledge_for_active(
        self,
        *,
        scopes: list[tuple[str, str]],
        query: str,
        limit: int,
        character_id: object = "",
    ) -> list[str]:
        return self.recall_knowledge(
            scopes=scopes,
            query=query,
            limit=limit,
            character_id=str(character_id or self.active_character_id()).strip(),
        )

    def knowledge_count(self, *, character_id: object | None = None) -> int:
        return int(self._state_store.knowledge_count(character_id=character_id) or 0)

    def knowledge_count_for_active(self, *, character_id: object | None = None) -> int:
        return self.knowledge_count(character_id=str(character_id or self.active_character_id()).strip())

    def embedded_knowledge_count(self, *, character_id: object | None = None) -> int:
        return int(self._state_store.embedded_knowledge_count(character_id=character_id) or 0)

    def embedded_knowledge_count_for_active(self, *, character_id: object | None = None) -> int:
        return self.embedded_knowledge_count(character_id=str(character_id or self.active_character_id()).strip())
