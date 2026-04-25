---
id: summary-command
name: 会话总结
description: 当用户要求总结最近对话、群聊上下文或本轮聊天重点时，调用会话总结工具。
input_schema: {"type":"object","properties":{"topic":{"type":"string","description":"可选的总结重点"}}}
output_schema: {"type":"object","properties":{"text":{"type":"string"}}}
trigger: {"command":"summary-command","llm_tool":true,"keywords":["总结一下","总结下","帮我总结","总结本群","总结对话","收一下重点"]}
handler: {"type":"tool_id","target":"summary","arg_mode":"structured"}
policy: {"priority":11,"user_invocable":true,"risk_level":"safe","timeout_seconds":30,"max_output_chars":6000}
default_args: {}
metadata: {"aliases":["summary","conversation_summary","对话总结","会话总结"]}
---
当用户要你收一收重点时，调用会话总结工具整理最近上下文。
