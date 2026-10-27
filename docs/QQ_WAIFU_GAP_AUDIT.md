# openqqwaifu 距离“真正的 QQ waifu”还有多远？

> 审计对象：当前工作区（HEAD `5865fa9` + 未提交改动）  
> 审计日期：2026-10-25  
> 审计方式：静态阅读 + 全量单测 + 本地探针；没有接真实 QQ，没有调用付费模型、生图或搜索接口。  
> 代码改动：无。本文只出报告。

---

## 0. 先说结论

**作为“QQ 机器人运行时 / 控制台”，它已经是一个能用的框架；作为“waifu”，它还差三层：连续记忆、群聊在场感、QQ 原生表达。**

如果 10 分是“真正像住在 QQ 里的 waifu”：

- **框架工程完成度：约 7 / 10**。NapCat 接入、角色卡、工具、权限、控制台、SQLite 状态、297 个单测都在。
- **waifu 体验完成度：约 3.5 / 10**。能接话、能搜、能画，但记不住、看不见群、不会主动、不像 QQ 用户。
- 如果只要求“能聊、能搜、能画、群里 @ 能回”，现在约 **6 / 10**；再往上，靠的不是加更多 skill，而是修记忆链路和群聊链路。

三个最直观的数字：

| 数字 | 含义 |
|---|---|
| **8** | 每轮真正送进提示词的历史只有 8 行，约 3–4 轮对话；而且当前消息还被 `[Latest Message]` 重复一次 |
| **3** | 每轮最多召回 3 条长期记忆；摘要也要和事实抢这 3 个名额 |
| **200** | 召回只在“最近 200 条”知识里选；更早的记忆即使更相关，也永远看不到 |

一句话：**它有记忆库，但没有记忆流；有技能系统，但触发面很窄；有 QQ 连接，但没有 QQ 表达。**

---

## 1. 审计口径与已验证的事实

- 全量测试通过：`.venv/bin/python -B -m unittest discover -s tests`，**297 passed**，约 43 秒。
- 当前工作区有未提交的记忆检索、工具授权改动；本报告按**当前工作树**判断，不按 `HEAD`。
- 当前 `data/cards/aurora_*.yaml` 是测试夹具内容（Profile 只有一两行），会显著拉低“人设体感”；这不代表框架只能做这么薄的角色卡，但确实是你现在看到的效果的一部分。
- 没有做真实 QQ / NapCat / 付费模型端到端验证；下面所有“能/不能”的结论来自代码路径、单测和本地探针。

---

## 2. 先说做得好的部分

不是全盘否定。这个项目的底子比“玩具”强：

- **职责拆分清楚**：NapCat 管协议，`runtime/` 管编排，`reply_flow/` 管提示词与生成，`memory/` 管会话与知识，`repositories/` 管 SQLite，`skills/` 管工具与 manifest。
- **单测覆盖广**：297 个测试，工具循环、记忆写入、权限闸门、OneBot 解析都有覆盖（虽然有覆盖不到真实链路的盲区）。
- **技能 manifest 化**：`SKILL.md` 有 schema、handler、risk_level、timeout、max_output_chars、telemetry；比“写死一堆 if/else”强很多。
- **角色隔离**：会话、知识、成员、运行时状态都按 character 分库/分目录；切换角色不会串记忆（但会清空，见 3.5）。
- **安全方向是对的**：未提交改动已经给 filesystem / command / network 工具加了用户白名单，并在 `ToolRegistry` 里做了执行期硬校验，不再只靠“模型别乱调”。
- **记忆检索在变好**：中文 bigram 分词、语义阈值、嵌入失败熔断、近似去重、去掉“高置信度但无关”的填充，这些都是正确方向。

这些是“能继续往上做”的基础。问题在于：**基础设施已经像产品，体验链路还像 demo。**

---

## 3. 记忆：有仓库，没有记忆流

### 3.1 名义上的三层，实际上的五层

| 层 | 存什么 | 何时写 | 何时进提示词 | 默认预算 | 评价 |
|---|---|---|---|---|---|
| `session_history` | 原始对话行 | 触发回复的消息 + 机器人回复 | 每轮最近 8 行 | 8 行（硬上限 120 行） | 太短；群里没 @ 的消息默认不入库（20 秒跟进窗口内除外） |
| `user_directory` | 称呼、群名片、画像摘要、好感度 | 群同步 / 称呼工具 / 抽取画像 | 当前说话人的 Speaker Notes | 画像 120 字、3 个片段 | 方向对，但太薄 |
| `knowledge_base` | 事实、偏好、关系、事件 | 每次回复后异步抽取，最多 2 条 | 每轮召回 3 条 | 候选池 200 条 | 写得多、读得少、没有生命周期 |
| `summary` | 历史归档摘要 | 开启摘要后，历史超过 30 行 | 只能和事实一起抢 3 个召回位 | 30 行后每轮 1 条（当前配置） | 不是“常驻摘要”，是“摘要垃圾” |
| 行为/好感 | 最近行为、affinity | 每条消息 / 每次回复 | 图谱高亮、关系阶段 | 内存 200 条 | 行为不落盘；好感只涨不跌 |

### 3.2 短期记忆：8 行不是“短期”，是“上一口气”

相关代码：[reply_flow/service.py:104](../src/waifu_standalone/reply_flow/service.py#L104)、[config.py:196](../src/waifu_standalone/config.py#L196)、[session_service.py:425](../src/waifu_standalone/memory/session_service.py#L425)。

具体问题：

1. **窗口只有 8 行**，且当前消息在 `asave_user_event` 时已经追加，`[Conversation]` 里会出现它，`[Latest Message]` 又重复一次。实际只有约 3.5 轮历史。
2. **群里没有 @ 机器人的消息默认不入库**（20 秒 follow-up window 内除外）。`handle_event_async_locked` 先过 `ignore_prefixes`，再过 `gate.should_reply`，最后才 `asave_user_event`（[reply_flow/service.py:45-51](../src/waifu_standalone/reply_flow/service.py#L45-L51)）。我本地验证：在没有跟进窗口时连发 3 条群消息再 @ 一次，历史里只有最后一条 @ 消息，前 3 条完全消失。
3. **历史没有时间戳**，提示词里也没有“当前时间”。机器人无法知道“现在几点”“今天周几”，也无法把“下周三”换算成日期；说“也不看看现在几点了”只能是碰巧。
4. **图片只变成一句“发送了图片”**。`image_payloads()` 从未被调用（[models.py:89](../src/waifu_standalone/models.py#L89)），没有视觉模型，也没有图片描述。
5. **引用回复、表情、语音、转发、小程序卡片会被丢掉**。`MessageKind` 只有 `text/image/mention`（[models.py:8](../src/waifu_standalone/models.py#L8)），`_parse_segments` 只解析 text/image/at（[web/http_api.py:101-115](../src/waifu_standalone/web/http_api.py#L101-L115)）。用户引用一条消息说“接着说”，机器人只看到“接着说”，看不到被引用的内容。
6. **窗口按条数，不按 token/字符预算**。8 行里如果有一行很长，可能挤掉其他上下文；反过来，短消息又浪费预算。
7. **历史硬上限 120 行**（[session_service.py:14](../src/waifu_standalone/memory/session_service.py#L14)），但 UI 允许把 `short_term_memory_limit` 调到 200；超过 120 的部分存不下来。
8. **回复换行被压平**：`_clean_response` 用 `re.sub(r"\s+", " ", ...)`（[prompt_assembler.py:2496](../src/waifu_standalone/reply_flow/prompt_assembler.py#L2496)），模型想发多行也会变成一行。长回复也没有切分（[onebot_actions.py:30](../src/waifu_standalone/gateways/onebot_actions.py#L30)），超长文本可能被平台截断。

**判定：不合理。** 8 行只够“接住当前这一句”，不够“记得刚才聊了什么”，更不够“群聊在场”。

### 3.3 长期记忆：写得比读得多

相关代码：[knowledge_service.py:163](../src/waifu_standalone/memory/knowledge_service.py#L163)、[runtime_state.py:2018](../src/waifu_standalone/repositories/runtime_state.py#L2018)。

写入侧：

- 每次机器人回复后异步抽取，最多 2 条；只从“触发回复的那条消息 + 最近 8 行”里抽。
- 群里没 @ 的消息既不回复也不抽取，所以机器人对群聊的长期认知只来自“别人点它”的那些片段。
- 抽取结果按 `scope_hint` 落到 group / member / person；但提示词没有定义这几个 scope 的边界，模型很容易把“我喜欢猫”写成 group 事实，导致下次别人问“我喜欢什么”时召回群主的偏好。
- 抽取没有“已有记忆”的上下文，不会更新/删除/过期，只会新增或近似去重。用户改口（“我现在不喜欢草莓蛋糕了”）会留下新旧两条，召回时可能同时出现，模型无法判断哪条是当前有效。

读取侧：

- 每轮最多 3 条（`memory_recall_limit=3`），由 `_merge_memory_hints` 截断（[reply_flow/service.py:119-123](../src/waifu_standalone/reply_flow/service.py#L119-L123)）。
- 召回只在**最近 200 条**里选（`candidate_limit=max(limit*20, 200)`，[runtime_state.py:2035](../src/waifu_standalone/repositories/runtime_state.py#L2035)）。超过 200 条后，旧记忆永远不会被考虑——这不是“长期记忆”，是“最近 200 条记忆”。
- 匹配是中文 bigram + 可选 embedding。当前部署开了 embedding，语义召回会比纯词法好；但没有 embedding 时非常脆：我本地纯词法探针里，“猫”这种单字查询直接没有 term；“我是做什么的”召不回“阿杰是程序员”；“推荐点动漫”召不回“群里讨论过《葬送的芙莉莲》”。
- 有 embedding 时也只是在最近 200 条里做语义重排，不能解决候选池截断，也不能解决“摘要不常驻”。
- 召回没有 recency / importance / access_count / decay；`confidence` 只加 0.18 权重，`archived` 只减 0.15，基本等于静态排序。
- `_existing_knowledge_entry` 为了去重会把**当前角色的全部知识**拉出来逐条比较（[knowledge_service.py:218-236](../src/waifu_standalone/memory/knowledge_service.py#L218-L236)），知识多时每次抽取都是 O(N)。

**判定：不合理。** 写侧每轮最多 2 条，读侧每轮 3 条，候选池 200 条；写得比读得多，存得比用得多。用户感觉就是“聊过的事它偶尔记得，但指望不上”。

### 3.4 中期记忆：当前配置反而在制造“摘要垃圾”

默认 `summarization_mode=False`；但当前 `data/docker-compose-config.json` 里是 `true`，`short_term_memory_limit=30`，`memory_summary_batch_size=12`。

实际行为（已用本地探针验证）：

- 历史到 32 行时，`archive_count = min(12, 32-30) = 2`，只把最旧 2 行拿去摘要，写一条 `memory_type="summary"` 的知识。
- 下一轮又到 32 行，再摘要 2 行，再写一条。只要摘要器返回非空，稳态下就是**每轮一条 2 行摘要**，而不是“每 12 条合并成一条滚动摘要”。
- 摘要调用发生在 `aemit` 的会话锁内（[emitter.py:107](../src/waifu_standalone/reply_flow/emitter.py#L107)、[service.py:508](../src/waifu_standalone/runtime/service.py#L508)），每轮多一次 LLM 调用。
- 摘要没有独立的 `[Earlier Conversation]` 段落，只能作为普通知识参与 3 条召回竞争。我本地用无嵌入探针验证：一条“两人从互相试探聊到交换童年经历”的摘要，问“在干嘛/陪我聊聊天/还记得我们聊过什么吗”都召回不到；即使开了 embedding，摘要仍然没有常驻位置，只能碰运气。

所以当前配置不是“补上中期记忆”，而是：

1. 每轮多一次摘要 LLM 调用；
2. 每轮多一条 2 行摘要知识；
3. 这些摘要挤进最近 200 条候选池，把真正的事实往后推；
4. 用户仍然看不到“我们之前聊过什么”的连续叙述。

**判定：不合理，而且当前配置很可能比关闭更糟。** 正确形态应该是“滚动摘要常驻提示词”，而不是“每轮生成一条摘要知识”。

### 3.5 两个会直接毁掉体验的数据问题

1. **“保存并启用 / 激活”当前角色会清空该角色的所有会话。**  
   `save_character_panel` 里 `activate_character(character, reset_sessions=True)`（[character/service.py:56-57](../src/waifu_standalone/character/service.py#L56-L57)），即使启用的是同一个角色。`_reset_character_runtime_context` 会调用 `memory.store.clear()`（[runtime/service.py:930-936](../src/waifu_standalone/runtime/service.py#L930-L936)）。我本地验证：保存一条会话后调用 `save_character_panel(..., set_active=True)`，历史从 `['用户: 记得我说过的话']` 变成 `[]`。  
   普通“保存”按钮传 `set_active=false`，不会清；但“保存并启用”和角色卡上的“启用”都会传 `true`。用户改完人设顺手点“保存并启用”，就会失忆。这不是“切换角色隔离”，这是“启用一下人设就失忆”。应该是显式“重置会话”按钮，而不是启用默认清空。

2. **`dry_run` 配置没有生效。**  
   `QQSidecarConfig.dry_run` 只在 config 里定义，运行时构建 outbound 时从未读取；`build_runtime_service` 在 `dry_run=True` 时仍然返回 `OneBotHttpOutboundPort`（[container.py:130-153](../src/waifu_standalone/runtime/container.py#L130-L153)）。这意味着你以为在“只捕获不发送”地测试，实际消息会真的发给 NapCat。文档里“dry_run 激活 CapturingOutboundPort”的说法与代码不符。

### 3.6 记忆判定

| 问题 | 严重度 | 结论 |
|---|---|---|
| 群消息默认不入库 | P0 | 机器人对群聊没有上下文 |
| 保存并启用/激活角色清空会话 | P0 | 直接数据丢失 |
| 摘要每轮一条、且不常驻 | P0 | 花钱制造噪音 |
| 召回只取 3 条 + 最近 200 条 | P1 | 长期记忆名不副实 |
| 无时间戳/当前时间 | P1 | 无法处理相对时间 |
| 无矛盾/过期/更新 | P1 | 记仇、记错、记旧 |
| 无通用“记住/忘掉”工具 | P1 | 用户说“记住”时模型没有可靠动作 |
| 画像 120 字 / 3 片段 | P2 | 长期关系装不下 |

**总评：记忆分层在代码结构上是成立的，在体验上不成立。** 它更像“会话日志 + 事实库 + 可选的摘要表”，还不是“短期工作记忆 + 中期情节记忆 + 长期语义记忆”的连续体。

### 3.7 建议的配比（只给方向，不是代码）

| 通道 | 现在 | 建议 |
|---|---|---|
| 短期历史 | 8 行，约 3–4 轮 | 24–30 行 / 2.5–3k 字符预算；包含群聊环境消息；带时间戳 |
| 滚动摘要 | 没有常驻；当前配置每轮一条 2 行摘要 | 1 条 600–800 字滚动摘要，永远放在 `[Earlier Conversation]` |
| 事实召回 | 3 条，和摘要抢名额 | 5–8 条；其中 1–2 条当前用户画像/偏好保底 |
| 召回候选 | 最近 200 条 | 分层：最近 N + 高重要度/高访问 + 语义 top-K；不要只按时间截断 |
| 写入频率 | 每轮最多 2 条事实 + 1 条摘要 | 每轮 1–2 条；按对话批次合并/更新，避免每轮一条摘要 |
| 用户画像 | 120 字 / 3 片段 | 300–500 字 / 5–8 片段；关系事件、承诺、纪念日单独存 |
| 关系状态 | 一个 affinity 分数 | affinity + 最近互动事件 + 未完成承诺 + 禁忌/边界 |

核心原则：**短期要够接住上下文，中期要常驻，长期要能被检索到；写入要少而准，读取要多而稳。**

---

## 4. 技能：框架是强项，触发和安全是短板

结论先行：**在“支持 tool calling 的后端 + 关键词命中 + 技能排进前 3”这三个条件同时满足时，技能能用；默认 Dify、显式 `/skill`、自然语言改写、越权文件读取这四种情况，分别对应“不能用 / 不可用 / 容易漏 / 不该用”。**

### 4.1 能用的部分

- 当前加载 17 个 skill，其中 15 个 ready，2 个是 `data/skills` 下的旧格式样本（`invalid_handler` / `duplicate_id`）。
- 12 个 tool skill：image、search、summary、summarize、skill-list、web-fetch、read-file、list-files、weather、search-links、write-file、exec-command；3 个 prompt policy：concise-answer、freshness-check、image-handoff。
- manifest 校验、tool alias、risk_level、timeout、max_output_chars、telemetry、skill pack / bundle 都在。
- 模型 tool loop 对 OpenAI / xAI / Claude 这类支持 function calling 的后端是通的；单测里有完整的 tool call → execute → tool message → final reply 流程。
- 未提交改动给 filesystem / command / network 工具加了 `authorized_user_ids` 白名单，并在 `execute_model` 里硬校验，模型即使直接吐工具名也绕不过去，这个方向是对的。

### 4.2 硬伤一：`/skill` 显式命令实际上不可用

README 写着“显式 `/skill` 调用”（[README.md:64](../README.md#L64)），但实际链路是：

1. `ignore_prefixes` 当前是 `["!", "/"]`，`handle_event_async_locked` 在第 45 行直接把 `/skill ...` 丢掉（[reply_flow/service.py:45](../src/waifu_standalone/reply_flow/service.py#L45)）。
2. 就算你把 `/` 从 `ignore_prefixes` 里去掉，第 84 行用的是 `svc.config.background_image_delivery`；`AppConfig` 没有这个字段，会抛 `AttributeError`（[reply_flow/service.py:84](../src/waifu_standalone/reply_flow/service.py#L84)）。这个值本来就是函数形参。
3. 测试里甚至明确断言 `/skill` 会被忽略（`test_slash_skill_command_no_longer_bypasses_tool_orchestrator`、`test_ignored_slash_summarize_does_not_use_legacy_dispatch`），也就是说这条路径是“故意废弃”的，但 README 和 `resolve_explicit_skill_request` 还留着。

结果：**用户以为有显式命令，实际没有；开发者以为有入口，实际一开就崩。**

### 4.3 硬伤二：每轮只暴露 3 个“关键词排名”技能

`candidate_manifests` 按关键词打分取前 `max_active_skills=3`（[registry.py:326-348](../src/waifu_standalone/skills/registry.py#L326-L348)）。模型能看到的工具面由这 3 个 skill 决定；没有排上的 tool skill，连它的原始 tool id 也会被 `skill_cards_by_tool` 抑制掉（[tool_registry.py:297-343](../src/waifu_standalone/skills/tool_registry.py#L297-L343)）。

本地验证（未授权用户，纯看暴露面）：

| 用户说 | 实际暴露的工具 |
|---|---|
| “你好呀” | image-command、search-command、summary-command |
| “帮我搜一下今天的新闻” | image-command、search-command |
| “今天天气怎么样” | image-command、weather-command |
| “帮我读一下 /etc/hosts” | image-command、search-command、summary-command（**没有 read-file**） |
| “帮我列一下当前目录” | image-command、search-command、summary-command（**没有 list-files**） |
| “列出技能” | image-command、search-command、summary-command（**没有 skill-list**） |

即使把用户加进 `authorized_user_ids`，“帮我读一下”仍然不暴露 read-file，因为 read-file-command 的关键词是“读文件/查看文件/打开文件”等，排不进前 3。模型不是“选择不调用”，是**根本看不到**。

这不是“安全限制”，是“关键词彩票”。正确的做法是：固定暴露一组低风险基础工具（搜索、天气、总结、生图、技能列表），其余按需补；高风险工具再由授权闸门控制。

### 4.4 硬伤三：默认后端 Dify 不支持 tool calling

`DifyLLMClient` 继承 `_BaseHTTPClient`，`supports_tool_calling` 返回 `False`（[llm_clients.py:107-109](../src/waifu_standalone/infra/llm_clients.py#L107-L109)）。而 `AppConfig.llm.backend` 默认是 `"dify"`。

结果：

- 默认配置下，所有 tool skill 都不会走 tool loop；`search-command` 等只是作为文本卡片，最终模型还是普通 chat，不会真的搜。
- UI 没有“当前模型不支持工具调用”的提示；用户只会觉得“它怎么不搜/不画/不读文件”。
- 当前 `data/docker-compose-config.json` 用的是 `xai`，所以这套部署能走工具；但一旦换回 Dify 或别的纯 chat 后端，skill 会静默退化。

### 4.5 硬伤四：`summarize` 可以越权读任意本地文件（安全问题，优先修）

这是本次审计最需要马上处理的一条：

- `summarize` 工具注册为 `risk_level="safe"`（[container.py:294-310](../src/waifu_standalone/runtime/container.py#L294-L310)），所以不在 `authorized_user_ids` 的高风险拦截范围内。
- 它的 `target` 接受本地路径；`_load_summarize_source` 直接 `Path(target).read_text()`，没有走 `_resolve_tool_path`，没有 allowed_roots，没有用户授权（[dispatcher.py:2124-2165](../src/waifu_standalone/skills/dispatcher.py#L2124-L2165)）。
- 我用临时目录里的 `secret.txt` 验证：`_load_summarize_source` 正常返回 `TOPSECRET-123`，路径在仓库外。
- `summarize-command` 对“帮我总结这个文件 /path”这类话会进入候选并被暴露；普通群成员就能让模型调用它。模型如果配合，或者被 prompt injection 诱导，就能把 `.env`、`data/docker-compose-config.json` 里的 API key 读出来，甚至走无 LLM 的 fallback 直接返回前 220 字。
- 它同时能抓任意 URL，绕过 `web-fetch` 的 network 授权闸门，存在 SSRF 面。

**这条不是“skill 好不好用”，是“skill 能不能安全地给人用”。** 在修好之前，不建议把机器人放进不信任的群。

### 4.6 其他技能问题

- `IntentRouter` 被构造但从未在运行时调用（[runtime/service.py:205](../src/waifu_standalone/runtime/service.py#L205)）；`dispatch_skill` 的关键词路径、`MemberOnboarding.maybe_handle` 也基本是死路径。技能触发只剩“模型 tool call”一条腿。
- 每轮 tool loop 最多 3 次；3 次工具调用后如果模型还没给最终文本，回复就是空，live runtime 下直接不发（[prompt_assembler.py:492-540](../src/waifu_standalone/reply_flow/prompt_assembler.py#L492-L540)）。
- 没有 rate limit / 配额。群里任何人都可以反复触发生图、搜索、summarize；生图是付费的，搜索是外部请求，没有 per-user / per-group 限流。
- `search-links` 跨轮依赖 `session.metadata["last_search"]`，但每轮开头 `_store_search_context_async(session, SearchContext())` 会把它清掉（[reply_flow/service.py:102-103](../src/waifu_standalone/reply_flow/service.py#L102-L103)）；同一轮内可用，跨轮追问来源就没了。
- 2 个 `data/skills` 旧格式样本一直显示 invalid/duplicate，实际运行的是内置 `summarize-command`；建议删除或转换，否则技能面板永远有红项。
- 没有真实 LLM / 真实 NapCat 的端到端测试；现有测试用 fake tool client，验证的是编排，不是线上可用性。

### 4.7 技能判定

| 能力 | 状态 |
|---|---|
| manifest / 校验 / telemetry / 权限框架 | 好 |
| 模型 tool loop | 有，但依赖后端支持 function calling |
| 自然语言触发 | 关键词前 3，容易漏 |
| 显式 `/skill` | 实际不可用，启用会崩 |
| 高风险工具隔离 | 方向对，但 `summarize` 绕过 |
| 限流 / 配额 | 没有 |
| 端到端验证 | 没有 |

**总评：技能系统“机制上能跑”，但“默认不好用、边界不安全”。** 它不是最弱的一环，但它是目前最容易让用户产生“怎么不听话”和“怎么乱读文件”的地方。

---

## 5. 离“真正的 QQ waifu”还差什么

把“真正的 QQ waifu”拆成 6 个可感知的能力，而不是功能列表：

| 维度 | 现在 | 分数 | 差距 |
|---|---|---|---|
| 能接住 QQ 消息 | 文本/图片/@ 能收发，群 @ 能回 | 7 | 基本盘没问题 |
| 像同一个人 | 角色卡 + 系统提示词，但卡很薄，extras 不进 prompt | 3.5 | 没有示例对话、口头禅、lorebook；当前卡还是测试夹具 |
| 记得住 | 8 行历史 + 3 条召回 + 200 条候选 | 3 | 跨天/跨周连续性几乎没有 |
| 在群里 | 只处理 @ 和 20 秒跟进窗口；不看群聊上下文 | 2.5 | 不知道群里在聊什么，不知道谁是谁，不会接梗 |
| QQ 原生表达 | 只有 text + image；没有引用/@回/表情/语音/戳一戳 | 2 | 发出去像“网页聊天机器人”，不像 QQ 用户 |
| 有生活感 | 无当前时间；proactive 只生成控制台草稿 | 1.5 | 不会早安晚安，不会隔天追问，不会主动找你 |
| 技能/工具 | 12 个工具，触发面窄，有安全洞 | 5 | 能用，但不可靠、不可放开 |
| 可靠/安全 | 297 测试、metrics、auth；但 dry_run 失效、无去重、无限流 | 4.5 | 工程底子好，产品边界没兜住 |

### 5.1 表达层：它不会“像 QQ 用户那样说话”

这是最直观的“不像 waifu”：

- 不会引用回复。群里别人问一句，它发一条裸消息，用户不知道它在回谁。
- 不会 @ 人。回复里没有真实 mention，只有文本。
- 不会发表情/表情包/戳一戳。`MessageKind` 只有三种；OneBot 的 face、mface、poke、record、video、forward、json 卡片全部丢弃。
- 不会发语音。语音消息连解析都没有。
- 不会发多行。`_clean_response` 把换行压成空格；长回复也不切分。
- 不会“看懂”图片。图片只变一句“发送了图片”，没有视觉模型，也没有图片描述。

**结果：即使文本内容很像人，交互形态仍然像“QQ 里的一个网页表单”。**

### 5.2 认知层：它不知道“现在”和“群里”

- 提示词里没有当前时间、日期、星期、时区。角色无法说“这么晚还不睡”，也无法处理“下周三”。
- 群聊不 @ 就默认不入库（20 秒跟进窗口是唯一例外）；机器人对群话题没有连续感知。
- 没有群成员 roster 进入提示词；它只知道当前说话人的画像，不知道群里还有谁、谁和谁在对话。
- 20 秒 follow-up window 内会接任何人的话，容易抢话/答错人。
- 没有“引用链”概念；用户回复某条消息时，上下文丢失。

### 5.3 关系层：好感是计数器，不是关系

- `EmotionSensor` 是关键词规则；历史超过 4 行就返回 `trust 0.55`，几乎恒定。
- `ValueGameEngine` 每回复一次就加分（base +0.08，私聊 +0.05，@ +0.03……），生气也只减 0.02；只涨不跌，没有事件、没有冷战、没有修复。
- `proactive_mode` 只在控制台生成草稿（[members/service.py:35-65](../src/waifu_standalone/members/service.py#L35-L65)），没有调度器，不会真的在第二天早上找你。
- 没有“承诺/约定/纪念日”的记忆结构；用户说“明天告诉我”，明天不会发生。
- 没有 per-user relationship state（除了一个 affinity 分数和 120 字画像）。

### 5.4 人设层：卡很薄，且 extras 不进 prompt

- `CharacterCard` 只有 assistant_name / user_name / language / profile / skills / background / rules / prologue；`extras` 被保存但从不进入 system_prompt（[cards.py:42-88](../src/waifu_standalone/character/cards.py#L42-L88)）。
- 没有 example dialogues、lorebook/world info、scenario、口头禅、禁忌、关系阶段台词。
- 当前 `data/cards/aurora_*.yaml` 是测试夹具，Profile 只有“群里说话利落”这类一行；这会让“人设感”直接掉一半。
- 没有风格校验；模型可以今天叫“极光”，明天换语气，除了 rules 里一句“自然口语化”没有约束。

### 5.5 工程层：几个会直接打断体验的坑

- **保存并启用/激活当前角色会清空会话**（P0，已验证）。
- **dry_run 不生效**（P0，已验证）：以为在彩排，实际在真发。
- **没有 message_id 去重**：NapCat 回调重试可能让同一条消息回两遍。
- **没有限流**：生图/搜索/summarize 可被群友刷。
- **后台知识抽取只有 4 个线程**，每轮最多 2 条 + 摘要 1 条；高峰时队列会积压，抽取用的是旧上下文。
- **LLM 异常被吞**：live runtime 下返回空，用户看到的是“已读不回”。
- **`data/docker-compose-config.json` 有真实 key，`.env` 未被 `.gitignore` 忽略**；不要 `git add .`。另外，已授权用户可以通过 `read-file` 读 `.env`（allowed_roots 包含 `.`），通过 `summarize` 更是任意路径可读。

---

## 6. 优先级路线图（不改代码，只排顺序）

### P0：先止血（1–3 天）

1. **修 `summarize` 越权读文件**  
   走 `_resolve_tool_path` + `allowed_roots` + `authorized_user_ids`；risk 改为 filesystem/network；禁止 `.env`、`data/` 下配置文件；加“仓库外文件不可读”的测试。  
   相关：[dispatcher.py:2124](../src/waifu_standalone/skills/dispatcher.py#L2124)、[container.py:294](../src/waifu_standalone/runtime/container.py#L294)。

2. **停止“保存并启用 = 清空会话”**  
   `save_character_panel` 在 `set_active=true` 时不要再传 `reset_sessions=True`；真要清空，做成独立按钮 + 二次确认。  
   相关：[character/service.py:56](../src/waifu_standalone/character/service.py#L56)。

3. **让 `dry_run` 真的 dry-run**  
   `_build_runtime_outbound` 先看 `qq_sidecar.dry_run`，返回 `CapturingOutboundPort`；加测试。  
   相关：[container.py:130](../src/waifu_standalone/runtime/container.py#L130)。

4. **处理 `/skill`：要么修，要么删干净**  
   修：放行 `/skill` 前缀、第 84 行改用形参、补端到端测试；删：去掉 README 的“显式 `/skill`”、删 `resolve_explicit_skill_request` 死路径。不要留“看起来有、实际崩”的中间态。

5. **群消息入库 + 历史预算**  
   没 @ 的群消息也写入环形缓冲；`history_window_messages` 提到 24–30，加 `history_window_chars`（建议 2–3k）；当前消息不要重复；机器人自己的 @ 从历史里去掉。

6. **把“摘要”改成常驻滚动摘要**  
   不再每轮写一条 2 行 summary 知识；改成 `session.metadata["rolling_summary"]`（600 字内），单独作为 `[Earlier Conversation]` 放在 `[Conversation]` 前；摘要调用放后台，不占会话锁。

7. **稳定工具面 + 后端能力提示**  
   固定暴露搜索/天气/总结/生图/技能列表这组低风险工具；其余按排名补。Dify 等不支持 function calling 的后端，在 AI 配置页明确标红“技能不会执行”。

8. **基础卫生**  
   message_id 去重；per-user/per-group 限流；`.env` 加入 `.gitignore`；`read-file` 默认 roots 收窄到 `data/`，或加 secrets 黑名单。

### P1：让记忆“像记忆”（1–2 周）

- 记忆生命周期：`last_accessed` / `access_count` / 重要度 / 时间衰减；过期 `event` 类记忆；矛盾检测（新事实覆盖旧事实或标记“已变更”）。
- 查询改写 + 实体解析：把“我是做什么的”“推荐点动漫”改写成带主题的检索；当前说话人没主题时，优先注入其 profile summary + 最近 3 条 member facts。
- 通用记忆工具：`remember-fact` / `forget-fact`，让“记住/忘掉”有真实动作，而不是让模型在“没有工具”和“乱调称呼工具”之间二选一。
- 群成员 roster + 引用链：知道群里谁是谁、谁在回谁；把 reply/quote 解析成上下文。
- 当前时间 + 时区：写入 `[State]`；相对日期在抽取时转绝对日期。
- 图片理解：至少接一个 vision caption，把图片描述写进历史/知识；否则 `multimodal_enabled=true` 是误导。
- QQ 表达：引用回复、@ 发送者、保留换行、长回复切分、face/mface 解析。

### P2：让它“像 waifu”（2–4 周）

- 主动调度器：早安/晚安、隔天跟进、承诺提醒、久未联系；不是控制台草稿。
- 关系事件：好感不只涨；冷战、道歉、纪念日、共同经历；per-user relationship state。
- 人设深度：example dialogues、lorebook、口头禅、场景卡、关系阶段台词；`extras` 进 prompt。
- 语音/表情包输出；QQ 原生互动（戳一戳、引用、回复）。
- 评测集：20–50 个真实场景（跨天记忆、群聊插话、引用追问、改口、忘记、限流），而不是只测 manifest。

---

## 7. 如果只做三件事

1. **修 `summarize` 越权 + 保存并启用清空会话**（安全和数据丢失，先止血）。
2. **让群消息入库，并把历史窗口提到 24–30 + 常驻滚动摘要**（这是“记得住”和“在群里”的地基）。
3. **固定暴露 5 个基础工具，修 `/skill` 或删掉它，Dify 用户明确提示不支持工具调用**（这是“技能能不能用”的关键）。

做完这三件，体验会从“偶尔能聊的 QQ bot”变成“能连续聊、知道群里在发生什么、工具可预期”的 waifu 雏形；大概能从 3.5 分到 6 分。再往上，才是时间感、主动性、QQ 原生表达和人设深度。

---

## 附录 A：关键证据文件

| 主题 | 文件 |
|---|---|
| 历史窗口 / 记忆配置 | [config.py:166-200](../src/waifu_standalone/config.py#L166-L200) |
| 回复链路 / 群消息入库 / `/skill` | [reply_flow/service.py:38-123](../src/waifu_standalone/reply_flow/service.py#L38-L123) |
| 历史存储 / 120 行上限 | [memory/session_service.py:14](../src/waifu_standalone/memory/session_service.py#L14)、[:425](../src/waifu_standalone/memory/session_service.py#L425) |
| 知识抽取 / 去重 / scope | [memory/knowledge_service.py:163-320](../src/waifu_standalone/memory/knowledge_service.py#L163-L320) |
| 召回 / 200 候选池 | [repositories/runtime_state.py:2018-2085](../src/waifu_standalone/repositories/runtime_state.py#L2018-L2085) |
| 工具暴露 / 授权 | [skills/tool_orchestrator.py:86-125](../src/waifu_standalone/skills/tool_orchestrator.py#L86-L125)、[skills/tool_registry.py:297-343](../src/waifu_standalone/skills/tool_registry.py#L297-L343) |
| 技能候选 top-3 | [skills/registry.py:326-348](../src/waifu_standalone/skills/registry.py#L326-L348) |
| 显式技能 / summarize | [skills/dispatcher.py:103-114](../src/waifu_standalone/skills/dispatcher.py#L103-L114)、[:2124-2165](../src/waifu_standalone/skills/dispatcher.py#L2124-L2165) |
| Dify 不支持工具 | [infra/llm_clients.py:107-109](../src/waifu_standalone/infra/llm_clients.py#L107-L109) |
| 角色保存清空会话 | [character/service.py:56-57](../src/waifu_standalone/character/service.py#L56-L57) |
| QQ 消息类型 | [models.py:8](../src/waifu_standalone/models.py#L8)、[web/http_api.py:101-115](../src/waifu_standalone/web/http_api.py#L101-L115) |
| 回复压平 / 不切分 | [prompt_assembler.py:2496](../src/waifu_standalone/reply_flow/prompt_assembler.py#L2496)、[onebot_actions.py:30](../src/waifu_standalone/gateways/onebot_actions.py#L30) |

## 附录 B：本地探针（未改代码）

| 探针 | 结果 |
|---|---|
| 297 个单测 | 全过 |
| 群里 3 条未 @ 消息 + 1 条 @ 消息 | 历史只有 @ 那条 |
| 保存并启用/激活角色 | 会话从 1 条变 0 条 |
| `dry_run=True` + 真实 outbound URL | 返回 `OneBotHttpOutboundPort`，不是 Capturing |
| 开启摘要、30 行阈值 | 每轮新增 1 条 2 行 summary，历史稳定 30 行 |
| 摘要召回（无嵌入探针） | “在干嘛/陪我聊聊天/还记得我们聊过什么吗”召回不到 |
| “猫” / “我是做什么的” / “推荐点动漫”（无嵌入探针） | 召回为空 |
| `summarize` 读仓库外临时文件 | 返回文件内容 |
| “帮我总结这个文件 .env” | summarize-command 进入候选并暴露 |
| “帮我读一下 /etc/hosts” | read-file 不暴露（关键词没排进前 3） |
| `/skill skill-list`（默认前缀） | 被忽略，返回 `None` |
| `/skill skill-list`（去掉 `/` 前缀） | `AttributeError: background_image_delivery` |

## 附录 C：本轮没有验证的东西

- 真实 NapCat 连接、真实 QQ 收发、消息撤回/重试。
- 真实 xAI / Dify / 嵌入 / 生图接口的端到端表现。
- Docker Compose 部署与重启恢复。
- 多人并发下的会话锁、后台任务队列、SQLite 写竞争。
- 技能市场 / skill pack 导入的真实包兼容性。

这些不影响上面的结构性结论，但会影响“上线后到底稳不稳”的判断；如果要上线，建议另做一轮集成测试。
