---
id: search-command
name: 联网检索
description: 当前缀是搜一下或查一下时，直接触发联网搜索工具。
triggers: ["搜一下", "查一下", "search", "搜搜"]
mode: prefix
priority: 12
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: search
command-arg-mode: raw
---
当用户明确要求你去查时，直接走联网检索，不要先绕回普通聊天。
