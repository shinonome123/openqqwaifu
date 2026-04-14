from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from ..config import MarketplaceConfig, MarketplaceSourceConfig


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
        request = Request(
            f"{source.base_url.rstrip('/')}{source.search_path}?{urlencode(params)}",
            headers=self._headers(source),
        )
        with urlopen(request, timeout=source.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
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
        raw_url = self._github_tree_to_raw(github_url)
        request = Request(raw_url, headers=self._headers(source))
        with urlopen(request, timeout=source.timeout_seconds) as response:
            markdown = response.read().decode("utf-8")
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

    @staticmethod
    def _github_tree_to_raw(github_url: str) -> str:
        parsed = urlparse(github_url)
        if parsed.netloc != "github.com":
            raise ValueError("only github.com skill sources are supported")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 4 or parts[2] != "tree":
            raise ValueError("github tree URL is required")
        owner, repo, _, branch, *rest = parts
        path = "/".join(rest)
        if not path:
            raise ValueError("skill directory path is missing")
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}/SKILL.md"
