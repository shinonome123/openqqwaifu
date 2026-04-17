from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .models import InboundEvent, SessionMemory

if TYPE_CHECKING:
    from .app import WaifuService


def _safe_float(payload: dict[str, object], key: str, default: float) -> float:
    raw = payload.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class KnowledgeCurator:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def _writeback_knowledge_if_needed(
        self,
        event: InboundEvent,
        *,
        session: SessionMemory,
        latest_message: str,
        assistant_name: str,
        address: str,
        conversation_view: str,
    ) -> None:
        svc = self.service
        if not svc.config.knowledge_auto_extract:
            return
        cleaned_message = " ".join(str(latest_message or "").split()).strip()
        if not cleaned_message:
            return
        if svc.generator._asks_for_name(cleaned_message):
            return
        member = self._member_record(event) or {}
        if str(member.get("onboarding_status", "") or "").strip() == "pending_name":
            if svc.onboarding.extract_preferred_name(cleaned_message):
                return
        if svc.dispatcher.extract_image_prompt(cleaned_message):
            return
        extracted = svc.generator.extract_knowledge(
            event,
            session,
            assistant_name=assistant_name,
            latest_message=cleaned_message,
            conversation_view=conversation_view,
            address=address,
            max_entries=max(1, int(svc.config.knowledge_auto_extract_limit)),
            allow_fallback=True,
        )
        entries = extracted.get("entries", []) if isinstance(extracted, dict) else []
        profile_summary = str(extracted.get("profile_summary", "") or "").strip() if isinstance(extracted, dict) else ""
        if not entries and not profile_summary:
            return
        saved_entries: list[dict[str, object]] = []
        for item in entries if isinstance(entries, list) else []:
            saved = self._persist_extracted_knowledge(event, item, message_id=event.message_id)
            if saved:
                saved_entries.append(saved)
        if saved_entries or profile_summary:
            svc.persona.update_member_profile_summary(
                event,
                extra_summary=profile_summary,
            )

    async def _awriteback_knowledge_if_needed(
        self,
        event: InboundEvent,
        *,
        session: SessionMemory,
        latest_message: str,
        assistant_name: str,
        address: str,
        conversation_view: str,
    ) -> None:
        svc = self.service
        if not svc.config.knowledge_auto_extract:
            return
        cleaned_message = " ".join(str(latest_message or "").split()).strip()
        if not cleaned_message:
            return
        if svc.generator._asks_for_name(cleaned_message):
            return
        member = self._member_record(event) or {}
        if str(member.get("onboarding_status", "") or "").strip() == "pending_name":
            if svc.onboarding.extract_preferred_name(cleaned_message):
                return
        if svc.dispatcher.extract_image_prompt(cleaned_message):
            return
        extracted = await svc.generator.aextract_knowledge(
            event,
            session,
            assistant_name=assistant_name,
            latest_message=cleaned_message,
            conversation_view=conversation_view,
            address=address,
            max_entries=max(1, int(svc.config.knowledge_auto_extract_limit)),
            allow_fallback=True,
        )
        entries = extracted.get("entries", []) if isinstance(extracted, dict) else []
        profile_summary = str(extracted.get("profile_summary", "") or "").strip() if isinstance(extracted, dict) else ""
        if not entries and not profile_summary:
            return
        saved_entries: list[dict[str, object]] = []
        for item in entries if isinstance(entries, list) else []:
            saved = self._persist_extracted_knowledge(event, item, message_id=event.message_id)
            if saved:
                saved_entries.append(saved)
        if saved_entries or profile_summary:
            svc.persona.update_member_profile_summary(
                event,
                extra_summary=profile_summary,
            )

    def _persist_extracted_knowledge(
        self,
        event: InboundEvent,
        entry: dict[str, object],
        *,
        message_id: str = "",
    ) -> dict[str, object] | None:
        svc = self.service
        summary = " ".join(str(entry.get("summary", "") or "").split()).strip()
        if not summary:
            return None
        scope_type, scope_id = self._knowledge_scope_for_candidate(event, entry)
        memory_type = str(entry.get("memory_type", "") or "fact").strip().lower() or "fact"
        tags = (
            [str(tag).strip() for tag in entry.get("tags", []) if str(tag).strip()]
            if isinstance(entry.get("tags"), list)
            else []
        )
        confidence = _safe_float(entry, "confidence", 0.6)
        source_message_ids = [str(message_id).strip()] if str(message_id or "").strip() else []
        existing = self._existing_knowledge_entry(scope_type, scope_id, summary)
        payload: dict[str, object] = {
            "character_id": svc._active_character_id(),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "memory_type": memory_type,
            "summary": summary,
            "tags": tags,
            "confidence": confidence,
            "source_message_ids": source_message_ids,
        }
        if existing is not None:
            payload["id"] = existing.get("id", 0)
            merged_tags = {
                *[str(tag).strip() for tag in existing.get("tags", []) if str(tag).strip()],
                *tags,
            }
            merged_source_ids = {
                *[str(item).strip() for item in existing.get("source_message_ids", []) if str(item).strip()],
                *source_message_ids,
            }
            payload["tags"] = sorted(merged_tags)[:8]
            payload["source_message_ids"] = sorted(merged_source_ids)
            payload["confidence"] = max(confidence, _safe_float(existing, "confidence", confidence))
        return svc.state_store.save_knowledge(payload)

    def _existing_knowledge_entry(
        self,
        scope_type: str,
        scope_id: str,
        summary: str,
    ) -> dict[str, object] | None:
        svc = self.service
        current_character = svc._active_character_id()
        limit = max(
            80,
            int(svc.state_store.knowledge_count(character_id=current_character))
            if hasattr(svc.state_store, "knowledge_count")
            else 80,
        )
        target = " ".join(str(summary or "").split()).strip().casefold()
        for item in svc.state_store.list_knowledge(limit=limit, character_id=current_character):
            if str(item.get("scope_type", "") or "").strip() != scope_type:
                continue
            if str(item.get("scope_id", "") or "").strip() != scope_id:
                continue
            current = " ".join(str(item.get("summary", "") or "").split()).strip().casefold()
            if current == target:
                return item
        return None

    def _knowledge_scope_for_candidate(self, event: InboundEvent, entry: dict[str, object]) -> tuple[str, str]:
        scope_hint = str(entry.get("scope_hint", "") or "").strip().lower()
        if scope_hint == "group" and event.launcher_type == "group":
            return "group", event.launcher_id
        if scope_hint == "global":
            return "global", ""
        if scope_hint == "person" and event.launcher_type == "person":
            return "person", event.launcher_id
        if event.launcher_type == "group":
            return "member", f"{event.launcher_id}:{event.sender_id}"
        return "member", event.sender_id

    @staticmethod
    def _merge_memory_hints(primary: list[str], secondary: list[str], *, limit: int) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for candidate in [*(primary or []), *(secondary or [])]:
            text = str(candidate or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
            if len(merged) >= max(1, int(limit)):
                break
        return merged

    def _knowledge_scopes(self, event: InboundEvent) -> list[tuple[str, str]]:
        scopes = [(event.launcher_type, event.launcher_id), ("global", "")]
        if event.launcher_type == "group":
            scopes.append(("member", f"{event.launcher_id}:{event.sender_id}"))
        else:
            scopes.append(("member", event.sender_id))
        return scopes

    def _member_record(self, event: InboundEvent) -> dict[str, Any] | None:
        svc = self.service
        return svc.state_store.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=svc._active_character_id(),
        )

    async def _amember_record(self, event: InboundEvent) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._member_record, event)

    def _session_knowledge_entries(self, event: InboundEvent, query: str) -> list[dict[str, Any]]:
        svc = self.service
        scopes = set(self._knowledge_scopes(event))
        query_terms = svc._extract_terms(query)
        entries = svc.state_store.list_knowledge(
            limit=max(80, svc.config.memory_graph_limit * 6),
            character_id=svc._active_character_id(),
        )
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            scope = (
                str(entry.get("scope_type", "") or "").strip().lower() or "global",
                str(entry.get("scope_id", "") or "").strip(),
            )
            if scope not in scopes:
                continue
            summary = str(entry.get("summary", "") or "").strip().lower()
            tags = [str(item or "").strip().lower() for item in entry.get("tags", []) if str(item or "").strip()]
            score = float(entry.get("confidence") or 0.0)
            if not query_terms:
                score += 0.1
            else:
                for term in query_terms:
                    if term in summary:
                        score += 1.2
                    elif any(term in tag or tag in term for tag in tags):
                        score += 1.6
            scored.append((score, dict(entry)))
        scored.sort(
            key=lambda item: (
                -float(item[0]),
                -int(item[1].get("updated_at") or 0),
                -int(item[1].get("id") or 0),
            )
        )
        return [entry for _, entry in scored[: max(1, int(svc.config.memory_graph_limit))]]

    async def _asession_knowledge_entries(self, event: InboundEvent, query: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._session_knowledge_entries, event, query)

    def _directory_member_notes(self, event: InboundEvent) -> list[str]:
        svc = self.service
        notes: list[str] = []
        current_group = event.launcher_id if event.launcher_type == "group" else ""
        current_character = svc._active_character_id()
        svc.persona.sanitize_member_state(
            group_id=current_group,
            user_id=event.sender_id,
            character_id=current_character,
        )
        members = svc.state_store.list_members(limit=240, character_id=current_character)
        scoped = [
            item
            for item in members
            if str(item.get("group_id", "") or "").strip() == current_group
        ]
        active_key = (current_group, event.sender_id)
        scoped.sort(
            key=lambda item: (
                (str(item.get("group_id", "") or "").strip(), str(item.get("user_id", "") or "").strip()) != active_key,
                -(int(item.get("last_seen_at") or 0)),
                -(int(item.get("updated_at") or 0)),
            )
        )
        for member in scoped[:4]:
            qq_name = str(member.get("qq_nickname", "") or "").strip() or event.sender_name
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            profile_summary = str(member.get("profile_summary", "") or "").strip()
            onboarding_status = str(member.get("onboarding_status", "") or "").strip()
            affinity_score = float(member.get("affinity_score") or 0.0)
            bond_stage = svc.value_game.bond_stage(affinity_score)
            if str(member.get("user_id", "") or "").strip() == event.sender_id:
                if preferred_name:
                    notes.append(f"{qq_name} prefers to be called {preferred_name}.")
                else:
                    notes.append(f"The current speaker is {qq_name}.")
                notes.append(f"Bond stage with {qq_name}: {bond_stage} ({affinity_score:.2f}).")
            elif preferred_name:
                notes.append(f"{qq_name} is usually addressed as {preferred_name}.")
            profile_summary = svc.persona.sanitize_profile_summary(profile_summary)
            if profile_summary:
                notes.append(f"Profile summary for {qq_name}: {profile_summary}")
            if onboarding_status and onboarding_status not in {"", "ready"}:
                notes.append(f"{qq_name} is still in onboarding status: {onboarding_status}.")
        return notes

    async def _adirectory_member_notes(self, event: InboundEvent) -> list[str]:
        return await asyncio.to_thread(self._directory_member_notes, event)

    def _detail_knowledge_entries(self, session: SessionMemory) -> list[dict[str, Any]]:
        svc = self.service
        entries = svc.state_store.list_knowledge(
            limit=max(80, svc.config.memory_graph_limit * 8),
            character_id=str(session.character_id or svc._active_character_id()).strip(),
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
        return filtered[: max(1, int(svc.config.memory_graph_limit))]

    def _knowledge_count_for_session(self, session: SessionMemory) -> int:
        svc = self.service
        current_character = str(session.character_id or svc._active_character_id()).strip()
        count_fn = getattr(svc.state_store, "count_knowledge_for_scopes", None)
        if callable(count_fn):
            if session.launcher_type == "group":
                scopes = [("group", session.launcher_id)]
            else:
                scopes = [
                    ("person", session.launcher_id),
                    ("member", session.launcher_id),
                ]
            return count_fn(scopes, character_id=current_character)
        total = max(1, int(svc.state_store.knowledge_count(character_id=current_character)))
        entries = svc.state_store.list_knowledge(limit=total, character_id=current_character)
        if session.launcher_type == "group":
            member_prefix = f"{session.launcher_id}:"
            return sum(
                1
                for entry in entries
                if (
                    str(entry.get("scope_type", "") or "").strip() == "group"
                    and str(entry.get("scope_id", "") or "").strip() == session.launcher_id
                )
                or (
                    str(entry.get("scope_type", "") or "").strip() == "member"
                    and str(entry.get("scope_id", "") or "").strip().startswith(member_prefix)
                )
            )
        return sum(
            1
            for entry in entries
            if (
                str(entry.get("scope_type", "") or "").strip() == "person"
                and str(entry.get("scope_id", "") or "").strip() == session.launcher_id
            )
            or (
                str(entry.get("scope_type", "") or "").strip() == "member"
                and str(entry.get("scope_id", "") or "").strip() == session.launcher_id
            )
        )
