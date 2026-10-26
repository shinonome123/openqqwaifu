from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.config import AppConfig
from waifu_standalone.gateways.inbound_dedup import InboundDeduplicator
from waifu_standalone.http_api import HttpApi
from waifu_standalone.models import InboundEvent, MessageSegment


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _event(text: str = "hello", *, message_id: str = "", sender_id: str = "783190298", group: str = "612475113"):
    return InboundEvent(
        launcher_id=group,
        launcher_type="group",
        sender_id=sender_id,
        sender_name="tester",
        segments=[MessageSegment(kind="text", text=text)],
        message_id=message_id,
        raw_message=text,
    )


class InboundDeduplicatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.dedup = InboundDeduplicator(id_ttl_seconds=600, content_window_seconds=10, clock=self.clock)

    def test_same_message_id_is_claimed_once_until_it_expires(self) -> None:
        self.assertIsNotNone(self.dedup.claim(_event(message_id="42")))
        self.clock.now += 300
        self.assertIsNone(self.dedup.claim(_event("edited text", message_id="42")))
        # The id is per chat: the same number in another group is a different message.
        self.assertIsNotNone(self.dedup.claim(_event(message_id="42", group="700000001")))
        self.clock.now += 301
        self.assertIsNotNone(self.dedup.claim(_event(message_id="42")))

    def test_without_message_id_same_sender_and_content_inside_window(self) -> None:
        self.assertIsNotNone(self.dedup.claim(_event("在吗")))
        self.assertIsNone(self.dedup.claim(_event("在吗")))
        self.assertIsNotNone(self.dedup.claim(_event("在吗", sender_id="783190299")))
        self.assertIsNotNone(self.dedup.claim(_event("在不在")))
        self.clock.now += 11
        self.assertIsNotNone(self.dedup.claim(_event("在吗")))

    def test_onebot_time_tells_a_retry_from_a_repeat(self) -> None:
        self.assertIsNotNone(self.dedup.claim(_event("1"), event_time=1761459300))
        self.clock.now += 60
        # A retry carries the original time and still matches after the content window.
        self.assertIsNone(self.dedup.claim(_event("1"), event_time=1761459300))
        # The same text sent again has a new time.
        self.assertIsNotNone(self.dedup.claim(_event("1"), event_time=1761459302))

    def test_release_lets_a_retry_through(self) -> None:
        key = self.dedup.claim(_event(message_id="7"))
        self.dedup.release(key)
        self.assertIsNotNone(self.dedup.claim(_event(message_id="7")))

    def test_entries_are_bounded(self) -> None:
        dedup = InboundDeduplicator(max_entries=2, clock=self.clock)
        for message_id in ("1", "2", "3"):
            dedup.claim(_event(message_id=message_id))
        # The oldest entry was evicted.
        self.assertIsNotNone(dedup.claim(_event(message_id="1")))
        self.assertIsNone(dedup.claim(_event(message_id="3")))


class _CountingService:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.config = AppConfig()
        self.events: list[InboundEvent] = []
        self.fail_first = fail_first

    def handle_event(self, event: InboundEvent):  # type: ignore[no-untyped-def]
        self.events.append(event)
        if self.fail_first and len(self.events) == 1:
            raise RuntimeError("model timed out")
        return None

    async def handle_event_async(self, event: InboundEvent):  # type: ignore[no-untyped-def]
        return self.handle_event(event)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 612475113,
        "user_id": 783190298,
        "message_id": 1001,
        "sender": {"user_id": 783190298, "nickname": "tester"},
        "raw_message": "hello",
    }
    payload.update(overrides)
    return payload


class HttpApiDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_callback_retry_is_ignored(self) -> None:
        service = _CountingService()
        api = HttpApi(service)  # type: ignore[arg-type]

        await api.handle_json_async(_payload())
        status, body = await api.handle_json_async(_payload())
        await api.handle_json_async(_payload(message_id=1002))

        self.assertEqual(body, {"status": "ignored", "reason": "duplicate message"})
        self.assertLess(status, 400)
        self.assertEqual(len(service.events), 2)
        self.assertIn("duplicate", api.prometheus_metrics())

    async def test_sync_callback_retry_is_ignored(self) -> None:
        service = _CountingService()
        api = HttpApi(service)  # type: ignore[arg-type]
        payload = _payload()
        payload.pop("message_id")

        api.handle_json(payload)
        _, body = api.handle_json(payload)

        self.assertEqual(body["reason"], "duplicate message")
        self.assertEqual(len(service.events), 1)

    async def test_failed_delivery_can_be_retried(self) -> None:
        service = _CountingService(fail_first=True)
        api = HttpApi(service)  # type: ignore[arg-type]

        status, _ = await api.handle_json_async(_payload())
        self.assertGreaterEqual(status, 500)
        await api.handle_json_async(_payload())

        self.assertEqual(len(service.events), 2)


if __name__ == "__main__":
    unittest.main()
