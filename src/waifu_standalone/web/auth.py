from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PASSWORD_ITERATIONS = 310_000
_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
_USERNAME_MIN_LENGTH = 3
_PASSWORD_MIN_LENGTH = 6


@dataclass(slots=True)
class AuthUser:
    username: str
    role: str
    created_at: float
    updated_at: float
    last_login_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
        }


class AuthManager:
    def __init__(self, data_root: str | Path):
        self.data_root = Path(data_root).resolve()
        self.path = self.data_root / "auth" / "users.json"
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._users: dict[str, dict[str, Any]] = {}
        self._load()

    def requires_setup(self) -> bool:
        with self._lock:
            return not bool(self._users)

    def bootstrap_admin(self, username: str, password: str) -> dict[str, Any]:
        normalized = _normalize_username(username)
        _validate_password(password)
        with self._lock:
            if self._users:
                raise ValueError("setup has already been completed")
            user = self._build_user_record(normalized, password=password, role="admin")
            self._users = {normalized: user}
            self._save_locked()
            return _public_user(user)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        normalized = _normalize_username(username)
        with self._lock:
            user = self._users.get(normalized)
            if user is None:
                return None
            if not _verify_password(password, salt=user["password_salt"], expected=user["password_hash"]):
                return None
            user["last_login_at"] = time.time()
            user["updated_at"] = time.time()
            self._save_locked()
            return _public_user(user)

    def get_user(self, username: str) -> dict[str, Any] | None:
        normalized = _normalize_username(username)
        with self._lock:
            user = self._users.get(normalized)
            return _public_user(user) if user is not None else None

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            users = [_public_user(item) for item in self._users.values()]
        return sorted(users, key=lambda item: (item["role"] != "admin", item["username"]))

    def change_password(self, username: str, current_password: str, new_password: str) -> dict[str, Any]:
        normalized = _normalize_username(username)
        _validate_password(new_password)
        with self._lock:
            user = self._users.get(normalized)
            if user is None:
                raise ValueError("user not found")
            if not _verify_password(current_password, salt=user["password_salt"], expected=user["password_hash"]):
                raise ValueError("current password is incorrect")
            password_hash, password_salt = _hash_password(new_password)
            user["password_hash"] = password_hash
            user["password_salt"] = password_salt
            user["updated_at"] = time.time()
            self._save_locked()
            return _public_user(user)

    def create_session(self, username: str) -> str:
        normalized = _normalize_username(username)
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + _SESSION_TTL_SECONDS
        with self._lock:
            if normalized not in self._users:
                raise ValueError("user not found")
            self._sessions[token] = {
                "username": normalized,
                "created_at": time.time(),
                "expires_at": expires_at,
            }
        return token

    def get_session_user(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            self._purge_expired_sessions_locked()
            session = self._sessions.get(token)
            if session is None:
                return None
            user = self._users.get(str(session["username"]))
            if user is None:
                self._sessions.pop(token, None)
                return None
            return _public_user(user)

    def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def session_ttl_seconds(self) -> int:
        return _SESSION_TTL_SECONDS

    def _load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._users = {}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        users = raw.get("users", [])
        if not isinstance(users, list):
            self._users = {}
            return
        loaded: dict[str, dict[str, Any]] = {}
        for item in users:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "")).strip()
            if not username:
                continue
            loaded[username] = {
                "username": username,
                "role": str(item.get("role", "user") or "user"),
                "password_hash": str(item.get("password_hash", "") or ""),
                "password_salt": str(item.get("password_salt", "") or ""),
                "created_at": float(item.get("created_at", time.time()) or time.time()),
                "updated_at": float(item.get("updated_at", time.time()) or time.time()),
                "last_login_at": float(item.get("last_login_at", 0.0) or 0.0),
            }
        self._users = loaded

    def _save_locked(self) -> None:
        payload = {
            "version": 1,
            "users": list(self._users.values()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _purge_expired_sessions_locked(self) -> None:
        now = time.time()
        expired = [token for token, session in self._sessions.items() if float(session.get("expires_at", 0.0)) <= now]
        for token in expired:
            self._sessions.pop(token, None)

    @staticmethod
    def _build_user_record(username: str, *, password: str, role: str) -> dict[str, Any]:
        password_hash, password_salt = _hash_password(password)
        timestamp = time.time()
        return {
            "username": username,
            "role": role,
            "password_hash": password_hash,
            "password_salt": password_salt,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_login_at": 0.0,
        }


def _public_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return AuthUser(
        username=str(user["username"]),
        role=str(user.get("role", "user") or "user"),
        created_at=float(user.get("created_at", 0.0) or 0.0),
        updated_at=float(user.get("updated_at", 0.0) or 0.0),
        last_login_at=float(user.get("last_login_at", 0.0) or 0.0),
    ).as_dict()


def _normalize_username(username: str) -> str:
    normalized = str(username or "").strip()
    if len(normalized) < _USERNAME_MIN_LENGTH:
        raise ValueError("username must be at least 3 characters")
    if len(normalized) > 32:
        raise ValueError("username must be 32 characters or fewer")
    if any(ch.isspace() for ch in normalized):
        raise ValueError("username must not contain spaces")
    return normalized


def _validate_password(password: str) -> None:
    value = str(password or "")
    if len(value) < _PASSWORD_MIN_LENGTH:
        raise ValueError("password must be at least 6 characters")


def _hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return (
        base64.b64encode(digest).decode("ascii"),
        base64.b64encode(salt).decode("ascii"),
    )


def _verify_password(password: str, *, salt: str, expected: str) -> bool:
    try:
        salt_bytes = base64.b64decode(salt.encode("ascii"))
        expected_bytes = base64.b64decode(expected.encode("ascii"))
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, _PASSWORD_ITERATIONS)
    return hmac.compare_digest(digest, expected_bytes)
