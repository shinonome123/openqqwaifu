from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from ..config import AppConfig
from ..models import InboundEvent


@dataclass(slots=True)
class SearchResult:
    title: str
    snippet: str
    url: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
        }


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
        "新闻",
        "价格",
        "最新",
        "今天",
        "实时",
        "热搜",
        "汇率",
        "天气",
        "股价",
        "比分",
        "票房",
        "热度",
        "多少美元",
        "多少人民币",
    )

    _CACHE_MAX_SIZE = 128
    _CACHE_TTL_SECONDS = 300.0
    _CACHE_FAILURE_TTL_SECONDS = 60.0
    _DEFAULT_EMPTY_SUMMARY = "这类问题通常需要联网确认，但这次没有拿到可靠结果。"
    _USER_AGENT = "Mozilla/5.0 (compatible; openqqwaifu/0.1; +https://github.com/shinonome123/openqqwaifu)"
    _RESULT_LINK_PATTERN = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    _RESULT_SNIPPET_PATTERN = re.compile(
        r'<(?:a|div)[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</(?:a|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    _HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

    def __init__(
        self,
        config: AppConfig,
        fetcher: Callable[[str], list[SearchResult]] | None = None,
    ):
        self.config = config
        self._fetcher = fetcher or self._duckduckgo_search
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

    def search_query(self, query: str, *, reason: str = "manual") -> SearchContext:
        query = self._normalize_query(query)
        if not query:
            return SearchContext()
        cached = self._cache.get(query)
        if cached is not None:
            is_failure = not cached.results
            ttl = self._CACHE_FAILURE_TTL_SECONDS if is_failure else self._CACHE_TTL_SECONDS
            if time.time() - cached.fetched_at < ttl:
                return cached
            del self._cache[query]
        results = self._safe_fetch(query)
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
        summary = f"{first.title}：{first.snippet}".strip("：")
        context = SearchContext(
            query=query,
            summary=summary,
            results=results[: max(1, self.config.search_result_limit)],
            fetched_at=time.time(),
            reason=reason,
        )
        self._cache_put(query, context)
        return context

    def build_hint(self, event: InboundEvent) -> str:
        return self.build_context(event).summary

    def cache_size(self) -> int:
        return len(self._cache)

    def _cache_put(self, query: str, context: SearchContext) -> None:
        if len(self._cache) >= self._CACHE_MAX_SIZE:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].fetched_at)
            del self._cache[oldest_key]
        self._cache[query] = context

    def _safe_fetch(self, query: str) -> list[SearchResult]:
        try:
            return self._fetcher(query)
        except Exception:
            return []

    def _duckduckgo_search(self, query: str) -> list[SearchResult]:
        results = self._duckduckgo_instant_answer(query)
        if results:
            return results[: max(1, self.config.search_result_limit)]
        return self._duckduckgo_html_search(query)[: max(1, self.config.search_result_limit)]

    def _duckduckgo_instant_answer(self, query: str) -> list[SearchResult]:
        params = urlencode(
            {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            }
        )
        request = Request(
            f"https://api.duckduckgo.com/?{params}",
            headers={
                "User-Agent": self._USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=self.config.search_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
        results: list[SearchResult] = []
        heading = str(payload.get("Heading", "") or "").strip()
        answer = str(payload.get("Answer", "") or "").strip()
        abstract = str(payload.get("AbstractText", "") or "").strip()
        abstract_url = str(payload.get("AbstractURL", "") or "").strip()
        if answer or abstract:
            results.append(
                SearchResult(
                    title=heading or query,
                    snippet=answer or abstract,
                    url=abstract_url,
                )
            )
        related = payload.get("RelatedTopics", [])
        for item in self._flatten_related_topics(related):
            title, snippet = self._split_topic_text(str(item.get("Text", "") or "").strip())
            url = str(item.get("FirstURL", "") or "").strip()
            if not title and not snippet:
                continue
            results.append(
                SearchResult(
                    title=title or query,
                    snippet=snippet or title,
                    url=url,
                )
            )
            if len(results) >= max(1, self.config.search_result_limit):
                break
        return self._dedupe_results(results)

    def _duckduckgo_html_search(self, query: str) -> list[SearchResult]:
        params = urlencode({"q": query})
        request = Request(
            f"https://html.duckduckgo.com/html/?{params}",
            headers={
                "User-Agent": self._USER_AGENT,
                "Accept": "text/html",
            },
        )
        with urlopen(request, timeout=self.config.search_timeout_seconds) as response:
            body = response.read().decode("utf-8", "ignore")
        return self._parse_duckduckgo_html_results(body)

    def _parse_duckduckgo_html_results(self, body: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        matches = list(self._RESULT_LINK_PATTERN.finditer(body))
        for index, match in enumerate(matches):
            raw_title = match.group("title")
            title = self._clean_html(raw_title)
            if not title:
                continue
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            snippet_match = self._RESULT_SNIPPET_PATTERN.search(body, match.end(), next_start)
            snippet = self._clean_html(snippet_match.group("snippet")) if snippet_match else ""
            url = self._normalize_result_url(match.group("url"))
            if not snippet:
                snippet = title
            results.append(SearchResult(title=title, snippet=snippet, url=url))
            if len(results) >= max(1, self.config.search_result_limit):
                break
        return self._dedupe_results(results)

    @classmethod
    def _clean_html(cls, raw: str) -> str:
        text = html.unescape(str(raw or ""))
        text = cls._HTML_TAG_PATTERN.sub(" ", text)
        return " ".join(text.split()).strip()

    @staticmethod
    def _normalize_result_url(raw_url: str) -> str:
        url = html.unescape(str(raw_url or "").strip())
        parsed = urlsplit(url)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [""])
            if target and target[0]:
                return target[0]
        return url

    @staticmethod
    def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
        unique: list[SearchResult] = []
        seen_keys: set[tuple[str, str]] = set()
        for result in results:
            key = (result.title, result.url)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique.append(result)
        return unique

    def _flatten_related_topics(self, items: object) -> list[dict[str, object]]:
        if not isinstance(items, list):
            return []
        flat: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            nested = item.get("Topics")
            if isinstance(nested, list):
                flat.extend(self._flatten_related_topics(nested))
                continue
            flat.append(item)
        return flat

    @staticmethod
    def _split_topic_text(text: str) -> tuple[str, str]:
        if " - " in text:
            title, snippet = text.split(" - ", 1)
            return title.strip(), snippet.strip()
        return text.strip(), text.strip()

    @staticmethod
    def _normalize_query(text: str) -> str:
        normalized = " ".join(str(text or "").strip().split())
        if len(normalized) <= 120:
            return normalized
        return normalized[:120].rstrip()
