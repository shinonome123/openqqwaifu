from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..systems.value_game import ValueGameEngine


@dataclass(slots=True)
class ProactivePlanner:
    config: AppConfig

    def list_candidates(self, *, members: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
        candidate_limit = max(1, int(limit or self.config.proactive_candidate_limit))
        now = time.time()
        items: list[dict[str, Any]] = []
        for member in members:
            affinity = float(member.get("affinity_score") or 0.0)
            if affinity < float(self.config.proactive_min_affinity):
                continue
            last_seen = float(member.get("last_seen_at") or 0.0)
            if not last_seen:
                continue
            idle_seconds = max(0.0, now - last_seen)
            if idle_seconds < float(self.config.proactive_inactive_hours) * 3600.0:
                continue
            display_name = self._display_name(member)
            stage = ValueGameEngine.bond_stage(affinity)
            reason = self._build_reason(display_name, stage, idle_seconds, member)
            score = affinity * 0.7 + min(idle_seconds / 86400.0, 2.0) * 0.3
            items.append(
                {
                    "group_id": str(member.get("group_id", "") or "").strip(),
                    "user_id": str(member.get("user_id", "") or "").strip(),
                    "display_name": display_name,
                    "affinity_score": affinity,
                    "bond_stage": stage,
                    "idle_seconds": idle_seconds,
                    "score": round(score, 4),
                    "reason": reason,
                }
            )
        items.sort(key=lambda item: (-float(item.get("score") or 0.0), -float(item.get("idle_seconds") or 0.0)))
        return items[:candidate_limit]

    def build_draft(
        self,
        *,
        member: dict[str, Any],
        assistant_name: str,
        relationship_hint: str = "",
    ) -> dict[str, Any]:
        display_name = self._display_name(member)
        stage = ValueGameEngine.bond_stage(member.get("affinity_score"))
        if stage in {"close", "devoted"}:
            text = f"嗯，{display_name}，刚刚又想起你了。{assistant_name}还记得你最近的节奏，要不要再来聊一会儿？"
        elif stage == "familiar":
            text = f"{display_name}，有点久没看到你冒泡了。{assistant_name}在这里，想聊什么都可以直接说。"
        else:
            text = f"{display_name}，今天过得怎么样？{assistant_name}在这边留了个位置，想说话的时候就回来吧。"
        if relationship_hint:
            text += f" {relationship_hint}"
        return {
            "launcher_type": "group" if str(member.get("group_id", "") or "").strip() else "person",
            "launcher_id": str(member.get("group_id") or member.get("user_id") or "").strip(),
            "user_id": str(member.get("user_id") or "").strip(),
            "display_name": display_name,
            "bond_stage": stage,
            "text": text,
        }

    @staticmethod
    def _display_name(member: dict[str, Any]) -> str:
        for key in ("preferred_name", "group_card", "qq_nickname", "user_id"):
            value = str(member.get(key, "") or "").strip()
            if value:
                return value
        return "对方"

    @staticmethod
    def _build_reason(display_name: str, stage: str, idle_seconds: float, member: dict[str, Any]) -> str:
        idle_hours = max(1, int(idle_seconds // 3600))
        group_id = str(member.get("group_id", "") or "").strip()
        if group_id:
            return f"{display_name} 在群 {group_id} 已经 {idle_hours} 小时没有出现，当前关系阶段是 {stage}。"
        return f"{display_name} 已经 {idle_hours} 小时没有私聊互动，当前关系阶段是 {stage}。"
