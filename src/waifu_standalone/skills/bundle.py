from __future__ import annotations

import shutil
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .registry import SkillRegistry, parse_skill_file, parse_skill_markdown

_BUNDLE_MARKERS: tuple[tuple[str, str], ...] = (
    (".codex-plugin/plugin.json", "codex"),
    (".claude-plugin/plugin.json", "claude"),
    (".cursor-plugin/plugin.json", "cursor"),
)
_SKILL_ROOTS: tuple[str, ...] = (
    "skills",
    "commands",
    ".cursor/commands",
)


def import_skill_bundle(
    registry: SkillRegistry,
    source: str | Path,
    *,
    overwrite: bool = True,
) -> dict[str, object]:
    with _materialize_bundle_source(source) as prepared:
        root = prepared["root"]
        source_kind = str(prepared["source_kind"])
        bundle_type = _detect_bundle_type(root)
        skill_files = _discover_skill_files(root)
        if not skill_files:
            raise ValueError("no compatible skill markdown files were found in the bundle")

        discovered: list[tuple[Path, str, object]] = []
        imported_refs: list[tuple[str, str, str]] = []
        skipped: list[dict[str, object]] = []
        for skill_file in skill_files:
            markdown = skill_file.read_text(encoding="utf-8")
            if not _looks_like_skill_markdown(markdown, skill_file=skill_file):
                skipped.append(
                    {
                        "id": "",
                        "path": str(skill_file.relative_to(root)).replace("\\", "/"),
                        "reason": "not_a_skill_markdown",
                    }
                )
                continue
            parsed = parse_skill_markdown(markdown, source=str(skill_file.relative_to(root)).replace("\\", "/"))
            existing = registry.get_skill(parsed.skill_id)
            if existing is not None and not overwrite:
                skipped.append(
                    {
                        "id": parsed.skill_id,
                        "path": str(skill_file.relative_to(root)).replace("\\", "/"),
                        "reason": "exists",
                    }
                )
                continue
            discovered.append((skill_file, markdown, parsed))

        destination: Path | None = None
        if discovered:
            bundle_dir = _suggest_workspace_bundle_dir(
                root,
                prepared["source"],
                primary_skill_id=discovered[0][2].skill_id,
            )
            destination = registry.workspace_root / bundle_dir
            if destination.exists():
                if overwrite:
                    shutil.rmtree(destination)
                else:
                    for skill_file, _, parsed in discovered:
                        skipped.append(
                            {
                                "id": parsed.skill_id,
                                "path": str(skill_file.relative_to(root)).replace("\\", "/"),
                                "reason": "bundle_exists",
                            }
                        )
                    discovered = []
                    destination = None
            if discovered and destination is not None:
                _copy_bundle_tree(root, destination)
        for skill_file, markdown, parsed in discovered:
            assert destination is not None
            imported_refs.append(
                (
                    parsed.skill_id,
                    markdown,
                    str(skill_file.relative_to(root)).replace("\\", "/"),
                )
            )
        if discovered:
            registry._touch_reload()
        imported: list[dict[str, object]] = []
        for skill_id, markdown, bundle_path in imported_refs:
            installed = registry.get_skill(skill_id)
            if installed is None:
                detail = parse_skill_file(destination / bundle_path).as_dict() if destination is not None else {}
            else:
                detail = installed.as_dict()
            detail["markdown"] = markdown
            detail["bundle_path"] = bundle_path
            imported.append(detail)
        ready = [detail for detail in imported if bool(detail.get("directly_usable"))]
        needs_setup = [detail for detail in imported if not bool(detail.get("directly_usable"))]

        return {
            "format": "openclaw-compatible-bundle",
            "bundle_type": bundle_type,
            "source": str(prepared["source"]),
            "source_kind": source_kind,
            "skill_count": len(skill_files),
            "imported_count": len(imported),
            "skipped_count": len(skipped),
            "ready_count": len(ready),
            "needs_setup_count": len(needs_setup),
            "status": "ready" if imported and not needs_setup else ("partial" if ready else "needs_setup"),
            "imported": imported,
            "ready": ready,
            "needs_setup": needs_setup,
            "skipped": skipped,
        }


@contextmanager
def _materialize_bundle_source(source: str | Path) -> Iterator[dict[str, object]]:
    raw_source = Path(source).expanduser()
    if raw_source.is_dir():
        yield {"root": raw_source.resolve(), "source": raw_source.resolve(), "source_kind": "directory"}
        return
    if raw_source.is_file() and raw_source.suffix.lower() == ".md":
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            target = temp_root / raw_source.name
            target.write_text(raw_source.read_text(encoding="utf-8"), encoding="utf-8")
            yield {"root": temp_root, "source": raw_source.resolve(), "source_kind": "markdown"}
        return
    if raw_source.is_file():
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            _extract_archive(raw_source, temp_root)
            yield {"root": _normalize_archive_root(temp_root), "source": raw_source.resolve(), "source_kind": "archive"}
        return
    raise ValueError("bundle source must be an existing directory, archive, or markdown file")


def _extract_archive(archive_path: Path, destination: Path) -> None:
    lowered_name = archive_path.name.lower()
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    _ensure_archive_path(destination, member.filename).mkdir(parents=True, exist_ok=True)
                    continue
                _write_archive_member(destination, member.filename, archive.read(member))
        return
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                if member.isdir():
                    _ensure_archive_path(destination, member.name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                _write_archive_member(destination, member.name, extracted.read())
        return
    raise ValueError(f"unsupported archive format: {lowered_name}")


def _write_archive_member(destination: Path, member_name: str, payload: bytes) -> None:
    target = _ensure_archive_path(destination, member_name)
    if member_name.endswith("/"):
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _ensure_archive_path(destination: Path, member_name: str) -> Path:
    candidate = (destination / member_name).resolve()
    try:
        candidate.relative_to(destination.resolve())
    except ValueError as exc:
        raise ValueError(f"archive member escapes bundle root: {member_name}") from exc
    return candidate


def _normalize_archive_root(root: Path) -> Path:
    directories = [item for item in root.iterdir() if item.is_dir()]
    files = [item for item in root.iterdir() if item.is_file()]
    if len(directories) == 1 and not files:
        return directories[0]
    return root


def _detect_bundle_type(root: Path) -> str:
    for marker, bundle_type in _BUNDLE_MARKERS:
        if (root / marker).is_file():
            return bundle_type
    if any((root / rel_path).exists() for rel_path in _SKILL_ROOTS):
        return "openclaw-compatible"
    return "plain"


def _discover_skill_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    direct_skill = root / "SKILL.md"
    if direct_skill.is_file():
        candidates.append(direct_skill)
    for rel_path in _SKILL_ROOTS:
        skill_root = root / rel_path
        if not skill_root.exists():
            continue
        candidates.extend(path for path in skill_root.rglob("*.md") if path.is_file())
    if not candidates:
        candidates.extend(path for path in root.glob("*.md") if path.is_file())
    if not candidates:
        candidates.extend(path for path in root.rglob("SKILL.md") if path.is_file())
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in sorted(candidates):
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def _suggest_workspace_filename(skill_file: Path, *, bundle_root: Path) -> str:
    if skill_file.name.lower() != "skill.md":
        return skill_file.name
    parent_name = skill_file.parent.name
    if parent_name and skill_file.parent != bundle_root:
        return f"{parent_name}.md"
    bundle_name = bundle_root.name or "bundle-skill"
    return f"{bundle_name}.md"


def _suggest_workspace_bundle_dir(bundle_root: Path, source: object, *, primary_skill_id: str) -> str:
    preferred = Path(str(source)).stem or bundle_root.name or primary_skill_id or "bundle-skill"
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in preferred.lower()).strip("-_")
    return cleaned or primary_skill_id or "bundle-skill"


def _copy_bundle_tree(source_root: Path, destination_root: Path) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    for child in source_root.iterdir():
        target = destination_root / child.name
        if child.is_dir():
            shutil.copytree(child, target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, target)


def _looks_like_skill_markdown(markdown: str, *, skill_file: Path) -> bool:
    if skill_file.name.lower() == "skill.md":
        return True
    return str(markdown or "").lstrip().startswith("---")
