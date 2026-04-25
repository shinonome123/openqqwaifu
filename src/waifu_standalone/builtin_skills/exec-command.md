---
id: exec-command
name: 执行命令
description: 当用户明确要求执行本地允许命令时，调用命令执行工具。
input_schema: {"type":"object","properties":{"command":{"type":"string","description":"命令字符串"},"argv":{"type":"array","items":{"type":"string"},"description":"结构化参数数组"},"cwd":{"type":"string","description":"执行目录"},"timeout_seconds":{"type":"number","description":"超时时间"}}}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"exec-command","llm_tool":true,"keywords":["执行命令","运行命令","run command","exec"]}
handler: {"type":"tool_id","target":"exec-command","arg_mode":"structured"}
policy: {"priority":4,"user_invocable":true,"risk_level":"command","timeout_seconds":30,"max_output_chars":12000,"requires_authorization":true}
default_args: {}
metadata: {"aliases":["exec","exec-command","执行命令"]}
---
只有用户明确要求执行命令时才调用，执行层仍必须遵守 allowlist、工作目录、超时和输出截断。
