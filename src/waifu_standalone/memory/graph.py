from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..models import InboundEvent, SessionMemory
from ..systems.value_game import ValueGameEngine


@dataclass(slots=True)
class MemoryGraphBuilder:
    config: AppConfig

    def build(
        self,
        *,
        event: InboundEvent,
        session: SessionMemory,
        member: dict[str, Any] | None,
        knowledge_entries: list[dict[str, Any]],
        behavior_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.config.memory_graph_mode:
            return {"enabled": False, "nodes": [], "edges": [], "highlights": []}

        session_id = f"{event.launcher_type}:{event.launcher_id}"
        member_name = self._member_name(member, fallback=event.sender_name or event.sender_id)
        affinity = float((member or {}).get("affinity_score") or 0.0)
        stage = ValueGameEngine.bond_stage(affinity)
        highlights: list[str] = []
        nodes: list[dict[str, Any]] = [
            {"id": "session", "kind": "session", "label": session_id},
            {
                "id": "speaker",
                "kind": "member",
                "label": member_name,
                "meta": {"affinity_score": affinity, "bond_stage": stage},
            },
        ]
        edges: list[dict[str, Any]] = [
            {"source": "speaker", "target": "session", "label": f"{stage}:{affinity:.2f}"}
        ]

        if member_name:
            highlights.append(f"当前主要对象是 {member_name}，关系阶段 {stage}。")

        for index, entry in enumerate(knowledge_entries[: max(1, int(self.config.memory_graph_limit))], start=1):
            summary = str(entry.get("summary", "") or "").strip()
            if not summary:
                continue
            node_id = f"knowledge-{index}"
            nodes.append(
                {
                    "id": node_id,
                    "kind": "knowledge",
                    "label": self._clip(summary, 38),
                    "meta": {
                        "memory_type": str(entry.get("memory_type", "") or "").strip() or "fact",
                        "scope_type": str(entry.get("scope_type", "") or "").strip() or "global",
                    },
                }
            )
            edges.append(
                {
                    "source": "session",
                    "target": node_id,
                    "label": str(entry.get("memory_type", "") or "memory"),
                }
            )
            if len(highlights) < 4:
                highlights.append(f"相关记忆：{self._clip(summary, 54)}")

        for index, item in enumerate(behavior_events[:3], start=1):
            summary = str(item.get("summary", "") or "").strip()
            if not summary:
                continue
            node_id = f"event-{index}"
            nodes.append(
                {
                    "id": node_id,
                    "kind": "event",
                    "label": self._clip(summary, 34),
                    "meta": {"kind": str(item.get("kind", "") or "").strip() or "event"},
                }
            )
            edges.append(
                {
                    "source": "speaker",
                    "target": node_id,
                    "label": str(item.get("kind", "") or "event"),
                }
            )
            if len(highlights) < 6:
                highlights.append(f"最近行为：{self._clip(summary, 48)}")

        if not highlights and session.history:
            highlights.append(f"最近对话还在围绕“{self._clip(session.history[-1], 42)}”展开。")

        return {
            "enabled": True,
            "session_id": session_id,
            "nodes": nodes,
            "edges": edges,
            "highlights": highlights,
            "member": {
                "display_name": member_name,
                "affinity_score": affinity,
                "bond_stage": stage,
            },
        }

    @staticmethod
    def _member_name(member: dict[str, Any] | None, *, fallback: str) -> str:
        if not isinstance(member, dict):
            return fallback
        for key in ("preferred_name", "group_card", "qq_nickname", "user_id"):
            value = str(member.get(key, "") or "").strip()
            if value:
                return value
        return fallback

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        compact = " ".join(str(text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: max(10, limit - 1)].rstrip() + "…"
