from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.app import build_default_service
from waifu_standalone.cells.skill_registry import build_skill_markdown_template
from waifu_standalone.config import AppConfig, ClawRuntimeConfig
from waifu_standalone.http_api import HttpApi, RequestTooLarge, _probe_http_endpoint, _read_chunked_body, parse_onebot_event
from waifu_standalone.infra.llm_clients import LLMToolCall, LLMToolResponse


class _BrokenService:
    def handle_event(self, event):  # type: ignore[no-untyped-def]
        raise RuntimeError("sidecar unavailable")


class _FakeChunkedHandler:
    def __init__(self, raw: bytes):
        self.rfile = io.BytesIO(raw)


class _FakeNapCatLoginBridge:
    def __init__(self) -> None:
        self.refreshed = False

    def webui_url(self) -> str:
        return "http://127.0.0.1:6099/webui"

    def panel(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
        return {
            "configured": True,
            "token_configured": True,
            "webui_url": self.webui_url(),
            "status": {
                "is_login": self.refreshed,
                "is_offline": False,
                "qrcode_url": "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
                "login_error": "",
            },
            "login_info": {},
        }

    def refresh_qrcode(self):  # type: ignore[no-untyped-def]
        self.refreshed = True
        return self.panel(refresh=True)

    def qrcode_payload(self) -> str:
        return "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.outbound = build_default_service()
        self.api = HttpApi(self.service)

    def _install_tool_client(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        final_text: str,
    ) -> None:
        class _SingleToolClient:
            enabled = True
            supports_tool_calling = True
            base_url = "https://example.com/v1"
            api_key = "secret"
            model = "tool-model"
            backend = "openai"
            timeout_seconds = 45.0
            app_type = "chat"

            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return final_text

            async def ainvoke(self, query: str, *, user: str = "waifu-standalone") -> str:
                return final_text

            def invoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                return self._next_response()

            async def ainvoke_with_tools(self, messages, *, tools, user: str = "waifu-standalone"):
                return self._next_response()

            def _next_response(self):
                self.calls += 1
                if self.calls == 1:
                    return LLMToolResponse(
                        tool_calls=[
                            LLMToolCall(
                                call_id="call_1",
                                tool_name=tool_name,
                                arguments=dict(arguments),
                            )
                        ],
                        assistant_message={
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(arguments, ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                    )
                return LLMToolResponse(
                    text=final_text,
                    assistant_message={"role": "assistant", "content": final_text},
                )

        self.service.config.llm.enabled = True
        self.service.generator._dify_client = _SingleToolClient()  # type: ignore[assignment]

    def test_message_payload_is_accepted(self) -> None:
        payload = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 612475113,
            "user_id": 783190298,
            "sender": {"user_id": 783190298, "nickname": "tester"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["reply"]["launcher_id"], "612475113")

    def test_message_payload_is_accepted_async(self) -> None:
        payload = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 612475113,
            "user_id": 783190298,
            "sender": {"user_id": 783190298, "nickname": "tester"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }

        status, body = asyncio.run(self.api.handle_json_async(payload))

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["reply"]["launcher_id"], "612475113")

    def test_string_message_payload_is_accepted(self) -> None:
        self._install_tool_client(
            tool_name="image",
            arguments={"prompt": "catgirl"},
            final_text="图片生成好了。",
        )
        payload = {
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "sender": {"nickname": "tester"},
            "message": "draw: catgirl",
        }

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["reply"]["images"], ["generated://catgirl"])

    def test_non_message_payload_is_ignored(self) -> None:
        payload = {"post_type": "notice"}

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "ignored")

    def test_notice_payload_is_forwarded_to_service(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_handle_notice(service, payload):  # type: ignore[no-untyped-def]
            calls.append(dict(payload))
            return {"status": "ok", "reason": "bot_joined_group"}

        with patch.object(type(self.service), "handle_notice_payload", fake_handle_notice):
            status, body = self.api.handle_json({"post_type": "notice", "notice_type": "group_increase"})

        self.assertEqual(status, 202)
        self.assertEqual(body["reason"], "bot_joined_group")
        self.assertEqual(len(calls), 1)

    def test_notice_payload_prefers_async_service_handler(self) -> None:
        calls: list[dict[str, object]] = []

        async def fake_handle_notice_async(service, payload):  # type: ignore[no-untyped-def]
            calls.append(dict(payload))
            return {"status": "ok", "reason": "async_notice"}

        with patch.object(type(self.service), "handle_notice_payload_async", fake_handle_notice_async):
            status, body = asyncio.run(
                self.api.handle_json_async({"post_type": "notice", "notice_type": "group_card"})
            )

        self.assertEqual(status, 202)
        self.assertEqual(body["reason"], "async_notice")
        self.assertEqual(len(calls), 1)

    def test_delivery_failures_are_mapped_to_502(self) -> None:
        api = HttpApi(_BrokenService())  # type: ignore[arg-type]

        status, body = api.handle_json({"message": "hello"})

        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "delivery_failed")

    def test_async_delivery_failures_fall_back_to_sync_service_handler(self) -> None:
        api = HttpApi(_BrokenService())  # type: ignore[arg-type]

        status, body = asyncio.run(api.handle_json_async({"message": "hello"}))

        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "delivery_failed")

    def test_reply_body_is_json_serializable(self) -> None:
        payload = {
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "sender": {"nickname": "tester"},
            "message": [{"type": "text", "data": {"text": "draw: catgirl"}}],
        }

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 200)
        json.dumps(body, ensure_ascii=False)

    def test_parse_event_prefers_raw_message_for_pure_text_payloads(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "person",
                "user_id": 2,
                "sender": {"user_id": 2, "nickname": "tester"},
                "message": [{"type": "text", "data": {"text": "garbled"}}],
                "raw_message": "call me luna",
            }
        )

        self.assertEqual(event.plain_text, "call me luna")

    def test_parse_event_falls_back_to_sender_id_for_suspicious_nickname(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "person",
                "user_id": 2,
                "sender": {"user_id": 2, "nickname": "闂傚倸鍊块埛瀣渻"},
                "raw_message": "hello",
            }
        )

        self.assertEqual(event.sender_name, "user_2")

    def test_parse_event_uses_user_id_for_private_temp_sessions(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "private",
                "sub_type": "group",
                "group_id": 612475113,
                "user_id": 783190298,
                "sender": {"user_id": 783190298, "nickname": "tester"},
                "raw_message": "hello",
            }
        )

        self.assertEqual(event.launcher_type, "person")
        self.assertEqual(event.launcher_id, "783190298")

    def test_parse_event_keeps_mentions_and_images(self) -> None:
        event = parse_onebot_event(
            {
                "message_type": "group",
                "group_id": 612475113,
                "user_id": 783190298,
                "sender": {"user_id": 783190298, "nickname": "tester"},
                "message": [
                    {"type": "at", "data": {"qq": "3518944354"}},
                    {"type": "text", "data": {"text": " draw: sunny sky"}},
                    {"type": "image", "data": {"base64": "data:image/png;base64,aaaa"}},
                ],
            }
        )

        self.assertTrue(event.has_bot_mention("3518944354"))
        self.assertEqual(event.command_text("3518944354"), "draw: sunny sky")
        self.assertEqual(event.image_count, 1)
        self.assertEqual(event.image_payloads(), ["data:image/png;base64,aaaa"])
        self.assertEqual(event.to_memory_text(), "发送了图片，并且说：“draw: sunny sky”。")

    def test_private_temp_session_reply_targets_user_not_group(self) -> None:
        payload = {
            "message_type": "private",
            "sub_type": "group",
            "group_id": 612475113,
            "user_id": 783190298,
            "sender": {"user_id": 783190298, "nickname": "tester"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }

        status, body = self.api.handle_json(payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["reply"]["launcher_type"], "person")
        self.assertEqual(body["reply"]["launcher_id"], "783190298")

    def test_skill_listing_is_available(self) -> None:
        skills = self.api.list_skills()

        self.assertTrue(skills["enabled"])
        self.assertGreaterEqual(skills["count"], 6)

    def test_tool_listing_is_available(self) -> None:
        tools = self.api.list_tools()

        self.assertEqual(tools["count"], 17)

    def test_skills_panel_includes_normalized_capabilities_and_safety(self) -> None:
        panel = self.api.get_skills_panel()

        self.assertIn("capabilities", panel)
        self.assertIn("skill_groups", panel)
        self.assertIn("tool_groups", panel)
        self.assertIn("safety", panel)
        self.assertIn("claw_runtime", panel)
        self.assertGreaterEqual(len(panel["capabilities"]), 3)
        self.assertIn("exec_allowlist", panel["safety"])

    def test_claw_runtime_api_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(
                AppConfig(
                    data_root=tmpdir,
                    claw_runtime=ClawRuntimeConfig(enabled=True, mode="managed", routing_mode="shadow"),
                )
            )
            api = HttpApi(service)
            try:
                runtime = api.get_claw_runtime_panel(refresh=True)
                self.assertTrue(runtime["healthy"])
                plugins = api.list_claw_plugins()
                self.assertIn("items", plugins)
            finally:
                service.close()

    def test_behavior_and_proactive_api_are_available(self) -> None:
        service, _ = build_default_service(
            AppConfig(
                group_reply_requires_mention=False,
                proactive_mode=True,
                proactive_inactive_hours=0.0,
                proactive_min_affinity=0.0,
            )
        )
        api = HttpApi(service)
        service.handle_event(
            parse_onebot_event(
                {
                    "message_type": "group",
                    "group_id": 612475113,
                    "user_id": 783190298,
                    "sender": {"user_id": 783190298, "nickname": "tester"},
                    "message": [{"type": "text", "data": {"text": "hello"}}],
                }
            )
        )

        behavior = api.behavior_events(limit=10)
        proactive = api.get_proactive_panel(limit=5)
        draft = api.generate_proactive_draft({"group_id": "612475113", "user_id": "783190298"})

        self.assertGreaterEqual(len(behavior["events"]), 2)
        self.assertGreaterEqual(len(proactive["candidates"]), 1)
        self.assertIn("text", draft["draft"])

    def test_prometheus_metrics_include_onebot_counters(self) -> None:
        status, _ = self.api.handle_json(
            {
                "message_type": "group",
                "group_id": 612475113,
                "user_id": 783190298,
                "sender": {"user_id": 783190298, "nickname": "tester"},
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )

        metrics_text = self.api.prometheus_metrics()

        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn("openqqwaifu_service_up 1", metrics_text)
        self.assertIn(
            'openqqwaifu_onebot_events_total{outcome="ok",post_type="message"} 1',
            metrics_text,
        )
        self.assertIn("openqqwaifu_uptime_seconds", metrics_text)

    def test_observability_panel_exposes_upstream_rollups(self) -> None:
        self.api.metrics.record_upstream_request(
            kind="sidecar",
            target="onebot_http",
            method="POST",
            url="http://127.0.0.1:3000/send_group_msg",
            status=200,
            outcome="ok",
            duration_seconds=0.05,
        )

        panel = self.api.observability_panel(log_limit=20, row_limit=20)

        self.assertEqual(panel["upstream"]["total"], 1)
        self.assertEqual(panel["upstream"]["error_total"], 0)
        self.assertEqual(panel["upstream"]["targets"][0]["target"], "onebot_http")
        self.assertEqual(panel["upstream"]["targets"][0]["kind"], "sidecar")

    def test_qq_login_api_is_available(self) -> None:
        self.service.napcat_login = _FakeNapCatLoginBridge()

        panel = self.api.get_qq_login_panel(refresh=True)
        self.assertTrue(panel["configured"])
        self.assertTrue(panel["token_configured"])
        self.assertFalse(panel["status"]["is_login"])

        refreshed = self.api.refresh_qq_login_panel()
        self.assertTrue(refreshed["status"]["is_login"])

        asset = self.api.get_qq_login_qrcode_image()
        self.assertIsNotNone(asset)
        assert asset is not None
        body, content_type = asset
        self.assertEqual(content_type, "image/svg+xml")
        self.assertEqual(body, b"<svg></svg>")

    def test_skill_pack_template_is_available(self) -> None:
        pack = self.api.skill_pack_template()

        self.assertEqual(pack["format"], "waifu-skill-pack")
        self.assertGreaterEqual(pack["skill_count"], 1)

    def test_skill_detail_is_available(self) -> None:
        detail = self.api.get_skill_detail("search-command")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["command_tool"], "search")
        self.assertIn("markdown", detail)

    def test_skill_template_is_available(self) -> None:
        template = self.api.new_skill_template()

        self.assertIn("markdown", template)
        self.assertIn("custom-skill", template["markdown"])

    def test_skill_install_save_and_delete_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            markdown = build_skill_markdown_template(
                skill_id="api-skill",
                name="API Skill",
                description="api path test",
                triggers=["api skill"],
                mode="prefix",
                priority=5,
                body="Use this from the API test.",
            )

            installed = api.install_skill(markdown)
            self.assertEqual(installed["id"], "api-skill")

            saved = api.save_skill("api-skill", markdown.replace("api path test", "updated api path test"))
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertIn("updated api path test", saved["markdown"])

            deleted = api.delete_skill("api-skill")
            self.assertTrue(deleted)

    def test_skill_pack_export_and_import_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            markdown = build_skill_markdown_template(
                skill_id="pack-api-skill",
                name="Pack API Skill",
                description="pack api test",
                triggers=["pack api"],
                mode="prefix",
                priority=5,
                body="Use this from pack API test.",
            )
            api.install_skill(markdown)

            bundle = api.export_skill_pack(skill_ids=["pack-api-skill"], include_builtin=False, name="single-pack")
            self.assertEqual(bundle["skill_count"], 1)

            imported = api.import_skill_pack(bundle, overwrite=True)
            self.assertEqual(imported["imported_count"], 1)

    def test_non_admin_user_panel_omits_directory_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _ = build_default_service(AppConfig(data_root=tmpdir))
            api = HttpApi(service)
            assert api.auth is not None
            auth = api.auth
            admin = auth.bootstrap_admin("admin", "password123")
            with auth._lock:
                auth._users["viewer"] = auth._build_user_record("viewer", password="password123", role="user")
                auth._save_locked()

            service.state_store.save_member(
                {
                    "group_id": "612475113",
                    "user_id": "783190298",
                    "qq_nickname": "tester",
                    "preferred_name": "luna",
                }
            )

            admin_panel = api.get_user_panel(admin["username"])
            viewer_panel = api.get_user_panel("viewer")

            self.assertIn("members", admin_panel)
            self.assertEqual(viewer_panel["current_user"]["username"], "viewer")
            self.assertEqual(viewer_panel["users"], [viewer_panel["current_user"]])
            self.assertNotIn("members", viewer_panel)

    def test_character_preview_endpoint_returns_reply_and_transcript(self) -> None:
        detail = self.api.preview_character_panel(
            {
                "launcher_type": "person",
                "user_name": "Captain",
                "message": "如果我现在找你聊天，你会怎么接？",
                "shared_fields": {
                    "assistant_name": "Aurora",
                    "user_name": "Captain",
                    "language": "简体中文",
                },
                "person_fields": {
                    "profile": ["gentle"],
                    "skills": ["keeps continuity"],
                    "background": ["private chat"],
                    "rules": ["stay concise"],
                    "prologue": ["lights on"],
                },
                "group_fields": {
                    "profile": ["playful"],
                    "skills": ["tracks speakers"],
                    "background": ["group chat"],
                    "rules": ["don't spam"],
                    "prologue": ["notification pops"],
                },
            }
        )

        self.assertEqual(detail["assistant_name"], "Aurora")
        self.assertEqual(detail["user_name"], "Captain")
        self.assertTrue(detail["reply_text"])
        self.assertEqual(len(detail["transcript"]), 2)
        self.assertEqual(detail["transcript"][0]["role"], "user")
        self.assertEqual(detail["transcript"][1]["role"], "assistant")

    def test_chunked_reader_rejects_oversized_payload(self) -> None:
        chunk_size = 10 * 1024 * 1024 + 1
        handler = _FakeChunkedHandler(f"{chunk_size:X}\r\n".encode("ascii"))

        with self.assertRaises(RequestTooLarge):
            _read_chunked_body(handler)  # type: ignore[arg-type]

    def test_probe_http_endpoint_success_payload_matches_frontend_expectations(self) -> None:
        class _OkResponse:
            status = 204

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
                return False

        with patch("waifu_standalone.http_api.urllib.request.urlopen", return_value=_OkResponse()):
            result = _probe_http_endpoint("https://example.com/v1", "secret")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_status"], 204)
        self.assertIn("elapsed_ms", result)
        self.assertTrue(result["reachable"])

    def test_probe_http_endpoint_network_failure_exposes_ok_false(self) -> None:
        with patch(
            "waifu_standalone.http_api.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            result = _probe_http_endpoint("https://example.com/v1", "secret")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["reachable"])
        self.assertIn("connection refused", result["error"])


if __name__ == "__main__":
    unittest.main()
