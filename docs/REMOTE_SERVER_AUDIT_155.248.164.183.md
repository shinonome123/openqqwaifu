# 155.248.164.183 远端仓库与 Waifu 插件采集结果

采集时间：

- 远端时间：`2026-04-14T14:36:35+00:00`
- 远端用户：`ubuntu`
- 主机名：`instance-20260405-2033`

## 发现的 Git 仓库

服务器上当前找到 5 个 Git 仓库：

```text
/home/ubuntu/build/CLIProxyAPI
/home/ubuntu/build/one-api
/home/ubuntu/data/langbot
/home/ubuntu/dify
/home/ubuntu/openlist-apipages-src
```

## 仓库摘要

### 1. `/home/ubuntu/build/CLIProxyAPI`

- Remote：`https://github.com/router-for-me/CLIProxyAPI.git`
- 分支：`main`
- 最近提交：`5ab9afa fix(executor): handle OAuth tool name remapping with rename detection and add tests`
- 工作区状态：`clean`

### 2. `/home/ubuntu/build/one-api`

- Remote：`https://github.com/songquanpeng/one-api.git`
- 分支：`main`
- 最近提交：`8df4a26 docs: update ByteDance Doubao model link in README`
- 工作区状态：`clean`

### 3. `/home/ubuntu/data/langbot`

- Remote：`https://github.com/langbot-app/LangBot.git`
- 分支：`master`
- 最近提交：`cc4d883 fix: update langbot-plugin version to 0.3.8`
- 工作区状态：`dirty`

当前可见的 LangBot 本地改动：

- 已修改：`docker/docker-compose.yaml`
- 未跟踪：`docker/data/`
- 未跟踪备份：
  - `docker/docker-compose.yaml.bak-20260411-110925`
  - `docker/docker-compose.yaml.bak.20260413211254`

### 4. `/home/ubuntu/dify`

- Remote：`https://github.com/langgenius/dify.git`
- 分支：`main`
- 最近提交：`ee87289917 refactor: convert AppMode if/elif to match/case in app_generate_service (#30001) (#34563)`
- 工作区状态：`dirty`

当前可见的 Dify 本地改动主要是未跟踪的部署配置：

- `docker/nginx/conf.d/*.conf`
- `docker/patches/`
- 多个 `docker/.env.backup.*`

### 5. `/home/ubuntu/openlist-apipages-src`

- Remote：`https://github.com/OpenListTeam/OpenList-APIPages.git`
- 分支：`main`
- 最近提交：`aef4399 fix(request): honor json body for application/json posts (#83)`
- 工作区状态：`clean`

## Waifu 插件路径

当前线上 Waifu 插件实际目录：

```text
/home/ubuntu/data/langbot/docker/data/plugins/Typer_Body__Waifu!
```

### 关键结论

这个插件目录本身 **不是独立 Git 仓库**。

确认结果：

- `pwd`：`/home/ubuntu/data/langbot/docker/data/plugins/Typer_Body__Waifu!`
- `git rev-parse --show-toplevel`：`/home/ubuntu/data/langbot`
- `git remote -v` 解析到的是父仓库：
  - `https://github.com/langbot-app/LangBot.git`

也就是说：

- 线上 Waifu 插件放在 `LangBot` 仓库工作树下面
- 但它本身位于 `docker/data/plugins/` 这种运行数据目录里
- 从 `LangBot` 顶层 `git status` 看，`docker/data/` 当前整体是未跟踪的

这意味着线上 Waifu 插件更像是：

- 运行时挂载出来的插件目录
- 落在 LangBot 仓库目录树中
- 但不受 LangBot 仓库正式版本控制

## Waifu 插件最近 Git 解析结果

因为它会回溯到父仓库，所以看到的是 LangBot 的 Git 信息：

- Remote：`https://github.com/langbot-app/LangBot.git`
- 最近提交：`cc4d883 fix: update langbot-plugin version to 0.3.8`

这不能代表插件自身版本，只能代表它所在父仓库当前 HEAD。

## Waifu 插件顶层结构

插件目录顶层当前可见：

```text
.env.example
.github/
README.md
assets/
cells/
components/
data/
main.py
manifest.yaml
organs/
static_data/
systems/
requirements.txt
```

同时还能看到多份现场备份文件，例如：

- `main.py.bak.*`
- `data/backup_*`

这说明线上目录里已经存在多轮人工修补与运行期备份痕迹。

## Waifu 插件核心文件清单

这批是最值得和本地 `openqqwaifu/src/waifu_standalone/` 对照的核心文件：

```text
./cells/__init__.py
./cells/cards.py
./cells/config.py
./cells/dify_service.py
./cells/generator.py
./cells/text_analyzer.py
./cells/xai_image_service.py
./components/commands/waifu_cmd.py
./components/commands/waifu_cmd.yaml
./components/event_listener/waifu_listener.py
./components/event_listener/waifu_listener.yaml
./organs/__init__.py
./organs/lru_cache.py
./organs/memories.py
./organs/memory_graph.py
./organs/memory_item.py
./organs/proactive.py
./organs/thoughts.py
./systems/__init__.py
./systems/emotions.py
./systems/events.py
./systems/narrator.py
./systems/searching.py
./systems/value_game.py
./main.py
./manifest.yaml
```

## Waifu 插件核心 Python 行数

只统计 `cells / components / organs / systems / main.py`：

```text
  151 ./cells/text_analyzer.py
    0 ./cells/__init__.py
  129 ./cells/config.py
  138 ./cells/cards.py
  282 ./cells/dify_service.py
  625 ./cells/generator.py
  121 ./cells/xai_image_service.py
  253 ./components/commands/waifu_cmd.py
 1303 ./components/event_listener/waifu_listener.py
 1970 ./organs/memories.py
   25 ./organs/lru_cache.py
    0 ./organs/__init__.py
  376 ./organs/proactive.py
  334 ./organs/memory_graph.py
   37 ./organs/memory_item.py
  184 ./organs/thoughts.py
  137 ./systems/value_game.py
    0 ./systems/__init__.py
  577 ./systems/searching.py
  364 ./systems/events.py
  468 ./systems/emotions.py
   64 ./systems/narrator.py
 7538 total
  673 ./main.py
```

合并 `main.py` 后，核心 Python 总规模约为：

```text
8211 lines
```

## 额外观察

### 1. 线上插件目录权限不一致

从 `ls -la` 看，插件目录下大部分文件属于 `root:root`，但 `main.py` 是：

```text
-rw-rw-r-- 1 ubuntu ubuntu 29203 Apr 13 14:48 main.py
```

这说明线上已经存在直接改文件、并且用户/权限混杂的情况。

### 2. `main.py` 被反复备份

目录里有多份：

- `main.py.bak.20260413095739`
- `main.py.bak.20260413102036`
- `main.py.bak.20260413110333`
- `main.py.bak.bot-account-20260413144837`
- `main.py.bak.bot-account-20260413144850`
- `main.py.bak.bot-id.20260413124000`
- `main.py.bak.reply-window.20260413123000`

这说明最近改动主要集中在 `main.py`，而且是线上直接修补式演进。

### 3. Waifu 插件运行数据和源码混放

插件目录下同时存在：

- 源码：`cells/ components/ organs/ systems/ main.py`
- 运行数据：`data/*.json`
- 备份数据：`data/backup_*`

这会让后续做“与上游 published project 的差异比对”时，需要先把：

- 核心源码
- 配置文件
- 运行时数据

分开看。

## 本地原始采集文件

本次原始输出已保存到本地：

- [server-repo-audit-155.248.164.183.txt](C:/qqbot/data/server-repo-audit-155.248.164.183.txt)
- [server-waifu-plugin-audit-155.248.164.183.txt](C:/qqbot/data/server-waifu-plugin-audit-155.248.164.183.txt)

## 下一步建议

如果要继续做“和本地独立版差多少”的判断，下一步最有价值的是：

1. 对照本地 `src/waifu_standalone/`
2. 逐个映射线上插件的：
   - `main.py`
   - `components/event_listener/waifu_listener.py`
   - `organs/memories.py`
   - `cells/generator.py`
   - `systems/searching.py`
   - `systems/emotions.py`
3. 明确哪些能力已经迁移，哪些还没有迁移

如果你要，我下一步可以直接基于这次采集结果，给你出一份：

- `线上 Typer_Body__Waifu! -> 本地 openqqwaifu` 的模块映射表
- 以及“还缺哪些核心能力”的差距文档
