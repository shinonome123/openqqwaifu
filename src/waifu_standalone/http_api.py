from __future__ import annotations

import gzip
import io
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .cells.auth import AuthManager
from .models import InboundEvent, MessageSegment


_WEB_DIR = Path(__file__).resolve().parent / "web"
_MAX_REQUEST_BYTES = 10 * 1024 * 1024
_SAFE_ROUTE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_ASSET_PATH = re.compile(r"^[A-Za-z0-9_./-]{1,256}$")
_ALLOWED_LAUNCHER_TYPES = {"group", "person"}
_SESSION_COOKIE_NAME = "waifu_session"
_STATIC_FILES = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
}
_ASSET_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


class RequestTooLarge(ValueError):
    pass


def parse_onebot_event(payload: dict[str, Any]) -> InboundEvent:
    launcher_type = "group" if payload.get("message_type") == "group" else "person"
    if launcher_type == "group":
        launcher_id = str(payload.get("group_id") or payload.get("user_id") or "unknown")
    else:
        launcher_id = str(payload.get("user_id") or "unknown")
    sender = payload.get("sender") or {}
    sender_id = str(sender.get("user_id") or payload.get("user_id") or "unknown")
    raw_message = str(payload.get("raw_message") or "")
    segments = _parse_segments(payload)
    sender_name = str(sender.get("card") or sender.get("nickname") or "user")
    if _looks_mojibake(sender_name):
        sender_name = f"user_{sender_id}"

    return InboundEvent(
        launcher_id=launcher_id,
        launcher_type=launcher_type,
        sender_id=sender_id,
        sender_name=sender_name,
        segments=segments,
        message_id=str(payload.get("message_id") or ""),
        raw_message=raw_message,
    )


def _parse_segments(payload: dict[str, Any]) -> list[MessageSegment]:
    raw_message = payload.get("message")
    raw_text = payload.get("raw_message")
    items: list[dict[str, Any]] = []
    if isinstance(raw_message, list):
        items = [item for item in raw_message if isinstance(item, dict)]
    elif isinstance(raw_message, dict):
        items = [raw_message]
    elif isinstance(raw_message, str):
        return [MessageSegment(kind="text", text=raw_message)]
    elif isinstance(raw_text, str):
        return [MessageSegment(kind="text", text=str(raw_text))]

    if _should_prefer_raw_message(raw_text, items):
        return [MessageSegment(kind="text", text=str(raw_text).strip())]

    segments: list[MessageSegment] = []
    for item in items:
        item_type = str(item.get("type") or "").strip().lower()
        data = item.get("data") or {}
        if item_type == "text":
            segments.append(MessageSegment(kind="text", text=str(data.get("text", ""))))
        elif item_type == "image":
            segments.append(
                MessageSegment(
                    kind="image",
                    image_url=str(data.get("url") or data.get("file") or ""),
                    image_base64=str(data.get("base64") or ""),
                )
            )
        elif item_type == "at":
            segments.append(
                MessageSegment(
                    kind="mention",
                    mention_target=str(data.get("qq") or data.get("target") or ""),
                    mention_display=str(data.get("name") or data.get("display") or ""),
                )
            )
    return segments


def _should_prefer_raw_message(raw_message: Any, items: list[dict[str, Any]]) -> bool:
    if not isinstance(raw_message, str):
        return False
    if not raw_message.strip():
        return False
    if not items:
        return True
    return all(str(item.get("type") or "").strip().lower() == "text" for item in items)


def _looks_mojibake(text: str) -> bool:
    if not text:
        return False
    suspicious = ("闂", "妫", "濡", "锛", "銆", "鐞", "鍛", "浣", "\ufffd")
    return any(token in text for token in suspicious)


@dataclass(slots=True)
class HttpApi:
    service: Any
    auth: AuthManager | None = None

    def __post_init__(self) -> None:
        if self.auth is None:
            data_root = getattr(getattr(self.service, "config", None), "data_root", "data")
            self.auth = AuthManager(data_root)

    def handle_json(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        post_type = payload.get("post_type")
        if post_type == "notice":
            try:
                return HTTPStatus.ACCEPTED, dict(self.service.handle_notice_payload(payload))
            except Exception as exc:
                return HTTPStatus.BAD_GATEWAY, {"status": "delivery_failed", "reason": str(exc)}
        if post_type and post_type != "message":
            return HTTPStatus.ACCEPTED, {"status": "ignored", "reason": "unsupported post_type"}

        event = parse_onebot_event(payload)
        try:
            message = self.service.handle_event(event)
        except Exception as exc:
            return HTTPStatus.BAD_GATEWAY, {"status": "delivery_failed", "reason": str(exc)}

        if message is None:
            return HTTPStatus.ACCEPTED, {"status": "ignored", "reason": "empty event"}
        return HTTPStatus.OK, {
            "status": "ok",
            "reply": {
                "launcher_id": message.launcher_id,
                "launcher_type": message.launcher_type,
                "text": message.text,
                "images": message.images,
            },
        }

    def dashboard_snapshot(self) -> dict[str, Any]:
        return dict(self.service.dashboard_snapshot())

    def console_panels(self) -> dict[str, Any]:
        return dict(self.service.get_console_panels())

    def recent_events(self, limit: int = 50) -> dict[str, Any]:
        return {"events": list(self.service.recent_events(limit=limit))}

    def behavior_events(
        self,
        *,
        limit: int = 80,
        launcher_type: str = "",
        launcher_id: str = "",
    ) -> dict[str, Any]:
        return {
            "events": list(
                self.service.get_behavior_events(
                    limit=limit,
                    launcher_type=launcher_type,
                    launcher_id=launcher_id,
                )
            )
        }

    def runtime_stats(self) -> dict[str, Any]:
        return dict(self.service.runtime_stats())

    def test_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "").strip().lower()
        base_url = str(payload.get("base_url") or "").strip()
        api_key = str(payload.get("api_key") or "").strip()
        if kind not in {"llm", "image", "embedding"}:
            raise ValueError("kind must be 'llm', 'image' or 'embedding'")
        if not base_url:
            raise ValueError("base_url is required")
        return _probe_http_endpoint(base_url, api_key)

    def test_sidecar(self) -> dict[str, Any]:
        return dict(self.service.get_sidecar_panel(refresh=True))

    def list_sessions(self, limit: int = 24) -> list[dict[str, Any]]:
        return list(self.service.list_sessions(limit=limit))

    def get_session_detail(self, launcher_type: str, launcher_id: str) -> dict[str, Any] | None:
        detail = self.service.get_session_detail(launcher_type, launcher_id)
        return dict(detail) if detail else None

    def list_skills(self) -> dict[str, Any]:
        return dict(self.service.list_skills())

    def list_tools(self) -> dict[str, Any]:
        return dict(self.service.list_tools())

    def skill_pack_template(self) -> dict[str, Any]:
        return dict(self.service.skill_pack_template())

    def export_skill_pack(
        self,
        *,
        skill_ids: list[str] | None = None,
        include_builtin: bool = False,
        name: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        return dict(
            self.service.export_skill_pack(
                skill_ids=skill_ids,
                include_builtin=include_builtin,
                name=name,
                description=description,
            )
        )

    def import_skill_pack(self, payload: dict[str, Any] | str, *, overwrite: bool = True) -> dict[str, Any]:
        return dict(self.service.import_skill_pack(payload, overwrite=overwrite))

    def get_skill_detail(self, skill_id: str) -> dict[str, Any] | None:
        detail = self.service.get_skill_detail(skill_id)
        return dict(detail) if detail else None

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any] | None:
        detail = self.service.set_skill_enabled(skill_id, enabled)
        return dict(detail) if detail else None

    def install_skill(self, markdown: str, filename: str | None = None) -> dict[str, Any]:
        return dict(self.service.install_skill(markdown, filename=filename))

    def save_skill(self, skill_id: str, markdown: str) -> dict[str, Any] | None:
        detail = self.service.save_skill(skill_id, markdown)
        return dict(detail) if detail else None

    def delete_skill(self, skill_id: str) -> bool:
        return bool(self.service.delete_skill(skill_id))

    def reload_skills(self) -> dict[str, Any]:
        return dict(self.service.reload_skills())

    def new_skill_template(self) -> dict[str, Any]:
        return dict(self.service.new_skill_template())

    def get_character_panel(self, character: str = "") -> dict[str, Any]:
        return dict(self.service.get_character_panel(character))

    def save_character_panel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_character_panel(payload))

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.service.get_character_portrait(character)

    def preview_character_panel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.preview_character_panel(payload))

    def get_ai_panel(self) -> dict[str, Any]:
        return dict(self.service.get_ai_panel())

    def save_ai_panel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_ai_panel(payload))

    def get_memory_panel(self) -> dict[str, Any]:
        return dict(self.service.get_memory_panel())

    def save_memory_session(self, launcher_type: str, launcher_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        detail = self.service.save_memory_session(launcher_type, launcher_id, payload)
        return dict(detail) if detail else None

    def save_knowledge_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_knowledge_entry(payload))

    def delete_knowledge_entry(self, entry_id: int) -> bool:
        return bool(self.service.delete_knowledge_entry(entry_id))

    def get_abilities_panel(self) -> dict[str, Any]:
        return dict(self.service.get_abilities_panel())

    def save_abilities_panel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_abilities_panel(payload))

    def get_proactive_panel(self, limit: int = 12) -> dict[str, Any]:
        return dict(self.service.get_proactive_panel(limit=limit))

    def generate_proactive_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.generate_proactive_draft(payload))

    def get_skills_panel(self) -> dict[str, Any]:
        return dict(self.service.get_skills_panel())

    def search_marketplace(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, Any]:
        return dict(self.service.search_marketplace(query, source_id=source_id, limit=limit))

    def import_marketplace_skill(self, *, source_id: str, github_url: str) -> dict[str, Any]:
        return dict(self.service.import_marketplace_skill(source_id=source_id, github_url=github_url))

    def get_sidecar_panel(self, *, refresh: bool = False) -> dict[str, Any]:
        return dict(self.service.get_sidecar_panel(refresh=refresh))

    def save_sidecar_panel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_sidecar_panel(payload))

    def get_qq_login_panel(self, *, refresh: bool = False) -> dict[str, Any]:
        return dict(self.service.get_qq_login_panel(refresh=refresh))

    def save_qq_login_panel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_qq_login_panel(payload))

    def refresh_qq_login_panel(self) -> dict[str, Any]:
        return dict(self.service.refresh_qq_login_panel())

    def get_qq_login_qrcode_image(self) -> tuple[bytes, str] | None:
        return self.service.get_qq_login_qrcode_image()

    def get_other_panel(self) -> dict[str, Any]:
        return dict(self.service.get_other_panel())

    def save_other_panel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_other_panel(payload))

    def auth_state(self, session_token: str | None) -> dict[str, Any]:
        user = self.auth.get_session_user(session_token) if self.auth is not None else None
        return {
            "authenticated": user is not None,
            "requires_setup": self.auth.requires_setup() if self.auth is not None else False,
            "user": user,
        }

    def bootstrap_auth(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if self.auth is None:
            raise ValueError("auth is unavailable")
        username = str(payload.get("username", "") or "")
        password = str(payload.get("password", "") or "")
        user = self.auth.bootstrap_admin(username, password)
        token = self.auth.create_session(user["username"])
        return (
            {
                "status": "ok",
                "requires_setup": False,
                "user": user,
            },
            _build_session_cookie(token, max_age=self.auth.session_ttl_seconds()),
        )

    def login(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if self.auth is None:
            raise ValueError("auth is unavailable")
        username = str(payload.get("username", "") or "")
        password = str(payload.get("password", "") or "")
        user = self.auth.authenticate(username, password)
        if user is None:
            raise ValueError("invalid username or password")
        token = self.auth.create_session(user["username"])
        return (
            {
                "status": "ok",
                "requires_setup": False,
                "user": user,
            },
            _build_session_cookie(token, max_age=self.auth.session_ttl_seconds()),
        )

    def logout(self, session_token: str | None) -> tuple[dict[str, Any], str]:
        if self.auth is not None:
            self.auth.destroy_session(session_token)
        return (
            {"status": "ok"},
            _build_session_cookie("", max_age=0),
        )

    def get_user_panel(self, username: str) -> dict[str, Any]:
        if self.auth is None:
            raise ValueError("auth is unavailable")
        user = self.auth.get_user(username)
        if user is None:
            raise ValueError("user not found")
        return {
            "current_user": user,
            "users": self.auth.list_users() if user.get("role") == "admin" else [user],
            **self.service.get_member_directory_panel(),
        }

    def save_directory_member(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.service.save_directory_member(payload))

    def reset_directory_member_persona(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        detail = self.service.reset_directory_member_persona(payload)
        return dict(detail) if detail else None

    def sync_group_members(self, group_id: str) -> dict[str, Any]:
        return dict(self.service.sync_group_members(group_id))

    def change_password(self, username: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.auth is None:
            raise ValueError("auth is unavailable")
        current_password = str(payload.get("current_password", "") or "")
        new_password = str(payload.get("new_password", "") or "")
        user = self.auth.change_password(username, current_password, new_password)
        return {
            "status": "ok",
            "user": user,
        }


def make_handler(api: HttpApi):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._write_json(HTTPStatus.OK, {"status": "ok"})
                return

            if parsed.path in _STATIC_FILES:
                filename, content_type = _STATIC_FILES[parsed.path]
                self._write_static_file(filename, content_type)
                return

            if parsed.path.startswith("/assets/"):
                relative = parsed.path[len("/assets/"):]
                asset = _resolve_asset_path(relative)
                if asset is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                filename, content_type = asset
                self._write_static_file(filename, content_type)
                return

            if parsed.path == "/api/auth/state":
                self._write_json(HTTPStatus.OK, api.auth_state(self._session_token()))
                return

            current_user = None
            if parsed.path.startswith("/api/"):
                current_user = self._require_auth()
                if current_user is None:
                    return

            if parsed.path == "/api/dashboard":
                self._write_json(HTTPStatus.OK, api.dashboard_snapshot())
                return

            if parsed.path == "/api/portraits":
                query = parse_qs(parsed.query, keep_blank_values=False)
                character = str(query.get("character", [""])[0] or "").strip()
                if not character:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "character is required"})
                    return
                try:
                    asset = api.get_character_portrait(character)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                if asset is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                body, content_type = asset
                self._write_bytes(HTTPStatus.OK, body, content_type)
                return

            if parsed.path == "/api/console":
                self._write_json(HTTPStatus.OK, api.console_panels())
                return

            if parsed.path == "/api/runtime":
                self._write_json(HTTPStatus.OK, api.runtime_stats())
                return

            if parsed.path == "/api/events/recent":
                query = parse_qs(parsed.query, keep_blank_values=False)
                limit = _coerce_limit(query.get("limit", ["50"])[0])
                self._write_json(HTTPStatus.OK, api.recent_events(limit=limit))
                return

            if parsed.path == "/api/events/behavior":
                query = parse_qs(parsed.query, keep_blank_values=False)
                limit = _coerce_limit(query.get("limit", ["80"])[0])
                launcher_type = str(query.get("launcher_type", [""])[0] or "").strip()
                launcher_id = str(query.get("launcher_id", [""])[0] or "").strip()
                if launcher_type:
                    try:
                        launcher_type = _validate_launcher_type(launcher_type)
                    except ValueError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                        return
                if launcher_id:
                    try:
                        launcher_id = _validate_route_segment(launcher_id, name="launcher_id")
                    except ValueError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                        return
                self._write_json(
                    HTTPStatus.OK,
                    api.behavior_events(limit=limit, launcher_type=launcher_type, launcher_id=launcher_id),
                )
                return

            if parsed.path == "/api/panels/character":
                query = parse_qs(parsed.query, keep_blank_values=False)
                character = query.get("character", [""])[0]
                self._write_json(HTTPStatus.OK, api.get_character_panel(character))
                return

            if parsed.path == "/api/panels/ai":
                self._write_json(HTTPStatus.OK, api.get_ai_panel())
                return

            if parsed.path == "/api/panels/memory":
                self._write_json(HTTPStatus.OK, api.get_memory_panel())
                return

            if parsed.path == "/api/panels/abilities":
                self._write_json(HTTPStatus.OK, api.get_abilities_panel())
                return

            if parsed.path == "/api/panels/proactive":
                query = parse_qs(parsed.query, keep_blank_values=False)
                limit = _coerce_limit(query.get("limit", ["12"])[0])
                self._write_json(HTTPStatus.OK, api.get_proactive_panel(limit=limit))
                return

            if parsed.path == "/api/panels/skills":
                self._write_json(HTTPStatus.OK, api.get_skills_panel())
                return

            if parsed.path == "/api/panels/sidecar":
                query = parse_qs(parsed.query, keep_blank_values=False)
                refresh = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
                self._write_json(HTTPStatus.OK, api.get_sidecar_panel(refresh=refresh))
                return

            if parsed.path == "/api/panels/qq-login":
                query = parse_qs(parsed.query, keep_blank_values=False)
                refresh = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
                self._write_json(HTTPStatus.OK, api.get_qq_login_panel(refresh=refresh))
                return

            if parsed.path == "/api/qq-login/qrcode-image":
                try:
                    asset = api.get_qq_login_qrcode_image()
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                if asset is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                body, content_type = asset
                self._write_bytes(HTTPStatus.OK, body, content_type)
                return

            if parsed.path == "/api/panels/other":
                self._write_json(HTTPStatus.OK, api.get_other_panel())
                return

            if parsed.path == "/api/panels/user":
                self._write_json(HTTPStatus.OK, api.get_user_panel(str(current_user["username"])))
                return

            if parsed.path == "/api/marketplace/search":
                query = parse_qs(parsed.query, keep_blank_values=False)
                q = query.get("q", [""])[0]
                source_id = query.get("source_id", [""])[0]
                limit = _coerce_limit(query.get("limit", ["12"])[0])
                if source_id:
                    try:
                        source_id = _validate_route_segment(source_id, name="source_id")
                    except ValueError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                        return
                self._write_json(HTTPStatus.OK, api.search_marketplace(q, source_id=source_id, limit=limit))
                return

            if parsed.path == "/api/skills":
                self._write_json(HTTPStatus.OK, api.list_skills())
                return

            if parsed.path == "/api/tools":
                self._write_json(HTTPStatus.OK, api.list_tools())
                return

            if parsed.path == "/api/skill-packs/template":
                self._write_json(HTTPStatus.OK, api.skill_pack_template())
                return

            if parsed.path == "/api/skills/template":
                self._write_json(HTTPStatus.OK, api.new_skill_template())
                return

            if parsed.path.startswith("/api/skills/"):
                parts = [unquote(part) for part in parsed.path.split("/") if part]
                if len(parts) != 3:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                try:
                    _, _, skill_id = parts
                    skill_id = _validate_route_segment(skill_id, name="skill_id")
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                detail = api.get_skill_detail(skill_id)
                if detail is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, detail)
                return

            if parsed.path == "/api/sessions":
                query = parse_qs(parsed.query, keep_blank_values=False)
                limit = _coerce_limit(query.get("limit", ["24"])[0])
                self._write_json(HTTPStatus.OK, {"sessions": api.list_sessions(limit=limit)})
                return

            if parsed.path.startswith("/api/sessions/"):
                parts = [unquote(part) for part in parsed.path.split("/") if part]
                if len(parts) != 4:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                try:
                    _, _, launcher_type, launcher_id = parts
                    launcher_type = _validate_launcher_type(launcher_type)
                    launcher_id = _validate_route_segment(launcher_id, name="launcher_id")
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                detail = api.get_session_detail(launcher_type, launcher_id)
                if detail is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, detail)
                return

            self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/onebot/events":
                payload = self._read_json_body()
                if payload is None:
                    return
                status, body = api.handle_json(payload)
                if status < HTTPStatus.BAD_REQUEST:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                    return
                self._write_json(status, body)
                return

            if parsed.path == "/api/auth/bootstrap":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    body, cookie = api.bootstrap_auth(payload)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, body, headers={"Set-Cookie": cookie})
                return

            if parsed.path == "/api/auth/login":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    body, cookie = api.login(payload)
                except ValueError as exc:
                    self._write_json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized", "reason": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, body, headers={"Set-Cookie": cookie})
                return

            if parsed.path == "/api/auth/logout":
                self._read_json_body(allow_empty=True)
                body, cookie = api.logout(self._session_token())
                self._write_json(HTTPStatus.OK, body, headers={"Set-Cookie": cookie})
                return

            current_user = None
            if parsed.path.startswith("/api/"):
                current_user = self._require_auth()
                if current_user is None:
                    return

            if parsed.path == "/api/auth/change-password":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    body = api.change_password(str(current_user["username"]), payload)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, body)
                return

            if parsed.path == "/api/skills/reload":
                self._read_json_body(allow_empty=True)
                self._write_json(HTTPStatus.OK, api.reload_skills())
                return

            if parsed.path == "/api/providers/test":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    result = api.test_provider(payload)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, result)
                return

            if parsed.path == "/api/sidecar/test":
                self._read_json_body(allow_empty=True)
                self._write_json(HTTPStatus.OK, api.test_sidecar())
                return

            if parsed.path == "/api/panels/character":
                payload = self._read_json_body()
                if payload is None:
                    return
                self._write_json(HTTPStatus.OK, api.save_character_panel(payload))
                return

            if parsed.path == "/api/character/preview":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    self._write_json(HTTPStatus.OK, api.preview_character_panel(payload))
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                return

            if parsed.path == "/api/panels/ai":
                payload = self._read_json_body()
                if payload is None:
                    return
                self._write_json(HTTPStatus.OK, api.save_ai_panel(payload))
                return

            if parsed.path == "/api/panels/abilities":
                payload = self._read_json_body()
                if payload is None:
                    return
                self._write_json(HTTPStatus.OK, api.save_abilities_panel(payload))
                return

            if parsed.path == "/api/proactive/draft":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    self._write_json(HTTPStatus.OK, api.generate_proactive_draft(payload))
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                return

            if parsed.path == "/api/panels/sidecar":
                payload = self._read_json_body()
                if payload is None:
                    return
                self._write_json(HTTPStatus.OK, api.save_sidecar_panel(payload))
                return

            if parsed.path == "/api/panels/qq-login":
                payload = self._read_json_body()
                if payload is None:
                    return
                self._write_json(HTTPStatus.OK, api.save_qq_login_panel(payload))
                return

            if parsed.path == "/api/qq-login/refresh":
                self._read_json_body(allow_empty=True)
                try:
                    self._write_json(HTTPStatus.OK, api.refresh_qq_login_panel())
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                return

            if parsed.path == "/api/panels/other":
                payload = self._read_json_body()
                if payload is None:
                    return
                self._write_json(HTTPStatus.OK, api.save_other_panel(payload))
                return

            if parsed.path == "/api/users/directory/save":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    self._write_json(HTTPStatus.OK, {"status": "ok", "member": api.save_directory_member(payload)})
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                return

            if parsed.path == "/api/users/directory/reset-persona":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    detail = api.reset_directory_member_persona(payload)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                if detail is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "member": detail})
                return

            if parsed.path == "/api/users/directory/sync":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    self._write_json(
                        HTTPStatus.OK,
                        api.sync_group_members(str(payload.get("group_id", "") or "")),
                    )
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                return

            if parsed.path == "/api/knowledge/save":
                payload = self._read_json_body()
                if payload is None:
                    return
                try:
                    self._write_json(HTTPStatus.OK, {"status": "ok", "entry": api.save_knowledge_entry(payload)})
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                return

            if parsed.path == "/api/marketplace/import":
                payload = self._read_json_body()
                if payload is None:
                    return
                source_id = str(payload.get("source_id", "") or "")
                github_url = str(payload.get("github_url", "") or "")
                if not github_url:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "github_url is required"})
                    return
                if source_id:
                    try:
                        source_id = _validate_route_segment(source_id, name="source_id")
                    except ValueError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                        return
                try:
                    detail = api.import_marketplace_skill(source_id=source_id, github_url=github_url)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "skill": detail})
                return

            if parsed.path == "/api/skill-packs/export":
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                skill_ids = payload.get("skill_ids", [])
                if not isinstance(skill_ids, list):
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "skill_ids must be a list"})
                    return
                result = api.export_skill_pack(
                    skill_ids=[str(item) for item in skill_ids if str(item).strip()],
                    include_builtin=bool(payload.get("include_builtin", False)),
                    name=str(payload.get("name", "") or ""),
                    description=str(payload.get("description", "") or ""),
                )
                self._write_json(HTTPStatus.OK, result)
                return

            if parsed.path == "/api/skill-packs/import":
                payload = self._read_json_body()
                if payload is None:
                    return
                pack_payload = payload.get("bundle")
                if pack_payload is None:
                    pack_payload = payload.get("payload")
                if pack_payload is None:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "bundle is required"})
                    return
                try:
                    result = api.import_skill_pack(pack_payload, overwrite=bool(payload.get("overwrite", True)))
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "pack": result})
                return

            if parsed.path == "/api/skills/install":
                payload = self._read_json_body()
                if payload is None:
                    return
                markdown = str(payload.get("markdown", "") or "")
                filename = payload.get("filename")
                if not markdown.strip():
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "markdown is required"})
                    return
                try:
                    detail = api.install_skill(markdown, filename=str(filename) if filename else None)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "skill": detail})
                return

            if parsed.path.startswith("/api/skills/") and parsed.path.endswith("/toggle"):
                parts = [unquote(part) for part in parsed.path.split("/") if part]
                if len(parts) != 4:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                try:
                    _, _, skill_id, _ = parts
                    skill_id = _validate_route_segment(skill_id, name="skill_id")
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                payload = self._read_json_body()
                if payload is None:
                    return
                enabled = bool(payload.get("enabled", True))
                detail = api.set_skill_enabled(skill_id, enabled)
                if detail is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "skill": detail})
                return

            if parsed.path.startswith("/api/skills/") and parsed.path.endswith("/save"):
                parts = [unquote(part) for part in parsed.path.split("/") if part]
                if len(parts) != 4:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                try:
                    _, _, skill_id, _ = parts
                    skill_id = _validate_route_segment(skill_id, name="skill_id")
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                payload = self._read_json_body()
                if payload is None:
                    return
                markdown = str(payload.get("markdown", "") or "")
                if not markdown.strip():
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "markdown is required"})
                    return
                try:
                    detail = api.save_skill(skill_id, markdown)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                if detail is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "skill": detail})
                return

            if parsed.path.startswith("/api/memory/sessions/") and parsed.path.endswith("/save"):
                parts = [unquote(part) for part in parsed.path.split("/") if part]
                if len(parts) != 6:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                try:
                    _, _, _, launcher_type, launcher_id, _ = parts
                    launcher_type = _validate_launcher_type(launcher_type)
                    launcher_id = _validate_route_segment(launcher_id, name="launcher_id")
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                payload = self._read_json_body()
                if payload is None:
                    return
                detail = api.save_memory_session(launcher_type, launcher_id, payload)
                if detail is None:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "session": detail})
                return

            self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                current_user = self._require_auth()
                if current_user is None:
                    return
            if parsed.path.startswith("/api/skills/"):
                parts = [unquote(part) for part in parsed.path.split("/") if part]
                if len(parts) != 3:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                try:
                    _, _, skill_id = parts
                    skill_id = _validate_route_segment(skill_id, name="skill_id")
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": str(exc)})
                    return
                if not api.delete_skill(skill_id):
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "deleted": skill_id})
                return
            if parsed.path.startswith("/api/knowledge/"):
                parts = [unquote(part) for part in parsed.path.split("/") if part]
                if len(parts) != 3:
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                try:
                    _, _, raw_entry_id = parts
                    entry_id = int(raw_entry_id)
                except ValueError:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request", "reason": "knowledge_id must be an integer"})
                    return
                if not api.delete_knowledge_entry(entry_id):
                    self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                    return
                self._write_json(HTTPStatus.OK, {"status": "ok", "deleted": entry_id})
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_json(self, status: int, body: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            if headers:
                for key, value in headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        def _write_static_file(self, filename: str, content_type: str) -> None:
            path = (_WEB_DIR / filename).resolve()
            try:
                path.relative_to(_WEB_DIR.resolve())
            except ValueError:
                self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                return
            if not path.is_file():
                self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)

        def _write_bytes(self, status: int, body: bytes, content_type: str) -> None:
            payload = bytes(body)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(payload)

        def _session_token(self) -> str | None:
            cookies = _parse_cookie_header(self.headers.get("Cookie", ""))
            return cookies.get(_SESSION_COOKIE_NAME)

        def _require_auth(self) -> dict[str, Any] | None:
            state = api.auth_state(self._session_token())
            user = state.get("user")
            if isinstance(user, dict):
                return user
            status = {
                "status": "unauthorized",
                "requires_setup": bool(state.get("requires_setup", False)),
            }
            if status["requires_setup"]:
                status["reason"] = "initial setup required"
            else:
                status["reason"] = "authentication required"
            self._write_json(HTTPStatus.UNAUTHORIZED, status)
            return None

        def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any] | None:
            try:
                raw = _read_request_body(self)
            except RequestTooLarge as exc:
                self.close_connection = True
                self._write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"status": "payload_too_large", "reason": str(exc)})
                return None
            except ValueError:
                self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})
                return None
            if allow_empty and not raw:
                return {}
            try:
                body = _decode_request_body(raw, self.headers.get("Content-Encoding", ""))
                payload = json.loads(body)
            except RequestTooLarge as exc:
                self._write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"status": "payload_too_large", "reason": str(exc)})
                return None
            except (UnicodeDecodeError, json.JSONDecodeError):
                preview = raw[:200].decode("utf-8", errors="replace")
                print(f"bad request body preview: {preview!r}")
                self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})
                return None
            if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
                payload = payload[0]
            if not isinstance(payload, dict):
                self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})
                return None
            return payload

    return Handler


def run_server(api: HttpApi, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(api))


def _validate_route_segment(raw_value: str, *, name: str) -> str:
    resolved = str(raw_value or "").strip()
    if not _SAFE_ROUTE_SEGMENT.fullmatch(resolved):
        raise ValueError(f"invalid {name}")
    return resolved


def _validate_launcher_type(raw_value: str) -> str:
    resolved = str(raw_value or "").strip().lower()
    if resolved not in _ALLOWED_LAUNCHER_TYPES:
        raise ValueError("invalid launcher_type")
    return resolved


def _coerce_limit(raw_value: str) -> int:
    try:
        return max(1, min(200, int(raw_value)))
    except ValueError:
        return 24


def _parse_cookie_header(raw_value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in str(raw_value or "").split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


def _build_session_cookie(value: str, *, max_age: int) -> str:
    parts = [
        f"{_SESSION_COOKIE_NAME}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max(0, int(max_age))}",
    ]
    return "; ".join(parts)


def _probe_http_endpoint(base_url: str, api_key: str) -> dict[str, Any]:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL")
    request = urllib.request.Request(base_url, method="GET")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=6.0) as response:
            latency = time.monotonic() - started
            return {
                "status": "ok",
                "http_status": int(response.status),
                "latency_ms": int(latency * 1000),
                "reachable": True,
            }
    except urllib.error.HTTPError as exc:
        latency = time.monotonic() - started
        return {
            "status": "ok",
            "http_status": int(exc.code),
            "latency_ms": int(latency * 1000),
            "reachable": True,
            "note": "endpoint responded with non-2xx; service is reachable",
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "status": "error",
            "reachable": False,
            "reason": str(exc),
        }


def _resolve_asset_path(relative: str) -> tuple[str, str] | None:
    """Resolve a request path under ``/assets/`` to (filename, content-type).

    Returns None on any suspicious path so the caller can reply 404.
    """
    if not relative or relative.endswith("/"):
        return None
    if not _SAFE_ASSET_PATH.fullmatch(relative):
        return None
    if ".." in relative.split("/"):
        return None
    suffix = Path(relative).suffix.lower()
    content_type = _ASSET_CONTENT_TYPES.get(suffix)
    if content_type is None:
        return None
    target = (_WEB_DIR / "assets" / relative).resolve()
    try:
        target.relative_to((_WEB_DIR / "assets").resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    return (str(target.relative_to(_WEB_DIR)).replace("\\", "/"), content_type)


def _decode_request_body(raw: bytes, content_encoding: str) -> str:
    encoding = (content_encoding or "").strip().lower()
    body = raw
    if encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            body = stream.read(_MAX_REQUEST_BYTES + 1)
        if len(body) > _MAX_REQUEST_BYTES:
            raise RequestTooLarge("request body exceeds 10 MB limit")
    elif len(body) > _MAX_REQUEST_BYTES:
        raise RequestTooLarge("request body exceeds 10 MB limit")
    return body.decode("utf-8-sig")


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    transfer_encoding = handler.headers.get("Transfer-Encoding", "")
    if "chunked" in transfer_encoding.lower():
        return _read_chunked_body(handler)

    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length < 0:
        raise ValueError("invalid content length")
    if content_length > _MAX_REQUEST_BYTES:
        raise RequestTooLarge("request body exceeds 10 MB limit")
    return handler.rfile.read(content_length)


def _read_chunked_body(handler: BaseHTTPRequestHandler) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        size_line = handler.rfile.readline()
        if not size_line:
            raise ValueError("missing chunk size")
        size_token = size_line.strip().split(b";", 1)[0]
        try:
            size = int(size_token, 16)
        except ValueError as exc:
            raise ValueError("invalid chunk size") from exc

        if size == 0:
            while True:
                trailer = handler.rfile.readline()
                if trailer in (b"", b"\r\n", b"\n"):
                    break
            break

        total += size
        if total > _MAX_REQUEST_BYTES:
            raise RequestTooLarge("request body exceeds 10 MB limit")
        chunks.append(handler.rfile.read(size))
        chunk_end = handler.rfile.read(2)
        if chunk_end not in (b"\r\n", b"\n"):
            raise ValueError("invalid chunk terminator")

    return b"".join(chunks)
