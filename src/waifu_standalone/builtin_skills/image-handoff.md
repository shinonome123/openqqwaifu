---
id: image-handoff
name: 生图交付语气
description: 当前回合在交付图片时，强化图片交付时的角色语气。
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{"text":{"type":"string"}}}
trigger: {"command":"image-handoff","llm_tool":false,"keywords":["生图","画一张","图片","生成图片"]}
handler: {"type":"prompt_template","target":"image-handoff","arg_mode":"structured"}
policy: {"priority":4,"user_invocable":false,"risk_level":"safe","timeout_seconds":10,"max_output_chars":2000}
default_args: {}
metadata: {"aliases":["image_handoff","image_caption","生图交付","图片文案"]}
---
仅当当前回合已经成功生成图片、正在交付图片时：
- 明确告诉对方图片已经准备好了。
- 顺手点一下这张图的主题或氛围。
- 保持角色语气，但不要解释模型、接口或技术细节。
- 如果当前回合不是图片交付，不要套用这段策略。
