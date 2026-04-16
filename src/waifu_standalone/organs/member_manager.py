from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from ..cells.cards import CardManager
from ..cells.generator import Generator
from ..config import AppConfig
from ..models import InboundEvent, OutboundMessage, SessionMemory
from ..organs.memories import Memory
from ..systems.value_game import ValueGameEngine


@dataclass(slots=True)
class MemberManager:
    config: AppConfig
    memory: Memory
    generator: Generator
    cards: CardManager
    state_store: Any
    value_game: ValueGameEngine
    current_character_id: Callable[[], str]
    emit_message: Callable[..., OutboundMessage]
    requires_live_llm: Callable[[], bool]
    should_retry_onboarding_prompt: Callable[[InboundEvent], bool]

    @staticmethod
    def mentions_any_assistant_name(text: str, names: set[str]) -> bool:
        lowered = str(text or "").casefold()
        return any(name and name.casefold() in lowered for name in names)

    @staticmethod
    def split_history_line(line: str) -> tuple[str, str]:
        speaker, sep, content = str(line or "").partition(": ")
        if sep:
            return speaker.strip() or "user", content.strip()
        return "user", str(line or "").strip()

    @staticmethod
    def merge_profile_summary(existing: str, addition: str) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        for candidate in (existing, addition):
            for fragment in str(candidate or "").split(";"):
                cleaned = " ".join(fragment.split()).strip().strip(",")
                if not cleaned:
                    continue
                key = cleaned.casefold()
                if key in seen:
                    continue
                seen.add(key)
                parts.append(cleaned)
        return "; ".join(parts[:3])

    def remember_directory_member(self, event: InboundEvent) -> None:
        self.state_store.record_member_seen(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            qq_nickname=event.sender_name,
        )

    def maybe_handle_member_onboarding(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> OutboundMessage | None:
        allow_fallback = not self.requires_live_llm()
        if event.launcher_type == "person":
            member = self.state_store.get_member(group_id="", user_id=event.sender_id) or {}
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return None
            candidate = self.memory.extract_preferred_name(latest_message)
            if not candidate:
                return None
            reply_text = self.generator.generate_onboarding_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="confirm_name",
                candidate_name=candidate,
                address_override=candidate,
                allow_fallback=allow_fallback,
            )
            if not reply_text:
                return None
            self.state_store.save_member(
                {
                    "group_id": "",
                    "user_id": event.sender_id,
                    "qq_nickname": event.sender_name,
                    "preferred_name": candidate,
                    "onboarding_status": "ready",
                }
            )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=reply_text,
            )
            return self.emit_message(event, message, assistant_name=assistant_name)
        if event.launcher_type != "group":
            return None
        member = self.state_store.get_member(group_id=event.launcher_id, user_id=event.sender_id)
        if member is None:
            return None

        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            return None

        onboarding_status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if onboarding_status == "pending_name":
            candidate = self.extract_directory_preferred_name(latest_message)
            if candidate:
                reply_text = self.generator.generate_onboarding_reply(
                    event,
                    session,
                    assistant_name=assistant_name,
                    stage="confirm_name",
                    candidate_name=candidate,
                    address_override=candidate,
                    allow_fallback=allow_fallback,
                )
                if not reply_text:
                    return None
                self.state_store.save_member(
                    {
                        "group_id": event.launcher_id,
                        "user_id": event.sender_id,
                        "qq_nickname": event.sender_name,
                        "preferred_name": candidate,
                        "onboarding_status": "ready",
                    }
                )
                message = OutboundMessage(
                    launcher_id=event.launcher_id,
                    launcher_type=event.launcher_type,
                    text=reply_text,
                )
                return self.emit_message(event, message, assistant_name=assistant_name)
            if self.should_retry_onboarding_prompt(event):
                reply_text = self.generator.generate_onboarding_reply(
                    event,
                    session,
                    assistant_name=assistant_name,
                    stage="retry_name",
                    allow_fallback=allow_fallback,
                )
                if reply_text:
                    message = OutboundMessage(
                        launcher_id=event.launcher_id,
                        launcher_type=event.launcher_type,
                        text=reply_text,
                    )
                    return self.emit_message(event, message, assistant_name=assistant_name)

        bot_account_id = str(self.config.bot_account_id or "").strip()
        if bot_account_id and event.has_bot_mention(bot_account_id):
            self.state_store.save_member(
                {
                    "group_id": event.launcher_id,
                    "user_id": event.sender_id,
                    "qq_nickname": event.sender_name,
                    "group_card": str(member.get("group_card", "") or ""),
                    "onboarding_status": "pending_name",
                }
            )
            reply_text = self.generator.generate_onboarding_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="ask_name",
                allow_fallback=allow_fallback,
            )
            if not reply_text:
                return None
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=reply_text,
            )
            return self.emit_message(event, message, assistant_name=assistant_name)
        return None

    def extract_directory_preferred_name(self, text: str) -> str:
        candidate = self.memory.extract_preferred_name(text)
        if candidate:
            return candidate
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return ""
        compact = compact.strip(".,!?;:，。！；：“”\"'`()[]{}")
        if not compact or len(compact) > 12:
            return ""
        if " " in compact or any(token in compact for token in ("http://", "https://", "/", "\\")):
            return ""
        if compact.startswith("@"):
            return ""
        blocked_fragments = (
            "什么",
            "玩意",
            "不知道",
            "不想说",
            "不告诉你",
            "秘密",
            "名字",
            "称呼",
            "昵称",
            "怎么叫",
            "算了",
            "是吗",
            "不是",
            "爸爸吗",
            "卧槽",
        )
        lowered = compact.casefold()
        if any(fragment in lowered for fragment in blocked_fragments):
            return ""
        blocked_exact = {
            "你好",
            "哈喽",
            "hello",
            "hi",
            "test",
            "哈哈",
            "呵呵",
            "是吗",
            "不是",
            "爸爸吗",
            "卧槽",
        }
        if lowered in blocked_exact:
            return ""
        return compact
    def member_record(self, event: InboundEvent) -> dict[str, Any] | None:
        return self.state_store.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=self.current_character_id(),
        )

    def directory_member_notes(self, event: InboundEvent) -> list[str]:
        notes: list[str] = []
        current_group = event.launcher_id if event.launcher_type == "group" else ""
        current_character = self.current_character_id()
        self.sanitize_member_persona_state(
            group_id=current_group,
            user_id=event.sender_id,
            character_id=current_character,
        )
        members = self.state_store.list_members(limit=240, character_id=current_character)
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
            bond_stage = self.value_game.bond_stage(affinity_score)
            if str(member.get("user_id", "") or "").strip() == event.sender_id:
                if preferred_name:
                    notes.append(f"{qq_name} prefers to be called {preferred_name}.")
                else:
                    notes.append(f"The current speaker is {qq_name}.")
                notes.append(f"Bond stage with {qq_name}: {bond_stage} ({affinity_score:.2f}).")
            elif preferred_name:
                notes.append(f"{qq_name} is usually addressed as {preferred_name}.")
            profile_summary = self.sanitize_profile_summary_text(profile_summary)
            if profile_summary:
                notes.append(f"Profile summary for {qq_name}: {profile_summary}")
            if onboarding_status and onboarding_status not in {"", "ready"}:
                notes.append(f"{qq_name} is still in onboarding status: {onboarding_status}.")
        return notes

    def known_assistant_aliases(self) -> dict[str, set[str]]:
        aliases: dict[str, set[str]] = {}
        try:
            for item in self.cards.list_characters():
                character_id = str(item.get("character", "") or "").strip()
                if not character_id:
                    continue
                names = aliases.setdefault(character_id, set())
                if bool(item.get("has_person")):
                    try:
                        names.add(
                            str(
                                self.cards.load(
                                    "person",
                                    SessionMemory(
                                        launcher_id="probe",
                                        launcher_type="person",
                                        character_id=character_id,
                                    ),
                                ).assistant_name
                            ).strip()
                        )
                    except Exception:
                        pass
                if bool(item.get("has_group")):
                    try:
                        names.add(
                            str(
                                self.cards.load(
                                    "group",
                                    SessionMemory(
                                        launcher_id="probe",
                                        launcher_type="group",
                                        character_id=character_id,
                                    ),
                                ).assistant_name
                            ).strip()
                        )
                    except Exception:
                        pass
                direct_name = str(item.get("assistant_name", "") or "").strip()
                if direct_name:
                    names.add(direct_name)
        except Exception:
            pass
        current_character = self.current_character_id()
        current_names = aliases.setdefault(current_character, set())
        for launcher_type in ("person", "group"):
            try:
                current_names.add(
                    str(
                        self.cards.load(
                            launcher_type,
                            SessionMemory(
                                launcher_id="probe",
                                launcher_type=launcher_type,
                                character_id=current_character,
                            ),
                        ).assistant_name
                    ).strip()
                )
            except Exception:
                continue
        fallback = str(self.config.assistant_name or "").strip()
        if fallback:
            current_names.add(fallback)
        return {
            character_id: {name for name in names if name}
            for character_id, names in aliases.items()
            if any(name for name in names)
        }

    def known_assistant_names(self) -> dict[str, str]:
        aliases = self.known_assistant_aliases()
        return {
            character_id: sorted(names, key=lambda item: (len(item), item.casefold()))[0]
            for character_id, names in aliases.items()
            if names
        }

    def sanitize_profile_summary_text(self, summary: str) -> str:
        fragments: list[str] = []
        seen: set[str] = set()
        assistant_names: set[str] = set()
        for names in self.known_assistant_aliases().values():
            assistant_names.update(names)
        for fragment in str(summary or "").split(";"):
            cleaned = " ".join(fragment.split()).strip().strip(",")
            if not cleaned:
                continue
            if assistant_names and self.mentions_any_assistant_name(cleaned, assistant_names):
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            fragments.append(cleaned)
        return "; ".join(fragments[:3])

    def sanitize_session_persona_state(
        self,
        session: SessionMemory,
        *,
        assistant_name: str,
    ) -> SessionMemory:
        known_aliases = self.known_assistant_aliases()
        other_names: set[str] = set()
        current_character = str(session.character_id or "").strip()
        for character_id, names in known_aliases.items():
            if character_id == current_character:
                continue
            other_names.update(name for name in names if name and name != assistant_name)
        if not other_names:
            return session
        filtered_history: list[str] = []
        changed = False
        for raw_line in list(session.history):
            speaker, content = self.split_history_line(raw_line)
            speaker_is_assistant = speaker == "assistant" or self.mentions_any_assistant_name(speaker, other_names)
            if speaker_is_assistant and (
                self.mentions_any_assistant_name(speaker, other_names)
                or self.mentions_any_assistant_name(content, other_names)
            ):
                changed = True
                continue
            filtered_history.append(raw_line)
        if not changed:
            return session
        session.history = filtered_history
        self.memory.store.save(session)
        return session

    def sanitize_member_persona_state(
        self,
        *,
        group_id: str,
        user_id: str,
        character_id: str,
    ) -> dict[str, object] | None:
        member = self.state_store.get_member(
            group_id=group_id,
            user_id=user_id,
            character_id=character_id,
        )
        if member is None:
            return None
        cleaned_summary = self.sanitize_profile_summary_text(str(member.get("profile_summary", "") or ""))
        if cleaned_summary == str(member.get("profile_summary", "") or ""):
            return member
        return self.state_store.save_member(
            {
                "group_id": group_id,
                "user_id": user_id,
                "character_id": character_id,
                "qq_nickname": str(member.get("qq_nickname", "") or ""),
                "group_card": str(member.get("group_card", "") or ""),
                "preferred_name": str(member.get("preferred_name", "") or ""),
                "onboarding_status": str(member.get("onboarding_status", "") or "new"),
                "membership_status": str(member.get("membership_status", "") or "active"),
                "profile_summary": cleaned_summary,
                "affinity_score": member.get("affinity_score", 0.0),
                "notes_count": int(member.get("notes_count", 0) or 0),
                "last_seen_at": int(member.get("last_seen_at", 0) or 0),
                "last_sync_at": int(member.get("last_sync_at", 0) or 0),
                "last_addressed_at": int(member.get("last_addressed_at", 0) or 0),
            }
        )

    def update_member_profile_summary(self, event: InboundEvent, *, extra_summary: str = "") -> None:
        group_id = event.launcher_id if event.launcher_type == "group" else ""
        current_character = self.current_character_id()
        member = self.sanitize_member_persona_state(
            group_id=group_id,
            user_id=event.sender_id,
            character_id=current_character,
        )
        if member is None:
            return
        scope_id = f"{event.launcher_id}:{event.sender_id}" if event.launcher_type == "group" else event.sender_id
        notes_count = 0
        count_scope = getattr(self.state_store, "count_knowledge_for_scope", None)
        if callable(count_scope):
            notes_count = int(count_scope("member", scope_id, character_id=current_character) or 0)
        merged_summary = self.merge_profile_summary(
            self.sanitize_profile_summary_text(str(member.get("profile_summary", "") or "")),
            self.sanitize_profile_summary_text(extra_summary),
        )
        self.state_store.save_member(
            {
                "group_id": group_id,
                "user_id": event.sender_id,
                "character_id": current_character,
                "qq_nickname": str(member.get("qq_nickname", "") or event.sender_name),
                "group_card": str(member.get("group_card", "") or ""),
                "preferred_name": str(member.get("preferred_name", "") or ""),
                "onboarding_status": str(member.get("onboarding_status", "") or "new"),
                "membership_status": str(member.get("membership_status", "") or "active"),
                "profile_summary": merged_summary,
                "affinity_score": member.get("affinity_score", 0.0),
                "notes_count": notes_count,
                "last_seen_at": member.get("last_seen_at", 0),
                "last_sync_at": member.get("last_sync_at", 0),
                "last_addressed_at": member.get("last_addressed_at", 0),
            }
        )

