<p align="center">
  <img src="./src/waifu_standalone/web/assets/img/brand-logo.png" alt="openqqwaifu logo" width="220" />
</p>

# openqqwaifu

`openqqwaifu` 是一个独立运行的 QQ Waifu 框架，目标是把原本耦合在宿主平台里的角色卡、记忆、技能、控制台和 OneBot/NapCat 对接能力拆出来，形成一套可以自行维护、扩展和部署的项目。

当前这版已经不是纯脚手架，而是可运行的独立控制台：

- 支持 OneBot 风格入站事件与 NapCat/HTTP 出站发送
- 支持本地登录、首次部署初始化管理员、控制台多标签页配置
- 支持人物卡轮播、私聊卡/群聊卡表单化编辑、立绘生成与绑定
- 支持短期记忆、长期记忆归档、会话查看与修改
- 支持技能系统、工具分发、Skill Pack 导入导出与远程技能源
- 支持联网搜索、思维分析、生图命令和 QQ 机器人运行链路

## 项目定位

这不是一个“只给 LangBot 做插件”的仓库，而是一个准备独立发展的 `QQ Waifu runtime + control plane`：

- `cells`：模型接入、角色卡、技能、市场、工具与配置单元
- `organs`：记忆与思维相关的编排
- `systems`：情绪、搜索等系统级能力
- `gateways`：OneBot/NapCat 边界与消息发送
- `web`：本地控制台前端

## 当前能力

### 控制台

- 本地用户名/密码登录
- 首次部署初始化管理员
- 总览、人物卡、用户、AI 接入、记忆、能力、技能、NapCat、事件、高级面板
- 品牌 logo 与主题切换

### 人物卡

- 人物卡轮播与启用切换
- 私聊卡 / 群聊卡分离
- 表单化编辑，不要求用户直接写 YAML
- 立绘工位：根据人物卡信息调用已接入的生图模型生成角色立绘

### 智能能力

- LLM 回复链
- 生图链
- 联网搜索
- 思维分析
- 长期记忆归档与召回
- 会话偏好称呼与群成员信息记录

### 技能生态

- Markdown Skill
- Tool dispatch
- Skill Pack 导入 / 导出
- 远程 Skill Source / Marketplace 检索

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

默认配置下会以 `dry_run=true` 运行，也就是：

- 接收入站事件
- 在内存中记录出站消息
- 不直接向 QQ 侧车发送真实消息

```powershell
python .\run_cli.py serve --config .\data\config.json
```

### 4. 打开控制台

服务启动后，直接访问配置中的 HTTP 地址。首次部署会要求先创建管理员账号。

## NapCat / OneBot 对接

本项目默认推荐把协议层交给 NapCat，业务层留在 `openqqwaifu`：

```text
QQ Client
  |
  v
NapCat
  |  \
  |   \-- HTTP API -> send_group_msg / send_private_msg
  |
  +------ HTTP event push -> openqqwaifu /onebot/events
```

本地检查 NapCat 侧车连通性：

```powershell
python .\run_cli.py check-sidecar --config .\examples\config.napcat.local.json
```

使用 Docker Compose：

```powershell
docker compose -f .\compose.napcat.yml up --build
```

详细说明见 [docs/NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md)。

## 导入已有 Waifu 数据

如果你已经有运行中的 Waifu 数据目录，可以直接迁入会话与配置：

```powershell
python .\run_cli.py import-waifu --waifu-root C:\path\to\Typer_Body__Waifu! --store-root .\data\sessions
```

## 适合谁

`openqqwaifu` 适合这几类场景：

- 想把 QQ Waifu 从原宿主平台里独立出来
- 想自己维护人物卡、技能和控制台
- 想继续扩展“Skill + Tool + Memory + Image”生态
- 想把 NapCat / OneBot 作为协议边界，减少上层业务耦合

## 当前状态

当前仓库已经可以作为独立版主线继续推进，但它仍处在快速迭代阶段。接口、配置字段和控制台细节还会继续收敛。

如果你准备直接用于现网，请先根据自己的模型接入、NapCat 环境和安全策略做一轮配置审查。
