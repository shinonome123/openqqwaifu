---
id: list-files-command
name: 列出文件
description: 当用户明确要求查看目录结构或列出文件时，直接调用目录列举工具。
triggers: ["列出文件", "看看目录", "list files", "ls "]
aliases: ["list-files", "list_files", "ls", "dir", "目录列表"]
mode: prefix
priority: 8
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: list-files
command-arg-mode: raw
---
当用户明确要求列出目录内容时，直接调用目录列举工具。
