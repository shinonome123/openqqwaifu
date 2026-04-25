---
id: summarize-command
name: 外部内容总结
description: 当用户要总结链接、视频、网页、播客或文件内容时，调用 summarize 工具。
input_schema: {"type":"object","properties":{"target":{"type":"string","description":"URL、文件路径或待总结对象"}},"required":["target"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"summarize-command","llm_tool":true,"keywords":["总结这个链接","总结这个网页","summarize","帮我总结这个","看看这个视频","这个网页在讲什么"]}
handler: {"type":"tool_id","target":"summarize","arg_mode":"structured"}
policy: {"priority":10,"user_invocable":true,"risk_level":"safe","timeout_seconds":90,"max_output_chars":10000}
default_args: {}
metadata: {"aliases":["summarize","external-summary","external_summary","链接总结"]}
---
当用户给出外部内容并要求总结时，必须调用 summarize 工具，不要假装已经看过内容。
