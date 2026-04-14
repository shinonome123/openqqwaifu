from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from .memory import FileMemoryStore
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
        session = self.store.load(launcher_id, resolved_type)
        session.metadata["imported_from"] = "waifu"
        session.metadata["waifu_root"] = str(self.waifu_root)

        config = self._load_config(launcher_id)
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

        if isinstance(config.get("assistant_name"), str):
            session.preferred_name = str(config["assistant_name"])

        saved = self.store.save(session)
        session_path = str(self.store.session_path(saved.launcher_id, saved.launcher_type))
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
        payload = json.loads(path.read_text(encoding="utf-8"))
        history: list[str] = []
        for item in payload:
            role = str(item.get("role", "unknown"))
            content = str(item.get("content", "")).strip()
            history.append(f"{role}: {content}")
        return history

    def _load_long_term_memory(self, launcher_id: str) -> list[dict[str, object]]:
        path = self.waifu_root / "data" / f"memories_{launcher_id}.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return list(payload.get("long_term", []))

    def _load_conversations(self, launcher_id: str) -> list[str]:
        path = self.waifu_root / "data" / f"conversations_{launcher_id}.log"
        if not path.exists():
            return []
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
