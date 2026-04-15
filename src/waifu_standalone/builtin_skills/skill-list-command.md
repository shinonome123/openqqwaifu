---
id: skill-list-command
name: 技能列表
description: 当用户询问可用技能时，列出当前所有已启用的技能及触发方式。
triggers: ["你会什么", "你会干什么", "你能做什么", "技能列表", "技能菜单", "功能菜单", "命令菜单", "你有什么技能", "你有哪些能力", "你都会啥", "你可以做什么", "skills", "help"]
mode: prefix
priority: 10
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: skill-list
command-arg-mode: raw
---
当用户想了解你能做什么时，直接展示完整的技能清单，不要进入普通对话。
