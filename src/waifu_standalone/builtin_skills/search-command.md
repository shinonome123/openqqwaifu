---
id: search-command
name: 联网搜索
description: 当用户明确让你查一下、搜一下时，直接调用联网搜索工具。
triggers: ["搜一下", "查一下", "search", "搜索"]
aliases: ["search", "web_search", "lookup", "查资料", "联网搜索"]
mode: prefix
priority: 12
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: search
command-arg-mode: raw
---
当用户明确要求你去查时，直接走联网检索，不要先绕回普通聊天。
