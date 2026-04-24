from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..models import InboundEvent, SessionMemory

if TYPE_CHECKING:
    from ..app import WaifuService


class MemberService:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def get_member_directory_panel(self, *, limit: int = 120) -> dict[str, object]:
        svc = self.service
        return {
            "character_id": svc._active_character_id(),
            "members": svc.member_repository.list_members_for_active(limit=limit),
            "member_count": svc.member_repository.member_count(),
            "proactive_enabled": svc.config.proactive_mode,
        }

    def save_directory_member(self, payload: dict[str, object]) -> dict[str, object]:
        return self.service.member_repository.save_member_for_active(payload)

    def reset_directory_member_persona(self, payload: dict[str, object]) -> dict[str, object] | None:
        svc = self.service
        return svc.member_repository.reset_member_persona_for_active(
            group_id=payload.get("group_id"),
            user_id=payload.get("user_id"),
        )

    def get_proactive_panel(self, *, limit: int = 12) -> dict[str, object]:
        svc = self.service
        members = svc.member_repository.list_members_for_active(limit=max(120, limit * 8))
        candidates = svc.proactive.list_candidates(members=members, limit=limit)
        return {
            "character_id": svc._active_character_id(),
            "enabled": svc.config.proactive_mode,
            "inactive_hours": svc.config.proactive_inactive_hours,
            "candidate_limit": svc.config.proactive_candidate_limit,
            "candidates": candidates,
        }

    def generate_proactive_draft(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        member = svc.member_repository.get_member_for_active(
            group_id=group_id,
            user_id=user_id,
        )
        if member is None:
            raise ValueError("member not found")
        relationship_hint = str(payload.get("relationship_hint", "") or "").strip()
        draft = svc.proactive.build_draft(
            member=member,
            assistant_name=svc.config.assistant_name,
            relationship_hint=relationship_hint,
        )
        return {"status": "ok", "draft": draft}

    def resolve_address(self, event: InboundEvent, session: SessionMemory) -> str:
        svc = self.service
        member = svc.member_repository.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
        )
        if member is not None:
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return preferred_name
        if event.launcher_type == "person":
            identity_metadata: dict[str, object] = {}
            waifu_root = session.metadata.get("waifu_root")
            if isinstance(waifu_root, str) and waifu_root.strip():
                identity_metadata["waifu_root"] = waifu_root
            identity_session = SessionMemory(
                launcher_id=session.launcher_id,
                launcher_type=session.launcher_type,
                character_id=str(session.character_id or svc._active_character_id()).strip(),
                metadata=identity_metadata,
            )
            user_name = str(svc.cards.load(event.launcher_type, identity_session).user_name or "").strip()
            if user_name:
                return user_name
        return event.sender_name or "你"

    async def resolve_address_async(self, event: InboundEvent, session: SessionMemory) -> str:
        return await asyncio.to_thread(self.resolve_address, event, session)

    def assistant_alias_for_user(self, user_id: str, *, character_id: str = "") -> str:
        svc = self.service
        safe_user_id = str(user_id or "").strip()
        safe_character_id = str(character_id or svc._active_character_id()).strip()
        if not (safe_user_id and safe_character_id):
            return ""
        try:
            record = svc.member_repository.get_assistant_alias(
                character_id=safe_character_id,
                user_id=safe_user_id,
            )
        except ValueError:
            return ""
        return str((record or {}).get("assistant_alias", "") or "").strip()

    def resolve_assistant_name(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        character_id: str = "",
    ) -> str:
        svc = self.service
        base_name = svc.generator.resolve_assistant_name(event.launcher_type, session)
        alias = self.assistant_alias_for_user(
            event.sender_id,
            character_id=str(character_id or session.character_id or svc._active_character_id()).strip(),
        )
        return alias or base_name

    def session_assistant_alias(self, session: SessionMemory) -> str:
        svc = self.service
        character_id = str(session.character_id or svc._active_character_id()).strip()
        if not character_id:
            return ""
        if session.launcher_type == "person":
            return self.assistant_alias_for_user(session.launcher_id, character_id=character_id)
        recent_behavior = svc.get_behavior_events(
            limit=1,
            launcher_type=session.launcher_type,
            launcher_id=session.launcher_id,
            character_id=character_id,
        )
        sender_id = str(recent_behavior[0].get("sender_id", "") or "").strip() if recent_behavior else ""
        if not sender_id:
            return ""
        return self.assistant_alias_for_user(sender_id, character_id=character_id)

    def session_preferred_name(self, session: SessionMemory) -> str:
        if session.launcher_type != "person":
            return ""
        member = self.service.member_repository.get_member(group_id="", user_id=session.launcher_id)
        if member is None:
            return ""
        return str(member.get("preferred_name", "") or "").strip()
