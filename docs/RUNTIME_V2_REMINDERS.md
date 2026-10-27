# 新版私聊：持久提醒

日期：2026-10-26。接在 [事实、经历与纠正](RUNTIME_V2_MEMORY.md) 之后，新增明确授权的一次性提醒，包括创建、延期、取消、重启恢复和发送核对。仅适用于 `runtime_v2` 名单中的私聊；陪伴式主动跟进、周期提醒和群聊提醒尚未开放。

最新进展：[经历跟进候选](RUNTIME_V2_FOLLOWUPS.md) 已接入，当前运行库为 schema 7；本文后续的版本与测试数量保留为阶段记录。

后续已接入 [会话删除与恢复屏障](RUNTIME_V2_PRIVACY.md) 和 [主动联系规则](RUNTIME_V2_CONTACT_POLICY.md)，该阶段运行库为 schema 6。提醒遵守逐私聊的暂停、静默时段与频率上限。下文 schema 4 和 459 项测试是提醒阶段的验证记录。

## 开启与使用

提醒默认关闭。先按 [试运行说明](RUNTIME_V2_FIRST_SLICE.md) 配置账号、私聊名单及捕获/真实发送模式，再增加以下配置并重启：

```json
{
  "runtime_v2": {
    "reminders_enabled": true,
    "reminder_timezone": "Asia/Shanghai",
    "reminder_grace_seconds": 300
  }
}
```

`reminder_timezone` 是部署者为当前试运行用户确认的默认时区，可以留空；不能用开发电脑时区推断用户所在地。不同私聊可以用 `/contact timezone` 确认自己的时区，也可在单条提醒中明确指定。环境变量支持 `OPENQQWAIFU_RUNTIME_V2_REMINDERS_ENABLED` 和 `OPENQQWAIFU_RUNTIME_V2_REMINDER_TIMEZONE`。补发窗口为 1—86400 秒，默认 300 秒。

私聊用户可发送以下命令，由程序直接解析，不经过模型：

```text
/remind +10m | 喝水
10分钟后提醒我喝水
/remind 2026-12-01T09:00 Asia/Shanghai | 准备面试
/remind list
/remind postpone 提醒ID +30m
/remind cancel 提醒ID
```

`+10m`、`+2h`、`+1d` 分别表示经过 10 分钟、2 小时、24 小时。用户命令从可信的消息接收时间起算；排队不会悄悄推迟执行点。处理命令时已经超过补发窗口会拒绝创建，要求重新指定时间。静默开启且无单条例外时，相对时长也需要确认时区，以判断是否会打扰用户；仅显示 UTC 不等于确认用户时区。

绝对时间必须带有明确的 IANA 时区，或使用已经配置的默认值，不能只提供 UTC 偏移。支持未来 366 天内的时间。不支持猜测“明天上午”等模糊表达，程序返回格式说明，不承诺已安排。仅谈到面试等计划不会创建任务，模型回复也不能自行创建任务。

夏令时跳过的本地时间被拒绝。回拨产生两次同名时间时，要求明确选择，例如：

```text
/remind 2026-11-01T01:30 America/New_York fold=1 | 提醒内容
```

`fold=0` 为第一次，`fold=1` 为第二次。不会简单把当地日期加 24 小时来计算下一天。静默时段的提醒需要显式添加 `allow-quiet`；延期时也需重新确认，且不能绕过暂停或频率上限。

## 会话控制台

管理员进入「会话记录」，选择私聊后可看到提醒列表，包含执行时间、时区、状态、暂停/取消原因、角色与任务版本。页面明确区分模拟捕获和真实发送。

从该会话当前角色处理过的用户消息点击“创建提醒”，填写内容、本地时间、IANA 时区、原文证据、操作说明，并勾选已经核实授权。可选关联此会话同一角色下尚未结束的经历 ID。程序核对来源、逐字摘录和授权勾选；管理员负责确认原文是否构成具体授权，原文匹配本身不能证明授权语义。

“延期 / 重新安排”会重新检查当前角色绑定与关联经历，并更新任务版本。“取消提醒”立即在事务中生效，不排在提醒发送后面等待。页面操作有请求 ID 幂等和目标版本校验；多窗口基于旧版本操作会被拒绝，刷新后重新核对。

“来源与操作记录”可查看证据、真实登录管理员账号、操作说明和每次触发对应的发送详情。任务触发在时间线上独立显示，不生成虚假的用户消息；只有确认受理或模拟捕获的提醒正文进入 assistant 历史。

## 持久化与发送边界

提醒阶段引入 schema 4，追加 `reminder_tasks`、`reminder_occurrences` 和 `reminder_requests`。schema 1/2/3 顺序升级，原消息、记忆、角色版本、上下文报告和 Outbox 保留。当前已继续升级到 schema 7，回退须使用兼容当前版本的代码；旧版程序不能直接打开新库，备份恢复还需遵守删除记录库的屏障。

任务保存 UTC 执行点/截止点、当地时间、IANA 时区、来源与授权依据、账号所属数据库、会话、角色快照、绑定代次及可选关联经历版本。原始时间表达保留在请求账本中。账号数据库与 capture/live 模式继续隔离，使用既有操作系统单写入者锁，未开放多实例调度。

后台约每 0.5 秒扫描，将到期任务事务写成唯一 `(task_id, task_version)` 的 `reminder_due` Inbox 事件，进入同一会话队列。任务与 occurrence 同事务提交；重启不会重复入队。保存过的 Outbox 直接复用；到期正文固定为“提醒：内容”，不调用模型。

发送前在 `begin_dispatch` 事务中重新检查：任务版本、取消状态、运行时/提醒开关、私聊名单与账号、当前角色绑定代次、关联经历状态/版本、补发截止时间。发送器仍负责连接可用性和真实平台回执。

- 角色切换导致暂停；切回来不会自动恢复，需要明确重新安排。
- 关联经历已完成、取消或撤回时停止提醒；版本变化或已入队但尚未处理的人工修订会先暂停，避免旧提醒抢先发送。
- 新消息中的精确取消/延期命令在成功入站时立即处理副作用，其正常回复仍按会话队列发送。暂停此私聊全部计划联系可用 `/contact pause`；支持的精确自然语言和完整边界见 [主动联系规则](RUNTIME_V2_CONTACT_POLICY.md)。
- 创建与延期不会由模型提案触发。事实、经历自动提取不授予主动联系权限。
- 每个私聊最多 20 条未结束提醒；联系频率默认每滚动 24 小时最多 20 次尝试、至少间隔 60 秒，可按私聊调整；每轮调度最多入队 32 条，并受 Inbox 总容量限制。
- 默认晚到不超过 5 分钟可补发一次；延迟达到 1 分钟会写入提醒正文。超窗标为已过期。服务停机时不执行外部任务，重启只在补发窗口内恢复。

取消与开始发送以持久化 `dispatching` 为边界：取消先提交，就阻止发送；发送先开始，就不能保证撤回。后一种情况记录 `cancel_requested`，保留在途/待核对状态，不谎报“已取消本次消息”。

进程在发送中退出或网络结果不明时，恢复为 `unknown`/`needs_review`，禁止自动重发，并保留会话发送屏障。管理员可以核对后确认已发或放弃；重试需要确认可能重复，且再次检查原授权、绑定与时效。取消或超窗后不允许重试。任务完成只代表通知流程结束，不把相关经历自动标记为完成。

`accepted` 仍仅表示平台受理，`captured` 仅表示模拟捕获，均不表示对方已读。

## 管理接口

同步与 aiohttp 服务均要求管理员登录：

```text
GET /api/v2/runtime/reminders?conversation_id=...&limit=30&before_id=...
GET /api/v2/runtime/reminders?conversation_id=...&task_id=...
POST /api/v2/runtime/commands
```

列表返回 `tasks`、`next_before_id`、`enabled`、`default_timezone`、`grace_seconds` 和 `mode`；`limit` 为 1—100。详情返回任务、最近 100 条操作及触发记录/Outbox 链接。创建示例：

```json
{
  "action": "reminder",
  "operation": "create",
  "conversation_id": "私聊QQ号",
  "character_id": "quiet-reader",
  "source_event_id": "用户消息事件ID",
  "evidence": "请在12月1日9点提醒我面试",
  "text": "准备面试",
  "when": "2026-12-01T09:00",
  "timezone": "Asia/Shanghai",
  "fold": null,
  "authorized": true,
  "note": "已核对用户授权、日期和时区",
  "request_id": "此次操作唯一ID",
  "linked_memory_id": ""
}
```

延期用 `operation=postpone`，取消用 `operation=cancel`；均提供 `task_id`、`expected_version`、`authorized`、`note` 和新的 `request_id`。延期另带 `when`、`timezone`、可选 `fold`。创建/延期均可显式传 `allow_quiet=true` 确认该时间的静默例外，默认 false。操作账号从已认证会话读取，请求正文不能伪造身份。

## 验证与范围

```powershell
python -B -m unittest tests.test_runtime_v2_reminders tests.test_runtime_v2.RuntimeHttpTests -v
python -B -m unittest discover -s tests -v
```

专项覆盖确定性命令、DST、时区、排队计时、授权/范围、幂等与版本冲突、重启去重和草稿复用、发送竞争、结果不明核对、角色/经历变更、动态禁用、过期与限速、后台到期唤醒、配置和迁移。两个 HTTP 服务验证未登录/非管理员拒绝访问、来源核对、真实账号审计和旧版本拒绝。

最终使用 Python 3.12.14 完成 459 项完整回归，全部通过（150.751 秒）。JavaScript 语法检查与 `git diff --check` 通过。

浏览器使用临时数据库与模拟生成/发送器，检查创建、延期、取消、操作记录、提醒时间线、unknown 取消后的核对入口、手机布局、中英文与 HTML 纯文本显示。本批未进行真实 QQ 发送、付费模型调用或多日运行验收。

浏览器还复现并修复了连续打开确认框时，旧弹窗关闭动画清掉新弹窗的问题；7 组页面验证均通过，无脚本异常或外部网络请求。临时测试服务已关闭。

本地验证记录位于 `.codex-temp/runtime-v2-reminders-*.log` 和 `.codex-temp/runtime-reminder-smoke-results.json`，不随程序发布。新版私聊删除屏障及联系规则已在后续批次接入；工具副作用账本、自动陪伴跟进、跨运行路径的全局勿扰、旧数据迁移及生产备份恢复演练仍属后续工作。
