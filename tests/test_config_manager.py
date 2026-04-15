from __future__ import annotations

import json
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

from waifu_standalone.cells.config import ConfigManager


class ConfigManagerTests(unittest.TestCase):
    def test_autodiscovers_napcat_webui_token_from_sibling_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config" / "webui.json").write_text(
                json.dumps(
                    {
                        "host": "::",
                        "port": 6099,
                        "token": "auto-token",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path = root / "data" / "config.json"
            config_path.write_text(
                json.dumps({"data_root": "./runtime-data", "qq_sidecar": {"webui_token": ""}}),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=False):
                config = ConfigManager(config_path).load()

        self.assertEqual(config.qq_sidecar.webui_token, "auto-token")
        self.assertEqual(config.qq_sidecar.webui_base_url, "http://127.0.0.1:6099")

    def test_env_overrides_webui_bridge_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "data_root": "./data",
                        "qq_sidecar": {
                            "webui_base_url": "http://127.0.0.1:6099",
                            "webui_api_prefix": "/api",
                            "webui_timeout_seconds": 10.0,
                            "webui_token": "",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "OPENQQWAIFU_QQ_SIDECAR_WEBUI_BASE_URL": "http://napcat:6099",
                    "OPENQQWAIFU_QQ_SIDECAR_WEBUI_API_PREFIX": "/api",
                    "OPENQQWAIFU_QQ_SIDECAR_WEBUI_TIMEOUT_SECONDS": "22",
                    "OPENQQWAIFU_QQ_SIDECAR_WEBUI_TOKEN": "bridge-secret",
                    "OPENQQWAIFU_QQ_SIDECAR_DRY_RUN": "false",
                },
                clear=False,
            ):
                config = ConfigManager(config_path).load()

        self.assertEqual(config.qq_sidecar.webui_base_url, "http://napcat:6099")
        self.assertEqual(config.qq_sidecar.webui_api_prefix, "/api")
        self.assertEqual(config.qq_sidecar.webui_timeout_seconds, 22.0)
        self.assertEqual(config.qq_sidecar.webui_token, "bridge-secret")
        self.assertFalse(config.qq_sidecar.dry_run)

    def test_autodiscovery_provisions_empty_napcat_onebot_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            onebot_path = root / "napcat" / "config" / "onebot11_3956638110.json"
            onebot_path.write_text(
                json.dumps(
                    {
                        "network": {
                            "httpServers": [],
                            "httpSseServers": [],
                            "httpClients": [],
                            "websocketServers": [],
                            "websocketClients": [],
                            "plugins": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "data" / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "qq_sidecar": {
                            "inbound_host": "127.0.0.1",
                            "inbound_port": 13405,
                            "outbound_base_url": "",
                            "access_token": "",
                            "dry_run": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=False):
                config = ConfigManager(config_path).load()

            raw = json.loads(onebot_path.read_text(encoding="utf-8"))

        http_clients = raw["network"]["httpClients"]
        http_servers = raw["network"]["httpServers"]
        self.assertEqual(len(http_clients), 1)
        self.assertEqual(http_clients[0]["url"], "http://127.0.0.1:13405/onebot/events")
        self.assertTrue(http_clients[0]["enable"])
        self.assertEqual(len(http_servers), 1)
        self.assertEqual(http_servers[0]["host"], "0.0.0.0")
        self.assertEqual(http_servers[0]["port"], 3000)
        self.assertTrue(http_servers[0]["enable"])

        self.assertEqual(config.qq_sidecar.outbound_base_url, "http://127.0.0.1:3000")
        self.assertFalse(config.qq_sidecar.dry_run)

    def test_autodiscovery_preserves_existing_napcat_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            onebot_path = root / "napcat" / "config" / "onebot11_42.json"
            onebot_path.write_text(
                json.dumps(
                    {
                        "network": {
                            "httpServers": [
                                {
                                    "name": "user-server",
                                    "enable": True,
                                    "host": "127.0.0.1",
                                    "port": 5700,
                                    "token": "user-token",
                                }
                            ],
                            "httpClients": [
                                {
                                    "name": "other-bot",
                                    "enable": True,
                                    "url": "http://example/hook",
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "data").mkdir(parents=True, exist_ok=True)
            config_path = root / "data" / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "qq_sidecar": {
                            "inbound_host": "127.0.0.1",
                            "inbound_port": 13405,
                            "outbound_base_url": "",
                            "access_token": "",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=False):
                config = ConfigManager(config_path).load()

            raw = json.loads(onebot_path.read_text(encoding="utf-8"))

        urls = [entry.get("url") for entry in raw["network"]["httpClients"]]
        self.assertIn("http://example/hook", urls)
        self.assertIn("http://127.0.0.1:13405/onebot/events", urls)
        # Only one httpServer existed; provisioning should reuse it.
        self.assertEqual(len(raw["network"]["httpServers"]), 1)
        self.assertEqual(config.qq_sidecar.outbound_base_url, "http://127.0.0.1:5700")
        self.assertEqual(config.qq_sidecar.access_token, "user-token")

    def test_autodiscovery_uses_localhost_for_host_runtime_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            onebot_path = root / "napcat" / "config" / "onebot11_1.json"
            onebot_path.write_text(
                json.dumps({"network": {"httpServers": [], "httpClients": []}}),
                encoding="utf-8",
            )
            config_path = root / "data" / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "qq_sidecar": {
                            "inbound_host": "0.0.0.0",
                            "inbound_port": 13405,
                            "outbound_base_url": "",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=False), patch(
                "waifu_standalone.cells.config._is_running_in_container",
                return_value=False,
            ):
                ConfigManager(config_path).load()

            raw = json.loads(onebot_path.read_text(encoding="utf-8"))

        self.assertEqual(
            raw["network"]["httpClients"][0]["url"],
            "http://127.0.0.1:13405/onebot/events",
        )

    def test_autodiscovery_uses_container_hosts_for_compose_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            onebot_path = root / "napcat" / "config" / "onebot11_2.json"
            onebot_path.write_text(
                json.dumps({"network": {"httpServers": [], "httpClients": []}}),
                encoding="utf-8",
            )
            config_path = root / "data" / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "qq_sidecar": {
                            "inbound_host": "0.0.0.0",
                            "inbound_port": 8080,
                            "outbound_base_url": "http://napcat:3000",
                            "webui_base_url": "http://napcat:6099",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"OPENQQWAIFU_CONTAINER_HOSTNAME": "openqqwaifu"},
                clear=False,
            ), patch(
                "waifu_standalone.cells.config._is_running_in_container",
                return_value=True,
            ):
                config = ConfigManager(config_path).load()

            raw = json.loads(onebot_path.read_text(encoding="utf-8"))

        self.assertEqual(
            raw["network"]["httpClients"][0]["url"],
            "http://openqqwaifu:8080/onebot/events",
        )
        self.assertEqual(raw["network"]["httpServers"][0]["host"], "0.0.0.0")
        self.assertEqual(config.qq_sidecar.outbound_base_url, "http://napcat:3000")

    def test_autodiscovery_uses_container_hosts_when_inbound_host_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            onebot_path = root / "napcat" / "config" / "onebot11_22.json"
            onebot_path.write_text(
                json.dumps({"network": {"httpServers": [], "httpClients": []}}),
                encoding="utf-8",
            )
            config_path = root / "data" / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "qq_sidecar": {
                            "inbound_host": "",
                            "inbound_port": 8080,
                            "outbound_base_url": "http://napcat:3000",
                            "webui_base_url": "http://napcat:6099",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"OPENQQWAIFU_CONTAINER_HOSTNAME": "openqqwaifu"},
                clear=False,
            ), patch(
                "waifu_standalone.cells.config._is_running_in_container",
                return_value=True,
            ):
                ConfigManager(config_path).load()

            raw = json.loads(onebot_path.read_text(encoding="utf-8"))

        self.assertEqual(
            raw["network"]["httpClients"][0]["url"],
            "http://openqqwaifu:8080/onebot/events",
        )

    def test_autodiscovery_replaces_duplicate_managed_webhooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            onebot_path = root / "napcat" / "config" / "onebot11_3.json"
            onebot_path.write_text(
                json.dumps(
                    {
                        "network": {
                            "httpServers": [
                                {
                                    "name": "openqqwaifu-actions",
                                    "enable": True,
                                    "host": "127.0.0.1",
                                    "port": 3000,
                                    "token": "",
                                },
                                {
                                    "name": "openqqwaifu-actions",
                                    "enable": True,
                                    "host": "127.0.0.1",
                                    "port": 3001,
                                    "token": "",
                                },
                            ],
                            "httpClients": [
                                {
                                    "name": "openqqwaifu-webhook",
                                    "enable": True,
                                    "url": "http://127.0.0.1:13405/onebot/events",
                                },
                                {
                                    "name": "openqqwaifu-webhook",
                                    "enable": True,
                                    "url": "http://host.docker.internal:8080/onebot/events",
                                },
                                {
                                    "name": "openqqwaifu-webhook",
                                    "enable": True,
                                    "url": "http://openqqwaifu:8080/onebot/events",
                                },
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "data" / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "qq_sidecar": {
                            "inbound_host": "0.0.0.0",
                            "inbound_port": 8080,
                            "outbound_base_url": "http://napcat:3000",
                            "webui_base_url": "http://napcat:6099",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"OPENQQWAIFU_CONTAINER_HOSTNAME": "openqqwaifu"},
                clear=False,
            ), patch(
                "waifu_standalone.cells.config._is_running_in_container",
                return_value=True,
            ):
                ConfigManager(config_path).load()

            raw = json.loads(onebot_path.read_text(encoding="utf-8"))

        self.assertEqual(len(raw["network"]["httpClients"]), 1)
        self.assertEqual(
            raw["network"]["httpClients"][0]["url"],
            "http://openqqwaifu:8080/onebot/events",
        )
        self.assertEqual(len(raw["network"]["httpServers"]), 1)
        self.assertEqual(raw["network"]["httpServers"][0]["name"], "openqqwaifu-actions")

    def test_env_override_can_rebind_relative_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"data_root": "./data"}), encoding="utf-8")

            with patch.dict(
                os.environ,
                {"OPENQQWAIFU_DATA_ROOT": "./runtime-data"},
                clear=False,
            ):
                config = ConfigManager(config_path).load()

        self.assertTrue(config.data_root.endswith("runtime-data"))
        self.assertTrue(Path(config.data_root).is_absolute())


if __name__ == "__main__":
    unittest.main()
