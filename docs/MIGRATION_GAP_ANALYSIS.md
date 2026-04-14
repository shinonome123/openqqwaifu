# openqqwaifu vs LangBot Waifu Gap Analysis

Updated: 2026-04-14

## Scope

Compared:

- Local repo: `C:\Users\a7831\OneDrive\文档\Playground\openqqwaifu`
- Online plugin: `/home/ubuntu/data/langbot/docker/data/plugins/Typer_Body__Waifu!`

Method:

- Counted code with blank lines and pure comment lines removed.
- Treated Python code volume as the fairest comparison metric.
- Reviewed the runtime entry, memory, message pipeline, search, emotion, cards, sidecar, and test surfaces.

## Executive Summary

- `openqqwaifu` is ahead on product shape: standalone service, control plane, auth, sidecar boundary, skill system, marketplace, and testability.
- The online `langbot + waifu` plugin is ahead on runtime depth: group reply rules, long-term memory, member profiles, event injection, proactive behavior, narrator mode, and value game.
- Replacement is not blocked by architecture anymore. It is blocked by missing runtime parity.

## Code Volume

| Metric | openqqwaifu | Online waifu plugin | Notes |
| --- | ---: | ---: | --- |
| Total files | 62 | 393 | Online tree includes runtime data, backups, and generated artifacts |
| Total code lines | 12,638 | 33,173 | Raw total favors the online plugin but includes non-source assets |
| Python files | 30 | 149 | Better proxy for business logic depth |
| Python code lines | 5,462 | 18,718 | Online runtime logic is about 3.4x larger |
| Web/UI code lines | 7,065 | ~0 | `openqqwaifu` has a real control plane, the plugin does not |
| Tests | 80 cases, 79 pass / 1 stale failure | No comparable local test suite found | The one local failure is an outdated dashboard title assertion |

## Code Concentration

| System | Area | Code lines | What it means |
| --- | --- | ---: | --- |
| openqqwaifu | `src/waifu_standalone/web/` | 7,065 | Heavy investment in admin UI and product surface |
| openqqwaifu | `app.py` + `http_api.py` | 2,017 | Strong standalone runtime and control API boundary |
| openqqwaifu | `cells/` | 2,270 | Config, cards, providers, skills, marketplace |
| Online plugin | `organs/memories.py` | 1,560 | Memory subsystem is the thickest missing runtime slice |
| Online plugin | `components/event_listener/waifu_listener.py` | 1,090 | Message behavior and group flow are mostly here |
| Online plugin | `systems/` | 1,305 | Emotion, search, events, narrator, value game are mature |

## Capability Matrix

| Capability | openqqwaifu | Online plugin | Lead |
| --- | --- | --- | --- |
| Standalone runtime | Complete | Depends on LangBot plugin runtime | openqqwaifu |
| QQ sidecar boundary | Complete | Indirect through LangBot | openqqwaifu |
| Auth and admin console | Complete | No comparable standalone console | openqqwaifu |
| Character card editing and portrait flow | Complete | Partial / config-driven | openqqwaifu |
| Skill registry and marketplace | Complete | Minimal | openqqwaifu |
| Basic message in/out | Complete | Complete | Tie |
| Group reply rules | Partial | Complete | Online plugin |
| Long-term memory recall | Basic | Complete | Online plugin |
| Group member profiles | Basic | Complete | Online plugin |
| Memory graph | Missing | Complete | Online plugin |
| Event injection | Missing | Complete | Online plugin |
| Proactive greeting | Missing | Complete | Online plugin |
| Narrator mode | Missing | Complete | Online plugin |
| Value / relationship game | Missing | Complete | Online plugin |
| Production runtime testability | Strong | Weak | openqqwaifu |

## File-to-File Mapping

| Concern | openqqwaifu | Online plugin | Gap |
| --- | --- | --- | --- |
| Runtime entry | `src/waifu_standalone/app.py` | `main.py` | Local shape is cleaner; online behavior is deeper |
| HTTP/API layer | `src/waifu_standalone/http_api.py` | LangBot host APIs | Local is ahead |
| Message pipeline | `src/waifu_standalone/app.py` | `components/event_listener/waifu_listener.py` | Missing group-specific rules and multimodal queueing |
| Memory organ | `src/waifu_standalone/organs/memories.py` | `organs/memories.py` | Missing graph, thresholds, richer recall, file layout parity |
| Emotion | `src/waifu_standalone/systems/emotions.py` | `systems/emotions.py` | Missing persistent state and prompt-driven evolution |
| Search | `src/waifu_standalone/systems/searching.py` | `systems/searching.py` | Missing LLM-driven query construction and persisted search history depth |
| Cards | `src/waifu_standalone/cells/cards.py` | `data/cards/*` + plugin config | Local is ahead on editing UX |
| Proactive | Missing | `organs/proactive.py` | Must migrate |
| Events | Missing | `systems/events.py` | Must migrate |
| Narrator | Missing | `systems/narrator.py` | Must migrate |
| Relationship value | Missing | `systems/value_game.py` | Must migrate |
| Skills/tools | `cells/skill_registry.py`, `cells/tool_registry.py` | None | Local is ahead |

## Missing Runtime Blocks

### P0: Message behavior parity

Current local behavior is centered in `src/waifu_standalone/app.py`.
Current online behavior is centered in `components/event_listener/waifu_listener.py`.

Missing pieces:

- Group reply gating with `@mention` and active follow-up window parity
- Reply target caching and sender identity handling
- Quote/source stripping and chain-aware text extraction
- Multi-part delayed sending and repeat handling
- Multimodal queueing for image-plus-text turns

Why it matters:

- This is the blocker for replacing the online bot in groups without behavior drift.

### P0: Memory parity

Current local memory is small and file-backed:

- `src/waifu_standalone/organs/memories.py`
- `src/waifu_standalone/memory.py`

Online memory is much deeper:

- `organs/memories.py`
- `organs/memory_graph.py`

Missing pieces:

- Tag-based long-term memory indexing parity
- Memory graph build and related-keyword expansion
- Group member profile persistence with stronger normalization
- Session recall thresholds and recency/priority weighting
- Rich short-term memory trimming rules

Why it matters:

- Without this, the standalone bot can reply, but it will not feel like the same character over time.

### P1: Event and proactive systems

Missing modules in local runtime:

- `systems/events.py`
- `organs/proactive.py`
- `systems/narrator.py`
- `systems/value_game.py`

Why they matter:

- These modules create "alive" behavior, not just reactive chat.
- They are the main difference between a tool bot and the current waifu experience.

### P1: Provider and runtime parity

Local runtime already has:

- Dify chat client in `cells/dify_service.py`
- xAI image client in `cells/xai_image_service.py`
- sidecar panel and health probing in `app.py`

Still missing:

- Production provider binding parity with the online plugin's per-launcher config
- Config import from `data/config/waifu_*.yaml`
- Full migration of `memories_*.json`, `short_term_memory_*.json`, `group_member_profiles_*.json`
- Better runtime metrics and failure counters

### P2: Test and rollout parity

Local test surface is already strong, but it still needs:

- Green test suite for the renamed dashboard title
- End-to-end tests with a real OneBot mock conversation flow
- Import tests against real online waifu data
- Shadow mode output comparisons against the live plugin

## Recommended Migration Order

### Phase 1: Replace message behavior gap

Target files:

- `src/waifu_standalone/app.py`
- `src/waifu_standalone/models.py`
- `src/waifu_standalone/http_api.py`

Port from online:

- Mention/follow-up rules from `components/event_listener/waifu_listener.py`
- Multimodal turn extraction
- Group send timing and reply-window refresh

Exit criteria:

- OneBot group conversations behave like the online plugin for mention, follow-up, and image turns.

### Phase 2: Replace memory gap

Target files:

- `src/waifu_standalone/organs/memories.py`
- `src/waifu_standalone/memory.py`
- new `src/waifu_standalone/organs/memory_graph.py`

Port from online:

- Group member profile logic
- Memory graph
- Weighted recall
- Better archiving and search tags

Exit criteria:

- Imported sessions preserve recall quality and group member naming behavior.

### Phase 3: Bring back waifu-only depth

New local modules to add:

- `src/waifu_standalone/organs/proactive.py`
- `src/waifu_standalone/systems/events.py`
- `src/waifu_standalone/systems/narrator.py`
- `src/waifu_standalone/systems/value_game.py`

Exit criteria:

- Standalone service can produce the same proactive/event/value behaviors without LangBot.

### Phase 4: Data migration and shadow run

Build:

- Importer for online `waifu_*.yaml`
- Importer for memory JSON files
- Shadow-run comparison mode

Exit criteria:

- One group can run on standalone with acceptable drift.

## Recommended Next Build Slice

Build this next:

1. Port message behavior from `components/event_listener/waifu_listener.py`
2. Add `memory_graph.py` and richer member-profile persistence
3. Fix the stale dashboard title test

This sequence gives the highest runtime gain with the least architectural churn.
