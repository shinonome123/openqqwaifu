---
id: web-fetch-command
name: 抓取网页
description: 当用户明确要求抓网页、打开网页内容或读取 URL 正文时，直接调用网页抓取工具。
triggers: ["抓网页", "读取网页", "fetch", "打开这个网址"]
aliases: ["web-fetch", "web_fetch", "fetch_url", "网页抓取", "打开网页"]
mode: prefix
priority: 9
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: web-fetch
command-arg-mode: raw
---
当用户明确要求抓取某个 URL 的正文时，直接调用网页抓取工具，并返回真实内容，不要只靠猜测。
