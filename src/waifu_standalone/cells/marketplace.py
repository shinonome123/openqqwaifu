from __future__ import annotations
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from ..config import MarketplaceConfig, MarketplaceSourceConfig
from ..http_transport import AsyncHttpTransport, SyncHttpTransport


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

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "id": self.skill_id,
            "name": self.name,
            "author": self.author,
            "description": self.description,
            "github_url": self.github_url,
            "skill_url": self.skill_url,
            "stars": self.stars,
            "updated_at": self.updated_at,
        }


class MarketplaceClient:
    def __init__(self, config: MarketplaceConfig):
        self.config = config
        max_timeout = max(
            (float(source.timeout_seconds or 10.0) for source in self.config.sources),
            default=10.0,
        )
        self._transport = SyncHttpTransport(timeout_seconds=max_timeout)
        self._async_transport = AsyncHttpTransport(timeout_seconds=max_timeout)

    def describe(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "default_query": self.config.default_query,
            "sources": [self._source_to_dict(source) for source in self.config.sources],
        }

    def search(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        if not self.config.enabled:
            return {"enabled": False, "items": [], "query": query}
        source = self._pick_source(source_id)
        if source is None:
            return {"enabled": True, "items": [], "query": query, "error": "source_not_found"}
        params = {
            "q": str(query or self.config.default_query or "").strip(),
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
        items = [self._skill_from_payload(source.source_id, item).as_dict() for item in raw_items if isinstance(item, dict)]
        return {
            "enabled": True,
            "source": self._source_to_dict(source),
            "query": params["q"],
            "items": items,
        }

    async def asearch(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        if not self.config.enabled:
            return {"enabled": False, "items": [], "query": query}
        source = self._pick_source(source_id)
        if source is None:
            return {"enabled": True, "items": [], "query": query, "error": "source_not_found"}
        params = {
            "q": str(query or self.config.default_query or "").strip(),
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
        items = [self._skill_from_payload(source.source_id, item).as_dict() for item in raw_items if isinstance(item, dict)]
        return {
            "enabled": True,
            "source": self._source_to_dict(source),
            "query": params["q"],
            "items": items,
        }

    def fetch_skill_markdown(self, source_id: str, github_url: str) -> dict[str, str]:
        source = self._pick_source(source_id)
        if source is None:
            raise ValueError("marketplace source not found")
        raw_url = self._github_source_to_raw(github_url, source)
        response = self._transport.request("GET", raw_url, headers=self._headers(source))
        markdown = response.text
        filename = Path(urlparse(raw_url).path).name or "SKILL.md"
        return {
            "filename": filename,
            "markdown": markdown,
            "raw_url": raw_url,
        }

    async def afetch_skill_markdown(self, source_id: str, github_url: str) -> dict[str, str]:
        source = self._pick_source(source_id)
        if source is None:
            raise ValueError("marketplace source not found")
        raw_url = await self._agithub_source_to_raw(github_url, source)
        response = await self._async_transport.request("GET", raw_url, headers=self._headers(source))
        markdown = response.text
        filename = Path(urlparse(raw_url).path).name or "SKILL.md"
        return {
            "filename": filename,
            "markdown": markdown,
            "raw_url": raw_url,
        }

    def _pick_source(self, source_id: str) -> MarketplaceSourceConfig | None:
        target = str(source_id or "").strip()
        for source in self.config.sources:
            if not source.enabled:
                continue
            if not target or source.source_id == target:
                return source
        return None

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
    def _skill_from_payload(source_id: str, payload: dict[str, Any]) -> MarketplaceSkill:
        return MarketplaceSkill(
            source_id=source_id,
            skill_id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            author=str(payload.get("author") or ""),
            description=str(payload.get("description") or ""),
            github_url=str(payload.get("githubUrl") or ""),
            skill_url=str(payload.get("skillUrl") or ""),
            stars=int(payload.get("stars", 0) or 0),
            updated_at=str(payload.get("updatedAt") or ""),
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

    def _github_source_to_raw(self, github_url: str, source: MarketplaceSourceConfig) -> str:
        parsed = urlparse(str(github_url or "").strip())
        host = parsed.netloc.lower()
        if host == "raw.githubusercontent.com":
            return self._normalize_raw_github_url(parsed)
        if host != "github.com":
            raise ValueError("only github.com skill sources are supported")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("github repository URL is required")
        owner, repo, *rest = parts
        if not rest:
            branch = self._github_default_branch(owner, repo, source)
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"
        if len(rest) >= 2 and rest[0] == "tree":
            branch = rest[1]
            path = "/".join(rest[2:])
            if path:
                return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}/SKILL.md"
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"
        if len(rest) >= 2 and rest[0] == "blob":
            branch = rest[1]
            path = "/".join(rest[2:])
            if not path:
                raise ValueError("github blob URL must point to SKILL.md")
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        raise ValueError("unsupported github skill URL; use repository root, tree directory, or SKILL.md blob URL")

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

    async def _agithub_source_to_raw(self, github_url: str, source: MarketplaceSourceConfig) -> str:
        parsed = urlparse(str(github_url or "").strip())
        host = parsed.netloc.lower()
        if host == "raw.githubusercontent.com":
            return self._normalize_raw_github_url(parsed)
        if host != "github.com":
            raise ValueError("only github.com skill sources are supported")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("github repository URL is required")
        owner, repo, *rest = parts
        if not rest:
            branch = await self._agithub_default_branch(owner, repo, source)
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"
        if len(rest) >= 2 and rest[0] == "tree":
            branch = rest[1]
            path = "/".join(rest[2:])
            if path:
                return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}/SKILL.md"
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"
        if len(rest) >= 2 and rest[0] == "blob":
            branch = rest[1]
            path = "/".join(rest[2:])
            if not path:
                raise ValueError("github blob URL must point to SKILL.md")
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        raise ValueError("unsupported github skill URL; use repository root, tree directory, or SKILL.md blob URL")

    async def _agithub_default_branch(self, owner: str, repo: str, source: MarketplaceSourceConfig) -> str:
        response = await self._async_transport.request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=self._headers(source),
        )
        payload = json.loads(response.text)
        branch = str(payload.get("default_branch") or "").strip()
        return branch or "main"

    def close(self) -> None:
        self._transport.close()
        self._async_transport.close()
