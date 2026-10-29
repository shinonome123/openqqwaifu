# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**openqqwaifu** is a standalone QQ Waifu console and runtime that integrates with NapCat/OneBot for QQ messaging. It owns all business logic (character cards, memory, skills, model calls, web console) while delegating QQ protocol handling to a NapCat sidecar.

- Language: Python 3.11+
- Build system: setuptools with `src/` layout
- No external linting/formatting tools configured

## Common Commands

Tests run in a temporary working directory (`tests/test__isolated_working_directory.py`), so a default `AppConfig()` never reads or writes the live `data/`; don't make a test depend on the working directory being the repository.

```bash
# Run all tests
python -B -m unittest discover -s tests -v

# Run a single test file
python -B -m unittest tests.test_app -v

# Run a single test method
python -B -m unittest tests.test_app.TestWaifuService.test_method_name -v

# Generate default config
python run_cli.py dump-config data/config.json

# Start the server (local)
python run_cli.py serve --config data/config.json

# Start the server in OneBot reverse-WS mode (NapCat connects back to /onebot/v11/ws)
OPENQQWAIFU_QQ_SIDECAR_GATEWAY_MODE=reverse_ws python run_cli.py serve --config examples/config.napcat.local.json

# Check NapCat sidecar connectivity
python run_cli.py check-sidecar --config data/config.json

# Export/import skill packs
python run_cli.py export-skill-pack --config data/config.json --output pack.json
python run_cli.py import-skill-pack --config data/config.json --input pack.json

# Docker Compose deployment (with NapCat sidecar)
docker compose -f compose.napcat.yml up -d
```

## Architecture

### Responsibility Split

- **openqqwaifu** owns: character cards, memory, skills, LLM/image/embedding calls, web console, event processing
- **NapCat** (sidecar) owns: QQ login, message send/receive, protocol compatibility, reconnection

### Source Layout (`src/waifu_standalone/`)

| Module | Purpose |
|---|---|
| `app.py` | Central `WaifuService` orchestration: event handling, dashboard, character panel, runtime behavior |
| `cli.py` | CLI entrypoint (`serve`, `dump-config`, `import-waifu`, `check-sidecar`, skill pack import/export) |
| `config.py` | Dataclass-based `AppConfig` with nested configs for QQ sidecar, LLM, image generation, embeddings, marketplace |
| `http_api.py` | HTTP server layer (health at `/healthz`, OneBot callbacks, web console API) |
| `state_store.py` | Persistent state for sessions, members, knowledge, runtime |
| `memory.py` | `FileMemoryStore` for session history persistence |
| `models.py` | Domain objects: inbound events, message segments |
| `services.py` | Service interfaces and outbound ports (re-exports `CapturingOutboundPort` from `runtime/testing.py`, used for dry-run and offline mode) |
| `contracts.py` | Shared interface/contract types |
| `migration.py` | Legacy waifu data import via `WaifuDataImporter` |
| `schema_migrations.py` | Schema-version driven sqlite migrations for `state_store.py` runtime storage |
| `plugin_api.py` | Tool-plugin discovery via Python entry points (`openqqwaifu.tools`) with a minimal `PluginContext` |
| `gateways/onebot_ws.py` | OneBot v11 reverse-WebSocket gateway -- inbound events and outbound actions share a single NapCat-initiated WS connection. Enabled via `qq_sidecar.gateway_mode = "reverse_ws"` |
| `console_panels.py` | Web console panel rendering (dashboard, session list, session detail) — delegated from `WaifuService.console` |
| `knowledge_curator.py` | Auto-extracted knowledge writeback, scope resolution, directory notes — delegated from `WaifuService.knowledge` |
| `notice_dispatcher.py` | OneBot notice payload routing (group increase/decrease/card) — delegated from `WaifuService.notice` |
| `reply_gate.py` | Reply gating, group follow-up window, pending-search confirmation, repeat detection — delegated from `WaifuService.gate` |
| `skill_dispatcher.py` | Skill/tool dispatch for image, search, summary and skill-list commands — delegated from `WaifuService.dispatcher` |
| `outbound_emitter.py` | Outbound message send with optional delay and persistence hooks — delegated from `WaifuService.emitter` |
| `member_onboarding.py` | New-member preferred-name flow (ask → confirm → save to directory) — delegated from `WaifuService.onboarding` |
| `legacy_migrator.py` | One-time migration from legacy session metadata to current state_store schema, and character-isolation repair — delegated from `WaifuService.migrator` |
| `persona_guard.py` | Assistant-alias bookkeeping and member-profile sanitization (prevents cross-character persona leakage) — delegated from `WaifuService.persona` |

### Subsystem Directories

- **`cells/`** - Provider integrations and registries: `dify_service.py` (LLM backend), `xai_image_service.py` (image generation), `embedding_service.py`, `skill_registry.py`, `skill_pack.py`, `tool_registry.py`, `marketplace.py`, `auth.py`, `config.py` (ConfigManager with env-var overlay)
- **`organs/`** - Higher-level cognition: `memories.py` (memory recall/summary), `thoughts.py` (thinking mode), `memory_graph.py` (graph-based memory), `proactive.py` (proactive messaging)
- **`systems/`** - Functional subsystems: `emotions.py`, `searching.py`, `events.py` (event buffering), `narrator.py` (narrator mode), `value_game.py` (per-conversation bond with each person; its stage and the card meta's `tone_by_stage` set her tone in `reply_flow/service.py`)
- **`gateways/`** - External protocol adapters: `onebot_http.py`, `onebot_actions.py` (OneBot action client), `onebot_ws.py` (reverse-WS inbound/event + outbound/action bridge), `napcat_login.py` (NapCat WebUI bridge for QQ login)
- **`tavern/`** - Tavern (tabletop story game) mode, played by SillyTavern itself: `mode.py` (chat commands, gathering players' actions into one round, idle timeout, saves, the content note), `sillytavern.py` (drives the SillyTavern 1.19 page that the `browser` container keeps open, over Chrome's DevTools protocol, through `sillytavern_page.js`, which calls SillyTavern's own functions; on first use it sets SillyTavern's connection to `tavern.llm_*`, imports the bundled `templates/tavern/*.json` game-master cards and the card files in `data/tavern/scenarios/` and `adult/`, and moves old saves into SillyTavern chats; state in `data/tavern/sillytavern.json`), `scenarios.py` (what the menu and content rule read from a SillyTavern character: intro, adult, minors, status-bar label, planned rounds), `session.py` (running games in `data/tavern/sessions/`, finished ones in `archive/`; a game is a SillyTavern chat, `TavernSession.chat`), `cards.py` (a card an admin sends as a QQ file in private chat is downloaded, checked, kept and imported into SillyTavern), `dice.py` (program-rolled dice). Hooked in at the top of `reply_flow/service.py`; tests drive it through a fake page (`tests/test_tavern.py::_SillyTavern`). `tavern/soup.py` is 海龟汤 (yes/no riddles judged by the chat model; riddles in `templates/turtle_soup.json` plus `data/turtle_soup/*.json`), hooked in right after the tavern; the two never share a chat
- **`web/`** - Static frontend: `dashboard.html` + JS/CSS assets with pages for overview, AI config, character editing, events, members, memory, sidecar status, skills, QQ login, abilities, advanced settings
- **`templates/`** - YAML character templates (`default_group.yaml`, `default_person.yaml`)
- **`builtin_skills/`** - Markdown skill definitions (concise-answer, image-command, search-command, etc.)

### Configuration System

`ConfigManager` loads config from JSON file, then overlays environment variables prefixed with `OPENQQWAIFU_` (e.g., `OPENQQWAIFU_QQ_SIDECAR_WEBUI_TOKEN`). Config is a nested `AppConfig` dataclass tree. Use `dump-config` to generate a reference file with all defaults.

### Memory System (Three Layers)

1. **`session_history`** - Per-launcher conversation logs
2. **`user_directory`** - Stable member info (updated via group sync, admin edits, first-interaction name confirmation -- not freely rewritten by the model)
3. **`knowledge_base`** - Model-extractable facts (via structured extraction only)

### Deployment Modes

- **Local**: `python run_cli.py serve --config data/config.json` (NapCat runs separately)
- **Docker Compose**: `compose.napcat.yml` runs `openqqwaifu` (port 8080), `napcat`, and for the tavern `sillytavern` (its page on 127.0.0.1:8000) and `browser` (headless Chrome) on their own `tavern` network
- Example configs: `examples/config.napcat.local.json` (local), `examples/config.napcat.compose.json` (compose)

## Key Design Patterns

- `QQSidecarConfig.dry_run = True` (env `OPENQQWAIFU_QQ_SIDECAR_DRY_RUN`) makes `build_runtime_service()` capture outbound messages in a `CapturingOutboundPort` instead of sending them, even when `outbound_base_url` is set; in `reverse_ws` mode NapCat still connects for inbound events. Everything else runs as live (real model calls, no offline fallbacks) -- useful for rehearsing against real QQ traffic without replying
- The service uses `build_default_service()` for offline/CLI operations and `build_runtime_service()` for the live server with real outbound connectivity
- Skills are markdown files that get registered in `skill_registry` and can be bundled/shared via skill packs
- Background tasks (`reply_flow/tasks.py`, `tasks` config): the `start-task` tool hands a multi-step job to a background run with every tool the sender may use and `tasks.max_rounds` model calls (`ToolExposureContext.max_rounds`; a reply gets 3), which sends the result to the chat; one per chat. The `chat-log` tool reads `memory/chat_log.py`'s per-day log of every line in the current chat (`data/chatlog/`, 90 days); once a day is over the companion clock has `memory/diary.py` write each chat's day into a short entry kept for good (`digests.json` beside the log), which the `recall` tool reads by date and keyword
- In a private chat someone talking to her also gets `private_offered_tool_ids` (drawing) besides `always_offered_tool_ids`; in groups drawing still needs the wording to ask for it
- Listen-first (`reply_flow/listener.py`, `listen` config, on by default): a person is answered once they stop typing (superseded messages get no reply of their own); in groups the bot answers @, its name, and for a while the person it just answered; other talk is scored against the card's `interests` after the group goes quiet and may get a spontaneous reply (`handle_event_async_locked(spontaneous=True)`), where the model may answer `[不说话]`. Tests that exercise the old group-wide follow-up window set `listen.enabled=False`
- Companion (`reply_flow/companion.py`, `companion` config): the `reminder` tool (always offered with the lookup tools) saves a note with a time to `data/companion/state.json`, and a once-a-minute clock started by the first inbound message has her say it when it is due; the `watch` tool ("帮我盯着…") has her search for something every few hours within `greeting_hours` and say only what the chat model judges new (the first search just learns what is known); greetings (`greetings_enabled`, off by default) let her open a quiet private chat or group herself within hours, daily limits and backoff when nobody answers. Tests set `service.companion.autostart = False` and call `tick(now)`
- The web console is served as static files bundled as package data
