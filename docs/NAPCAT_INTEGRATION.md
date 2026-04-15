# NapCat 接入说明

`openqqwaifu` 不直接接 QQ 协议，而是把协议层交给 `NapCat`。  
这样做的边界更稳定：

- `openqqwaifu` 负责人物卡、记忆、技能、模型调用、控制台
- `NapCat` 负责 QQ 登录、消息收发、重连和协议兼容

## 推荐拓扑

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

## 当前仓库的部署方式

仓库里已经提供了两种模式：

- 本地模式：`examples/config.napcat.local.json`
- Docker Compose 模式：`compose.napcat.yml`

其中 Compose 模式会**同时拉起**：

- `openqqwaifu`
- `napcat`

不是把 NapCat 当成 Python 子进程塞进一个容器，而是标准的双容器结构。

## Docker Compose 快速启动

### 1. 准备环境变量

先复制一份环境变量模板：

```powershell
cd C:\openqqwaifu
copy .env.example .env
```

至少建议确认这几个值：

- `ACCOUNT`
- `OPENQQWAIFU_PORT`
- `NAPCAT_HTTP_PORT`
- `NAPCAT_WS_PORT`
- `NAPCAT_WEBUI_PORT`
- `NAPCAT_WEBUI_TOKEN`

### 2. 启动服务

```powershell
docker compose -f .\compose.napcat.yml up --build -d
```

启动后：

- `openqqwaifu` 默认在 `http://127.0.0.1:8080`
- `NapCat WebUI` 默认在 `http://127.0.0.1:6099`

### 3. 打开控制台

访问：

- [http://127.0.0.1:8080/](http://127.0.0.1:8080/)

首次部署先创建控制台管理员账号。  
登录后可以直接进入：

- `QQ 登录` 页面
- `NapCat` 页面

## QQ 登录页如何工作

控制台里的 `QQ 登录` 页不是截图 NapCat 页面，而是通过 NapCat 官方 WebUI API 做桥接：

- 登录页会读取二维码状态
- 可以刷新二维码
- 可以显示当前登录 QQ 信息
- 可以跳转 NapCat 原始 WebUI

这条链路依赖 NapCat WebUI token。

### token 的推荐处理方式

推荐在 `.env` 里直接固定一个 `NAPCAT_WEBUI_TOKEN`，然后 Compose 会把它传给 `openqqwaifu` 的登录桥。

如果你暂时没有固定 token，也可以：

1. 先启动容器
2. 从 `docker logs napcat` 里查看当前 WebUI token
3. 打开控制台的 `QQ 登录` 页
4. 把 token 保存进去

## Compose 文件现在做了什么

当前 `compose.napcat.yml` 已经包含：

- 同时启动 `openqqwaifu` 和 `napcat`
- `openqqwaifu -> napcat` 的容器内地址约定
- `openqqwaifu` 的健康检查
- `openqqwaifu` 的自动重启
- `NapCat WebUI` 桥接所需的环境变量

## 示例配置做了什么

`examples/config.napcat.compose.json` 现在已经包含：

- `outbound_base_url = http://napcat:3000`
- `reverse_ws_url = ws://napcat:3001/onebot/v11/ws`
- `webui_base_url = http://napcat:6099`
- `webui_api_prefix = /api`

其中 `webui_token` 默认留空，优先建议通过环境变量覆盖：

- `OPENQQWAIFU_QQ_SIDECAR_WEBUI_TOKEN`

Compose 已经把它映射成：

- `NAPCAT_WEBUI_TOKEN -> OPENQQWAIFU_QQ_SIDECAR_WEBUI_TOKEN`

## 健康检查与排错

### 查看容器状态

```powershell
docker compose -f .\compose.napcat.yml ps
```

### 查看 openqqwaifu 日志

```powershell
docker compose -f .\compose.napcat.yml logs -f waifu
```

### 查看 NapCat 日志

```powershell
docker compose -f .\compose.napcat.yml logs -f napcat
```

### 查看 openqqwaifu 健康接口

```powershell
Invoke-WebRequest http://127.0.0.1:8080/healthz
```

## 生产环境建议

- 不要把 NapCat 原始 WebUI 裸露到公网
- `QQ 登录` 页面必须放在 `openqqwaifu` 控制台登录之后
- 建议只对外暴露 `openqqwaifu`
- 如果需要公网访问，使用反向代理统一挂载域名

## 下一步

部署层下一阶段建议继续补：

1. 服务器版 `docker compose` 文档
2. 反向代理样例
3. NapCat token 固化策略
4. 真实灰度验收清单
