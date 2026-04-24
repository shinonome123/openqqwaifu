from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..app import WaifuService
    from ..models import InboundEvent, SessionMemory


@dataclass(slots=True)
class NamingIntentRouteResult:
    mode: str = "none"
    preferred_name: str = ""
    assistant_alias: str = ""
    clarification_text: str = ""
    reason: str = ""


class NamingIntentRouter:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service

    def route(
        self,
        event: "InboundEvent",
        session: "SessionMemory",
        *,
        latest_message: str,
        assistant_name: str,
        onboarding_status: str,
        preferred_name: str,
        passive_capture_allowed: bool,
    ) -> NamingIntentRouteResult:
        if not self._routing_ready():
            return NamingIntentRouteResult()
        payload = self._service.generator.resolve_naming_intent(
            event,
            session,
            assistant_name=assistant_name,
            latest_message=latest_message,
            onboarding_status=onboarding_status,
            preferred_name=preferred_name,
            passive_capture_allowed=passive_capture_allowed,
        )
        return self._normalize_route_result(payload)

    async def aroute(
        self,
        event: "InboundEvent",
        session: "SessionMemory",
        *,
        latest_message: str,
        assistant_name: str,
        onboarding_status: str,
        preferred_name: str,
        passive_capture_allowed: bool,
    ) -> NamingIntentRouteResult:
        if not self._routing_ready():
            return NamingIntentRouteResult()
        payload = await self._service.generator.aresolve_naming_intent(
            event,
            session,
            assistant_name=assistant_name,
            latest_message=latest_message,
            onboarding_status=onboarding_status,
            preferred_name=preferred_name,
            passive_capture_allowed=passive_capture_allowed,
        )
        return self._normalize_route_result(payload)

    def _routing_ready(self) -> bool:
        return bool(self._service.generator.llm_ready)

    @staticmethod
    def _normalize_route_result(payload: dict[str, Any]) -> NamingIntentRouteResult:
        mode = str(payload.get("mode", "") or "none").strip().lower()
        if mode not in {
            "set_preferred_name",
            "set_assistant_alias",
            "reject_third_party_naming",
            "clarify_naming_intent",
            "none",
        }:
            mode = "none"

        preferred_name = str(payload.get("preferred_name", "") or "").strip()
        assistant_alias = str(payload.get("assistant_alias", "") or "").strip()
        clarification_text = str(payload.get("clarification_text", "") or "").strip()
        reason = str(payload.get("reason", "") or "").strip()

        if mode == "set_preferred_name" and not preferred_name:
            mode = "none"
        if mode == "set_assistant_alias" and not assistant_alias:
            mode = "none"
        if mode == "clarify_naming_intent" and clarification_text not in {"user_name", "assistant_alias"}:
            clarification_text = "user_name"

        return NamingIntentRouteResult(
            mode=mode,
            preferred_name=preferred_name,
            assistant_alias=assistant_alias,
            clarification_text=clarification_text,
            reason=reason,
        )
