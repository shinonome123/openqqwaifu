---
id: search-links-command
name: 搜索来源链接
description: 当用户要上一轮检索的原文链接或来源时，直接调用搜索来源链接工具。
triggers: ["给我链接", "给我来源", "sources", "原文链接"]
aliases: ["search-links", "search_links", "search_sources", "sources", "links"]
mode: contains
priority: 8
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: search-links
command-arg-mode: raw
---
当用户要搜索来源或原文链接时，优先返回真实来源，不要凭空编造 URL。
