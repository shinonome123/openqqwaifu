from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .skill_registry import SkillRegistry, build_skill_markdown_template


@dataclass(slots=True)
class SkillPackEntry:
    skill_id: str
    filename: str
    markdown: str
    name: str = ""
    description: str = ""
    source_kind: str = "workspace"

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.skill_id,
            "filename": self.filename,
            "markdown": self.markdown,
            "name": self.name,
            "description": self.description,
            "source_kind": self.source_kind,
        }


@dataclass(slots=True)
class SkillPack:
    name: str
    description: str
    skills: list[SkillPackEntry]
    version: int = 1
    exported_at: float = 0.0
    format_name: str = "waifu-skill-pack"

    def as_dict(self) -> dict[str, object]:
        return {
            "format": self.format_name,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "exported_at": self.exported_at,
            "skill_count": len(self.skills),
            "skills": [entry.as_dict() for entry in self.skills],
        }


def build_skill_pack_template() -> dict[str, object]:
    return SkillPack(
        name="custom-skill-pack",
        description="A portable bundle of Waifu standalone skills.",
        exported_at=time.time(),
        skills=[
            SkillPackEntry(
                skill_id="custom-skill",
                filename="custom-skill.md",
                markdown=build_skill_markdown_template(
                    skill_id="custom-skill",
                    name="自定义技能",
                    description="描述这个 skill 的职责。",
                    triggers=["触发词"],
                    mode="prefix",
                    priority=4,
                    body="在这里写下技能说明，或者补 command-dispatch / command-tool 做工具分发。",
                ),
                name="自定义技能",
                description="描述这个 skill 的职责。",
                source_kind="workspace",
            )
        ],
    ).as_dict()


def export_skill_pack(
    registry: SkillRegistry,
    *,
    skill_ids: list[str] | None = None,
    include_builtin: bool = False,
    name: str = "",
    description: str = "",
) -> dict[str, object]:
    selected = {str(item).strip() for item in skill_ids or [] if str(item).strip()}
    entries: list[SkillPackEntry] = []
    for skill in registry.list_skills():
        if selected and skill.skill_id not in selected:
            continue
        if not include_builtin and skill.source_kind != "workspace":
            continue
        markdown = registry.get_skill_markdown(skill.skill_id)
        if not markdown:
            continue
        entries.append(
            SkillPackEntry(
                skill_id=skill.skill_id,
                filename=Path(skill.source).name or f"{skill.skill_id}.md",
                markdown=markdown,
                name=skill.name,
                description=skill.description,
                source_kind=skill.source_kind,
            )
        )
    entries.sort(key=lambda item: item.skill_id)
    pack = SkillPack(
        name=str(name or ("selected-skill-pack" if selected else "workspace-skill-pack")).strip(),
        description=str(description or "").strip(),
        exported_at=time.time(),
        skills=entries,
    )
    return pack.as_dict()


def import_skill_pack(
    registry: SkillRegistry,
    payload: dict[str, object] | str,
    *,
    overwrite: bool = True,
) -> dict[str, object]:
    pack = parse_skill_pack(payload)
    imported: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for entry in pack.skills:
        existing = registry.get_skill(entry.skill_id)
        if existing is not None and not overwrite:
            skipped.append(
                {
                    "id": entry.skill_id,
                    "reason": "exists",
                }
            )
            continue
        skill = registry.install_workspace_skill(entry.markdown, filename=entry.filename)
        imported.append(skill.as_dict())
    return {
        "format": pack.format_name,
        "version": pack.version,
        "name": pack.name,
        "description": pack.description,
        "skill_count": len(pack.skills),
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "imported": imported,
        "skipped": skipped,
    }


def parse_skill_pack(payload: dict[str, object] | str) -> SkillPack:
    raw: dict[str, object]
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("skill pack payload is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("skill pack payload must be a JSON object")
        raw = parsed
    elif isinstance(payload, dict):
        raw = payload
    else:
        raise ValueError("skill pack payload must be a dict or JSON string")

    format_name = str(raw.get("format") or "waifu-skill-pack").strip() or "waifu-skill-pack"
    if format_name != "waifu-skill-pack":
        raise ValueError("unsupported skill pack format")
    skills_value = raw.get("skills")
    if not isinstance(skills_value, list) or not skills_value:
        raise ValueError("skill pack must include at least one skill")

    entries: list[SkillPackEntry] = []
    for item in skills_value:
        if not isinstance(item, dict):
            raise ValueError("skill pack entry must be an object")
        skill_id = str(item.get("id") or "").strip()
        filename = str(item.get("filename") or f"{skill_id}.md").strip()
        markdown = str(item.get("markdown") or "").strip()
        if not skill_id or not markdown:
            raise ValueError("skill pack entry requires id and markdown")
        entries.append(
            SkillPackEntry(
                skill_id=skill_id,
                filename=filename,
                markdown=markdown,
                name=str(item.get("name") or "").strip(),
                description=str(item.get("description") or "").strip(),
                source_kind=str(item.get("source_kind") or "workspace").strip() or "workspace",
            )
        )
    return SkillPack(
        name=str(raw.get("name") or "imported-skill-pack").strip() or "imported-skill-pack",
        description=str(raw.get("description") or "").strip(),
        skills=entries,
        version=int(raw.get("version", 1) or 1),
        exported_at=float(raw.get("exported_at", 0.0) or 0.0),
        format_name=format_name,
    )
