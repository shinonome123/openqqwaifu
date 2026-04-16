from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import AppConfig
from ..models import InboundEvent
from .search_client import DuckDuckGoSearchClient, SearchClient, SearchResult


@dataclass(slots=True)
class SearchContext:
    query: str = ""
    summary: str = ""
    results: list[SearchResult] = field(default_factory=list)
    fetched_at: float = 0.0
    reason: str = ""

    @property
    def active(self) -> bool:
        return bool(self.query and self.results)

    def to_prompt_block(self) -> str:
        if not self.active:
            return ""
        lines = [f"[Web Search]\nQuery: {self.query}"]
        for result in self.results:
            lines.append(f"- {result.title}: {result.snippet}")
            if result.url:
                lines.append(f"  URL: {result.url}")
        return "\n".join(lines).strip()

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "summary": self.summary,
            "results": [result.as_dict() for result in self.results],
            "fetched_at": self.fetched_at,
            "reason": self.reason,
        }


class SearchDecider:
    """Keyword-triggered web lookup with a DuckDuckGo-backed fallback scraper."""

    _KEYWORDS = (
        "\u65b0\u95fb",
        "\u4ef7\u683c",
        "\u6700\u65b0",
        "\u4eca\u5929",
        "\u5b9e\u65f6",
        "\u70ed\u641c",
        "\u6c47\u7387",
        "\u5929\u6c14",
        "\u80a1\u4ef7",
        "\u6bd4\u5206",
        "\u7968\u623f",
        "\u70ed\u5ea6",
        "\u591a\u5c11\u7f8e\u5143",
        "\u591a\u5c11\u4eba\u6c11\u5e01",
        "news",
        "price",
        "today",
        "weather",
        "score",
        "stock",
    )
    _CACHE_MAX_SIZE = 128
    _CACHE_TTL_SECONDS = 300.0
    _CACHE_FAILURE_TTL_SECONDS = 60.0
    _DEFAULT_EMPTY_SUMMARY = "\u8fd9\u7c7b\u95ee\u9898\u901a\u5e38\u9700\u8981\u8054\u7f51\u786e\u8ba4\uff0c\u4f46\u8fd9\u6b21\u6ca1\u6709\u62ff\u5230\u53ef\u9760\u7ed3\u679c\u3002"

    def __init__(
        self,
        config: AppConfig,
        fetcher: callable | None = None,
        search_client: SearchClient | None = None,
    ):
        self.config = config
        if fetcher is not None:
            self._search_client = _CallableSearchClient(fetcher)
        else:
            self._search_client = search_client or DuckDuckGoSearchClient(config)
        self._cache: dict[str, SearchContext] = {}

    def should_search(self, event: InboundEvent) -> bool:
        if not self.config.search_enabled:
            return False
        text = event.command_text(self.config.bot_account_id) or event.plain_text
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        return any(keyword in normalized for keyword in self._KEYWORDS)

    def build_context(self, event: InboundEvent) -> SearchContext:
        if not self.should_search(event):
            return SearchContext()
        query = self._normalize_query(event.command_text(self.config.bot_account_id) or event.plain_text)
        return self.search_query(query, reason="keyword-hit")

    async def abuild_context(self, event: InboundEvent) -> SearchContext:
        if not self.should_search(event):
            return SearchContext()
        query = self._normalize_query(event.command_text(self.config.bot_account_id) or event.plain_text)
        return await self.asearch_query(query, reason="keyword-hit")

    def search_query(self, query: str, *, reason: str = "manual") -> SearchContext:
        query = self._normalize_query(query)
        if not query:
            return SearchContext()
        cached = self._get_cached(query)
        if cached is not None:
            return cached
        results = self._safe_fetch(query)
        return self._finalize_query(query, results, reason=reason)

    async def asearch_query(self, query: str, *, reason: str = "manual") -> SearchContext:
        query = self._normalize_query(query)
        if not query:
            return SearchContext()
        cached = self._get_cached(query)
        if cached is not None:
            return cached
        results = await self._asafe_fetch(query)
        return self._finalize_query(query, results, reason=reason)

    def build_hint(self, event: InboundEvent) -> str:
        return self.build_context(event).summary

    def cache_size(self) -> int:
        return len(self._cache)

    def _get_cached(self, query: str) -> SearchContext | None:
        cached = self._cache.get(query)
        if cached is None:
            return None
        is_failure = not cached.results
        ttl = self._CACHE_FAILURE_TTL_SECONDS if is_failure else self._CACHE_TTL_SECONDS
        if time.time() - cached.fetched_at < ttl:
            return cached
        del self._cache[query]
        return None

    def _finalize_query(self, query: str, results: list[SearchResult], *, reason: str) -> SearchContext:
        if not results:
            context = SearchContext(
                query=query,
                summary=self._DEFAULT_EMPTY_SUMMARY,
                results=[],
                fetched_at=time.time(),
                reason=f"{reason}:no-results",
            )
            self._cache_put(query, context)
            return context
        first = results[0]
        context = SearchContext(
            query=query,
            summary=self._summarize_result(first),
            results=results[: max(1, self.config.search_result_limit)],
            fetched_at=time.time(),
            reason=reason,
        )
        self._cache_put(query, context)
        return context

    def _cache_put(self, query: str, context: SearchContext) -> None:
        if len(self._cache) >= self._CACHE_MAX_SIZE:
            oldest_key = min(self._cache, key=lambda item: self._cache[item].fetched_at)
            del self._cache[oldest_key]
        self._cache[query] = context

    def _safe_fetch(self, query: str) -> list[SearchResult]:
        try:
            return self._search_client.fetch(query)
        except Exception:
            return []

    async def _asafe_fetch(self, query: str) -> list[SearchResult]:
        try:
            return await self._search_client.afetch(query)
        except Exception:
            return []

    def _parse_duckduckgo_html_results(self, body: str) -> list[SearchResult]:
        return DuckDuckGoSearchClient.parse_html_results(body, limit=self.config.search_result_limit)

    def close(self) -> None:
        close = getattr(self._search_client, "close", None)
        if callable(close):
            close()

    @property
    def _fetcher(self):  # type: ignore[no-untyped-def]
        return self._search_client.fetch

    @_fetcher.setter
    def _fetcher(self, fetcher):  # type: ignore[no-untyped-def]
        self._search_client = _CallableSearchClient(fetcher)

    @staticmethod
    def _summarize_result(result: SearchResult) -> str:
        title = str(result.title or "").strip()
        snippet = str(result.snippet or "").strip()
        if title and snippet:
            return f"{title}: {snippet}"
        return title or snippet

    @staticmethod
    def _normalize_query(text: str) -> str:
        normalized = " ".join(str(text or "").strip().split())
        if len(normalized) <= 120:
            return normalized
        return normalized[:120].rstrip()


class _CallableSearchClient:
    def __init__(self, fetcher):  # type: ignore[no-untyped-def]
        self._fetcher = fetcher

    def fetch(self, query: str) -> list[SearchResult]:
        return self._fetcher(query)

    async def afetch(self, query: str) -> list[SearchResult]:
        return self.fetch(query)
