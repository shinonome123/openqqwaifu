from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from ..config import (
    AppConfig,
    EmbeddingConfig,
    ImageGenerationConfig,
    LLMConfig,
    MarketplaceConfig,
    MarketplaceSourceConfig,
    QQSidecarConfig,
)


class ConfigManager:
    """Small config loader inspired by the upstream Waifu layout."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None

    def load(self) -> AppConfig:
        if self.path is None or not self.path.exists():
            config = _apply_env_overrides(AppConfig())
            config = _apply_napcat_autodiscovery(config, config_path=self.path)
            return _normalize_webui_fields(config)

        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        data_root = str(raw.get("data_root", "data"))
        if not Path(data_root).is_absolute():
            data_root = str((self.path.parent / data_root).resolve())

        qq_sidecar = QQSidecarConfig(**raw.get("qq_sidecar", {}))
        llm = LLMConfig(**raw.get("llm", {}))
        image_generation = ImageGenerationConfig(**raw.get("image_generation", {}))
        embedding = EmbeddingConfig(**raw.get("embedding", {}))
        marketplace = _load_marketplace(raw.get("marketplace"))
        config = AppConfig(
            service_name=str(raw.get("service_name", "waifu-standalone")),
            config_path=str(self.path.resolve()),
            assistant_name=str(raw.get("assistant_name", "鐞夌拑")),
            character=str(raw.get("character", "default")),
            bot_account_id=str(raw.get("bot_account_id", "")),
            group_reply_requires_mention=bool(raw.get("group_reply_requires_mention", True)),
            skills_enabled=bool(raw.get("skills_enabled", True)),
            max_active_skills=int(raw.get("max_active_skills", 3)),
            search_enabled=bool(raw.get("search_enabled", True)),
            search_result_limit=int(raw.get("search_result_limit", 3)),
            search_timeout_seconds=float(raw.get("search_timeout_seconds", 8.0)),
            thinking_mode=bool(raw.get("thinking_mode", True)),
            conversation_analysis=bool(raw.get("conversation_analysis", True)),
            summarization_mode=bool(raw.get("summarization_mode", False)),
            member_auto_sync=bool(raw.get("member_auto_sync", True)),
            knowledge_auto_extract=bool(raw.get("knowledge_auto_extract", True)),
            knowledge_auto_extract_limit=int(raw.get("knowledge_auto_extract_limit", 2)),
            event_mode=bool(raw.get("event_mode", True)),
            event_buffer_limit=int(raw.get("event_buffer_limit", 120)),
            narrator_mode=bool(raw.get("narrator_mode", True)),
            narrator_style=str(raw.get("narrator_style", "subtle")),
            narrator_detail_level=int(raw.get("narrator_detail_level", 2)),
            value_game_mode=bool(raw.get("value_game_mode", True)),
            value_game_reply_bonus=float(raw.get("value_game_reply_bonus", 0.08)),
            memory_graph_mode=bool(raw.get("memory_graph_mode", True)),
            memory_graph_limit=int(raw.get("memory_graph_limit", 8)),
            proactive_mode=bool(raw.get("proactive_mode", False)),
            proactive_inactive_hours=float(raw.get("proactive_inactive_hours", 6.0)),
            proactive_candidate_limit=int(raw.get("proactive_candidate_limit", 6)),
            proactive_min_affinity=float(raw.get("proactive_min_affinity", 0.12)),
            image_command_prefix=str(raw.get("image_command_prefix", "鐢熷浘")),
            image_command_aliases=_load_str_list(raw.get("image_command_aliases"), ["鐢熷浘", "draw"]),
            ignore_prefixes=_load_str_list(raw.get("ignore_prefixes"), ["!", "/"]),
            group_follow_up_window_seconds=float(raw.get("group_follow_up_window_seconds", 5.0)),
            group_response_delay_seconds=float(raw.get("group_response_delay_seconds", 0.0)),
            repeat_trigger_count=int(raw.get("repeat_trigger_count", 0)),
            multimodal_enabled=bool(raw.get("multimodal_enabled", True)),
            history_window_messages=int(raw.get("history_window_messages", 8)),
            memory_recall_limit=int(raw.get("memory_recall_limit", 3)),
            max_thinking_words=int(raw.get("max_thinking_words", 30)),
            short_term_memory_limit=int(raw.get("short_term_memory_limit", 30)),
            memory_summary_batch_size=int(raw.get("memory_summary_batch_size", 12)),
            data_root=data_root,
            llm=llm,
            image_generation=image_generation,
            embedding=embedding,
            marketplace=marketplace,
            qq_sidecar=qq_sidecar,
        )
        config = _apply_env_overrides(config, config_path=self.path)
        config = _apply_napcat_autodiscovery(config, config_path=self.path)
        return _normalize_webui_fields(config)

    def dump_default(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        config = AppConfig(config_path=str(target.resolve()))
        self.save(config, target)

    def save(self, config: AppConfig, path: str | Path | None = None) -> None:
        target = Path(path) if path else Path(config.config_path)
        if not str(target):
            raise ValueError("config path is required")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(serialize_app_config(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sync_napcat_sidecar_files(config, config_path=target)


def _load_str_list(raw_value: object, default: list[str]) -> list[str]:
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if isinstance(raw_value, str):
        return [item.strip() for item in raw_value.split(",") if item.strip()]
    return list(default)


def _load_marketplace(raw_value: object) -> MarketplaceConfig:
    if not isinstance(raw_value, dict):
        return MarketplaceConfig()
    raw_sources = raw_value.get("sources", [])
    sources: list[MarketplaceSourceConfig] = []
    if isinstance(raw_sources, list):
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            sources.append(MarketplaceSourceConfig(**item))
    return MarketplaceConfig(
        enabled=bool(raw_value.get("enabled", True)),
        default_query=str(raw_value.get("default_query", "codex")),
        sources=sources or MarketplaceConfig().sources,
    )


def serialize_app_config(config: AppConfig) -> dict[str, object]:
    payload = asdict(config)
    payload.pop("config_path", None)
    return payload


def _apply_env_overrides(config: AppConfig, config_path: Path | None = None) -> AppConfig:
    data_root = _env_str("OPENQQWAIFU_DATA_ROOT")
    if data_root:
        root = Path(data_root)
        if not root.is_absolute() and config_path is not None:
            root = (config_path.parent / root).resolve()
        config.data_root = str(root)

    qq_sidecar = config.qq_sidecar
    qq_sidecar.outbound_base_url = _env_str(
        "OPENQQWAIFU_QQ_SIDECAR_OUTBOUND_BASE_URL",
        qq_sidecar.outbound_base_url,
    )
    qq_sidecar.access_token = _env_str(
        "OPENQQWAIFU_QQ_SIDECAR_ACCESS_TOKEN",
        qq_sidecar.access_token,
    )
    qq_sidecar.webui_base_url = _env_str(
        "OPENQQWAIFU_QQ_SIDECAR_WEBUI_BASE_URL",
        qq_sidecar.webui_base_url,
    )
    qq_sidecar.webui_api_prefix = _env_str(
        "OPENQQWAIFU_QQ_SIDECAR_WEBUI_API_PREFIX",
        qq_sidecar.webui_api_prefix,
    )
    qq_sidecar.webui_timeout_seconds = _env_float(
        "OPENQQWAIFU_QQ_SIDECAR_WEBUI_TIMEOUT_SECONDS",
        qq_sidecar.webui_timeout_seconds,
    )
    qq_sidecar.webui_token = _env_str(
        "OPENQQWAIFU_QQ_SIDECAR_WEBUI_TOKEN",
        qq_sidecar.webui_token,
    )
    qq_sidecar.reverse_ws_url = _env_str(
        "OPENQQWAIFU_QQ_SIDECAR_REVERSE_WS_URL",
        qq_sidecar.reverse_ws_url,
    )
    qq_sidecar.dry_run = _env_bool(
        "OPENQQWAIFU_QQ_SIDECAR_DRY_RUN",
        qq_sidecar.dry_run,
    )
    return config


def _apply_napcat_autodiscovery(config: AppConfig, config_path: Path | None = None) -> AppConfig:
    napcat_config_dirs = _napcat_config_dirs(config_path, data_root=config.data_root)
    discovered = _discover_napcat_webui_config(napcat_config_dirs)
    qq_sidecar = config.qq_sidecar
    if discovered:
        if not str(qq_sidecar.webui_token or "").strip():
            qq_sidecar.webui_token = discovered.get("token", "")
        if not str(qq_sidecar.webui_base_url or "").strip():
            qq_sidecar.webui_base_url = discovered.get("base_url", qq_sidecar.webui_base_url)

    _provision_napcat_onebot_network(config, napcat_config_dirs)
    return config


def sync_napcat_sidecar_files(config: AppConfig, *, config_path: str | Path | None = None) -> None:
    resolved_path: Path | None = None
    if config_path is not None:
        resolved_path = Path(config_path)
    elif str(config.config_path or "").strip():
        resolved_path = Path(config.config_path)
    napcat_config_dirs = _napcat_config_dirs(resolved_path, data_root=config.data_root)
    _provision_napcat_onebot_network(config, napcat_config_dirs)


def _normalize_webui_fields(config: AppConfig) -> AppConfig:
    qq_sidecar = config.qq_sidecar
    normalized_base, normalized_token = _normalize_webui_settings(
        qq_sidecar.webui_base_url,
        qq_sidecar.webui_token,
    )
    qq_sidecar.webui_base_url = normalized_base
    qq_sidecar.webui_token = normalized_token
    return config


def _napcat_config_dirs(config_path: Path | None, *, data_root: str) -> list[Path]:
    candidates: list[Path] = []
    if config_path is not None:
        repo_root = config_path.parent.parent
        candidates.append(repo_root / "napcat" / "config")
    candidates.append(Path.cwd() / "napcat" / "config")
    candidates.append(Path("/app/napcat-sidecar/config"))
    try:
        data_root_parent = Path(data_root).resolve().parent
        candidates.append(data_root_parent / "napcat-sidecar" / "config")
        candidates.append(data_root_parent / "napcat" / "config")
    except Exception:
        pass

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _discover_napcat_webui_config(config_dirs: list[Path]) -> dict[str, str]:
    for directory in config_dirs:
        path = directory / "webui.json"
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        token = str(raw.get("token") or "").strip()
        host = str(raw.get("host") or "").strip()
        port = int(raw.get("port", 6099) or 6099)
        if host in {"", "::", "0.0.0.0"}:
            host = "127.0.0.1"
        if not token and not host:
            continue
        return {
            "token": token,
            "base_url": f"http://{host}:{port}",
        }
    return {}


def _provision_napcat_onebot_network(config: AppConfig, config_dirs: list[Path]) -> None:
    """Ensure the NapCat OneBot config targets a single managed openqqwaifu runtime."""

    onebot_path = _find_napcat_onebot_config(config_dirs)
    if onebot_path is None:
        return
    try:
        raw = json.loads(onebot_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return

    network = raw.get("network")
    if not isinstance(network, dict):
        network = {}
        raw["network"] = network
    http_clients = _ensure_list(network, "httpClients")
    http_servers = _ensure_list(network, "httpServers")

    qq_sidecar = config.qq_sidecar
    inbound_port = int(qq_sidecar.inbound_port or 8080)
    webhook_url = _desired_napcat_webhook_url(qq_sidecar, inbound_port=inbound_port)

    changed = False
    http_clients, clients_changed = _ensure_managed_http_client(
        http_clients,
        webhook_url,
        qq_sidecar.access_token,
    )
    if clients_changed:
        network["httpClients"] = http_clients
        changed = True

    existing_server = _first_enabled_http_server(http_servers)
    managed_server = _find_named_http_server(http_servers, "openqqwaifu-actions")
    http_servers, managed_server, servers_changed = _ensure_managed_http_server(
        http_servers,
        qq_sidecar.access_token,
    )
    if servers_changed:
        network["httpServers"] = http_servers
        changed = True
    if managed_server is not None:
        existing_server = managed_server
    elif existing_server is None:
        default_port = _unused_local_port(http_servers, preferred=3000)
        existing_server = _default_http_server_entry(default_port, qq_sidecar.access_token)
        http_servers.append(existing_server)
        network["httpServers"] = http_servers
        changed = True

    server_host = str(existing_server.get("host") or "").strip()
    server_port = int(existing_server.get("port") or 3000)
    if server_host in {"", "0.0.0.0", "::"}:
        server_host = _desired_napcat_action_host(qq_sidecar)
    server_token = str(existing_server.get("token") or "").strip()
    outbound_url = f"http://{server_host}:{server_port}"

    if not str(qq_sidecar.outbound_base_url or "").strip():
        qq_sidecar.outbound_base_url = outbound_url
    if not str(qq_sidecar.access_token or "").strip() and server_token:
        qq_sidecar.access_token = server_token
    if qq_sidecar.outbound_base_url and qq_sidecar.dry_run:
        qq_sidecar.dry_run = False

    if changed:
        try:
            _write_json_file_atomic(onebot_path, raw)
        except Exception:
            pass


def _find_napcat_onebot_config(config_dirs: list[Path]) -> Path | None:
    for directory in config_dirs:
        if not directory.exists():
            continue
        try:
            matches = sorted(directory.glob("onebot11_*.json"))
        except Exception:
            matches = []
        for candidate in matches:
            if candidate.is_file():
                return candidate
    return None


def _ensure_list(container: dict, key: str) -> list:
    value = container.get(key)
    if not isinstance(value, list):
        value = []
        container[key] = value
    return value


def _first_enabled_http_server(entries: list) -> dict | None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("enable") is False:
            continue
        return entry
    return None


def _find_named_http_server(entries: list, name: str) -> dict | None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "").strip() == name:
            return entry
    return None


def _unused_local_port(entries: list, *, preferred: int) -> int:
    used: set[int] = set()
    for entry in entries:
        if isinstance(entry, dict):
            try:
                used.add(int(entry.get("port") or 0))
            except (TypeError, ValueError):
                continue
    port = preferred
    while port in used:
        port += 1
    return port


def _default_http_client_entry(webhook_url: str, token: str) -> dict:
    return {
        "name": "openqqwaifu-webhook",
        "enable": True,
        "url": webhook_url,
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "token": str(token or ""),
        "debug": False,
    }


def _default_http_server_entry(port: int, token: str) -> dict:
    return {
        "name": "openqqwaifu-actions",
        "enable": True,
        "host": "0.0.0.0",
        "port": int(port),
        "messagePostFormat": "array",
        "enableCors": False,
        "enableWebsocket": False,
        "token": str(token or ""),
        "debug": False,
    }


def _normalize_managed_http_server(entry: dict, token: str) -> bool:
    changed = False
    desired = _default_http_server_entry(
        int(entry.get("port") or 3000),
        str(entry.get("token") or token or ""),
    )
    for key, value in desired.items():
        if entry.get(key) != value:
            entry[key] = value
            changed = True
    return changed


def _ensure_managed_http_client(entries: list, webhook_url: str, token: str) -> tuple[list, bool]:
    desired = _default_http_client_entry(webhook_url, token)
    managed_entry: dict | None = None
    next_entries: list = []
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            changed = True
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").rstrip("/")
        if name == "openqqwaifu-webhook" or _looks_like_openqqwaifu_webhook(url):
            if managed_entry is None:
                managed_entry = dict(entry)
            else:
                changed = True
            continue
        next_entries.append(entry)
    if managed_entry is None:
        managed_entry = desired
        changed = True
    else:
        for key, value in desired.items():
            if managed_entry.get(key) != value:
                managed_entry[key] = value
                changed = True
    next_entries.append(managed_entry)
    return next_entries, changed


def _ensure_managed_http_server(entries: list, token: str) -> tuple[list, dict | None, bool]:
    next_entries: list = []
    managed_entry: dict | None = None
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            changed = True
            continue
        name = str(entry.get("name") or "").strip()
        if name == "openqqwaifu-actions":
            if managed_entry is None:
                managed_entry = dict(entry)
            else:
                changed = True
            continue
        next_entries.append(entry)
    if managed_entry is None:
        return next_entries, None, changed
    if _normalize_managed_http_server(managed_entry, token):
        changed = True
    next_entries.append(managed_entry)
    return next_entries, managed_entry, changed


def _desired_napcat_webhook_url(qq_sidecar: QQSidecarConfig, *, inbound_port: int) -> str:
    callback_base_url = _env_str("OPENQQWAIFU_QQ_SIDECAR_CALLBACK_BASE_URL")
    if not callback_base_url:
        callback_host = _desired_napcat_callback_host(qq_sidecar)
        callback_base_url = f"http://{callback_host}:{inbound_port}"
    return f"{callback_base_url.rstrip('/')}/onebot/events"


def _desired_napcat_callback_host(qq_sidecar: QQSidecarConfig) -> str:
    inbound_host = str(qq_sidecar.inbound_host or "").strip()
    if inbound_host not in {"", "0.0.0.0", "::"}:
        return inbound_host
    if _should_use_container_sidecar_network(qq_sidecar):
        return _env_str("OPENQQWAIFU_CONTAINER_HOSTNAME", "openqqwaifu")
    return "127.0.0.1"


def _desired_napcat_action_host(qq_sidecar: QQSidecarConfig) -> str:
    parsed = urlsplit(str(qq_sidecar.outbound_base_url or "").strip())
    outbound_host = str(parsed.hostname or "").strip()
    if outbound_host and outbound_host not in {"", "0.0.0.0", "::"}:
        return outbound_host
    if _should_use_container_sidecar_network(qq_sidecar):
        return "napcat"
    return "127.0.0.1"


def _should_use_container_sidecar_network(qq_sidecar: QQSidecarConfig) -> bool:
    if not _is_running_in_container():
        return False
    return any(
        _host_looks_nonlocal(url)
        for url in (
            qq_sidecar.outbound_base_url,
            qq_sidecar.webui_base_url,
            qq_sidecar.reverse_ws_url,
        )
    )


def _is_running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def _host_looks_nonlocal(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False
    return host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"}


def _looks_like_openqqwaifu_webhook(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    path = str(parsed.path or "").rstrip("/")
    if path != "/onebot/events":
        return False
    host = str(parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal", "openqqwaifu"}


def _normalize_webui_settings(base_url: str, webui_token: str) -> tuple[str, str]:
    raw_base = str(base_url or "").strip()
    token = str(webui_token or "").strip()
    if not raw_base:
        return "", token
    parsed = urlsplit(raw_base)
    query = parse_qs(parsed.query)
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
    normalized = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return normalized.rstrip("/"), token


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _write_json_file_atomic(path: Path, payload: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)
