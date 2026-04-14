from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..config import (
    AppConfig,
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
            return AppConfig()

        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        data_root = str(raw.get("data_root", "data"))
        if not Path(data_root).is_absolute():
            data_root = str((self.path.parent / data_root).resolve())

        qq_sidecar = QQSidecarConfig(**raw.get("qq_sidecar", {}))
        llm = LLMConfig(**raw.get("llm", {}))
        image_generation = ImageGenerationConfig(**raw.get("image_generation", {}))
        marketplace = _load_marketplace(raw.get("marketplace"))
        return AppConfig(
            service_name=str(raw.get("service_name", "waifu-standalone")),
            config_path=str(self.path.resolve()),
            assistant_name=str(raw.get("assistant_name", "琉璃")),
            character=str(raw.get("character", "default")),
            bot_account_id=str(raw.get("bot_account_id", "")),
            skills_enabled=bool(raw.get("skills_enabled", True)),
            max_active_skills=int(raw.get("max_active_skills", 3)),
            search_enabled=bool(raw.get("search_enabled", True)),
            search_result_limit=int(raw.get("search_result_limit", 3)),
            search_timeout_seconds=float(raw.get("search_timeout_seconds", 8.0)),
            thinking_mode=bool(raw.get("thinking_mode", True)),
            conversation_analysis=bool(raw.get("conversation_analysis", True)),
            summarization_mode=bool(raw.get("summarization_mode", False)),
            image_command_prefix=str(raw.get("image_command_prefix", "生图")),
            image_command_aliases=_load_str_list(raw.get("image_command_aliases"), ["生图", "draw"]),
            ignore_prefixes=_load_str_list(raw.get("ignore_prefixes"), ["!", "！", "/"]),
            group_follow_up_window_seconds=float(raw.get("group_follow_up_window_seconds", 5.0)),
            history_window_messages=int(raw.get("history_window_messages", 8)),
            memory_recall_limit=int(raw.get("memory_recall_limit", 3)),
            max_thinking_words=int(raw.get("max_thinking_words", 30)),
            short_term_memory_limit=int(raw.get("short_term_memory_limit", 30)),
            memory_summary_batch_size=int(raw.get("memory_summary_batch_size", 12)),
            data_root=data_root,
            llm=llm,
            image_generation=image_generation,
            marketplace=marketplace,
            qq_sidecar=qq_sidecar,
        )

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
