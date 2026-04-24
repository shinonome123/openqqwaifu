from __future__ import annotations

import asyncio
import html
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from ..config import MarketplaceConfig, MarketplaceSourceConfig
from ..infra import AsyncHttpTransport, SyncHttpTransport, TransportMetricsScope

_GITHUB_BUNDLE_MARKERS = {
    "SKILL.md",
    "skills",
    "commands",
    ".codex-plugin",
    ".claude-plugin",
    ".cursor-plugin",
}
_FILENAME_RE = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)
_CLAWHUB_DOWNLOAD_RE = re.compile(r'href="([^"]+/api/v1/download\?[^"]*slug=[^"&]+[^"]*)"', re.IGNORECASE)


@dataclass(slots=True)
class MarketplaceSkill:
    source_id: str
    skill_id: str
    name: str
    author: str
    description: str
    github_url: str
    skill_url: str
    stars: int = 0
    updated_at: str = ""
    install_url: str = ""
    source_name: str = ""
    score: float | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "id": self.skill_id,
            "name": self.name,
            "author": self.author,
            "description": self.description,
            "github_url": self.github_url,
            "skill_url": self.skill_url,
            "install_url": self.install_url or self.github_url or self.skill_url,
            "stars": self.stars,
            "updated_at": self.updated_at,
        }
        if self.score is not None:
            payload["score"] = self.score
        return payload


class MarketplaceClient:
    def __init__(self, config: MarketplaceConfig):
        self.config = config
        max_timeout = max(
            (float(source.timeout_seconds or 10.0) for source in self.config.sources),
            default=10.0,
        )
        scope = TransportMetricsScope(kind="marketplace", target="skill_source")
        self._transport = SyncHttpTransport(timeout_seconds=max_timeout, metrics_scope=scope)
        self._async_transport = AsyncHttpTransport(timeout_seconds=max_timeout, metrics_scope=scope)

    def describe(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "default_query": self.config.default_query,
            "sources": [self._source_to_dict(source) for source in self.config.sources],
        }

    def search(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        if not self.config.enabled:
            return {"enabled": False, "items": [], "query": query}
        sources = self._selected_sources(source_id)
        if not sources:
            return {"enabled": True, "items": [], "query": query, "error": "source_not_found"}
        if len(sources) == 1:
            source = sources[0]
            items = self._search_source(source, query=query, limit=limit)
            return {
                "enabled": True,
                "source": self._source_to_dict(source),
                "query": self._resolved_query(query),
                "items": items,
            }

        source_items: list[list[dict[str, object]]] = []
        errors: dict[str, str] = {}
        for source in sources:
            try:
                source_items.append(self._search_source(source, query=query, limit=limit))
            except Exception as exc:
                errors[source.source_id] = str(exc)
        return {
            "enabled": True,
            "aggregated": True,
            "sources": [self._source_to_dict(source) for source in sources],
            "query": self._resolved_query(query),
            "items": self._round_robin_merge(source_items, limit=limit),
            "errors": errors,
        }

    async def asearch(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        if not self.config.enabled:
            return {"enabled": False, "items": [], "query": query}
        sources = self._selected_sources(source_id)
        if not sources:
            return {"enabled": True, "items": [], "query": query, "error": "source_not_found"}
        if any(source.kind not in {"skillsmp"} for source in sources):
            return await asyncio.to_thread(self.search, query, source_id=source_id, limit=limit)
        if len(sources) == 1:
            source = sources[0]
            items = await self._asearch_source(source, query=query, limit=limit)
            return {
                "enabled": True,
                "source": self._source_to_dict(source),
                "query": self._resolved_query(query),
                "items": items,
            }

        errors: dict[str, str] = {}
        gathered = await asyncio.gather(
            *(self._asearch_source(source, query=query, limit=limit) for source in sources),
            return_exceptions=True,
        )
        source_items: list[list[dict[str, object]]] = []
        for source, result in zip(sources, gathered):
            if isinstance(result, Exception):
                errors[source.source_id] = str(result)
                continue
            source_items.append(result)
        return {
            "enabled": True,
            "aggregated": True,
            "sources": [self._source_to_dict(source) for source in sources],
            "query": self._resolved_query(query),
            "items": self._round_robin_merge(source_items, limit=limit),
            "errors": errors,
        }

    def fetch_skill_markdown(self, source_id: str, remote_url: str) -> dict[str, str]:
        source = self._pick_source(source_id)
        if source is None:
            raise ValueError("marketplace source not found")
        if source.kind in {"skillsmp", "github"}:
            raw_url = self._github_source_to_raw(remote_url, source)
            response = self._transport.request("GET", raw_url, headers=self._headers(source))
            filename = self._filename_from_url(raw_url, fallback="SKILL.md")
            return {
                "filename": filename,
                "markdown": response.text,
                "raw_url": raw_url,
            }
        if source.kind == "clawhub":
            slug = self._clawhub_slug_from_url(remote_url)
            raw_url = f"{source.base_url.rstrip('/')}/api/v1/skills/{slug}/file?path=SKILL.md"
            response = self._transport.request("GET", raw_url, headers=self._headers(source))
            return {
                "filename": "SKILL.md",
                "markdown": response.text,
                "raw_url": raw_url,
            }
        raise ValueError(f"unsupported marketplace source kind: {source.kind}")

    async def afetch_skill_markdown(self, source_id: str, remote_url: str) -> dict[str, str]:
        source = self._pick_source(source_id)
        if source is None:
            raise ValueError("marketplace source not found")
        if source.kind in {"skillsmp", "github"}:
            raw_url = await self._agithub_source_to_raw(remote_url, source)
            response = await self._async_transport.request("GET", raw_url, headers=self._headers(source))
            filename = self._filename_from_url(raw_url, fallback="SKILL.md")
            return {
                "filename": filename,
                "markdown": response.text,
                "raw_url": raw_url,
            }
        if source.kind == "clawhub":
            slug = self._clawhub_slug_from_url(remote_url)
            raw_url = f"{source.base_url.rstrip('/')}/api/v1/skills/{slug}/file?path=SKILL.md"
            response = await self._async_transport.request("GET", raw_url, headers=self._headers(source))
            return {
                "filename": "SKILL.md",
                "markdown": response.text,
                "raw_url": raw_url,
            }
        raise ValueError(f"unsupported marketplace source kind: {source.kind}")

    def prepare_skill_bundle(self, source_id: str, remote_url: str, destination: str | Path) -> dict[str, str]:
        source = self._pick_source(source_id)
        if source is None:
            raise ValueError("marketplace source not found")
        target_dir = Path(destination).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        if source.kind in {"skillsmp", "github"}:
            return self._prepare_github_bundle(source, remote_url, target_dir)
        if source.kind == "clawhub":
            return self._prepare_clawhub_bundle(source, remote_url, target_dir)
        raise ValueError(f"unsupported marketplace source kind: {source.kind}")

    def _selected_sources(self, source_id: str) -> list[MarketplaceSourceConfig]:
        target = str(source_id or "").strip()
        if target:
            source = self._pick_source(target)
            return [source] if source is not None else []
        return [source for source in self.config.sources if source.enabled]

    def _pick_source(self, source_id: str) -> MarketplaceSourceConfig | None:
        target = str(source_id or "").strip()
        for source in self.config.sources:
            if not source.enabled:
                continue
            if not target or source.source_id == target:
                return source
        return None

    def _search_source(
        self,
        source: MarketplaceSourceConfig,
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, object]]:
        if source.kind == "skillsmp":
            return self._search_skillsmp(source, query=query, limit=limit)
        if source.kind == "clawhub":
            return self._search_clawhub(source, query=query, limit=limit)
        if source.kind == "github":
            return self._search_github(source, query=query, limit=limit)
        raise ValueError(f"unsupported marketplace source kind: {source.kind}")

    async def _asearch_source(
        self,
        source: MarketplaceSourceConfig,
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, object]]:
        if source.kind == "skillsmp":
            return await self._asearch_skillsmp(source, query=query, limit=limit)
        return await asyncio.to_thread(self._search_source, source, query=query, limit=limit)

    def _search_skillsmp(
        self,
        source: MarketplaceSourceConfig,
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, object]]:
        params = {
            "q": self._resolved_query(query),
            "limit": max(1, min(100, int(limit or source.max_results))),
            "sortBy": "recent",
        }
        response = self._transport.request(
            "GET",
            f"{source.base_url.rstrip('/')}{source.search_path}?{urlencode(params)}",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        raw_items = payload.get("data", {}).get("skills", [])
        return [
            self._skill_from_skillsmp_payload(source, item).as_dict()
            for item in raw_items
            if isinstance(item, dict)
        ]

    async def _asearch_skillsmp(
        self,
        source: MarketplaceSourceConfig,
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, object]]:
        params = {
            "q": self._resolved_query(query),
            "limit": max(1, min(100, int(limit or source.max_results))),
            "sortBy": "recent",
        }
        response = await self._async_transport.request(
            "GET",
            f"{source.base_url.rstrip('/')}{source.search_path}?{urlencode(params)}",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        raw_items = payload.get("data", {}).get("skills", [])
        return [
            self._skill_from_skillsmp_payload(source, item).as_dict()
            for item in raw_items
            if isinstance(item, dict)
        ]

    def _search_clawhub(
        self,
        source: MarketplaceSourceConfig,
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, object]]:
        params = {
            "q": self._resolved_query(query),
            "limit": max(1, min(50, int(limit or source.max_results))),
        }
        response = self._transport.request(
            "GET",
            f"{source.base_url.rstrip('/')}{source.search_path}?{urlencode(params)}",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        raw_items = payload.get("results", [])
        return [
            self._skill_from_clawhub_payload(source, item).as_dict()
            for item in raw_items
            if isinstance(item, dict)
        ]

    def _search_github(
        self,
        source: MarketplaceSourceConfig,
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, object]]:
        params = {
            "q": f'{self._resolved_query(query)} skill',
            "per_page": max(5, min(20, max(limit, source.max_results))),
            "sort": "updated",
            "order": "desc",
        }
        response = self._transport.request(
            "GET",
            f"{source.base_url.rstrip('/')}{source.search_path}?{urlencode(params)}",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        raw_items = payload.get("items", [])
        items: list[dict[str, object]] = []
        for candidate in raw_items:
            if not isinstance(candidate, dict):
                continue
            full_name = str(candidate.get("full_name") or "").strip()
            if not full_name:
                continue
            if not self._github_repo_looks_like_skill_bundle(full_name, source):
                continue
            items.append(self._skill_from_github_payload(source, candidate).as_dict())
            if len(items) >= limit:
                break
        return items

    def _prepare_github_bundle(
        self,
        source: MarketplaceSourceConfig,
        remote_url: str,
        destination: Path,
    ) -> dict[str, str]:
        info = self._parse_github_source(remote_url, source)
        archive_url = f"https://api.github.com/repos/{info['owner']}/{info['repo']}/zipball/{info['ref']}"
        response = self._transport.request("GET", archive_url, headers=self._headers(source))
        extract_root = destination / f"{info['repo']}-bundle"
        extract_root.mkdir(parents=True, exist_ok=True)
        archive_root = self._extract_zip_bytes(response.content, extract_root)
        selected = archive_root
        if info["path"]:
            selected = archive_root / str(info["path"])
            if info["is_file"]:
                selected = selected.parent
        if not selected.exists():
            raise ValueError(f"selected GitHub bundle path does not exist: {info['path']}")
        return {
            "path": str(selected.resolve()),
            "source_url": str(remote_url or "").strip(),
            "bundle_url": archive_url,
            "page_url": f"https://github.com/{info['owner']}/{info['repo']}",
        }

    def _prepare_clawhub_bundle(
        self,
        source: MarketplaceSourceConfig,
        remote_url: str,
        destination: Path,
    ) -> dict[str, str]:
        slug = self._clawhub_slug_from_url(remote_url)
        detail_url = f"{source.base_url.rstrip('/')}/api/v1/skills/{slug}"
        detail_response = self._transport.request("GET", detail_url, headers=self._headers(source))
        detail_payload = json.loads(detail_response.text)
        owner = str(detail_payload.get("owner", {}).get("handle") or "").strip()
        page_url = f"{source.base_url.rstrip('/')}/{owner}/{slug}" if owner else f"{source.base_url.rstrip('/')}/skills/{slug}"
        page_response = self._transport.request("GET", page_url, headers=self._headers(source))
        download_url = self._extract_clawhub_download_url(page_response.text, base_url=source.base_url)
        bundle_response = self._transport.request("GET", download_url, headers=self._headers(source))
        filename = self._filename_from_headers(
            bundle_response.headers,
            fallback=f"{slug}.zip",
        )
        archive_path = destination / filename
        archive_path.write_bytes(bundle_response.content)
        return {
            "path": str(archive_path.resolve()),
            "source_url": str(remote_url or "").strip() or slug,
            "bundle_url": download_url,
            "page_url": page_url,
        }

    def _github_repo_looks_like_skill_bundle(self, full_name: str, source: MarketplaceSourceConfig) -> bool:
        response = self._transport.request(
            "GET",
            f"https://api.github.com/repos/{full_name}/contents",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        if not isinstance(payload, list):
            return False
        names = {str(item.get("name") or "") for item in payload if isinstance(item, dict)}
        return any(name in names for name in _GITHUB_BUNDLE_MARKERS)

    @staticmethod
    def _round_robin_merge(groups: list[list[dict[str, object]]], *, limit: int) -> list[dict[str, object]]:
        remaining = [list(group) for group in groups if group]
        merged: list[dict[str, object]] = []
        while remaining and len(merged) < limit:
            next_groups: list[list[dict[str, object]]] = []
            for group in remaining:
                if len(merged) >= limit:
                    break
                if group:
                    merged.append(group.pop(0))
                if group:
                    next_groups.append(group)
            remaining = next_groups
        return merged

    def _parse_github_source(self, github_url: str, source: MarketplaceSourceConfig) -> dict[str, object]:
        parsed = urlparse(str(github_url or "").strip())
        host = parsed.netloc.lower()
        if host == "raw.githubusercontent.com":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) < 4:
                raise ValueError("raw github URL must point to a repository path")
            owner, repo, ref, *rest = parts
            path = "/".join(rest)
            if not path:
                raise ValueError("raw github URL must include a file path")
            return {
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "path": path,
                "is_file": True,
            }
        if host != "github.com":
            raise ValueError("only github.com skill sources are supported")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("github repository URL is required")
        owner, repo, *rest = parts
        if not rest:
            ref = self._github_default_branch(owner, repo, source)
            return {
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "path": "",
                "is_file": False,
            }
        if len(rest) >= 2 and rest[0] == "tree":
            return {
                "owner": owner,
                "repo": repo,
                "ref": rest[1],
                "path": "/".join(rest[2:]),
                "is_file": False,
            }
        if len(rest) >= 2 and rest[0] == "blob":
            path = "/".join(rest[2:])
            if not path:
                raise ValueError("github blob URL must point to a file")
            return {
                "owner": owner,
                "repo": repo,
                "ref": rest[1],
                "path": path,
                "is_file": True,
            }
        raise ValueError("unsupported github skill URL; use repository root, tree directory, or SKILL.md blob URL")

    def _github_source_to_raw(self, github_url: str, source: MarketplaceSourceConfig) -> str:
        info = self._parse_github_source(github_url, source)
        path = str(info["path"] or "").strip()
        if not path:
            return f"https://raw.githubusercontent.com/{info['owner']}/{info['repo']}/{info['ref']}/SKILL.md"
        if not bool(info["is_file"]):
            path = f"{path.rstrip('/')}/SKILL.md"
        return f"https://raw.githubusercontent.com/{info['owner']}/{info['repo']}/{info['ref']}/{path}"

    async def _agithub_source_to_raw(self, github_url: str, source: MarketplaceSourceConfig) -> str:
        parsed = urlparse(str(github_url or "").strip())
        if parsed.netloc.lower() == "github.com":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2:
                owner, repo = parts
                branch = await self._agithub_default_branch(owner, repo, source)
                return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"
        return self._github_source_to_raw(github_url, source)

    @staticmethod
    def _normalize_raw_github_url(parsed: Any) -> str:
        path = str(parsed.path or "").strip("/")
        if not path.endswith("SKILL.md"):
            raise ValueError("raw github URL must point to SKILL.md")
        return f"https://raw.githubusercontent.com/{path}"

    def _github_default_branch(self, owner: str, repo: str, source: MarketplaceSourceConfig) -> str:
        response = self._transport.request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        branch = str(payload.get("default_branch") or "").strip()
        return branch or "main"

    async def _agithub_default_branch(self, owner: str, repo: str, source: MarketplaceSourceConfig) -> str:
        response = await self._async_transport.request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        branch = str(payload.get("default_branch") or "").strip()
        return branch or "main"

    @staticmethod
    def _clawhub_slug_from_url(remote_url: str) -> str:
        raw = str(remote_url or "").strip().strip("/")
        if not raw:
            raise ValueError("clawhub skill URL is required")
        parsed = urlparse(raw if "://" in raw else f"https://clawhub.ai/{raw}")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("clawhub skill slug is required")
        return parts[-1]

    @staticmethod
    def _extract_clawhub_download_url(page_html: str, *, base_url: str) -> str:
        match = _CLAWHUB_DOWNLOAD_RE.search(str(page_html or ""))
        if match is None:
            raise ValueError("failed to locate ClawHub bundle download URL")
        return urljoin(base_url, html.unescape(match.group(1)))

    @staticmethod
    def _extract_zip_bytes(payload: bytes, destination: Path) -> Path:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                target = MarketplaceClient._safe_archive_target(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
        directories = [item for item in destination.iterdir() if item.is_dir()]
        files = [item for item in destination.iterdir() if item.is_file()]
        if len(directories) == 1 and not files:
            return directories[0]
        return destination

    @staticmethod
    def _safe_archive_target(root: Path, member_name: str) -> Path:
        target = (root / member_name).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"archive member escapes destination: {member_name}") from exc
        return target

    @staticmethod
    def _filename_from_headers(headers: dict[str, str], *, fallback: str) -> str:
        content_disposition = str(headers.get("content-disposition") or headers.get("Content-Disposition") or "")
        match = _FILENAME_RE.search(content_disposition)
        if match is None:
            return fallback
        return Path(match.group(1)).name or fallback

    @staticmethod
    def _filename_from_url(raw_url: str, *, fallback: str) -> str:
        name = Path(urlparse(raw_url).path).name
        return name or fallback

    def _resolved_query(self, query: str) -> str:
        return str(query or self.config.default_query or "").strip()

    @staticmethod
    def _source_to_dict(source: MarketplaceSourceConfig) -> dict[str, object]:
        return {
            "id": source.source_id,
            "name": source.name,
            "kind": source.kind,
            "enabled": source.enabled,
            "base_url": source.base_url,
            "browse_url": source.browse_url,
            "max_results": source.max_results,
        }

    @staticmethod
    def _skill_from_skillsmp_payload(source: MarketplaceSourceConfig, payload: dict[str, Any]) -> MarketplaceSkill:
        github_url = str(payload.get("githubUrl") or "").strip()
        skill_url = str(payload.get("skillUrl") or "").strip()
        return MarketplaceSkill(
            source_id=source.source_id,
            source_name=source.name,
            skill_id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            author=str(payload.get("author") or ""),
            description=str(payload.get("description") or ""),
            github_url=github_url,
            skill_url=skill_url,
            install_url=github_url or skill_url,
            stars=int(payload.get("stars", 0) or 0),
            updated_at=str(payload.get("updatedAt") or ""),
        )

    @staticmethod
    def _skill_from_clawhub_payload(source: MarketplaceSourceConfig, payload: dict[str, Any]) -> MarketplaceSkill:
        slug = str(payload.get("slug") or "").strip()
        return MarketplaceSkill(
            source_id=source.source_id,
            source_name=source.name,
            skill_id=slug,
            name=str(payload.get("displayName") or slug),
            author="",
            description=str(payload.get("summary") or ""),
            github_url="",
            skill_url=f"{source.base_url.rstrip('/')}/skills/{slug}",
            install_url=f"{source.base_url.rstrip('/')}/skills/{slug}",
            updated_at=str(payload.get("updatedAt") or ""),
            score=float(payload.get("score")) if payload.get("score") is not None else None,
        )

    @staticmethod
    def _skill_from_github_payload(source: MarketplaceSourceConfig, payload: dict[str, Any]) -> MarketplaceSkill:
        owner = payload.get("owner") or {}
        return MarketplaceSkill(
            source_id=source.source_id,
            source_name=source.name,
            skill_id=str(payload.get("full_name") or "").replace("/", "-"),
            name=str(payload.get("name") or ""),
            author=str(owner.get("login") or ""),
            description=str(payload.get("description") or ""),
            github_url=str(payload.get("html_url") or ""),
            skill_url=str(payload.get("html_url") or ""),
            install_url=str(payload.get("html_url") or ""),
            stars=int(payload.get("stargazers_count", 0) or 0),
            updated_at=str(payload.get("updated_at") or ""),
        )

    @staticmethod
    def _headers(source: MarketplaceSourceConfig) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "waifu-standalone/0.1 marketplace",
        }
        if source.api_key:
            headers["Authorization"] = f"Bearer {source.api_key}"
        return headers

    def close(self) -> None:
        self._transport.close()
        self._async_transport.close()
