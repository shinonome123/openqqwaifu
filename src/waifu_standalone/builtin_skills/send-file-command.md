---
id: send-file-command
name: 发送文件
description: 当用户要把共享文件夹里的文件发到群里或发给自己时，调用发送文件工具。
input_schema: {"type":"object","properties":{"path":{"type":"string","description":"共享文件夹里的文件路径"},"name":{"type":"string","description":"发出去时显示的文件名"}},"required":["path"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"send-file-command","llm_tool":true,"keywords":["发文件","发群文件","上传文件","把文件发","发到群里"]}
handler: {"type":"tool_id","target":"send-file","arg_mode":"structured"}
policy: {"priority":5,"user_invocable":true,"risk_level":"filesystem","timeout_seconds":60,"max_output_chars":2000,"requires_authorization":true}
default_args: {}
metadata: {"aliases":["send-file","send_file","发送文件","发文件"]}
---
只发共享文件夹里的文件；群里发成群文件，私聊发成私聊文件。文件还不存在时，先用写入文件工具写好再发。
