<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ Waifu 控制台与运行时，基于 `NapCat + OneBot + Docker` 部署。

它把人物卡、记忆、技能、模型接入、知识库、成员目录和 QQ 登录桥接，从传统插件宿主里拆出来，形成一套可以单独部署、单独演进、单独排障的服务。

## 当前状态

- Docker 单运行时部署
- NapCat WebUI / QQ 登录桥接
- 统一提示词回复链路
- 人物卡编辑、切换、立绘工位、测试面板
- 群聊 / 私聊独立会话
- 三层记忆系统：
  - `session_history`
  - `user_directory`
  - `knowledge_base`
- 向量召回与知识库持久化
- 技能系统：
  - builtin skills
  - workspace skills
  - skill pack 导入导出
  - marketplace 远程源
- 联网搜索、摘要、生图工具链
- 成员目录、关系值、行为事件、session detail 记忆图谱
- 控制台登录、用户页、密码修改

## 2026-04-16 最新进展

### 提示词与回复主链路收口

- 主回复流程改成单一 PromptBuilder 路径，统一输出：
  - `[人设]`
  - `[回复原则]`
  - `[你和 X 的关系]`
  - `[最近对话]`
  - `[X 刚刚说]`
- 普通回复不再额外注入：
  - `Thoughts.analyze()` 的独立分析结果
  - `Narrator` 的英文 cue
  - `EmotionSensor` 的情绪标签
  - `MemoryGraphBuilder` 的图谱高亮
- onboarding 也已切到统一 prompt builder，不再保留旧的并行提示词路径
- 主回复热路径压缩为一次 LLM 调用，后处理逻辑留在回复之后执行

### 组件拆分与职责收口

- `WaifuService` 继续负责主编排，但重逻辑已拆出到独立组件：
  - `PromptBuilder`
  - `KnowledgeManager`
  - `MemberManager`
  - `RelationshipTracker`
  - `DashboardService`
  - `SessionDetailGraphService`
- `Thoughts` 与 `Narrator` 已退出代码路径并删除
- `MemoryGraphBuilder` 只保留给 memory 页 session detail 观测使用，不再参与主回复提示词
- 统一记忆召回入口收口到 `KnowledgeManager.recall()`，负责合并、去重和限流

### 控制台与能力页收口

- abilities 页只保留仍在主回复链路里生效的能力开关
- `thinking / narrator / memory_graph / max_thinking_words` 等旧热路径控制被归档到 advanced 观测区
- 角色预览面板不再展示分析 trace，直接预览最终回复
- message behavior 相关配置从角色页挪回 abilities 页，角色编辑和运行时配置边界更清晰

### Docker 作为唯一正式运行方式

- Docker 启动后会同时拉起 `openqqwaifu` 与 `NapCat`
- 控制台内可直接走 `QQ 登录` 页，不需要手工单独打开 NapCat 管理页
- NapCat 回调地址会自动配置为容器内可达地址
- 当前正式运行入口统一为 Docker Compose，不再建议在宿主机上直接启动 Python 服务

## 架构概览

```text
NapCat (QQ 登录 / OneBot)
        |
        v
openqqwaifu http_api.py
        |
        v
app.py runtime orchestration
        |
        +-- cards / generator / prompt_builder
        +-- member_manager / knowledge_manager / relationship_tracker
        +-- memory / state_store / migration
        +-- searching / events / value_game / proactive
        +-- dashboard_service / session_detail_graph
        +-- web dashboard
```

## 目录结构

```text
src/waifu_standalone/
  app.py
  http_api.py
  config.py
  state_store.py
  memory.py
  migration.py
  cells/
  gateways/
  organs/
  systems/
  web/

data/
  cards/
  sessions/
  portraits/
  docker-compose-config.json

docs/
examples/
tests/
```

## 部署方式

只建议使用 Docker 作为正式运行方式。

### 1. 启动

```powershell
cd C:\openqqwaifu
copy .env.example .env
docker compose -f .\compose.napcat.yml up --build -d
```

后续更新也建议继续走 Docker：

```powershell
cd C:\openqqwaifu
docker compose -f .\compose.napcat.yml up -d --build
docker compose -f .\compose.napcat.yml ps
```

### 2. 打开控制台

- 控制台：[http://127.0.0.1:8080/](http://127.0.0.1:8080/)
- NapCat WebUI：[http://127.0.0.1:6099/](http://127.0.0.1:6099/)

### 3. 完成首次部署

- 首次打开控制台时，先创建管理员账号
- 登录控制台后，进入 `QQ 登录` 页完成扫码登录
- 在 `AI 接入` 页配置聊天模型 / 生图模型 / embedding
- 在 `人物卡` 页选择或编辑当前角色

## 控制台能力

- 概览
- 人物卡
- AI 接入
- 记忆
- 成员目录
- Skills
- 能力
- NapCat
- QQ 登录
- 个人用户
- 其他设置

## 记忆系统

### Session History

- 每个 `character_id + launcher_type + launcher_id` 一份短期会话
- 用于当前轮次 prompt 拼装、跟聊窗口、待确认搜索等运行时状态

### User Directory

- 保存共享昵称、称呼、群名片、入群状态等稳定资料
- 不把角色人格态和共享目录混在一起

### Knowledge Base

- 保存摘要、知识条目、向量 embedding、角色隔离后的长期记忆
- 支持按角色、会话、成员维度召回

## 技能系统

- 内置工具型技能：
  - `search`
  - `summary`
  - `image`
  - `skill-list`
- 支持 markdown skill
- 支持 workspace 覆盖 builtin
- 支持 skill pack 导入导出
- 支持 marketplace 远程源检索与安装

## 已知边界

### 1. 旧 legacy session 文件可能仍在磁盘上

历史版本遗留的 root-level `data/sessions/group_xxx.json` / `person_xxx.json` 可能还存在。

当前运行时不会再把它们当作新角色的活动会话，但磁盘文件本身仍可能需要人工清理。

### 2. 人物卡本身的约束强度仍然取决于卡内容

人格隔离解决的是“角色不应串线”。

但如果某张卡本身没有明确限制口吻、尺度、风格，模型仍可能在该卡自己的边界内说出不符合你预期的话。这个问题应通过人物卡规则本身约束，而不是靠隔离机制兜底。

### 3. QQ / WebUI 稳定性仍受上游 NapCat 版本影响

控制台已经加了登录桥、二维码自动刷新、路径兼容和本地二维码生成，但 NapCat WebUI 的接口变化仍可能要求后续继续兼容。

## 测试

```powershell
cd C:\openqqwaifu
python -B -m unittest discover -s tests -v
```

当前仓库基线：`177/177` 通过。

## 文档

- [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)
- [docs/DEPLOYMENT_PLAN.md](./docs/DEPLOYMENT_PLAN.md)
- [docs/MEMORY_SYSTEM_PLAN.md](./docs/MEMORY_SYSTEM_PLAN.md)
- [docs/MIGRATION_GAP_ANALYSIS.md](./docs/MIGRATION_GAP_ANALYSIS.md)
