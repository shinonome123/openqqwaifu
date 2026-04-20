from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from ..config import AppConfig
from ..http_transport import AsyncHttpTransport, SyncHttpTransport
from ..observability import TransportMetricsScope


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


class SearchClient(Protocol):
    def fetch(self, query: str) -> list[SearchResult]:
        ...

    async def afetch(self, query: str) -> list[SearchResult]:
        ...


class DuckDuckGoSearchClient:
    _USER_AGENT = "Mozilla/5.0 (compatible; openqqwaifu/0.1; +https://github.com/shinonome123/openqqwaifu)"
    _FRESHNESS_HINTS = (
        "最新",
        "最近",
        "刚",
        "刚刚",
        "今天",
        "今日",
        "昨天",
        "前天",
        "新闻",
        "罚款",
        "被罚",
        "处罚",
        "罚单",
        "监管",
        "公告",
        "通报",
        "立案",
        "垄断",
        "反垄断",
    )
    _DATE_PATTERN = re.compile(r"(20\d{2}年?|(?:1[0-2]|0?[1-9])月(?:[12]\d|3[01]|0?[1-9])日)")
    _RESULT_LINK_PATTERN = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    _RESULT_SNIPPET_PATTERN = re.compile(
        r'<(?:a|div)[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</(?:a|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    _HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

    def __init__(self, config: AppConfig):
        self.config = config
        scope = TransportMetricsScope(kind="search", target="duckduckgo")
        self._transport = SyncHttpTransport(
            timeout_seconds=self.config.search_timeout_seconds,
            metrics_scope=scope,
        )
        self._async_transport = AsyncHttpTransport(
            timeout_seconds=self.config.search_timeout_seconds,
            metrics_scope=scope,
        )

    def fetch(self, query: str) -> list[SearchResult]:
        if self._prefer_html_search(query):
            results = self._duckduckgo_html_search(query)
            if results:
                return results[: max(1, self.config.search_result_limit)]
        results = self._duckduckgo_instant_answer(query)
        if results:
            return results[: max(1, self.config.search_result_limit)]
        return self._duckduckgo_html_search(query)[: max(1, self.config.search_result_limit)]

    async def afetch(self, query: str) -> list[SearchResult]:
        if self._prefer_html_search(query):
            results = await self._aduckduckgo_html_search(query)
            if results:
                return results[: max(1, self.config.search_result_limit)]
        results = await self._aduckduckgo_instant_answer(query)
        if results:
            return results[: max(1, self.config.search_result_limit)]
        return (await self._aduckduckgo_html_search(query))[: max(1, self.config.search_result_limit)]

    def _duckduckgo_instant_answer(self, query: str) -> list[SearchResult]:
        params = urlencode(
            {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            }
        )
        response = self._transport.request(
            "GET",
            f"https://api.duckduckgo.com/?{params}",
            headers={
                "User-Agent": self._USER_AGENT,
                "Accept": "application/json",
            },
        )
        payload = json.loads(response.text)
        return self._parse_instant_answer_payload(payload, query)

    async def _aduckduckgo_instant_answer(self, query: str) -> list[SearchResult]:
        params = urlencode(
            {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            }
        )
        response = await self._async_transport.request(
            "GET",
            f"https://api.duckduckgo.com/?{params}",
            headers={
                "User-Agent": self._USER_AGENT,
                "Accept": "application/json",
            },
        )
        payload = json.loads(response.text)
        return self._parse_instant_answer_payload(payload, query)

    def _parse_instant_answer_payload(self, payload: dict[str, object], query: str) -> list[SearchResult]:
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
        response = self._transport.request(
            "GET",
            f"https://html.duckduckgo.com/html/?{params}",
            headers={
                "User-Agent": self._USER_AGENT,
                "Accept": "text/html",
            },
        )
        body = response.text
        return self.parse_html_results(body, limit=self.config.search_result_limit)

    async def _aduckduckgo_html_search(self, query: str) -> list[SearchResult]:
        params = urlencode({"q": query})
        response = await self._async_transport.request(
            "GET",
            f"https://html.duckduckgo.com/html/?{params}",
            headers={
                "User-Agent": self._USER_AGENT,
                "Accept": "text/html",
            },
        )
        body = response.text
        return self.parse_html_results(body, limit=self.config.search_result_limit)

    @classmethod
    def parse_html_results(cls, body: str, *, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        matches = list(cls._RESULT_LINK_PATTERN.finditer(body))
        for index, match in enumerate(matches):
            raw_title = match.group("title")
            title = cls._clean_html(raw_title)
            if not title:
                continue
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            snippet_match = cls._RESULT_SNIPPET_PATTERN.search(body, match.end(), next_start)
            snippet = cls._clean_html(snippet_match.group("snippet")) if snippet_match else ""
            url = cls._normalize_result_url(match.group("url"))
            if not snippet:
                snippet = title
            results.append(SearchResult(title=title, snippet=snippet, url=url))
            if len(results) >= max(1, int(limit)):
                break
        return cls._dedupe_results(results)

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

    @classmethod
    def _prefer_html_search(cls, query: str) -> bool:
        normalized = " ".join(str(query or "").strip().lower().split())
        if not normalized:
            return False
        return any(hint in normalized for hint in cls._FRESHNESS_HINTS) or bool(
            cls._DATE_PATTERN.search(normalized)
        )

    def close(self) -> None:
        self._transport.close()
        self._async_transport.close()
