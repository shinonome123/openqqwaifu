from __future__ import annotations

from typing import Any, Callable


class MemberRepository:
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

    def save_assistant_alias(
        self,
        *,
        character_id: object,
        user_id: object,
        assistant_alias: object,
    ) -> dict[str, Any]:
        return self._state_store.save_assistant_alias(
            character_id=character_id,
            user_id=user_id,
            assistant_alias=assistant_alias,
        )

    def get_assistant_alias(
        self,
        *,
        character_id: object,
        user_id: object,
    ) -> dict[str, Any] | None:
        getter = getattr(self._state_store, "get_assistant_alias", None)
        if not callable(getter):
            return None
        return getter(character_id=character_id, user_id=user_id)

    def record_member_seen(
        self,
        *,
        group_id: object,
        user_id: object,
        qq_nickname: object = "",
        group_card: object = "",
    ) -> dict[str, Any]:
        return self._state_store.record_member_seen(
            group_id=group_id,
            user_id=user_id,
            qq_nickname=qq_nickname,
            group_card=group_card,
        )

    def save_member(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._state_store.save_member(payload)

    def save_member_for_active(
        self,
        payload: dict[str, Any],
        *,
        character_id: object = "",
    ) -> dict[str, Any]:
        next_payload = dict(payload)
        next_payload["character_id"] = str(character_id or self.active_character_id()).strip()
        return self.save_member(next_payload)

    def mark_member_membership(
        self,
        *,
        group_id: object,
        user_id: object,
        membership_status: object,
        last_sync_at: object = 0,
    ) -> dict[str, Any] | None:
        return self._state_store.mark_member_membership(
            group_id=group_id,
            user_id=user_id,
            membership_status=membership_status,
            last_sync_at=last_sync_at,
        )

    def mark_group_members_missing(
        self,
        *,
        group_id: object,
        active_user_ids: list[str],
        membership_status: object,
        last_sync_at: object = 0,
    ) -> int:
        marker = getattr(self._state_store, "mark_group_members_missing", None)
        if not callable(marker):
            return 0
        return int(
            marker(
                group_id=group_id,
                active_user_ids=active_user_ids,
                membership_status=membership_status,
                last_sync_at=last_sync_at,
            )
            or 0
        )

    def mark_member_addressed(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        return self._state_store.mark_member_addressed(
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )

    def get_member(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        return self._state_store.get_member(
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )

    def get_member_for_active(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        return self.get_member(
            group_id=group_id,
            user_id=user_id,
            character_id=str(character_id or self.active_character_id()).strip(),
        )

    def reset_member_persona(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object,
    ) -> dict[str, Any] | None:
        return self._state_store.reset_member_persona(
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )

    def reset_member_persona_for_active(
        self,
        *,
        group_id: object,
        user_id: object,
        character_id: object = "",
    ) -> dict[str, Any] | None:
        return self.reset_member_persona(
            group_id=group_id,
            user_id=user_id,
            character_id=str(character_id or self.active_character_id()).strip(),
        )

    def list_members(self, *, limit: int = 120, character_id: object = "") -> list[dict[str, Any]]:
        return self._state_store.list_members(limit=limit, character_id=character_id)

    def list_members_for_active(
        self,
        *,
        limit: int = 120,
        character_id: object = "",
    ) -> list[dict[str, Any]]:
        return self.list_members(limit=limit, character_id=str(character_id or self.active_character_id()).strip())

    def member_count(self) -> int:
        return int(self._state_store.member_count())

    def count_members_in_group(self, group_id: str) -> int:
        counter = getattr(self._state_store, "count_members_in_group", None)
        if not callable(counter):
            return 0
        return int(counter(group_id) or 0)
