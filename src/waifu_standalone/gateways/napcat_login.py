from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class NapCatLoginError(RuntimeError):
    pass


@dataclass(slots=True)
class NapCatLoginBridge:
    base_url: str
    api_prefix: str = "/api"
    webui_token: str = ""
    timeout: float = 10.0
    _credential: str = field(default="", init=False, repr=False)
    _resolved_api_base: str = field(default="", init=False, repr=False)

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
        }
        if not refresh or not self.configured():
            return panel
        try:
            status = self.fetch_status()
            panel["status"] = status
            if status.get("is_login"):
                panel["login_info"] = self.fetch_login_info()
        except NapCatLoginError as exc:
            panel["error"] = str(exc)
        return panel

    def webui_url(self) -> str:
        base, _ = normalize_webui_settings(self.base_url, self.webui_token)
        if not base:
            return ""
        return f"{base}/webui"

    def refresh_qrcode(self) -> dict[str, Any]:
        self._request_with_auth("/QQLogin/RefreshQRcode", {})
        return self.fetch_status(force=True)

    def fetch_status(self, *, force: bool = False) -> dict[str, Any]:
        raw = self._request_with_auth("/QQLogin/CheckLoginStatus", {}, force=force)
        return {
            "is_login": bool(raw.get("isLogin")),
            "is_offline": bool(raw.get("isOffline")),
            "qrcode_url": str(raw.get("qrcodeurl") or ""),
            "login_error": str(raw.get("loginError") or ""),
        }

    def fetch_login_info(self) -> dict[str, Any]:
        raw = self._request_with_auth("/QQLogin/GetQQLoginInfo", {})
        return {
            "uin": str(raw.get("uin") or raw.get("user_id") or raw.get("qq") or ""),
            "nickname": str(raw.get("nickname") or raw.get("nick") or ""),
            "avatar_url": str(raw.get("avatarUrl") or raw.get("avatar_url") or ""),
            "online": bool(raw.get("online", False)),
            "raw": raw,
        }

    def qrcode_payload(self) -> str:
        status = self.fetch_status(force=True)
        return str(status.get("qrcode_url") or "")

    def reset_auth(self) -> None:
        self._credential = ""
        self._resolved_api_base = ""

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
            return self._post_json(
                self._resolved_api_base,
                route,
                payload,
                authorization=f"Bearer {self._credential}",
            )
        except NapCatLoginError as exc:
            if not _looks_unauthorized(str(exc)):
                raise
        self._ensure_credential(force=True)
        if not self._credential or not self._resolved_api_base:
            raise NapCatLoginError("NapCat WebUI credential refresh failed")
        return self._post_json(
            self._resolved_api_base,
            route,
            payload,
            authorization=f"Bearer {self._credential}",
        )

    def _ensure_credential(self, *, force: bool = False) -> None:
        if not self.configured():
            raise NapCatLoginError("NapCat WebUI base URL is not configured")
        normalized_base, effective_token = normalize_webui_settings(self.base_url, self.webui_token)
        if not effective_token:
            raise NapCatLoginError("NapCat WebUI token is not configured")
        if self._credential and self._resolved_api_base and not force:
            return
        last_error = ""
        self._credential = ""
        for api_base in self._candidate_api_bases():
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
                self._resolved_api_base = api_base
                return
            except NapCatLoginError as exc:
                last_error = str(exc)
        raise NapCatLoginError(last_error or "NapCat WebUI login failed")

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
        seen: set[str] = set()
        unique: list[str] = []
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _post_json(
        self,
        api_base: str,
        route: str,
        payload: dict[str, Any],
        *,
        authorization: str = "",
    ) -> dict[str, Any]:
        endpoint = f"{api_base.rstrip('/')}/{route.lstrip('/')}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = _read_http_error(exc)
            raise NapCatLoginError(detail) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise NapCatLoginError(f"NapCat WebUI is unreachable: {reason}") from exc
        parsed = _parse_json_response(raw)
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


def _read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        payload = _parse_json_response(exc.read())
    except Exception:
        payload = {}
    message = str(payload.get("message") or payload.get("error") or "").strip()
    if message:
        return message
    return f"NapCat WebUI request failed ({exc.code})"


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
    return "unauthorized" in lowered or "authorization failed" in lowered or "token" in lowered


def qrcode_payload_to_image_source(payload: str) -> tuple[str, bytes]:
    value = str(payload or "").strip()
    if not value:
        raise NapCatLoginError("NapCat did not return a QR payload")
    if value.startswith("data:image/"):
        return _decode_data_image(value)
    encoded = urllib.parse.quote(value, safe="")
    request = urllib.request.Request(
        f"https://api.qrserver.com/v1/create-qr-code/?size=240x240&data={encoded}",
        headers={"Accept": "image/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            content_type = response.headers.get_content_type() or "image/png"
            return content_type, response.read()
    except urllib.error.URLError as exc:
        raise NapCatLoginError(f"Failed to render QQ QR code: {getattr(exc, 'reason', exc)}") from exc


def _decode_data_image(value: str) -> tuple[str, bytes]:
    header, _, raw = value.partition(",")
    if not raw:
        raise NapCatLoginError("Invalid data URL returned by NapCat")
    content_type = header.removeprefix("data:").split(";", 1)[0] or "image/png"
    if ";base64" in header:
        return content_type, base64.b64decode(raw)
    return content_type, urllib.parse.unquote_to_bytes(raw)
