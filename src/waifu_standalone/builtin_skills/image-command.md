---
id: image-command
name: 直接生图
description: 当用户明确要你生成图片时，调用图片生成工具。
input_schema: {"type":"object","properties":{"prompt":{"type":"string","description":"用户想生成的图片内容"}},"required":["prompt"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"images":{"type":"array","items":{"type":"string"}}}}
trigger: {"command":"image-command","llm_tool":true,"keywords":["生图","draw","画一张","生成一张图","生成图片","画图"]}
handler: {"type":"tool_id","target":"image","arg_mode":"structured"}
policy: {"priority":13,"user_invocable":true,"risk_level":"safe","timeout_seconds":120,"max_output_chars":6000}
default_args: {}
metadata: {"aliases":["image","image_generate","draw","生成图片","画图"]}
---
当用户明确要你画图时，直接生成图片并回传，不要先进入普通聊天回复。
