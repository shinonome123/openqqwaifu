# 新版私聊运行链路：第一批实现

日期：2026-10-26。对应架构重置方案的 P0 基线和 P1 最小纵向闭环，尚未完成 P1 的全部目标，更不代表整个架构重置已完成。

最新进展：[经历跟进候选](RUNTIME_V2_FOLLOWUPS.md) 已接入，当前运行库为 schema 7；本文后续的版本与测试数量保留为阶段记录。

后续进展：结构化上下文与管理员「会话记录」页面已加入，当前行为见 [上下文与会话控制台](RUNTIME_V2_CONSOLE.md)。下文的首批测试数量与实现边界保留为阶段记录。

## 本批可运行的结果

私聊事件可以走独立运行链路：持久接收 → 按会话顺序创建轮次 → 调用现有回复生成 → 持久 Outbox → 发送 → 原子记账。HTTP 接收确认只等待入库，不等待模型；请求断开不会取消后台处理。

新版通过 `runtime_v2.enabled` 和私聊 QQ ID 列表显式开启，默认关闭。其他私聊和群聊仍走原流程。每个部署目前只支持一个配置明确的机器人账号，数据库元信息阻止其他账号复用同一运行库。

本批没有修改真实部署配置、用户 `.env`，也没有进行真实 QQ 发送或付费模型调用。

## 基线与改动位置

开发分支：`codex/runtime-v2-private-flow`。之前的修复与本批修改都尚未提交。

开始改造前保存了 `780651d` 加工作区修改的回退基线：本地 `.codex-temp/runtime-v2-baseline/` 中的 `manifest.json`、`working-changes.zip` 和 `tracked.patch`。快照包含当时 56 个修改/新增文件，不包含 `.env`。基线复核：Python 3.12.14，359 项 unittest 全部通过。

新增的 `runtime_v2` 包负责：

- `contracts.py`：不可变事件、角色快照、会话绑定、轮次上下文和交付回执。
- `store.py`：追加式 schema 起点、Inbox、轮次、结构化历史、Outbox、发送尝试、核对审计和操作系统持有的单写入者锁。
- `engine.py`：会话内顺序处理、跨会话有界并发、故障恢复和停机排空。
- `integration.py`：与旧服务的路由、角色快照、纯回复生成适配和出站适配。
- `demo.py`：不联网的完整演示。

旧入口增加接收结果类型和管理 API；HTTP/reverse WS 发送端保留并返回平台回执。旧消息处理路径保持默认行为。

## 先运行离线演示

在仓库根目录、受支持的 Python 3.11+ 环境运行：

```powershell
python -B run_cli.py runtime-v2-demo --output-dir .codex-temp/runtime-v2-demo-local
python -B run_cli.py runtime-v2-status --database .codex-temp/runtime-v2-demo-local/runtime-v2-preview.sqlite3
```

演示输出目录必须是新目录，防止覆盖之前的记录。演示不加载用户配置、不连接 NapCat、不调用真实模型，使用确定性生成器与模拟发送器，展示：

1. 接收后重启，再恢复处理。
2. 重复消息 ID 被持久去重。
3. 会话切换角色，之后的消息使用新角色；新角色不会读到旧角色历史。
4. 模拟发送超时，出站进入 `unknown`；再次处理也不会自动重发。

结果写入新目录中的 `report.json` 与 SQLite 数据库。`captured_attempts` 是模拟调用记录，最后一次特意模拟结果不明，不能据此断言消息已发送。

本次实际运行的报告保存在本机 `.codex-temp/runtime-v2-demo/report.json`，不在仓库中。

## 如何启用试运行

示例配置见 [config.runtime-v2.preview.json](../examples/config.runtime-v2.preview.json)。先替换机器人 QQ ID 与试验私聊 QQ ID；示例使用独立数据目录、8081 端口、关闭模型并捕获出站。HTTP 回调令牌为空时仍按现有规则拒绝回调，需要另行配置匹配的 inbound token 才能接收 NapCat 消息。

关键配置：

```json
{
  "bot_account_id": "机器人QQ号",
  "runtime_v2": {
    "enabled": true,
    "private_user_ids": ["试验对象QQ号"],
    "workers": 4,
    "max_pending_events": 1000
  },
  "qq_sidecar": { "dry_run": true }
}
```

可以用 `OPENQQWAIFU_RUNTIME_V2_ENABLED` 和 `OPENQQWAIFU_RUNTIME_V2_PRIVATE_USER_IDS` 覆盖开关及逗号分隔的私聊列表。没有机器人账号或私聊列表时启用会失败，不会解释成“对所有人启用”。

实际存储位置在配置的 `data_root` 下：捕获模式使用 `runtime-v2-preview.sqlite3`，真实出站使用 `runtime-v2.sqlite3`。两种模式不互相复用历史/队列，也不会把已有捕获记录补发到 QQ。

`dry_run` 只表示捕获出站；若启用了真实模型，仍会调用模型并产生相应费用。要完全离线验证，使用上面的 demo。真实环境的连接、重连与发送还需另做部署验收。

新版模型设置、路由名单和初始角色在启动时固定。修改模型或连接配置后重启生效；运行中改变账号或 dry-run 模式会阻止旧快照继续发送。现有全局“激活角色”只影响旧路径；新版会话通过下面的绑定命令切换。

## 管理与核对

以下端点在同步 HTTP 与 aiohttp 服务上都要求管理员登录，继续使用现有登录 cookie：

- `GET /api/v2/runtime`：队列计数、角色绑定、出站状态、失败原因码和工作线程错误；默认不返回消息正文。
- `GET /api/v2/runtime/delivery?outbox_id=...`：单次交付的正文、目标、角色、发送尝试与平台消息 ID，用于人工核对。
- `POST /api/v2/runtime/commands`：提交角色绑定或交付核对。

角色切换示例：

```json
{
  "action": "bind",
  "conversation_id": "试验对象QQ号",
  "character_id": "default",
  "request_id": "一个新的唯一操作标识"
}
```

命令按会话顺序进入队列，返回事件 ID 和顺序号。切换前的消息使用旧绑定，之后的消息使用新绑定。相同 request_id 与相同内容重复提交不会再次切换；相同 ID 搭配不同内容会拒绝。发送结果不明时切换也会等待核对。

核对示例：

```json
{
  "action": "discard",
  "outbox_id": "待核对的出站ID",
  "note": "核对依据或处理说明"
}
```

动作支持 `confirm_sent`、`discard`、`retry`。`retry` 还必须携带 `"acknowledge_duplicate_risk": true`；它复用持久回复提案，不重新调用模型。确认已发只补记本地历史，放弃则不把草稿加入 assistant 历史。所有核对记录备注与操作，不自动推断对方已读。

也可以在不启动服务的情况下只读查看一条出站：

```powershell
python -B run_cli.py runtime-v2-status --database PATH_TO_DATABASE --outbox-id OUTBOX_ID
```

第一批提供 API、CLI 和数据库记录；后续已增加管理员「会话记录」页面，支持轮次查询、角色绑定与交付核对。

## 本批已经建立的保证

- 有平台消息 ID 的重复入站，跨重启不重复创建轮次。没有 ID 的事件不做猜测性合并，可能重复，避免把用户真实连发误删。
- 入站容量超限时拒绝新事件，HTTP 返回可重试的 503；已接收事件保留在库中，重复事件可以正常确认。
- 每个会话只运行一轮，不同会话可并发；并发数有界，等待离线连接不会把任务无限堆入线程池。
- 角色卡内容以版本摘要和 JSON 快照固定在会话/轮次中；工作任务不读取全局当前角色来决定写入位置。
- 用户消息、已确认交付的回复保存在同一个 SQLite 库。草稿不冒充已发消息；dry-run 明确标记 `captured`，平台成功标记 `accepted`。
- 平台成功回执与历史写回之间发生本地故障时，恢复进入待核对，不直接重复发送。只拿到 HTTP 成功但缺少明确 OneBot 成功字段，也不会宣称已受理。
- 明确 `failed` 的 OneBot 回执与超时/异步受理分开处理。结果不明冻结该会话后续发言，其他会话继续运行。
- 未开始发送的 Outbox 在重启后复用；生成中断可重新生成，但不会调用本地工具。恢复使用新的模型配置版本并记录恢复审计，角色快照保持原值；这可能产生一次额外模型调用。
- 关闭先停止接单与领取新任务，等待在途工作；等待超时保留存储及写入者锁，不在后台工作仍使用它们时强制释放。

SQLite 运行库目前限定一个账号；操作系统锁阻止同一数据库有两个写入进程，重启不依赖猜测租约过期时间。每次真实网络发送仍存在平台与本地提交之间的不确定窗口，因此没有承诺端到端 exactly-once。

## 迁移边界与后续工作

这批实现只接入普通私聊生成与文本出站。没有把旧聊天历史、身份目录或知识库自动搬入新版，也没有接入工具执行、关系更新、主动任务、消息合并与完整删除屏障。图片输入可沿用现有模型适配能力，图片生成/发送未进入首批交付。

首批曾复用旧 `Generator` 的提示词格式；后续 P2 已改用结构化 `ReplyComposer`，分离人设与真实消息，并明确拒绝无法保留消息角色的提供商接口。由于本地工具还没有持久副作用账本，新版不绑定工具，`/skill` 给出明确的暂未接入提示。

旧版的“全量清空记忆”入口在新版启用时明确拒绝，不会错误地宣称已经清掉新版数据。新版数据删除/导入和界面需要后续专门实现。

存在新版会话时，直接禁用功能或删除名单中的已有会话会导致启动报错，防止静默回到旧引擎并丢失上下文。当前安全回退方式是保留新版库与所有未完成记录，使用能读取该 schema 的兼容代码版本；退回原始旧程序需要额外导出与迁移，不能靠删除数据库完成。

角色/消息上下文分层、会话记录界面、经历与纠正已在后续批次落地；[一次性持久提醒](RUNTIME_V2_REMINDERS.md) 与 [会话删除屏障](RUNTIME_V2_PRIVACY.md) 已接入，[主动联系规则](RUNTIME_V2_CONTACT_POLICY.md) 已接入，该阶段运行库为 schema 6。全面上线前还要完成全应用保留策略、备份/回退演练、真实 NapCat 集成和连续对话评估。

## 验证入口

```powershell
python -B -m unittest tests.test_runtime_v2 -v
python -B -m unittest discover -s tests -v
```

新版 25 项测试覆盖跨重启去重、生成恢复、已提交草稿恢复、发送不明、成功回执后本地故障、角色切换、角色历史隔离、队列背压、会话并发、停机超时、配置往返、明确平台回执，以及两个 HTTP 实现的管理员权限与核对接口。

完整回归 384 项通过（133.644 秒）；随后补充交付详情接口与权限断言，专项复测通过。137 个 Python 模块通过 AST 解析，wheel 构建成功。没有重新执行未改动的前端视觉检查。

本轮基线、专项和完整回归日志分别位于 `.codex-temp/runtime-v2-baseline-tests.log`、`.codex-temp/runtime-v2-focused-tests.log` 和 `.codex-temp/runtime-v2-full-tests.log`；离线演示不替代真实 QQ 集成测试。
