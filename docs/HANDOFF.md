# 交接说明（2026-10-25，2026-10-26 更新）

给新会话：这份文档只记录工程事实。先读第 1、2 节。第 4 节的 A、B、C 三块已在 2026-10-26 做完（未提交、未实机验证），剩下的事在各块的「没做的」和第 5 节。标注「（审计结论）」的条目来自代码审查，没有逐条复核，改之前先对一下代码。

## 1. 先读这些

- 跑测试：`PYTHONPATH=src .venv/bin/python -B -m unittest discover -s tests`，336 个，约 45 秒，目前全过。系统 `python3` 没装 `httpx`，必须用项目的 `.venv/bin/python`。
- CLAUDE.md 里的模块表已经过时，代码现在按子包组织：`runtime/`（服务编排）、`reply_flow/`（回复链路、提示词）、`memory/`（会话与知识）、`repositories/`（SQLite 状态）、`skills/`（技能与工具）、`members/`、`character/`、`settings_admin/`、`web/`、`infra/`、`gateways/`、`systems/`、`organs/`。
- 一次回复的最终输入是一条 system 消息加一条 user 消息：
  - system：`reply_flow/prompt_assembler.py` 顶部的 `_TOOL_CALLING_SYSTEM_PROMPT`。
  - user：`character/cards.py::CharacterCard.system_prompt` 依次拼出 Profile、Skills、Background、Rules、Memories、Speaker Notes、State、Conversation、Latest Message 和输出指令；`prompt_assembler.py`（约 1403 行）再追加 `[Inner Thought]`。
- 每条被回复的消息通常触发 3–5 次模型调用（分析、回复、后台知识抽取，外加称呼判断、摘要、工具循环的额外轮次），最多约 7 次；每次召回还有 1 次嵌入调用。

## 2. 当前工作区状态

分支 `codex/runtime-ui-and-image-fixes`，HEAD `5865fa9`，已并入 origin/main（本地 main 落后 3 个合并提交）。**全部改动未提交。** 提交前先 `git switch -c <新分支名>`，不要继续在这条已合并的分支上提交。

未跟踪文件 `.env`（NapCat 令牌、账号）没有被 `.gitignore` 忽略，另有 `output/`（截图和 PR 草稿）。不要 `git add .`。

改动分七组，建议按这个顺序拆成七个提交。`prompt_assembler.py`、`runtime/container.py`、`runtime/service.py`、`reply_flow/service.py`、`skills/dispatcher.py`、`tests/test_skills.py`、`tests/test_app.py` 里几组改动混在一起，拆分要用 `git add -p`。

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

## 3. 运行数据与配置（`data/` 被 gitignore，不在 git 里）

- `data/docker-compose-config.json` 已改：`tool_policy.exec_enabled=false`、`write_enabled=false`、`authorized_user_ids=[]`。`embedding.model` 由 `qwen3-vl-embedding` 改为 `text-embedding-v4`：前者在 DashScope 的 OpenAI 兼容 `/embeddings` 上不可用，每次都返回 404；后者已实测，1024 维。更换嵌入模型要重新校准 `_SEMANTIC_RELEVANCE_MIN`，`text-embedding-v4` 上相关句对约 0.42–0.64，无关句对约 0.19–0.37。
- 会话记录 `data/sessions/aurora/*.jsonl` 已清空；`runtime.sqlite3` 的 `knowledge_entries` 已清空，`members`、`member_persona_state` 的画像文本已清空，成员、好感度等结构数据保留。服务启动时 `refresh_knowledge_embeddings()` 会给缺向量的条目补嵌入。清空前的备份在系统临时目录，可能被系统清理：`/private/tmp/claude-501/-Users-shinonome-openqqwaifu/bd3c61d7-dd79-4eaf-bd72-9fa0d7f1d365/scratchpad/data-backup-20261025-160341`。
- 角色卡 `data/cards/aurora_person.yaml`、`aurora_group.yaml` 现在是测试夹具内容，是被上面第 4 组里的两个测试覆盖的。原人设文字只留在 `data/cards/aurora.meta.json` 的 `portrait_prompt` 里 `Character traits:` 下的那 8 行；恢复办法是把这 8 行写回两个 yaml 的 `Profile:`。这件事由 owner 决定，新会话不要自行改动。
- `data/docker-compose-config.json` 里仍是 `"history_window_messages": 8`、`"max_thinking_words": 30`，没有 `history_window_chars`（缺省 2500）和 `timezone`（缺省 `Asia/Shanghai`）。A、B 块的新默认值（30、80）被这个文件覆盖，要生效需要 owner 改掉或删掉这两项。
- 线上 `summarization_mode` 是 `true`、`memory_summary_batch_size` 是 12：现在每多出 12 行才调一次摘要。`knowledge_entries` 里如果还有旧的 `memory_type="summary"` 条目，召回会跳过，不会自动删除。
- 以上改动之后服务没有在真实 QQ 环境跑过，Docker 当前没有运行。

## 4. 三块（A、B、C 已完成）

### A. 短期窗口（已完成，未在真实 QQ 环境验证）

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

- 机器人看不到图片：`InboundEvent.image_payloads()` 从没被调用，图片只存成「发送了图片」；`web/http_api.py`（105–115 行）只解析 text、image、at，引用回复、表情、语音、转发、小程序卡片会被丢掉。
- 好感度只涨不跌（`systems/value_game.py`），情绪几乎恒定（`systems/emotions.py` 的 `trust 0.55`），主动问候没有调度器（`organs/proactive.py` 只在控制台生成草稿），故事模式线上关闭，`memory/graph.py` 的高亮与知识库内容重复。
- 激活角色会清空所有会话记录：`character/service.py::save_character_panel` 在 `set_active` 时调用 `activate_character(..., reset_sessions=True)`，即使是同一个角色（57 行）。
- 群里 20 秒的跟进窗口（`group_follow_up_window_seconds`）会让机器人接任何人的话；（审计结论）LLM 出错时异常被吞，机器人静默不回复。
- 召回时同步发起一次嵌入调用，延迟约一次网络往返。

## 6. 验证方法

1. 单测：见第 1 节。新测试一律用临时目录。
2. 不碰真实数据的实机冒烟。它会调用付费的模型和嵌入接口，每条消息约 3–4k 提示词 token、3–7 次调用：
   - 把 `data/docker-compose-config.json` 复制到一个临时目录，把 `data_root` 改成该目录下的 `data`，`qq_sidecar.dry_run` 设为 `true`；同时拷贝 `data/cards` 和 `data/state` 过去。
   - 以这个临时目录作为工作目录运行。`ConfigManager.load()` 会按配置路径和当前目录去找 `napcat/config`，可能改写其中的 OneBot 配置，所以不要在仓库根目录跑。

```python
# PYTHONPATH=<repo>/src  <repo>/.venv/bin/python
from waifu_standalone.settings_admin.config_manager import ConfigManager
from waifu_standalone.runtime.container import build_file_service
from waifu_standalone.models import InboundEvent, MessageSegment

service, outbound = build_file_service(ConfigManager("<tmp>/data/config.json").load())
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
