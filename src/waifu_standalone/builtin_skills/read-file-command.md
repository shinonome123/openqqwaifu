---
id: read-file-command
name: 读取文件
description: 当用户明确要求读取本地文件时，直接调用文件读取工具。
triggers: ["读文件", "查看文件", "read file", "cat "]
aliases: ["read-file", "read_file", "read", "file_read", "读取文件"]
mode: prefix
priority: 9
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: read-file
command-arg-mode: raw
---
当用户明确要求读取本地文件时，直接调用文件读取工具，避免凭空转述文件内容。
