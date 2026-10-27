from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, RateLimitConfig
from waifu_standalone.infra.http_transport import TransportError
from waifu_standalone.infra.rate_limit import TokenBucketLimiter
from waifu_standalone.models import InboundEvent, MessageSegment, SessionMemory
from waifu_standalone.settings_admin.config_manager import ConfigManager
from waifu_standalone.skills.tool_registry import ToolInvocation


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


class TokenBucketLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.limiter = TokenBucketLimiter(clock=self.clock)

    def test_burst_then_wait_for_refill(self) -> None:
        rule = [("user:1", 2, 1 / 60)]  # 2 at once, one more per minute
        self.assertEqual(self.limiter.acquire(rule), 0.0)
        self.assertEqual(self.limiter.acquire(rule), 0.0)
        self.assertAlmostEqual(self.limiter.acquire(rule), 60.0)
        self.clock.now += 30
        self.assertAlmostEqual(self.limiter.acquire(rule), 30.0)
        self.clock.now += 30
        self.assertEqual(self.limiter.acquire(rule), 0.0)

    def test_tokens_are_taken_from_all_buckets_or_none(self) -> None:
        user_a = ("user:a", 5, 0.0)
        user_b = ("user:b", 5, 0.0)
        group = ("group:1", 1, 0.0)
        self.assertEqual(self.limiter.acquire([user_a, group]), 0.0)
        self.assertTrue(math.isinf(self.limiter.acquire([user_b, group])))
        # user:b kept its tokens because the group refused.
        for _ in range(5):
            self.assertEqual(self.limiter.acquire([user_b]), 0.0)
        self.assertGreater(self.limiter.acquire([user_b]), 0.0)

    def test_zero_capacity_turns_a_bucket_off(self) -> None:
        for _ in range(20):
            self.assertEqual(self.limiter.acquire([("user:1", 0, 0.0)]), 0.0)


def _event(sender_id: str = "783190298", *, group: str = "", text: str = "") -> InboundEvent:
    return InboundEvent(
        launcher_id=group or sender_id,
        launcher_type="group" if group else "person",
        sender_id=sender_id,
        sender_name="tester",
        segments=[MessageSegment(kind="text", text=text)],
    )


def _invocation(tool_id: str, event: InboundEvent, **arguments: object) -> ToolInvocation:
    return ToolInvocation(
        tool_id=tool_id,
        raw_args="",
        event=event,
        session=SessionMemory(launcher_id=event.launcher_id, launcher_type=event.launcher_type),
        address="你",
        arguments=dict(arguments),
    )


class _FakeImageClient:
    """Image generation is off by default and then refuses, so the tests plug in a provider."""

    enabled = True

    def generate(self, prompt: str) -> str:
        return f"generated://{prompt}"

    async def agenerate(self, prompt: str) -> str:
        return f"generated://{prompt}"


class DispatcherRateLimitTests(unittest.TestCase):
    def _service(self, tmpdir: str, **limits: object):  # type: ignore[no-untyped-def]
        service, outbound = build_default_service(
            AppConfig(data_root=tmpdir, rate_limits=RateLimitConfig(**limits))
        )
        service.config.image_generation.enabled = True
        service.generator._image_client = _FakeImageClient()
        return service, outbound

    def test_image_prefix_is_limited_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = self._service(tmpdir, image_user_burst=1, image_user_per_hour=2)

            first = service.handle_event(_event(text="生图 一只橘猫"))
            second = service.handle_event(_event(text="生图 一只黑猫"))
            other_user = service.handle_event(_event("783190299", text="生图 一只白猫"))

            assert first is not None and second is not None and other_user is not None
            self.assertEqual(first.images, ["generated://一只橘猫"])
            self.assertEqual(second.images, [])
            self.assertIn("生图刚才用得有点多", second.text)
            self.assertIn("30 分钟", second.text)
            self.assertEqual(other_user.images, ["generated://一只白猫"])

    def test_group_bucket_is_shared_by_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = self._service(tmpdir, image_group_burst=1, image_group_per_hour=0)
            dispatcher = service.dispatcher

            first = dispatcher.use_image_tool(_invocation("image", _event("1", group="612475113"), prompt="猫"))
            second = dispatcher.use_image_tool(_invocation("image", _event("2", group="612475113"), prompt="狗"))
            private = dispatcher.use_image_tool(_invocation("image", _event("2"), prompt="狗"))

            self.assertFalse(first.error)
            self.assertTrue(second.metadata.get("rate_limited"))
            self.assertIn("过一会儿", second.text)
            self.assertFalse(private.error)

    def test_search_fetch_and_summarize_share_the_network_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = self._service(tmpdir, network_user_burst=2, network_user_per_hour=0)
            dispatcher = service.dispatcher
            event = _event()
            fetch = "waifu_standalone.skills.dispatcher.fetch_public_url"
            with patch(fetch, side_effect=TransportError("offline")) as fetcher:
                first = dispatcher.use_web_fetch_tool(_invocation("web-fetch", event, url="https://example.com/a"))
                second = dispatcher.use_web_fetch_tool(_invocation("web-fetch", event, url="https://example.com/b"))
                third = dispatcher.use_web_fetch_tool(_invocation("web-fetch", event, url="https://example.com/c"))
            search = dispatcher.use_search_tool(_invocation("search", event, query="天气"))
            summarize = dispatcher.use_summarize_tool(
                _invocation("summarize", event, target="https://example.com/article")
            )

            self.assertEqual(fetcher.call_count, 2)
            self.assertFalse(first.metadata.get("rate_limited"))
            self.assertFalse(second.metadata.get("rate_limited"))
            for limited in (third, search, summarize):
                self.assertTrue(limited.metadata.get("rate_limited"))
                self.assertIn("联网查询", limited.text)
            # A missing argument is rejected before it costs a token.
            missing = dispatcher.use_web_fetch_tool(_invocation("web-fetch", event, url=""))
            self.assertFalse(missing.metadata.get("rate_limited"))

    def test_exempt_users_and_disabled_limits_are_not_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = self._service(
                tmpdir,
                image_user_burst=1,
                image_user_per_hour=0,
                exempt_user_ids=["10001"],
            )
            dispatcher = service.dispatcher
            for _ in range(3):
                result = dispatcher.use_image_tool(_invocation("image", _event("10001"), prompt="猫"))
                self.assertFalse(result.error)

            service.config.rate_limits.enabled = False
            for _ in range(3):
                result = dispatcher.use_image_tool(_invocation("image", _event("783190298"), prompt="猫"))
                self.assertFalse(result.error)


class RateLimitConfigTests(unittest.TestCase):
    def test_rate_limits_load_from_config_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "rate_limits": {
                            "image_user_burst": 2,
                            "image_group_per_hour": 12.5,
                            "network_user_burst": -4,
                            "network_group_burst": "oops",
                            "exempt_user_ids": ["10001"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"OPENQQWAIFU_RATE_LIMITS_ENABLED": "false", "OPENQQWAIFU_RATE_LIMITS_EXEMPT_USER_IDS": "1,2"},
                clear=False,
            ):
                config = ConfigManager(config_path).load()

        limits = config.rate_limits
        self.assertFalse(limits.enabled)
        self.assertEqual(limits.image_user_burst, 2)
        self.assertEqual(limits.image_group_per_hour, 12.5)
        self.assertEqual(limits.network_user_burst, 0)
        self.assertEqual(limits.network_group_burst, RateLimitConfig().network_group_burst)
        self.assertEqual(limits.image_user_per_hour, RateLimitConfig().image_user_per_hour)
        self.assertEqual(limits.exempt_user_ids, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
