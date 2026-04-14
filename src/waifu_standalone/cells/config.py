from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..config import AppConfig, QQSidecarConfig


class ConfigManager:
    """Small config loader inspired by the upstream Waifu layout."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None

    def load(self) -> AppConfig:
        if self.path is None or not self.path.exists():
            return AppConfig()

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        data_root = str(raw.get("data_root", "data"))
        if not Path(data_root).is_absolute():
            data_root = str((self.path.parent / data_root).resolve())

        qq_sidecar = QQSidecarConfig(**raw.get("qq_sidecar", {}))
        return AppConfig(
            service_name=str(raw.get("service_name", "waifu-standalone")),
            image_command_prefix=str(raw.get("image_command_prefix", "draw")),
            data_root=data_root,
            qq_sidecar=qq_sidecar,
        )

    def dump_default(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(AppConfig()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
