from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..cells.generator import Generator
from ..cells.utils import safe_float
from ..config import AppConfig
from ..models import InboundEvent, SessionMemory


@dataclass(slots=True)
class KnowledgeManager:
    config: AppConfig
    generator: Generator
    state_store: Any
    current_character_id: Callable[[], str]
    member_record: Callable[[InboundEvent], dict[str, Any] | None]
    extract_directory_preferred_name: Callable[[str], str]
    extract_image_prompt: Callable[[str], str | None]
    update_member_profile_summary: Callable[..., None]

    @staticmethod
    def merge_memory_hints(primary: list[str], secondary: list[str], *, limit: int) -> list[str]:
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

    def knowledge_scopes(self, event: InboundEvent) -> list[tuple[str, str]]:
        scopes = [(event.launcher_type, event.launcher_id), ("global", "")]
        if event.launcher_type == "group":
            scopes.append(("member", f"{event.launcher_id}:{event.sender_id}"))
        else:
            scopes.append(("member", event.sender_id))
        return scopes

    def recall(
        self,
        event: InboundEvent,
        *,
        query: str,
        limit: int = 3,
    ) -> list[str]:
        safe_limit = max(1, int(limit))
        recalled = self.state_store.recall_knowledge(
            scopes=self.knowledge_scopes(event),
            query=query,
            limit=safe_limit,
            character_id=self.current_character_id(),
        )
        session_entries = self.session_knowledge_entries(event, query, limit=safe_limit)
        session_summaries = [str(item.get("summary", "") or "").strip() for item in session_entries]
        return self.merge_memory_hints(recalled, session_summaries, limit=safe_limit)

    def session_knowledge_entries(self, event: InboundEvent, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        safe_limit = max(1, int(limit))
        scopes = set(self.knowledge_scopes(event))
        query_terms = self._extract_terms(query)
        entries = self.state_store.list_knowledge(
            limit=max(80, safe_limit * 6),
            character_id=self.current_character_id(),
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
        return [entry for _, entry in scored[:safe_limit]]

    def writeback_knowledge_if_needed(
        self,
        event: InboundEvent,
        *,
        session: SessionMemory,
        latest_message: str,
        assistant_name: str,
        address: str,
        conversation_view: str,
    ) -> None:
        if not self.config.knowledge_auto_extract:
            return
        cleaned_message = " ".join(str(latest_message or "").split()).strip()
        if not cleaned_message:
            return
        if self.generator._asks_for_name(cleaned_message):
            return
        member = self.member_record(event) or {}
        if str(member.get("onboarding_status", "") or "").strip() == "pending_name":
            if self.extract_directory_preferred_name(cleaned_message):
                return
        if self.extract_image_prompt(cleaned_message):
            return
        extracted = self.generator.extract_knowledge(
            event,
            session,
            assistant_name=assistant_name,
            latest_message=cleaned_message,
            conversation_view=conversation_view,
            address=address,
            max_entries=max(1, int(self.config.knowledge_auto_extract_limit)),
            allow_fallback=True,
        )
        entries = extracted.get("entries", []) if isinstance(extracted, dict) else []
        profile_summary = str(extracted.get("profile_summary", "") or "").strip() if isinstance(extracted, dict) else ""
        if not entries and not profile_summary:
            return
        saved_entries: list[dict[str, object]] = []
        for item in entries if isinstance(entries, list) else []:
            saved = self.persist_extracted_knowledge(event, item, message_id=event.message_id)
            if saved:
                saved_entries.append(saved)
        if saved_entries or profile_summary:
            self.update_member_profile_summary(event, extra_summary=profile_summary)

    def persist_extracted_knowledge(
        self,
        event: InboundEvent,
        entry: dict[str, object],
        *,
        message_id: str = "",
    ) -> dict[str, object] | None:
        summary = " ".join(str(entry.get("summary", "") or "").split()).strip()
        if not summary:
            return None
        scope_type, scope_id = self.knowledge_scope_for_candidate(event, entry)
        memory_type = str(entry.get("memory_type", "") or "fact").strip().lower() or "fact"
        tags = [str(tag).strip() for tag in entry.get("tags", []) if str(tag).strip()] if isinstance(entry.get("tags"), list) else []
        confidence = safe_float(entry, "confidence", 0.6)
        source_message_ids = [str(message_id).strip()] if str(message_id or "").strip() else []
        existing = self.existing_knowledge_entry(scope_type, scope_id, summary)
        payload: dict[str, object] = {
            "character_id": self.current_character_id(),
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
            payload["confidence"] = max(confidence, safe_float(existing, "confidence", confidence))
        return self.state_store.save_knowledge(payload)

    def existing_knowledge_entry(self, scope_type: str, scope_id: str, summary: str) -> dict[str, object] | None:
        current_character = self.current_character_id()
        limit = max(
            80,
            int(self.state_store.knowledge_count(character_id=current_character))
            if hasattr(self.state_store, "knowledge_count")
            else 80,
        )
        target = " ".join(str(summary or "").split()).strip().casefold()
        for item in self.state_store.list_knowledge(limit=limit, character_id=current_character):
            if str(item.get("scope_type", "") or "").strip() != scope_type:
                continue
            if str(item.get("scope_id", "") or "").strip() != scope_id:
                continue
            current = " ".join(str(item.get("summary", "") or "").split()).strip().casefold()
            if current == target:
                return item
        return None

    def knowledge_scope_for_candidate(self, event: InboundEvent, entry: dict[str, object]) -> tuple[str, str]:
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
    def _extract_terms(value: object) -> list[str]:
        raw = str(value or "").strip().lower()
        if not raw:
            return []
        cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in raw)
        terms: list[str] = []
        seen: set[str] = set()
        for part in cleaned.split():
            if len(part) < 2 or part in seen:
                continue
            seen.add(part)
            terms.append(part)
        return terms
