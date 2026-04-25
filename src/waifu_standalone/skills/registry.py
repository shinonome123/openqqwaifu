from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
import time
import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..config import AppConfig
from .tool_aliases import (
    SELF_HOSTED_TOOL_TRIGGERS,
    ToolBindingError,
    normalize_tool_like_name,
    reload_tool_bindings,
    resolve_compatible_tool_id,
)
from .telemetry import record_skill_error_event

_WORKSPACE_BUNDLE_ROOTS: tuple[str, ...] = (
    "skills",
    "commands",
    ".cursor/commands",
)


_LEGACY_SKILL_KEYS: set[str] = {
    "triggers",
    "mode",
    "disable-model-invocation",
    "command-dispatch",
    "command-tool",
    "command-arg-mode",
}

_DANGEROUS_TOOL_IDS: set[str] = {"write-file", "exec-command"}


class SkillManifestError(ValueError):
    def __init__(self, code: str, message: str, *, source: str = "") -> None:
        super().__init__(message)
        self.code = str(code or "invalid_skill_manifest")
        self.source = str(source or "")


@dataclass(slots=True)
class SkillTriggerSpec:
    command: str = ""
    llm_tool: bool = False
    keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "llm_tool": self.llm_tool,
            "keywords": list(self.keywords),
        }


@dataclass(slots=True)
class SkillHandlerSpec:
    type: str
    target: str = ""
    arg_mode: str = "structured"

    def as_dict(self) -> dict[str, object]:
        return {
            "type": self.type,
            "target": self.target,
            "arg_mode": self.arg_mode,
        }


@dataclass(slots=True)
class SkillPolicySpec:
    priority: int = 0
    user_invocable: bool = True
    risk_level: str = "safe"
    timeout_seconds: float = 30.0
    max_output_chars: int = 6000
    requires_authorization: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "priority": self.priority,
            "user_invocable": self.user_invocable,
            "risk_level": self.risk_level,
            "timeout_seconds": self.timeout_seconds,
            "max_output_chars": self.max_output_chars,
            "requires_authorization": self.requires_authorization,
        }


@dataclass(slots=True)
class SkillManifest:
    skill_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    trigger: SkillTriggerSpec
    handler: SkillHandlerSpec
    policy: SkillPolicySpec
    default_args: dict[str, Any]
    metadata: dict[str, Any]
    content: str
    source: str
    source_kind: str = "workspace"
    enabled: bool = True
    eligible: bool = True
    ineligibility_reasons: list[str] = field(default_factory=list)
    validation_errors: list[dict[str, str]] = field(default_factory=list)
    sha256: str = ""
    last_execution: dict[str, object] | None = None

    @property
    def priority(self) -> int:
        return int(self.policy.priority)

    @property
    def user_invocable(self) -> bool:
        return bool(self.policy.user_invocable)

    @property
    def command(self) -> str:
        return self.trigger.command or self.skill_id

    @property
    def keywords(self) -> list[str]:
        return list(self.trigger.keywords)

    @property
    def aliases(self) -> list[str]:
        return _coerce_str_list(self.metadata.get("aliases"))

    @property
    def triggers(self) -> list[str]:
        return list(self.trigger.keywords)

    @property
    def mode(self) -> str:
        return "contains"

    @property
    def homepage(self) -> str:
        return str(self.metadata.get("homepage", "") or "").strip()

    @property
    def handler_type(self) -> str:
        return self.handler.type

    @property
    def handler_target(self) -> str:
        return self.handler.target

    @property
    def command_tool(self) -> str:
        return self.handler.target if self.handler.type == "tool_id" else ""

    @command_tool.setter
    def command_tool(self, value: str) -> None:
        if self.handler.type == "tool_id":
            self.handler.target = str(value or "").strip().lower()

    @property
    def command_dispatch(self) -> str:
        return "tool" if self.handler.type == "tool_id" else self.handler.type

    @property
    def command_arg_mode(self) -> str:
        return self.handler.arg_mode

    @property
    def is_tool_handler(self) -> bool:
        return self.handler.type == "tool_id" and bool(self.handler.target)

    @property
    def is_prompt_policy(self) -> bool:
        return self.handler.type == "prompt_template"

    @property
    def dispatches_tool(self) -> bool:
        return self.is_tool_handler

    @property
    def prompt_visible(self) -> bool:
        return self.is_prompt_policy and self.enabled and self.eligible

    @property
    def disable_model_invocation(self) -> bool:
        return not bool(self.trigger.llm_tool)

    @property
    def model_visible(self) -> bool:
        return self.enabled and self.eligible and self.trigger.llm_tool

    @property
    def status(self) -> str:
        if self.validation_errors:
            return "invalid"
        if not self.enabled:
            return "disabled"
        if not self.eligible:
            return "ineligible"
        return "ready"

    def keyword_score(self, text: str) -> int:
        normalized = _normalize_lookup_key(text)
        if not normalized:
            return 0
        score = 0
        for keyword in self.keywords:
            candidate = _normalize_lookup_key(keyword)
            if candidate and candidate in normalized:
                score += max(1, min(8, len(candidate)))
        return score

    def as_dict(self) -> dict[str, object]:
        manifest = {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "trigger": self.trigger.as_dict(),
            "handler": self.handler.as_dict(),
            "policy": self.policy.as_dict(),
            "default_args": dict(self.default_args),
            "metadata": dict(self.metadata),
            "content": self.content,
            "sha256": self.sha256,
        }
        return {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "manifest": manifest,
            "status": self.status,
            "source": self.source,
            "source_kind": self.source_kind,
            "enabled": self.enabled,
            "eligible": self.eligible,
            "validation_errors": [dict(item) for item in self.validation_errors],
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "telemetry": _skill_telemetry_summary(self.skill_id),
            "last_execution": dict(self.last_execution or {}),
            "editable": self.source_kind == "workspace",
            "deletable": self.source_kind == "workspace",
        }


# Keep the old Python import name as a compatibility alias. The runtime
# contract is SkillManifest; legacy markdown fields are rejected by the parser.
SkillSpec = SkillManifest


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
        self._tool_binding_errors: list[dict[str, str]] = []

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def list_skills(self) -> list[SkillSpec]:
        state = self._load_state()
        skills: list[SkillSpec] = []
        seen_ids: set[str] = set()
        for skill_file in sorted(self._builtin_root.glob("*.md")):
            try:
                skill = parse_skill_file(skill_file)
            except SkillManifestError as exc:
                skills.append(_invalid_manifest_from_error(exc, skill_file, source_kind="builtin"))
                continue
            if skill.skill_id in seen_ids:
                skill.validation_errors.append({"code": "duplicate_id", "message": f"duplicate skill id: {skill.skill_id}"})
            seen_ids.add(skill.skill_id)
            runtime = state.get(skill.skill_id, {})
            skill.enabled = bool(runtime.get("enabled", True))
            skill.source_kind = "builtin"
            self._apply_runtime_bridge(skill)
            self._apply_runtime_compatibility(skill)
            skills.append(skill)
        for skill_file in self._workspace_skill_files():
            try:
                skill = parse_skill_file(skill_file)
            except SkillManifestError as exc:
                skills.append(_invalid_manifest_from_error(exc, skill_file, source_kind="workspace"))
                continue
            if skill.skill_id in seen_ids:
                skill.validation_errors.append({"code": "duplicate_id", "message": f"duplicate skill id: {skill.skill_id}"})
            seen_ids.add(skill.skill_id)
            runtime = state.get(skill.skill_id, {})
            skill.enabled = bool(runtime.get("enabled", True))
            skill.source_kind = "workspace"
            self._apply_runtime_bridge(skill)
            self._apply_runtime_compatibility(skill)
            skills.append(skill)
        for registered in self._registered_skills.values():
            if registered.skill_id in seen_ids:
                registered.validation_errors.append({"code": "duplicate_id", "message": f"duplicate skill id: {registered.skill_id}"})
            seen_ids.add(registered.skill_id)
            skill = replace(registered)
            runtime = state.get(skill.skill_id, {})
            skill.enabled = bool(runtime.get("enabled", skill.enabled))
            self._apply_runtime_bridge(skill)
            self._apply_runtime_compatibility(skill)
            skills.append(skill)
        self._apply_conflict_validation(skills)
        skills.sort(key=lambda item: (-item.priority, item.name))
        return skills

    def candidate_manifests(self, text: str, *, include_prompt: bool = True) -> list[SkillManifest]:
        """Return context-relevant manifests without executing keyword matches."""
        if not self.config.skills_enabled:
            return []
        skills = [
            skill
            for skill in self.list_skills()
            if skill.enabled and skill.eligible and not skill.validation_errors
        ]
        scored: list[tuple[int, SkillManifest]] = []
        normalized_text = str(text or "")
        has_url = bool(re.search(r"https?://|www\.", normalized_text, flags=re.IGNORECASE))
        for skill in skills:
            if not include_prompt and not skill.is_tool_handler:
                continue
            score = skill.keyword_score(text)
            if has_url and skill.command_tool == "summarize":
                score += 24
            elif has_url and skill.command_tool == "summary":
                score -= 12
            scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], -item[1].priority, item[1].name))
        return [skill for _, skill in scored[: max(1, int(self.config.max_active_skills or 3))]]

    def describe(self) -> dict[str, Any]:
        skills = self.list_skills()
        return {
            "enabled": self.config.skills_enabled,
            "count": len(skills),
            "reload_count": self._reload_count,
            "last_reload_at": self._last_reload_at,
            "reload_status": "error" if self._tool_binding_errors else "ok",
            "tool_binding_errors": [dict(item) for item in self._tool_binding_errors],
            "items": [skill.as_dict() for skill in skills],
        }

    def reload(self) -> dict[str, Any]:
        binding_errors: list[dict[str, str]] = []
        try:
            reload_tool_bindings()
        except ToolBindingError as exc:
            binding_errors.append({"code": exc.code, "message": str(exc)})
            record_skill_error_event(
                skill_id="__tool_bindings__",
                error_code=exc.code,
                message=str(exc),
                source="tool_bindings",
            )
        self._tool_binding_errors = binding_errors
        self._touch_reload()
        payload = self.describe()
        return payload

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
        return any(skill.is_tool_handler and skill.command_tool == target for skill in self.list_skills())

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
        skill = SkillManifest(
            skill_id=parsed.skill_id,
            name=parsed.name,
            description=parsed.description,
            input_schema=dict(parsed.input_schema),
            output_schema=dict(parsed.output_schema),
            trigger=replace(parsed.trigger),
            handler=replace(parsed.handler),
            policy=replace(parsed.policy),
            default_args=dict(parsed.default_args),
            metadata=dict(parsed.metadata),
            content=parsed.content,
            source=source,
            source_kind=str(source_kind or "plugin").strip() or "plugin",
            enabled=bool(enabled),
            eligible=parsed.eligible,
            ineligibility_reasons=list(parsed.ineligibility_reasons),
            validation_errors=[dict(item) for item in parsed.validation_errors],
            sha256=parsed.sha256,
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
            except (OSError, UnicodeDecodeError, SkillManifestError):
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

    @staticmethod
    def _apply_conflict_validation(skills: list[SkillSpec]) -> None:
        commands: dict[str, SkillSpec] = {}
        keywords: dict[str, SkillSpec] = {}
        for skill in skills:
            if skill.validation_errors:
                continue
            if skill.is_tool_handler and skill.handler_target in _DANGEROUS_TOOL_IDS and not skill.policy.requires_authorization:
                skill.validation_errors.append(
                    {
                        "code": "dangerous_tool_requires_authorization",
                        "message": f"{skill.handler_target} requires policy.requires_authorization=true",
                    }
                )
                continue
            command_key = _normalize_lookup_key(skill.command)
            if command_key:
                previous = commands.get(command_key)
                if previous is not None:
                    message = f"trigger.command conflicts with {previous.skill_id}: {skill.command}"
                    skill.validation_errors.append({"code": "command_conflict", "message": message})
                    previous.validation_errors.append({"code": "command_conflict", "message": message})
                else:
                    commands[command_key] = skill
            if skill.model_visible and skill.is_tool_handler:
                for keyword in skill.keywords:
                    keyword_key = _normalize_lookup_key(keyword)
                    if not keyword_key:
                        continue
                    previous_keyword = keywords.get(keyword_key)
                    if previous_keyword is not None and previous_keyword.handler_target != skill.handler_target:
                        message = f"trigger keyword conflicts with {previous_keyword.skill_id}: {keyword}"
                        skill.validation_errors.append({"code": "keyword_conflict", "message": message})
                        previous_keyword.validation_errors.append({"code": "keyword_conflict", "message": message})
                    else:
                        keywords[keyword_key] = skill

    def _apply_runtime_compatibility(self, skill: SkillSpec) -> None:
        metadata = _compat_runtime_metadata(skill.metadata)
        reasons: list[str] = []
        if skill.is_tool_handler:
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
        if skill.is_tool_handler and skill.command_tool:
            resolved_tool = resolve_compatible_tool_id(skill.command_tool)
            if resolved_tool:
                skill.command_tool = resolved_tool
            return

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


def _invalid_manifest_from_error(exc: SkillManifestError, path: Path, *, source_kind: str) -> SkillManifest:
    name = path.stem if path.stem else "invalid-skill"
    raw = ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        pass
    return SkillManifest(
        skill_id=f"invalid-{_slugify(name)}",
        name=name,
        description=str(exc),
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        trigger=SkillTriggerSpec(command=f"invalid-{_slugify(name)}", llm_tool=False, keywords=[]),
        handler=SkillHandlerSpec(type="prompt_template", target=""),
        policy=SkillPolicySpec(priority=-1000, user_invocable=False),
        default_args={},
        metadata={},
        content="",
        source=str(path),
        source_kind=source_kind,
        enabled=False,
        eligible=False,
        ineligibility_reasons=[exc.code],
        validation_errors=[{"code": exc.code, "message": str(exc)}],
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else "",
    )


def _skill_telemetry_summary(skill_id: str) -> dict[str, object]:
    try:
        from .telemetry import get_skill_telemetry_summary
    except ImportError:
        return {"calls": 0, "success": 0, "failure": 0, "success_rate": 0.0}
    return get_skill_telemetry_summary(skill_id)


def parse_skill_markdown(raw: str, source: str | Path = "<memory>") -> SkillSpec:
    file_path = Path(source)
    frontmatter, body = _split_frontmatter(raw)
    payload = _parse_frontmatter(frontmatter)
    legacy_keys = sorted(key for key in _LEGACY_SKILL_KEYS if key in payload)
    if legacy_keys:
        raise SkillManifestError(
            "legacy_skill_format",
            "legacy skill fields are no longer supported; use trigger/handler manifest fields",
            source=str(file_path),
        )
    name = str(payload.get("name") or file_path.stem).strip() or file_path.stem
    description = str(payload.get("description") or "").strip()
    metadata = _coerce_mapping(payload.get("metadata"))
    signature = str(payload.get("signature") or "").strip()
    if signature:
        metadata["signature"] = signature
    compat_metadata = _compat_runtime_metadata(metadata)
    aliases = _coerce_str_list(payload.get("aliases"))
    aliases.extend(_coerce_str_list(compat_metadata.get("aliases")))
    if aliases:
        metadata["aliases"] = _dedupe_str_list(aliases)
    homepage = str(payload.get("homepage") or compat_metadata.get("homepage") or "").strip()
    if homepage:
        metadata["homepage"] = homepage
    content = body.strip()
    explicit_id = str(payload.get("id") or "").strip()
    if explicit_id:
        skill_id = explicit_id
    elif file_path.stem.lower() == "skill":
        skill_id = _slugify(str(payload.get("name") or file_path.parent.name or file_path.stem))
    else:
        skill_id = str(file_path.stem).strip() or file_path.stem
    trigger = _parse_trigger_spec(payload.get("trigger"), skill_id=skill_id)
    handler = _parse_handler_spec(payload.get("handler"))
    policy = _parse_policy_spec(payload.get("policy"))
    input_schema = _schema_or_default(payload.get("input_schema") or payload.get("input-schema"))
    output_schema = _schema_or_default(payload.get("output_schema") or payload.get("output-schema"))
    default_args = _coerce_mapping(payload.get("default_args") or payload.get("default-args"))
    errors = _validate_manifest(
        skill_id=skill_id,
        name=name,
        trigger=trigger,
        handler=handler,
        input_schema=input_schema,
        output_schema=output_schema,
    )
    _apply_signature_validation(errors, metadata, content)
    return SkillManifest(
        skill_id=skill_id,
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        trigger=trigger,
        handler=handler,
        policy=policy,
        default_args=default_args,
        content=content,
        source=str(file_path),
        metadata=metadata,
        validation_errors=errors,
        sha256=hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest(),
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
    resolved_aliases = _dedupe_str_list(aliases or [])
    resolved_tool = str(command_tool or "").strip().lower()
    handler_type = "tool_id" if command_dispatch == "tool" or resolved_tool else "prompt_template"
    handler_target = resolved_tool if handler_type == "tool_id" else resolved_id
    trigger_payload = {
        "command": resolved_id,
        "llm_tool": handler_type == "tool_id",
        "keywords": triggers or [],
    }
    handler_payload = {
        "type": handler_type,
        "target": handler_target,
        "arg_mode": command_arg_mode or "structured",
    }
    policy_payload = {
        "priority": int(priority or 0),
        "user_invocable": bool(user_invocable),
        "risk_level": "safe",
        "timeout_seconds": 30,
        "max_output_chars": 6000,
    }
    metadata_payload = dict(metadata or {})
    if resolved_aliases:
        metadata_payload["aliases"] = resolved_aliases
    if homepage:
        metadata_payload["homepage"] = homepage
    lines = [
        "---",
        f"id: {resolved_id}",
        f"name: {resolved_name}",
        f"description: {description}",
        'input_schema: {"type":"object","properties":{}}',
        'output_schema: {"type":"object","properties":{}}',
        f"trigger: {json.dumps(trigger_payload, ensure_ascii=False)}",
        f"handler: {json.dumps(handler_payload, ensure_ascii=False)}",
        f"policy: {json.dumps(policy_payload, ensure_ascii=False)}",
        "default_args: {}",
    ]
    if metadata_payload:
        lines.append(f"metadata: {json.dumps(metadata_payload, ensure_ascii=False)}")
    lines.extend(["---", body.strip()])
    return "\n".join(lines).rstrip() + "\n"


def _parse_trigger_spec(value: Any, *, skill_id: str) -> SkillTriggerSpec:
    payload = _coerce_mapping(value)
    command = str(payload.get("command") or skill_id).strip() or skill_id
    return SkillTriggerSpec(
        command=command,
        llm_tool=bool(payload.get("llm_tool", False)),
        keywords=_dedupe_str_list(_coerce_str_list(payload.get("keywords"))),
    )


def _parse_handler_spec(value: Any) -> SkillHandlerSpec:
    payload = _coerce_mapping(value)
    handler_type = str(payload.get("type") or "").strip().lower()
    if handler_type not in {"tool_id", "prompt_template", "callable"}:
        handler_type = ""
    target = str(payload.get("target") or "").strip()
    arg_mode = str(payload.get("arg_mode") or "structured").strip().lower() or "structured"
    return SkillHandlerSpec(type=handler_type, target=target, arg_mode=arg_mode)


def _parse_policy_spec(value: Any) -> SkillPolicySpec:
    payload = _coerce_mapping(value)
    risk_level = str(payload.get("risk_level") or "safe").strip().lower()
    if risk_level not in {"safe", "filesystem", "command", "mutating"}:
        risk_level = "safe"
    return SkillPolicySpec(
        priority=_safe_int(payload.get("priority"), 0),
        user_invocable=bool(payload.get("user_invocable", True)),
        risk_level=risk_level,
        timeout_seconds=max(1.0, _safe_float(payload.get("timeout_seconds"), 30.0)),
        max_output_chars=max(200, _safe_int(payload.get("max_output_chars"), 6000)),
        requires_authorization=bool(payload.get("requires_authorization", False)),
    )


def _schema_or_default(value: Any) -> dict[str, Any]:
    payload = _coerce_mapping(value)
    if not payload:
        return {"type": "object", "properties": {}}
    if payload.get("type") != "object":
        payload["type"] = "object"
    if not isinstance(payload.get("properties"), dict):
        payload["properties"] = {}
    return payload


def _validate_manifest(
    *,
    skill_id: str,
    name: str,
    trigger: SkillTriggerSpec,
    handler: SkillHandlerSpec,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not str(skill_id or "").strip():
        errors.append({"code": "missing_id", "message": "manifest id is required"})
    if not str(name or "").strip():
        errors.append({"code": "missing_name", "message": "manifest name is required"})
    if not trigger.command:
        errors.append({"code": "missing_command", "message": "trigger.command is required"})
    if handler.type not in {"tool_id", "prompt_template", "callable"}:
        errors.append({"code": "invalid_handler", "message": "handler.type must be tool_id, prompt_template or callable"})
    if handler.type in {"tool_id", "callable"} and not handler.target:
        errors.append({"code": "missing_handler_target", "message": "handler.target is required"})
    for key, schema in (("input_schema", input_schema), ("output_schema", output_schema)):
        if schema.get("type") != "object":
            errors.append({"code": f"invalid_{key}", "message": f"{key}.type must be object"})
    return errors


def _apply_signature_validation(errors: list[dict[str, str]], metadata: dict[str, Any], content: str) -> None:
    signature = str(metadata.get("signature") or "").strip()
    if not signature:
        return
    if not signature.startswith("sha256:"):
        metadata["signature_verified"] = False
        errors.append(
            {
                "code": "unsupported_signature",
                "message": "signature must use sha256:<body_digest> format",
            }
        )
        return
    expected = signature.removeprefix("sha256:").strip().lower()
    actual = hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()
    metadata["signature_verified"] = expected == actual
    if expected != actual:
        errors.append(
            {
                "code": "signature_mismatch",
                "message": "skill signature does not match body digest",
            }
        )


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


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
