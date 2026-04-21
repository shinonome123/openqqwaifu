---
id: summarize-command
name: 外部内容总结
description: 当用户要总结一个链接、视频或文件时，直接调用 summarize 工具。
triggers: ["总结这个链接", "总结这个网页", "summarize", "帮我总结这个"]
aliases: ["summarize", "external-summary", "external_summary", "链接总结"]
mode: prefix
priority: 10
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: summarize
command-arg-mode: raw
---
当用户明确给出链接、视频或文件，并要求总结时，直接调用 summarize 工具，不要假装已经看过内容。
