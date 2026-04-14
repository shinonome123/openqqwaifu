from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import AppConfig
from ..models import EmotionState, SessionMemory


_DEFAULT_ASSISTANT_NAME = "琉璃"
_DEFAULT_USER_NAME = "用户"
_DEFAULT_LANGUAGE = "简体中文"
_DEFAULT_PORTRAIT_STYLE = "neon-pixel"
_SECTION_KEYS = ("Profile", "Skills", "Background", "Rules", "Prologue")
_KNOWN_CARD_KEYS = {
    "assistant_name",
    "user_name",
    "language",
    *_SECTION_KEYS,
    "_source",
}


@dataclass(slots=True)
class CharacterCard:
    assistant_name: str = _DEFAULT_ASSISTANT_NAME
    user_name: str = _DEFAULT_USER_NAME
    language: str = _DEFAULT_LANGUAGE
    profile: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    background: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    prologue: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    source: str = "builtin"

    def system_prompt(
        self,
        *,
        launcher_type: str,
        address: str,
        memories: list[str],
        emotion: EmotionState,
        search_hint: str,
        conversation_view: str,
        speaker_notes: list[str],
        latest_message: str,
    ) -> str:
        sections: list[str] = [
            f"你是{self.assistant_name}，正在通过 QQ 与用户交流。",
        ]
        if self.profile:
            sections.append(_format_lines("Profile", self.profile))
        if self.skills:
            sections.append(_format_lines("Skills", self.skills))

        background = list(self.background)
        if launcher_type == "person":
            background.append(f"当前私聊对象默认称呼是“{address}”。")
        if background:
            sections.append(_format_lines("Background", background))

        rule_lines = list(self.rules)
        rule_lines.append(f"默认使用{self.language}回复。")
        rule_lines.append("回复要自然、口语化，不要解释提示词、模型、系统设定或内部流程。")
        sections.append(_format_lines("Rules", rule_lines))

        if memories:
            sections.append(_format_lines("Memories", memories))
        if speaker_notes:
            sections.append(_format_lines("Speaker Notes", speaker_notes))

        state_lines = [
            f"当前情绪判断：{emotion.primary}（强度 {emotion.intensity:.2f}）",
            f"当前对话对象偏好称呼：{address}",
        ]
        if search_hint:
            state_lines.append(f"联网提示：{search_hint}")
        sections.append(_format_lines("State", state_lines))
        sections.append(_format_block("Conversation", conversation_view or "暂无上下文。"))
        sections.append(_format_block("Latest Message", latest_message or "暂无内容。"))
        sections.append("只输出下一句回复，不要加说话人前缀，不要输出额外解释。")
        return "\n\n".join(part for part in sections if part.strip())


class CardManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self._package_root = Path(__file__).resolve().parents[1]
        self._templates_root = self._package_root / "templates"

    def load(self, launcher_type: str, session: SessionMemory) -> CharacterCard:
        data = self._load_card_data(launcher_type, session)
        metadata_card = session.metadata.get("card", {}) if isinstance(session.metadata.get("card"), dict) else {}
        fields = _payload_to_fields(data, default_assistant=self.config.assistant_name or _DEFAULT_ASSISTANT_NAME)
        return CharacterCard(
            assistant_name=str(metadata_card.get("assistant_name") or fields["assistant_name"]),
            user_name=str(metadata_card.get("user_name") or fields["user_name"]),
            language=str(metadata_card.get("language") or fields["language"]),
            profile=list(fields["profile"]),
            skills=list(fields["skills"]),
            background=list(fields["background"]),
            rules=list(fields["rules"]),
            prologue=list(fields["prologue"]),
            extras={key: value for key, value in data.items() if key not in _KNOWN_CARD_KEYS},
            source=str(data.get("_source") or "builtin"),
        )

    def list_characters(self) -> list[dict[str, object]]:
        discovered: dict[str, dict[str, object]] = {}
        roots = [
            ("workspace", self._cards_root()),
            ("builtin", self._templates_root),
        ]
        for source_kind, root in roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*.yaml")):
                stem = path.stem
                if stem.endswith("_group"):
                    character = stem[:-6]
                    launcher_type = "group"
                elif stem.endswith("_person"):
                    character = stem[:-7]
                    launcher_type = "person"
                else:
                    character = stem
                    launcher_type = "both"
                if not character:
                    continue
                entry = discovered.setdefault(
                    character,
                    {
                        "character": character,
                        "has_person": False,
                        "has_group": False,
                        "sources": [],
                    },
                )
                if launcher_type in {"person", "both"}:
                    entry["has_person"] = True
                if launcher_type in {"group", "both"}:
                    entry["has_group"] = True
                entry["sources"].append({"kind": source_kind, "path": str(path)})

        items: list[dict[str, object]] = []
        for character, entry in discovered.items():
            meta = self._load_character_meta(character)
            person_variant = self._editor_variant(character, "person")
            group_variant = self._editor_variant(character, "group")
            shared = self._derive_shared_fields(person_variant["fields"], group_variant["fields"])
            portrait = self._portrait_payload(character, meta)
            summary = self._derive_summary(person_variant["fields"], group_variant["fields"])
            items.append(
                {
                    **entry,
                    "assistant_name": shared["assistant_name"],
                    "user_name": shared["user_name"],
                    "language": shared["language"],
                    "summary": summary,
                    "portrait": portrait,
                }
            )
        items.sort(key=lambda item: str(item["character"]))
        return items

    def get_editor_bundle(self, character: str) -> dict[str, object]:
        resolved = self._normalize_character_name(character or self.config.character or "default")
        person_variant = self._editor_variant(resolved, "person")
        group_variant = self._editor_variant(resolved, "group")
        meta = self._load_character_meta(resolved)
        return {
            "character": resolved,
            "shared": self._derive_shared_fields(person_variant["fields"], group_variant["fields"]),
            "person": person_variant,
            "group": group_variant,
            "portrait": self._portrait_payload(resolved, meta),
            "available": self.list_characters(),
        }

    def save_editor_bundle(
        self,
        character: str,
        person_content: str = "",
        group_content: str = "",
        *,
        person_fields: dict[str, Any] | None = None,
        group_fields: dict[str, Any] | None = None,
        shared_fields: dict[str, Any] | None = None,
        portrait: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        resolved = self._normalize_character_name(character)
        current_person = self._editor_variant(resolved, "person")["fields"]
        current_group = self._editor_variant(resolved, "group")["fields"]
        shared = self._normalize_shared_fields(shared_fields, current_person, current_group)

        next_person = self._merge_variant_fields(
            base=current_person,
            incoming=person_fields,
            raw_content=person_content,
            shared=shared,
        )
        next_group = self._merge_variant_fields(
            base=current_group,
            incoming=group_fields,
            raw_content=group_content,
            shared=shared,
        )

        self._save_variant(resolved, "person", _render_card_yaml(next_person))
        self._save_variant(resolved, "group", _render_card_yaml(next_group))

        if isinstance(portrait, dict):
            meta = self._load_character_meta(resolved)
            meta["portrait_style"] = str(portrait.get("style") or meta.get("portrait_style") or _DEFAULT_PORTRAIT_STYLE)
            meta["portrait_prompt_suffix"] = str(portrait.get("prompt_suffix") or meta.get("portrait_prompt_suffix") or "")
            meta["portrait_auto_generate"] = bool(portrait.get("auto_generate", meta.get("portrait_auto_generate", True)))
            self._save_character_meta(resolved, meta)

        return self.get_editor_bundle(resolved)

    def save_portrait_asset(
        self,
        character: str,
        image_bytes: bytes,
        content_type: str,
        *,
        prompt: str,
        style: str,
        prompt_suffix: str = "",
        auto_generate: bool = True,
    ) -> dict[str, object]:
        resolved = self._normalize_character_name(character)
        extension = _image_extension_for_content_type(content_type)
        portraits_root = self._portraits_root()
        portraits_root.mkdir(parents=True, exist_ok=True)
        filename = f"{resolved}{extension}"
        path = portraits_root / filename
        path.write_bytes(image_bytes)

        meta = self._load_character_meta(resolved)
        meta.update(
            {
                "portrait_path": str(path.relative_to(self._cards_root()).as_posix()),
                "portrait_content_type": content_type,
                "portrait_prompt": prompt,
                "portrait_style": style or _DEFAULT_PORTRAIT_STYLE,
                "portrait_prompt_suffix": prompt_suffix,
                "portrait_auto_generate": bool(auto_generate),
                "portrait_updated_at": time.time(),
            }
        )
        self._save_character_meta(resolved, meta)
        return self._portrait_payload(resolved, meta)

    def load_portrait_asset(self, character: str) -> tuple[bytes, str] | None:
        resolved = self._normalize_character_name(character)
        meta = self._load_character_meta(resolved)
        relative = str(meta.get("portrait_path") or "").strip()
        if not relative:
            return None
        path = (self._cards_root() / relative).resolve()
        try:
            path.relative_to(self._cards_root().resolve())
        except ValueError:
            return None
        if not path.exists() or not path.is_file():
            return None
        content_type = str(meta.get("portrait_content_type") or _guess_content_type(path))
        return path.read_bytes(), content_type

    def build_portrait_prompt(
        self,
        character: str,
        *,
        shared_fields: dict[str, Any],
        person_fields: dict[str, Any],
        group_fields: dict[str, Any],
        portrait: dict[str, Any] | None = None,
    ) -> str:
        style = str((portrait or {}).get("style") or _DEFAULT_PORTRAIT_STYLE)
        prompt_suffix = str((portrait or {}).get("prompt_suffix") or "").strip()
        assistant_name = str(shared_fields.get("assistant_name") or character or _DEFAULT_ASSISTANT_NAME)
        traits = _collect_portrait_traits(person_fields, group_fields)
        style_prompt = _portrait_style_prompt(style)
        lines = [
            f"Create a single-character portrait for an AI companion named {assistant_name}.",
            f"Character codename: {character}.",
            style_prompt,
            "Show the character in a striking standing pose, centered composition, polished lighting, and a clean silhouette.",
            "Make the design suitable for a control-panel card: vivid, iconic, and immediately recognizable.",
            "One character only. No text, no watermark, no UI frame, no duplicate limbs.",
        ]
        if traits:
            lines.append("Character traits:")
            lines.extend(f"- {item}" for item in traits[:8])
        if prompt_suffix:
            lines.append(f"Extra direction: {prompt_suffix}")
        return "\n".join(lines)

    def portrait_url(self, character: str, updated_at: float = 0.0) -> str:
        resolved = self._normalize_character_name(character)
        url = f"/api/portraits?character={quote(resolved)}"
        stamp = int(updated_at or 0)
        if stamp:
            url += f"&v={stamp}"
        return url

    def _cards_root(self) -> Path:
        return Path(self.config.data_root) / "cards"

    def _portraits_root(self) -> Path:
        return self._cards_root() / "portraits"

    def _meta_path(self, character: str) -> Path:
        return self._cards_root() / f"{character}.meta.json"

    def _editor_variant(self, character: str, launcher_type: str) -> dict[str, Any]:
        target_path = self._cards_root() / f"{character}_{launcher_type}.yaml"
        source_path = target_path
        if source_path.exists():
            content = source_path.read_text(encoding="utf-8")
            data = parse_card_file(source_path)
        else:
            fallback = self._templates_root / f"{character}_{launcher_type}.yaml"
            if not fallback.exists():
                fallback = self._templates_root / f"default_{launcher_type}.yaml"
            source_path = fallback
            content = fallback.read_text(encoding="utf-8") if fallback.exists() else ""
            data = parse_card_file(fallback) if fallback.exists() else {}
        return {
            "path": str(target_path),
            "source_path": str(source_path),
            "content": content,
            "fields": _payload_to_fields(data, default_assistant=self.config.assistant_name or _DEFAULT_ASSISTANT_NAME),
        }

    def _load_card_data(self, launcher_type: str, session: SessionMemory) -> dict[str, Any]:
        character = self._normalize_character_name(self.config.character or "default")
        metadata_card = session.metadata.get("card", {})
        if isinstance(metadata_card, dict):
            source = metadata_card.get("source")
            if isinstance(source, str) and source.strip():
                source_path = Path(source)
                if source_path.exists():
                    data = parse_card_file(source_path)
                    data["_source"] = str(source_path)
                    return data

        for candidate in self._candidate_paths(character, launcher_type, session):
            if candidate.exists():
                data = parse_card_file(candidate)
                data["_source"] = str(candidate)
                return data

        fallback = self._templates_root / f"default_{launcher_type}.yaml"
        data = parse_card_file(fallback)
        data["_source"] = str(fallback)
        return data

    def _candidate_paths(self, character: str, launcher_type: str, session: SessionMemory) -> list[Path]:
        candidates = [
            self._cards_root() / f"{character}_{launcher_type}.yaml",
            self._cards_root() / f"{character}.yaml",
        ]
        waifu_root = self._waifu_root(session)
        if waifu_root is not None:
            candidates.extend(
                [
                    waifu_root / "cards" / f"{character}_{launcher_type}.yaml",
                    waifu_root / "cards" / f"{character}.yaml",
                    waifu_root / "data" / "cards" / f"{character}_{launcher_type}.yaml",
                    waifu_root / "data" / "cards" / f"{character}.yaml",
                ]
            )
        return candidates

    def _normalize_shared_fields(
        self,
        shared_fields: dict[str, Any] | None,
        person_fields: dict[str, Any],
        group_fields: dict[str, Any],
    ) -> dict[str, str]:
        base = self._derive_shared_fields(person_fields, group_fields)
        incoming = shared_fields if isinstance(shared_fields, dict) else {}
        assistant_name = str(incoming.get("assistant_name") or base["assistant_name"] or _DEFAULT_ASSISTANT_NAME).strip()
        user_name = str(incoming.get("user_name") or base["user_name"] or _DEFAULT_USER_NAME).strip()
        language = str(incoming.get("language") or base["language"] or _DEFAULT_LANGUAGE).strip()
        return {
            "assistant_name": assistant_name or _DEFAULT_ASSISTANT_NAME,
            "user_name": user_name or _DEFAULT_USER_NAME,
            "language": language or _DEFAULT_LANGUAGE,
        }

    def _merge_variant_fields(
        self,
        *,
        base: dict[str, Any],
        incoming: dict[str, Any] | None,
        raw_content: str,
        shared: dict[str, str],
    ) -> dict[str, Any]:
        if isinstance(incoming, dict):
            fields = _normalize_editor_fields(incoming, fallback=base)
        elif raw_content.strip():
            fields = _normalize_editor_fields(_payload_to_fields(parse_card_text(raw_content), default_assistant=shared["assistant_name"]), fallback=base)
        else:
            fields = _normalize_editor_fields(base, fallback=base)
        fields["assistant_name"] = shared["assistant_name"]
        fields["user_name"] = shared["user_name"]
        fields["language"] = shared["language"]
        return fields

    def _derive_shared_fields(self, person_fields: dict[str, Any], group_fields: dict[str, Any]) -> dict[str, str]:
        assistant_name = str(person_fields.get("assistant_name") or group_fields.get("assistant_name") or self.config.assistant_name or _DEFAULT_ASSISTANT_NAME)
        user_name = str(person_fields.get("user_name") or group_fields.get("user_name") or _DEFAULT_USER_NAME)
        language = str(person_fields.get("language") or group_fields.get("language") or _DEFAULT_LANGUAGE)
        return {
            "assistant_name": assistant_name or _DEFAULT_ASSISTANT_NAME,
            "user_name": user_name or _DEFAULT_USER_NAME,
            "language": language or _DEFAULT_LANGUAGE,
        }

    def _derive_summary(self, person_fields: dict[str, Any], group_fields: dict[str, Any]) -> str:
        for key in ("profile", "background", "rules", "skills"):
            lines = person_fields.get(key) or group_fields.get(key) or []
            if isinstance(lines, list) and lines:
                return str(lines[0])
        return ""

    def _load_character_meta(self, character: str) -> dict[str, Any]:
        path = self._meta_path(character)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_character_meta(self, character: str, payload: dict[str, Any]) -> None:
        path = self._meta_path(character)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _portrait_payload(self, character: str, meta: dict[str, Any]) -> dict[str, Any]:
        updated_at = float(meta.get("portrait_updated_at", 0.0) or 0.0)
        asset = self.load_portrait_asset(character)
        available = asset is not None
        return {
            "available": available,
            "url": self.portrait_url(character, updated_at) if available else "",
            "style": str(meta.get("portrait_style") or _DEFAULT_PORTRAIT_STYLE),
            "prompt_suffix": str(meta.get("portrait_prompt_suffix") or ""),
            "auto_generate": bool(meta.get("portrait_auto_generate", True)),
            "last_prompt": str(meta.get("portrait_prompt") or ""),
            "updated_at": updated_at,
        }

    @staticmethod
    def _waifu_root(session: SessionMemory) -> Path | None:
        waifu_root = session.metadata.get("waifu_root")
        if not isinstance(waifu_root, str) or not waifu_root.strip():
            return None
        return Path(waifu_root)

    @staticmethod
    def _normalize_character_name(value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("character name is required")
        if len(name) > 48:
            raise ValueError("character name is too long")
        if any(token in name for token in ("/", "\\", ":", "*", "?", "\"", "<", ">", "|")):
            raise ValueError("character name contains invalid path characters")
        return name

    def _save_variant(self, character: str, launcher_type: str, content: str) -> None:
        path = self._cards_root() / f"{character}_{launcher_type}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or "").rstrip() + "\n", encoding="utf-8")


def parse_card_file(path: str | Path) -> dict[str, Any]:
    return parse_card_text(Path(path).read_text(encoding="utf-8"))


def parse_card_text(content: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    current_key = ""
    for raw_line in str(content or "").splitlines():
        line = _strip_inline_comment(raw_line.rstrip())
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and ":" in stripped:
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            payload[key] = _parse_scalar(value) if value else []
            continue
        if current_key and stripped.startswith("- "):
            item = _parse_scalar(stripped[2:].strip())
            existing = payload.setdefault(current_key, [])
            if isinstance(existing, list):
                existing.append(item)
            continue
        if current_key and indent > 0:
            existing = payload.setdefault(current_key, [])
            if isinstance(existing, list):
                existing.append(_parse_scalar(stripped))
    return payload


def _payload_to_fields(payload: dict[str, Any], *, default_assistant: str) -> dict[str, Any]:
    return _normalize_editor_fields(
        {
            "assistant_name": payload.get("assistant_name", default_assistant or _DEFAULT_ASSISTANT_NAME),
            "user_name": payload.get("user_name", _DEFAULT_USER_NAME),
            "language": payload.get("language", _DEFAULT_LANGUAGE),
            "profile": _coerce_str_list(payload.get("Profile")),
            "skills": _coerce_str_list(payload.get("Skills")),
            "background": _coerce_str_list(payload.get("Background")),
            "rules": _coerce_str_list(payload.get("Rules")),
            "prologue": _coerce_str_list(payload.get("Prologue")),
        }
    )


def _normalize_editor_fields(values: dict[str, Any], *, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    base = fallback or {}
    return {
        "assistant_name": str(values.get("assistant_name") or base.get("assistant_name") or _DEFAULT_ASSISTANT_NAME).strip() or _DEFAULT_ASSISTANT_NAME,
        "user_name": str(values.get("user_name") or base.get("user_name") or _DEFAULT_USER_NAME).strip() or _DEFAULT_USER_NAME,
        "language": str(values.get("language") or base.get("language") or _DEFAULT_LANGUAGE).strip() or _DEFAULT_LANGUAGE,
        "profile": _normalize_line_values(values.get("profile"), base.get("profile")),
        "skills": _normalize_line_values(values.get("skills"), base.get("skills")),
        "background": _normalize_line_values(values.get("background"), base.get("background")),
        "rules": _normalize_line_values(values.get("rules"), base.get("rules")),
        "prologue": _normalize_line_values(values.get("prologue"), base.get("prologue")),
    }


def _normalize_line_values(value: Any, fallback: Any = None) -> list[str]:
    if isinstance(value, str):
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if lines:
            return lines
    if isinstance(value, list):
        lines = [str(item).strip() for item in value if str(item).strip()]
        if lines:
            return lines
    if isinstance(fallback, str):
        return [line.strip() for line in fallback.splitlines() if line.strip()]
    if isinstance(fallback, list):
        return [str(item).strip() for item in fallback if str(item).strip()]
    return []


def _render_card_yaml(fields: dict[str, Any]) -> str:
    normalized = _normalize_editor_fields(fields)
    lines = [
        f"user_name: {normalized['user_name']}",
        f"assistant_name: {normalized['assistant_name']}",
        f"language: {normalized['language']}",
    ]
    for section_key, field_key in (
        ("Profile", "profile"),
        ("Skills", "skills"),
        ("Background", "background"),
        ("Rules", "rules"),
        ("Prologue", "prologue"),
    ):
        lines.append(f"{section_key}:")
        values = normalized[field_key]
        if values:
            lines.extend(f"  - {item}" for item in values)
        else:
            lines.append("  - ")
    return "\n".join(lines).rstrip() + "\n"


def _format_lines(title: str, lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines if str(line).strip())
    return f"[{title}]\n{body}".strip()


def _format_block(title: str, content: str) -> str:
    return f"[{title}]\n{content}".strip()


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_scalar(value: str) -> str | int | float | bool | None:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if value.startswith(("'", "\"", "[", "{")):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value.strip("'\"")
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("'\"")


def _strip_inline_comment(line: str) -> str:
    in_single = False
    in_double = False
    result: list[str] = []
    for char in line:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == "\"" and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result).rstrip()


def _collect_portrait_traits(person_fields: dict[str, Any], group_fields: dict[str, Any]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for field_key in ("profile", "background", "skills", "rules"):
        for source in (person_fields, group_fields):
            for item in source.get(field_key, []) or []:
                cleaned = str(item).strip()
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                merged.append(cleaned)
    return merged


def _portrait_style_prompt(style: str) -> str:
    presets = {
        "neon-pixel": "Style: premium pixel art, neon cyber glow, anime facial design, clean edges, rich blue-pink palette.",
        "dream-anime": "Style: polished anime illustration, luminous gradients, soft rim light, elegant fashion details.",
        "comic-pop": "Style: bold pop-comic portrait, high contrast colors, stylish linework, glossy highlights.",
    }
    return presets.get(str(style or "").strip(), presets[_DEFAULT_PORTRAIT_STYLE])


def _image_extension_for_content_type(content_type: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mapping.get(str(content_type or "").split(";", 1)[0].strip().lower(), ".png")


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/png"
