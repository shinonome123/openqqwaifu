---
id: image-command
name: 直接生图
description: 对方想看到一幅画面时调用图片生成工具：明说要画、要图，或者话里明显希望看到（「好想看看」「长什么样啊」）。
input_schema: {"type":"object","properties":{"prompt":{"type":"string","description":"用户想生成的图片内容"}},"required":["prompt"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"images":{"type":"array","items":{"type":"string"}}}}
trigger: {"command":"image-command","llm_tool":true,"keywords":["生图","draw","画一张","生成一张图","生成图片","画图"]}
handler: {"type":"tool_id","target":"image","arg_mode":"structured"}
policy: {"priority":13,"user_invocable":true,"risk_level":"safe","timeout_seconds":120,"max_output_chars":6000}
default_args: {}
metadata: {"aliases":["image","image_generate","draw","生成图片","画图"]}
---
对方想看一幅画面时（明说要画，或者「好想看看那是什么样子」这种），直接调用工具画出来，不要只嘴上答应。只是普通聊天、没想看画面，就不要自己配图。
