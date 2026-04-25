---
id: weather-command
name: 天气查询
description: 当用户询问某地天气、气温或天气趋势时，调用天气查询工具。
input_schema: {"type":"object","properties":{"location":{"type":"string","description":"城市、地区或地名"}},"required":["location"]}
output_schema: {"type":"object","properties":{"text":{"type":"string"},"metadata":{"type":"object"}}}
trigger: {"command":"weather-command","llm_tool":true,"keywords":["天气","气温","下雨吗","weather","温度"]}
handler: {"type":"tool_id","target":"weather","arg_mode":"structured"}
policy: {"priority":8,"user_invocable":true,"risk_level":"safe","timeout_seconds":20,"max_output_chars":6000}
default_args: {}
metadata: {"aliases":["weather","天气查询","查天气"]}
---
当用户想知道天气时，先调用天气工具，不要凭印象回答。
