from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import ClawRuntimeConfig
from .http_transport import SyncHttpTransport, TransportError
from .observability import TransportMetricsScope

_LOGGER = logging.getLogger(__name__)
_RUNTIME_VERSION = "0.1.0"


def build_claw_runtime_bridge(config: ClawRuntimeConfig, *, data_root: str) -> "ClawRuntimeBridge":
    return ClawRuntimeBridge(config, data_root=data_root)


class ClawRuntimeBridge:
    def __init__(self, config: ClawRuntimeConfig, *, data_root: str) -> None:
        self.config = config
        self._data_root = str(data_root or ".")
        self._transport = SyncHttpTransport(
            timeout_seconds=max(2.0, float(config.startup_timeout_seconds or 10.0)),
            metrics_scope=TransportMetricsScope(kind="claw_runtime", target="node_runtime"),
        )
        self._process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._stderr_handle: Any = None
        self._base_url = self._resolve_base_url()
        self._last_error = ""
        self._last_health: dict[str, object] = {}
        self._last_health_at = 0.0

    def describe(self, *, refresh: bool = False) -> dict[str, object]:
        payload = {
            "enabled": bool(self.config.enabled),
            "mode": str(self.config.mode or "managed"),
            "routing_mode": str(self.config.routing_mode or "shadow"),
            "base_url": self._base_url,
            "node_path": self._resolved_node_path(),
            "runtime_root": str(self._runtime_root()),
            "acp_enabled": bool(self.config.acp_enabled),
            "acp_default_configured": bool(str(self.config.acp_default_command or "").strip()),
            "codex_harness_configured": bool(str(self.config.codex_harness_command or "").strip()),
            "acp_session_timeout_seconds": float(self.config.acp_session_timeout_seconds or 2.0),
            "plugin_tools_mcp_bridge": bool(self.config.plugin_tools_mcp_bridge),
            "version": _RUNTIME_VERSION,
            "state": "disabled",
            "healthy": False,
            "process_running": bool(self._process is not None and self._process.poll() is None),
            "error": self._last_error,
        }
        if not self.config.enabled:
            return payload
        if refresh:
            try:
                health = self._health(start_if_needed=True)
                payload.update(health)
                payload["healthy"] = True
                payload["state"] = "running" if str(self.config.mode or "managed") == "managed" else "external"
                payload["process_running"] = bool(self._process is not None and self._process.poll() is None)
                payload["error"] = ""
                return payload
            except Exception as exc:  # pragma: no cover - defensive logging path
                self._last_error = str(exc)
                payload["state"] = "error"
                payload["error"] = self._last_error
                payload["process_running"] = bool(self._process is not None and self._process.poll() is None)
                return payload
        if self._process is not None and self._process.poll() is None:
            payload["state"] = "running"
        elif str(self.config.mode or "managed").strip().lower() == "external":
            payload["state"] = "external"
        else:
            payload["state"] = "idle"
        if self._last_health:
            payload.update(self._last_health)
            payload["healthy"] = True
        return payload

    def list_plugins(self) -> dict[str, object]:
        return self._request_json("GET", "/plugins")

    def inspect_plugin(self, plugin_id: str) -> dict[str, object] | None:
        target = str(plugin_id or "").strip()
        if not target:
            return None
        try:
            return self._request_json("GET", f"/plugins/{target}")
        except RuntimeError as exc:
            if "not_found" in str(exc):
                return None
            raise

    def install_plugin(
        self,
        source: str | Path,
        *,
        overwrite: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise ValueError("source path does not exist")
        payload = {
            "source_path": str(source_path),
            "overwrite": bool(overwrite),
            "metadata": metadata or {},
        }
        body = self._request_json("POST", "/plugins/install", payload)
        plugin = body.get("plugin")
        if not isinstance(plugin, dict):
            raise RuntimeError("claw runtime install did not return plugin details")
        return plugin

    def update_plugin(
        self,
        plugin_id: str,
        *,
        source: str | Path | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "plugin_id": str(plugin_id or "").strip(),
            "metadata": metadata or {},
        }
        if source is not None:
            source_path = Path(source).expanduser().resolve()
            if not source_path.exists():
                raise ValueError("source path does not exist")
            payload["source_path"] = str(source_path)
        body = self._request_json("POST", "/plugins/update", payload)
        plugin = body.get("plugin")
        if not isinstance(plugin, dict):
            raise RuntimeError("claw runtime update did not return plugin details")
        return plugin

    def check_plugins(self) -> dict[str, object]:
        return self._request_json("POST", "/plugins/check", {})

    def sync_workspace_bundles(
        self,
        workspace_root: str | Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, object]:
        root = Path(workspace_root).expanduser().resolve()
        if not root.exists():
            return {"items": [], "imported_count": 0, "skipped_count": 0, "errors": []}
        imported: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        skipped = 0
        candidates: list[Path] = []
        for child in sorted(root.iterdir()):
            if child.is_dir():
                candidates.append(child)
                continue
            if child.is_file() and child.suffix.lower() == ".md":
                candidates.append(child)
        for candidate in candidates:
            try:
                imported.append(
                    self.install_plugin(
                        candidate,
                        overwrite=overwrite,
                        metadata={"source_id": "workspace", "source_url": str(candidate)},
                    )
                )
            except Exception as exc:
                if "already exists" in str(exc):
                    skipped += 1
                    continue
                errors.append({"source": str(candidate), "reason": str(exc)})
        return {
            "items": imported,
            "imported_count": len(imported),
            "skipped_count": skipped,
            "errors": errors,
        }

    def list_skills(self) -> dict[str, object]:
        return self._request_json("GET", "/skills")

    def inspect_skill(self, skill_id: str) -> dict[str, object] | None:
        target = str(skill_id or "").strip()
        if not target:
            return None
        try:
            return self._request_json("GET", f"/skills/{target}")
        except RuntimeError as exc:
            if "not_found" in str(exc):
                return None
            raise

    def list_tools(self) -> dict[str, object]:
        return self._request_json("GET", "/tools")

    def invoke_tool(self, tool_id: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        return self._request_json(
            "POST",
            "/tools/invoke",
            {
                "tool_id": str(tool_id or "").strip(),
                "arguments": arguments or {},
            },
        )

    def start_acp_session(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        return self._request_json("POST", "/acp/sessions", payload or {})

    def send_acp_input(self, session_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        target = str(session_id or "").strip()
        if not target:
            raise ValueError("session_id is required")
        return self._request_json("POST", f"/acp/sessions/{target}/input", payload or {})

    def close_acp_session(self, session_id: str) -> dict[str, object]:
        target = str(session_id or "").strip()
        if not target:
            raise ValueError("session_id is required")
        return self._request_json("DELETE", f"/acp/sessions/{target}")

    def close(self) -> None:
        self._transport.close()
        with self._process_lock:
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
            self._stderr_handle = None

    def _health(self, *, start_if_needed: bool) -> dict[str, object]:
        if not self.config.enabled:
            raise RuntimeError("claw runtime is disabled")
        if start_if_needed:
            self._ensure_started()
        response = self._transport.request("GET", f"{self._base_url}/healthz")
        payload = json.loads(response.text)
        self._last_health = payload if isinstance(payload, dict) else {}
        self._last_health_at = time.time()
        return dict(self._last_health)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._ensure_started()
        try:
            response = self._transport.request(
                method,
                f"{self._base_url}{path}",
                json_payload=payload,
            )
        except TransportError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            body = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("claw runtime returned invalid json") from exc
        if not isinstance(body, dict):
            raise RuntimeError("claw runtime returned invalid payload")
        return body

    def _ensure_started(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("claw runtime is disabled")
        mode = str(self.config.mode or "managed").strip().lower()
        if mode == "external":
            if not self._base_url:
                raise RuntimeError("claw runtime base_url is required in external mode")
            self._health(start_if_needed=False)
            return
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                self._health(start_if_needed=False)
                return
            if self._process is not None and self._process.poll() is not None:
                self._last_error = self._tail_runtime_log()
                self._process = None
            self._start_managed_runtime_locked()
        self._health(start_if_needed=False)

    def _start_managed_runtime_locked(self) -> None:
        runtime_root = self._runtime_root()
        runtime_root.mkdir(parents=True, exist_ok=True)
        logs_root = runtime_root / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        self._base_url = self._resolve_base_url()
        if not self._base_url:
            raise RuntimeError("failed to resolve managed claw runtime base_url")
        stderr_path = logs_root / "claw-runtime.stderr.log"
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
        self._stderr_handle = stderr_path.open("a", encoding="utf-8")
        port = _port_from_url(self._base_url)
        command = [
            self._resolved_node_path(),
            str(_runtime_server_path()),
            "--root",
            str(runtime_root),
            "--port",
            str(port),
            "--routing-mode",
            str(self.config.routing_mode or "shadow"),
            "--acp-enabled",
            "true" if self.config.acp_enabled else "false",
            "--acp-default-command",
            str(self.config.acp_default_command or ""),
            "--acp-default-args",
            json.dumps(list(self.config.acp_default_args or []), ensure_ascii=False),
            "--codex-harness-command",
            str(self.config.codex_harness_command or ""),
            "--codex-harness-args",
            json.dumps(list(self.config.codex_harness_args or []), ensure_ascii=False),
            "--acp-session-timeout-seconds",
            str(float(self.config.acp_session_timeout_seconds or 2.0)),
            "--plugin-tools-mcp-bridge",
            "true" if self.config.plugin_tools_mcp_bridge else "false",
        ]
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = int(subprocess.CREATE_NO_WINDOW)
        self._process = subprocess.Popen(
            command,
            cwd=str(runtime_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_handle,
            text=True,
            creationflags=creationflags,
        )
        deadline = time.monotonic() + max(2.0, float(self.config.startup_timeout_seconds or 10.0))
        last_error = ""
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                last_error = self._tail_runtime_log()
                break
            try:
                self._transport.request("GET", f"{self._base_url}/healthz")
                self._last_error = ""
                return
            except Exception as exc:  # pragma: no cover - startup race
                last_error = str(exc)
                time.sleep(0.2)
        self._last_error = last_error or self._tail_runtime_log() or "claw runtime failed to start"
        raise RuntimeError(self._last_error)

    def _resolved_node_path(self) -> str:
        return str(self.config.node_path or "node").strip() or "node"

    def _resolve_base_url(self) -> str:
        configured = _normalize_base_url(self.config.base_url)
        mode = str(self.config.mode or "managed").strip().lower()
        if mode == "external":
            return configured
        if configured:
            return configured
        port = _pick_ephemeral_port()
        return f"http://127.0.0.1:{port}"

    def _runtime_root(self) -> Path:
        configured = str(self.config.runtime_root or "").strip()
        if configured:
            root = Path(configured).expanduser()
            if not root.is_absolute():
                root = (Path(self._data_root).resolve().parent / root).resolve()
            return root
        return (Path(self._data_root) / "claw_runtime").resolve()

    def _tail_runtime_log(self, *, max_lines: int = 10) -> str:
        runtime_log = self._runtime_root() / "logs" / "claw-runtime.stderr.log"
        if not runtime_log.exists():
            return ""
        try:
            lines = runtime_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])


def _runtime_server_path() -> Path:
    return Path(__file__).resolve().with_name("claw_runtime_server.mjs")


def _normalize_base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return raw


def _port_from_url(base_url: str) -> int:
    parsed = urlparse(str(base_url or "").strip())
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def _pick_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])
