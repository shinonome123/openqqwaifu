---
id: list-files-command
name: 列出文件
description: 当用户要求查看目录结构或列出文件时，调用目录列举工具。
input_schema: {"type":"object","properties":{"path":{"type":"string","description":"要列出的目录路径"}}}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"list-files-command","llm_tool":true,"keywords":["列出文件","看看目录","list files","ls","dir","目录列表"]}
handler: {"type":"tool_id","target":"list-files","arg_mode":"structured"}
policy: {"priority":8,"user_invocable":true,"risk_level":"filesystem","timeout_seconds":20,"max_output_chars":12000}
default_args: {}
metadata: {"aliases":["list-files","list_files","ls","dir","目录列表"]}
---
当用户明确要求列出目录内容时，调用目录列举工具。
