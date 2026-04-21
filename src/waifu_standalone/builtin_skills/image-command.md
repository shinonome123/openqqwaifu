---
id: image-command
name: 直接生图
description: 当用户明确要你生成图片时，直接调用图片生成工具。
triggers: ["生图", "draw", "画一张", "生成一张图"]
aliases: ["image", "image_generate", "draw", "生成图片", "画图"]
mode: prefix
priority: 13
user-invocable: true
disable-model-invocation: true
command-dispatch: tool
command-tool: image
command-arg-mode: raw
---
当用户明确要你画图时，直接生成图片并回传，不要先进入普通聊天回复。
