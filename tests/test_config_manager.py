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
from waifu_standalone.config import AppConfig, ClawRuntimeConfig, QQSidecarConfig


class ConfigManagerTests(unittest.TestCase):
    def test_legacy_narrator_keys_load_into_story_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "narrator_mode": True,
                        "narrator_style": "subtle",
                        "narrator_detail_level": 4,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=False):
                config = ConfigManager(config_path).load()

        self.assertTrue(config.story_mode)
        self.assertEqual(config.story_style, "intimate")
        self.assertEqual(config.story_detail_level, 4)

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

    def test_autodiscovery_provisions_reverse_ws_client_and_removes_http_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "napcat" / "config").mkdir(parents=True, exist_ok=True)
            onebot_path = root / "napcat" / "config" / "onebot11_99.json"
            onebot_path.write_text(
                json.dumps(
                    {
                        "network": {
                            "httpServers": [],
                            "httpClients": [
                                {
                                    "name": "openqqwaifu-webhook",
                                    "enable": True,
                                    "url": "http://openqqwaifu:8080/onebot/events",
                                }
                            ],
                            "websocketServers": [],
                            "websocketClients": [],
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
                            "gateway_mode": "reverse_ws",
                            "inbound_host": "0.0.0.0",
                            "inbound_port": 8080,
                            "reverse_ws_url": "ws://openqqwaifu:8080/onebot/v11/ws",
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

        self.assertEqual(raw["network"]["httpClients"], [])
        self.assertEqual(len(raw["network"]["websocketClients"]), 1)
        self.assertEqual(
            raw["network"]["websocketClients"][0]["url"],
            "ws://openqqwaifu:8080/onebot/v11/ws",
        )
        self.assertTrue(raw["network"]["websocketClients"][0]["enable"])
        self.assertEqual(config.qq_sidecar.gateway_mode, "reverse_ws")

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

    def test_save_reprovisions_napcat_network_with_current_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_root = root / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            napcat_dir = root / "napcat" / "config"
            napcat_dir.mkdir(parents=True, exist_ok=True)
            onebot_path = napcat_dir / "onebot11_3956638110.json"
            onebot_path.write_text(
                json.dumps(
                    {
                        "network": {
                            "httpServers": [
                                {
                                    "name": "openqqwaifu-actions",
                                    "enable": True,
                                    "host": "0.0.0.0",
                                    "port": 3000,
                                    "token": "",
                                }
                            ],
                            "httpClients": [
                                {
                                    "name": "openqqwaifu-webhook",
                                    "enable": True,
                                    "url": "http://127.0.0.1:8080/onebot/events",
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_path = data_root / "config.json"
            config = AppConfig(
                config_path=str(config_path),
                data_root=str(data_root),
                qq_sidecar=QQSidecarConfig(
                    inbound_host="0.0.0.0",
                    inbound_port=8080,
                    outbound_base_url="http://napcat:3000",
                    webui_base_url="http://napcat:6099",
                ),
            )

            with patch.dict(
                os.environ,
                {
                    "OPENQQWAIFU_QQ_SIDECAR_CALLBACK_BASE_URL": "http://openqqwaifu:8080",
                },
                clear=False,
            ):
                ConfigManager().save(config)

            raw = json.loads(onebot_path.read_text(encoding="utf-8"))

        self.assertEqual(
            raw["network"]["httpClients"][0]["url"],
            "http://openqqwaifu:8080/onebot/events",
        )

    def test_loads_and_saves_claw_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "claw_runtime": {
                            "enabled": True,
                            "mode": "external",
                            "routing_mode": "hybrid",
                            "base_url": "http://127.0.0.1:19555",
                            "node_path": "custom-node",
                            "runtime_root": "./runtime/claw",
                            "acp_enabled": True,
                            "acp_default_command": "python",
                            "acp_default_args": ["-m", "openqqwaifu_acp"],
                            "codex_harness_command": "codex",
                            "codex_harness_args": ["serve"],
                            "acp_session_timeout_seconds": 6.5,
                            "plugin_tools_mcp_bridge": True,
                            "startup_timeout_seconds": 18.0,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            config = ConfigManager(config_path).load()

            self.assertTrue(config.claw_runtime.enabled)
            self.assertEqual(config.claw_runtime.mode, "external")
            self.assertEqual(config.claw_runtime.routing_mode, "hybrid")
            self.assertEqual(config.claw_runtime.base_url, "http://127.0.0.1:19555")
            self.assertEqual(config.claw_runtime.node_path, "custom-node")
            runtime_root = Path(config.claw_runtime.runtime_root)
            self.assertTrue(runtime_root.is_absolute())
            self.assertEqual(runtime_root.name, "claw")
            self.assertEqual(runtime_root.parent.name, "runtime")
            self.assertTrue(config.claw_runtime.acp_enabled)
            self.assertEqual(config.claw_runtime.acp_default_command, "python")
            self.assertEqual(config.claw_runtime.acp_default_args, ["-m", "openqqwaifu_acp"])
            self.assertEqual(config.claw_runtime.codex_harness_command, "codex")
            self.assertEqual(config.claw_runtime.codex_harness_args, ["serve"])
            self.assertEqual(config.claw_runtime.acp_session_timeout_seconds, 6.5)
            self.assertTrue(config.claw_runtime.plugin_tools_mcp_bridge)
            self.assertEqual(config.claw_runtime.startup_timeout_seconds, 18.0)

            saved_path = root / "saved.json"
            ConfigManager().save(config, saved_path)
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertIn("claw_runtime", saved)
            self.assertEqual(saved["claw_runtime"]["routing_mode"], "hybrid")
            self.assertEqual(saved["claw_runtime"]["codex_harness_command"], "codex")

    def test_env_overrides_claw_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"claw_runtime": {"enabled": False}}), encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "OPENQQWAIFU_CLAW_RUNTIME_ENABLED": "true",
                    "OPENQQWAIFU_CLAW_RUNTIME_MODE": "external",
                    "OPENQQWAIFU_CLAW_RUNTIME_ROUTING_MODE": "authoritative",
                    "OPENQQWAIFU_CLAW_RUNTIME_BASE_URL": "http://127.0.0.1:19666",
                    "OPENQQWAIFU_CLAW_RUNTIME_NODE_PATH": "node-custom",
                    "OPENQQWAIFU_CLAW_RUNTIME_ROOT": "./runtime-root",
                    "OPENQQWAIFU_CLAW_RUNTIME_ACP_ENABLED": "true",
                    "OPENQQWAIFU_CLAW_RUNTIME_ACP_DEFAULT_COMMAND": "python3",
                    "OPENQQWAIFU_CLAW_RUNTIME_ACP_DEFAULT_ARGS": "-m,agent_runtime",
                    "OPENQQWAIFU_CLAW_RUNTIME_CODEX_HARNESS_COMMAND": "codex",
                    "OPENQQWAIFU_CLAW_RUNTIME_CODEX_HARNESS_ARGS": "serve,--stdio",
                    "OPENQQWAIFU_CLAW_RUNTIME_ACP_SESSION_TIMEOUT_SECONDS": "8.5",
                    "OPENQQWAIFU_CLAW_RUNTIME_PLUGIN_TOOLS_MCP_BRIDGE": "true",
                    "OPENQQWAIFU_CLAW_RUNTIME_STARTUP_TIMEOUT_SECONDS": "21",
                },
                clear=False,
            ):
                config = ConfigManager(config_path).load()

        self.assertTrue(config.claw_runtime.enabled)
        self.assertEqual(config.claw_runtime.mode, "external")
        self.assertEqual(config.claw_runtime.routing_mode, "authoritative")
        self.assertEqual(config.claw_runtime.base_url, "http://127.0.0.1:19666")
        self.assertEqual(config.claw_runtime.node_path, "node-custom")
        self.assertTrue(config.claw_runtime.runtime_root.endswith("runtime-root"))
        self.assertTrue(Path(config.claw_runtime.runtime_root).is_absolute())
        self.assertTrue(config.claw_runtime.acp_enabled)
        self.assertEqual(config.claw_runtime.acp_default_command, "python3")
        self.assertEqual(config.claw_runtime.acp_default_args, ["-m", "agent_runtime"])
        self.assertEqual(config.claw_runtime.codex_harness_command, "codex")
        self.assertEqual(config.claw_runtime.codex_harness_args, ["serve", "--stdio"])
        self.assertEqual(config.claw_runtime.acp_session_timeout_seconds, 8.5)
        self.assertTrue(config.claw_runtime.plugin_tools_mcp_bridge)
        self.assertEqual(config.claw_runtime.startup_timeout_seconds, 21.0)


if __name__ == "__main__":
    unittest.main()
