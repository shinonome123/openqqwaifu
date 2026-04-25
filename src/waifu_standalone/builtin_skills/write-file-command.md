---
id: write-file-command
name: 写入文件
description: 当用户明确要求写入允许目录内的文件时，调用文件写入工具。
input_schema: {"type":"object","properties":{"path":{"type":"string","description":"要写入的文件路径"},"content":{"type":"string","description":"要写入的文本内容"},"append":{"type":"boolean","description":"是否追加写入"}},"required":["path","content"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"write-file-command","llm_tool":true,"keywords":["写文件","保存到文件","write file","追加到文件"]}
handler: {"type":"tool_id","target":"write-file","arg_mode":"structured"}
policy: {"priority":5,"user_invocable":true,"risk_level":"filesystem","timeout_seconds":20,"max_output_chars":6000,"requires_authorization":true}
default_args: {}
metadata: {"aliases":["write-file","write_file","写入文件"]}
---
只有用户明确要求写入文件时才调用，执行层仍必须遵守允许目录和写入策略。
