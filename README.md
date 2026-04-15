<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ Waifu 控制台与运行时。

它把人物卡、记忆、技能、模型接入、成员数据库、知识库和 QQ sidecar 边界从传统插件宿主里拆出来，形成一套可单独部署、单独演进的服务。

## 当前能力

- 独立控制台：角色卡、成员库、知识库、AI 配置、技能、QQ 登录
- NapCat / OneBot 接入
- 技能系统：内置技能、第三方技能、skill pack、技能市场
- 记忆系统：
  - `session_history`
  - `user_directory`
  - `knowledge_base`
  - `memory_graph`
- 行为系统：
  - `value_game`
  - `narrator`
  - `events`
  - `proactive` 基础能力
- 图片生成、联网搜索、摘要工具链

## 2026-04-16 更新

本轮完成了两类关键修复：

1. 人物卡切换与卡片加载修复
- 运行时不再让 `session.metadata["card"]` 覆盖当前激活人物卡
- 当前角色优先从活动角色 / session 绑定角色读取
- 修复了导入数据后旧卡片身份抢占当前角色的问题

2. 角色隔离存储落地
- 会话按 `character_id` 隔离
- 知识库按 `character_id` 隔离
- 成员共享目录和角色态拆分：
  - 共享：`preferred_name`、`qq_nickname`、`group_card`、`onboarding_status`
  - 角色态：`profile_summary`、`affinity_score`、`notes_count`、`last_addressed_at`
- Docker 运行时已重建到这套新代码

## 已知未解决问题

### 1. 上游模型侧的角色隔离还没有完全做完

本地会话、知识库、成员画像已经按 `character_id` 隔离，但远程 LLM 调用目前仍主要按发送者 ID 组织请求。

这意味着：
- 如果上游后端自己保留用户级会话或隐式记忆
- 某些模型服务仍可能在切换人物卡后带出旧人格残留

当前状态：
- 本地运行时已经不再复用旧 root-level 会话历史
- 但“远程模型用户标识是否也要带 `character_id`”这一步还没有完全做完

计划修复：
- 将远程 LLM `user/session` 标识显式改成 `character_id + launcher_id + sender_id` 组合键

### 2. 旧 root-level session 文件不会自动清理

旧版本遗留的 `data/sessions/group_xxx.json` / `person_xxx.json` 还会保留在磁盘上。

当前策略是：
- 新代码不再把这些旧文件当作新角色的运行时上下文
- 但仓库还没有提供自动清理或批量迁移脚本

## 项目结构

```text
src/waifu_standalone/
  app.py
  http_api.py
  state_store.py
  memory.py
  migration.py
  cells/
  gateways/
  organs/
  systems/
  web/

docs/
examples/
tests/
```

## 运行方式

实际联调和运行请使用 Docker。

### Docker Compose

```powershell
cd C:\openqqwaifu
copy .env.example .env
docker compose -f .\compose.napcat.yml up --build -d
```

默认入口：

- 控制台：[http://127.0.0.1:8080/](http://127.0.0.1:8080/)
- NapCat WebUI：[http://127.0.0.1:6099/](http://127.0.0.1:6099/)

## 测试

```powershell
cd C:\openqqwaifu
python -B -m unittest discover -s tests -v
```

当前基线：`150/150` 通过。

## 文档

- [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)
- [docs/DEPLOYMENT_PLAN.md](./docs/DEPLOYMENT_PLAN.md)
- [docs/MEMORY_SYSTEM_PLAN.md](./docs/MEMORY_SYSTEM_PLAN.md)
- [docs/MIGRATION_GAP_ANALYSIS.md](./docs/MIGRATION_GAP_ANALYSIS.md)
