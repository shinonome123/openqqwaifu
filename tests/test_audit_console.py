from __future__ import annotations

import gzip
import hashlib
import hmac
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig
from waifu_standalone.models import SessionMemory
from waifu_standalone.settings_admin.config_manager import ConfigManager, _provision_napcat_onebot_network
from waifu_standalone.web.http_api import HttpApi, make_handler, run_server
from waifu_standalone.web.http_api_async import AsyncCompatServer


class ConsoleConfigRegressionTests(unittest.TestCase):
    def test_explicit_missing_config_fails_without_silent_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(FileNotFoundError, "dump-config"):
                ConfigManager(Path(root) / "missing.json").load()
        with self.assertRaisesRegex(ValueError, "config path"):
            ConfigManager().save(AppConfig())

    def test_partial_config_uses_dataclass_text_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "config.json"
            target.write_text("{}", encoding="utf-8")
            with patch("waifu_standalone.settings_admin.config_manager._napcat_config_dirs", return_value=[]):
                config = ConfigManager(target).load()
            defaults = AppConfig()
            self.assertEqual(config.assistant_name, defaults.assistant_name)
            self.assertEqual(config.image_command_prefix, defaults.image_command_prefix)
            self.assertEqual(config.image_command_aliases, defaults.image_command_aliases)

    def test_atomic_saves_preserve_previous_config_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "config.json"
            target.write_text('{"service_name":"previous"}', encoding="utf-8")
            with patch("waifu_standalone.settings_admin.config_manager.os.replace", side_effect=OSError("full")):
                with self.assertRaises(OSError):
                    ConfigManager(target).save(AppConfig(config_path=str(target)))
            self.assertEqual(json.loads(target.read_text())["service_name"], "previous")
            self.assertEqual(list(Path(root).glob("*.tmp")), [])

    def test_concurrent_writes_produce_one_complete_config(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "config.json"
            configs = [AppConfig(config_path=str(target), service_name=str(i) * (1000 + i)) for i in range(12)]
            with patch("waifu_standalone.settings_admin.config_manager._napcat_config_dirs", return_value=[]):
                with ThreadPoolExecutor(max_workers=6) as executor:
                    list(executor.map(ConfigManager(target).save, configs))
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(payload["service_name"], [item.service_name for item in configs])

    def test_security_fields_roundtrip_and_environment_overlay(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "config.json"
            config = AppConfig(config_path=str(target))
            config.qq_sidecar.inbound_access_token = "callback-secret"
            config.tool_policy.authorized_user_ids = ["123", "456"]
            with patch("waifu_standalone.settings_admin.config_manager._napcat_config_dirs", return_value=[]):
                ConfigManager(target).save(config)
                loaded = ConfigManager(target).load()
                self.assertEqual(loaded.qq_sidecar.inbound_access_token, "callback-secret")
                self.assertEqual(loaded.tool_policy.authorized_user_ids, ["123", "456"])
                with patch.dict(os.environ, {"OPENQQWAIFU_QQ_SIDECAR_INBOUND_ACCESS_TOKEN": "new",
                                           "OPENQQWAIFU_TOOL_POLICY_AUTHORIZED_USER_IDS": "789,012",
                                           "OPENQQWAIFU_QQ_SIDECAR_DRY_RUN": "true"}):
                    loaded = ConfigManager(target).load()
                self.assertEqual(loaded.qq_sidecar.inbound_access_token, "new")
                self.assertEqual(loaded.tool_policy.authorized_user_ids, ["789", "012"])
                self.assertTrue(loaded.qq_sidecar.dry_run)

    def test_managed_napcat_tokens_are_independent_and_rotate(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "onebot11_123.json"
            target.write_text(json.dumps({"network": {"httpServers": [{"name": "openqqwaifu-actions", "port": 3000, "token": "old"}]}}))
            config = AppConfig()
            config.qq_sidecar.access_token = "new-actions"
            config.qq_sidecar.inbound_access_token = "new-callback"
            _provision_napcat_onebot_network(config, [Path(root)])
            network = json.loads(target.read_text())["network"]
            self.assertEqual(network["httpServers"][0]["token"], "new-actions")
            self.assertEqual(network["httpClients"][0]["token"], "new-callback")

    def test_partial_settings_preserve_secrets_and_explicit_empty_clears(self):
        with tempfile.TemporaryDirectory() as root:
            service, _ = build_default_service(AppConfig(data_root=root))
            try:
                for section in ("llm", "image_generation", "embedding"):
                    getattr(service.config, section).api_key = "secret-token"
                service.save_ai_panel({"llm": {"model": "new-model"}})
                self.assertEqual(service.config.llm.api_key, "secret-token")
                service.save_ai_panel({section: {"api_key": ""} for section in ("llm", "image_generation", "embedding")})
                for section in ("llm", "image_generation", "embedding"):
                    self.assertEqual(getattr(service.config, section).api_key, "")
                service.config.qq_sidecar.webui_token = "webui-secret"
                service.save_sidecar_panel({"outbound_timeout_seconds": 7})
                self.assertEqual(service.config.qq_sidecar.webui_token, "webui-secret")
                service.save_sidecar_panel({"webui_token": ""})
                self.assertEqual(service.config.qq_sidecar.webui_token, "")
            finally:
                service.close()


class ConsoleHttpRegressionTests(unittest.TestCase):
    @contextmanager
    def server(self, asynchronous):
        with tempfile.TemporaryDirectory() as root:
            service, _ = build_default_service(AppConfig(data_root=root))
            service.config.qq_sidecar.inbound_access_token = "callback-secret"
            api = HttpApi(service)
            server = run_server(api, "127.0.0.1", 0) if asynchronous else ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield api, server.server_address
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()
                service.close()

    def request(self, address, path, payload=b"{}", headers=None, method="POST"):
        connection = http.client.HTTPConnection(*address, timeout=5)
        try:
            connection.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else {}
        finally:
            connection.close()

    def test_callback_auth_gzip_and_gateway_mode_on_both_servers(self):
        body = b'{"post_type":"meta_event"}'
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), self.server(asynchronous) as (api, address):
                status, _ = self.request(address, "/onebot/events", body)
                self.assertEqual(status, 401)
                # Outbound action credentials must not authorize inbound events.
                api.service.config.qq_sidecar.access_token = "outbound-only"
                status, _ = self.request(address, "/onebot/events", body, {"Authorization": "Bearer outbound-only"})
                self.assertEqual(status, 401)
                for raw, encoding in ((body, ""), (gzip.compress(body), "gzip")):
                    signature = "sha1=" + hmac.new(b"callback-secret", raw, hashlib.sha1).hexdigest()
                    status, _ = self.request(address, "/onebot/events", raw, {"Content-Encoding": encoding, "X-Signature": signature})
                    self.assertEqual(status, 204)
                status, _ = self.request(address, "/onebot/events", b"invalid-gzip", {"Content-Encoding": "gzip", "Authorization": "Bearer callback-secret"})
                self.assertEqual(status, 400)
                status, _ = self.request(address, "/onebot/events", gzip.compress(b"x" * (10 * 1024 * 1024 + 1)), {"Content-Encoding": "gzip", "Authorization": "Bearer callback-secret"})
                self.assertEqual(status, 413)
                api.service.config.qq_sidecar.inbound_access_token = ""
                status, payload = self.request(address, "/onebot/events", body)
                self.assertEqual(status, 503)
                self.assertIn("inbound_access_token", payload["reason"])
                api.service.config.qq_sidecar.gateway_mode = "reverse_ws"
                status, _ = self.request(address, "/onebot/events", body)
                self.assertEqual(status, 404)

    def test_non_admin_cannot_delete_and_admin_purge_is_complete(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), self.server(asynchronous) as (api, address):
                api.auth.bootstrap_admin("admin", "password123")
                with api.auth._lock:
                    api.auth._users["viewer"] = api.auth._build_user_record("viewer", password="password123", role="viewer")
                    api.auth._save_locked()
                _, admin_cookie = api.login({"username": "admin", "password": "password123"})
                _, viewer_cookie = api.login({"username": "viewer", "password": "password123"})
                admin_headers = {"Cookie": admin_cookie.split(";", 1)[0]}
                viewer_headers = {"Cookie": viewer_cookie.split(";", 1)[0]}
                service = api.service
                for index in range(61):
                    service.session_repository.save(SessionMemory(launcher_id=str(index), launcher_type="person", character_id=service._active_character_id(), history=["hello"]))
                for index in range(81):
                    service.save_knowledge_entry({"scope_type": "global", "scope_id": "", "summary": f"fact-{index}"})
                status, _ = self.request(address, "/api/knowledge/1", headers=viewer_headers, method="DELETE")
                self.assertEqual(status, 403)
                self.assertEqual(service.knowledge_repository.knowledge_count(character_id=service._active_character_id()), 81)
                status, _ = self.request(address, "/api/memory/purge", headers=viewer_headers)
                self.assertEqual(status, 403)
                service._active_events = 1
                try:
                    status, _ = self.request(address, "/api/memory/purge", headers=admin_headers)
                    self.assertEqual(status, 409)
                    status, _ = self.request(address, "/api/panels/character", b'{"character":"other","set_active":true}', admin_headers)
                    self.assertEqual(status, 409)
                finally:
                    service._active_events = 0
                status, result = self.request(address, "/api/memory/sessions/person/0/save", b'{"history":[],"metadata":{}}', admin_headers)
                self.assertEqual(status, 200)
                self.assertEqual(result["session"]["history"], [])
                status, result = self.request(address, "/api/memory/purge", headers=admin_headers)
                self.assertEqual(status, 200)
                self.assertEqual(result["sessions_cleared"], 61)
                self.assertEqual(result["knowledge_deleted"], 81)
                self.assertFalse(any(item.history for item in service.session_repository.list_sessions()))
                self.assertEqual(service.knowledge_repository.knowledge_count(character_id=service._active_character_id()), 0)
                status, _ = self.request(address, "/api/auth/change-password",
                                         b'{"current_password":"password123","new_password":"changed123"}', admin_headers)
                self.assertEqual(status, 200)
                status, _ = self.request(address, "/api/panels/user", headers=admin_headers, method="GET")
                self.assertEqual(status, 401)


class ConsoleShutdownRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_business_work_before_runner_cleanup(self):
        sequence = []

        def step(name):
            async def operation(*args, **kwargs):
                sequence.append(name)
            return operation

        service = SimpleNamespace(wait_idle=step("idle"), close=Mock(),
                                  reverse_ws_gateway=SimpleNamespace(close=step("gateway")))
        server = AsyncCompatServer(SimpleNamespace(service=service), "127.0.0.1", 0)
        server._site = SimpleNamespace(stop=step("site"))
        server._runner = SimpleNamespace(cleanup=step("runner"))
        try:
            await server._cleanup()
            self.assertEqual(sequence, ["site", "gateway", "idle", "runner"])
        finally:
            server._close_resources()

    async def test_shutdown_timeout_preserves_store_for_active_workers(self):
        service = SimpleNamespace(wait_idle=AsyncMock(side_effect=TimeoutError("still busy")),
                                  close=Mock(), reverse_ws_gateway=None)
        server = AsyncCompatServer(SimpleNamespace(service=service), "127.0.0.1", 0)
        with self.assertRaises(TimeoutError):
            await server._cleanup()
        server._close_resources()
        service.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
