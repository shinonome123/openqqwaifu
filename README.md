<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ AI 角色服务。

它把角色卡、会话记忆、知识库、成员目录、技能系统、模型接入、Web Console 和 QQ 登录桥接整合到一个服务里；NapCat / OneBot 只负责 QQ 协议和消息收发，业务逻辑全部由本项目自己维护。

当前推荐部署方式是 `Docker + NapCat sidecar`。

## 现在到了哪一步

项目已经不是“原型插件”，而是一个可以独立部署、持续迭代的运行时：

- 有独立的 Python 后端、Web Console、Docker 部署和测试体系
- 支持角色卡编辑、切换、预览、立绘管理
- 支持群聊 / 私聊会话、成员目录、知识抽取、技能调度、搜索和生图能力
- 支持 NapCat WebUI 登录桥和控制台内 QQ 登录流程
- 已建立按角色隔离的会话和运行时存储

截至当前分支，测试基线为 `191/191` 通过：

```powershell
python -B -m unittest discover -s tests -v
```

## 最近完成的里程碑

### 1. 角色隔离补齐

这轮最重要的改动，是把“切角色卡后仍受上一张卡影响”的问题收敛到了真正的角色边界内。

现在已经做到：

- 会话文件按角色分目录保存：`data/sessions/<character>/...`
- 运行时 SQLite 按角色分库保存：`data/state/characters/<character>/runtime.sqlite3`
- 切换角色时会重绑当前角色的 memory store 和 state store
- 切换角色时会清掉上一张卡留下来的运行时上下文
  - follow-up 窗口
  - recent behavior events
  - session locks
- 行为事件和 follow-up 状态现在也带 `character_id`
- 角色切换不再只是更新 `active_character.json`，而是会真正切换整套角色作用域

这意味着：

- `default` 的会话不会再跑到 `aurora`
- `default` 的运行时成员状态不会再被 `aurora` 读到
- 上一张卡的 follow-up / behavior graph 上下文不会再串到下一张卡

这不意味着：

- 当前激活角色自己的聊天内容不会继续影响它自己

如果某张卡本身允许自动知识回写，那么它仍然会把“这张卡自己刚聊出来的内容”写进它自己的长期状态。这是当前刻意保留的行为，而不是隔离缺失。

### 2. 前端控制台完成一轮稳定化

本分支已经完成一轮控制台修补，主要包括：

- 角色切换竞态修复
- 非管理员导航和接口收口
- 通用 modal 事件泄漏修复
- 前端不再直接暴露部署机本地绝对路径
- 角色相关页面的默认文案不再硬编码旧人格名

控制台现在的目标不是“做演示”，而是作为正式运维入口存在。

### 3. 运行时和接口层继续工程化

当前代码库已经具备：

- async HTTP runtime
- `aiohttp` / `httpx` 传输层
- OneBot 回调接入
- 健康检查接口 `/healthz`
- Console API、角色页、技能页、NapCat / QQ 登录页

## 系统结构

```text
NapCat / OneBot
        |
        v
http_api / http_api_async
        |
        v
WaifuService (app.py)
        |
        +-- cards / generator / auth / skill registry / marketplace
        +-- memory / state_store / migration
        +-- searching / events / narrator / proactive / value_game
        +-- web console
```

### 关键模块

| 路径 | 作用 |
|---|---|
| `src/waifu_standalone/app.py` | 核心运行时编排，当前仍是项目的控制中心 |
| `src/waifu_standalone/http_api.py` | 兼容 HTTP 服务层和控制台 API |
| `src/waifu_standalone/http_api_async.py` | async 服务层 |
| `src/waifu_standalone/memory.py` | 会话存储，当前支持角色作用域隔离 |
| `src/waifu_standalone/state_store.py` | 成员、知识、画像、运行时状态存储 |
| `src/waifu_standalone/cells/cards.py` | 角色卡加载、编辑、切换、预览 |
| `src/waifu_standalone/console_panels.py` | Web Console 业务面板 |
| `src/waifu_standalone/gateways/napcat_login.py` | NapCat WebUI / QQ 登录桥 |
| `src/waifu_standalone/web/` | 静态前端资源 |

## 数据模型

### 1. Session History

短期会话历史，当前按以下维度隔离：

- `character_id`
- `launcher_type`
- `launcher_id`

文件位置示例：

```text
data/sessions/default/group_568701249.jsonl
data/sessions/aurora/group_568701249.jsonl
```

### 2. User Directory

成员目录保存稳定用户资料，例如：

- QQ 昵称
- 群名片
- preferred name
- onboarding 状态
- profile summary
- affinity / bond 状态

这部分也按角色隔离存放在各自的 runtime DB 中。

### 3. Knowledge Base

知识条目同样属于角色作用域内的数据，和成员目录一起进入该角色自己的 SQLite runtime DB。

## 当前推荐部署方式

### Docker Compose

```powershell
copy .env.example .env
docker compose -f compose.napcat.yml up --build -d
```

启动后访问：

- Console: [http://127.0.0.1:8080/](http://127.0.0.1:8080/)
- NapCat WebUI: [http://127.0.0.1:6099/](http://127.0.0.1:6099/)

### 本地直接运行

```powershell
python run_cli.py dump-config data/config.json
python run_cli.py serve --config data/config.json
```

## 开发者入口

### 常用命令

```powershell
# 全量测试
python -B -m unittest discover -s tests -v

# 单测文件
python -B -m unittest tests.test_app -v

# 启动服务
python run_cli.py serve --config data/config.json

# 检查 NapCat sidecar
python run_cli.py check-sidecar --config data/config.json

# 导出 / 导入技能包
python run_cli.py export-skill-pack --config data/config.json --output pack.json
python run_cli.py import-skill-pack --config data/config.json --input pack.json
```

### 代码阅读顺序

如果你是第一次接手，建议按这个顺序看：

1. `src/waifu_standalone/app.py`
2. `src/waifu_standalone/cells/cards.py`
3. `src/waifu_standalone/memory.py`
4. `src/waifu_standalone/state_store.py`
5. `src/waifu_standalone/console_panels.py`
6. `src/waifu_standalone/http_api.py` / `http_api_async.py`

## 当前已知边界

这些问题不是“未知问题”，而是当前明确还在排队的工程项：

- `WaifuService` 仍然很大，后续仍需要继续拆分
- 可观测性仍然偏弱，结构化日志和 metrics 还没有补齐
- graceful shutdown 还有继续完善空间
- 配置 schema 校验还不够强
- 同一张角色卡自己的知识回写，仍然可能把当前聊天风格沉淀到它自己的长期状态

最后这一条要特别强调：

项目当前已经修的是“跨角色串人格”，不是“阻止角色学习它自己刚聊出来的内容”。

## 这份 README 的定位

这份 README 不再把项目描述成“还在试验的脚本集合”，而是把它当成一个正在持续工程化的独立服务来写。

如果你接下来要继续推进项目，当前比较明确的优先级是：

1. 继续拆 `WaifuService`
2. 补 structured logging / metrics
3. 继续打磨角色隔离边界和运行时生命周期

## 文档

- [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)
- [docs/DEPLOYMENT_PLAN.md](./docs/DEPLOYMENT_PLAN.md)
- [docs/MEMORY_SYSTEM_PLAN.md](./docs/MEMORY_SYSTEM_PLAN.md)
- [docs/MIGRATION_GAP_ANALYSIS.md](./docs/MIGRATION_GAP_ANALYSIS.md)
