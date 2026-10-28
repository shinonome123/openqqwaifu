<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ Waifu 控制台与运行时。

它把角色卡、会话记忆、知识库、成员目录、技能系统、模型接入、Web Console，以及 OpenClaw 兼容 skill runtime 都收敛在同一个服务里；NapCat / OneBot 只负责 QQ 协议和消息收发。

## 现在它是什么

正在开发中的一套结合了openclaw与qq机器人相关的虚拟女友

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

新版私聊运行链路已提供默认关闭的试运行入口：持久事件、会话角色绑定、结构化上下文、Outbox、重启恢复和发送结果核对。使用范围与配置见 [第一批实现说明](docs/RUNTIME_V2_FIRST_SLICE.md)；管理员可在「会话记录」查看轮次、上下文预算、角色版本并核对发送，详见 [上下文与会话控制台](docs/RUNTIME_V2_CONSOLE.md)。[事实、经历与纠正](docs/RUNTIME_V2_MEMORY.md) 已接入持久记录、来源追踪、修订和召回；[持久提醒](docs/RUNTIME_V2_REMINDERS.md) 支持明确授权的一次性提醒、延期、取消和恢复。[会话删除与恢复屏障](docs/RUNTIME_V2_PRIVACY.md) 防止旧任务回写和恢复旧主库导致数据复活。[主动联系规则](docs/RUNTIME_V2_CONTACT_POLICY.md) 已接入逐私聊授权、时区、静默时段、频率限制和暂停恢复，现有提醒也遵守这些边界。[经历跟进候选](docs/RUNTIME_V2_FOLLOWUPS.md) 已支持来源核对、正文与时间确认、排期、失效取消和发送核对；[角色化跟进草稿](docs/RUNTIME_V2_FOLLOWUP_DRAFTS.md) 支持明确请求后台生成、来源追踪和人工选用，生成不会自动发送。[经历时间提取与澄清](docs/RUNTIME_V2_EPISODE_TIMES.md) 支持按来源解析常见时间表达、人工确认日期或时段，并在时间更正后撤销旧排期。[经历时间对话确认](docs/RUNTIME_V2_EPISODE_TIME_DIALOGUE.md) 已支持用户直接在私聊中澄清、选择、确认和更正时间，控制台保留来源审计。自动记忆提取与提醒仍分别默认关闭，无人核对的自动排期尚未开放。

真实模型的有限抽样结果见 [连续对话试验](docs/RUNTIME_V2_EXPERIENCE_TRIAL.md)。DeepSeek 纯对话已体现角色差异，但结构化回复仍有空白返回，完整体验验收尚未通过；后续修复入口与合成复现材料见 [问题交接](docs/HANDOFF_DEEPSEEK_2026-10-26.md)。

不连接 QQ 或真实模型即可运行演示（输出目录需为新目录）：

```bash
python -B run_cli.py runtime-v2-demo --output-dir .codex-temp/runtime-v2-demo-local
```

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
  - 听完再说（`listen`）：连着发的几句等说完再一起回；群里她刚回过的人接着说不用 @；没人叫她时，等群里安静下来，按话题和她的兴趣（人物卡 meta 的 `interests`）判断要不要插话，模型也可以选择不说；每张卡可以在 meta 的 `chime_in` 里调插话的门槛和频率，`call_names` 写群友对她的其他叫法
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
- 酒馆模式（跑团）
  - 群里 @机器人说「启动酒馆」列出剧本，说的人当房主，回「选 1」开本，机器人改当主持人
  - 「加入」「离开」上下桌；直接说话就是行动，几秒内所有人的行动由主持人一次接上；括号（…）或 `//` 开头是场外话
  - `.r 2d6+1 撬锁` 由程序掷骰，结果交给主持人；「酒馆状态」看谁在玩；只有房主（和 `tavern.admin_user_ids`、`tool_policy.authorized_user_ids`）能「退出酒馆模式」，30 分钟没人说话自动结束
  - 自带 4 个本；自己的本放进 `data/tavern/scenarios/`：YAML（格式见 `src/waifu_standalone/templates/tavern/`），或直接放 SillyTavern 角色卡（`.json` / `.png`）
  - 剧情存在 `data/tavern/`，不进角色的聊天记录和记忆
  - SillyTavern 角色卡按 QQ 聊天的口吻演（第一人称、动作写在括号里），不发网页版的状态栏和选项
  - 成人模式只在私聊里、只对 `tavern.adult_user_ids` 里的人生效，成人卡放 `data/tavern/adult/`；群里永远不开
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

显式指定的配置文件必须存在；首次启动先执行 `dump-config`。HTTP 模式须设置独立的
`qq_sidecar.inbound_access_token`（或 `OPENQQWAIFU_QQ_SIDECAR_INBOUND_ACCESS_TOKEN`）并在
NapCat HTTP 客户端填写相同 token。控制台的 Sidecar 页面也可配置，程序会同步管理的
NapCat 回调配置。回调接受 `Authorization: Bearer <token>` 或 OneBot `X-Signature`
HMAC-SHA1 签名；未配置令牌会拒绝 HTTP 事件。`qq_sidecar.access_token` 仅用于调用
NapCat 动作接口；`reverse_ws` 模式关闭 HTTP 事件入口并使用单独的反向 WebSocket 令牌。

`tool_policy.authorized_user_ids` 指定允许读取、列出、写入本地文件、执行命令和抓取网页（web-fetch）的 QQ 用户 ID，默认空列表；
本地文件摘要也需要这项授权，并受允许读取目录约束。远程网页摘要和正常会话摘要不需要这项本地文件授权。
也可用 `OPENQQWAIFU_TOOL_POLICY_AUTHORIZED_USER_IDS` 设置以逗号分隔的 ID。
执行命令使用服务进程的宿主权限；只应添加你信任拥有这些权限的用户。

`qq_sidecar.dry_run = true` 会捕获出站消息，不发送到 QQ；HTTP 和反向 WebSocket 模式均适用，
反向 WebSocket 仍可接收入站事件。也可设置 `OPENQQWAIFU_QQ_SIDECAR_DRY_RUN=true`。

开启 `multimodal_enabled` 后，OpenAI 兼容接口和 Claude 接口会收到消息中的图片 URL 或 base64 数据；
所选模型也必须支持图片输入。当前 Dify 适配器不支持这条图片理解路径，会明确提示不可用。
生图须同时启用 `image_generation.enabled` 并配置可用提供商；禁用或未配置时会报告失败。
关闭 `search_enabled` 后，搜索执行和搜索缓存结果都不可用。

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
