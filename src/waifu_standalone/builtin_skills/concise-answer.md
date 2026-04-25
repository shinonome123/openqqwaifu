---
id: concise-answer
name: 简洁回答
description: 当用户要求一句话、简短点或直接结论时，压缩回复长度。
input_schema: {"type":"object","properties":{}}
output_schema: {"type":"object","properties":{"text":{"type":"string"}}}
trigger: {"command":"concise-answer","llm_tool":false,"keywords":["简短点","一句话","直接说结论","短一点","别废话"]}
handler: {"type":"prompt_template","target":"concise-answer","arg_mode":"structured"}
policy: {"priority":5,"user_invocable":false,"risk_level":"safe","timeout_seconds":10,"max_output_chars":2000}
default_args: {}
metadata: {}
---
如果用户明确要求更短的回答：
- 先给结论，再补一句必要说明。
- 总长度尽量控制在两句以内。
- 不要为了简短牺牲核心事实。
- 这个约束只对当前用户这一次明确要求生效，不要无故延续到后续复杂问题。
