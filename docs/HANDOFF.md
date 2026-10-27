# 交接说明（2026-10-25，2026-10-27 更新）

给新会话：这份文档只记录工程事实。先读第 1、2、3 节。第 4 节的 A、B、C 三块（PR #27）、阶段 0 的 P0 修复（第 2.1 节，PR #28）和审计修复加新版私聊链路（PR #29）都已合入 main；2026-10-27 起服务在本机 Docker 里真实收发，部署和实测情况见第 3 节。剩下的事在各块的「没做的」、第 2.1 节的「要 owner 决定的」和第 5 节。标注「（审计结论）」的条目来自代码审查，没有逐条复核，改之前先对一下代码。

## 1. 先读这些

- 跑测试：`PYTHONPATH=src .venv/bin/python -B -m unittest discover -s tests`，723 个，约 70 秒，目前全过。系统 `python3` 没装 `httpx`，必须用项目的 `.venv/bin/python`。
- CLAUDE.md 里的模块表已经过时，代码现在按子包组织：`runtime/`（服务编排）、`reply_flow/`（回复链路、提示词）、`memory/`（会话与知识）、`repositories/`（SQLite 状态）、`skills/`（技能与工具）、`members/`、`character/`、`settings_admin/`、`web/`、`infra/`、`gateways/`、`systems/`、`organs/`。
- 一次回复发给模型的是真正的多轮对话（2026-10-27 起，`feat/prompt-structure`）：
  - system：`character/cards.py::CharacterCard.system_message` 拼出人设、擅长、背景、规则、你记得的事、关于对方、当前情况、更早的聊天摘要（中文标题）；这条消息需要工具时，末尾再加 `[工具]`（`_TOOL_CALLING_SYSTEM_PROMPT`）。
  - 之后是 `Memory.dialogue_turns` 给出的历史，机器人的发言是 assistant，别人的是 user；群聊里 user 消息前带发言人名字。最后一条 user 是当前消息（图片也在这里）。
  - 只支持单条文本提示词的后端（Dify）仍走 `CharacterCard.system_prompt` 拼成的一大段文字。
- 机器人永远是卡片上的角色名；某个用户给它起的别名只作为一句备注（「X 管你叫「大母牛」…」），不会再替换它的名字。
- 工具按需出现（`tools_on_demand`，默认开，`skills/tool_intent.py`）：消息说到天气、搜索、链接、称呼、画图、文件时才带对应工具，或者某个技能的关键词命中；普通闲聊不带工具也不带工具规则。机器人刚问过称呼时，下一条回复带称呼工具。`/skill` 不受影响。
- 防复读：直接回复如果和机器人最近 6 条之一几乎一样、共用 10 字以上的句子、或和前 3 条用同一个开头，就带着「别重复」提示重写一次；前 3 条都以「……」开头而重写后还是，就去掉开头的「……」。
- 每条被回复的消息通常 2 次模型调用：回复、后台知识抽取；需要工具、称呼判断或重写时多 1–2 次。`thinking_mode`（回复前的「内心分析」调用）默认关。每次召回还有 1 次嵌入调用。
- 像真人地发（2026-10-27，`feat/human-touch`，第 3.6 节有实测）：
  - 分段：模型写的聊天回复（`OutboundMessage.conversational`）不超过 `segmented_reply.max_total_chars`（150 字）时，按句末标点和换行拆成至多 `max_parts`（3）条，不到 4 个字的碎句并到相邻一句；条与条之间随机停 `min_delay_seconds`–`max_delay_seconds`（1–2.5 秒），dry_run 不停。列表、代码块、长回复整段发；命令结果、通知、没有模型时的兜底回复不拆。会话历史里仍是完整的一条。
  - 引用：群里回复时，如果那条消息之后群里又有人说过话，第一条带 OneBot `reply` 段引用它；紧接着回的不引用。`OutboundEmitter.note_inbound` 在拿会话锁之前记下每个群最新的消息 id，引用与否在真正发送时判断（群回复延迟之后）。`group_reply_quote=false` 关掉。
  - 表情包：`data/stickers/<分类>/` 里的 png、jpg、gif、webp（单张不超过 2 MB，`reply_flow/stickers.py`）。有可发的分类时，system 里加 `[表情包]` 段列出分类；模型在回复末尾写「[表情:分类]」就从该分类随机挑一张（名字对不上时取互相包含的分类，「很开心」→「开心」），`sub_type=1`（QQ 按表情显示）单独发在文字后面。同一个聊天两张之间至少隔 `stickers.min_replies_between`（3）条回复，冷却期间不出现 `[表情包]` 段。发出的标签留在历史里，没发出的标签删掉；任何消息里的标签都不会当文字发出去。冷却计数在内存里，重启清零。线上的表情包库见第 3.5 节。
  - 看图：收到的图片由 `infra/image_inputs.py` 自己下载（走 `safe_fetch`，最多 10 MB），GIF 和动图取第一帧，长边超过 1536 像素就缩小，以 data URL 发给模型，每条消息最多 4 张。下载或解码失败、后端不支持图片、模型拒收时，当前消息末尾加「（对方发来的图片没能加载出来，你看不到它的内容）」让角色自己回应（模型拒收时去掉图片再请求一次），不再回「图片处理失败…」「当前模型后端不支持图片理解…」这类系统话。QQ 表情包（图片段 `sub_type=1`）在聊天记录里记作「发了一个表情」，模型照样看得到图。

## 2. 当前工作区状态

- 2026-10-27 已合入 main：PR #27（下面七组改动）、PR #28（第 2.1 节）、PR #29（`codex/runtime-v2-private-flow` 拆成的三个提交：审计修复、新版私聊链路、文档，见 `docs/AUDIT_FIXES_2026-10-26.md`、`docs/RUNTIME_V2_FIRST_SLICE.md`、`docs/ARCHITECTURE_RESET_PLAN.md`）。
- `fix/deploy-hardening`：群回复限速和忽略名单、空环境变量不覆盖配置、固定 NapCat 版本、本文档，见第 3 节。
- `.env`（NapCat 令牌、账号）和 `output/`（截图和 PR 草稿）已在 `.gitignore` 里。

改动分七组（已按这个顺序提交，下面留作每组改了什么的记录）：

1. **称呼工具更严格**（本轮之前就有的未提交改动）
   - `reply_flow/prompt_assembler.py`：`_TOOL_CALLING_SYSTEM_PROMPT` 抽成常量，新增「保存类请求必须先调工具」。
   - `runtime/container.py`：四个称呼工具的 description 重写。
   - `tests/test_skills.py`：`test_naming_tools_expose_strict_persistence_instructions`。
2. **高风险工具授权闸门**
   - `config.py`：`ToolPolicyConfig.exec_enabled`、`write_enabled` 默认改为 `False`；新增 `authorized_user_ids: list[str]`，默认空，空表示任何人都不可用。
   - `settings_admin/config_manager.py`：`_load_tool_policy` 读取 `authorized_user_ids`；环境变量 `OPENQQWAIFU_TOOL_POLICY_AUTHORIZED_USER_IDS`。
   - `skills/tool_orchestrator.py`：`_allowed_tool_ids` 对未授权发送者去掉 `risk_level` 为 `filesystem`、`command`、`network` 的工具；`_is_tool_authorized` 按 `event.sender_id` 判断。
   - `skills/tool_registry.py`：新增风险等级 `network`；`ToolInvocation.allowed_tool_ids`；`ToolRegistry._execution_blocked` 在 `execute_model`、`aexecute_model` 里强校验，模型即使直接给出工具名，不在允许集合里也会被拒。
   - `skills/registry.py`：技能清单的 `risk_level` 接受 `network`。
   - `runtime/container.py`：`web-fetch` 的风险等级由 `safe` 改为 `network`。
   - `reply_flow/prompt_assembler.py`：两个工具循环创建 `ToolInvocation` 时带上 `tool_context.allowed_tool_ids`。
   - `tests/test_skills.py`：新增 `test_high_risk_tools_require_authorized_user`；另有 4 个依赖旧默认值的测试改成显式开启。
3. **长期记忆检索**
   - `repositories/runtime_state.py`：
     - `_EmbeddingBreaker`：嵌入失败写日志；连续 3 次失败后暂停 300 秒；`set_embedder` 会清除暂停。
     - SQLite 存储写知识时，嵌入在拿写锁之前计算（`_prepare_knowledge_embedding`）。
     - `_extract_terms` 支持中文：汉字连续段切成相邻二字词，并过滤 `_STOP_BIGRAMS`。
     - `_score_knowledge` 改为 0 到 4 的相关度，没有词重合返回 0。
     - 语义相似度低于 `_SEMANTIC_RELEVANCE_MIN = 0.40` 不计分；两个存储的召回都跳过分数不大于 0 的条目，置信度不再单独让条目入选。
   - `runtime/service.py`：`WaifuService._extract_terms` 直接复用上面的分词。
   - `memory/knowledge_service.py`：
     - `_session_knowledge_entries` 需要有词重合才返回。
     - `_is_near_duplicate`：一条是另一条中间多出几个字（长度比不低于 0.7、至少 4 个字、多出的字里没有否定字）就视为同一事实；`_existing_knowledge_entry` 先找完全相同的，再找近似重复；合并时保留更长的表述。
     - `scope_hint == "global"` 不再写成全局条目，落到所在聊天的作用域。
   - `reply_flow/service.py`：`_restates_memory` 去掉图谱高亮里已经出现在 `[Memories]` 的「相关记忆：…」。
   - `tests/test_state_store.py`：新增熔断、中文召回、无关不召回、写入不占写锁等测试；`FakeEmbedder` 的映射键现在按空白归一化，旧的键因含换行从未命中过。
   - `tests/test_memory.py`：新增 `KnowledgeWritebackTests`。
4. **测试隔离**
   - `tests/test_app.py`：两个会保存角色卡的测试（`test_active_character_card_identity_wins_for_person_session`、`test_save_character_panel_can_edit_without_switching_active_character`）改用临时目录；前者的 `set_active_character` 换成 `service.activate_character`。
   - 规则：凡是会保存角色卡、切换角色、写会话的测试，必须用 `tempfile.TemporaryDirectory()` 加 `AppConfig(data_root=...)`。`build_default_service(AppConfig())` 不带 `data_root` 时，角色卡读写落在当前目录的 `data/cards/`。
5. **短期窗口（第 4 节 A 块）**
   - `config.py`、`settings_admin/config_manager.py`、`settings_admin/service.py`、`web/console_panels.py`、`web/assets/js/i18n.js`、`web/assets/js/pages/abilities.js`：`history_window_messages` 默认 8 改 30；新增 `history_window_chars`（默认 2500，0 表示不限），控制台可改。
   - `models.py`：`to_memory_text(bot_account_id=...)` 去掉机器人自己的 @，别人的 @ 保留。
   - `runtime/service.py`：`_ambient_history` 内存环形缓冲（`_remember_ambient_event`、`_peek_ambient_history`、`_clear_ambient_history`）；`_preview_conversation_view` 复用同一套窗口裁剪。
   - `memory/session_service.py`：`save_user_event` 接收 `context_lines`、`bot_account_id`；`format_dialogue` 新增 `max_chars`、`exclude_last`；单行上限 `_DIALOGUE_LINE_MAX_CHARS = 400`。
   - `reply_flow/service.py`：缓冲并入历史、窗口去掉当前消息、群聊机器人标签固定为角色卡名字。
   - `reply_flow/prompt_assembler.py`：知识抽取只看最近 `_KNOWLEDGE_CONTEXT_LINES = 8` 行。
   - 测试：`tests/test_app.py` 5 个、`tests/test_organs.py` 2 个。
6. **当前时间、滚动摘要、提示词质量（第 4 节 B 块）**
   - `infra/clock.py`（新文件）：`resolve_timezone`、`local_now`、`format_prompt_time`、`absolutize_relative_dates`。
   - `config.py`、`settings_admin/config_manager.py`：新增 `timezone`（默认 `Asia/Shanghai`，环境变量 `OPENQQWAIFU_TIMEZONE`）；`max_thinking_words` 默认 30 改 80（`abilities.js` 同步）。
   - `character/cards.py`：`system_prompt` 新增 `current_time`、`earlier_summary`。
   - `memory/session_service.py`、`memory/__init__.py`：`ROLLING_SUMMARY_KEY`、`clip_rolling_summary`；`maybe_archive_history` 改为整批归档并写入 `metadata["rolling_summary"]`，新增 `commit_lock`；删除不再使用的 `amaybe_archive_history`。
   - `runtime/service.py`、`reply_flow/emitter.py`：`_archive_if_needed[_async]` 换成 `_schedule_archive` + `_archive_history`（后台）；新增 `_session_lock_for_key`、`_archive_inflight`。
   - `reply_flow/prompt_assembler.py`：`_prompt_time`、`summarize_rolling`、`_build_rolling_summary_query`、`_clip_at_sentence`；对话、分析、称呼引导、工具渲染提示词带当前时间；抽取提示词带今天日期。
   - `repositories/runtime_state.py`：`excluded_from_recall`；`memory/knowledge_service.py`：存知识前改写相对日期，线上关掉正则回退；抽取结果里的 `memory_type="summary"` 按 `fact` 存（`_normalize_knowledge_item`）。
   - `reply_flow/service.py`：交给 emitter 的名字在群聊里用卡名。
   - 测试：新文件 `tests/test_clock.py`；`tests/test_app.py`（`test_summarization_mode_archives_older_history` 改写为 `test_summarization_mode_folds_overflow_into_rolling_summary`，另加 3 个）、`tests/test_organs.py` 2 个、`tests/test_memory.py` 2 个、`tests/test_state_store.py` 2 个。

7. **技能触发与回复渲染（第 4 节 C 块）**
   - `config.py`、`settings_admin/config_manager.py`：新增 `explicit_skill_prefixes`（默认 `["/skill"]`）、`outbound_message_max_chars`（默认 900，0 不切分）。
   - `reply_flow/service.py`：`/skill` 命令越过 `ignore_prefixes`；`生图` 前缀直接走生图技能；`background_image_delivery` 用形参。
   - `skills/dispatcher.py`：`resolve_explicit_skill_request` 按配置前缀匹配；新增 `resolve_image_command`、`_fill_single_required_argument`；`_build_tool_invocation` 带上授权闸门的 `allowed_tool_ids`；`extract_image_prompt` 对英文前缀要求词边界；`summarize` 本地文件只对授权用户开放；`_compose_tool_display_text` 去重。
   - `skills/registry.py`、`skills/tool_orchestrator.py`：`exposed_manifests` 固定暴露搜索、天气、总结、生图。
   - `reply_flow/prompt_assembler.py`：工具循环按暴露的技能查清单（`_merge_skills`）；`_clean_response` 保留换行；工具渲染提示词用 `_clip_multiline`；`_append_raw_tool_blocks` 去重。
   - `reply_flow/emitter.py`：`split_reply_text`、`message_parts`，长回复分多条发。
   - `runtime/service.py`：有结果的 `last_search` 跨轮保留。
   - `infra/safe_fetch.py`（新文件）：`check_public_url`、`fetch_public_url`、`afetch_public_url`；`skills/dispatcher.py` 的 web-fetch、summarize 内置抓取改用它，summarize CLI 调用前先 `check_public_url`。测试 `tests/test_safe_fetch.py`。
   - 测试：`tests/test_skills.py` 把 `test_slash_skill_command_no_longer_bypasses_tool_orchestrator`、`test_ignored_slash_summarize_does_not_use_legacy_dispatch` 换成 `test_slash_skill_command_runs_with_default_ignore_prefixes`、`test_other_slash_messages_are_still_ignored`、`test_slash_skill_command_respects_tool_authorization`，`test_image_request_keeps_skill_match` 改断言，另加 3 个；`tests/test_app.py` 加 5 个。

### 2.1 阶段 0 的 P0 修复（PR #28，2026-10-26，已合入）

`REFACTOR_ROADMAP.md` 第 1 节第 4 步的剩余项。URL 抓取拦截在 PR #27 里已经做完，这里没再动。每项一个提交，都有测试；全量 362 个测试通过。2026-10-27 实机验证过 `dry_run` 和私聊、群聊的基本收发（第 3 节）。

1. **`dry_run` 生效**（`c28c520`）
   - `runtime/container.py::_build_runtime_outbound`：`qq_sidecar.dry_run` 为真时返回 `CapturingOutboundPort`，配了 `outbound_base_url` 也不发；`reverse_ws` 模式保留网关，NapCat 照常连进来推事件，回复只捕获。控制台改 sidecar 配置后重建出站时同样生效。
   - `settings_admin/config_manager.py`：环境变量 `OPENQQWAIFU_QQ_SIDECAR_DRY_RUN` 以前读了但结果被丢掉，现在会写回配置。
   - `runtime/service.py`：`_requires_live_llm()` 在 `dry_run` 时为真，彩排走线上路径（模型没配好就回「AI 工具编排模型还没连上」，知识抽取不走正则回退、生图前缀要求 `image_ready`）；`_outbound_mode_label` 显示 `dry-run (capture)`。`serve` 的启动日志是 `outbound=capture(dry-run)`（已实测）。
   - CLAUDE.md、AGENTS.md 和第 6 节的说法已改。
   - 测试：`test_runtime_service_dry_run_captures_even_with_sidecar_url`、`test_runtime_service_dry_run_keeps_reverse_ws_gateway_for_inbound`（`tests/test_app.py`）、`test_env_override_turns_on_dry_run`（`tests/test_config_manager.py`）。
2. **启用角色不再清空会话**（`077a26a`）
   - `character/service.py`：`activate_character` 去掉 `reset_sessions` 参数，只切换当前角色；启用的就是已绑定的角色时不重建会话存储和 SQLite 连接。`runtime/service.py` 的 `_rebind_character_scoped_storage`、`_reset_character_runtime_context` 也不再带清空开关（仍会清跟进窗口、环境缓冲这些内存状态）。
   - 清空会话改成单独的操作：`CharacterService.reset_character_sessions` / `POST /api/character/reset-sessions`，`{"character": X, "confirm": X}`，`confirm` 不等于角色代号、或角色不在 `cards.list_characters()` 里都返回 400（后者也挡住 `..` 这类代号去删别的目录）。只删这个角色的会话文件（含滚动摘要），知识库、成员、好感度不动。仅管理员可用。
   - 控制台：人物卡页立绘卡片的按钮行多了「重置会话…」，弹窗说明删什么，要求输入角色代号；输错不发请求。已在浏览器里实测：输错被拒；「保存并启用」同一角色后会话文件还在；输对后会话文件被删、接口 200。
   - 没做：重置时没有拿会话锁，正在生成的回复写回时会把那一个会话再存回来。
   - 测试：`test_save_character_panel_switch_keeps_character_sessions`（原来的 `..._switch_resets_character_sessions` 反过来了）、`test_save_and_activate_same_character_keeps_sessions_and_storage`、`test_reset_character_sessions_requires_matching_confirmation`（`tests/test_app.py`）、`test_character_reset_sessions_endpoint_requires_confirmation`（`tests/test_server_integration.py`）。
3. **入站消息去重**（`f76041e`）
   - 新文件 `gateways/inbound_dedup.py::InboundDeduplicator`；`web/http_api.py::HttpApi` 持有一个实例，`handle_json` 和 `handle_json_async` 在解析出事件后先 `claim`。HTTP 回调和反向 WS（`_handle_reverse_ws_payload` → `handle_json_async`）都经过这里。
   - 有 `message_id`：按（会话类型、会话 ID、`message_id`）记 10 分钟。没有：按（会话、发送者、内容哈希）记 10 秒；载荷里有 OneBot 的 `time` 时把它并进键并记 10 分钟（重试带原来的 `time`，同一句话再发一次 `time` 不同）。最多 4096 条，超出丢最旧的。参数是模块常量，没进配置。
   - 重复的返回 `ignored` / `duplicate message`（HTTP 回调照常回 204），onebot 事件指标里的 outcome 是 `duplicate`。处理抛异常时释放这条记录，之后的重试还能处理。
   - 没做：只在内存里，重启就忘；直接调用 `service.handle_event` 的路径（CLI、测试、控制台预览）不去重。
   - 测试：新文件 `tests/test_inbound_dedup.py`；`test_replayed_event_frame_is_handled_once`（`tests/test_onebot_ws.py`，真实 WS 连接）、`test_http_callback_retry_does_not_reply_twice`（`tests/test_server_integration.py`，真实 HTTP，出站只发一次）。这两个端到端测试在去掉去重后都会失败（已验证）。
4. **付费和外部工具限流**（`964d18a`）
   - 新文件 `infra/rate_limit.py::TokenBucketLimiter`：一次调用要从发送者的桶和（群聊时）群的桶各拿一个令牌，两个都有才扣，否则都不扣并返回要等多久。桶在内存里，初始是满的，最多 1 万个。
   - `config.py::RateLimitConfig`（`AppConfig.rate_limits`），`ConfigManager.load()` 的 `_load_rate_limits` 读取；负数按 0，非数字用默认值。环境变量 `OPENQQWAIFU_RATE_LIMITS_ENABLED`、`OPENQQWAIFU_RATE_LIMITS_EXEMPT_USER_IDS`。默认值：
     - `image`（生图）：每人 3 次连发、每小时回填 10 次；每群 6 次、每小时 30 次。
     - `network`（搜索、web-fetch、summarize、带新查询的 search-links，共用一个桶）：每人 8 次、每小时 60 次；每群 16 次、每小时 180 次。
     - `*_burst` 为 0 关掉那一层；`exempt_user_ids` 里的人不受限；`enabled=false` 全关。
   - `skills/dispatcher.py`：检查放在各个真正执行的函数里（`use_*`/`ause_*` 模型工具、`handle_image_request`/`handle_search_request` 及异步版、summarize 的离线路径），所以模型工具循环、`/skill`、`生图` 前缀、旧关键词分发都覆盖；放在参数校验之后，空请求不扣令牌。被限时回复「X，生图刚才用得有点多，30 分钟后再来吧。」；`/skill` 和 `生图` 直发时这句话原样发出，不再调一次模型来改写；模型工具循环里模型看到的是带 `rate_limited` 的错误结果。
   - 不限：会话总结（`summary`）、天气、生图交付文案、普通聊天的模型调用；控制台的立绘生成。控制台没有改这些参数的界面，只能改配置文件或环境变量。重启后桶回到满的。
   - 测试：新文件 `tests/test_rate_limit.py`（令牌桶、按人、按群、三种网络工具共用一个桶、豁免和关闭、配置读取与环境变量）。
5. **仓库卫生**（`ce48bd2`）
   - `.gitignore` 加上 `.env`、`.env.*`（保留 `!.env.example`，它是被跟踪的）和 `output/`。测试 `tests/test_repo_hygiene.py` 用 `git check-ignore --no-index` 检查，没有 git 或不在工作树里时跳过。
   - 没做：`data/skills/` 下两个旧格式技能文件（路线图里也列在这一步）按约定留给 owner。

要 owner 决定的：

- 限流默认值合不合适，要不要把自己的 QQ 号放进 `rate_limits.exempt_user_ids`。线上的 `data/docker-compose-config.json` 没有 `rate_limits` 段，合入后直接按默认值生效。
- `dry_run` 时 `_requires_live_llm()` 为真（按线上行为彩排）。如果更想要「dry_run = 离线模式」，改回去只需要删掉 `runtime/service.py::_requires_live_llm` 里的 `dry_run` 判断，并改掉 `test_runtime_service_dry_run_captures_even_with_sidecar_url` 的断言。
- 去重的时间窗（10 分钟 / 10 秒）是常量；要调的话再挪进配置。
- 顺带发现、没有修：`settings_admin/config_manager.py::ConfigManager.load()` 里 `assistant_name` 的默认值 `"鐞夌拑"`、`image_command_prefix` 和 `image_command_aliases` 的默认值 `"鐢熷浘"` 是乱码（UTF-8 被当成 GBK 读过一次，原文是「琉璃」「生图」）；`config.py` 里的默认值是对的。配置文件里没写这几项时，生图前缀会变成乱码。线上的 `data/docker-compose-config.json` 三项都写了，目前不受影响。

## 3. 部署、运行数据与配置（`data/`、`napcat/` 被 gitignore，不在 git 里）

### 3.1 部署现状（2026-10-27）

- 本机 Docker Compose（`compose.napcat.yml`）：容器 `openqqwaifu`（8080）和 `napcat`（3000/3001/6099），真实发送，`qq_sidecar.dry_run=false`。2026-10-27 起聊天模型是 `grok-4.7`（xAI，每百万 token 输入 2 美元、输出 6 美元，会先思考再回答，dry_run 实测平均 15 秒、最长 34 秒），`thinking_mode=false`。机器人 QQ `3956638110`（昵称「幸福的奴隶Bot」），好友 2 个，群 1 个（`612475113`）。
- `data/docker-compose-config.json` 当天的改动：`qq_sidecar.gateway_mode=reverse_ws`；`qq_sidecar.access_token` 设成随机令牌，反向 WS 和 NapCat 的 HTTP 动作端口 3000 都用它（3000 映射在宿主机上，原来没有令牌）。HTTP 回调已关闭，所以不需要 `inbound_access_token`。改之前的配置、NapCat 配置和 QQ 数据备份在 `data/backups/before-deploy-20261027-023259/`。
- 只重建 openqqwaifu：`docker compose -f compose.napcat.yml up -d --build waifu`，不会动 NapCat。改了 `napcat` 服务的定义（例如镜像标签）再 `up`，NapCat 容器会被重建，很可能要重新扫码。
- compose 给可选设置传 `${VAR:-}`，没设置时是空字符串。以前 `_env_str` 会用这个空串覆盖配置文件里的令牌；现在空串按未设置处理（`fix/deploy-hardening`）。

### 3.2 NapCat 登录

- 镜像固定为 `mlikiowa/napcat-docker:v4.18.41`（NapCat 4.18.41，QQ 3.2.34-53644）。升级要有意为之，升级后准备好扫码。
- 2026-04 的旧镜像（QQ 3.2.25）加旧 QQ 数据目录：能登录，状态 online/good，好友、资料查询都正常，但收不到任何消息，最近会话为空，查聊天记录超时。换新镜像后仍然一样；把数据目录换成空的、重新扫码后恢复。旧数据目录在 `napcat/qq.old-20261027-031156`，稳定后可删。
- 重启 NapCat 后快速登录可能失败（「登录态已失效」），这时要重新扫码。二维码取法：`docker cp napcat:/app/napcat/cache/qrcode.png .`；或用 NapCat 日志里的「二维码解码URL」；或控制台的 QQ 登录页。二维码约 2 分钟过期，会自动刷新。要让重启后自动登录，只能在 `.env` 里配 `NAPCAT_QUICK_PASSWORD`（机器人账号的密码，由 owner 自己填）。
- 反向 WS 断开期间（NapCat 重启、登录过程中）到达的消息不会补推，直接丢失。

### 3.3 群聊防刷屏（`fix/deploy-hardening`）

- 2026-10-27 15:29，群里另一个机器人「大肥鱼」（`3246300953`）和本机器人互相接话：owner @ 一次之后，跟进窗口让机器人接所有人的话，每次回复又把窗口延长，70 秒内发了 6 条，其中两条和前一条一字不差。当时靠会话外的脚本停掉了容器。
- 现在有两道闸门，都在 `reply_flow/reply_gate.py`：
  - `ignore_user_ids`（顶层配置，环境变量 `OPENQQWAIFU_IGNORE_USER_IDS`，逗号分隔）：这些 QQ 号的消息永远不触发回复，私聊也一样，也不能用 `/skill`；消息仍作为群聊上下文进环境缓冲。
  - `rate_limits.group_reply_burst` / `group_reply_per_hour`（默认 8 和 60）：每个群一个令牌桶，每次回复扣一个，用完就不回，只记一条 WARNING 日志，不在群里说明。`rate_limits.exempt_user_ids` 里的人不受限，`rate_limits.enabled=false` 全关。桶在内存里，重启后是满的。
- 控制台「其他设置」接口（`/api/panels/other`）能读写 `ignore_user_ids`，页面上还没有输入框；限速参数只能改配置文件。

### 3.4 实机测试的发现（2026-10-27，模型 grok-4-fast-non-reasoning）

- 私聊和群 @ 都正常，一条回复 6–9 秒，约 3 次模型调用加 1 次嵌入。群里不 @ 的消息进环境缓冲，下次回复时用上了（回复里引用了前面群聊的话题）。
- 回复会照抄自己上一条的后半句，群里出现过两次连续相同的回复。原因是人设只在 user 消息里、聊天记录是一段文字、别名顶替了角色名，`feat/prompt-structure` 已改；改前改后和换 grok-4.7 的 dry_run 对比见该 PR。
- 用户说「给我讲个故事」时，回复「我就勉强讲一段」然后没有下文：角色卡 Rules 段还是测试夹具的「不超过三句话」。
- 只发 `/skill` 不会有任何回应（`/` 是忽略前缀，只有 `/skill <技能 ID> 参数` 才会执行）。问「你会什么」会触发技能列表。
- 群临时会话的消息（陌生人 `2747277822`）没有被处理，原因没查。
- 群里大肥鱼发的图，机器人有一次看懂了，有一次回了「图片处理失败，暂时无法读取这张图片，请稍后重试。」，原因见第 3.6 节。

### 3.5 运行数据

- `embedding.model` 由 `qwen3-vl-embedding` 改为 `text-embedding-v4`：前者在 DashScope 的 OpenAI 兼容 `/embeddings` 上不可用，每次都返回 404；后者已实测，1024 维。更换嵌入模型要重新校准 `_SEMANTIC_RELEVANCE_MIN`，`text-embedding-v4` 上相关句对约 0.42–0.64，无关句对约 0.19–0.37。
- `tool_policy.exec_enabled=false`、`write_enabled=false`、`authorized_user_ids=[]`；`history_window_messages=30`、`max_thinking_words=80`（2026-10-26 改）；没有 `history_window_chars`（缺省 2500）和 `timezone`（缺省 `Asia/Shanghai`）。
- `member_persona_state.profile_summary` 里原来的 4 条画像写的全是机器人自己的傲娇人设（「masks care with complaints」之类），2026-10-27 备份后清空；抽取提示词已不再带人设。
- 2026-10-25 清空过会话记录和知识条目，清空前的备份在系统临时目录，可能已被清理：`/private/tmp/claude-501/-Users-shinonome-openqqwaifu/bd3c61d7-dd79-4eaf-bd72-9fa0d7f1d365/scratchpad/data-backup-20261025-160341`。2026-10-27 的实测写入了新的会话（`person_783190298`、`group_612475113`）和知识。
- 角色卡 `data/cards/aurora_person.yaml`、`aurora_group.yaml`：Profile 段 2026-10-26 已换回 `aurora.meta.json` 里 `portrait_prompt` 的 8 行人设；Skills、Background、Rules、Prologue 仍是测试夹具内容（例如 Rules「不超过三句话」），要由 owner 决定怎么写。
- 表情包库 `data/stickers/`（2026-10-27 导入，约 1.1 GB）：AstrBot Meme Manager「表情包广场」索引（`anka-afk/astrbot-meme-pack-index`）的 14 个条目，其中 `ddzs987-semantic-001` 和官方表情包1号是同一批图，实际 13 个包，共 2,359 张不重复的图。各包原来的 50 种英文分类合并成 42 个中文分类（开心、生气、害羞、贴贴、宕机、要钱、早安…），对照表、每个包的来源、维护者、许可和收录张数在 `data/stickers/README.md`。文件名以包 ID 开头，去掉某个包就删这个前缀的文件。超过 2 MB 的 424 张已缩小（动图 240 像素、静图 512 像素）后收录。许可大多是「仅限非商业使用」或 CC BY-NC-SA 4.0，几个包写的是「原素材许可未指定」，owner 决定全部用于本机器人的非商业聊天。下载用的是各包仓库按索引 `ref` 的浅克隆（只取 `memes/`），整理脚本没有进仓库。线上容器扫描一次目录约 35 毫秒（第一次 191 毫秒）。
- 线上 `summarization_mode` 是 `true`、`memory_summary_batch_size` 是 12：现在每多出 12 行才调一次摘要。`knowledge_entries` 里如果还有旧的 `memory_type="summary"` 条目，召回会跳过，不会自动删除。

### 3.6 看图的实测（2026-10-27）

- 直接调 xAI：grok-4.7 读 PNG、JPEG、WebP 正常（2–4 秒）；GIF 返回 400 `invalid_image`（「does not contain a valid JPG, PNG, WebP, or ICO image」）。把图片链接交给 xAI 自己下载也不可靠：维基百科的图片链接返回 `image_download_error`。原来的代码把 QQ 图床链接原样交给模型，所以两种情况都会失败。
- 在 openqqwaifu 容器里直接下载 QQ 图床（`multimedia.nt.qq.com.cn`）正常，域名解析到公网地址，`safe_fetch` 放行。群里大肥鱼最近发的 4 张都是 `sub_type=1`、`summary="[动画表情]"` 的表情，3 张 PNG、1 张 JPEG，几十到几百 KB。
- 现在的处理见第 1 节「看图」。dry_run 实测：GIF 测试图转成第一帧后 grok-4.7 能说出「红圆圈旁边一个蓝 42」；标成表情的图片，回复是「这算哪门子表情」。
- 同一次 dry_run 里，新用户的第一条「在吗」有一次没有任何回复（5 秒）；同样的消息重放 7 次都正常。当时这条路径的模型错误不写日志，原因查不到，现在会记 WARNING。

## 4. 三块（A、B、C 已完成）

### A. 短期窗口（已完成，2026-10-27 实机验证过群聊环境缓冲）

原来的问题：群里没 @ 机器人的消息不入库；窗口 8 行且包含当前消息，`[Latest Message]` 又重复一次；历史行里带机器人自己的 `@号码`；机器人行的标签随当前说话人起的别名变化。

现在的做法：

- 群里没触发回复的消息先进内存环形缓冲（`WaifuService._ambient_history`，按 角色、会话类型、会话 ID 分，长度 `max(40, min(120, short_term_memory_limit * 2))`），机器人下次在这个群回复时一次性并入会话历史再保存。没有选「每条都存」，是为了避免每条群消息写一份整份会话快照。代价：服务重启会丢掉还没并入的缓冲；机器人发的消息、`ignore_prefixes` 开头的消息不进缓冲。缓冲的读、并、清都在同一把会话锁里（`_session_lock_for` 的键和缓冲键一致），没有并发丢行。
- `format_dialogue(..., exclude_last=True)` 去掉当前消息，`limit` 仍按「当前消息之前的行数」计。`max_chars` 从最新的行往前装，超出预算就停；每行先截到 `min(预算, 400)` 字，末尾加「…」。
- `to_memory_text(bot_account_id=...)` 只去掉机器人自己的 @；缓冲行、入库行、`[Latest Message]` 都用这个形式。
- 群聊里机器人行一律标成角色卡的 `assistant_name`；当前说话人起的别名仍作为「你是…」的身份（别名功能不变），另在 Speaker Notes 第一行加「X叫你「别名」；对话记录里「卡名」的发言就是你说的」。私聊只有一个人，标签和身份继续用别名，不加备注。知识抽取在群聊里也用卡名。
- 窗口变大后，知识抽取只取对话的最后 8 行，避免把群里其他人的话记到当前说话人头上。

测试：`test_group_ambient_messages_fold_into_next_reply`（3 条未触发 + 1 条 @，前 3 条进 `[Conversation]`，最新消息不重复，历史里没有机器人的 @）、`test_history_window_char_budget_drops_oldest_lines`、`test_to_memory_text_drops_only_bot_self_mention`、`test_group_dialogue_labels_bot_with_card_name_and_passes_alias_as_note`、`test_knowledge_query_only_sees_recent_conversation_lines`（`tests/test_app.py`）；`test_format_dialogue_exclude_last_keeps_full_window`、`test_format_dialogue_truncates_long_line_without_dropping_context`（`tests/test_organs.py`）。

没做的：（审计结论）快照回退（32 行变 31 行）的丢更新没有查。注意：缓冲并入后历史一次会多出很多行，会更快超过 30 行触发归档；归档的问题在 B 块。

### B. 当前时间、摘要、提示词质量（已完成，未在真实 QQ 环境验证）

原来的问题：提示词没有当前时间；每次回复只要超过 30 行就归档约 2 行、调一次摘要模型，摘要写成 `summary` 知识条目和事实抢召回名额，且摘要调用占着会话锁；内心想法被硬截到 30 字；抽取提示词不知道今天几号；模型说「没有值得记的」时线上会掉进正则回退去存东西。

现在的做法：

- 当前时间：`[State]` 第一行「当前时间：2026-10-26 周一 12:30（Asia/Shanghai）」。分析、称呼引导、工具结果渲染的提示词也带。时区来自 `config.timezone`；机器上没有时区库（精简容器、没装 tzdata 的 Windows）时按内置的固定偏移，未知名字按 UTC+8 并写一条警告。历史行仍然没有时间戳。
- 相对日期：抽取提示词给出今天的日期并要求写绝对日期；`_persist_extracted_knowledge` 存之前再用 `absolutize_relative_dates` 兜底改写（今天/明天/后天/大后天/昨天/前天/大前天、今早/今晚/明早/明晚/昨晚、上/这/本/下/下下 + 周/星期/礼拜 + 一到日/末/无）。「每周三」、单独的「周三」、「马上周末」「晚上周三」「看一下周五」「这周围」不改。召回时的查询没有做同样的改写，「下周三干嘛」只能靠话题词或向量召回到「2026-11-04 面试」。
- 滚动摘要：溢出达到 `memory_summary_batch_size` 才归档，一次把全部溢出折进 `session.metadata["rolling_summary"]`（模型要求 300 字内，硬上限 600 字，超了保留后半段）。提示词里是 `[Conversation]` 前的 `[Earlier Conversation]` 段。不再写 `summary` 知识条目；召回（两种存储和 `_session_knowledge_entries`）跳过 `summary` 和 30 天前的 `event`（`_EVENT_RECALL_MAX_AGE_SECONDS`）。归档在后台线程跑，模型调用不占会话锁，只有「重读、核对前缀、裁剪、保存」这一步拿会话锁；同一个会话同时只排一个归档。`skills/dispatcher.py` 里用户主动要的「总结」技能仍用原来的 `summarize_history`，没动。
- 内心想法：默认 80 字，`_clip_at_sentence` 在上限内最后一个句末标点处截断（该位置不到上限一半时才硬截加「…」）。没有做「很短的消息跳过分析调用」。
- 正则回退：`_writeback_knowledge_if_needed` / `_awriteback_knowledge_if_needed` 改为 `allow_fallback=not svc._requires_live_llm()`，线上模型返回空就不存；dry-run 和离线仍走回退。

测试：`test_summarization_mode_folds_overflow_into_rolling_summary`、`test_chat_prompt_has_current_time_and_earlier_conversation_section`、`test_knowledge_query_gives_today_for_absolute_dates`、`test_analysis_is_clipped_at_a_sentence_boundary`（`tests/test_app.py`）；`test_archive_waits_for_full_batch_and_stores_rolling_summary`、`test_archive_summarizes_outside_commit_lock`（`tests/test_organs.py`）；`test_relative_dates_are_stored_as_absolute_dates`、`test_live_runtime_does_not_fall_back_to_regex_extraction`（`tests/test_memory.py`）；`test_chat_summaries_are_not_recalled`、`test_events_older_than_a_month_are_excluded_from_recall`（`tests/test_state_store.py`）；`tests/test_clock.py`。

没做的（原建议第 5 条）：抽取时带上已有记忆、返回 add/update/delete；中文抽取提示词；过滤关于机器人自己、昵称、工具和搜索结果的条目。

### C. 技能触发（已完成，未在真实 QQ 环境验证）

原来的问题：线上 `ignore_prefixes` 含 `/`，`/skill` 命令到不了分发，到了也会因 `svc.config.background_image_delivery` 抛 `AttributeError`；`生图` 前缀只影响排名，仍要模型决定调不调；每轮只暴露排名前 `max_active_skills` 个技能，天气、搜索常常不在里面；`_clean_response` 把回复里的换行压掉；原始结果代码块可能重复；长回复整段发一条；`last_search` 每轮被清掉，跨轮问来源拿不到。

现在的做法：

- 显式命令：`/skill <技能 id/名字/别名> <参数>` 即使 `/` 在 `ignore_prefixes` 里也会执行；不认识的技能、其他 `/` 开头的消息仍然忽略。前缀由 `explicit_skill_prefixes` 配置。参数是 JSON/kv 之外的纯文本时，填进技能唯一的必填字符串字段（`/skill weather-command 上海` → `location`）。**这推翻了提交 a224e96 的约定**（当时的测试要求 `/skill` 被忽略、不绕过工具编排）；现在命令同样经过授权闸门：`_build_tool_invocation` 带上 `tool_orchestrator._allowed_tool_ids(event)`，未授权用户的 `/skill read-file-command …` 会被 `_execution_blocked` 拒绝。
- 生图：去掉对机器人的 @ 后以 `image_command_prefix` 或别名开头，就直接执行 `handler_target == "image"` 的技能，不经模型决策。线上没配好生图（`image_ready` 为假）时不拦截，交给模型。英文别名要求词边界（`draw a cat` 触发，`drawing` 不触发）。
- 暴露：模型可调用的技能 = 排名前 `max_active_skills` 个 + 固定的 `search`、`weather`、`summarize`、`image`（只取 `risk_level == "safe"` 的）；提示词里的技能卡片仍只放排名的那几个。高风险工具照旧由授权闸门控制。
- **安全修复**：`summarize` 工具标为 `safe`，但原来会读取任意本地路径（`_load_summarize_source` 直接 `Path(target).read_text()`，不检查目录），包括仓库里的 `.env` 和含 API key 的配置。现在本地文件只对 `tool_policy.authorized_user_ids` 里的人开放（`_extract_summarize_target(allow_local=...)`，`_resolve_summarize_payload` 再挡一次）；对其他人，路径当普通文字处理，不暴露文件是否存在。URL 抓取的内网拦截见下一条。
- 渲染：`_clean_response` 只合并词间空白、去行尾空白、三个以上换行压成两个，保留换行和行首缩进；它的其他调用点（图片说明、称呼引导、工具回复）都是给用户看的回复，分析和滚动摘要之后会再压平。工具渲染提示词里的结果预览改用 `_clip_multiline`。模型回复里已经整段出现过的原始结果不再追加代码块（工具循环和显式命令两处）。
- 长回复：`outbound_message_max_chars`（默认 900）以上按空行、换行、句末依次找切点（切点不到上限一半就硬切），分多条按顺序发，图片跟最后一条；历史和返回值里仍是完整回复。只作用于经过 emitter 的消息。
- 抓取 URL：web-fetch 和 summarize 原来能访问内网（NapCat WebUI、控制台、Docker 内部服务、169.254.169.254），而 summarize 是固定暴露、任何人都能触发的。现在走 `infra/safe_fetch.py`：只接受 http/https；先用 `getaddrinfo` 解析主机，任一地址不是公网（`ipaddress` 的 `is_global` 为假，或是组播；IPv4 映射的 IPv6 按 IPv4 判断）就拒绝，`127.1`、`2130706433`、`0x7f000001`、`napcat` 这类写法同样会被挡；重定向关掉自动跟随，手动跟最多 5 跳，每跳重新检查；正文流式读取，最多 2 MB（`DEFAULT_MAX_BYTES`，按解压后的字节算），超出截断并在 web-fetch 的 metadata 里标 `truncated`。被拦截时报「不能访问内网或本机地址：<主机>」。对所有人一律拦截，授权用户也不例外。仍有的缺口：检查和 httpx 连接各解析一次域名，DNS rebinding 可以钻这个空子；summarize CLI 只查第一跳，它自己跟的重定向和下载大小管不到；配了 `HTTP_PROXY` 时实际由代理去解析和连接。天气（wttr.in）和搜索（DuckDuckGo）的主机是固定的，没改。
- `last_search`：本轮没有搜索时，带结果的 `last_search` 保留（`LAST_SEARCH_TTL_SECONDS` 30 分钟后自然失效）；没结果的照旧清掉，避免被 `reply_gate._pending_search_payload` 当成待确认搜索。

测试：见第 2 节第 7 组。

没做的：

- `data/skills/skill.md`、`data/skills/summarize/SKILL.md` 两个旧格式文件没删（这轮不碰 `data/`），校验仍会报 `invalid_handler`、`duplicate_id`。
- （审计结论）`skills/intent_router.py`、`dispatcher.dispatch_skill` 的关键词分发、`MemberOnboarding.maybe_handle` 这些旧路径没有清理。
- 图片仍只能单张随最后一段发送；`gateways/onebot_actions.py::build_onebot_message_segments` 没改。

## 5. 其他待办（不在本轮）

- `web/http_api.py::_parse_segments` 只解析 text、image、at：引用回复（不知道对方在回哪条）、QQ 自带的小表情（`face`）、语音、转发、小程序卡片会被丢掉。图片已能看（第 1 节「看图」）。
- 好感度只涨不跌（`systems/value_game.py`），情绪几乎恒定（`systems/emotions.py` 的 `trust 0.55`），主动问候没有调度器（`organs/proactive.py` 只在控制台生成草稿），故事模式线上关闭，`memory/graph.py` 的高亮与知识库内容重复。
- ~~激活角色会清空所有会话记录~~（2026-10-26 已修）：`activate_character` 不再带 `reset_sessions`，启用同一个角色时也不重建存储；清空会话改为单独的 `POST /api/character/reset-sessions`（`confirm` 必须等于角色代号），控制台人物卡页「重置会话…」按钮要求输入代号确认。只删会话文件（含滚动摘要），长期记忆、成员和好感度不动。
- 群里 20 秒的跟进窗口（`group_follow_up_window_seconds`）按群算，不按人算：机器人回复后接任何人的话，每次回复都延长窗口（2026-10-27 因此和另一个机器人互相刷屏，见第 3.3 节）。（审计结论）LLM 出错时机器人静默不回复；主回复链路现在至少记一条 WARNING「chat reply failed: …」（`feat/human-touch`），其他调用（分析、抽取、摘要）仍不记。
- 召回时同步发起一次嵌入调用，延迟约一次网络往返。

## 6. 验证方法

1. 单测：见第 1 节。新测试一律用临时目录。
2. 不碰真实数据的实机冒烟。它会调用付费的模型和嵌入接口，每条消息约 3–4k 提示词 token、3–7 次调用：
   - 把 `data/docker-compose-config.json` 复制到一个临时目录，把 `data_root` 改成该目录下的 `data`，`qq_sidecar.dry_run` 设为 `true`（或设环境变量 `OPENQQWAIFU_QQ_SIDECAR_DRY_RUN=true`）；同时拷贝 `data/cards` 和 `data/state` 过去。
   - 以这个临时目录作为工作目录运行。`ConfigManager.load()` 会按配置路径和当前目录去找 `napcat/config`，可能改写其中的 OneBot 配置，所以不要在仓库根目录跑。
   - `dry_run` 现在真的生效（2026-10-26 修）：`build_runtime_service` 在 `dry_run` 为真时出站一律用 `CapturingOutboundPort`，配了 `outbound_base_url` 也不发；`reverse_ws` 模式仍保留网关，NapCat 照常连进来推事件，只是回复不发出去。其余按线上走：`_requires_live_llm()` 为真，模型没配好会回「AI 工具编排模型还没连上」，知识抽取不走正则回退。之前的说法「dry_run 已不改变行为」作废。
   - 也可以直接 `serve`：`dry_run` 为真时启动日志里是 `outbound=capture(dry-run)`，控制台概览的出站模式显示 `dry-run (capture)`。
   - 不想接 NapCat、只想喂几条消息时，用下面的脚本。`build_file_service` 本来就只捕获，但它按离线处理（允许回退）；要和线上一致就用 `build_runtime_service` 加 `dry_run`。

```python
# PYTHONPATH=<repo>/src  <repo>/.venv/bin/python
from waifu_standalone.settings_admin.config_manager import ConfigManager
from waifu_standalone.runtime.container import build_runtime_service
from waifu_standalone.models import InboundEvent, MessageSegment

config = ConfigManager("<tmp>/data/config.json").load()
assert config.qq_sidecar.dry_run  # 否则会真发
service, outbound = build_runtime_service(config)
ev = InboundEvent(launcher_id="20001", launcher_type="person", sender_id="20001",
                  sender_name="测试用户", segments=[MessageSegment(kind="text", text="在吗")])
reply = service.handle_event(ev)
service.flush_background_tasks(timeout=120)
print(reply.text if reply else None)  # outbound.sent 里是被捕获的外发消息
```

3. 想看最终提示词，可以在 `prompt_assembler.py` 的两个工具循环调用 LLM 之前打印 `prompt`，或在 `infra/llm_clients.py` 的请求处记录。

## 7. 工作方式提示

- 一次只改一块，改完跑全量测试。
- 提交前确认没有带上 `.env`、`output/`、`data/`。
- 不要在仓库根目录用默认的 `AppConfig()` 去保存角色卡或会话。
