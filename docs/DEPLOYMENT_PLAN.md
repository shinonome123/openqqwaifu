# openqqwaifu + NapCat 部署计划

这份计划描述的是把 `openqqwaifu` 作为主控制台和业务运行时，`NapCat` 作为 QQ 协议 sidecar，一起部署到服务器上的收口路径。

## 目标

形成一套稳定的双容器部署结构：

- `openqqwaifu`
- `NapCat`

并满足下面这些条件：

- Compose 一次启动两者
- 控制台可以直接完成 QQ 登录
- NapCat 不需要再单独手工开一个页面操作
- 配置、数据目录、日志和健康检查都明确

## 阶段一：部署契约收口

已完成的内容：

- `Dockerfile`
- `compose.napcat.yml`
- `examples/config.napcat.compose.json`
- `.env.example`
- `QQ 登录` 页面桥接 NapCat WebUI

这一阶段的验收标准：

- `docker compose up --build -d` 可以拉起服务
- 控制台能打开
- `QQ 登录` 页面能显示状态和二维码

## 阶段二：服务器部署闭环

要完成的内容：

1. 固定服务器目录结构
   - 代码目录
   - `data/`
   - `napcat/qq/`
   - `napcat/config/`

2. 固定环境变量来源
   - `.env`
   - `NAPCAT_WEBUI_TOKEN`
   - 对外端口

3. 固定反向代理入口
   - 控制台域名
   - 是否保留 NapCat 原始 WebUI 入口

4. 固定备份策略
   - `data/`
   - `napcat/qq/`
   - `napcat/config/`

这一阶段的验收标准：

- 重启机器后可以自动恢复
- 不需要重新手工拼配置
- 控制台和 NapCat 登录链仍然正常

## 阶段三：灰度替换现网

上线方式建议按下面顺序走：

1. 先在测试群跑
2. 再在小流量群跑
3. 最后替换主群

重点验收：

- 群消息接收
- 私聊接收
- 5 秒跟聊窗口
- 生图回传
- 记忆写入和召回
- NapCat 断线后的恢复

## 风险点

### 1. NapCat token 管理

`QQ 登录` 桥接依赖 WebUI token。  
如果 token 不固定，第一次部署时仍然需要从日志或配置里确认一次。

### 2. 公网暴露面

不建议把 NapCat WebUI 裸露到公网。  
更推荐只暴露 `openqqwaifu`，通过控制台代理完成登录和状态查看。

### 3. 数据目录权限

NapCat 的 QQ 数据目录和 `openqqwaifu` 的 `data/` 需要稳定挂载，否则重建容器时容易丢状态。

## 建议的后续执行顺序

1. 用当前 Compose 在本机做一轮完整 Docker 冒烟
2. 把同一套 Compose 搬到服务器
3. 接 nginx / 域名
4. 做灰度切换
