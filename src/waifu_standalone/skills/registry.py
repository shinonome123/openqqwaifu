from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..config import AppConfig
from .tool_aliases import (
    SELF_HOSTED_TOOL_IDS,
    SELF_HOSTED_TOOL_TRIGGERS,
    normalize_tool_like_name,
    resolve_compatible_tool_id,
)

_WORKSPACE_BUNDLE_ROOTS: tuple[str, ...] = (
    "skills",
    "commands",
    ".cursor/commands",
)


@dataclass(slots=True)
class SkillSpec:
    skill_id: str
    name: str
    description: str
    triggers: list[str]
    aliases: list[str]
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
    homepage: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    auto_bound: bool = False
    eligible: bool = True
    ineligibility_reasons: list[str] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        if not self.enabled or not self.eligible:
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
        return self.enabled and self.eligible and self.command_dispatch == "tool" and bool(self.command_tool)

    @property
    def prompt_visible(self) -> bool:
        return self.enabled and self.eligible and not self.disable_model_invocation

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "aliases": list(self.aliases),
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
            "homepage": self.homepage,
            "metadata": dict(self.metadata),
            "auto_bound": self.auto_bound,
            "eligible": self.eligible,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "dispatches_tool": self.dispatches_tool,
            "prompt_visible": self.prompt_visible,
            "directly_usable": self.enabled and self.eligible and (self.dispatches_tool or bool(self.triggers)),
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
        self._registered_skills: dict[str, SkillSpec] = {}
        self._last_reload_at = time.time()
        self._reload_count = 0

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def list_skills(self) -> list[SkillSpec]:
        state = self._load_state()
        skills: list[SkillSpec] = []
        seen_ids: set[str] = set()
        for skill_file in sorted(self._builtin_root.glob("*.md")):
            skill = parse_skill_file(skill_file)
            if skill.skill_id in seen_ids:
                continue
            seen_ids.add(skill.skill_id)
            runtime = state.get(skill.skill_id, {})
            skill.enabled = bool(runtime.get("enabled", True))
            skill.source_kind = "builtin"
            self._apply_runtime_bridge(skill)
            self._apply_runtime_compatibility(skill)
            skills.append(skill)
        for skill_file in self._workspace_skill_files():
            skill = parse_skill_file(skill_file)
            if skill.skill_id in seen_ids:
                continue
            seen_ids.add(skill.skill_id)
            runtime = state.get(skill.skill_id, {})
            skill.enabled = bool(runtime.get("enabled", True))
            skill.source_kind = "workspace"
            self._apply_runtime_bridge(skill)
            self._apply_runtime_compatibility(skill)
            skills.append(skill)
        for registered in self._registered_skills.values():
            if registered.skill_id in seen_ids:
                continue
            seen_ids.add(registered.skill_id)
            skill = replace(registered)
            runtime = state.get(skill.skill_id, {})
            skill.enabled = bool(runtime.get("enabled", skill.enabled))
            self._apply_runtime_bridge(skill)
            self._apply_runtime_compatibility(skill)
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
        skills = self.list_skills()
        for skill in skills:
            if _normalize_lookup_key(skill.skill_id) == target:
                return skill
        for skill in skills:
            if _normalize_lookup_key(skill.name) == target:
                return skill
        for skill in skills:
            for alias in skill.aliases:
                if _normalize_lookup_key(alias) == target:
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
        if current_path.parent != self._workspace_root:
            if current_path.name.lower() == "skill.md":
                next_path = current_path.parent / "SKILL.md"
            else:
                next_path = current_path.parent / current_path.name
        else:
            next_name = _normalize_workspace_filename(f"{parsed.skill_id}.md")
            next_path = self._workspace_root / next_name
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        next_path.parent.mkdir(parents=True, exist_ok=True)
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
        if path.parent != self._workspace_root and path.name.lower() == "skill.md":
            shutil.rmtree(path.parent, ignore_errors=False)
        else:
            path.unlink()
        state = self._load_state()
        state.pop(target, None)
        self._save_state(state)
        self._touch_reload()
        return True

    def register_runtime_skill(
        self,
        markdown: str,
        *,
        source_name: str,
        source_kind: str = "plugin",
        enabled: bool = True,
    ) -> SkillSpec:
        source = str(source_name or "runtime-skill").strip() or "runtime-skill"
        parsed = parse_skill_markdown(markdown, source=source)
        skill = SkillSpec(
            skill_id=parsed.skill_id,
            name=parsed.name,
            description=parsed.description,
            triggers=list(parsed.triggers),
            aliases=list(parsed.aliases),
            mode=parsed.mode,
            priority=parsed.priority,
            content=parsed.content,
            source=source,
            source_kind=str(source_kind or "plugin").strip() or "plugin",
            enabled=bool(enabled),
            user_invocable=parsed.user_invocable,
            disable_model_invocation=parsed.disable_model_invocation,
            command_dispatch=parsed.command_dispatch,
            command_tool=parsed.command_tool,
            command_arg_mode=parsed.command_arg_mode,
            homepage=parsed.homepage,
            metadata=dict(parsed.metadata),
            eligible=parsed.eligible,
            ineligibility_reasons=list(parsed.ineligibility_reasons),
        )
        self._registered_skills[skill.skill_id] = skill
        self._touch_reload()
        return self.get_skill(skill.skill_id) or skill

    def _candidate_paths(self) -> list[Path]:
        return [self._workspace_root, self._builtin_root]

    def _find_skill_file(self, skill_id: str, *, workspace_only: bool = False) -> Path | None:
        target = str(skill_id or "").strip()
        if not target:
            return None
        if workspace_only:
            candidates = self._workspace_skill_files()
        else:
            candidates = sorted(self._builtin_root.glob("*.md")) + self._workspace_skill_files()
        for skill_file in candidates:
            try:
                skill = parse_skill_file(skill_file)
            except (OSError, UnicodeDecodeError):
                continue
            if skill.skill_id == target:
                return skill_file
        return None

    def _workspace_skill_files(self) -> list[Path]:
        if not self._workspace_root.exists():
            return []
        candidates: list[Path] = [path for path in self._workspace_root.glob("*.md") if path.is_file()]
        for bundle_root in sorted(self._workspace_root.iterdir()):
            if not bundle_root.is_dir():
                continue
            direct_skill = bundle_root / "SKILL.md"
            if direct_skill.is_file():
                candidates.append(direct_skill)
            discovered = False
            for rel_path in _WORKSPACE_BUNDLE_ROOTS:
                skill_root = bundle_root / rel_path
                if not skill_root.exists():
                    continue
                discovered = True
                candidates.extend(path for path in skill_root.rglob("*.md") if path.is_file())
            if not discovered and not direct_skill.is_file():
                candidates.extend(path for path in bundle_root.glob("*.md") if path.is_file())
        seen: set[str] = set()
        ordered: list[Path] = []
        for path in candidates:
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            ordered.append(path)
        return ordered

    def _touch_reload(self) -> None:
        self._reload_count += 1
        self._last_reload_at = time.time()

    def _apply_runtime_compatibility(self, skill: SkillSpec) -> None:
        metadata = _compat_runtime_metadata(skill.metadata)
        reasons: list[str] = []
        if skill.command_dispatch == "tool" and skill.command_tool in SELF_HOSTED_TOOL_IDS:
            skill.eligible = True
            skill.ineligibility_reasons = []
            return
        if bool(metadata.get("always")):
            skill.eligible = True
            skill.ineligibility_reasons = []
            return

        allowed_platforms = _coerce_str_list(metadata.get("os"))
        if allowed_platforms and sys.platform not in allowed_platforms:
            reasons.append("unsupported os")

        requires = metadata.get("requires", {})
        requires = requires if isinstance(requires, dict) else {}

        for binary in _coerce_str_list(requires.get("bins")):
            if shutil.which(binary) is None:
                reasons.append(f"missing bin:{binary}")

        any_bins = _coerce_str_list(requires.get("anyBins"))
        if any_bins and not any(shutil.which(binary) for binary in any_bins):
            reasons.append(f"missing any bin:{'/'.join(any_bins[:3])}")

        for env_name in _coerce_str_list(requires.get("env")):
            if not str(os.getenv(env_name) or "").strip():
                reasons.append(f"missing env:{env_name}")

        for config_path in _coerce_str_list(requires.get("config")):
            if not _config_path_truthy(self.config, config_path):
                reasons.append(f"missing config:{config_path}")

        skill.eligible = not reasons
        skill.ineligibility_reasons = reasons

    def _apply_runtime_bridge(self, skill: SkillSpec) -> None:
        if skill.command_dispatch == "tool" and skill.command_tool:
            resolved_tool = resolve_compatible_tool_id(skill.command_tool)
            if resolved_tool:
                skill.command_tool = resolved_tool
            if not skill.triggers and skill.command_tool in SELF_HOSTED_TOOL_IDS:
                skill.triggers = _infer_tool_triggers(skill, skill.command_tool)
            return
        resolved_tool = resolve_compatible_tool_id(skill.skill_id, skill.name, *skill.aliases)
        if not resolved_tool:
            return
        skill.command_dispatch = "tool"
        skill.command_tool = resolved_tool
        skill.disable_model_invocation = True
        skill.auto_bound = True
        if not skill.triggers:
            skill.triggers = _infer_tool_triggers(skill, resolved_tool)
        if skill.priority <= 0:
            skill.priority = 10

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
    metadata = _coerce_mapping(payload.get("metadata"))
    compat_metadata = _compat_runtime_metadata(metadata)
    triggers = _coerce_str_list(payload.get("triggers"))
    aliases = _coerce_str_list(payload.get("aliases"))
    aliases.extend(_coerce_str_list(compat_metadata.get("aliases")))
    aliases = [alias for alias in aliases if alias]
    mode = str(payload.get("mode") or "contains").strip().lower() or "contains"
    if mode not in {"contains", "prefix"}:
        mode = "contains"
    priority = int(payload.get("priority", 0) or 0)
    content = body.strip()
    explicit_id = str(payload.get("id") or "").strip()
    if explicit_id:
        skill_id = explicit_id
    elif file_path.stem.lower() == "skill":
        skill_id = _slugify(str(payload.get("name") or file_path.parent.name or file_path.stem))
    else:
        skill_id = str(file_path.stem).strip() or file_path.stem
    return SkillSpec(
        skill_id=skill_id,
        name=name,
        description=description,
        triggers=triggers,
        aliases=_dedupe_str_list(aliases),
        mode=mode,
        priority=priority,
        content=content,
        source=str(file_path),
        user_invocable=bool(payload.get("user-invocable", True)),
        disable_model_invocation=bool(payload.get("disable-model-invocation", False)),
        command_dispatch=str(payload.get("command-dispatch") or "").strip().lower(),
        command_tool=str(payload.get("command-tool") or "").strip().lower(),
        command_arg_mode=str(payload.get("command-arg-mode") or "raw").strip().lower() or "raw",
        homepage=str(payload.get("homepage") or compat_metadata.get("homepage") or "").strip(),
        metadata=metadata,
    )


def build_skill_markdown_template(
    *,
    skill_id: str = "",
    name: str = "",
    description: str = "",
    triggers: list[str] | None = None,
    aliases: list[str] | None = None,
    mode: str = "contains",
    priority: int = 0,
    user_invocable: bool = True,
    disable_model_invocation: bool = False,
    command_dispatch: str = "",
    command_tool: str = "",
    command_arg_mode: str = "raw",
    homepage: str = "",
    metadata: dict[str, Any] | None = None,
    body: str = "在这里写下技能说明。",
) -> str:
    resolved_id = _slugify(skill_id or name or "custom-skill")
    resolved_name = str(name or resolved_id).strip() or resolved_id
    resolved_triggers = triggers or []
    resolved_aliases = _dedupe_str_list(aliases or [])
    lines = [
        "---",
        f"id: {resolved_id}",
        f"name: {resolved_name}",
        f"description: {description}",
        f"triggers: {json.dumps(resolved_triggers, ensure_ascii=False)}",
    ]
    if resolved_aliases:
        lines.append(f"aliases: {json.dumps(resolved_aliases, ensure_ascii=False)}")
    lines.extend(
        [
            f"mode: {mode}",
            f"priority: {priority}",
            f"user-invocable: {'true' if user_invocable else 'false'}",
            f"disable-model-invocation: {'true' if disable_model_invocation else 'false'}",
        ]
    )
    if command_dispatch:
        lines.append(f"command-dispatch: {command_dispatch}")
    if command_tool:
        lines.append(f"command-tool: {command_tool}")
    if command_arg_mode:
        lines.append(f"command-arg-mode: {command_arg_mode}")
    if homepage:
        lines.append(f"homepage: {homepage}")
    if metadata:
        lines.append(f"metadata: {json.dumps(metadata, ensure_ascii=False)}")
    lines.extend(["---", body.strip()])
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


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _compat_runtime_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    merged: dict[str, Any] = {}
    for namespace in ("openclaw", "nanobot"):
        raw = metadata.get(namespace, {})
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if key == "requires":
                existing = merged.get("requires", {})
                existing = dict(existing) if isinstance(existing, dict) else {}
                if isinstance(value, dict):
                    existing.update(value)
                merged["requires"] = existing
                continue
            if key == "aliases":
                existing_aliases = _coerce_str_list(merged.get("aliases"))
                merged["aliases"] = _dedupe_str_list(existing_aliases + _coerce_str_list(value))
                continue
            if key == "install":
                merged["install"] = list(value) if isinstance(value, list) else value
                continue
            if key == "homepage":
                merged["homepage"] = value
                continue
            merged[key] = value
    return merged


def _infer_tool_triggers(skill: SkillSpec, tool_id: str) -> list[str]:
    candidates = [
        skill.name,
        skill.skill_id,
        *skill.aliases,
    ]
    triggers: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        normalized = normalize_tool_like_name(raw)
        if not normalized or normalized in seen:
            continue
        if resolve_compatible_tool_id(normalized) != tool_id:
            continue
        seen.add(normalized)
        triggers.append(normalized)
    for raw in SELF_HOSTED_TOOL_TRIGGERS.get(tool_id, ()):
        value = str(raw or "").strip()
        normalized = _normalize_lookup_key(value)
        if not value or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        triggers.append(value)
    return triggers[:4]


def _dedupe_str_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        normalized = _normalize_lookup_key(value)
        if not value or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


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
    if value.startswith(('"', "[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
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


def _config_path_truthy(config: AppConfig, path: str) -> bool:
    current: Any = config
    for segment in [part for part in str(path or "").split(".") if part]:
        if isinstance(current, dict):
            current = current.get(segment)
            continue
        if hasattr(current, segment):
            current = getattr(current, segment)
            continue
        return False
    if isinstance(current, bool):
        return current
    if current is None:
        return False
    if isinstance(current, (list, tuple, dict, set)):
        return bool(current)
    return bool(str(current).strip()) if isinstance(current, str) else bool(current)
