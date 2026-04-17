"""Member onboarding: ask for name, confirm it, save to directory.

Extracted from :mod:`waifu_standalone.app`. Handles the flow where a new
group member (or DM) is asked for a preferred name, and the confirmation
hand-off when they reply.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from .models import InboundEvent, OutboundMessage, SessionMemory

if TYPE_CHECKING:
    from .app import WaifuService


class MemberOnboarding:
    _BARE_ALIAS_LATIN_RE = re.compile(r"^[A-Za-z0-9_]{1,12}$")
    _BARE_ALIAS_CJK_RE = re.compile("^[\u4e00-\u9fff]{1,4}$")
    _ADDRESS_COMMAND_PATTERNS = (
        re.compile(
            "^(?:/|!|\uFF01)?(?:\u53EB\u6211|\u558A\u6211|\u5C31\u53EB\u6211|\u8BF7\u53EB\u6211|\u4F60\u5C31\u53EB\u6211|\u79F0\u547C\u6211|\u79F0\u547C)\s*[:\uFF1A]?\s*(\S{1,12})$"
        ),
        re.compile("^(?:/|!|\uFF01)?call\s+me\s+([A-Za-z0-9_]{1,12})$", flags=re.IGNORECASE),
    )
    _BARE_ALIAS_BLOCKED_EXACT = {
        "hello",
        "hi",
        "hey",
        "help",
        "\u4F60\u597D",
        "\u60A8\u597D",
        "\u5728\u5417",
        "\u5728\u561B",
        "\u662F\u5417",
        "\u4E0D\u662F",
        "\u5367\u69FD",
        "\u6280\u80FD\u83DC\u5355",
        "\u603B\u7ED3\u4E00\u4E0B",
    }
    _BARE_ALIAS_BLOCKED_FRAGMENTS = (
        "\u4EC0\u4E48",
        "\u73A9\u610F",
        "\u4E1C\u897F",
        "\u600E\u4E48",
        "\u4E3A\u5565",
        "\u5E72\u561B",
        "\u4F60\u662F",
        "\u4E0D\u662F",
        "\u673A\u68B0",
        "\u53EB\u4ED6",
        "\u53EB\u5979",
        "\u53EB\u4F60",
        "\u53EB\u673A\u5668\u4EBA",
        "\u79F0\u547C",
        "\u603B\u7ED3",
        "\u6280\u80FD",
        "\u83DC\u5355",
        "\u5E2E\u6211",
        "\u67E5\u4E00\u4E0B",
        "\u67E5\u67E5",
    )
    _BARE_ALIAS_FORBIDDEN_CHARS = set(
        "\u6211\u4F60\u4ED6\u5979\u5B83\u4EEC\u8FD9\u90A3\u54EA\u53EB\u662F\u7684\u4E86\u5417\u5462\u5427\u554A"
    )
    _NAME_RETRY_HINTS = (
        "\u53EB\u6211",
        "\u600E\u4E48\u53EB",
        "\u600E\u4E48\u79F0\u547C",
        "\u79F0\u547C",
        "\u6211\u53EB",
        "\u6211\u662F",
        "call me",
        "my name",
    )
    _NAME_HINT_MIN_CONFIDENCE = 0.85

    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    def remember_directory_member(self, event: InboundEvent) -> None:
        self._service.state_store.record_member_seen(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            qq_nickname=event.sender_name,
        )

    async def aremember_directory_member(self, event: InboundEvent) -> None:
        await asyncio.to_thread(self.remember_directory_member, event)

    def maybe_handle(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> OutboundMessage | None:
        allow_fallback = not self._service._requires_live_llm()
        command_candidate = self.extract_command_preferred_name(latest_message)
        if event.launcher_type == "person":
            member = self._service.state_store.get_member(group_id="", user_id=event.sender_id) or {}
            if command_candidate:
                return self._confirm_preferred_name(
                    event,
                    session,
                    assistant_name=assistant_name,
                    candidate=command_candidate,
                    member=member,
                    allow_fallback=allow_fallback,
                )
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return None
            candidate = self.extract_explicit_preferred_name(latest_message)
            if not candidate:
                return None
            return self._confirm_preferred_name(
                event,
                session,
                assistant_name=assistant_name,
                candidate=candidate,
                member=member,
                allow_fallback=allow_fallback,
            )

        if event.launcher_type != "group":
            return None
        member = self._service.state_store.get_member(group_id=event.launcher_id, user_id=event.sender_id)
        if member is None:
            return None
        if command_candidate:
            return self._confirm_preferred_name(
                event,
                session,
                assistant_name=assistant_name,
                candidate=command_candidate,
                member=member,
                allow_fallback=allow_fallback,
            )

        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            return None

        onboarding_status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if onboarding_status == "pending_name":
            candidate = self.extract_preferred_name(latest_message)
            if not candidate:
                candidate = self._infer_preferred_name_candidate(
                    event,
                    session,
                    latest_message=latest_message,
                    assistant_name=assistant_name,
                )
            if candidate:
                return self._confirm_preferred_name(
                    event,
                    session,
                    assistant_name=assistant_name,
                    candidate=candidate,
                    member=member,
                    allow_fallback=allow_fallback,
                )
            if self.should_retry_prompt(event, latest_message=latest_message):
                reply_text = self._service.generator.generate_onboarding_reply(
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
                    return self._service.emitter.emit(event, message, assistant_name=assistant_name)
            return None

        bot_account_id = str(self._service.config.bot_account_id or "").strip()
        if bot_account_id and event.has_bot_mention(bot_account_id):
            self._service.state_store.save_member(
                {
                    "group_id": event.launcher_id,
                    "user_id": event.sender_id,
                    "qq_nickname": event.sender_name,
                    "group_card": str(member.get("group_card", "") or ""),
                    "onboarding_status": "pending_name",
                }
            )
            reply_text = self._service.generator.generate_onboarding_reply(
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
            return self._service.emitter.emit(event, message, assistant_name=assistant_name)
        return None

    async def amaybe_handle(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> OutboundMessage | None:
        allow_fallback = not self._service._requires_live_llm()
        command_candidate = self.extract_command_preferred_name(latest_message)
        if event.launcher_type == "person":
            member = await asyncio.to_thread(
                self._service.state_store.get_member,
                group_id="",
                user_id=event.sender_id,
            ) or {}
            if command_candidate:
                return await self._aconfirm_preferred_name(
                    event,
                    session,
                    assistant_name=assistant_name,
                    candidate=command_candidate,
                    member=member,
                    allow_fallback=allow_fallback,
                )
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return None
            candidate = self.extract_explicit_preferred_name(latest_message)
            if not candidate:
                return None
            return await self._aconfirm_preferred_name(
                event,
                session,
                assistant_name=assistant_name,
                candidate=candidate,
                member=member,
                allow_fallback=allow_fallback,
            )

        if event.launcher_type != "group":
            return None
        member = await asyncio.to_thread(
            self._service.state_store.get_member,
            group_id=event.launcher_id,
            user_id=event.sender_id,
        )
        if member is None:
            return None
        if command_candidate:
            return await self._aconfirm_preferred_name(
                event,
                session,
                assistant_name=assistant_name,
                candidate=command_candidate,
                member=member,
                allow_fallback=allow_fallback,
            )

        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            return None

        onboarding_status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if onboarding_status == "pending_name":
            candidate = self.extract_preferred_name(latest_message)
            if not candidate:
                candidate = await self._ainfer_preferred_name_candidate(
                    event,
                    session,
                    latest_message=latest_message,
                    assistant_name=assistant_name,
                )
            if candidate:
                return await self._aconfirm_preferred_name(
                    event,
                    session,
                    assistant_name=assistant_name,
                    candidate=candidate,
                    member=member,
                    allow_fallback=allow_fallback,
                )
            if self.should_retry_prompt(event, latest_message=latest_message):
                reply_text = await self._service.generator.agenerate_onboarding_reply(
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
                    return await self._service.emitter.aemit(event, message, assistant_name=assistant_name)
            return None

        bot_account_id = str(self._service.config.bot_account_id or "").strip()
        if bot_account_id and event.has_bot_mention(bot_account_id):
            await asyncio.to_thread(
                self._service.state_store.save_member,
                {
                    "group_id": event.launcher_id,
                    "user_id": event.sender_id,
                    "qq_nickname": event.sender_name,
                    "group_card": str(member.get("group_card", "") or ""),
                    "onboarding_status": "pending_name",
                },
            )
            reply_text = await self._service.generator.agenerate_onboarding_reply(
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
            return await self._service.emitter.aemit(event, message, assistant_name=assistant_name)
        return None

    def _normalize_candidate(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return ""
        return compact.strip(".,!?;:\uFF0C\u3002\uFF01\uFF1F\uFF1B\uFF1A\u201C\u201D\"'`()[]{}<>")

    def _looks_like_general_name(self, candidate: str) -> bool:
        if not candidate or len(candidate) > 12:
            return False
        if " " in candidate or candidate.startswith("@"):
            return False
        if any(token in candidate for token in ("http://", "https://", "/", "\\")):
            return False
        return candidate.casefold() not in self._BARE_ALIAS_BLOCKED_EXACT

    def _looks_like_bare_alias(self, candidate: str) -> bool:
        if not self._looks_like_general_name(candidate):
            return False
        if any(fragment in candidate for fragment in self._BARE_ALIAS_BLOCKED_FRAGMENTS):
            return False
        if self._BARE_ALIAS_LATIN_RE.fullmatch(candidate):
            return True
        if not self._BARE_ALIAS_CJK_RE.fullmatch(candidate):
            return False
        return all(char not in self._BARE_ALIAS_FORBIDDEN_CHARS for char in candidate)

    def looks_like_address_command(self, text: str) -> bool:
        return bool(self.extract_command_preferred_name(text))

    def extract_command_preferred_name(self, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        normalized_raw = re.sub(r"\s+", "", raw)
        for pattern in self._ADDRESS_COMMAND_PATTERNS:
            match = pattern.fullmatch(raw) or pattern.fullmatch(normalized_raw)
            if not match:
                continue
            candidate = self._normalize_candidate(match.group(1))
            if self._looks_like_general_name(candidate):
                return candidate
        return ""

    def extract_explicit_preferred_name(self, text: str) -> str:
        candidate = self._service.memory.extract_preferred_name(text)
        if candidate and self._looks_like_general_name(candidate):
            return candidate
        return ""

    def extract_preferred_name(self, text: str) -> str:
        candidate = self.extract_explicit_preferred_name(text)
        if candidate:
            return candidate
        normalized = self._normalize_candidate(text)
        if self._looks_like_bare_alias(normalized):
            return normalized
        return ""

    def _candidate_from_hint(self, hint: object) -> str:
        if not isinstance(hint, dict):
            return ""
        if not bool(hint.get("is_self_intro")):
            return ""
        try:
            confidence = float(hint.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            return ""
        if confidence < self._NAME_HINT_MIN_CONFIDENCE:
            return ""
        candidate = self._normalize_candidate(str(hint.get("name", "") or ""))
        if not self._looks_like_general_name(candidate):
            return ""
        return candidate

    def _infer_preferred_name_candidate(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> str:
        hint = self._service.generator.extract_preferred_name_hint(
            event,
            session,
            assistant_name=assistant_name,
            latest_message=latest_message,
        )
        return self._candidate_from_hint(hint)

    async def _ainfer_preferred_name_candidate(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> str:
        hint = await self._service.generator.aextract_preferred_name_hint(
            event,
            session,
            assistant_name=assistant_name,
            latest_message=latest_message,
        )
        return self._candidate_from_hint(hint)

    def _member_payload(
        self,
        event: InboundEvent,
        member: dict[str, Any] | None,
        *,
        candidate: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "group_id": event.launcher_id if event.launcher_type == "group" else "",
            "user_id": event.sender_id,
            "qq_nickname": event.sender_name,
            "preferred_name": candidate,
            "onboarding_status": "ready",
        }
        if event.launcher_type == "group":
            payload["group_card"] = str((member or {}).get("group_card", "") or "")
        return payload

    def _confirm_preferred_name(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        candidate: str,
        member: dict[str, Any] | None,
        allow_fallback: bool,
    ) -> OutboundMessage | None:
        reply_text = self._service.generator.generate_onboarding_reply(
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
        self._service.state_store.save_member(self._member_payload(event, member, candidate=candidate))
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return self._service.emitter.emit(event, message, assistant_name=assistant_name)

    async def _aconfirm_preferred_name(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        candidate: str,
        member: dict[str, Any] | None,
        allow_fallback: bool,
    ) -> OutboundMessage | None:
        reply_text = await self._service.generator.agenerate_onboarding_reply(
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
        await asyncio.to_thread(
            self._service.state_store.save_member,
            self._member_payload(event, member, candidate=candidate),
        )
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return await self._service.emitter.aemit(event, message, assistant_name=assistant_name)

    def should_retry_prompt(self, event: InboundEvent, *, latest_message: str = "") -> bool:
        if event.launcher_type != "group":
            return False
        bot_account_id = str(self._service.config.bot_account_id or "").strip()
        if not (bot_account_id and event.has_bot_mention(bot_account_id)):
            return False
        cleaned = self._normalize_candidate(latest_message)
        if not cleaned:
            return True
        lowered = cleaned.casefold()
        return any(marker in cleaned for marker in self._NAME_RETRY_HINTS) or any(
            marker in lowered for marker in ("call me", "my name")
        )
