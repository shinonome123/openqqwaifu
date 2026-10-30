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
    # 群里 @ 她或叫她名字的人，她回过之后这么久之内接着说的话也算在跟她说（不用 @）。
    # 她回这些接着说的话不会延长这段时间；对方这时 @ 别人的话不算。
    continuation_seconds: float = 30.0
    # 群里有人叫她「别说话」「闭嘴」时，她在这个群安静这么久：不主动插话、不接没 @ 她的话。
    quiet_minutes: float = 30.0
    # 群里没人叫她时，大家安静这么久，她才判断要不要插话。
    group_quiet_seconds: float = 4.0
    # 插话的门槛（0 到 1）：最近的聊天和她的兴趣（人物卡 meta 的 interests，没有就用人设）、
    # 和她刚说过的话有多相关。过了门槛才调模型，模型也可以决定不说。
    # text-embedding-v4 上相关的话约 0.42–0.64，无关的约 0.19–0.37。
    chime_in_threshold: float = 0.45
    # 每个群主动插话的次数：连着最多几次、每小时回填几次；和她上一次在这个群说话至少隔多久。
    chime_in_burst: int = 1
    chime_in_per_hour: float = 3.0
    chime_in_min_gap_seconds: float = 600.0


@dataclass(slots=True)
class TavernConfig:
    # 酒馆模式：@机器人说「启动酒馆」列出角色卡，说的人当房主，「选 N」开局，机器人收集玩家的行动交给 SillyTavern 玩。
    # 卡由 SillyTavern 本身来玩（提示词顺序、世界书、正则、预设、调模型都是它的），回复只在发到 QQ 时转成纯文本。
    # 菜单是 SillyTavern 里的角色：自带的几张跑团卡、data_root 下 tavern/scenarios/ 和 adult/ 里的卡文件（启动时导入），
    # 以及在 SillyTavern 网页里自己加的卡。
    enabled: bool = True
    # SillyTavern 的网页地址（无头浏览器打开它），和无头浏览器的 DevTools 地址；两项都填了酒馆才能开。
    # compose 部署里是 http://sillytavern:8000/ 和 http://browser:9222。
    sillytavern_url: str = ""
    browser_url: str = ""
    # 在 QQ 酒馆操作前检查网页已保存的预设/扩展配置；变化时在本轮生成前重载，不中断正在进行的回复。
    sync_web_settings: bool = True
    # 检查节流秒数；0 表示每次操作前检查。不会同步网页里尚未保存的临时状态。
    web_sync_interval_seconds: float = 2.0
    # SillyTavern 要装的扩展（「git 地址@tag」，和它网页上「安装扩展」一样装给这个用户）；tag 改了就重装成那个版本。
    # 默认是酒馆助手（JS-Slash-Runner）：卡里带的酒馆助手脚本会跑；它默认把上下文开到 200 万，这一项会被关掉。
    sillytavern_extensions: list[str] = field(default_factory=lambda: ["https://github.com/N0VI028/JS-Slash-Runner@4.11.3"])
    # 第一次接上 SillyTavern 时给它设的上下文长度和单条回复长度（它自带的是 4k 和 300，角色卡不够用）；之后在它网页里改的为准。
    context_tokens: int = 32000
    reply_tokens: int = 2000
    # 有人行动后最多等这么久，把这段时间所有玩家的行动合在一起，一次交给 SillyTavern。
    batch_seconds: float = 10.0
    # 每个玩家都说过话、之后又安静了这么久，就不再等满 batch_seconds（只有一个玩家时总是这样）。
    settle_seconds: float = 2.0
    # 这么久没人说话就自动结束；菜单等房主选本最多等这么久。
    idle_timeout_seconds: float = 1800.0
    menu_timeout_seconds: float = 300.0
    max_players: int = 6
    # 能开酒馆的 QQ 号；空表示谁都可以。
    starter_user_ids: list[str] = field(default_factory=list)
    # 除了房主，还能强制结束的 QQ 号；tool_policy.authorized_user_ids 里的人也能。
    admin_user_ids: list[str] = field(default_factory=list)
    # 成人模式：这些 QQ 号可开启成人卡（角色必须成年、双方自愿、说停就停）。空表示不允许任何房主开启。
    adult_user_ids: list[str] = field(default_factory=list)
    # 默认仅私聊；群成员确认成年且同意接收后可开启所有群，房主仍须在 adult_user_ids 中。
    adult_in_groups: bool = False
    # 酒馆用的模型（OpenAI 兼容接口）：三项都填了就把它设成 SillyTavern 的连接，否则用 llm 那个（Dify 不行）；
    # 改了才会重新写进 SillyTavern。timeout 是等 SillyTavern 写完一轮的时间。
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 120.0
    llm_reasoning_effort: str = ""


@dataclass(slots=True)
class TurtleSoupConfig:
    # 海龟汤：说「海龟汤」出一碗（汤面），大家问能用「是」「不是」回答的问题，猜出汤底就赢。
    # 题目是自带的一批，再加 data_root 下 turtle_soup/ 里的 .json；用日常聊天那个模型当裁判。
    enabled: bool = True
    # 这么久没人问，就悄悄收掉这碗汤（群里不再把问句当成在猜汤）。
    idle_timeout_seconds: float = 1200.0


@dataclass(slots=True)
class TaskConfig:
    # 要好几步的事（「把这周群里聊的游戏整理成文件」）交给后台做，做完把结果发回聊天；在 reply_flow/tasks.py。
    enabled: bool = True
    # 后台最多调用几次模型（每次可以用一批工具）、最多做多久。
    max_rounds: int = 8
    timeout_seconds: float = 600.0
    # 所有聊天加起来同时最多做几件；每个聊天同时只做一件。
    max_running: int = 3


@dataclass(slots=True)
class CompanionConfig:
    # 她先开口：别人让她记着的事到点就说（「明天早上九点叫我」「回头问问我考得怎么样」），
    # 以及偶尔主动找熟人说句话。都在 reply_flow/companion.py。
    reminders_enabled: bool = True
    # 每个聊天最多同时记着几件事。
    reminders_per_chat: int = 20
    # 帮人长期盯一件事（「帮我盯着影之刃零的发售日，有消息告诉我」）：隔一阵自己搜一次，
    # 跟上次知道的比，真有新消息才说；只在 greeting_hours 里查和说。
    watches_enabled: bool = True
    watches_per_chat: int = 5
    # 最短隔几个小时查一次、最多盯几天。
    watch_min_hours: float = 6.0
    watch_max_days: int = 60
    # 每天过完，把每个聊天这一天的聊天记录写成一小段日记（谁聊了什么、约了什么），一直留着，
    # 她就能回想「上个月我们聊过什么」；在 memory/diary.py。
    diary_enabled: bool = True
    # 主动找人说话；默认关，打开前先看好下面这些限制。
    greetings_enabled: bool = False
    # 只在这几个钟点之间（本地时间，左闭右开）主动开口；提醒不受限，约好几点就几点。
    greeting_hours: list[int] = field(default_factory=lambda: [10, 22])
    # 私聊：关系至少到这一步（new / warming / familiar / close / devoted），对方这么多小时没说话。
    person_min_stage: str = "warming"
    person_idle_hours: float = 20.0
    # 群：安静了这么多小时；每个群每天最多几次。
    group_quiet_hours: float = 3.0
    group_daily_limit: int = 2
    # 群里也主动说话；关掉就只在私聊。
    greet_groups: bool = True
    # 所有聊天加起来每天最多主动开口几次。
    daily_limit: int = 5
    # 上一次主动开口没人理：再等这么多小时；连着这么多次没人理就不再主动，等对方先说话。
    unanswered_wait_hours: float = 72.0
    unanswered_stop: int = 2
    # 只对这些 QQ 号 / 群号主动开口；空表示都可以。
    only_ids: list[str] = field(default_factory=list)


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
    # 有人在跟她说话时（不是她自己插话），这些查东西的工具总是交给模型，由模型按意思判断要不要用：
    # 「帮我看看影之刃零评价怎么样」没有「搜」字，也该去查。其余工具仍按 tools_on_demand 看措辞。
    always_offered_tool_ids: list[str] = field(
        default_factory=lambda: ["search", "search-links", "weather", "web-fetch", "summarize", "reminder", "watch", "chat-log", "start-task", "recall"]
    )
    # 私聊里有人跟她说话时，这些也总是交给模型：「这场景要是有张图就好了」没说「画」，也该能画。
    # 群里仍按措辞给，免得她在群里动不动就生图。
    private_offered_tool_ids: list[str] = field(default_factory=lambda: ["image"])
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
    # 好感度：每次回复后按「这是不是一次新的对话」涨一点（越接近 1 涨得越慢），三天不聊开始慢慢回落，
    # 骂她会掉；所处阶段决定她对这个人的语气（角色卡 meta 的 tone_by_stage 可以按阶段写语气）。
    value_game_mode: bool = True
    # 一次新对话（距上次回这个人超过半小时）涨多少；间隔更短的按比例少涨，最少是它的五十分之一。
    value_game_reply_bonus: float = 0.04
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
    turtle_soup: TurtleSoupConfig = field(default_factory=TurtleSoupConfig)
    companion: CompanionConfig = field(default_factory=CompanionConfig)
    tasks: TaskConfig = field(default_factory=TaskConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    # 群里回复时，如果那条消息之后又有人说过话，就引用它（QQ 里显示为「回复 xx」），
    # 看得出在回谁；紧接着回的不引用，跟真人一样。
    group_reply_quote: bool = True
    claw_runtime: ClawRuntimeConfig = field(default_factory=ClawRuntimeConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    qq_sidecar: QQSidecarConfig = field(default_factory=QQSidecarConfig)
    runtime_v2: RuntimeV2Config = field(default_factory=RuntimeV2Config)
