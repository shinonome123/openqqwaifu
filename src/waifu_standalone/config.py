from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PluginsConfig:
    enabled: bool = True
    disabled_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QQSidecarConfig:
    mode: str = "onebot-http"
    gateway_mode: str = "http"
    adapter_name: str = "napcat"
    inbound_host: str = "127.0.0.1"
    inbound_port: int = 8080
    outbound_base_url: str = "http://127.0.0.1:3000"
    outbound_timeout_seconds: float = 10.0
    access_token: str = ""
    webui_base_url: str = "http://127.0.0.1:6099"
    webui_api_prefix: str = "/api"
    webui_timeout_seconds: float = 10.0
    webui_token: str = ""
    reverse_ws_url: str = "ws://127.0.0.1:3001/onebot/v11/ws"
    reverse_ws_access_token: str = ""
    reverse_ws_send_timeout_seconds: float = 15.0
    dry_run: bool = False


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = False
    backend: str = "dify"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    app_type: str = "chat"
    timeout_seconds: float = 45.0


@dataclass(slots=True)
class ImageGenerationConfig:
    enabled: bool = False
    base_url: str = "https://api.x.ai/v1"
    api_key: str = ""
    model: str = "grok-imagine-image-pro"
    timeout_seconds: float = 180.0
    response_format: str = "b64_json"
    aspect_ratio: str = "1:1"
    resolution: str = ""


@dataclass(slots=True)
class EmbeddingConfig:
    enabled: bool = False
    backend: str = "openai"
    base_url: str = ""
    api_key: str = ""
    model: str = "text-embedding-3-small"
    timeout_seconds: float = 30.0


@dataclass(slots=True)
class MarketplaceSourceConfig:
    source_id: str = "skillsmp"
    name: str = "SkillsMP"
    kind: str = "skillsmp"
    enabled: bool = True
    base_url: str = "https://skillsmp.com"
    search_path: str = "/api/v1/skills/search"
    browse_url: str = "https://skillsmp.com/zh"
    api_key: str = ""
    timeout_seconds: float = 10.0
    max_results: int = 12


@dataclass(slots=True)
class MarketplaceConfig:
    enabled: bool = True
    default_query: str = "codex"
    sources: list[MarketplaceSourceConfig] = field(
        default_factory=lambda: [
            MarketplaceSourceConfig(),
            MarketplaceSourceConfig(
                source_id="clawhub",
                name="ClawHub",
                kind="clawhub",
                enabled=True,
                base_url="https://clawhub.ai",
                search_path="/api/v1/search",
                browse_url="https://clawhub.ai/skills",
            ),
            MarketplaceSourceConfig(
                source_id="github",
                name="GitHub Skills",
                kind="github",
                enabled=True,
                base_url="https://api.github.com",
                search_path="/search/repositories",
                browse_url="https://github.com/search?q=SKILL.md&type=repositories",
                max_results=8,
            ),
        ]
    )


@dataclass(slots=True)
class ToolPolicyConfig:
    enabled: bool = True
    allowed_roots: list[str] = field(default_factory=lambda: [".", "data"])
    write_enabled: bool = True
    write_allowed_roots: list[str] = field(default_factory=lambda: [".", "data"])
    exec_enabled: bool = True
    exec_allowed_roots: list[str] = field(default_factory=lambda: ["."])
    exec_allowlist: list[str] = field(
        default_factory=lambda: [
            "python",
            "python3",
            "py",
            "uv",
            "pytest",
            "git",
            "node",
            "npm",
            "npx",
            "pnpm",
            "yarn",
            "docker",
            "docker-compose",
        ]
    )


@dataclass(slots=True)
class ClawRuntimeConfig:
    enabled: bool = False
    mode: str = "managed"
    routing_mode: str = "hybrid"
    base_url: str = ""
    node_path: str = "node"
    runtime_root: str = ""
    acp_enabled: bool = False
    acp_default_command: str = ""
    acp_default_args: list[str] = field(default_factory=list)
    codex_harness_command: str = ""
    codex_harness_args: list[str] = field(default_factory=list)
    acp_session_timeout_seconds: float = 2.0
    plugin_tools_mcp_bridge: bool = False
    startup_timeout_seconds: float = 10.0


@dataclass(slots=True)
class AppConfig:
    service_name: str = "waifu-standalone"
    config_path: str = ""
    assistant_name: str = "Assistant"
    character: str = "default"
    bot_account_id: str = ""
    group_reply_requires_mention: bool = True
    skills_enabled: bool = True
    max_active_skills: int = 3
    search_enabled: bool = True
    search_result_limit: int = 3
    search_timeout_seconds: float = 8.0
    thinking_mode: bool = True
    conversation_analysis: bool = True
    summarization_mode: bool = False
    member_auto_sync: bool = True
    knowledge_auto_extract: bool = True
    knowledge_auto_extract_limit: int = 2
    event_mode: bool = True
    event_buffer_limit: int = 120
    story_mode: bool = False
    story_style: str = "intimate"
    story_detail_level: int = 2
    value_game_mode: bool = True
    value_game_reply_bonus: float = 0.08
    memory_graph_mode: bool = True
    memory_graph_limit: int = 8
    proactive_mode: bool = False
    proactive_inactive_hours: float = 6.0
    proactive_candidate_limit: int = 6
    proactive_min_affinity: float = 0.12
    image_command_prefix: str = "生图"
    image_command_aliases: list[str] = field(default_factory=lambda: ["draw"])
    ignore_prefixes: list[str] = field(default_factory=lambda: ["!", "？", "/"])
    group_follow_up_window_seconds: float = 5.0
    group_response_delay_seconds: float = 0.0
    repeat_trigger_count: int = 0
    multimodal_enabled: bool = True
    history_window_messages: int = 8
    memory_recall_limit: int = 3
    max_thinking_words: int = 30
    short_term_memory_limit: int = 30
    memory_summary_batch_size: int = 12
    data_root: str = "data"
    llm: LLMConfig = field(default_factory=LLMConfig)
    image_generation: ImageGenerationConfig = field(default_factory=ImageGenerationConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    marketplace: MarketplaceConfig = field(default_factory=MarketplaceConfig)
    tool_policy: ToolPolicyConfig = field(default_factory=ToolPolicyConfig)
    claw_runtime: ClawRuntimeConfig = field(default_factory=ClawRuntimeConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    qq_sidecar: QQSidecarConfig = field(default_factory=QQSidecarConfig)
