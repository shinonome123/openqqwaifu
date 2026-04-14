from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class QQSidecarConfig:
    mode: str = "onebot-http"
    adapter_name: str = "napcat"
    inbound_host: str = "127.0.0.1"
    inbound_port: int = 8080
    outbound_base_url: str = "http://127.0.0.1:3000"
    outbound_timeout_seconds: float = 10.0
    access_token: str = ""
    reverse_ws_url: str = "ws://127.0.0.1:3001/onebot/v11/ws"
    dry_run: bool = True


@dataclass(slots=True)
class AppConfig:
    service_name: str = "waifu-standalone"
    image_command_prefix: str = "draw"
    data_root: str = "data"
    qq_sidecar: QQSidecarConfig = field(default_factory=QQSidecarConfig)
