---
id: search-links-command
name: 搜索来源链接
description: 当用户要上一轮检索的原文链接或来源时，调用搜索来源链接工具。
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{"text":{"type":"string"}}}
trigger: {"command":"search-links-command","llm_tool":true,"keywords":["给我链接","给我来源","sources","原文链接","来源链接","出处"]}
handler: {"type":"tool_id","target":"search-links","arg_mode":"structured"}
policy: {"priority":8,"user_invocable":true,"risk_level":"safe","timeout_seconds":20,"max_output_chars":8000}
default_args: {}
metadata: {"aliases":["search-links","search_links","search_sources","sources","links"]}
---
当用户要搜索来源或原文链接时，返回真实来源，不要凭空编造 URL。
