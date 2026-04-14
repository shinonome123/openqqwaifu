from __future__ import annotations

from ..models import InboundEvent


class SearchDecider:
    """Stub search system. Matches the role but keeps behavior deterministic for tests."""

    def should_search(self, event: InboundEvent) -> bool:
        text = event.plain_text
        keywords = ("新闻", "价格", "最新", "今天", "实时")
        return any(keyword in text for keyword in keywords)
