from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.systems.search_client import DuckDuckGoSearchClient
from waifu_standalone.systems.searching import SearchDecider, SearchResult


class SearchDeciderTests(unittest.TestCase):
    def test_keyword_hit_builds_search_context(self) -> None:
        decider = SearchDecider(
            AppConfig(search_enabled=True, search_result_limit=2),
            fetcher=lambda query: [
                SearchResult(title="晴朗天空", snippet=f"{query} 相关结果", url="https://example.com/sky"),
                SearchResult(title="天气摘要", snippet="阳光充足，能见度高", url="https://example.com/weather"),
            ],
        )
        event = InboundEvent(
            launcher_id="1",
            launcher_type="person",
            sender_id="2",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="今天北京天气怎么样")],
        )

        context = decider.build_context(event)

        self.assertTrue(context.active)
        self.assertEqual(context.query, "今天北京天气怎么样")
        self.assertEqual(len(context.results), 2)
        self.assertIn("晴朗天空", context.summary)
        self.assertIn("[Web Search]", context.to_prompt_block())

    def test_cache_reuses_same_query(self) -> None:
        calls: list[str] = []

        def fake_fetch(query: str) -> list[SearchResult]:
            calls.append(query)
            return [SearchResult(title="结果", snippet="摘要", url="https://example.com")]

        decider = SearchDecider(AppConfig(search_enabled=True), fetcher=fake_fetch)
        event = InboundEvent(
            launcher_id="1",
            launcher_type="person",
            sender_id="2",
            sender_name="tester",
            segments=[MessageSegment(kind="text", text="最新汇率多少")],
        )

        first = decider.build_context(event)
        second = decider.build_context(event)

        self.assertEqual(calls, ["最新汇率多少"])
        self.assertEqual(first.summary, second.summary)
        self.assertEqual(decider.cache_size(), 1)

    def test_async_build_context_uses_async_fetcher(self) -> None:
        class _AsyncOnlySearchClient:
            def fetch(self, query: str) -> list[SearchResult]:
                raise AssertionError("sync fetch should not run")

            async def afetch(self, query: str) -> list[SearchResult]:
                return [SearchResult(title="hangzhou weather", snippet="cloudy, 22C")]

        decider = SearchDecider(
            AppConfig(search_enabled=True, search_result_limit=2),
            search_client=_AsyncOnlySearchClient(),
        )
        context = asyncio.run(decider.asearch_query("hangzhou weather", reason="manual"))

        self.assertTrue(context.active)
        self.assertEqual(context.query, "hangzhou weather")
        self.assertIn("hangzhou weather", context.summary)

    def test_service_uses_search_summary_in_fallback_reply(self) -> None:
        service, _ = build_default_service(AppConfig(search_enabled=True))
        service.search._fetcher = lambda query: [
            SearchResult(title="北京天气", snippet="今天晴，最高温 26 度", url="https://example.com/weather")
        ]

        reply = service.handle_event(
            InboundEvent(
                launcher_id="783190298",
                launcher_type="person",
                sender_id="783190298",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text="今天北京天气怎么样")],
            )
        )

        session = service.memory.load("783190298", "person")
        last_search = session.metadata.get("last_search", {})

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("我刚查了一下", reply.text)
        self.assertIn("北京天气", reply.text)
        self.assertEqual(last_search.get("query"), "今天北京天气怎么样")

    def test_recent_regulatory_queries_trigger_search(self) -> None:
        decider = SearchDecider(AppConfig(search_enabled=True))
        queries = (
            "拼多多刚被罚款了",
            "拼多多最近被罚了",
            "拼多多刚被罚款是什么事",
            "拼多多2026年罚款",
        )

        for query in queries:
            event = InboundEvent(
                launcher_id="1",
                launcher_type="person",
                sender_id="2",
                sender_name="tester",
                segments=[MessageSegment(kind="text", text=query)],
            )
            self.assertTrue(decider.should_search(event), query)

    def test_recent_news_fetch_prefers_html_search(self) -> None:
        client = DuckDuckGoSearchClient(AppConfig(search_enabled=True, search_result_limit=2))
        calls: list[str] = []
        client._duckduckgo_instant_answer = lambda query: calls.append("instant") or [  # type: ignore[method-assign]
            SearchResult(title="旧摘要", snippet="旧内容", url="https://example.com/stale")
        ]
        client._duckduckgo_html_search = lambda query: calls.append("html") or [  # type: ignore[method-assign]
            SearchResult(title="新新闻", snippet="最新处罚结果", url="https://example.com/fresh")
        ]

        try:
            results = client.fetch("拼多多刚被罚款了")
        finally:
            client.close()

        self.assertEqual(calls, ["html"])
        self.assertEqual(results[0].title, "新新闻")

    def test_html_fallback_extracts_real_search_results(self) -> None:
        decider = SearchDecider(AppConfig(search_enabled=True, search_result_limit=2))
        html_body = """
        <div class="result">
          <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fxiaomi-stock">小米集团-W (01810)_最新价格_行情_走势图—东方财富网</a>
          <a class="result__snippet">提供小米集团-W (01810)实时行情数据。</a>
        </div>
        """

        results = decider._parse_duckduckgo_html_results(html_body)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "小米集团-W (01810)_最新价格_行情_走势图—东方财富网")
        self.assertEqual(results[0].snippet, "提供小米集团-W (01810)实时行情数据。")
        self.assertEqual(results[0].url, "https://example.com/xiaomi-stock")


if __name__ == "__main__":
    unittest.main()
