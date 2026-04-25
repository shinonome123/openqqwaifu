---
id: read-file-command
name: 读取文件
description: 当用户要求读取允许目录内的本地文件时，调用文件读取工具。
input_schema: {"type":"object","properties":{"path":{"type":"string","description":"要读取的文件路径"}},"required":["path"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"read-file-command","llm_tool":true,"keywords":["读文件","查看文件","read file","cat","打开文件"]}
handler: {"type":"tool_id","target":"read-file","arg_mode":"structured"}
policy: {"priority":9,"user_invocable":true,"risk_level":"filesystem","timeout_seconds":20,"max_output_chars":12000}
default_args: {}
metadata: {"aliases":["read-file","read_file","read","file_read","读取文件"]}
---
当用户明确要求读取本地文件时，调用文件读取工具，避免凭空转述文件内容。
