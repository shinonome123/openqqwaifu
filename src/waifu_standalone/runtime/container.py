from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..character import CardManager
from ..config import AppConfig
from ..contracts import OutboundPort
from ..gateways.napcat_login import NapCatLoginBridge
from ..gateways.onebot_actions import OneBotActionClient, OneBotHttpOutboundPort
from ..gateways.onebot_ws import OneBotWsGateway, OneBotWsOutboundPort
from ..infra import (
    AsyncRuntime,
    EmbeddingClient,
    MetricsRegistry,
    build_claw_runtime_bridge,
    build_embedding_client,
    build_image_client,
    build_llm_client,
    set_active_metrics_registry,
)
from ..memory import FileMemoryStore, InMemoryStore, Memory
from ..models import SessionMemory
from ..organs.memory_graph import MemoryGraphBuilder
from ..organs.proactive import ProactivePlanner
from ..organs.thoughts import Thoughts
from ..repositories import InMemoryRuntimeStateStore, SqliteRuntimeStateStore
from ..reply_flow import Generator
from ..runtime.testing import CapturingOutboundPort
from ..systems.emotions import EmotionSensor
from ..systems.events import BehaviorEventEngine
from ..systems.searching import SearchDecider
from ..skills import (
    MarketplaceClient,
    OPENCLAW_TOOL_ALIASES,
    PluginContext,
    SkillRegistry,
    ToolRegistry,
    load_tool_plugins,
)
from ..systems.value_game import ValueGameEngine

if TYPE_CHECKING:
    from ..app import WaifuService


_LOGGER = logging.getLogger(__name__)
_ASYNC_RUNTIME = AsyncRuntime()


def build_default_service(config: AppConfig | None = None) -> tuple["WaifuService", CapturingOutboundPort]:
    app_config = config or AppConfig()
    metrics = MetricsRegistry(service_name=str(app_config.service_name or "openqqwaifu"))
    set_active_metrics_registry(metrics)
    initial_character = _initial_character_id(app_config)
    memory_builder = lambda character_id: InMemoryStore(scoped_character_id=character_id)
    state_builder = lambda character_id: InMemoryRuntimeStateStore(embedder=_build_embedding_client(app_config))
    return _build_service(
        app_config,
        metrics,
        memory_builder(initial_character),
        state_builder(initial_character),
        CapturingOutboundPort(),
        memory_store_builder=memory_builder,
        state_store_builder=state_builder,
    )


def build_file_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple["WaifuService", CapturingOutboundPort]:
    app_config = config or AppConfig()
    metrics = MetricsRegistry(service_name=str(app_config.service_name or "openqqwaifu"))
    set_active_metrics_registry(metrics)
    session_root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    state_root = Path(app_config.data_root) / "state" / "characters"
    initial_character = _initial_character_id(app_config)
    memory_builder = lambda character_id: FileMemoryStore(
        session_root / character_id,
        scoped_character_id=character_id,
    )
    state_builder = lambda character_id: SqliteRuntimeStateStore(
        state_root / character_id / "runtime.sqlite3",
        embedder=_build_embedding_client(app_config),
    )
    return _build_service(
        app_config,
        metrics,
        memory_builder(initial_character),
        state_builder(initial_character),
        CapturingOutboundPort(),
        memory_store_builder=memory_builder,
        state_store_builder=state_builder,
    )


def build_runtime_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple["WaifuService", OutboundPort]:
    app_config = config or AppConfig()
    metrics = MetricsRegistry(service_name=str(app_config.service_name or "openqqwaifu"))
    set_active_metrics_registry(metrics)
    session_root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    outbound, reverse_ws_gateway = _build_runtime_outbound(app_config)
    state_root = Path(app_config.data_root) / "state" / "characters"
    initial_character = _initial_character_id(app_config)
    memory_builder = lambda character_id: FileMemoryStore(
        session_root / character_id,
        scoped_character_id=character_id,
    )
    state_builder = lambda character_id: SqliteRuntimeStateStore(
        state_root / character_id / "runtime.sqlite3",
        embedder=_build_embedding_client(app_config),
    )
    return _build_service(
        app_config,
        metrics,
        memory_builder(initial_character),
        state_builder(initial_character),
        outbound,
        reverse_ws_gateway=reverse_ws_gateway,
        memory_store_builder=memory_builder,
        state_store_builder=state_builder,
    )


def _build_runtime_outbound(
    app_config: AppConfig,
    *,
    existing_gateway: OneBotWsGateway | None = None,
) -> tuple[OutboundPort, OneBotWsGateway | None]:
    gateway_mode = str(app_config.qq_sidecar.gateway_mode or "http").strip().lower()
    if gateway_mode == "reverse_ws":
        gateway = existing_gateway or OneBotWsGateway(
            access_token=app_config.qq_sidecar.reverse_ws_access_token or app_config.qq_sidecar.access_token,
            send_timeout_seconds=app_config.qq_sidecar.reverse_ws_send_timeout_seconds,
        )
        gateway.configure(
            access_token=app_config.qq_sidecar.reverse_ws_access_token or app_config.qq_sidecar.access_token,
            send_timeout_seconds=app_config.qq_sidecar.reverse_ws_send_timeout_seconds,
        )
        return OneBotWsOutboundPort(gateway), gateway
    if not app_config.qq_sidecar.outbound_base_url:
        return CapturingOutboundPort(), None
    client = OneBotActionClient(
        base_url=app_config.qq_sidecar.outbound_base_url,
        timeout=app_config.qq_sidecar.outbound_timeout_seconds,
        access_token=app_config.qq_sidecar.access_token,
    )
    return OneBotHttpOutboundPort(client), None


def _build_embedding_client(app_config: AppConfig) -> EmbeddingClient:
    return build_embedding_client(app_config)


def _build_napcat_login_bridge(app_config: AppConfig) -> NapCatLoginBridge:
    return NapCatLoginBridge(
        base_url=app_config.qq_sidecar.webui_base_url,
        api_prefix=app_config.qq_sidecar.webui_api_prefix,
        webui_token=app_config.qq_sidecar.webui_token,
        timeout=app_config.qq_sidecar.webui_timeout_seconds,
    )


def _initial_character_id(app_config: AppConfig) -> str:
    return str(CardManager(app_config).active_character() or app_config.character or "default").strip() or "default"


def _build_service(
    app_config: AppConfig,
    metrics: MetricsRegistry,
    store: Any,
    state_store: Any,
    outbound: OutboundPort,
    *,
    reverse_ws_gateway: OneBotWsGateway | None = None,
    memory_store_builder: Callable[[str], Any] | None = None,
    state_store_builder: Callable[[str], Any] | None = None,
) -> tuple["WaifuService", OutboundPort]:
    from ..app import WaifuService

    set_active_metrics_registry(metrics)
    generator = Generator(
        app_config,
        llm_client=build_llm_client(app_config),
        image_client=build_image_client(app_config),
    )
    cards = generator._cards
    skills = SkillRegistry(app_config)
    tools = ToolRegistry()
    claw_runtime = build_claw_runtime_bridge(app_config.claw_runtime, data_root=app_config.data_root)
    if app_config.plugins.enabled:
        load_tool_plugins(
            PluginContext(
                app_config=app_config,
                tool_registry=tools,
                skill_registry=skills,
                logger=logging.getLogger("waifu.plugins"),
                metrics=metrics,
            ),
            disabled=set(app_config.plugins.disabled_names),
        )
    service = WaifuService(
        config=app_config,
        metrics=metrics,
        memory=Memory(store),
        emotions=EmotionSensor(),
        thoughts=Thoughts(app_config, generator),
        generator=generator,
        cards=cards,
        search=SearchDecider(app_config),
        event_engine=BehaviorEventEngine(app_config),
        value_game=ValueGameEngine(app_config),
        memory_graph=MemoryGraphBuilder(app_config),
        proactive=ProactivePlanner(app_config),
        marketplace=MarketplaceClient(app_config.marketplace),
        skills=skills,
        tools=tools,
        claw_runtime=claw_runtime,
        state_store=state_store,
        outbound=outbound,
        reverse_ws_gateway=reverse_ws_gateway,
        napcat_login=_build_napcat_login_bridge(app_config),
        _memory_store_builder=memory_store_builder,
        _state_store_builder=state_store_builder,
    )
    service.memory.set_character_resolver(service.cards.active_character)
    service._bound_character_id = service.cards.active_character()
    if hasattr(service.state_store, "set_default_character"):
        service.state_store.set_default_character(service.cards.active_character())
    service.migrator.repair_character_isolation(service.cards.active_character())
    if hasattr(service.state_store, "refresh_knowledge_embeddings"):
        service.state_store.refresh_knowledge_embeddings()
    tools.register(
        "image",
        name="图片生成",
        description="调用图像生成能力并返回图文消息。",
        handler=service.dispatcher.run_image_tool,
        async_handler=service.dispatcher.arun_image_tool,
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "要生成图片的提示词。"},
            },
            "required": ["prompt"],
        },
        model_handler=service.dispatcher.use_image_tool,
        async_model_handler=service.dispatcher.ause_image_tool,
        risk_level="mutating",
    )
    tools.register(
        "search",
        name="联网搜索",
        description="执行联网检索并把摘要整理成回复。",
        handler=service.dispatcher.run_search_tool,
        async_handler=service.dispatcher.arun_search_tool,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜索的关键词。"},
            },
            "required": ["query"],
        },
        model_handler=service.dispatcher.use_search_tool,
        async_model_handler=service.dispatcher.ause_search_tool,
        risk_level="safe",
    )
    tools.register(
        "summary",
        name="会话总结",
        description="总结最近会话并提取重点标签。",
        handler=service.dispatcher.run_summary_tool,
        async_handler=service.dispatcher.arun_summary_tool,
        parameters={"type": "object", "properties": {}},
        model_handler=service.dispatcher.use_summary_tool,
        async_model_handler=service.dispatcher.ause_summary_tool,
        risk_level="safe",
    )
    tools.register(
        "skill-list",
        name="技能列表",
        description="列出当前所有已启用的技能及其触发方式。",
        handler=service.dispatcher.run_skill_list_tool,
        async_handler=service.dispatcher.arun_skill_list_tool,
        parameters={"type": "object", "properties": {}},
        model_handler=service.dispatcher.use_skill_list_tool,
        async_model_handler=service.dispatcher.ause_skill_list_tool,
        risk_level="safe",
    )
    tools.register(
        "summarize",
        name="外部内容总结",
        description="调用 summarize CLI 总结 URL、视频或本地文件。",
        handler=service.dispatcher.run_summarize_tool,
        async_handler=service.dispatcher.arun_summarize_tool,
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "要总结的 URL 或文件路径。"},
            },
            "required": ["target"],
        },
        model_handler=service.dispatcher.use_summarize_tool,
        async_model_handler=service.dispatcher.ause_summarize_tool,
        risk_level="safe",
    )
    tools.register(
        "weather",
        name="天气查询",
        description="查询指定城市或地区的当前天气和近两天天气趋势，不需要额外 API key。",
        handler=service.dispatcher.run_weather_tool,
        async_handler=service.dispatcher.arun_weather_tool,
        parameters={
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "要查询天气的城市、地区或地名。"},
            },
            "required": ["location"],
        },
        model_handler=service.dispatcher.use_weather_tool,
        async_model_handler=service.dispatcher.ause_weather_tool,
        risk_level="safe",
    )
    tools.register(
        "search-links",
        name="搜索来源链接",
        description="返回最近一次搜索或指定查询对应的来源链接。",
        handler=service.dispatcher.run_search_links_tool,
        async_handler=service.dispatcher.arun_search_links_tool,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "可选。如果提供则先执行搜索再返回来源链接。"},
            },
        },
        model_handler=service.dispatcher.use_search_links_tool,
        async_model_handler=service.dispatcher.ause_search_links_tool,
        risk_level="safe",
    )
    tools.register(
        "image-caption",
        name="生图交付文案",
        description="为刚生成的图片生成一句交付文案。",
        handler=service.dispatcher.run_image_caption_tool,
        async_handler=service.dispatcher.arun_image_caption_tool,
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "图片生成时使用的提示词。"},
            },
            "required": ["prompt"],
        },
        model_handler=service.dispatcher.use_image_caption_tool,
        async_model_handler=service.dispatcher.ause_image_caption_tool,
        model_callable=False,
        risk_level="safe",
    )
    tools.register(
        "web-fetch",
        name="网页抓取",
        description="抓取指定 URL 的正文文本。",
        handler=service.dispatcher.run_web_fetch_tool,
        async_handler=service.dispatcher.arun_web_fetch_tool,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的 http 或 https 链接。"},
                "max_chars": {"type": "integer", "description": "返回文本的最大字符数。"},
            },
            "required": ["url"],
        },
        model_handler=service.dispatcher.use_web_fetch_tool,
        async_model_handler=service.dispatcher.ause_web_fetch_tool,
        risk_level="safe",
    )
    tools.register(
        "read-file",
        name="读取文件",
        description="读取工作区或数据目录内的本地文本文件。",
        handler=service.dispatcher.run_read_file_tool,
        async_handler=service.dispatcher.arun_read_file_tool,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径。"},
                "max_chars": {"type": "integer", "description": "返回内容的最大字符数。"},
            },
            "required": ["path"],
        },
        model_handler=service.dispatcher.use_read_file_tool,
        async_model_handler=service.dispatcher.ause_read_file_tool,
        risk_level="filesystem",
    )
    tools.register(
        "list-files",
        name="列出文件",
        description="列出工作区或数据目录内的目录内容。",
        handler=service.dispatcher.run_list_files_tool,
        async_handler=service.dispatcher.arun_list_files_tool,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径。"},
                "recursive": {"type": "boolean", "description": "是否递归列出子目录。"},
                "limit": {"type": "integer", "description": "最多返回多少条路径。"},
            },
        },
        model_handler=service.dispatcher.use_list_files_tool,
        async_model_handler=service.dispatcher.ause_list_files_tool,
        risk_level="filesystem",
    )
    tools.register(
        "write-file",
        name="写入文件",
        description="向工作区或数据目录内的文件写入文本内容。",
        handler=service.dispatcher.run_write_file_tool,
        async_handler=service.dispatcher.arun_write_file_tool,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要写入的文件路径。"},
                "content": {"type": "string", "description": "要写入的文本。"},
                "append": {"type": "boolean", "description": "是否追加写入。"},
            },
            "required": ["path", "content"],
        },
        model_handler=service.dispatcher.use_write_file_tool,
        async_model_handler=service.dispatcher.ause_write_file_tool,
        model_callable=True,
        risk_level="filesystem",
    )
    tools.register(
        "exec-command",
        name="执行命令",
        description="在允许目录内执行非 shell 的本地命令。",
        handler=service.dispatcher.run_exec_command_tool,
        async_handler=service.dispatcher.arun_exec_command_tool,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "命令字符串。"},
                "argv": {"type": "array", "items": {"type": "string"}, "description": "结构化参数数组。"},
                "cwd": {"type": "string", "description": "执行目录。"},
                "timeout_seconds": {"type": "number", "description": "超时时间。"},
            },
        },
        model_handler=service.dispatcher.use_exec_command_tool,
        async_model_handler=service.dispatcher.ause_exec_command_tool,
        model_callable=True,
        risk_level="command",
    )
    tools.register(
        "set-preferred-name",
        name="设置用户称呼",
        description="当用户明确要求机器人以后怎么称呼自己时，保存当前用户的 preferred_name。",
        handler=lambda invocation: service.dispatcher._emit_tool_execution_result(
            invocation,
            service.onboarding.use_set_preferred_name_tool(invocation),
        ),
        async_handler=lambda invocation: service.dispatcher._aemit_tool_execution_result(
            invocation,
            service.onboarding.use_set_preferred_name_tool(invocation),
        ),
        parameters={
            "type": "object",
            "properties": {
                "preferred_name": {"type": "string", "description": "用户要求保存的短称呼，只填称呼本身。"},
            },
            "required": ["preferred_name"],
        },
        model_handler=service.onboarding.use_set_preferred_name_tool,
        async_model_handler=service.onboarding.ause_set_preferred_name_tool,
        risk_level="mutating",
    )
    tools.register(
        "set-assistant-alias",
        name="设置机器人别名",
        description="当用户明确说以后要怎么叫机器人时，按当前角色和当前用户保存 assistant_alias。",
        handler=lambda invocation: service.dispatcher._emit_tool_execution_result(
            invocation,
            service.onboarding.use_set_assistant_alias_tool(invocation),
        ),
        async_handler=lambda invocation: service.dispatcher._aemit_tool_execution_result(
            invocation,
            service.onboarding.use_set_assistant_alias_tool(invocation),
        ),
        parameters={
            "type": "object",
            "properties": {
                "assistant_alias": {"type": "string", "description": "用户给机器人起的短称呼，只填称呼本身。"},
            },
            "required": ["assistant_alias"],
        },
        model_handler=service.onboarding.use_set_assistant_alias_tool,
        async_model_handler=service.onboarding.ause_set_assistant_alias_tool,
        risk_level="mutating",
    )
    tools.register(
        "reject-third-party-naming",
        name="拒绝代第三方命名",
        description="当用户试图替别人决定称呼，或要求机器人以后叫其他人某个名字时调用；不能保存任何称呼。",
        handler=lambda invocation: service.dispatcher._emit_tool_execution_result(
            invocation,
            service.onboarding.use_reject_third_party_naming_tool(invocation),
        ),
        async_handler=lambda invocation: service.dispatcher._aemit_tool_execution_result(
            invocation,
            service.onboarding.use_reject_third_party_naming_tool(invocation),
        ),
        parameters={"type": "object", "properties": {}},
        model_handler=service.onboarding.use_reject_third_party_naming_tool,
        async_model_handler=service.onboarding.ause_reject_third_party_naming_tool,
        risk_level="safe",
    )
    tools.register(
        "clarify-naming-intent",
        name="澄清命名意图",
        description="当用户在问称呼规则或询问当前叫法，但没有明确命名时调用；不能保存任何称呼。",
        handler=lambda invocation: service.dispatcher._emit_tool_execution_result(
            invocation,
            service.onboarding.use_clarify_naming_intent_tool(invocation),
        ),
        async_handler=lambda invocation: service.dispatcher._aemit_tool_execution_result(
            invocation,
            service.onboarding.use_clarify_naming_intent_tool(invocation),
        ),
        parameters={
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["user_name", "assistant_alias"],
                    "description": "澄清方向：用户自己的称呼，或机器人别名。",
                },
            },
        },
        model_handler=service.onboarding.use_clarify_naming_intent_tool,
        async_model_handler=service.onboarding.ause_clarify_naming_intent_tool,
        risk_level="safe",
    )
    tools.register_alias("set_preferred_name", "set-preferred-name")
    tools.register_alias("set_assistant_alias", "set-assistant-alias")
    tools.register_alias("reject_third_party_naming", "reject-third-party-naming")
    tools.register_alias("clarify_naming_intent", "clarify-naming-intent")
    _register_tool_aliases(tools)
    generator.bind_tools(tools)
    return service, outbound


def _register_tool_aliases(tools: ToolRegistry) -> None:
    for alias, target in OPENCLAW_TOOL_ALIASES.items():
        try:
            tools.register_alias(alias, target)
        except ValueError:
            _LOGGER.debug("skipping unresolved tool alias %s -> %s", alias, target)
