from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .gateways.onebot_actions import OneBotActionClient
from .gateways.onebot_ws import OneBotWsActionClient

if TYPE_CHECKING:
    from .app import WaifuService


class NoticeDispatcher:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def handle_notice_payload(self, payload: dict[str, object]) -> dict[str, object]:
        notice_type = str(payload.get("notice_type", "") or "").strip().lower()
        if notice_type == "group_increase":
            return self._handle_group_increase_notice(payload)
        if notice_type == "group_decrease":
            return self._handle_group_decrease_notice(payload)
        if notice_type == "group_card":
            return self._handle_group_card_notice(payload)
        return {"status": "ignored", "reason": "unsupported notice_type"}

    async def ahandle_notice_payload(self, payload: dict[str, object]) -> dict[str, object]:
        notice_type = str(payload.get("notice_type", "") or "").strip().lower()
        if notice_type == "group_increase":
            return await self._ahandle_group_increase_notice(payload)
        if notice_type == "group_decrease":
            return await self._ahandle_group_decrease_notice(payload)
        if notice_type == "group_card":
            return await self._ahandle_group_card_notice(payload)
        return {"status": "ignored", "reason": "unsupported notice_type"}

    def sync_group_members(self, group_id: str) -> dict[str, object]:
        svc = self.service
        safe_group_id = str(group_id or "").strip()
        if not safe_group_id:
            raise ValueError("group_id is required")
        client = self._sidecar_action_client()
        response = client.get_group_member_list(safe_group_id)
        members = response.get("data", response)
        if not isinstance(members, list):
            raise ValueError("group member payload is invalid")
        synced: list[dict[str, object]] = []
        active_user_ids: list[str] = []
        synced_at = int(time.time())
        for item in members:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", "") or "").strip()
            if not user_id:
                continue
            active_user_ids.append(user_id)
            saved = svc.state_store.save_member(
                {
                    "group_id": safe_group_id,
                    "user_id": user_id,
                    "qq_nickname": str(item.get("nickname", "") or ""),
                    "group_card": str(item.get("card", "") or ""),
                    "membership_status": "active",
                    "last_sync_at": synced_at,
                }
            )
            synced.append(saved)
        missing_count = 0
        mark_missing = getattr(svc.state_store, "mark_group_members_missing", None)
        if callable(mark_missing):
            missing_count = int(
                mark_missing(
                    group_id=safe_group_id,
                    active_user_ids=active_user_ids,
                    membership_status="left",
                    last_sync_at=synced_at,
                )
                or 0
            )
        return {
            "status": "ok",
            "group_id": safe_group_id,
            "count": len(synced),
            "marked_missing_count": missing_count,
            "members": synced[:20],
        }

    async def async_group_members(self, group_id: str) -> dict[str, object]:
        svc = self.service
        safe_group_id = str(group_id or "").strip()
        if not safe_group_id:
            raise ValueError("group_id is required")
        client = self._sidecar_action_client()
        response = await client.aget_group_member_list(safe_group_id)
        members = response.get("data", response)
        if not isinstance(members, list):
            raise ValueError("group member payload is invalid")
        synced: list[dict[str, object]] = []
        active_user_ids: list[str] = []
        synced_at = int(time.time())
        for item in members:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", "") or "").strip()
            if not user_id:
                continue
            active_user_ids.append(user_id)
            saved = await asyncio.to_thread(
                svc.state_store.save_member,
                {
                    "group_id": safe_group_id,
                    "user_id": user_id,
                    "qq_nickname": str(item.get("nickname", "") or ""),
                    "group_card": str(item.get("card", "") or ""),
                    "membership_status": "active",
                    "last_sync_at": synced_at,
                },
            )
            synced.append(saved)
        missing_count = 0
        mark_missing = getattr(svc.state_store, "mark_group_members_missing", None)
        if callable(mark_missing):
            missing_count = int(
                await asyncio.to_thread(
                    mark_missing,
                    group_id=safe_group_id,
                    active_user_ids=active_user_ids,
                    membership_status="left",
                    last_sync_at=synced_at,
                )
                or 0
            )
        return {
            "status": "ok",
            "group_id": safe_group_id,
            "count": len(synced),
            "marked_missing_count": missing_count,
            "members": synced[:20],
        }

    def _sidecar_action_client(self) -> OneBotActionClient | OneBotWsActionClient:
        svc = self.service
        if (
            str(svc.config.qq_sidecar.gateway_mode or "http").strip().lower() == "reverse_ws"
            and svc.reverse_ws_gateway is not None
        ):
            return OneBotWsActionClient(svc.reverse_ws_gateway)
        if not svc.config.qq_sidecar.outbound_base_url:
            raise ValueError("sidecar is not available")
        return OneBotActionClient(
            base_url=svc.config.qq_sidecar.outbound_base_url,
            timeout=svc.config.qq_sidecar.outbound_timeout_seconds,
            access_token=svc.config.qq_sidecar.access_token,
        )

    def _handle_group_increase_notice(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        bot_id = str(payload.get("self_id", "") or svc.config.bot_account_id or "").strip()
        if bot_id and user_id == bot_id and svc.config.member_auto_sync:
            synced = svc.sync_group_members(group_id)
            return {"status": "ok", "reason": "bot_joined_group", "sync": synced}

        member_payload: dict[str, object] = {
            "group_id": group_id,
            "user_id": user_id,
            "membership_status": "active",
            "last_sync_at": int(time.time()),
        }
        if svc.config.member_auto_sync:
            try:
                response = self._sidecar_action_client().get_group_member_info(group_id, user_id, no_cache=False)
                data = response.get("data", response)
                if isinstance(data, dict):
                    member_payload["qq_nickname"] = str(data.get("nickname", "") or "")
                    member_payload["group_card"] = str(data.get("card", "") or "")
            except Exception:
                pass
        saved = svc.state_store.save_member(member_payload)
        return {"status": "ok", "reason": "member_joined", "member": saved}

    async def _ahandle_group_increase_notice(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        bot_id = str(payload.get("self_id", "") or svc.config.bot_account_id or "").strip()
        if bot_id and user_id == bot_id and svc.config.member_auto_sync:
            synced = await self.async_group_members(group_id)
            return {"status": "ok", "reason": "bot_joined_group", "sync": synced}

        member_payload: dict[str, object] = {
            "group_id": group_id,
            "user_id": user_id,
            "membership_status": "active",
            "last_sync_at": int(time.time()),
        }
        if svc.config.member_auto_sync:
            try:
                response = await self._sidecar_action_client().aget_group_member_info(group_id, user_id, no_cache=False)
                data = response.get("data", response)
                if isinstance(data, dict):
                    member_payload["qq_nickname"] = str(data.get("nickname", "") or "")
                    member_payload["group_card"] = str(data.get("card", "") or "")
            except Exception:
                pass
        saved = await asyncio.to_thread(svc.state_store.save_member, member_payload)
        return {"status": "ok", "reason": "member_joined", "member": saved}

    def _handle_group_decrease_notice(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        sub_type = str(payload.get("sub_type", "") or "").strip().lower()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        bot_id = str(payload.get("self_id", "") or svc.config.bot_account_id or "").strip()
        synced_at = int(time.time())
        if bot_id and user_id == bot_id:
            mark_missing = getattr(svc.state_store, "mark_group_members_missing", None)
            affected = 0
            if callable(mark_missing):
                affected = int(
                    mark_missing(
                        group_id=group_id,
                        active_user_ids=[],
                        membership_status="removed",
                        last_sync_at=synced_at,
                    )
                    or 0
                )
            return {"status": "ok", "reason": "bot_left_group", "affected_members": affected}

        status = "left" if sub_type == "leave" else "removed"
        saved = svc.state_store.mark_member_membership(
            group_id=group_id,
            user_id=user_id,
            membership_status=status,
            last_sync_at=synced_at,
        )
        if saved is None:
            saved = svc.state_store.save_member(
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "membership_status": status,
                    "last_sync_at": synced_at,
                }
            )
        return {"status": "ok", "reason": "member_left_group", "member": saved}

    async def _ahandle_group_decrease_notice(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        sub_type = str(payload.get("sub_type", "") or "").strip().lower()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        bot_id = str(payload.get("self_id", "") or svc.config.bot_account_id or "").strip()
        synced_at = int(time.time())
        if bot_id and user_id == bot_id:
            mark_missing = getattr(svc.state_store, "mark_group_members_missing", None)
            affected = 0
            if callable(mark_missing):
                affected = int(
                    await asyncio.to_thread(
                        mark_missing,
                        group_id=group_id,
                        active_user_ids=[],
                        membership_status="removed",
                        last_sync_at=synced_at,
                    )
                    or 0
                )
            return {"status": "ok", "reason": "bot_left_group", "affected_members": affected}

        status = "left" if sub_type == "leave" else "removed"
        saved = await asyncio.to_thread(
            svc.state_store.mark_member_membership,
            group_id=group_id,
            user_id=user_id,
            membership_status=status,
            last_sync_at=synced_at,
        )
        if saved is None:
            saved = await asyncio.to_thread(
                svc.state_store.save_member,
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "membership_status": status,
                    "last_sync_at": synced_at,
                },
            )
        return {"status": "ok", "reason": "member_left_group", "member": saved}

    def _handle_group_card_notice(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        card_value = (
            str(payload.get("card_new", "") or "").strip()
            or str(payload.get("card", "") or "").strip()
            or str(payload.get("nickname", "") or "").strip()
        )
        saved = svc.state_store.save_member(
            {
                "group_id": group_id,
                "user_id": user_id,
                "group_card": card_value,
                "membership_status": "active",
                "last_sync_at": int(time.time()),
            }
        )
        return {"status": "ok", "reason": "group_card_updated", "member": saved}

    async def _ahandle_group_card_notice(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        group_id = str(payload.get("group_id", "") or "").strip()
        user_id = str(payload.get("user_id", "") or "").strip()
        if not group_id or not user_id:
            return {"status": "ignored", "reason": "missing group_id or user_id"}
        card_value = (
            str(payload.get("card_new", "") or "").strip()
            or str(payload.get("card", "") or "").strip()
            or str(payload.get("nickname", "") or "").strip()
        )
        saved = await asyncio.to_thread(
            svc.state_store.save_member,
            {
                "group_id": group_id,
                "user_id": user_id,
                "group_card": card_value,
                "membership_status": "active",
                "last_sync_at": int(time.time()),
            },
        )
        return {"status": "ok", "reason": "group_card_updated", "member": saved}
