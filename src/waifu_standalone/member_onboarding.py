"""Member onboarding: ask for name, confirm it, save to directory.

Extracted from :mod:`waifu_standalone.app`. Handles the flow where a new
group member (or DM) is asked for a preferred name, and the confirmation
hand-off when they reply.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from .models import InboundEvent, OutboundMessage, SessionMemory

if TYPE_CHECKING:
    from .app import WaifuService


class MemberOnboarding:
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
        if event.launcher_type == "person":
            member = self._service.state_store.get_member(group_id="", user_id=event.sender_id) or {}
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return None
            candidate = self._service.memory.extract_preferred_name(latest_message)
            if not candidate:
                return None
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
            self._service.state_store.save_member(
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
            return self._service.emitter.emit(event, message, assistant_name=assistant_name)
        if event.launcher_type != "group":
            return None
        member = self._service.state_store.get_member(group_id=event.launcher_id, user_id=event.sender_id)
        if member is None:
            return None

        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            return None

        onboarding_status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if onboarding_status == "pending_name":
            candidate = self.extract_preferred_name(latest_message)
            if candidate:
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
                self._service.state_store.save_member(
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
                return self._service.emitter.emit(event, message, assistant_name=assistant_name)
            if self.should_retry_prompt(event):
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
        if event.launcher_type == "person":
            member = await asyncio.to_thread(
                self._service.state_store.get_member,
                group_id="",
                user_id=event.sender_id,
            ) or {}
            preferred_name = str(member.get("preferred_name", "") or "").strip()
            if preferred_name:
                return None
            candidate = self._service.memory.extract_preferred_name(latest_message)
            if not candidate:
                return None
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
                {
                    "group_id": "",
                    "user_id": event.sender_id,
                    "qq_nickname": event.sender_name,
                    "preferred_name": candidate,
                    "onboarding_status": "ready",
                },
            )
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=reply_text,
            )
            return await self._service.emitter.aemit(event, message, assistant_name=assistant_name)
        if event.launcher_type != "group":
            return None
        member = await asyncio.to_thread(
            self._service.state_store.get_member,
            group_id=event.launcher_id,
            user_id=event.sender_id,
        )
        if member is None:
            return None

        preferred_name = str(member.get("preferred_name", "") or "").strip()
        if preferred_name:
            return None

        onboarding_status = str(member.get("onboarding_status", "") or "").strip() or "new"
        if onboarding_status == "pending_name":
            candidate = self.extract_preferred_name(latest_message)
            if candidate:
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
                    {
                        "group_id": event.launcher_id,
                        "user_id": event.sender_id,
                        "qq_nickname": event.sender_name,
                        "preferred_name": candidate,
                        "onboarding_status": "ready",
                    },
                )
                message = OutboundMessage(
                    launcher_id=event.launcher_id,
                    launcher_type=event.launcher_type,
                    text=reply_text,
                )
                return await self._service.emitter.aemit(event, message, assistant_name=assistant_name)
            if self.should_retry_prompt(event):
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

    def extract_preferred_name(self, text: str) -> str:
        candidate = self._service.memory.extract_preferred_name(text)
        if candidate:
            return candidate
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return ""
        compact = compact.strip(".,!?;:，。！？；：\"'`()[]{}")
        if not compact or len(compact) > 12:
            return ""
        if " " in compact or any(token in compact for token in ("http://", "https://", "/", "\\")):
            return ""
        if compact.startswith("@"):
            return ""
        blocked_fragments = (
            "什么",
            "玩意",
            "东西",
            "怎么",
            "咋",
            "为啥",
            "干嘛",
            "谁",
            "你是",
            "不是",
        )
        if any(fragment in compact for fragment in blocked_fragments):
            return ""
        if compact.endswith(("吗", "呢", "呀", "啊", "吧")):
            return ""
        lowered = compact.casefold()
        blocked_exact = {
            "你好",
            "您好",
            "嗨",
            "哈喽",
            "hello",
            "hi",
            "hey",
            "在吗",
            "在嘛",
            "是吗",
            "不是",
            "爸爸吗",
            "卧槽",
        }
        if lowered in blocked_exact:
            return ""
        return compact

    def should_retry_prompt(self, event: InboundEvent) -> bool:
        if event.launcher_type != "group":
            return False
        bot_account_id = str(self._service.config.bot_account_id or "").strip()
        return bool(bot_account_id and event.has_bot_mention(bot_account_id))
