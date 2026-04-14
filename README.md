<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ Waifu 项目，目标是把原本依附在宿主里的角色卡、记忆、技能、控制台和 OneBot/NapCat 对接能力拆出来，形成一套可以单独部署、单独演进的运行时。

当前仓库已经不是空骨架，而是一个可运行的独立控制台和消息服务。

## 当前已实现

### 1. 独立控制台

- 本地账号登录和首次管理员初始化
- 总览、人物卡、个人用户、AI 接入、记忆、能力、技能、NapCat、事件、高级页面
- 人物卡轮播、结构化编辑、切换动效和立绘工位

### 2. OneBot / NapCat 接入

- 支持 OneBot 风格入站事件
- 支持通过 NapCat HTTP API 发送群聊和私聊消息
- 支持 sidecar 连通性检查
- 支持群成员同步接口，直接从 QQ sidecar 拉取当前群成员列表

### 3. 记忆系统第一阶段

当前记忆已经拆成三层：

- `session_history`
  - 原始会话和短期上下文
- `user_directory`
  - 结构化成员档案
  - `group_id / user_id / qq_nickname / group_card / preferred_name / onboarding_status / profile_summary`
- `knowledge_base`
  - 长期知识条目
  - `scope_type / scope_id / memory_type / summary / tags / confidence / source_message_ids`

实现方式：

- 运行时支持内存版和 SQLite 版双存储
- 前端可以直接查看和编辑成员库、知识库
- 长期摘要会写入知识库
- 回复时会同时召回会话长期记忆和知识库条目

### 4. 首版成员 onboarding

当前已接入最小可用流程：

1. 群成员可以手动同步进成员库
2. 某个成员第一次 `@` 机器人且没有稳定称呼时
3. 机器人会先问一句：`What should I call you?`
4. 用户下一条消息会写入成员库的 `preferred_name`
5. 后续称呼优先从成员库读取，不再混用长期记忆里的称呼

这一步的目的，是先把“称呼”和“长期知识”彻底分开。

## 当前架构

```text
QQ Client
   |
   v
NapCat / OneBot
   |
   +--> /onebot/events
            |
            v
      message router / session manager
            |
            +--> session_history
            +--> user_directory
            +--> knowledge_base
            |
            v
      generator / reply pipeline
            |
            v
      NapCat HTTP API
```

关键边界：

- 模型可以读取 `user_directory` 和 `knowledge_base`
- 模型可以通过提取流程写 `knowledge_base`
- 模型不应直接改 `preferred_name`
- `preferred_name` 只能来自成员同步、onboarding 或后台人工修改

## 目录结构

```text
src/waifu_standalone/
  app.py                    # 运行时组装与主服务逻辑
  http_api.py               # HTTP API / OneBot 入口
  state_store.py            # user_directory + knowledge_base 存储
  gateways/onebot_actions.py
  organs/                   # memory / thoughts
  systems/                  # emotions / searching
  web/                      # 控制台前端

tests/
  test_app.py
  test_http_api.py
  test_onebot_actions.py
  test_server_integration.py
  test_state_store.py
```

## 快速开始

### 1. 运行测试

```powershell
cd C:\path\to\openqqwaifu
$env:PYTHONDONTWRITEBYTECODE='1'
python -B -m unittest discover -s tests -v
```

### 2. 导出默认配置

```powershell
python .\run_cli.py dump-config .\data\config.json
```

### 3. 启动服务

默认是 `dry_run=true`，也就是服务会处理消息，但不会真实向 QQ 发出消息。

```powershell
python .\run_cli.py serve --config .\data\config.json
```

启动后访问：

- [http://127.0.0.1:8080/](http://127.0.0.1:8080/)

首次进入会先要求创建管理员账号。

## NapCat / OneBot 对接

推荐把协议层交给 NapCat，业务层留在 `openqqwaifu`：

```text
QQ Client
  |
  v
NapCat
  | \
  |  \--> HTTP API -> send_group_msg / send_private_msg
  |
  +-----> HTTP event push -> openqqwaifu /onebot/events
```

检查 sidecar：

```powershell
python .\run_cli.py check-sidecar --config .\examples\config.napcat.local.json
```

如果使用 Docker：

```powershell
docker compose -f .\compose.napcat.yml up --build
```

## 已有数据迁移

如果你手上已经有旧版 Waifu 数据目录，可以把会话和部分配置导入进来：

```powershell
python .\run_cli.py import-waifu --waifu-root C:\path\to\Typer_Body__Waifu! --store-root .\data\sessions
```

## 当前适合做什么

这个仓库现在适合：

- 作为独立 `QQ Waifu runtime + control plane` 的主线继续开发
- 用 NapCat / OneBot 做 QQ 协议边界
- 在前端直接管理角色卡、成员库和知识库
- 继续把线上 `langbot + waifu` 的核心运行时能力迁过来

## 下一步

当前最值得继续补的是两块：

1. 自动群成员同步
   - 基于 NapCat 的群成员事件和群成员列表接口
2. 模型驱动的结构化知识提取
   - 自动把稳定事实、偏好、事件写入 `knowledge_base`

然后再继续往下做：

- `memory_graph`
- `proactive / events`
- `value_game`
- `narrator`

## 相关文档

- [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)
- [docs/MEMORY_SYSTEM_PLAN.md](./docs/MEMORY_SYSTEM_PLAN.md)
- [docs/MIGRATION_GAP_ANALYSIS.md](./docs/MIGRATION_GAP_ANALYSIS.md)
