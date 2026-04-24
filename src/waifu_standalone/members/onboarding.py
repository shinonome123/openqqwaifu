"""Member onboarding and naming flows.

This module handles:

- Explicit self-naming: ``叫我 xx`` / ``call me xx``
- Explicit assistant naming: ``以后就叫你 xx``
- Rejection of third-party naming attempts
- Clarification replies for naming-related questions
- A single soft follow-up after the first normal reply
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from ..models import InboundEvent, OutboundMessage, SessionMemory
from ..skills import ToolExecutionResult, ToolInvocation

if TYPE_CHECKING:
    from ..app import WaifuService


class MemberOnboarding:
    _BARE_ALIAS_LATIN_RE = re.compile(r"^[A-Za-z0-9_]{1,12}$")
    _BARE_ALIAS_CJK_RE = re.compile("^[\u4e00-\u9fff]{1,4}$")
    _ADDRESS_COMMAND_PATTERNS = (
        re.compile(
            "^(?:/|!|\uFF01)?(?:\u53EB\u6211|\u558A\u6211|\u5C31\u53EB\u6211|\u8BF7\u53EB\u6211|\u4F60\u5C31\u53EB\u6211|\u79F0\u547C\u6211|\u79F0\u547C)\\s*[:\uFF1A]?\\s*(\\S{1,12})$"
        ),
        re.compile("^(?:/|!|\uFF01)?call\\s+me\\s+([A-Za-z0-9_]{1,12})$", flags=re.IGNORECASE),
    )
    _ASSISTANT_ALIAS_COMMAND_PATTERNS = (
        re.compile(
            "^(?:/|!|\uFF01)?(?:\u4EE5\u540E\u5C31\u53EB\u4F60|\u6211\u4EE5\u540E\u53EB\u4F60|\u90A3\u6211\u5C31\u53EB\u4F60|\u90A3\u5C31\u53EB\u4F60|\u4EE5\u540E\u53EB\u4F60|\u5C31\u53EB\u4F60)\\s*[:\uFF1A]?\\s*(\\S{1,12})$"
        ),
        re.compile(
            "^(?:/|!|\uFF01)?(?:i(?:'|’)ll\\s+call\\s+you|i\\s+will\\s+call\\s+you|call\\s+you)\\s+([A-Za-z0-9_]{1,12})$",
            flags=re.IGNORECASE,
        ),
    )
    _THIRD_PARTY_NAMING_PATTERNS = (
        re.compile("^(?:/|!|\uFF01)?(?:\u53EB|\u558A|\u79F0\u547C)(?:\u4ED6|\u5979|ta|TA|\u5B83)\\S{0,12}$"),
        re.compile(
            "^(?:/|!|\uFF01)?(?:\u4EE5\u540E)?(?:\u4F60|\u90A3\u4F60)(?:\u4EE5\u540E)?(?:\u5C31)?(?:\u53EB|\u558A|\u79F0\u547C)(?:\u4ED6|\u5979|ta|TA|\u5B83)\\S{0,12}$"
        ),
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
    _NAME_HINT_MIN_CONFIDENCE = 0.85
    _PASSIVE_CAPTURE_STATUSES = {"asked_once", "pending_name"}
    _TRAILING_PARTICLES = set("\u4E86\u5462\u5440\u554A\u54E6\u5427\u561B\u5566")

    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    def remember_directory_member(self, event: InboundEvent) -> None:
        self._service.member_repository.record_member_seen(
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
        member = self._current_member(event)
        return self._maybe_handle_common(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )

    async def amaybe_handle(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
    ) -> OutboundMessage | None:
        member = await asyncio.to_thread(self._current_member, event)
        return await self._amaybe_handle_common(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )

    def maybe_append_soft_ask(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        reply_text: str,
        character_id: str = "",
    ) -> str:
        member = self._current_member(event)
        return self._maybe_append_soft_ask_common(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            reply_text=reply_text,
            character_id=character_id,
            member=member,
        )

    async def amaybe_append_soft_ask(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        reply_text: str,
        character_id: str = "",
    ) -> str:
        member = await asyncio.to_thread(self._current_member, event)
        return await self._amaybe_append_soft_ask_common(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            reply_text=reply_text,
            character_id=character_id,
            member=member,
        )

    def looks_like_address_command(self, text: str) -> bool:
        return bool(self.extract_command_preferred_name(text))

    def looks_like_direct_group_naming_request(self, text: str) -> bool:
        if self._asks_for_assistant_name(text):
            return False
        return any(
            (
                bool(self.extract_command_preferred_name(text)),
                bool(self.extract_assistant_alias_command(text)),
                self._is_third_party_naming_request(text),
                bool(self._clarify_naming_intent(text)),
            )
        )

    def looks_like_naming_input(self, text: str) -> bool:
        if self._asks_for_assistant_name(text):
            return False
        return any(
            (
                self.looks_like_direct_group_naming_request(text),
                bool(self.extract_explicit_preferred_name(text)),
            )
        )

    def extract_command_preferred_name(self, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        normalized_raw = re.sub(r"\s+", "", raw)
        for pattern in self._ADDRESS_COMMAND_PATTERNS:
            match = pattern.fullmatch(raw) or pattern.fullmatch(normalized_raw)
            if not match:
                continue
            candidate = self._normalize_name_candidate(match.group(1))
            if self._looks_like_general_name(candidate):
                return candidate
        return ""

    def extract_assistant_alias_command(self, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        normalized_raw = re.sub(r"\s+", "", raw)
        for pattern in self._ASSISTANT_ALIAS_COMMAND_PATTERNS:
            match = pattern.fullmatch(raw) or pattern.fullmatch(normalized_raw)
            if not match:
                continue
            candidate = self._normalize_name_candidate(match.group(1))
            if self._looks_like_general_name(candidate):
                return candidate
        return ""

    def extract_explicit_preferred_name(self, text: str) -> str:
        candidate = self._service.memory.extract_preferred_name(text)
        candidate = self._normalize_name_candidate(candidate)
        if candidate and self._looks_like_general_name(candidate):
            return candidate
        return ""

    def extract_preferred_name(self, text: str) -> str:
        candidate = self.extract_explicit_preferred_name(text)
        if candidate:
            return candidate
        normalized = self._normalize_name_candidate(text)
        if self._looks_like_bare_alias(normalized):
            return normalized
        return ""

    def use_set_preferred_name_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        candidate = self._name_argument(invocation, "preferred_name", "name")
        if not candidate:
            return ToolExecutionResult(error="missing preferred_name", text="请提供要保存的用户称呼。")
        if not self._is_naming_route_eligible(invocation.event):
            return ToolExecutionResult(
                error="naming requires mention in group chat",
                text="群聊里设置称呼必须先 @ 机器人。",
            )
        member = self._current_member(invocation.event)
        self._service.member_repository.save_member(
            self._member_payload(invocation.event, member, candidate=candidate, onboarding_status="ready")
        )
        return ToolExecutionResult(
            text=f"已保存用户称呼：{candidate}。最终回复必须明确表达“好的，我叫你 {candidate}”。",
            metadata={"preferred_name": candidate, "naming_action": "set_preferred_name"},
        )

    async def ause_set_preferred_name_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_set_preferred_name_tool, invocation)

    def use_set_assistant_alias_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        candidate = self._name_argument(invocation, "assistant_alias", "alias", "name")
        if not candidate:
            return ToolExecutionResult(error="missing assistant_alias", text="请提供要保存的机器人称呼。")
        if not self._is_naming_route_eligible(invocation.event):
            return ToolExecutionResult(
                error="naming requires mention in group chat",
                text="群聊里给机器人起别名必须先 @ 机器人。",
            )
        self._service.member_repository.save_assistant_alias(
            character_id=str(invocation.session.character_id or self._service._active_character_id()).strip(),
            user_id=invocation.event.sender_id,
            assistant_alias=candidate,
        )
        return ToolExecutionResult(
            text=f"已保存当前用户对机器人的称呼：{candidate}。最终回复必须明确表达“好的，那就叫我 {candidate}”。",
            metadata={"assistant_alias": candidate, "naming_action": "set_assistant_alias"},
        )

    async def ause_set_assistant_alias_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_set_assistant_alias_tool, invocation)

    def use_reject_third_party_naming_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        del invocation
        return ToolExecutionResult(
            text="我只能接受你给你自己定称呼，也只能接受别人亲自来给我定称呼，不能替别人决定怎么叫哦。",
            metadata={"naming_action": "reject_third_party_naming"},
        )

    async def ause_reject_third_party_naming_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_reject_third_party_naming_tool, invocation)

    def use_clarify_naming_intent_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        intent = str(invocation.argument("intent", "") or invocation.argument("kind", "") or "").strip()
        if intent not in {"user_name", "assistant_alias"}:
            intent = "user_name"
        if intent == "assistant_alias":
            text = "如果用户想给机器人起称呼，引导用户直接说“以后就叫你 xx”。不要保存任何别名。"
        else:
            text = "如果用户想让机器人换个叫法，引导用户直接说“叫我 xx”。不要保存任何用户称呼。"
        return ToolExecutionResult(text=text, metadata={"naming_action": "clarify_naming_intent", "intent": intent})

    async def ause_clarify_naming_intent_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_clarify_naming_intent_tool, invocation)

    def _name_argument(self, invocation: ToolInvocation, *names: str) -> str:
        for name in names:
            candidate = self._normalize_name_candidate(str(invocation.argument(name, "") or ""))
            if self._looks_like_general_name(candidate):
                return candidate
        candidate = self._normalize_name_candidate(invocation.raw_args)
        if self._looks_like_general_name(candidate):
            return candidate
        return ""

    def _current_member(self, event: InboundEvent) -> dict[str, Any]:
        return self._service.member_repository.get_member(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
        ) or {}

    def _maybe_handle_common(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> OutboundMessage | None:
        if not self._is_naming_route_eligible(event):
            return None
        allow_fallback = True
        action, payload = self._resolve_naming_action(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )
        if action == "set_preferred_name" and payload:
            return self._confirm_preferred_name(
                event,
                session,
                assistant_name=assistant_name,
                candidate=payload,
                member=member,
                allow_fallback=allow_fallback,
            )
        if action == "set_assistant_alias" and payload:
            return self._confirm_assistant_alias(
                event,
                session,
                assistant_name=assistant_name,
                candidate=payload,
                allow_fallback=allow_fallback,
            )
        if action == "reject_third_party_naming":
            return self._emit_stage_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="reject_third_party_naming",
                allow_fallback=allow_fallback,
            )
        if action == "clarify_naming_intent" and payload:
            return self._emit_stage_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="clarify_naming_intent",
                allow_fallback=allow_fallback,
                intent_hint=payload,
            )
        return None

    async def _amaybe_handle_common(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> OutboundMessage | None:
        if not self._is_naming_route_eligible(event):
            return None
        allow_fallback = True
        action, payload = await self._aresolve_naming_action(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )
        if action == "set_preferred_name" and payload:
            return await self._aconfirm_preferred_name(
                event,
                session,
                assistant_name=assistant_name,
                candidate=payload,
                member=member,
                allow_fallback=allow_fallback,
            )
        if action == "set_assistant_alias" and payload:
            return await self._aconfirm_assistant_alias(
                event,
                session,
                assistant_name=assistant_name,
                candidate=payload,
                allow_fallback=allow_fallback,
            )
        if action == "reject_third_party_naming":
            return await self._aemit_stage_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="reject_third_party_naming",
                allow_fallback=allow_fallback,
            )
        if action == "clarify_naming_intent" and payload:
            return await self._aemit_stage_reply(
                event,
                session,
                assistant_name=assistant_name,
                stage="clarify_naming_intent",
                allow_fallback=allow_fallback,
                intent_hint=payload,
            )
        return None

    def _maybe_append_soft_ask_common(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        reply_text: str,
        character_id: str,
        member: dict[str, Any],
    ) -> str:
        cleaned_reply = str(reply_text or "").strip()
        if not cleaned_reply:
            return ""
        if self.looks_like_naming_input(latest_message):
            return cleaned_reply
        if str(member.get("preferred_name", "") or "").strip():
            return cleaned_reply
        status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if status != "new":
            return cleaned_reply
        address = self._resolved_address(event, session, member=member)
        soft_ask = self._service.generator.generate_onboarding_reply(
            event,
            session,
            assistant_name=assistant_name,
            stage="soft_ask_name",
            address_override=address,
            allow_fallback=True,
        )
        if not soft_ask:
            return cleaned_reply
        self._service.member_repository.save_member(
            self._member_payload(
                event,
                member,
                onboarding_status="asked_once",
            )
        )
        return self._append_soft_ask(cleaned_reply, soft_ask)

    async def _amaybe_append_soft_ask_common(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        reply_text: str,
        character_id: str,
        member: dict[str, Any],
    ) -> str:
        cleaned_reply = str(reply_text or "").strip()
        if not cleaned_reply:
            return ""
        if self.looks_like_naming_input(latest_message):
            return cleaned_reply
        if str(member.get("preferred_name", "") or "").strip():
            return cleaned_reply
        status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if status != "new":
            return cleaned_reply
        address = self._resolved_address(event, session, member=member)
        soft_ask = await self._service.generator.agenerate_onboarding_reply(
            event,
            session,
            assistant_name=assistant_name,
            stage="soft_ask_name",
            address_override=address,
            allow_fallback=True,
        )
        if not soft_ask:
            return cleaned_reply
        await asyncio.to_thread(
            self._service.member_repository.save_member,
            self._member_payload(
                event,
                member,
                onboarding_status="asked_once",
            ),
        )
        return self._append_soft_ask(cleaned_reply, soft_ask)

    def _resolve_naming_action(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> tuple[str, str]:
        routed_action, routed_payload = self._resolve_routed_naming_action(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )
        if routed_action:
            return routed_action, routed_payload
        return self._fallback_naming_action(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )

    async def _aresolve_naming_action(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> tuple[str, str]:
        routed_action, routed_payload = await self._aresolve_routed_naming_action(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )
        if routed_action:
            return routed_action, routed_payload
        return await self._afallback_naming_action(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            member=member,
        )

    def _resolve_routed_naming_action(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> tuple[str, str]:
        status = str(member.get("onboarding_status", "") or "").strip() or "new"
        preferred_name = str(member.get("preferred_name", "") or "").strip()
        passive_capture_allowed = self._allows_passive_capture(member)
        route = self._service.naming_router.route(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            onboarding_status=status,
            preferred_name=preferred_name,
            passive_capture_allowed=passive_capture_allowed,
        )
        return self._normalize_routed_naming_action(route.mode, route.preferred_name, route.assistant_alias, route.clarification_text)

    async def _aresolve_routed_naming_action(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> tuple[str, str]:
        status = str(member.get("onboarding_status", "") or "").strip() or "new"
        preferred_name = str(member.get("preferred_name", "") or "").strip()
        passive_capture_allowed = self._allows_passive_capture(member)
        route = await self._service.naming_router.aroute(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            onboarding_status=status,
            preferred_name=preferred_name,
            passive_capture_allowed=passive_capture_allowed,
        )
        return self._normalize_routed_naming_action(route.mode, route.preferred_name, route.assistant_alias, route.clarification_text)

    def _normalize_routed_naming_action(
        self,
        mode: str,
        preferred_name: str,
        assistant_alias: str,
        clarification_text: str,
    ) -> tuple[str, str]:
        if mode == "set_preferred_name":
            candidate = self._normalize_name_candidate(preferred_name)
            if self._looks_like_general_name(candidate):
                return "set_preferred_name", candidate
            return "", ""
        if mode == "set_assistant_alias":
            candidate = self._normalize_name_candidate(assistant_alias)
            if self._looks_like_general_name(candidate):
                return "set_assistant_alias", candidate
            return "", ""
        if mode == "reject_third_party_naming":
            return "reject_third_party_naming", ""
        if mode == "clarify_naming_intent":
            hint = self._normalize_clarification_hint(clarification_text)
            if hint:
                return "clarify_naming_intent", hint
        return "", ""

    def _fallback_naming_action(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> tuple[str, str]:
        command_candidate = self.extract_command_preferred_name(latest_message)
        if command_candidate:
            return "set_preferred_name", command_candidate

        assistant_candidate = self.extract_assistant_alias_command(latest_message)
        if assistant_candidate:
            return "set_assistant_alias", assistant_candidate

        if self._is_third_party_naming_request(latest_message):
            return "reject_third_party_naming", ""

        clarify_hint = self._clarify_naming_intent(latest_message)
        if clarify_hint:
            return "clarify_naming_intent", clarify_hint

        explicit_candidate = self.extract_explicit_preferred_name(latest_message)
        if explicit_candidate and self._allows_unsolicited_explicit_name(event, member):
            return "set_preferred_name", explicit_candidate

        if not self._allows_passive_capture(member):
            return "", ""

        passive_candidate = self.extract_preferred_name(latest_message)
        if passive_candidate:
            return "set_preferred_name", passive_candidate
        inferred_candidate = self._infer_preferred_name_candidate(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
        )
        if inferred_candidate:
            return "set_preferred_name", inferred_candidate
        return "", ""

    async def _afallback_naming_action(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        latest_message: str,
        assistant_name: str,
        member: dict[str, Any],
    ) -> tuple[str, str]:
        command_candidate = self.extract_command_preferred_name(latest_message)
        if command_candidate:
            return "set_preferred_name", command_candidate

        assistant_candidate = self.extract_assistant_alias_command(latest_message)
        if assistant_candidate:
            return "set_assistant_alias", assistant_candidate

        if self._is_third_party_naming_request(latest_message):
            return "reject_third_party_naming", ""

        clarify_hint = self._clarify_naming_intent(latest_message)
        if clarify_hint:
            return "clarify_naming_intent", clarify_hint

        explicit_candidate = self.extract_explicit_preferred_name(latest_message)
        if explicit_candidate and self._allows_unsolicited_explicit_name(event, member):
            return "set_preferred_name", explicit_candidate

        if not self._allows_passive_capture(member):
            return "", ""

        passive_candidate = self.extract_preferred_name(latest_message)
        if passive_candidate:
            return "set_preferred_name", passive_candidate
        inferred_candidate = await self._ainfer_preferred_name_candidate(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
        )
        if inferred_candidate:
            return "set_preferred_name", inferred_candidate
        return "", ""

    @classmethod
    def _normalize_name_candidate(cls, text: str) -> str:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return ""
        cleaned = compact.strip(".,!?;:\uFF0C\u3002\uFF01\uFF1F\uFF1B\uFF1A\u201C\u201D\"'`()[]{}<>")
        if len(cleaned) > 1 and cleaned[-1] in cls._TRAILING_PARTICLES:
            cleaned = cleaned[:-1].rstrip()
        return cleaned

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

    def _allows_passive_capture(self, member: dict[str, Any]) -> bool:
        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            return False
        status = str(member.get("onboarding_status", "") or "").strip() or "new"
        return status in self._PASSIVE_CAPTURE_STATUSES

    def _allows_unsolicited_explicit_name(
        self,
        event: InboundEvent,
        member: dict[str, Any],
    ) -> bool:
        if event.launcher_type != "group":
            return True
        bot_account_id = str(self._service.config.bot_account_id or "").strip()
        return bool(bot_account_id and event.has_bot_mention(bot_account_id))

    def _is_naming_route_eligible(self, event: InboundEvent) -> bool:
        if event.launcher_type != "group":
            return True
        bot_account_id = str(self._service.config.bot_account_id or "").strip()
        return bool(bot_account_id and event.has_bot_mention(bot_account_id))

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
        candidate = self._normalize_name_candidate(str(hint.get("name", "") or ""))
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

    def _is_third_party_naming_request(self, text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        normalized_raw = re.sub(r"\s+", "", raw)
        return any(
            pattern.fullmatch(raw) or pattern.fullmatch(normalized_raw)
            for pattern in self._THIRD_PARTY_NAMING_PATTERNS
        )

    def _clarify_naming_intent(self, text: str) -> str:
        compact = re.sub(r"\s+", "", str(text or "")).strip()
        lowered = compact.casefold()
        if not compact:
            return ""
        if self._asks_for_assistant_name(compact):
            return ""
        user_name_markers = (
            "\u4F60\u8BE5\u53EB\u6211\u4EC0\u4E48",
            "\u4F60\u60F3\u600E\u4E48\u53EB\u6211",
            "\u4F60\u600E\u4E48\u53EB\u6211",
            "\u4F60\u600E\u4E48\u79F0\u547C\u6211",
            "\u4F60\u5E94\u8BE5\u600E\u4E48\u79F0\u547C\u6211",
        )
        assistant_name_markers = (
            "\u522B\u4EBA\u90FD\u53EB\u4F60",
            "\u4F60\u53EB",
            "\u4F60\u662F\u4E0D\u662F\u53EB",
            "\u5927\u5BB6\u90FD\u53EB\u4F60",
        )
        if any(marker in compact for marker in user_name_markers):
            return "user_name"
        if "whatshouldyoucallme" in lowered or "howshouldyoucallme" in lowered:
            return "user_name"
        if any(marker in compact for marker in assistant_name_markers) and compact.endswith(("\u5417", "?", "\uFF1F")):
            return "assistant_alias"
        if "callyou" in lowered and lowered.endswith("?"):
            return "assistant_alias"
        return ""

    @staticmethod
    def _asks_for_assistant_name(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "")).strip()
        lowered = compact.casefold()
        if not compact:
            return False
        markers = (
            "\u4F60\u53EB\u4EC0\u4E48",
            "\u4F60\u53EB\u5565",
            "\u4F60\u53EB\u5565\u540D\u5B57",
            "\u4F60\u53EB\u4EC0\u4E48\u540D\u5B57",
            "\u4F60\u662F\u8C01",
        )
        if any(marker in compact for marker in markers):
            return True
        return lowered in {
            "whatisyourname",
            "what'syourname",
            "whatisurname",
            "whoareyou",
        }

    @staticmethod
    def _normalize_clarification_hint(text: str) -> str:
        compact = str(text or "").strip().lower()
        if not compact:
            return ""
        if compact == "assistant_alias":
            return "assistant_alias"
        if compact == "user_name":
            return "user_name"
        if "assistant" in compact or "alias" in compact or "叫你" in compact or "别人都叫你" in compact:
            return "assistant_alias"
        if "user" in compact or "callme" in compact or "叫我" in compact:
            return "user_name"
        return ""

    def _member_payload(
        self,
        event: InboundEvent,
        member: dict[str, Any] | None,
        *,
        candidate: str = "",
        onboarding_status: str = "",
        character_id: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "group_id": event.launcher_id if event.launcher_type == "group" else "",
            "user_id": event.sender_id,
            "qq_nickname": event.sender_name,
        }
        if candidate:
            payload["preferred_name"] = candidate
        if onboarding_status:
            payload["onboarding_status"] = onboarding_status
        if character_id:
            payload["character_id"] = character_id
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
        self._service.member_repository.save_member(
            self._member_payload(event, member, candidate=candidate, onboarding_status="ready")
        )
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
            self._service.member_repository.save_member,
            self._member_payload(event, member, candidate=candidate, onboarding_status="ready"),
        )
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return await self._service.emitter.aemit(event, message, assistant_name=assistant_name)

    def _confirm_assistant_alias(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        candidate: str,
        allow_fallback: bool,
    ) -> OutboundMessage | None:
        address = self._resolved_address(event, session)
        reply_text = self._service.generator.generate_onboarding_reply(
            event,
            session,
            assistant_name=assistant_name,
            stage="confirm_assistant_alias",
            candidate_name=candidate,
            address_override=address,
            allow_fallback=allow_fallback,
        )
        if not reply_text:
            return None
        self._service.member_repository.save_assistant_alias(
            character_id=str(session.character_id or self._service._active_character_id()).strip(),
            user_id=event.sender_id,
            assistant_alias=candidate,
        )
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return self._service.emitter.emit(event, message, assistant_name=candidate)

    async def _aconfirm_assistant_alias(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        candidate: str,
        allow_fallback: bool,
    ) -> OutboundMessage | None:
        address = await asyncio.to_thread(self._resolved_address, event, session)
        reply_text = await self._service.generator.agenerate_onboarding_reply(
            event,
            session,
            assistant_name=assistant_name,
            stage="confirm_assistant_alias",
            candidate_name=candidate,
            address_override=address,
            allow_fallback=allow_fallback,
        )
        if not reply_text:
            return None
        await asyncio.to_thread(
            self._service.member_repository.save_assistant_alias,
            character_id=str(session.character_id or self._service._active_character_id()).strip(),
            user_id=event.sender_id,
            assistant_alias=candidate,
        )
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return await self._service.emitter.aemit(event, message, assistant_name=candidate)

    def _emit_stage_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        stage: str,
        allow_fallback: bool,
        intent_hint: str = "",
    ) -> OutboundMessage | None:
        address = self._resolved_address(event, session)
        reply_text = self._service.generator.generate_onboarding_reply(
            event,
            session,
            assistant_name=assistant_name,
            stage=stage,
            allow_fallback=allow_fallback,
            intent_hint=intent_hint,
            address_override=address,
        )
        if not reply_text:
            return None
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return self._service.emitter.emit(event, message, assistant_name=assistant_name)

    async def _aemit_stage_reply(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        assistant_name: str,
        stage: str,
        allow_fallback: bool,
        intent_hint: str = "",
    ) -> OutboundMessage | None:
        address = await asyncio.to_thread(self._resolved_address, event, session)
        reply_text = await self._service.generator.agenerate_onboarding_reply(
            event,
            session,
            assistant_name=assistant_name,
            stage=stage,
            allow_fallback=allow_fallback,
            intent_hint=intent_hint,
            address_override=address,
        )
        if not reply_text:
            return None
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        return await self._service.emitter.aemit(event, message, assistant_name=assistant_name)

    @staticmethod
    def _append_soft_ask(reply_text: str, soft_ask: str) -> str:
        base = str(reply_text or "").strip()
        extra = str(soft_ask or "").strip()
        if not extra:
            return base
        if not base:
            return extra
        return f"{base}\n{extra}"

    def _resolved_address(
        self,
        event: InboundEvent,
        session: SessionMemory,
        *,
        member: dict[str, Any] | None = None,
    ) -> str:
        record = member if member is not None else self._current_member(event)
        preferred_name = str((record or {}).get("preferred_name", "") or "").strip()
        if preferred_name:
            return preferred_name
        if event.launcher_type == "person":
            user_name = str(self._service.cards.load(event.launcher_type, session).user_name or "").strip()
            if user_name:
                return user_name
        return event.sender_name or "你"
