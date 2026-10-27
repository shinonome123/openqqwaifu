# DeepSeek 角色体验问题交接

日期：2026-10-26。工作分支：`codex/runtime-v2-private-flow`。本提交包含此前累积的架构重置、审计修复和本轮角色试验，并非仅一次模型参数修改。新版私聊仍是试运行；不要将提交推送理解为生产验收或上线。

用户希望由另一个 AI 继续修复。先阅读本文及 [体验记录](RUNTIME_V2_EXPERIENCE_TRIAL.md)。本轮不再调用模型，DeepSeek 已使用 22 次生成请求；接手后的任何真实调用应另行明确预算。仓库不包含密钥或真实私聊。

## P1：结构化回复模式返回空白，整轮对话失败

这是下一步最优先的问题。

- 官方入口 `https://api.deepseek.com/v1`，模型 ID `deepseek-flash`，官方说明对应 DeepSeek-V4.1-Flash-1017。
- 新版运行时开启 `structured_memory_enabled`，要求 `{"reply":"正文","memories":[]}` 或符合字段契约的记忆提案。
- 原先仅通过提示要求 JSON，偶尔得到普通正文。已添加 `response_format={"type":"json_object"}`，但仍出现 `content` 只有空格、`finish_reason="stop"`。这不是本地 JSON 解析器把有效正文丢掉，也不是 `length` 截断。
- 运行时返回 `generation_IncompleteModelResponse`，没有生成该轮出站消息或记忆。用户侧表现为没有收到回复。严格拒绝空结果是正确的；导致结果为空的原因及用户可理解的失败恢复尚待处理。

固定三轮复现：

1. “周三要参加面试，我最担心自我介绍。先不用帮我核对日期。”
2. “我刚看错了，是下周四，其他安排没变。”
3. “先不聊日期，刚才说的那个担心，你还记得是什么吗？”

第三轮在两次独立试验均为空白。前两轮成功生成结构化回复，经历修订至版本 2，并保留“最担心自我介绍”；不要误判为记忆未保存。

单轮也能触发：“下雨天有一点无聊，想听你聊点自己喜欢的东西。”同一角色下，多条 system 提示与合并成一条 system 的对照均为空白。**不能据此认定合并 system 就能修好。**关闭结构化记忆后，两角色三轮各完成，但这只验证普通角色对话，不解决生产结构化链路。

仓库中的 [合成失败与对照材料](../examples/deepseek-flash-1017-observations.json) 包含失败请求的 wire messages、两个 system 形态的原始响应、三轮纠正结果、六轮纯对话对照。wire messages 取自 ContextBuilder，在供应商适配器规范化之前；不包含 HTTP 认证头。真实模型有随机性，材料证明曾经出现的问题，不保证每次复现。

建议排查入口：

- `src/waifu_standalone/conversation/composer.py`：单次调用、JSON 模式选择和失败记录。
- `src/waifu_standalone/conversation/memory_output.py`：输出契约及严格解析。
- `src/waifu_standalone/conversation/context.py`：角色、历史、记忆和风格示例的实际组织。
- `src/waifu_standalone/infra/llm_clients.py`：供应商请求与空白判断。
- `src/waifu_standalone/runtime_v2/engine.py`：失败状态与出站提交边界。

修复应先用固定输入和模拟空响应建立离线回归，再做受控真实验证。若引入重试，必须显式限制次数、记录原因，并证明不会重复写记忆或发送；不要偷偷无限重试。不要通过关闭严格记忆校验、将空白判为成功、编造角色回复或全局关闭记忆来绕过问题。若调整供应商参数或提示，应保留能解释前后差异的对照。官方 JSON 模式文档有空输出说明，但具体根因本轮尚未证实。

验收至少包含：两角色在倾听、纠正、兴趣场景都能完成结构化多轮；当前用户引文与记忆版本校验保持有效；供应商空结果/截断/格式错时无误写、无重复发送，且失败可诊断、可明确恢复。自然语言正文与记忆提取如何解耦属于可评审的设计选择，不应在解析器里临时偷放行。

## P2：角色每轮都反问，节奏固定

纯对话对照的六条回复全部以问题结尾。用户第三轮要求“推荐一个今晚能轻松聊的话题吧，不用列计划”，角色虽没有列计划，仍惯性以反问结束。

检查两张角色卡、风格示例和上下文提示是否持续强化“回应后提问”。目标是保持具体兴趣与角色差异，也允许分享、接话和停顿；不要简单用字符串删问号。增加包含“只想听你说”“不用问我”的连续场景，以真实回复评审，避免关键词匹配冒充自然度评估。

涉及 `src/waifu_standalone/templates/quiet-reader_person.yaml` 与 `curious-maker_person.yaml`。小栞的书与文字、阿澈的机械与小游戏差异已经可辨认，修复时应保留。

## P2：虚构兴趣的措辞容易被当成现实经历

阿澈首轮说：“下雨天正好适合摆弄工作台上那堆小东西。我最近迷上……”；被追问后才澄清“其实我也没有现实里的工作台”。小栞回答“是我自己的偏好”后虽否认共同经历，对设定性质的说明仍不够直接。

本次没有捏造与用户的共同经历，但现实活动的措辞仍需改进。目标是自然表达角色兴趣，不声称实际正在操作设备或近期真的做过某事，也不必每轮机械声明“我是虚构角色”。用同一场景检查初次表达及被追问后的澄清是否一致。

## P2：Windows MCP 子进程清理偶发失败

一次全量 593 项运行中，`tests.test_claw_runtime.ClawRuntimeTests.test_runtime_lists_and_invokes_mcp_tools_when_bridge_enabled` 在临时目录删除阶段报 WinError 32，目录为测试临时根下的 `claw_runtime/plugins/echo-bundle`。业务断言已执行，异常发生于清理。单独复跑及之后全量 596 项均通过，尚未确定竞态原因，也没有修改无关清理代码。

可在 Windows 重复该单测，检查 `service.close()`、MCP 子进程结束及工作目录句柄释放。不要吞掉所有清理错误或误杀其他运行进程。完整堆栈只在本地临时日志，仓库仅保留这一事实描述。

## 已修复，不要重复处理

- 完整用户正文及近期跨轮倾听边界检查，含“先不用帮我核对日期”。
- 时间核对取消后的 30 分钟自动提问间隔，显式命令仍可进入核对。
- 自动核对轮次保留用户情绪正文和普通回复，仅过滤追加的管理问题。
- 结构化请求的 JSON 模式及 UnifiedLLMClient 转发；语义和来源校验未放宽。
- 体验脚本调用上限、首个失败停止、逐轮落盘、网络失败不复用旧输出、失败也保存记忆快照、明确标记纯对话对照。

## 验证与运行方式

最终全量 596 项通过（180.643 秒）。之后评估脚本新增纯对话开关，4 项专项全部通过；供应商空白样例加到现有测试中另做专项检查。提交前相关专项 73 项通过，17 个变更 JavaScript 文件语法检查通过。模型试验没有真实 QQ 发送或生产配置修改。之前 593 项运行的清理错误仍列为未确认的偶发问题。

```powershell
python -B -m unittest discover -s tests -v
python -B -m unittest tests.test_conversation_context tests.test_role_experience_runner tests.test_runtime_v2_experience tests.test_runtime_v2_episode_time_dialogue -v
```

本机已验证环境为 `.codex-temp/audit-venv/Scripts/python.exe`（Python 3.12.14）；其他环境请按项目依赖安装，不依赖这个临时路径。无需模型密钥即可运行上述测试。

真实抽样脚本为 `scripts/evaluate_role_experience.py`。密钥只从进程环境变量 `WAIFU_EVAL_API_KEY` 读取，不自动读取 `.env`。命令示例在体验记录中；`--reply-only` 是诊断模式，不能用其结果宣称结构化记忆通过。临时 `.codex-temp/` 日志不随提交推送，接手应使用已提交的 examples 证据与场景。

尚未完成：两角色全部四组真实场景、跨日体验、40 组多次评审、七天连续验收以及真实 QQ 发送/重连验证。不要把离线全绿等同于这些验收完成。
