---
id: web-fetch-command
name: 抓取网页
description: 当用户要求抓取网页、打开网页内容或读取 URL 正文时，调用网页抓取工具。
input_schema: {"type":"object","properties":{"url":{"type":"string","description":"要抓取的 URL"}},"required":["url"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"web-fetch-command","llm_tool":true,"keywords":["抓网页","读取网页","fetch","打开这个网址","读取这个网址","网页正文"]}
handler: {"type":"tool_id","target":"web-fetch","arg_mode":"structured"}
policy: {"priority":9,"user_invocable":true,"risk_level":"safe","timeout_seconds":45,"max_output_chars":12000}
default_args: {}
metadata: {"aliases":["web-fetch","web_fetch","fetch_url","网页抓取","打开网页"]}
---
当用户明确要求抓取某个 URL 的正文时，调用网页抓取工具并返回真实内容。
