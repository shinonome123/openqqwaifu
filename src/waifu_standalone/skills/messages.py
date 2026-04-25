from __future__ import annotations

from typing import Any


_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "skill.disabled": "skill is disabled",
        "skill.invalid_manifest": "skill manifest is invalid or ineligible",
        "skill.unsupported_handler": "unsupported skill handler: {handler}",
        "skill.tool_not_registered": "tool is not registered: {tool_id}",
        "skill.tool_not_callable": "tool is not available for model invocation: {tool_id}",
        "skill.permission_denied": "skill is not authorized to call dangerous tool: {tool_id}",
        "skill.invalid_arguments": "skill arguments are invalid: {reason}",
        "skill.timeout": "skill timed out after {seconds}s",
        "skill.unhandled_exception": "skill execution failed: {error}",
        "skill.unbound": "{address}, `{skill_name}` is not an executable tool manifest, so I cannot pretend it ran. To force execution, set handler.type to `tool_id` and bind an available tool.",
        "skill.image_ready": "image is ready.",
        "skill.image_failed": "{address}, image generation failed: {reason}",
        "skill.image_failed_brief": "image generation failed, please try again later.",
        "skill.image_ack": "{address}, I will draw it first and send it when it is ready.",
        "skill.search_links_missing_with_query": "{address}, I only have the summary for the `{query}` search, not stable source links, so I will not invent them. You can ask me to search official or primary sources again.",
        "skill.search_links_missing": "{address}, I need a real search result before I can provide source links. Send me the query again and I will paste verified links.",
        "skill.search_links_intro": "{address}, here are the real links from this search.",
        "skill.search_links_query": "Query: {query}",
        "skill.search_missing_query": "{address}, tell me the keywords you want me to search.",
        "skill.search_no_stable_result": "{address}, I did not find a stable result this time. Want me to try another query?",
        "skill.search_done": "{address}, I searched it for you.",
        "skill.summary_insufficient_context": "{address}, there is not enough context to summarize yet.",
        "skill.summary_done": "{address}, here are the key points: {summary}",
        "skill.summary_unavailable": "{address}, I cannot summarize this conversation reliably yet.",
        "skill.summary_tags": "Tags: {tags}",
        "skill.list_empty": "{address}, I do not have any enabled skills right now.",
        "skill.list_intro": "{address}, my enabled skills are:",
        "skill.list_count_custom": "{total} skills total, including {workspace_count} custom skills.",
        "skill.list_count": "{total} skills total.",
        "skill.image_missing_prompt": "Please provide an image prompt.",
        "skill.image_success": "Generated image, prompt: {prompt}",
        "skill.search_tool_missing_query": "Please provide search keywords.",
        "skill.search_tool_no_result": "No stable result found for `{query}`.",
        "skill.tool_empty_result": "tool returned no content.",
        "skill.claw_success": "Executed through ClawRuntime.",
        "skill.claw_failed": "ClawRuntime could not execute this tool.",
    },
    "zh_CN": {
        "skill.disabled": "技能已禁用",
        "skill.invalid_manifest": "技能 Manifest 无效或当前不可用",
        "skill.unsupported_handler": "不支持的技能处理器：{handler}",
        "skill.tool_not_registered": "工具未注册：{tool_id}",
        "skill.tool_not_callable": "工具不可供模型调用：{tool_id}",
        "skill.permission_denied": "该技能未获准调用危险工具：{tool_id}",
        "skill.invalid_arguments": "技能参数无效：{reason}",
        "skill.timeout": "技能执行超过 {seconds}s 后超时",
        "skill.unhandled_exception": "技能执行失败：{error}",
        "skill.unbound": "{address}，`{skill_name}` 这个技能不是可执行工具型 manifest，我不能假装已经替你跑完。如果要强制执行，请把 handler.type 设为 `tool_id` 并绑定可用工具。",
        "skill.image_ready": "图片已经准备好了。",
        "skill.image_failed": "{address}，这次图没画成，原因是：{reason}",
        "skill.image_failed_brief": "呜，这次图片没有画好，稍后再试一次吧。",
        "skill.image_ack": "{address}，我先去画，画好就马上发你。",
        "skill.search_links_missing_with_query": "{address}，我这边只有“{query}”那次检索的摘要，没有拿到稳定的原文链接，不能给你乱编。你要的话我可以换个关键词再查一次官网或原始来源。",
        "skill.search_links_missing": "{address}，你要的是来源链接的话，我得先拿到一轮真实检索结果，现在手里没有可核对的链接，不能给你乱编。你把关键词再发我一次，我查到真实链接就直接贴给你。",
        "skill.search_links_intro": "{address}，我把这次检索里拿到的真实链接发你。",
        "skill.search_links_query": "关键词：{query}",
        "skill.search_missing_query": "{address}，你想让我查什么呀，把关键词直接告诉我就好。",
        "skill.search_no_stable_result": "{address}，这次我没查到稳定结果，要不要换个关键词让我再试一次？",
        "skill.search_done": "{address}，我帮你查了一下。",
        "skill.summary_insufficient_context": "{address}，现在还没有足够的上下文，我再多陪你聊几句就能帮你总结啦。",
        "skill.summary_done": "{address}，我先帮你收一下重点：{summary}",
        "skill.summary_unavailable": "{address}，这段对话我还没法总结得漂亮，你再给我一点上下文吧。",
        "skill.summary_tags": "标签：{tags}",
        "skill.list_empty": "{address}，我现在还没有任何已启用的技能。",
        "skill.list_intro": "{address}，我目前掌握的技能有：",
        "skill.list_count_custom": "共 {total} 个技能（其中 {workspace_count} 个是自定义技能）。",
        "skill.list_count": "共 {total} 个技能。",
        "skill.image_missing_prompt": "请提供要生成图片的提示词。",
        "skill.image_success": "已生成图片，prompt: {prompt}",
        "skill.search_tool_missing_query": "请提供要搜索的关键词。",
        "skill.search_tool_no_result": "没有查到“{query}”的稳定结果。",
        "skill.tool_empty_result": "工具没有返回内容。",
        "skill.claw_success": "已通过 ClawRuntime 执行。",
        "skill.claw_failed": "ClawRuntime 未能执行该工具。",
    },
}


def t(key: str, *, locale: str = "en", **params: Any) -> str:
    resolved_locale = locale if locale in _MESSAGES else "en"
    template = _MESSAGES.get(resolved_locale, {}).get(key) or _MESSAGES["en"].get(key) or key
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template
