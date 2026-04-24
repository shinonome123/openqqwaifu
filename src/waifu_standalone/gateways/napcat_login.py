from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ..infra import SyncHttpTransport, TransportError, TransportMetricsScope


class NapCatLoginError(RuntimeError):
    pass


@dataclass(slots=True)
class NapCatLoginBridge:
    base_url: str
    api_prefix: str = "/api"
    webui_token: str = ""
    timeout: float = 10.0
    _credential: str = field(default="", init=False, repr=False)
    _resolved_auth_base: str = field(default="", init=False, repr=False)
    _resolved_api_base: str = field(default="", init=False, repr=False)
    _last_status: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _last_status_at: float = field(default=0.0, init=False, repr=False)
    _last_login_info: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _transport: SyncHttpTransport = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._transport = SyncHttpTransport(
            timeout_seconds=self.timeout,
            metrics_scope=TransportMetricsScope(kind="sidecar", target="napcat_webui"),
        )

    def configured(self) -> bool:
        base, _ = normalize_webui_settings(self.base_url, self.webui_token)
        return bool(base)

    def auth_configured(self) -> bool:
        _, token = normalize_webui_settings(self.base_url, self.webui_token)
        return bool(token)

    def panel(self, *, refresh: bool = False) -> dict[str, Any]:
        normalized_base, effective_token = normalize_webui_settings(self.base_url, self.webui_token)
        panel: dict[str, Any] = {
            "configured": self.configured(),
            "token_configured": self.auth_configured(),
            "webui_base_url": normalized_base,
            "webui_api_prefix": self.api_prefix,
            "webui_timeout_seconds": self.timeout,
            "webui_url": self.webui_url(),
            "resolved_api_base": self._resolved_api_base,
            "token_from_url": bool(not str(self.webui_token or "").strip() and effective_token),
            "status": {
                "is_login": False,
                "is_offline": False,
                "qrcode_url": "",
                "login_error": "",
            },
            "login_info": {},
            "login_info_error": "",
        }
        if not refresh or not self.configured():
            return panel
        try:
            status = self._status_with_recovery(force=refresh, allow_refresh=refresh)
            panel["status"] = status
            if status.get("is_login"):
                try:
                    panel["login_info"] = self.fetch_login_info(force=refresh)
                except NapCatLoginError as exc:
                    cached_login = self._cached_login_info()
                    if cached_login:
                        panel["login_info"] = cached_login
                    panel["login_info_error"] = str(exc)
        except NapCatLoginError as exc:
            fallback_status, fallback_info = self._recover_logged_in_state(force=refresh)
            if fallback_status is not None:
                panel["status"] = fallback_status
                if fallback_info:
                    panel["login_info"] = fallback_info
            else:
                panel["error"] = str(exc)
        panel["resolved_api_base"] = self._resolved_api_base
        return panel

    def webui_url(self) -> str:
        base, _ = normalize_webui_settings(self.base_url, self.webui_token)
        if not base:
            return ""
        return f"{base}/webui"

    def refresh_qrcode(self) -> dict[str, Any]:
        fallback_status, _ = self._recover_logged_in_state(force=True)
        if fallback_status is not None and fallback_status.get("is_login"):
            self._cache_status(fallback_status)
            return dict(fallback_status)
        try:
            self._refresh_qrcode_via_api(force=True)
        except NapCatLoginError as exc:
            if not _looks_already_logged_in(str(exc)):
                raise
        status = self.fetch_status(force=True)
        if status.get("is_login"):
            return status
        return status

    def fetch_status(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._last_status and (time.time() - self._last_status_at) < 2.0:
            return dict(self._last_status)
        raw = self._request_with_auth("/QQLogin/CheckLoginStatus", {}, force=force)
        status = self._normalize_status({
            "is_login": bool(raw.get("isLogin")),
            "is_offline": bool(raw.get("isOffline")),
            "qrcode_url": str(raw.get("qrcodeurl") or ""),
            "login_error": str(raw.get("loginError") or ""),
        })
        self._cache_status(status)
        return dict(status)

    def fetch_login_info(self, *, force: bool = False) -> dict[str, Any]:
        raw = self._request_with_auth("/QQLogin/GetQQLoginInfo", {}, force=force)
        info = {
            "uin": str(raw.get("uin") or raw.get("user_id") or raw.get("qq") or ""),
            "nickname": str(raw.get("nickname") or raw.get("nick") or ""),
            "avatar_url": str(raw.get("avatarUrl") or raw.get("avatar_url") or ""),
            "online": bool(raw.get("online", False)),
            "raw": raw,
        }
        if info.get("uin"):
            self._last_login_info = dict(info)
        return info

    def qrcode_payload(self, *, force: bool = False) -> str:
        status = self._status_with_recovery(force=force, allow_refresh=True)
        if status.get("is_login"):
            raise NapCatLoginError("QQ is already logged in")
        return str(status.get("qrcode_url") or "")

    def reset_auth(self) -> None:
        self._credential = ""
        self._resolved_auth_base = ""
        self._resolved_api_base = ""
        self._last_status = {}
        self._last_status_at = 0.0

    def close(self) -> None:
        self._transport.close()

    def _request_with_auth(
        self,
        route: str,
        payload: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        self._ensure_credential(force=force)
        if not self._credential or not self._resolved_api_base:
            raise NapCatLoginError("NapCat WebUI credential is unavailable")
        try:
            return self._request_against_candidates(
                route,
                payload,
                authorization=f"Bearer {self._credential}",
            )
        except NapCatLoginError as exc:
            if not (_looks_unauthorized(str(exc)) or _looks_missing_route(str(exc))):
                raise
        self._ensure_credential(force=True)
        if not self._credential or not self._resolved_api_base:
            raise NapCatLoginError("NapCat WebUI credential refresh failed")
        return self._request_against_candidates(
            route,
            payload,
            authorization=f"Bearer {self._credential}",
        )

    def _status_with_recovery(self, *, force: bool, allow_refresh: bool) -> dict[str, Any]:
        status = self.fetch_status(force=force)
        if allow_refresh and _status_needs_qrcode_refresh(status):
            self._refresh_qrcode_via_api(force=True)
            status = self.fetch_status(force=True)
        return status

    def _refresh_qrcode_via_api(self, *, force: bool) -> None:
        self._request_with_auth("/QQLogin/RefreshQRcode", {}, force=force)
        self._last_status = {}
        self._last_status_at = 0.0

    def _cache_status(self, status: dict[str, Any]) -> None:
        self._last_status = dict(self._normalize_status(status))
        self._last_status_at = time.time()

    def _normalize_status(self, status: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "is_login": bool(status.get("is_login")),
            "is_offline": bool(status.get("is_offline")),
            "qrcode_url": str(status.get("qrcode_url") or ""),
            "login_error": str(status.get("login_error") or ""),
        }
        if normalized["is_login"]:
            normalized["qrcode_url"] = ""
            normalized["login_error"] = ""
        return normalized

    def _cached_login_info(self) -> dict[str, Any]:
        return dict(self._last_login_info)

    def _recover_logged_in_state(self, *, force: bool) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        try:
            login_info = self.fetch_login_info(force=force)
        except NapCatLoginError:
            login_info = self._cached_login_info()
        if str(login_info.get("uin") or "").strip():
            status = self._normalize_status(
                {
                    "is_login": True,
                    "is_offline": not bool(login_info.get("online", False)),
                    "qrcode_url": "",
                    "login_error": "",
                }
            )
            self._cache_status(status)
            return status, login_info
        cached_status = self._normalize_status(self._last_status)
        if cached_status.get("is_login"):
            return cached_status, self._cached_login_info()
        return None, {}

    def _ensure_credential(self, *, force: bool = False) -> None:
        if not self.configured():
            raise NapCatLoginError("NapCat WebUI base URL is not configured")
        normalized_base, effective_token = normalize_webui_settings(self.base_url, self.webui_token)
        if not effective_token:
            raise NapCatLoginError("NapCat WebUI token is not configured")
        if self._credential and (self._resolved_auth_base or self._resolved_api_base) and not force:
            return
        last_error = ""
        self._credential = ""
        for api_base in self._candidate_auth_bases():
            try:
                result = self._post_json(
                    api_base,
                    "/auth/login",
                    {"hash": _hash_webui_token(effective_token)},
                )
                credential = str(result.get("Credential") or result.get("credential") or "")
                if not credential:
                    raise NapCatLoginError("NapCat WebUI did not return a credential")
                self._credential = credential
                self._resolved_auth_base = api_base
                self._resolved_api_base = api_base
                return
            except NapCatLoginError as exc:
                last_error = str(exc)
        raise NapCatLoginError(last_error or "NapCat WebUI login failed")

    def _candidate_auth_bases(self) -> list[str]:
        candidates = []
        seen: set[str] = set()
        preferred = str(self._resolved_auth_base or self._resolved_api_base or "").strip()
        for item in [preferred, *self._candidate_api_bases()]:
            normalized = str(item or "").rstrip("/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
        return candidates

    def _candidate_api_bases(self) -> list[str]:
        base, _ = normalize_webui_settings(self.base_url, self.webui_token)
        prefix = _normalize_prefix(self.api_prefix)
        if not base:
            return []
        candidates: list[str] = []
        if prefix:
            if base.endswith(prefix):
                candidates.append(base)
            else:
                candidates.append(f"{base}{prefix}")
                candidates.append(base)
        else:
            candidates.append(base)
        if f"{base}/api" not in candidates:
            candidates.append(f"{base}/api")
        seen: set[str] = set()
        unique: list[str] = []
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _request_against_candidates(
        self,
        route: str,
        payload: dict[str, Any],
        *,
        authorization: str,
    ) -> dict[str, Any]:
        last_error = ""
        for api_base in self._candidate_route_bases():
            try:
                result = self._post_json(
                    api_base,
                    route,
                    payload,
                    authorization=authorization,
                )
                self._resolved_api_base = api_base
                return result
            except NapCatLoginError as exc:
                last_error = str(exc)
                if _looks_missing_route(last_error):
                    continue
                raise
        raise NapCatLoginError(last_error or "NapCat WebUI request failed")

    def _candidate_route_bases(self) -> list[str]:
        candidates = []
        seen: set[str] = set()
        preferred = str(self._resolved_api_base or "").strip()
        for item in [preferred, *self._candidate_api_bases()]:
            normalized = str(item or "").rstrip("/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
        return candidates

    def _post_json(
        self,
        api_base: str,
        route: str,
        payload: dict[str, Any],
        *,
        authorization: str = "",
    ) -> dict[str, Any]:
        endpoint = f"{api_base.rstrip('/')}/{route.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        if authorization:
            headers["Authorization"] = authorization
        try:
            response = self._transport.request(
                "POST",
                endpoint,
                headers=headers,
                json_payload=payload,
            )
        except TransportError as exc:
            raise NapCatLoginError(_napcat_transport_error_message(exc)) from exc
        parsed = _parse_json_response(response.content)
        code = parsed.get("code")
        if code not in {None, 0}:
            raise NapCatLoginError(str(parsed.get("message") or parsed.get("error") or "NapCat request failed"))
        data = parsed.get("data")
        return data if isinstance(data, dict) else {}


def _parse_json_response(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {"data": data}


def _napcat_transport_error_message(exc: TransportError) -> str:
    body = str(exc.body or "").strip()
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "").strip()
            if message:
                return message
    if exc.status_code is not None:
        return f"NapCat WebUI request failed ({exc.status_code})"
    return f"NapCat WebUI is unreachable: {exc}"


def _normalize_prefix(prefix: str) -> str:
    value = str(prefix or "").strip()
    if not value:
        return ""
    return "/" + value.strip("/")


def normalize_webui_settings(base_url: str, webui_token: str) -> tuple[str, str]:
    raw_base = str(base_url or "").strip()
    token = str(webui_token or "").strip()
    if not raw_base:
        return "", token
    parsed = urllib.parse.urlsplit(raw_base)
    query = urllib.parse.parse_qs(parsed.query)
    if not token:
        token = str((query.get("token") or [""])[0]).strip()
    path = (parsed.path or "").rstrip("/")
    lowered = path.lower()
    while lowered.endswith("/webui") or lowered.endswith("/api"):
        if lowered.endswith("/webui"):
            path = path[: -len("/webui")]
        elif lowered.endswith("/api"):
            path = path[: -len("/api")]
        path = path.rstrip("/")
        lowered = path.lower()
    normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return normalized.rstrip("/"), token


def _hash_webui_token(token: str) -> str:
    return hashlib.sha256(f"{str(token or '')}.napcat".encode("utf-8")).hexdigest()


def _looks_unauthorized(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(
        token in lowered
        for token in (
            "unauthorized",
            "authorization failed",
            "invalid token",
            "token expired",
            "credential expired",
        )
    )


def _looks_missing_route(message: str) -> bool:
    lowered = str(message or "").lower()
    return "(404)" in lowered or "cannot get " in lowered or "cannot post " in lowered or "not found" in lowered


def _looks_already_logged_in(message: str) -> bool:
    lowered = str(message or "").strip().lower()
    return any(
        token in lowered
        for token in (
            "qq is logined",
            "already logged in",
            "already login",
            "已登录",
            "宸茬櫥褰?",
        )
    )


def qrcode_payload_to_image_source(payload: str) -> tuple[str, bytes]:
    value = str(payload or "").strip()
    if not value:
        raise NapCatLoginError("NapCat did not return a QR payload")
    if value.startswith("data:image/"):
        return _decode_data_image(value)
    return _render_local_qrcode_png(value)


def _decode_data_image(value: str) -> tuple[str, bytes]:
    header, _, raw = value.partition(",")
    if not raw:
        raise NapCatLoginError("Invalid data URL returned by NapCat")
    content_type = header.removeprefix("data:").split(";", 1)[0] or "image/png"
    if ";base64" in header:
        return content_type, base64.b64decode(raw)
    return content_type, urllib.parse.unquote_to_bytes(raw)


def _render_local_qrcode_png(value: str) -> tuple[str, bytes]:
    try:
        import qrcode
    except ImportError as exc:
        raise NapCatLoginError("Local QR renderer is unavailable; install qrcode[pil]") from exc

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "image/png", buffer.getvalue()


def _status_needs_qrcode_refresh(status: dict[str, Any]) -> bool:
    if bool(status.get("is_login")):
        return False
    qrcode_url = str(status.get("qrcode_url") or "").strip()
    if not qrcode_url:
        return True
    return _looks_like_expired_qrcode_error(str(status.get("login_error") or ""))


def _looks_like_expired_qrcode_error(message: str) -> bool:
    lowered = str(message or "").strip().lower()
    if not lowered:
        return False
    return any(
        token in lowered
        for token in (
            "expired",
            "expire",
            "timeout",
            "invalid qrcode",
            "二维码已失效",
            "二维码过期",
            "二维码失效",
            "请刷新",
            "已过期",
        )
    )
