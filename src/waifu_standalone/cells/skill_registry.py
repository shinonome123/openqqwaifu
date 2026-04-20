from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppConfig


@dataclass(slots=True)
class SkillSpec:
    skill_id: str
    name: str
    description: str
    triggers: list[str]
    mode: str
    priority: int
    content: str
    source: str
    source_kind: str = "workspace"
    enabled: bool = True
    user_invocable: bool = True
    disable_model_invocation: bool = False
    command_dispatch: str = ""
    command_tool: str = ""
    command_arg_mode: str = "raw"

    def matches(self, text: str) -> bool:
        if not self.enabled:
            return False
        normalized_text = str(text or "").strip()
        if not normalized_text or not self.triggers:
            return False
        lowered_text = normalized_text.lower()
        for trigger in self.triggers:
            lowered_trigger = str(trigger or "").strip().lower()
            if not lowered_trigger:
                continue
            if self.mode == "prefix":
                if lowered_text.startswith(lowered_trigger):
                    return True
            elif lowered_trigger in lowered_text:
                return True
        return False

    def extracts_args(self, text: str) -> str:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return ""
        lowered_text = normalized_text.lower()
        if self.mode == "prefix":
            for trigger in self.triggers:
                raw_trigger = str(trigger or "").strip()
                lowered_trigger = raw_trigger.lower()
                if lowered_trigger and lowered_text.startswith(lowered_trigger):
                    return normalized_text[len(raw_trigger) :].lstrip(" ：:,-")
        return normalized_text

    @property
    def dispatches_tool(self) -> bool:
        return self.enabled and self.command_dispatch == "tool" and bool(self.command_tool)

    @property
    def prompt_visible(self) -> bool:
        return self.enabled and not self.disable_model_invocation

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "mode": self.mode,
            "priority": self.priority,
            "source": self.source,
            "source_kind": self.source_kind,
            "enabled": self.enabled,
            "user_invocable": self.user_invocable,
            "disable_model_invocation": self.disable_model_invocation,
            "command_dispatch": self.command_dispatch,
            "command_tool": self.command_tool,
            "command_arg_mode": self.command_arg_mode,
            "content": self.content,
            "editable": self.source_kind == "workspace",
            "deletable": self.source_kind == "workspace",
        }


class SkillRegistry:
    def __init__(self, config: AppConfig):
        self.config = config
        self._package_root = Path(__file__).resolve().parents[1]
        self._builtin_root = self._package_root / "builtin_skills"
        self._workspace_root = Path(config.data_root) / "skills"
        self._state_path = Path(config.data_root) / "skill_state.json"
        self._last_reload_at = time.time()
        self._reload_count = 0

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def list_skills(self) -> list[SkillSpec]:
        state = self._load_state()
        skills: list[SkillSpec] = []
        seen_ids: set[str] = set()
        for path in self._candidate_paths():
            if not path.exists():
                continue
            for skill_file in sorted(path.glob("*.md")):
                skill = parse_skill_file(skill_file)
                if skill.skill_id in seen_ids:
                    continue
                seen_ids.add(skill.skill_id)
                runtime = state.get(skill.skill_id, {})
                skill.enabled = bool(runtime.get("enabled", True))
                skill.source_kind = "builtin" if path == self._builtin_root else "workspace"
                skills.append(skill)
        skills.sort(key=lambda item: (-item.priority, item.name))
        return skills

    def match(self, text: str) -> list[SkillSpec]:
        if not self.config.skills_enabled:
            return []
        matched = [skill for skill in self.list_skills() if skill.matches(text)]
        return matched[: max(0, self.config.max_active_skills)]

    def resolve_dispatch(self, text: str) -> tuple[SkillSpec, str] | None:
        for skill in self.match(text):
            if not skill.dispatches_tool:
                continue
            return skill, skill.extracts_args(text)
        return None

    def describe(self) -> dict[str, Any]:
        skills = self.list_skills()
        return {
            "enabled": self.config.skills_enabled,
            "count": len(skills),
            "reload_count": self._reload_count,
            "last_reload_at": self._last_reload_at,
            "items": [skill.as_dict() for skill in skills],
        }

    def reload(self) -> dict[str, Any]:
        self._touch_reload()
        return self.describe()

    def get_skill(self, skill_id: str) -> SkillSpec | None:
        target = str(skill_id or "").strip()
        if not target:
            return None
        for skill in self.list_skills():
            if skill.skill_id == target:
                return skill
        return None

    def find_by_name_or_id(self, value: str) -> SkillSpec | None:
        target = _normalize_lookup_key(value)
        if not target:
            return None
        for skill in self.list_skills():
            if _normalize_lookup_key(skill.skill_id) == target:
                return skill
            if _normalize_lookup_key(skill.name) == target:
                return skill
        return None

    def get_skill_markdown(self, skill_id: str) -> str | None:
        path = self._find_skill_file(skill_id)
        if path is None:
            return None
        return path.read_text(encoding="utf-8")

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillSpec | None:
        skill = self.get_skill(skill_id)
        if skill is None:
            return None
        state = self._load_state()
        state[skill.skill_id] = {"enabled": bool(enabled)}
        self._save_state(state)
        self._touch_reload()
        return self.get_skill(skill.skill_id)

    def enabled_ids(self) -> list[str]:
        return [skill.skill_id for skill in self.list_skills() if skill.enabled]

    def has_dispatch_tool(self, tool_name: str) -> bool:
        target = str(tool_name or "").strip().lower()
        if not target:
            return False
        return any(skill.command_tool == target and skill.command_dispatch == "tool" for skill in self.list_skills())

    def install_workspace_skill(self, markdown: str, filename: str | None = None) -> SkillSpec:
        raw = str(markdown or "").strip()
        if not raw:
            raise ValueError("skill markdown is empty")
        tentative_name = _normalize_workspace_filename(filename or "")
        skill = parse_skill_markdown(raw, source=str(self._workspace_root / tentative_name))
        resolved_name = _normalize_workspace_filename(filename or f"{skill.skill_id}.md")
        resolved_path = self._workspace_root / resolved_name
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(raw.rstrip() + "\n", encoding="utf-8")
        self._touch_reload()
        installed = parse_skill_file(resolved_path)
        state = self._load_state()
        if installed.skill_id not in state:
            state[installed.skill_id] = {"enabled": True}
            self._save_state(state)
        return self.get_skill(installed.skill_id) or installed

    def save_workspace_skill(self, skill_id: str, markdown: str) -> SkillSpec | None:
        target = str(skill_id or "").strip()
        if not target:
            return None
        current_path = self._find_skill_file(target, workspace_only=True)
        if current_path is None:
            return None
        raw = str(markdown or "").strip()
        if not raw:
            raise ValueError("skill markdown is empty")
        parsed = parse_skill_markdown(raw, source=str(current_path))
        next_name = _normalize_workspace_filename(f"{parsed.skill_id}.md")
        next_path = self._workspace_root / next_name
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        next_path.write_text(raw.rstrip() + "\n", encoding="utf-8")
        if next_path != current_path and current_path.exists():
            current_path.unlink()
        self._touch_reload()
        saved = parse_skill_file(next_path)
        state = self._load_state()
        if saved.skill_id != target:
            state.pop(target, None)
        if saved.skill_id not in state:
            state[saved.skill_id] = {"enabled": True}
        self._save_state(state)
        return self.get_skill(saved.skill_id) or saved

    def delete_workspace_skill(self, skill_id: str) -> bool:
        target = str(skill_id or "").strip()
        path = self._find_skill_file(target, workspace_only=True)
        if path is None or not path.exists():
            return False
        path.unlink()
        state = self._load_state()
        state.pop(target, None)
        self._save_state(state)
        self._touch_reload()
        return True

    def _candidate_paths(self) -> list[Path]:
        return [self._workspace_root, self._builtin_root]

    def _find_skill_file(self, skill_id: str, *, workspace_only: bool = False) -> Path | None:
        target = str(skill_id or "").strip()
        if not target:
            return None
        paths = [self._workspace_root] if workspace_only else self._candidate_paths()
        for path in paths:
            if not path.exists():
                continue
            for skill_file in sorted(path.glob("*.md")):
                try:
                    skill = parse_skill_file(skill_file)
                except (OSError, UnicodeDecodeError):
                    continue
                if skill.skill_id == target:
                    return skill_file
        return None

    def _touch_reload(self) -> None:
        self._reload_count += 1
        self._last_reload_at = time.time()

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self._state_path.exists():
            return {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw_state = payload.get("skills", payload)
        if not isinstance(raw_state, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, value in raw_state.items():
            if not isinstance(value, dict):
                continue
            result[str(key)] = value
        return result

    def _save_state(self, state: dict[str, dict[str, Any]]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps({"skills": state}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def parse_skill_file(path: str | Path) -> SkillSpec:
    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8")
    return parse_skill_markdown(raw, source=file_path)


def parse_skill_markdown(raw: str, source: str | Path = "<memory>") -> SkillSpec:
    file_path = Path(source)
    frontmatter, body = _split_frontmatter(raw)
    payload = _parse_frontmatter(frontmatter)
    name = str(payload.get("name") or file_path.stem).strip() or file_path.stem
    description = str(payload.get("description") or "").strip()
    triggers = _coerce_str_list(payload.get("triggers"))
    mode = str(payload.get("mode") or "contains").strip().lower() or "contains"
    if mode not in {"contains", "prefix"}:
        mode = "contains"
    priority = int(payload.get("priority", 0) or 0)
    content = body.strip()
    skill_id = str(payload.get("id") or file_path.stem).strip() or file_path.stem
    return SkillSpec(
        skill_id=skill_id,
        name=name,
        description=description,
        triggers=triggers,
        mode=mode,
        priority=priority,
        content=content,
        source=str(file_path),
        user_invocable=bool(payload.get("user-invocable", True)),
        disable_model_invocation=bool(payload.get("disable-model-invocation", False)),
        command_dispatch=str(payload.get("command-dispatch") or "").strip().lower(),
        command_tool=str(payload.get("command-tool") or "").strip().lower(),
        command_arg_mode=str(payload.get("command-arg-mode") or "raw").strip().lower() or "raw",
    )


def build_skill_markdown_template(
    *,
    skill_id: str = "",
    name: str = "",
    description: str = "",
    triggers: list[str] | None = None,
    mode: str = "contains",
    priority: int = 0,
    body: str = "在这里写下技能说明。",
) -> str:
    resolved_id = _slugify(skill_id or name or "custom-skill")
    resolved_name = str(name or resolved_id).strip() or resolved_id
    resolved_triggers = triggers or []
    lines = [
        "---",
        f"id: {resolved_id}",
        f"name: {resolved_name}",
        f"description: {description}",
        f"triggers: {json.dumps(resolved_triggers, ensure_ascii=False)}",
        f"mode: {mode}",
        f"priority: {priority}",
        "---",
        body.strip(),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _split_frontmatter(raw: str) -> tuple[str, str]:
    stripped = raw.lstrip()
    if not stripped.startswith("---"):
        return "", raw
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", raw
    frontmatter_lines: list[str] = []
    body_lines: list[str] = []
    in_frontmatter = True
    for line in lines[1:]:
        if in_frontmatter and line.strip() == "---":
            in_frontmatter = False
            continue
        if in_frontmatter:
            frontmatter_lines.append(line)
        else:
            body_lines.append(line)
    return "\n".join(frontmatter_lines), "\n".join(body_lines)


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    current_key = ""
    for raw_line in raw.splitlines():
        line = _strip_inline_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("- ") and current_key:
            existing = payload.get(current_key)
            if isinstance(existing, list):
                item = _parse_scalar(line[2:].strip())
                if item is not None:
                    existing.append(item)
            continue
        if ":" not in line:
            current_key = ""
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            current_key = ""
            continue
        if not value:
            payload[key] = []
            current_key = key
            continue
        payload[key] = _parse_scalar(value)
        current_key = key if isinstance(payload[key], list) else ""
    return payload


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return [] 


def _normalize_lookup_key(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def _parse_scalar(value: str) -> Any:
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
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result).rstrip()


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower())
    cleaned = cleaned.strip("-_")
    return cleaned or "custom-skill"


def _normalize_workspace_filename(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "custom-skill.md"
    name = Path(raw).name
    stem = _slugify(Path(name).stem)
    return f"{stem}.md"
