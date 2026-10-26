# openqqwaifu 长期重构路线图

> 日期：2026-10-26
> 基线：分支 `codex/runtime-ui-and-image-fixes`，HEAD `5865fa9`，加上未提交的 7 组改动（见 [HANDOFF.md](HANDOFF.md) 第 2 节）；328 个测试全部通过。
> 姊妹文档：[QQ_WAIFU_GAP_AUDIT.md](QQ_WAIFU_GAP_AUDIT.md) 讲“体验差在哪、先补什么功能”；[HANDOFF.md](HANDOFF.md) 讲“代码现在处于什么状态”；本文讲“代码结构要怎么改，那些功能才做得好、做得稳”。
> 文中的行数、计数是 2026-10-26 的快照，改动后会漂移，以函数名为准。度量口径见附录 B。

---

## 0. 一页纸

### 现状判断

包已经按职责拆开了（`runtime/`、`reply_flow/`、`memory/`、`skills/`……），但**边界没有拆开**：

- **中枢还在。** `WaifuService`（`runtime/service.py`）有 117 个方法（55 个私有）、56 个字段。拆出去的子系统通过 **99 处** `svc._私有成员` 反向调用它，涉及 38 个不同的私有成员。名义上分了模块，实际上是双向耦合。
- **同一件事写两遍。**
  - 约 **136 组**同步/异步孪生方法（`xxx` 和 `axxx`）。
  - 两套运行时状态存储：内存版 735 行、SQLite 版 1178 行。
  - 两层 HTTP：同步门面 `web/http_api.py` 1459 行，aiohttp 路由 `web/http_api_async.py` 1027 行，同一个接口要声明两次。
- **会话是整份快照的读—改—写。** 9 个调用点分布在 5 个文件里，各自加载整份会话、修改、再整份保存。（审计发现）历史数据里出现过一次快照回退。
- **消息模型太窄。** 入站只认 text、image、mention 三种段落，其余全部丢弃；出站只有“文本加若干图片”一种形态。
- **配置要改好几处。**
  - `AppConfig` 有 55 个顶层字段，`ConfigManager.load()` 逐个手写映射。
  - 另外还有 25 处手写的环境变量覆盖，以及控制台的字段映射。
  - 新增一个配置要改 3–4 处，漏改 `load()` 时，配置文件里的值会被静默忽略。
- **三个文件超过 2200 有效行**：`reply_flow/prompt_assembler.py` 2493、`skills/dispatcher.py` 2394、`repositories/runtime_state.py` 2237。

**后果：** 做群聊在场、QQ 原生表达、关系与主动这类体验功能时，每个功能都要同时改 5–8 个文件，同步、异步两条路径各改一遍，还容易引入并发丢更新。刚完成的 A/B/C 三块就是这样：约 1940 行改动分散在 27 个文件里。

### 目标形态

一条显式的消息管线，事件日志式的状态，纯异步的内核，`WaifuService` 只负责装配。

```
NapCat / OneBot
   │  gateway：解析成富消息模型、去重
   ▼
Inbound → Gate → BuildContext → Plan（可选）→ Generate（含工具循环）→ Render → Outbound
   │        回不回   预算化的提示词段落    分析          授权闸门            QQ 段落、分条
   │
   └─ 消息事件日志（SQLite，只追加）
         ▲
         └─ 后台任务队列：记忆抽取 / 滚动摘要 / 嵌入 / 主动消息 / 图片描述
```

### 阶段总览

| 阶段 | 主题 | 解决什么 | 估算（单人加 AI 辅助，仅供参考） | 前置 |
|---|---|---|---|---|
| 0 | 收尾与止血 | 提交现有改动，修剩余 P0，真机验证 | 3–5 天 | 无 |
| 1 | 安全网 | 场景测试、提示词快照、实测评测集、测试卫生 | 1–2 周 | 0 |
| 2 | 消息模型与管线 | 富消息模型、`TurnContext`、提示词分段与预算 | 2–3 周 | 1 |
| 3 | 状态与存储 | 消息事件日志、记忆模型 v2、只留一套存储实现 | 2–3 周 | 2 |
| 4 | 运行时 | 纯异步内核、统一后台任务队列 | 约 2 周 | 3 |
| 5 | 拆中枢与大文件 | 显式依赖、拆三个大文件、删死代码和兼容壳 | 2–3 周 | 4 |
| 6 | 配置与扩展边界 | 配置单点声明，技能内核与外围扩展分离 | 1–2 周 | 5 |
| 7 | 体验能力 | 记忆生命周期、群聊在场、QQ 表达、看图、关系、主动 | 持续 | 2、3、4 |

**关键路径：0 → 1 → 2 → 3。** 做完这四段，体验功能（阶段 7）就有了地基。阶段 4–6 主要解决可维护性，可以和阶段 7 交替进行。时间不够时，至少做完 0–3。

### 原则

1. **不大爆炸。** 每个阶段拆成多个小 PR，每个 PR 测试全绿；行为要么不变，要么有快照证明是有意改变。
2. **先安全网，后重构。** 场景测试和提示词快照就位之前，不动提示词结构和回复管线。
3. **先解耦，再加功能。** 新功能写进新结构，不再往 `WaifuService` 和三个大文件里加东西。
4. **数据迁移可回滚。** 任何存储格式变化都要有备份、演练和回滚步骤。
5. **用数字跟踪。** 每个阶段结束时更新附录 A 的度量。

---

## 1. 阶段 0：收尾与止血（现在，3–5 天）

这就是“下一步”。它不是重构，而是把手头的工作落地，并修掉会丢数据、会误发消息的问题。

1. **提交现有改动。** 约 1940 行改动全在未提交的工作区里，这是目前最大的单点风险。
   - 新建分支（例如 `feat/memory-and-skill-overhaul`），按 HANDOFF.md 第 2 节的 7 组拆成提交。混在同一文件里的改动用 `git add -p` 拆开。
   - 开 PR 合入 main。不要在已合并的 `codex/runtime-ui-and-image-fixes` 上继续提交。
2. **对齐线上配置。**
   - `data/docker-compose-config.json` 里的 `history_window_messages: 8` 和 `max_thinking_words: 30` 会覆盖新默认值（30 和 80），删掉这两项或改成新值。
   - 确认 `timezone`，以及 `summarization_mode` 与 `memory_summary_batch_size` 的组合是你想要的。
3. **决定角色卡。** `data/cards/aurora_*.yaml` 现在是测试夹具内容，原人设只剩 `aurora.meta.json` 里的 8 行。是否写回由你决定。人设太薄会直接拉低体验判断。
4. **修剩余的 P0。** 每项都是小改动，配一个测试：
   - **`dry_run` 不生效。** `runtime/container.py::_build_runtime_outbound` 从不读取 `qq_sidecar.dry_run`，只要配了出站地址就会真发。改成 `dry_run` 时返回 `CapturingOutboundPort`。
   - **启用角色会清空会话。** `character/service.py::save_character_panel` 在 `set_active` 时传 `reset_sessions=True`，启用的是同一个角色也会清空。改成启用不清空，“重置会话”做成单独的操作并二次确认。
   - **URL 抓取不校验目标。** `summarize` 和 `web-fetch` 抓取前拒绝私有、回环、链路本地地址；跟随重定向后对最终地址再校验一次；加响应大小上限。
   - **入站消息不去重。** 按 `message_id` 做短期去重；没有 `message_id` 时，按发送者、内容和时间窗去重，避免回调重试导致重复回复。
   - **付费和外部工具不限流。** 生图、搜索、抓取按用户和群各加一个简单的令牌桶。
   - **仓库卫生。** `.env` 加入 `.gitignore`；删除 `data/skills/` 下两个旧格式文件，它们让技能面板一直显示校验错误。
   - 状态（2026-10-26）：除删除 `data/skills/` 的两个旧文件外都已修，在分支 `fix/stage0-p0`，见 HANDOFF.md 第 2.1 节。
5. **真机冒烟。** `dry_run` 修好之后，先用它跑一遍 A/B/C，再接一个真实的小群验证：环境消息并入、滚动摘要、`/skill`、`生图` 前缀、长回复分条、当前时间。

**验收：** PR 合入 main；每个 P0 都有测试；真机冒烟的结果记进 HANDOFF.md。

### 与差距审计 P0 的对应

`QQ_WAIFU_GAP_AUDIT.md` 写于 A/B/C 之前，它的 P0 清单现在的状态如下：

| 审计 P0 | 状态 |
|---|---|
| `summarize` 读任意本地文件 | 已修：本地文件只对授权用户开放 |
| `summarize` 和 `web-fetch` 抓 URL 不校验目标 | 已修（PR #27，`infra/safe_fetch.py`） |
| 保存并启用角色会清空会话 | 已修（`fix/stage0-p0`）：启用不清空，重置会话是单独的二次确认操作 |
| `dry_run` 不生效 | 已修（`fix/stage0-p0`） |
| `/skill` 不可用 | 已修 |
| 群消息入库，历史窗口加预算 | 已修：内存环形缓冲，30 行，2500 字符 |
| 摘要改成常驻滚动摘要 | 已修 |
| 固定暴露基础工具 | 已修；“后端不支持工具调用时在控制台提示”**未做** |
| 消息去重、限流、`.env` 忽略 | 已修（`fix/stage0-p0`，去重和限流都是内存版） |

---

## 2. 阶段 1：安全网（1–2 周）

**为什么先做：** 现有 328 个测试大多是单元测试加假 LLM，验证的是“编排”，不是“行为”。接下来要改提示词组装、会话存储和并发模型。没有行为级护栏，就很难判断有没有改坏。

1. **场景测试框架**（`tests/scenarios/`）
   - 用 YAML 或 JSON 描述一段对话：
     - 一组入站事件：发送者、群聊还是私聊、文本、@、时间偏移。
     - 脚本化的假 LLM：预设的回复和工具调用。
   - 驱动真实的 `WaifuService`：临时 `data_root`，出站用 `CapturingOutboundPort`。
   - 断言：
     - 哪些消息被回复了；
     - 出站内容和分条；
     - 调用了哪些工具；
     - 会话历史和写入的知识。
   - 首批 15–20 个场景：
     - 私聊多轮；
     - 群聊环境消息并入；
     - @ 和跟进窗口；
     - 设置称呼；
     - 记忆召回；
     - 滚动摘要触发；
     - `/skill` 和 `生图`；
     - 未授权用户调用高风险工具被拒；
     - 长回复分条。
2. **提示词快照**
   - 对固定场景，把最终发给模型的 messages 渲染出来，存成 `tests/snapshots/*.txt`。
   - 改动导致快照变化时测试失败，需要显式更新快照（例如设置 `UPDATE_SNAPSHOTS=1`）。阶段 2 改提示词结构时，这是最重要的护栏。
   - 前提是时间可注入。`infra/clock.py` 已经有了，要确认所有取当前时间的地方都经过它，否则快照不稳定。
3. **实测评测集**（`evals/`，手动运行，不进 CI）
   - 20–50 个真实场景，用真实模型跑。
   - 记录：
     - 跨轮记忆命中率；
     - 群聊上下文理解；
     - 工具触发正确率；
     - 人设一致性，例如回复开头的重复率；
     - 每轮的调用次数、延迟和 tokens。
   - 每次大改后跑一遍，结果存档对比。
4. **测试卫生**
   - **守卫：** 测试开始前和结束后检查仓库的 `data/` 有没有被改动，改了就让测试失败，防止再出现测试覆盖角色卡。
   - **导入：** 测试里的导入全部改用新的包路径。目前有 43 处还在用旧路径，例如 `waifu_standalone.cells.*` 和 `waifu_standalone.app`。这是阶段 5 删除兼容壳的前提。
   - **拆分：** `tests/test_app.py` 有 2816 行，按子系统拆成几个文件。

**验收：**
- 场景测试不少于 15 个；
- 提示词快照不少于 8 个；
- 评测集不少于 20 条，并有一次基线结果；
- 测试里旧路径导入为 0；
- `data/` 守卫生效。

---

## 3. 阶段 2：消息模型与回复管线（2–3 周）

**目标：** 把“一条消息进来后发生的事”变成一条显式的管线，每一步都能单独测试；同时让消息模型能表达 QQ 的真实形态。后面所有体验功能都建立在这上面，包括引用、表情、看图、群聊在场。

### 3.1 富消息模型

现状：
- 入站的 `MessageKind` 只有 `text`、`image`、`mention`（`models.py`）。
- 解析写在 `web/http_api.py` 里，只认 text、image、at。
- 出站的 `OutboundMessage` 只有 `text` 和 `images`。

目标：
- **入站段落：** text、mention、image、reply（被引用消息的 id）、face、mface、record、video、file、forward、json（卡片）、poke。未知类型保留原始 payload 并写日志，不再静默丢弃。
- **`InboundEvent` 新增字段：** `timestamp`、`reply_to`、`addressed`（是否点名机器人）、`raw`。`message_id` 已有。
- **出站：** 改成段落列表（text、at、reply、image、face、record），`reply_flow/emitter.py` 的分条在段落层面进行。
- **解析器位置：** 从 `web/` 移到 `gateways/onebot_parser.py`，HTTP 和反向 WS 共用一份。
- **兼容：** 保留 `plain_text`、`to_memory_text()` 等现有接口，旧调用点不必一次改完。

### 3.2 显式管线与 TurnContext

现状：`reply_flow/service.py::handle_event_async_locked` 有 208 行，从过滤、闸门、入库、称呼、召回、图谱、分析、生成、工具一路串到发送。所在文件对 `WaifuService` 私有成员的调用有 15 处。

目标：

```
parse → dedupe → gate.decide → TurnContextBuilder.build → Planner（可选）→ Generator → Renderer → Emitter
                                                           后台：KnowledgeWriter / Summarizer
```

- **`TurnContext`（冻结的数据类）。** 包含：事件、会话视图（窗口和滚动摘要）、说话人与成员信息、人设、召回的记忆、可用工具、当前时间、提示词预算。由一个 builder 一次性构建，后面的阶段只读它。
- **每个阶段是一个小类或函数。** 依赖从构造函数注入（见阶段 5），不再写 `svc._xxx`。
- **`handle_event_async_locked` 缩成编排。** 只剩 20–40 行，按顺序调用各个阶段。

### 3.3 提示词分段组装，按预算分配

现状：
- `character/cards.py::system_prompt` 拼出一大段字符串，`prompt_assembler.py` 再往后追加 `[Inner Thought]` 等内容。
- 最终是一条 system 消息（只有工具调用规则），加一条 user 消息（人设、记忆、对话、最新消息全在里面）。
- 每一段的长度靠各自的硬上限控制。

目标：
- **分段与预算。** 用 `PromptSection(name, content, priority, max_chars)` 列表加一个预算分配器：在总预算内按优先级分配；人设和最新消息不可裁剪，对话和记忆按优先级裁剪。
- **消息格式。** 人设和规则放进 system 消息。对话要不要改成原生的多轮 user/assistant 消息，用阶段 1 的评测集做 A/B 再定。多轮格式可能减轻模型模仿自己上一句开头的问题，但要实测。
- **模板集中。** 所有提示词模板（回复、分析、抽取、摘要、称呼、工具结果渲染）集中到 `reply_flow/prompts/`，方便审阅，也方便做快照。

**验收：**
- `handle_event_async_locked` 不超过 40 行；
- `reply_flow` 对 `WaifuService` 私有成员的调用为 0；
- 入站的未知段落类型会被保留并记日志；
- 快照里能看到每一段的内容和预算占用。

---

## 4. 阶段 3：状态与存储（2–3 周）

**目标：** 把状态从“整份快照”改成“事件加视图”，一次解决这几件事：
- 并发丢更新；
- 重启丢掉群聊环境消息；
- 历史没有时间戳；
- 群消息的写放大；
- 长期记忆的候选池被截断。

### 4.1 消息事件日志

现状：
- 会话是 `SessionMemory`：`history` 是字符串列表，`metadata` 是字典。
- `FileMemoryStore` 每保存一次，就把整份快照追加到 JSONL，超过 256KB 再压缩。
- 9 个调用点各自对整份会话读—改—写。
- 群聊环境消息放在内存环形缓冲里（`WaifuService._ambient_history`），重启就没了。
- 历史行没有时间戳。

目标：
- **消息表。** 每个角色已有一个 `runtime.sqlite3`，在里面新增只追加的表 `messages(id, launcher_type, launcher_id, sender_id, sender_name, role, ts, text, segments_json, reply_to, addressed, message_id)`。
- **视图代替列表。** 对话窗口、滚动摘要、最近说话人都改成查询，不再存进 `history` 列表。
- **易变状态单独存。** `metadata` 里的跟进窗口、`last_search`、`rolling_summary` 拆成小表或键值，按键更新，不再整份覆盖。
- **群聊环境消息直接入库**，标 `addressed=false`，取消内存缓冲。每条消息只是一次单行 INSERT，没有写放大。
- **迁移：**
  - 写一个一次性脚本，把每个 JSONL 文件的最后一份快照导入 `messages`。没有时间戳的行，用文件修改时间近似。
  - 原 JSONL 保留只读一段时间，`FileMemoryStore` 只留作导入用途。

### 4.2 记忆模型 v2

现状：
- 知识条目有作用域、类型、摘要、标签、置信度和嵌入。
- 召回只在最近 200 条里挑（`candidate_limit=max(limit*20, 200)`）。
- 没有访问计数、重要度、有效期，也没有“被哪条取代”的关系。
- 抽取时看不到已有记忆，只能新增或做近似去重。

目标：
- **新字段：** `importance`、`access_count`、`last_accessed_at`、`valid_until`（事件类）、`superseded_by`、`source_message_ids`（指向 `messages` 表）。
- **召回：**
  - 候选来自三处：
    - 全量语义 top-K（几千条以内暴力算余弦就够；数据量大了再考虑 sqlite-vec）；
    - 当前说话人的画像保底；
    - 近期的高重要度条目。
  - 打分：相关度 × 时间衰减 × 重要度。
  - 去掉“最近 200 条”的截断。
- **写入：** 抽取时附上同作用域的相关已有记忆，让模型返回 add、update、forget 操作。`update` 把旧条目标记为“已被取代”，不直接删除，方便追溯。
- **显式记忆工具：** 加 `remember` 和 `forget`，让“记住”“忘掉”有确定的动作。

### 4.3 存储收口

- **格式统一。** 现在运行数据分散在六种格式里：
  - 角色卡（YAML）；
  - 角色元数据（JSON）；
  - 当前角色（JSON）；
  - 技能状态（`skill_state.json`）；
  - 会话（JSONL）；
  - 运行时状态（SQLite）。

  目标：运行时数据全部进每个角色的 SQLite；角色卡保留 YAML 方便手改，但写入改成原子替换并先备份。
- **只留一套运行时状态实现。** 删掉 `InMemoryRuntimeStateStore`（735 行，功能和 SQLite 版重复），测试改用 SQLite 的 `:memory:` 或临时文件。

**验收：**
- 并发写入测试没有丢更新；
- 重启后群聊环境消息还在；
- `messages` 表能按时间查询；
- 召回不再受 200 条限制；
- 运行时状态只剩一套实现。

**风险：** 数据迁移。迁移前自动备份 `data/`，先在副本上演练；迁移脚本要幂等；旧格式保留只读几周。

---

## 5. 阶段 4：运行时与并发模型（约 2 周）

现状：服务端是 aiohttp，本身是异步的，但内核同时保留着同步和异步两套：
- 约 136 组孪生方法：`skills/dispatcher.py` 36 组、`members/onboarding.py` 16 组、`reply_flow/prompt_assembler.py` 14 组、`infra/llm_clients.py` 12 组……
- 70 处 `asyncio.to_thread`。
- 一个 4 线程的 `ThreadPoolExecutor`、一个自建的 `AsyncRuntime`，再加 7 把线程锁。
- 后台任务（知识抽取、归档、嵌入补全）各自提交，没有统一的排队、重试和观测。

目标：

1. **纯异步内核。** 每个能力只保留一个 `async def`。CLI 和测试通过 `asyncio.run` 或一层很薄的同步适配调用。SQLite 访问集中在存储层，用一个专用线程或 `aiosqlite`。
2. **统一后台任务队列 `TaskQueue`：**
   - 并发有上限，同一会话内保持顺序，失败可重试，带指标。
   - 承接知识抽取、滚动摘要、嵌入补全，以后还有主动消息、提醒和图片描述。
   - 控制台能看到队列深度和失败记录。
3. **会话级串行化收拢到一处。** 用一个按会话键管理的 `asyncio.Lock` 表，合并现在的 `_session_lock_for`、`commit_lock` 和归档用的 `_archive_inflight`。
4. **每轮模型调用的预算：**
   - “分析”调用可以按配置关掉，或者对很短的消息跳过。
   - 召回和分析并行执行。
   - 记录每轮的调用次数、延迟和 tokens，接入 `infra/observability.py`。

**验收：**
- 孪生方法少于 10 组；
- `asyncio.to_thread` 只出现在存储和阻塞 I/O 的边界上；
- 后台队列有指标；
- 控制台能看到每轮的调用次数和延迟。

---

## 6. 阶段 5：拆中枢与大文件（2–3 周）

### 6.1 WaifuService 只负责装配

现状：
- `WaifuService` 有 117 个方法、56 个字段。
- 至少 8 个模块通过 99 处私有调用反向依赖它。调用最多的几个：`reply_flow/service.py` 15 处、`settings_admin/service.py` 14 处、`reply_flow/reply_gate.py` 11 处、`reply_flow/emitter.py` 11 处。

目标：
- **端口。** 定义少量端口（`Protocol`）：`SessionStore`、`MemberDirectory`、`KnowledgeStore`、`ToolGateway`、`OutboundPort`（已有）、`LLMPort`、`Clock`、`TaskQueue`。
- **注入。** 子系统从构造函数拿到端口，不再持有对 `service` 的反向引用。
- **组合根。** `WaifuService` 只负责构建、连线和生命周期（启动、关闭），目标少于 300 行；`runtime/container.py` 仍是唯一的装配处。
- **迁移方式。** 一次迁一个子系统：
  1. 列出它对 `svc._x` 的所有调用；
  2. 逐个换成端口上的方法；
  3. 用“跨模块私有调用数”作为进度指标。

### 6.2 拆三个大文件

| 文件 | 有效行 | 拆分方向 |
|---|---|---|
| `reply_flow/prompt_assembler.py` | 2493 | `prompts/`（模板）；`generation/reply.py`（回复与工具循环）；`generation/analysis.py`；`generation/memory_extraction.py`；`generation/summary.py`；`postprocess.py`（清洗、截断、原始结果块） |
| `skills/dispatcher.py` | 2394 | 按工具族拆进 `tools/`：`image.py`、`search.py`、`fetch_summarize.py`、`files.py`、`exec.py`、`skill_list.py`；共用的调用构建和结果渲染放进 `tools/common.py` |
| `repositories/runtime_state.py` | 2237 | 按聚合拆：`members.py`、`persona.py`、`aliases.py`、`knowledge.py`、`telemetry.py`，公共的连接和事务助手放进 `db.py`；内存版在阶段 3 已删除 |

另外两处：
- **HTTP 合成一层。** `web/http_api.py` 和 `web/http_api_async.py` 合并成一个 aiohttp 路由表，直接调用异步处理函数，每个接口只声明一次。
- **称呼功能瘦身。** `members/onboarding.py`（1075 行）、`naming_intent_router` 和 4 个称呼工具加起来，相对这个功能的价值太重了。建议收敛成两部分：
  - 一个确定性解析器：“叫我 X”“以后叫我 X”这类明确句式直接保存；
  - 一个模型工具兜底。

  其余分支删掉。

### 6.3 删除死代码和兼容壳

- **构造了但从不调用：** `skills/intent_router.py`。`runtime/service.py` 第 211 行构造了 `IntentRouter`，全仓库没有任何地方调用它的 `.route()`。
- **没有调用方：** `dispatcher.dispatch_skill`、`adispatch_skill`，`MemberOnboarding.maybe_handle`、`amaybe_handle`。
- **兼容壳：** 包根目录 22 个、`cells/` 16 个纯转发模块。
  - 前提：阶段 1 已经把测试里的导入迁到新路径。
  - `src` 里还有几处 `from ..app import WaifuService`（只用于类型检查），改成从 `runtime` 导入。
- **文档：** 更新 CLAUDE.md 的模块表，它描述的还是旧结构。

**验收：**
- 单个文件的有效行不超过 800；
- `WaifuService` 少于 300 行；
- 跨模块私有调用为 0；
- 兼容壳为 0。

---

## 7. 阶段 6：配置与扩展边界（1–2 周）

### 7.1 配置单点声明

现状：
- 改一个配置要动好几处：
  - `AppConfig` 有 55 个顶层字段，`ConfigManager.load()` 逐个手写 55 个映射；
  - `_apply_env_overrides` 里另有 25 处手写的环境变量；
  - 控制台的设置服务和前端各有一份字段映射。

  新增一个配置要改 3–4 处。`load()` 是逐个字段手写的，漏加一行，配置文件里的值就会被静默忽略，没有任何报错。
- 线上配置文件会覆盖新的默认值，而且没有任何提示。`history_window_messages=8` 就是例子。

目标：
- **单点声明。** 每个字段只声明一次：类型、默认值、环境变量名、控制台分组、校验范围（用 dataclass 的 `field(metadata=...)`，或 pydantic）。加载、环境变量覆盖、控制台 schema、`dump-config` 都从这份声明生成。
- **按领域分组：** `memory`、`group_chat`、`persona`、`tools`、`outbound`、`llm`、`image`、`embedding`、`qq_sidecar`……旧的扁平键继续兼容读取一段时间，读到时给出迁移提示。
- **版本号与迁移。** 配置带版本号，启动时报告“配置文件覆盖了哪些新默认值”。

### 7.2 技能内核与外围扩展分离

现状：
- 技能相关代码约 6100 行。
- 其中市场（631）、bundle（240）、pack（180）、ClawRuntime 桥（389，另有 Node 侧的 `claw_runtime_server.mjs`）、`skills_admin`（427）都是围绕“兼容 OpenClaw 和技能市场”的外围能力。
- 核心（manifest、注册表、执行器、`tool_registry`、授权）和外围放在同一个包里，互相引用。

目标：
- **内核：** manifest 解析与校验、注册表、执行器、授权与风险策略、遥测。
- **扩展（可选安装，可以关闭）：** 技能市场、bundle 和 pack 的导入导出、ClawRuntime 桥。
- **权限策略数据化。** 谁（用户、群、角色）在哪里（群聊、私聊）可以用哪个风险等级的工具，写成配置，而不是代码分支。
- **统一注册入口。** 所有工具都经 `plugin_api` 注册，内置工具也一样，注册时声明风险等级和所需权限。

**验收：**
- 新增一个配置只改一处，并有测试；
- 技能内核不导入任何扩展模块；
- 权限策略可以在控制台查看。

---

## 8. 阶段 7：在新地基上做体验（持续）

具体功能见 `QQ_WAIFU_GAP_AUDIT.md` 第 6 节的 P1、P2。这里只标出每项依赖哪个阶段：

| 能力 | 依赖 | 要点 |
|---|---|---|
| 引用回复、@ 发送者、表情、分条拟人发送（带打字延迟） | 阶段 2 | 出站段落模型，由 Renderer 决定发成什么样 |
| 看图（把图片描述写进消息日志） | 阶段 2、4 | 后台任务生成描述，按图片哈希缓存 |
| 群聊在场：话题线程、谁在回谁、插话策略（取代 20 秒跟进窗口） | 阶段 2、3 | 消息日志加上 `reply_to` 和 `addressed` |
| 记忆生命周期、查询改写、`remember` / `forget` | 阶段 3 | 记忆模型 v2 |
| 关系状态（事件驱动，好感可升可降），承诺与纪念日 | 阶段 3 | 新增关系事件表 |
| 主动消息（早安晚安、隔天跟进、提醒） | 阶段 4 | 任务队列、定时触发、免打扰时段 |
| 人设深度（示例对话、世界书、口头禅，`extras` 进提示词） | 阶段 2 | 提示词分段 |
| 语音和表情包输出（可选） | 阶段 2、6 | 出站段落，加工具扩展 |

---

## 9. 横切关注：观测、成本、安全

- **每轮追踪。** 记录一轮从入站到出站的全过程：
  - 各阶段耗时；
  - 提示词各段的长度；
  - 模型调用的次数、tokens 和费用；
  - 工具调用和结果；
  - 出站分条。

  控制台按会话查看最近 N 轮。
- **成本。** 按用户和群统计模型调用和生图调用，配合阶段 0 加上的限流。
- **安全基线（每个阶段都要守住）：**
  - 高风险工具只对授权用户开放，执行层强校验；
  - 抓取类工具校验目标地址；
  - 密钥只从环境变量读取，不放进工具能读到的目录；
  - 新工具必须声明风险等级。

---

## 10. 不做什么

- 不重写 Web 控制台前端（约 1.25 万行 JS、CSS、HTML），只随配置 schema 生成做必要的调整。
- 不更换 NapCat / OneBot 的接入方式。
- 不追求兼容所有外部技能格式；外围扩展可以降级为可选。
- 阶段 1 完成之前，不改提示词结构。

---

## 11. 执行方式

- 每个阶段开一个跟踪 issue，列出要拆的小 PR。
- 每个 PR：
  - 测试全绿；
  - 描述里附上度量变化；
  - 改了提示词，附快照 diff；
  - 改了存储，附迁移和回滚说明。
- 每个阶段结束：跑一次评测集，结果存进 `evals/results/`，并更新附录 A。
- 建议在阶段 1 加一个 `scripts/arch_metrics.py`，自动输出附录 A 的数字，不再手工统计。

---

## 附录 A：度量基线（2026-10-26）

| 指标 | 现值 | 目标 |
|---|---|---|
| Python 文件数 / 有效代码行 | 131 / 27,739 | — |
| 测试数 | 328（全过） | 再加场景测试和快照 |
| `WaifuService` 方法 / 字段 | 117（私有 55）/ 56 | 只做组合根，少于 300 行 |
| 跨模块调用 `WaifuService` 私有成员 | 99 处，38 个成员 | 0 |
| 同步/异步孪生方法 | 约 136 组 | 少于 10 |
| `asyncio.to_thread` 调用点 | 70 | 只在存储和阻塞 I/O 边界 |
| 有效行超过 2000 的文件 | 3 | 0（每个文件不超过 800） |
| 整份会话读—改—写的保存点 | 9 处，5 个文件 | 0（改为事件日志） |
| 运行时状态实现 | 2 套（内存版 735 行，SQLite 版 1178 行） | 1 套 |
| HTTP 层 | 2 层（1459 行 + 1027 行） | 1 层 |
| 配置手写映射 | `load()` 55 个，环境变量 25 处 | 单点声明 |
| 纯转发兼容模块 | 38 个（根目录 22，`cells/` 16） | 0 |
| 测试里的旧路径导入 | 43 处 | 0 |
| 入站段落类型 | 3 种 | 覆盖全部 OneBot 段落，未知类型保留 |
| 入站去重 / 限流 | 无 / 无 | 有 / 有 |

## 附录 B：度量口径

- **有效代码行：** 非空、不是纯注释的行。
- **跨模块私有调用：** 在 `runtime/service.py` 之外，匹配 `svc._x`、`self._service._x`、`self.service._x`、`service._x` 这几种写法。
- **孪生方法：** 同一个类里同时存在 `xxx` 和 `axxx`，或者 `_xxx` 和 `_axxx`。
- **纯转发兼容模块：** 文件里除了导入和 `__all__`，没有别的代码。
- 其余数字都是用 grep 和 `ast` 统计的。阶段 1 建议把这些统计固化成脚本。
