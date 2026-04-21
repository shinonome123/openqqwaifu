---
id: summary-command
name: 会话总结
description: 当用户明确要求总结最近对话时，直接调用会话总结工具。
triggers: ["总结一下", "总结下", "帮我总结", "总结本群", "总结对话"]
aliases: ["summary", "conversation_summary", "对话总结", "会话总结"]
mode: prefix
priority: 11
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: summary
command-arg-mode: raw
---
当用户要你收一收重点时，直接整理最近上下文并给出简明总结。
