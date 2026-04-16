from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import AppConfig
from ..models import InboundEvent, MessageSegment, SessionMemory
from .memory_graph import MemoryGraphBuilder


@dataclass(slots=True)
class SessionDetailGraphService:
    config: AppConfig
    state_store: Any
    active_character_id: Callable[[], str]
    get_behavior_events: Callable[..., list[dict[str, object]]]
    _memory_graph: MemoryGraphBuilder = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._memory_graph = MemoryGraphBuilder(self.config)

    def build(self, session: SessionMemory) -> dict[str, Any]:
        launcher_id = str(session.launcher_id or "").strip()
        launcher_type = str(session.launcher_type or "").strip()
        character_id = str(session.character_id or self.active_character_id()).strip()
        if launcher_type == "person":
            member = self.state_store.get_member(group_id="", user_id=launcher_id, character_id=character_id)
            sender_id = launcher_id
            sender_name = str((member or {}).get("preferred_name") or (member or {}).get("qq_nickname") or launcher_id)
        else:
            recent_behavior = self.get_behavior_events(limit=1, launcher_type=launcher_type, launcher_id=launcher_id)
            sender_id = str(recent_behavior[0].get("sender_id", "") or "") if recent_behavior else ""
            member = (
                self.state_store.get_member(
                    group_id=launcher_id,
                    user_id=sender_id,
                    character_id=character_id,
                )
                if sender_id
                else None
            )
            sender_name = str((member or {}).get("preferred_name") or (member or {}).get("qq_nickname") or launcher_id)
        detail_event = InboundEvent(
            launcher_id=launcher_id,
            launcher_type=launcher_type if launcher_type in {"group", "person"} else "person",
            sender_id=sender_id,
            sender_name=sender_name,
            segments=[MessageSegment(kind="text", text=session.history[-1] if session.history else "")],
        )
        behavior_events = self.get_behavior_events(limit=8, launcher_type=launcher_type, launcher_id=launcher_id)
        return self._memory_graph.build(
            event=detail_event,
            session=session,
            member=member,
            knowledge_entries=self._detail_knowledge_entries(session),
            behavior_events=behavior_events,
        )

    def _detail_knowledge_entries(self, session: SessionMemory) -> list[dict[str, Any]]:
        entries = self.state_store.list_knowledge(
            limit=max(80, self.config.memory_graph_limit * 8),
            character_id=str(session.character_id or self.active_character_id()).strip(),
        )
        scopes = {(session.launcher_type, session.launcher_id), ("global", "")}
        if session.launcher_type == "person":
            scopes.add(("member", session.launcher_id))
        filtered = [
            dict(entry)
            for entry in entries
            if (
                str(entry.get("scope_type", "") or "").strip(),
                str(entry.get("scope_id", "") or "").strip(),
            )
            in scopes
        ]
        filtered.sort(key=lambda item: (-float(item.get("confidence") or 0.0), -int(item.get("updated_at") or 0)))
        return filtered[: max(1, int(self.config.memory_graph_limit))]
