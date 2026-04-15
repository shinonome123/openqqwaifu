<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ Waifu 控制台与运行时。  
它把人物卡、记忆、技能、模型接入、管理面板和 QQ sidecar 边界从传统插件体系里拆出来，形成一套可单独部署、单独演进的服务。

## 当前能力

- 控制台登录、用户管理、人物卡编辑、立绘位、人物卡测试面板
- AI 接入面板：聊天模型、生图模型、向量召回
- 记忆系统：`session_history`、`user_directory`、`knowledge_base`
- 技能系统：Markdown skills、skill packs、远程源
- NapCat / OneBot 接入
- `QQ 登录` 页面桥接 NapCat WebUI 二维码登录
- 生图指令链路和图片回传

## 项目结构

```text
src/waifu_standalone/
  app.py
  http_api.py
  state_store.py
  config.py
  cells/
  gateways/
  organs/
  systems/
  web/

docs/
examples/
tests/
```

## 本地运行

### 1. 跑测试

```powershell
cd C:\openqqwaifu
python -B -m unittest discover -s tests -v
```

### 2. 导出默认配置

```powershell
python .\run_cli.py dump-config .\data\config.json
```

### 3. 启动服务

```powershell
python .\run_cli.py serve --config .\data\config.json
```

默认访问：

- [http://127.0.0.1:8080/](http://127.0.0.1:8080/)

## Docker Compose 部署

仓库已经提供了双容器部署：

- `openqqwaifu`
- `NapCat`

快速开始：

```powershell
cd C:\openqqwaifu
copy .env.example .env
docker compose -f .\compose.napcat.yml up --build -d
```

然后访问：

- [http://127.0.0.1:8080/](http://127.0.0.1:8080/)

更完整的说明见：

- [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)
- [docs/DEPLOYMENT_PLAN.md](./docs/DEPLOYMENT_PLAN.md)

## NapCat 边界

推荐的运行拓扑：

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

边界原则：

- `openqqwaifu` 负责人格、记忆、技能、模型和控制台
- `NapCat` 负责 QQ 登录、消息收发和协议稳定性

## 当前文档

- [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)
- [docs/DEPLOYMENT_PLAN.md](./docs/DEPLOYMENT_PLAN.md)
- [docs/MEMORY_SYSTEM_PLAN.md](./docs/MEMORY_SYSTEM_PLAN.md)
- [docs/MIGRATION_GAP_ANALYSIS.md](./docs/MIGRATION_GAP_ANALYSIS.md)
