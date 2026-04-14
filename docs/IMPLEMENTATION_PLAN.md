# Standalone Waifu Implementation Plan

## Goal

Build a standalone `waifu` service that owns its business logic and receives QQ messages through a sidecar adapter instead of relying on LangBot plugin I/O.

## Phase 1: Local scaffold

- Define domain models for inbound events, replies, and session state.
- Define ports for memory, emotion analysis, image generation, and outbound delivery.
- Provide an in-memory implementation to make the system testable.
- Add a minimal OneBot-compatible HTTP ingress shell.
- Add tests for core flow and error fallback.

## Phase 2: Runtime split

- Replace in-memory storage with persistent storage.
- Add outbound sender integration for a real OneBot adapter.
- Add sidecar configuration for NapCat or Lagrange.
- Add structured logging and health checks.

## Phase 3: Data migration

- Import existing Waifu YAML and JSON session files.
- Map launcher IDs to standalone session keys.
- Migrate long-term memory, short-term memory, and character cards.

## Phase 4: Shadow traffic

- Receive events from QQ sidecar but do not send replies.
- Compare standalone outputs with the current plugin outputs.
- Fix output drift and missing edge cases.

## Phase 5: Cutover

- Enable sending for a single group.
- Monitor failures, latency, and duplicate sends.
- Expand rollout and retire the plugin path.

## Immediate architecture

```text
QQ Client/NapCat/Lagrange
        |
        v
OneBot HTTP or Reverse WS
        |
        v
waifu_standalone.http_api
        |
        v
waifu_standalone.app.WaifuService
        |
        +--> MemoryStore
        +--> EmotionAnalyzer
        +--> ImageGenerator
        +--> OutboundPort
```
