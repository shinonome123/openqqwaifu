from __future__ import annotations

import json
import re
from pathlib import Path

_PACKAGE_DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
_DEFAULT_BINDINGS_PATH = _PACKAGE_DATA_ROOT / "tool_bindings.yaml"


class ToolBindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "tool_binding_error")


def load_tool_bindings(path: str | Path | None = None) -> dict[str, object]:
    target = Path(path) if path else _DEFAULT_BINDINGS_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ToolBindingError("tool_bindings_missing", f"tool bindings file cannot be read: {target}") from exc
    except json.JSONDecodeError as exc:
        raise ToolBindingError("tool_bindings_invalid_json", f"tool bindings file is not valid JSON: {target}") from exc
    if not isinstance(payload, dict):
        raise ToolBindingError("tool_bindings_invalid", "tool bindings root must be an object")
    _validate_tool_bindings(payload)
    return payload if isinstance(payload, dict) else {}


CANONICAL_TOOL_IDS: set[str] = set()
OPENCLAW_TOOL_ALIASES: dict[str, str] = {}
TOOL_BINDINGS: dict[str, dict[str, object]] = {}


def reload_tool_bindings(path: str | Path | None = None) -> dict[str, object]:
    payload = load_tool_bindings(path)
    CANONICAL_TOOL_IDS.clear()
    CANONICAL_TOOL_IDS.update(
        normalize_tool_like_name(str(item))
        for item in payload.get("canonical_tool_ids", [])
        if normalize_tool_like_name(str(item))
    )
    OPENCLAW_TOOL_ALIASES.clear()
    OPENCLAW_TOOL_ALIASES.update(
        {
            normalize_tool_like_name(str(alias)): normalize_tool_like_name(str(target))
            for alias, target in dict(payload.get("aliases", {})).items()
            if normalize_tool_like_name(str(alias)) and normalize_tool_like_name(str(target))
        }
    )
    TOOL_BINDINGS.clear()
    TOOL_BINDINGS.update(
        {
            str(skill_id).strip(): dict(binding)
            for skill_id, binding in dict(payload.get("bindings", {})).items()
            if str(skill_id).strip() and isinstance(binding, dict)
        }
    )
    SELF_HOSTED_TOOL_IDS.clear()
    SELF_HOSTED_TOOL_IDS.update(CANONICAL_TOOL_IDS)
    return payload

SELF_HOSTED_TOOL_IDS: set[str] = set()

SELF_HOSTED_TOOL_TRIGGERS: dict[str, tuple[str, ...]] = {
    "weather": ("weather", "forecast", "天气", "天气预报", "查天气"),
}


def normalize_tool_like_name(value: str) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
    if not normalized or not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", normalized):
        return ""
    return normalized


def resolve_compatible_tool_id(*values: str) -> str:
    for raw in values:
        normalized = normalize_tool_like_name(raw)
        if not normalized:
            continue
        candidate = normalized
        seen: set[str] = set()
        while candidate and candidate not in seen:
            seen.add(candidate)
            if candidate in CANONICAL_TOOL_IDS:
                return candidate
            candidate = OPENCLAW_TOOL_ALIASES.get(candidate, "")
    return ""


def resolve_bound_tool_id(skill_id: str) -> str:
    binding = TOOL_BINDINGS.get(str(skill_id or "").strip(), {})
    backend = str(binding.get("backend", "") or "").strip()
    return resolve_compatible_tool_id(backend)


def _validate_tool_bindings(payload: dict[str, object]) -> None:
    raw_canonical = payload.get("canonical_tool_ids", [])
    if not isinstance(raw_canonical, list):
        raise ToolBindingError("tool_bindings_invalid", "canonical_tool_ids must be a list")
    canonical: set[str] = set()
    for item in raw_canonical:
        normalized = normalize_tool_like_name(str(item))
        if not normalized:
            raise ToolBindingError("tool_bindings_invalid_tool_id", f"invalid canonical tool id: {item}")
        if normalized in canonical:
            raise ToolBindingError("tool_bindings_duplicate_tool_id", f"duplicate canonical tool id: {normalized}")
        canonical.add(normalized)

    raw_aliases = payload.get("aliases", {})
    if not isinstance(raw_aliases, dict):
        raise ToolBindingError("tool_bindings_invalid", "aliases must be an object")
    aliases: dict[str, str] = {}
    for alias, target in raw_aliases.items():
        normalized_alias = normalize_tool_like_name(str(alias))
        normalized_target = normalize_tool_like_name(str(target))
        if not normalized_alias or not normalized_target:
            raise ToolBindingError("tool_bindings_invalid_alias", f"invalid alias mapping: {alias} -> {target}")
        if normalized_alias in aliases and aliases[normalized_alias] != normalized_target:
            raise ToolBindingError("tool_bindings_alias_conflict", f"alias {normalized_alias} maps to multiple targets")
        if normalized_alias in canonical and normalized_alias != normalized_target:
            raise ToolBindingError("tool_bindings_alias_conflict", f"alias conflicts with canonical tool id: {normalized_alias}")
        if normalized_target not in canonical:
            raise ToolBindingError("tool_bindings_unknown_alias_target", f"alias {normalized_alias} targets unknown tool: {normalized_target}")
        aliases[normalized_alias] = normalized_target

    raw_bindings = payload.get("bindings", {})
    if not isinstance(raw_bindings, dict):
        raise ToolBindingError("tool_bindings_invalid", "bindings must be an object")
    backends: dict[str, str] = {}
    for skill_id, binding in raw_bindings.items():
        normalized_skill_id = str(skill_id or "").strip()
        if not normalized_skill_id:
            raise ToolBindingError("tool_bindings_invalid_skill_id", "binding skill id is required")
        if not isinstance(binding, dict):
            raise ToolBindingError("tool_bindings_invalid_binding", f"binding for {normalized_skill_id} must be an object")
        backend = normalize_tool_like_name(str(binding.get("backend") or ""))
        if not backend:
            raise ToolBindingError("tool_bindings_missing_backend", f"binding for {normalized_skill_id} requires backend")
        resolved_backend = backend if backend in canonical else aliases.get(backend, "")
        if not resolved_backend:
            raise ToolBindingError(
                "tool_bindings_unknown_backend",
                f"binding for {normalized_skill_id} targets unknown backend: {backend}",
            )
        previous_skill = backends.get(resolved_backend)
        if previous_skill and previous_skill != normalized_skill_id:
            raise ToolBindingError(
                "tool_bindings_duplicate_backend",
                f"backend {resolved_backend} is bound by both {previous_skill} and {normalized_skill_id}",
            )
        backends[resolved_backend] = normalized_skill_id


try:
    reload_tool_bindings()
except ToolBindingError:
    pass
