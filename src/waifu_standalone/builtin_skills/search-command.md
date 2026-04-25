---
id: search-command
name: 联网搜索
description: 当用户需要查询事实、实时信息、资料或来源时，调用联网搜索工具。
input_schema: {"type":"object","properties":{"query":{"type":"string","description":"要检索的问题或关键词"}},"required":["query"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"search-command","llm_tool":true,"keywords":["搜一下","查一下","search","搜索","最新","新闻","价格","刚刚"]}
handler: {"type":"tool_id","target":"search","arg_mode":"structured"}
policy: {"priority":12,"user_invocable":true,"risk_level":"safe","timeout_seconds":45,"max_output_chars":8000}
default_args: {}
metadata: {"aliases":["search","web_search","lookup","查资料","联网搜索"]}
---
当用户需要你查询外部事实或实时信息时，调用联网检索，不要凭空回答。
