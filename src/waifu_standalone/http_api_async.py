from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from aiohttp import web

from .http_api import (
    HttpApi,
    RequestTooLarge,
    _MAX_REQUEST_BYTES,
    _NON_ADMIN_GET_PATHS,
    _NON_ADMIN_POST_PATHS,
    _SESSION_COOKIE_NAME,
    _STATIC_FILES,
    _WEB_DIR,
    _coerce_limit,
    _decode_request_body,
    _parse_cookie_header,
    _resolve_asset_path,
    _validate_launcher_type,
    _validate_route_segment,
)
from .observability import logging_is_configured, record_http_exchange


_LOGGER = logging.getLogger(__name__)


def _cache_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Connection": "close",
    }


def _json_response(
    status: int,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> web.Response:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    merged_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(encoded)),
        **_cache_headers(),
    }
    if headers:
        merged_headers.update(headers)
    return web.Response(status=int(status), body=encoded, headers=merged_headers)


def _bytes_response(status: int, body: bytes, content_type: str) -> web.Response:
    payload = bytes(body)
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(payload)),
        **_cache_headers(),
    }
    return web.Response(status=int(status), body=payload, headers=headers)


def _text_response(status: int, body: str, content_type: str) -> web.Response:
    payload = str(body or "").encode("utf-8")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(payload)),
        **_cache_headers(),
    }
    return web.Response(status=int(status), body=payload, headers=headers)


def _no_content_response(status: int = HTTPStatus.NO_CONTENT) -> web.Response:
    return web.Response(status=int(status), headers=_cache_headers())


def _static_file_response(filename: str, content_type: str) -> web.Response:
    path = (_WEB_DIR / filename).resolve()
    try:
        path.relative_to(_WEB_DIR.resolve())
    except ValueError:
        return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
    if not path.is_file():
        return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
    return _bytes_response(HTTPStatus.OK, path.read_bytes(), content_type)


async def _read_request_body(request: web.Request) -> bytes:
    content_length = request.content_length
    if content_length is not None:
        if content_length < 0:
            raise ValueError("invalid content length")
        if content_length > _MAX_REQUEST_BYTES:
            raise RequestTooLarge("request body exceeds 10 MB limit")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > _MAX_REQUEST_BYTES:
            raise RequestTooLarge("request body exceeds 10 MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_body(
    request: web.Request,
    *,
    allow_empty: bool = False,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    try:
        raw = await _read_request_body(request)
    except RequestTooLarge as exc:
        return None, _json_response(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            {"status": "payload_too_large", "reason": str(exc)},
        )
    except ValueError:
        return None, _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})

    if allow_empty and not raw:
        return {}, None

    try:
        body = _decode_request_body(raw, request.headers.get("Content-Encoding", ""))
        payload = json.loads(body)
    except RequestTooLarge as exc:
        return None, _json_response(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            {"status": "payload_too_large", "reason": str(exc)},
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        preview = raw[:200].decode("utf-8", errors="replace")
        _LOGGER.warning("bad request body preview=%r", preview)
        return None, _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})

    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        return None, _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})
    return payload, None


class _AioHttpDispatcher:
    def __init__(self, api: HttpApi) -> None:
        self.api = api

    async def _call_api_in_thread(self, callback, *args, **kwargs):  # type: ignore[no-untyped-def]
        return await asyncio.to_thread(callback, *args, **kwargs)

    async def handle_get(self, request: web.Request) -> web.Response:
        if response := await self._dispatch_static_get(request):
            return response

        current_user: dict[str, Any] | None = None
        if request.path.startswith("/api/"):
            current_user, response = await self._require_auth(request)
            if response is not None:
                return response
            response = self._authorize_api_request("GET", request.path, current_user)
            if response is not None:
                return response

        routes = {
            "/api/dashboard": self._get_dashboard,
            "/api/portraits": self._get_portrait,
            "/api/console": self._get_console,
            "/api/runtime": self._get_runtime,
            "/api/panels/observability": self._get_observability_panel,
            "/api/events/recent": self._get_recent_events,
            "/api/events/behavior": self._get_behavior_events,
            "/api/panels/character": self._get_character_panel,
            "/api/panels/ai": self._get_ai_panel,
            "/api/panels/memory": self._get_memory_panel,
            "/api/panels/abilities": self._get_abilities_panel,
            "/api/panels/proactive": self._get_proactive_panel,
            "/api/panels/skills": self._get_skills_panel,
            "/api/claw/runtime": self._get_claw_runtime_panel,
            "/api/claw/plugins": self._get_claw_plugins,
            "/api/panels/sidecar": self._get_sidecar_panel,
            "/api/panels/qq-login": self._get_qq_login_panel,
            "/api/qq-login/qrcode-image": self._get_qq_login_qrcode_image,
            "/api/panels/other": self._get_other_panel,
            "/api/panels/user": self._get_user_panel,
            "/api/marketplace/search": self._get_marketplace_search,
            "/api/skills": self._get_skills,
            "/api/tools": self._get_tools,
            "/api/skill-packs/template": self._get_skill_pack_template,
            "/api/skills/template": self._get_skill_template,
            "/api/sessions": self._get_sessions,
        }
        handler = routes.get(request.path)
        if handler is not None:
            return await handler(request, current_user)

        response = await self._dispatch_dynamic_get(request)
        if response is not None:
            return response
        return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})

    async def handle_post(self, request: web.Request) -> web.Response:
        response = await self._dispatch_static_post(request)
        if response is not None:
            return response

        current_user: dict[str, Any] | None = None
        if request.path.startswith("/api/"):
            current_user, response = await self._require_auth(request)
            if response is not None:
                return response
            response = self._authorize_api_request("POST", request.path, current_user)
            if response is not None:
                return response

        routes = {
            "/api/auth/change-password": self._post_change_password,
            "/api/skills/reload": self._post_reload_skills,
            "/api/providers/test": self._post_test_provider,
            "/api/sidecar/test": self._post_test_sidecar,
            "/api/panels/character": self._post_character_panel,
            "/api/character/preview": self._post_character_preview,
            "/api/panels/ai": self._post_ai_panel,
            "/api/panels/abilities": self._post_abilities_panel,
            "/api/proactive/draft": self._post_proactive_draft,
            "/api/panels/sidecar": self._post_sidecar_panel,
            "/api/panels/qq-login": self._post_qq_login_panel,
            "/api/qq-login/refresh": self._post_qq_login_refresh,
            "/api/panels/other": self._post_other_panel,
            "/api/users/directory/save": self._post_directory_member_save,
            "/api/users/directory/reset-persona": self._post_directory_member_reset_persona,
            "/api/users/directory/sync": self._post_directory_sync,
            "/api/knowledge/save": self._post_knowledge_save,
            "/api/marketplace/import": self._post_marketplace_import,
            "/api/skill-packs/export": self._post_skill_pack_export,
            "/api/skill-packs/import": self._post_skill_pack_import,
            "/api/skill-bundles/import-local": self._post_skill_bundle_import_local,
            "/api/skills/install": self._post_skill_install,
            "/api/claw/plugins/check": self._post_claw_plugins_check,
            "/api/claw/plugins/update": self._post_claw_plugin_update,
        }
        handler = routes.get(request.path)
        if handler is not None:
            return await handler(request, current_user)

        response = await self._dispatch_dynamic_post(request)
        if response is not None:
            return response
        return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})

    async def handle_delete(self, request: web.Request) -> web.Response:
        if request.path.startswith("/api/"):
            _, response = await self._require_auth(request)
            if response is not None:
                return response
        response = await self._dispatch_dynamic_delete(request)
        if response is not None:
            return response
        return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})

    async def _dispatch_static_get(self, request: web.Request) -> web.Response | None:
        if request.path in _STATIC_FILES:
            filename, content_type = _STATIC_FILES[request.path]
            return _static_file_response(filename, content_type)
        if request.path.startswith("/assets/"):
            relative = request.path[len("/assets/") :]
            asset = _resolve_asset_path(relative)
            if asset is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            filename, content_type = asset
            return _static_file_response(filename, content_type)
        if request.path == "/healthz":
            return _json_response(HTTPStatus.OK, {"status": "ok"})
        if request.path == "/metrics":
            return _text_response(
                HTTPStatus.OK,
                await self._call_api_in_thread(self.api.prometheus_metrics),
                "text/plain; version=0.0.4; charset=utf-8",
            )
        if request.path == "/api/auth/state":
            return _json_response(
                HTTPStatus.OK,
                await self._call_api_in_thread(self.api.auth_state, self._session_token(request)),
            )
        return None

    async def _dispatch_static_post(self, request: web.Request) -> web.Response | None:
        routes = {
            "/onebot/events": self._post_onebot_events,
            "/api/auth/bootstrap": self._post_auth_bootstrap,
            "/api/auth/login": self._post_auth_login,
            "/api/auth/logout": self._post_auth_logout,
        }
        handler = routes.get(request.path)
        return await handler(request, None) if handler is not None else None

    async def _dispatch_dynamic_get(self, request: web.Request) -> web.Response | None:
        if request.path.startswith("/api/skills/"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 3:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, skill_id = parts
                skill_id = _validate_route_segment(skill_id, name="skill_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            detail = await self._call_api_in_thread(self.api.get_skill_detail, skill_id)
            if detail is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, detail)

        if request.path.startswith("/api/claw/plugins/"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 4:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, _, plugin_id = parts
                plugin_id = _validate_route_segment(plugin_id, name="plugin_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            detail = await self._call_api_in_thread(self.api.inspect_claw_plugin, plugin_id)
            if detail is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, detail)

        if request.path.startswith("/api/sessions/"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 4:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, launcher_type, launcher_id = parts
                launcher_type = _validate_launcher_type(launcher_type)
                launcher_id = _validate_route_segment(launcher_id, name="launcher_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            detail = await self._call_api_in_thread(self.api.get_session_detail, launcher_type, launcher_id)
            if detail is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, detail)
        return None

    async def _dispatch_dynamic_post(self, request: web.Request) -> web.Response | None:
        if request.path.startswith("/api/skills/") and request.path.endswith("/toggle"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 4:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, skill_id, _ = parts
                skill_id = _validate_route_segment(skill_id, name="skill_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            payload, response = await _read_json_body(request)
            if response is not None:
                return response
            detail = await self._call_api_in_thread(
                self.api.set_skill_enabled,
                skill_id,
                bool((payload or {}).get("enabled", True)),
            )
            if detail is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, {"status": "ok", "skill": detail})

        if request.path.startswith("/api/skills/") and request.path.endswith("/save"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 4:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, skill_id, _ = parts
                skill_id = _validate_route_segment(skill_id, name="skill_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            payload, response = await _read_json_body(request)
            if response is not None:
                return response
            markdown = str((payload or {}).get("markdown", "") or "")
            if not markdown.strip():
                return _json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "bad_request", "reason": "markdown is required"},
                )
            try:
                detail = await self._call_api_in_thread(self.api.save_skill, skill_id, markdown)
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            if detail is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, {"status": "ok", "skill": detail})

        if request.path.startswith("/api/memory/sessions/") and request.path.endswith("/save"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 6:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, _, launcher_type, launcher_id, _ = parts
                launcher_type = _validate_launcher_type(launcher_type)
                launcher_id = _validate_route_segment(launcher_id, name="launcher_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            payload, response = await _read_json_body(request)
            if response is not None:
                return response
            detail = await self._call_api_in_thread(
                self.api.save_memory_session,
                launcher_type,
                launcher_id,
                payload or {},
            )
            if detail is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, {"status": "ok", "session": detail})
        return None

    async def _dispatch_dynamic_delete(self, request: web.Request) -> web.Response | None:
        if request.path.startswith("/api/skills/"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 3:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, skill_id = parts
                skill_id = _validate_route_segment(skill_id, name="skill_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
            if not await self._call_api_in_thread(self.api.delete_skill, skill_id):
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, {"status": "ok", "deleted": skill_id})

        if request.path.startswith("/api/knowledge/"):
            parts = [unquote(part) for part in request.path.split("/") if part]
            if len(parts) != 3:
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            try:
                _, _, raw_entry_id = parts
                entry_id = int(raw_entry_id)
            except ValueError:
                return _json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "bad_request", "reason": "knowledge_id must be an integer"},
                )
            if not await self._call_api_in_thread(self.api.delete_knowledge_entry, entry_id):
                return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return _json_response(HTTPStatus.OK, {"status": "ok", "deleted": entry_id})
        return None

    async def _get_dashboard(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.dashboard_snapshot))

    async def _get_portrait(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        character = str(request.query.get("character", "") or "").strip()
        if not character:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "character is required"})
        try:
            asset = await self._call_api_in_thread(self.api.get_character_portrait, character)
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        if asset is None:
            return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
        body, content_type = asset
        return _bytes_response(HTTPStatus.OK, body, content_type)

    async def _get_console(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.console_panels))

    async def _get_runtime(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.runtime_stats))

    async def _get_observability_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        log_limit = _coerce_limit(request.query.get("log_limit", "120"))
        row_limit = _coerce_limit(request.query.get("row_limit", "60"))
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(
                self.api.observability_panel,
                log_limit=log_limit,
                row_limit=row_limit,
            ),
        )

    async def _get_recent_events(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        limit = _coerce_limit(request.query.get("limit", "50"))
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.recent_events, limit=limit),
        )

    async def _get_behavior_events(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        limit = _coerce_limit(request.query.get("limit", "80"))
        launcher_type = str(request.query.get("launcher_type", "") or "").strip()
        launcher_id = str(request.query.get("launcher_id", "") or "").strip()
        if launcher_type:
            try:
                launcher_type = _validate_launcher_type(launcher_type)
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        if launcher_id:
            try:
                launcher_id = _validate_route_segment(launcher_id, name="launcher_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(
                self.api.behavior_events,
                limit=limit,
                launcher_type=launcher_type,
                launcher_id=launcher_id,
            ),
        )

    async def _get_character_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.get_character_panel, request.query.get("character", "")),
        )

    async def _get_ai_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.get_ai_panel))

    async def _get_memory_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.get_memory_panel))

    async def _get_abilities_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.get_abilities_panel))

    async def _get_proactive_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        limit = _coerce_limit(request.query.get("limit", "12"))
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.get_proactive_panel, limit=limit),
        )

    async def _get_skills_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.get_skills_panel))

    async def _get_claw_runtime_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        refresh = str(request.query.get("refresh", "0")).lower() in {"1", "true", "yes"}
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.get_claw_runtime_panel, refresh=refresh),
        )

    async def _get_claw_plugins(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.list_claw_plugins))

    async def _get_sidecar_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        refresh = str(request.query.get("refresh", "0")).lower() in {"1", "true", "yes"}
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.get_sidecar_panel, refresh=refresh),
        )

    async def _get_qq_login_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        refresh = str(request.query.get("refresh", "0")).lower() in {"1", "true", "yes"}
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.get_qq_login_panel, refresh=refresh),
        )

    async def _get_qq_login_qrcode_image(
        self,
        request: web.Request,
        current_user: dict[str, Any] | None,
    ) -> web.Response:
        try:
            asset = await self._call_api_in_thread(self.api.get_qq_login_qrcode_image)
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        if asset is None:
            return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
        body, content_type = asset
        return _bytes_response(HTTPStatus.OK, body, content_type)

    async def _get_other_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.get_other_panel))

    async def _get_user_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        if not isinstance(current_user, dict):
            return _json_response(
                HTTPStatus.UNAUTHORIZED,
                {"status": "unauthorized", "reason": "authentication required"},
            )
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.get_user_panel, str(current_user["username"])),
        )

    async def _get_marketplace_search(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        query = str(request.query.get("q", "") or "")
        source_id = str(request.query.get("source_id", "") or "")
        limit = _coerce_limit(request.query.get("limit", "12"))
        if source_id:
            try:
                source_id = _validate_route_segment(source_id, name="source_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.search_marketplace, query, source_id=source_id, limit=limit),
        )

    async def _get_skills(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.list_skills))

    async def _get_tools(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.list_tools))

    async def _get_skill_pack_template(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.skill_pack_template))

    async def _get_skill_template(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.new_skill_template))

    async def _get_sessions(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        limit = _coerce_limit(request.query.get("limit", "24"))
        return _json_response(
            HTTPStatus.OK,
            {"sessions": await self._call_api_in_thread(self.api.list_sessions, limit=limit)},
        )

    async def _post_onebot_events(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        status, body = await self.api.handle_json_async(payload or {})
        if status < HTTPStatus.BAD_REQUEST:
            return _no_content_response(HTTPStatus.NO_CONTENT)
        return _json_response(status, body)

    async def _post_auth_bootstrap(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            body, cookie = await self._call_api_in_thread(self.api.bootstrap_auth, payload or {})
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, body, headers={"Set-Cookie": cookie})

    async def _post_auth_login(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            body, cookie = await self._call_api_in_thread(self.api.login, payload or {})
        except ValueError as exc:
            return _json_response(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, body, headers={"Set-Cookie": cookie})

    async def _post_auth_logout(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        _, response = await _read_json_body(request, allow_empty=True)
        if response is not None:
            return response
        body, cookie = await self._call_api_in_thread(self.api.logout, self._session_token(request))
        return _json_response(HTTPStatus.OK, body, headers={"Set-Cookie": cookie})

    async def _post_change_password(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            body = await self._call_api_in_thread(
                self.api.change_password,
                str((current_user or {}).get("username", "")),
                payload or {},
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, body)

    async def _post_reload_skills(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        _, response = await _read_json_body(request, allow_empty=True)
        if response is not None:
            return response
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.reload_skills))

    async def _post_test_provider(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            result = await self._call_api_in_thread(self.api.test_provider, payload or {})
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, result)

    async def _post_test_sidecar(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        _, response = await _read_json_body(request, allow_empty=True)
        if response is not None:
            return response
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.test_sidecar))

    async def _post_character_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.save_character_panel, payload or {}),
        )

    async def _post_character_preview(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            return _json_response(
                HTTPStatus.OK,
                await self._call_api_in_thread(self.api.preview_character_panel, payload or {}),
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})

    async def _post_ai_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.save_ai_panel, payload or {}))

    async def _post_abilities_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.save_abilities_panel, payload or {}),
        )

    async def _post_proactive_draft(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            return _json_response(
                HTTPStatus.OK,
                await self._call_api_in_thread(self.api.generate_proactive_draft, payload or {}),
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})

    async def _post_sidecar_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.save_sidecar_panel, payload or {}),
        )

    async def _post_qq_login_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.save_qq_login_panel, payload or {}),
        )

    async def _post_qq_login_refresh(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        _, response = await _read_json_body(request, allow_empty=True)
        if response is not None:
            return response
        try:
            return _json_response(
                HTTPStatus.OK,
                await self._call_api_in_thread(self.api.refresh_qq_login_panel),
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})

    async def _post_other_panel(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        return _json_response(
            HTTPStatus.OK,
            await self._call_api_in_thread(self.api.save_other_panel, payload or {}),
        )

    async def _post_directory_member_save(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            member = await self._call_api_in_thread(self.api.save_directory_member, payload or {})
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, {"status": "ok", "member": member})

    async def _post_directory_member_reset_persona(
        self,
        request: web.Request,
        current_user: dict[str, Any] | None,
    ) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            detail = await self._call_api_in_thread(self.api.reset_directory_member_persona, payload or {})
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        if detail is None:
            return _json_response(HTTPStatus.NOT_FOUND, {"status": "not_found"})
        return _json_response(HTTPStatus.OK, {"status": "ok", "member": detail})

    async def _post_directory_sync(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            body = await self._call_api_in_thread(
                self.api.sync_group_members,
                str((payload or {}).get("group_id", "") or ""),
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, body)

    async def _post_knowledge_save(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        try:
            entry = await self._call_api_in_thread(self.api.save_knowledge_entry, payload or {})
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, {"status": "ok", "entry": entry})

    async def _post_marketplace_import(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        source_id = str((payload or {}).get("source_id", "") or "")
        github_url = str((payload or {}).get("github_url", "") or "")
        if not github_url:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "github_url is required"})
        if source_id:
            try:
                source_id = _validate_route_segment(source_id, name="source_id")
            except ValueError as exc:
                return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        try:
            detail = await self._call_api_in_thread(
                self.api.import_marketplace_skill,
                source_id=source_id,
                github_url=github_url,
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, {"status": "ok", "bundle": detail})

    async def _post_skill_pack_export(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request, allow_empty=True)
        if response is not None:
            return response
        skill_ids = (payload or {}).get("skill_ids", [])
        if not isinstance(skill_ids, list):
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "skill_ids must be a list"})
        result = await self._call_api_in_thread(
            self.api.export_skill_pack,
            skill_ids=[str(item) for item in skill_ids if str(item).strip()],
            include_builtin=bool((payload or {}).get("include_builtin", False)),
            name=str((payload or {}).get("name", "") or ""),
            description=str((payload or {}).get("description", "") or ""),
        )
        return _json_response(HTTPStatus.OK, result)

    async def _post_skill_pack_import(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        pack_payload = (payload or {}).get("bundle")
        if pack_payload is None:
            pack_payload = (payload or {}).get("payload")
        if pack_payload is None:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "bundle is required"})
        try:
            result = await self._call_api_in_thread(
                self.api.import_skill_pack,
                pack_payload,
                overwrite=bool((payload or {}).get("overwrite", True)),
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, {"status": "ok", "pack": result})

    async def _post_skill_bundle_import_local(
        self,
        request: web.Request,
        current_user: dict[str, Any] | None,
    ) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        source_path = str((payload or {}).get("path", "") or "").strip()
        if not source_path:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "path is required"})
        try:
            result = await self._call_api_in_thread(
                self.api.import_skill_bundle,
                source_path,
                overwrite=bool((payload or {}).get("overwrite", True)),
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, {"status": "ok", "bundle": result})

    async def _post_skill_install(self, request: web.Request, current_user: dict[str, Any] | None) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        markdown = str((payload or {}).get("markdown", "") or "")
        filename = (payload or {}).get("filename")
        if not markdown.strip():
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "markdown is required"})
        try:
            detail = await self._call_api_in_thread(
                self.api.install_skill,
                markdown,
                filename=str(filename) if filename else None,
            )
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, {"status": "ok", "skill": detail})

    async def _post_claw_plugins_check(
        self,
        request: web.Request,
        current_user: dict[str, Any] | None,
    ) -> web.Response:
        _, response = await _read_json_body(request, allow_empty=True)
        if response is not None:
            return response
        return _json_response(HTTPStatus.OK, await self._call_api_in_thread(self.api.check_claw_plugins))

    async def _post_claw_plugin_update(
        self,
        request: web.Request,
        current_user: dict[str, Any] | None,
    ) -> web.Response:
        payload, response = await _read_json_body(request)
        if response is not None:
            return response
        plugin_id = str((payload or {}).get("plugin_id", "") or "").strip()
        if not plugin_id:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "plugin_id is required"})
        try:
            detail = await self._call_api_in_thread(self.api.update_claw_plugin, plugin_id)
        except ValueError as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
        return _json_response(HTTPStatus.OK, {"status": "ok", "bundle": detail})

    def _session_token(self, request: web.Request) -> str | None:
        cookies = _parse_cookie_header(request.headers.get("Cookie", ""))
        return cookies.get(_SESSION_COOKIE_NAME)

    async def _require_auth(self, request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
        state = await self._call_api_in_thread(self.api.auth_state, self._session_token(request))
        user = state.get("user")
        if isinstance(user, dict):
            return user, None
        status = {
            "status": "unauthorized",
            "requires_setup": bool(state.get("requires_setup", False)),
        }
        status["reason"] = "initial setup required" if status["requires_setup"] else "authentication required"
        return None, _json_response(HTTPStatus.UNAUTHORIZED, status)

    def _authorize_api_request(
        self,
        method: str,
        path: str,
        current_user: dict[str, Any] | None,
    ) -> web.Response | None:
        if str((current_user or {}).get("role", "")).strip().lower() == "admin":
            return None
        if method == "GET" and path in _NON_ADMIN_GET_PATHS:
            return None
        if method == "POST" and path in _NON_ADMIN_POST_PATHS:
            return None
        return _json_response(
            HTTPStatus.FORBIDDEN,
            {"status": "forbidden", "reason": "admin privileges required"},
        )


def _build_application(api: HttpApi) -> web.Application:
    dispatcher = _AioHttpDispatcher(api)
    reverse_ws_gateway = getattr(api.service, "reverse_ws_gateway", None)

    @web.middleware
    async def _observability_middleware(request: web.Request, handler: web.Handler) -> web.StreamResponse:
        started = time.perf_counter()
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            record_http_exchange(
                api.metrics,
                method=request.method,
                path=request.path,
                status_code=exc.status,
                duration_seconds=time.perf_counter() - started,
                remote_addr=str(request.remote or ""),
            )
            raise
        except Exception:
            if logging_is_configured():
                _LOGGER.exception(
                    "unhandled http request failure method=%s path=%s",
                    request.method,
                    request.path,
                )
            record_http_exchange(
                api.metrics,
                method=request.method,
                path=request.path,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                duration_seconds=time.perf_counter() - started,
                remote_addr=str(request.remote or ""),
            )
            raise
        record_http_exchange(
            api.metrics,
            method=request.method,
            path=request.path,
            status_code=response.status,
            duration_seconds=time.perf_counter() - started,
            remote_addr=str(request.remote or ""),
        )
        return response

    app = web.Application(client_max_size=_MAX_REQUEST_BYTES, middlewares=[_observability_middleware])
    if reverse_ws_gateway is not None:
        app.router.add_get("/onebot/v11/ws", reverse_ws_gateway.handle)
    app.router.add_route("GET", "/", dispatcher.handle_get)
    app.router.add_route("GET", "/{tail:.*}", dispatcher.handle_get)
    app.router.add_route("POST", "/{tail:.*}", dispatcher.handle_post)
    app.router.add_route("DELETE", "/{tail:.*}", dispatcher.handle_delete)
    return app


def _create_listen_socket(host: str, port: int) -> socket.socket:
    last_error: OSError | None = None
    flags = socket.AI_PASSIVE if not host else 0
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=flags):
        candidate = socket.socket(family, socktype, proto)
        try:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(sockaddr)
            candidate.listen(socket.SOMAXCONN)
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()
    if last_error is not None:
        raise last_error
    raise OSError("unable to bind server socket")


class AsyncCompatServer:
    def __init__(self, api: HttpApi, host: str, port: int) -> None:
        self._api = api
        self._socket = _create_listen_socket(host, port)
        sockname = self._socket.getsockname()
        self.server_address = (sockname[0], sockname[1])
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._stop_event: asyncio.Event | None = None
        self._resources_closed = False
        self._shutdown_requested = False

    def serve_forever(self) -> None:
        if self._resources_closed:
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._start())
            if self._shutdown_requested:
                self._loop.call_soon(self._stop_event.set)
            self._loop.run_until_complete(self._stop_event.wait())
        finally:
            try:
                self._loop.run_until_complete(self._cleanup())
            finally:
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                finally:
                    asyncio.set_event_loop(None)
                    self._loop.close()
                    self._loop = None
                    self._stop_event = None
                    self._close_resources()

    def shutdown(self) -> None:
        self._shutdown_requested = True
        if self._loop is not None and self._stop_event is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def server_close(self) -> None:
        self.shutdown()
        if self._loop is None:
            self._close_resources()

    async def _start(self) -> None:
        self._runner = web.AppRunner(
            _build_application(self._api),
            keepalive_timeout=0,
        )
        await self._runner.setup()
        self._site = web.SockSite(self._runner, self._socket)
        await self._site.start()

    async def _cleanup(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        loop = asyncio.get_running_loop()
        current = asyncio.current_task(loop=loop)
        pending = [task for task in asyncio.all_tasks(loop=loop) if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)
        self._site = None

    def _close_resources(self) -> None:
        if self._resources_closed:
            return
        self._resources_closed = True
        try:
            self._socket.close()
        except OSError:
            pass
        close = getattr(self._api.service, "close", None)
        if callable(close):
            close()
