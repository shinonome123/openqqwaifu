---
id: freshness-check
name: 时效性核验
description: 遇到明显依赖实时信息的话题时，要求模型优先调用搜索等工具核验事实。
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{"text":{"type":"string"}}}
trigger: {"command":"freshness-check","llm_tool":false,"keywords":["最新","今天","刚刚","价格","新闻","实时","2026"]}
handler: {"type":"prompt_template","target":"freshness-check","arg_mode":"structured"}
policy: {"priority":8,"user_invocable":false,"risk_level":"safe","timeout_seconds":10,"max_output_chars":2000}
default_args: {}
metadata: {}
---
遇到明显依赖实时信息的话题时：
- 需要回答事实时，优先调用搜索等可用工具核验，而不是只提醒用户去查。
- 如果工具已经返回可靠结果，直接基于结果回答，不要再补一句“建议先查一下”。
- 如果没有工具结果或工具失败，要说明无法确认，不要装作已经确认过。
- 语气保持自然，不要把这段技能文本直接复读出来。
