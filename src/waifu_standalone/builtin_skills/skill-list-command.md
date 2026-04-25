---
id: skill-list-command
name: 技能列表
description: 当用户询问你会什么、能做什么或有哪些工具时，调用技能列表工具。
input_schema: {"type":"object","properties":{"detail":{"type":"boolean","description":"是否展示更详细的能力说明"}}}
output_schema: {"type":"object","properties":{"text":{"type":"string"}}}
trigger: {"command":"skill-list-command","llm_tool":true,"keywords":["你会什么","你会干什么","你能做什么","你有什么技能","你有哪些能力","你可以做什么","你都会啥","技能列表","技能菜单","功能菜单","命令菜单","skills","help"]}
handler: {"type":"tool_id","target":"skill-list","arg_mode":"structured"}
policy: {"priority":10,"user_invocable":true,"risk_level":"safe","timeout_seconds":20,"max_output_chars":8000}
default_args: {}
metadata: {"aliases":["skill-list","skills","abilities","help"]}
---
当用户想了解你能做什么时，调用技能列表工具展示当前能力清单。
