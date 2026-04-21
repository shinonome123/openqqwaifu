<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ Waifu 控制台与运行时。

它把角色卡、会话记忆、知识库、成员目录、技能系统、模型接入、Web Console，以及 OpenClaw 兼容 skill runtime 都收敛在同一个服务里；NapCat / OneBot 只负责 QQ 协议和消息收发。

## 现在它是什么

这已经不是一个临时脚本集合，而是一套可单独部署、可持续迭代的运行时：

- 独立的 Python 后端和 Web Console
- 角色卡编辑、切换、预览、画像管理
- 按角色隔离的会话、知识、成员与运行时状态
- 搜索、生图、总结、文件读取、网页抓取等 tool 能力
- skill marketplace、bundle 导入、技能面板和安全策略
- OpenClaw 兼容运行时
  - native / bundle 检测
  - ClawHub / SkillsMP / GitHub 多源导入
  - MCP tool bridge
  - hook-pack 执行
  - hybrid routing
  - ACP / Codex harness session bridge
- NapCat WebUI 登录桥与 sidecar 管理
- Docker Compose 部署

## 架构概览

```text
NapCat / OneBot
        |
        v
http_api / http_api_async
        |
        v
WaifuService (app.py)
        |
        +-- cards / generator / auth / skills / marketplace
        +-- memory / state_store / migration
        +-- searching / events / proactive / narrator / value_game
        +-- ClawRuntime bridge
        +-- web console
```

## 核心能力

- QQ runtime
  - 通过 NapCat / OneBot 接收和发送消息
  - 支持群聊、私聊、follow-up window、重复触发等行为控制
- 角色系统
  - 支持多角色卡
  - 切换角色时会切换对应的 memory store 与 runtime state
- 记忆与状态
  - `session_history`
  - `user_directory`
  - `knowledge_base`
  - 角色级运行时 SQLite
- 技能与工具
  - 内置技能 + 工作区技能 + 插件技能
  - skill pack / bundle 导入导出
  - tool alias、显式 `/skill` 调用、模型 tool loop
- OpenClaw 兼容层
  - `SKILL.md` / bundle 导入
  - native plugin / bundle capability 诊断
  - `wired / detect-only / unsupported` 边界展示
  - MCP、hook-pack、ACP/Codex harness 兼容桥

## 快速开始

### 1. Docker Compose

推荐部署方式：

```powershell
docker compose -f compose.napcat.yml up -d --build
```

默认入口：

- Console: [http://127.0.0.1:8080/](http://127.0.0.1:8080/)
- NapCat WebUI: [http://127.0.0.1:6099/](http://127.0.0.1:6099/)

如果本机 `8080` 被占用，可以像现在这套本地环境一样通过环境变量覆盖端口。

### 2. 本地运行

```powershell
python run_cli.py dump-config data/config.json
python run_cli.py serve --config data/config.json
```

## 常用命令

```powershell
# 全量测试
python -B -m unittest discover -s tests -v

# 单个测试文件
python -B -m unittest tests.test_app -v

# 启动服务
python run_cli.py serve --config data/config.json

# 检查 NapCat sidecar
python run_cli.py check-sidecar --config data/config.json

# 检查 OpenClaw 兼容运行时
python run_cli.py check-claw-runtime --config data/config.json

# 列出 ClawRuntime 插件
python run_cli.py list-claw-plugins --config data/config.json

# 导出 / 导入 skill pack
python run_cli.py export-skill-pack --config data/config.json --output pack.json
python run_cli.py import-skill-pack --config data/config.json --input pack.json

# 导入本地 skill bundle
python run_cli.py import-skill-bundle --config data/config.json --input C:\path\to\bundle
```

## 配置说明

配置由 `AppConfig` 管理，主文件通常是 `data/config.json`。

- JSON 文件是主配置源
- 环境变量会以 `OPENQQWAIFU_` 前缀覆盖配置
- 可通过 `dump-config` 生成带默认值的模板

关键配置块：

- `llm`
- `image_generation`
- `embedding`
- `qq_sidecar`
- `marketplace`
- `tool_policy`
- `claw_runtime`

`claw_runtime` 里已经支持：

- `enabled`
- `mode`
- `routing_mode`
- `plugin_tools_mcp_bridge`
- `acp_enabled`
- `acp_default_command`
- `acp_default_args`
- `codex_harness_command`
- `codex_harness_args`
- `acp_session_timeout_seconds`

## 目录结构

| 路径 | 作用 |
|---|---|
| `src/waifu_standalone/app.py` | 核心运行时编排 |
| `src/waifu_standalone/cli.py` | CLI 入口 |
| `src/waifu_standalone/config.py` | dataclass 配置定义 |
| `src/waifu_standalone/http_api.py` | 同步 HTTP API |
| `src/waifu_standalone/http_api_async.py` | 异步 HTTP API |
| `src/waifu_standalone/console_panels.py` | 控制台面板数据 |
| `src/waifu_standalone/skill_dispatcher.py` | skill 调度与 tool 分发 |
| `src/waifu_standalone/cells/skill_registry.py` | skill 注册与兼容层 |
| `src/waifu_standalone/cells/tool_registry.py` | tool 注册表 |
| `src/waifu_standalone/cells/marketplace.py` | marketplace 聚合与下载 |
| `src/waifu_standalone/cells/skill_bundle.py` | bundle 导入 |
| `src/waifu_standalone/claw_runtime.py` | Python -> ClawRuntime bridge |
| `src/waifu_standalone/claw_runtime_server.mjs` | 受管 Node/OpenClaw 兼容运行时 |
| `src/waifu_standalone/web/` | Web Console 静态资源 |
| `src/waifu_standalone/builtin_skills/` | 内置技能定义 |
| `tests/` | 单测与集成测试 |

## 当前边界

项目已经具备 OpenClaw 等级兼容的主骨架，但边界仍然明确：

- 目标是“对齐 OpenClaw 已接线能力面”，不是兼容所有外部 skill
- detect-only 能力会明确显示，不会伪装成已可执行
- ACP / Codex harness 已经有 session bridge，但是否真的可用仍取决于你是否配置了实际 harness 命令
- `WaifuService` 仍然偏大，后续还可以继续拆分

## 文档

- [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)
- [docs/DEPLOYMENT_PLAN.md](./docs/DEPLOYMENT_PLAN.md)
- [docs/MEMORY_SYSTEM_PLAN.md](./docs/MEMORY_SYSTEM_PLAN.md)
- [docs/MIGRATION_GAP_ANALYSIS.md](./docs/MIGRATION_GAP_ANALYSIS.md)

## Star History

<a href="https://www.star-history.com/?type=date&repos=shinonome123%2Fopenqqwaifu">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=shinonome123/openqqwaifu&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=shinonome123/openqqwaifu&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=shinonome123/openqqwaifu&type=date&legend=top-left" />
 </picture>
</a>
