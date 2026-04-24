from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from ..memory import FileMemoryStore

def _strip_inline_comment(line: str) -> str:
    in_single = False
    in_double = False
    result: list[str] = []
    for char in line:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result).rstrip()


def parse_simple_yaml(path: str | Path) -> dict[str, object]:
    data: dict[str, object] = {}
    file_path = Path(path)
    if not file_path.exists():
        return data
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = _strip_inline_comment(raw_line).strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        data[key] = _parse_scalar(value)
    return data


def _parse_scalar(value: str) -> object:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if value.startswith(("'", '"', "[", "{")):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value.strip("'\"")
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("'\"")


@dataclass(slots=True)
class ImportResult:
    launcher_id: str
    launcher_type: str
    imported: bool
    session_path: str


class WaifuDataImporter:
    def __init__(self, store: FileMemoryStore, waifu_root: str | Path, default_launcher_type: str = "group"):
        self.store = store
        self.waifu_root = Path(waifu_root)
        self.default_launcher_type = default_launcher_type

    def scan_launcher_ids(self) -> list[str]:
        ids: set[str] = set()
        config_dir = self.waifu_root / "data" / "config"
        for path in config_dir.glob("waifu_*.yaml"):
            ids.add(path.stem.removeprefix("waifu_"))
        data_dir = self.waifu_root / "data"
        for prefix in ("short_term_memory_", "memories_", "conversations_"):
            for path in data_dir.glob(f"{prefix}*"):
                ids.add(path.stem.removeprefix(prefix))
        return sorted(ids)

    def import_all(self, launcher_type: str | None = None) -> list[ImportResult]:
        return [self.import_launcher(launcher_id, launcher_type) for launcher_id in self.scan_launcher_ids()]

    def import_launcher(self, launcher_id: str, launcher_type: str | None = None) -> ImportResult:
        resolved_type = launcher_type or self.default_launcher_type
        if resolved_type not in {"group", "person"}:
            raise ValueError("launcher_type must be 'group' or 'person'")
        config = self._load_config(launcher_id)
        configured_character = str(config.get("character", "") or "").strip()
        session = self.store.load(launcher_id, resolved_type, character_id=configured_character)
        session.metadata["imported_from"] = "waifu"
        session.metadata["waifu_root"] = str(self.waifu_root)
        if config:
            session.metadata["waifu_config"] = config

        short_term = self._load_short_term_memory(launcher_id)
        if short_term:
            session.history = short_term[-20:]
            session.metadata["history_source"] = "short_term_memory"
        else:
            conversations = self._load_conversations(launcher_id)
            if conversations:
                session.history = conversations[-20:]
                session.metadata["history_source"] = "conversations"

        long_term = self._load_long_term_memory(launcher_id)
        if long_term:
            session.metadata["long_term_memory"] = long_term

        card_identity = self._load_card_identity(config, resolved_type)
        if card_identity:
            session.character_id = str(card_identity.get("character", "") or session.character_id or "").strip()
            session.metadata["card"] = card_identity
            if isinstance(card_identity.get("assistant_name"), str):
                session.metadata["assistant_name"] = str(card_identity["assistant_name"])
            if isinstance(card_identity.get("user_name"), str):
                session.metadata["user_name"] = str(card_identity["user_name"])
        elif config.get("character"):
            session.character_id = str(config.get("character") or "").strip()

        saved = self.store.save(session)
        session_path = str(
            self.store.session_path(
                saved.launcher_id,
                saved.launcher_type,
                character_id=saved.character_id,
            )
        )
        return ImportResult(
            launcher_id=saved.launcher_id,
            launcher_type=saved.launcher_type,
            imported=True,
            session_path=session_path,
        )

    def _load_config(self, launcher_id: str) -> dict[str, object]:
        config_dir = self.waifu_root / "data" / "config"
        base = parse_simple_yaml(config_dir / "waifu.yaml")
        override = parse_simple_yaml(config_dir / f"waifu_{launcher_id}.yaml")
        merged = dict(base)
        merged.update(override)
        return merged

    def _load_short_term_memory(self, launcher_id: str) -> list[str]:
        path = self.waifu_root / "data" / f"short_term_memory_{launcher_id}.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "unknown"))
            content = str(item.get("content", "")).strip()
            history.append(f"{role}: {content}")
        return history

    def _load_long_term_memory(self, launcher_id: str) -> list[dict[str, object]]:
        path = self.waifu_root / "data" / f"memories_{launcher_id}.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        raw_long_term = payload.get("long_term", [])
        if not isinstance(raw_long_term, list):
            return []
        return [dict(item) for item in raw_long_term if isinstance(item, dict)]

    def _load_conversations(self, launcher_id: str) -> list[str]:
        path = self.waifu_root / "data" / f"conversations_{launcher_id}.log"
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _load_card_identity(self, config: dict[str, object], launcher_type: str) -> dict[str, object]:
        character = str(config.get("character", "default") or "default").strip() or "default"
        if character == "off":
            return {}
        for path in self._candidate_card_paths(character, launcher_type):
            if not path.exists():
                continue
            payload = parse_simple_yaml(path)
            payload["character"] = character
            payload["source"] = str(path)
            return payload
        return {"character": character}

    def _candidate_card_paths(self, character: str, launcher_type: str) -> list[Path]:
        return [
            self.waifu_root / "cards" / f"{character}_{launcher_type}.yaml",
            self.waifu_root / "cards" / f"{character}.yaml",
            self.waifu_root / "data" / "cards" / f"{character}_{launcher_type}.yaml",
            self.waifu_root / "data" / "cards" / f"{character}.yaml",
        ]
