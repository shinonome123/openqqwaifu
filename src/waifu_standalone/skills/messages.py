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
    },
}


def t(key: str, *, locale: str = "en", **params: Any) -> str:
    resolved_locale = locale if locale in _MESSAGES else "en"
    template = _MESSAGES.get(resolved_locale, {}).get(key) or _MESSAGES["en"].get(key) or key
    try:
        return template.format(**params)
    except Exception:
        return template
