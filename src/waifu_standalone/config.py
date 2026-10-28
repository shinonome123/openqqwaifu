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
    inbound_access_token: str = ""
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
    # 推理模型想多久（OpenAI 兼容接口的 reasoning_effort：low、medium、high），空表示用模型默认。
    # grok-4.7 默认每条回复先想一两千个 token，24–40 秒；low 只要 5–8 秒，聊天质量看不出差别。
    # 非推理模型（例如 grok-4.20-0309-non-reasoning）不认这个参数，留空。
    reasoning_effort: str = ""


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
    write_enabled: bool = False
    write_allowed_roots: list[str] = field(default_factory=lambda: [".", "data"])
    exec_enabled: bool = False
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
    # QQ 用户 ID 白名单：只有这些人才能让模型调用高风险工具
    # （读/写文件、列目录、执行命令、联网抓取）。默认空 = 任何人都不行。
    authorized_user_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SegmentedReplyConfig:
    # 像真人一样把一段话拆成几条发：按换行和句末标点切开，中间随机停一下。
    # 超过 max_total_chars 的长回复（讲故事、查到的结果）照旧整段发。
    enabled: bool = True
    max_parts: int = 3
    max_total_chars: int = 150
    min_delay_seconds: float = 1.0
    max_delay_seconds: float = 2.5


@dataclass(slots=True)
class StickerConfig:
    # 表情包库：data_root 下 directory/<分类>/ 里的图片（png、jpg、gif、webp）。
    # 模型在回复最后写「[表情:分类]」时随机发一张；两张之间至少隔 min_replies_between 条回复。
    enabled: bool = True
    directory: str = "stickers"
    min_replies_between: int = 3


@dataclass(slots=True)
class ListenConfig:
    # 听完再说：等对方把话说完再一起回；群里没人叫她时，按话题和她的兴趣决定要不要插话。
    # 关掉就回到原来的做法：私聊每条都回，群里 @ 她或者在跟进窗口里才回。
    enabled: bool = True
    # 同一个人连着发时，最后一条之后安静这么久才开始回；中间又来消息就重新等。
    quiet_seconds: float = 2.0
    # 最早一条还没回的消息已经等了这么久，就不再等，直接回。
    max_wait_seconds: float = 8.0
    # 群里她刚回过的人，这么久之内接着说的话都算在跟她说（不用 @）。
    continuation_seconds: float = 60.0
    # 群里没人叫她时，大家安静这么久，她才判断要不要插话。
    group_quiet_seconds: float = 4.0
    # 插话的门槛（0 到 1）：最近的聊天和她的兴趣（人物卡 meta 的 interests，没有就用人设）、
    # 和她刚说过的话有多相关。过了门槛才调模型，模型也可以决定不说。
    # text-embedding-v4 上相关的话约 0.42–0.64，无关的约 0.19–0.37。
    chime_in_threshold: float = 0.40
    # 每个群主动插话的次数：连着最多几次、每小时回填几次；和她上一次在这个群说话至少隔多久。
    chime_in_burst: int = 2
    chime_in_per_hour: float = 6.0
    chime_in_min_gap_seconds: float = 180.0


@dataclass(slots=True)
class TavernConfig:
    # 酒馆模式：@机器人说「启动酒馆」列出剧本，说的人当房主，「选 N」开本，机器人改当主持人带大家玩。
    # 剧本在 data_root 下 tavern/scenarios/（自己写的 YAML，或 SillyTavern 角色卡 .json/.png），另有几个自带的。
    enabled: bool = True
    # 有人行动后最多等这么久，把这段时间所有玩家的行动合在一起，主持人一次写完。
    batch_seconds: float = 10.0
    # 每个玩家都说过话、之后又安静了这么久，就不再等满 batch_seconds（只有一个玩家时总是这样）。
    settle_seconds: float = 2.0
    # 这么久没人说话就自动结束；菜单等房主选本最多等这么久。
    idle_timeout_seconds: float = 1800.0
    menu_timeout_seconds: float = 300.0
    max_players: int = 6
    # 发给主持人的剧情最多多少字，超出的旧剧情压成前情提要。
    history_chars: int = 8000
    # 能开酒馆的 QQ 号；空表示谁都可以。
    starter_user_ids: list[str] = field(default_factory=list)
    # 除了房主，还能强制结束的 QQ 号；tool_policy.authorized_user_ids 里的人也能。
    admin_user_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RateLimitConfig:
    # 付费和外部工具的令牌桶：每个用户一个、每个群一个，两个都有余量才放行。
    # image 管生图；network 管搜索、网页抓取、summarize（三者共用一个桶）。
    # *_burst 是桶容量（可以连续用几次），*_per_hour 是每小时回填几次；burst 为 0 关掉这一层。
    enabled: bool = True
    image_user_burst: int = 3
    image_user_per_hour: float = 10.0
    image_group_burst: int = 6
    image_group_per_hour: float = 30.0
    network_user_burst: int = 8
    network_user_per_hour: float = 60.0
    network_group_burst: int = 16
    network_group_per_hour: float = 180.0
    # 群聊回复的总阀门：每个群一个桶，回复一次用一次。两个机器人互相 @，或在跟进窗口里
    # 来回接话时，用完 burst 就只能按 per_hour 的速度回，不会一直刷屏。
    group_reply_burst: int = 8
    group_reply_per_hour: float = 60.0
    # 直接 @ 机器人的消息不占上面的群额度，改用每人一个的桶：机器人之间斗嘴把群额度
    # 用光时，真人 @ 它照样有回应；反复 @ 它的机器人也只能按这个速度回。burst 为 0 时
    # @ 也走群额度（旧行为）。
    mention_reply_burst: int = 8
    mention_reply_per_hour: float = 120.0
    # 这些 QQ 号不受限制（例如机器人主人）。
    exempt_user_ids: list[str] = field(default_factory=list)


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
class RuntimeV2Config:
    enabled: bool = False
    private_user_ids: list[str] = field(default_factory=list)
    workers: int = 4
    max_pending_events: int = 1000
    context_window_tokens: int = 16384
    max_output_tokens: int = 1024
    image_token_reserve: int = 2048
    max_context_images: int = 4
    openai_instruction_role: str = "system"
    openai_output_token_parameter: str = "max_completion_tokens"
    structured_memory_enabled: bool = False
    reminders_enabled: bool = False
    reminder_timezone: str = ""
    reminder_grace_seconds: int = 300


@dataclass(slots=True)
class AppConfig:
    service_name: str = "waifu-standalone"
    config_path: str = ""
    assistant_name: str = "Assistant"
    character: str = "default"
    bot_account_id: str = ""
    group_reply_requires_mention: bool = True
    skills_enabled: bool = True
    # 只在消息看起来需要时才把工具交给模型（天气、搜索、改称呼、链接……）；普通闲聊不带工具。
    tools_on_demand: bool = True
    max_active_skills: int = 3
    search_enabled: bool = True
    search_result_limit: int = 3
    search_timeout_seconds: float = 8.0
    # 回复前先让模型写一段内心分析再塞回提示词：多一次调用、多 2–3 秒，回复也更套路，默认关。
    thinking_mode: bool = False
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
    # 这些 QQ 号的消息永远不触发回复（例如群里的其他机器人），但仍作为群聊上下文保留。
    ignore_user_ids: list[str] = field(default_factory=list)
    # 能在聊天里说「切换xxx」换人物卡的 QQ 号；空表示谁都可以。换卡对所有聊天生效。
    character_switch_user_ids: list[str] = field(default_factory=list)
    # Commands that still run although they start with an ignored prefix ("/skill weather-command 上海").
    explicit_skill_prefixes: list[str] = field(default_factory=lambda: ["/skill"])
    group_follow_up_window_seconds: float = 5.0
    group_response_delay_seconds: float = 0.0
    repeat_trigger_count: int = 0
    multimodal_enabled: bool = True
    # Longer replies are sent as several QQ messages; 0 sends one message whatever the length.
    outbound_message_max_chars: int = 900
    history_window_messages: int = 30
    history_window_chars: int = 2500
    memory_recall_limit: int = 3
    max_thinking_words: int = 80
    short_term_memory_limit: int = 30
    memory_summary_batch_size: int = 12
    # IANA name; used for the prompt's current time and for dating extracted memories.
    timezone: str = "Asia/Shanghai"
    data_root: str = "data"
    llm: LLMConfig = field(default_factory=LLMConfig)
    image_generation: ImageGenerationConfig = field(default_factory=ImageGenerationConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    marketplace: MarketplaceConfig = field(default_factory=MarketplaceConfig)
    tool_policy: ToolPolicyConfig = field(default_factory=ToolPolicyConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    segmented_reply: SegmentedReplyConfig = field(default_factory=SegmentedReplyConfig)
    stickers: StickerConfig = field(default_factory=StickerConfig)
    tavern: TavernConfig = field(default_factory=TavernConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    # 群里回复时，如果那条消息之后又有人说过话，就引用它（QQ 里显示为「回复 xx」），
    # 看得出在回谁；紧接着回的不引用，跟真人一样。
    group_reply_quote: bool = True
    claw_runtime: ClawRuntimeConfig = field(default_factory=ClawRuntimeConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    qq_sidecar: QQSidecarConfig = field(default_factory=QQSidecarConfig)
    runtime_v2: RuntimeV2Config = field(default_factory=RuntimeV2Config)
