# 记忆系统改造草案

## 目标

把当前运行时里的“会话历史”拆成三层，避免称呼、画像、长期记忆混在一起。

1. `session_history`
   - 原始对话历史
   - 跟当前会话强相关的短期上下文
2. `user_directory`
   - 用户和群成员的结构化资料
   - 包括 `preferred_name`、群昵称、角色状态、入库时间、最近活跃时间
3. `knowledge_base`
   - 模型从对话中抽取出的长期知识
   - 只存“可复用事实”和“可检索经验”，不直接覆盖用户资料

## 为什么要分开

- `preferred_name` 这类字段必须稳定，不能让模型自由改写。
- 长期记忆适合存“她喜欢深夜聊天”“这个群平时聊游戏”，不适合承担用户主档案。
- 群成员数据库要支持前端人工修正，这和自由文本记忆是两种数据形态。

## 推荐的数据边界

### user_directory

每个成员一条记录，建议字段：

- `group_id`
- `user_id`
- `qq_nickname`
- `group_card`
- `preferred_name`
- `profile_summary`
- `onboarding_status`
- `last_seen_at`
- `last_addressed_at`
- `notes_count`

### knowledge_base

每条记忆一条记录，建议字段：

- `memory_id`
- `scope_type` (`global` / `group` / `user`)
- `scope_id`
- `memory_type` (`fact` / `preference` / `relationship` / `event`)
- `summary`
- `tags`
- `source_message_ids`
- `confidence`
- `created_at`
- `updated_at`
- `archived`

## 写入策略

### user_directory

不要让模型直接写。

只通过下面几类动作更新：

1. 群成员扫描同步
2. 用户首次互动后的称呼确认
3. 管理台人工修改
4. 明确规则触发的字段更新

### knowledge_base

允许模型写，但必须走结构化抽取：

1. 对话结束后异步提取
2. 输出固定 schema
3. 做去重和低置信度过滤
4. 只追加或归档，不直接覆盖用户主档案字段

## 进群后的推荐流程

1. 机器人进群
2. 调 sidecar 拉群成员列表
3. 初始化 `user_directory`
4. 不主动骚扰全群
5. 当有人首次 `@` 机器人时：
   - 如果该成员没有 `preferred_name`
   - 机器人先问一句“我该怎么称呼你？”
6. 用户回复后：
   - 写入 `preferred_name`
   - 更新 `onboarding_status=ready`
7. 后续对话检索时：
   - 先查 `user_directory`
   - 再查 `knowledge_base`

## 前端应该体现什么

### 个人用户页

- 群成员列表
- `preferred_name`
- QQ 原始昵称 / 群名片
- 画像摘要
- onboarding 状态
- 最近活跃时间

### 记忆页

- 长期知识列表
- 类型、作用域、标签、来源、置信度
- 支持 pin / archive / edit

## 第一阶段范围

先做下面这些，不迁老画像：

1. 空的 `user_directory`
2. 空的 `knowledge_base`
3. 群成员拉取与初始化
4. 首次称呼确认流程
5. 前端成员表和知识表

## 暂时不做

1. 旧画像自动迁移
2. memory graph
3. 主动剧情 / value game / narrator 深度耦合
