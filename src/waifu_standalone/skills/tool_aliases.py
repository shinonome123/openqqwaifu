from __future__ import annotations

import re

CANONICAL_TOOL_IDS: set[str] = {
    "image",
    "image-caption",
    "list-files",
    "read-file",
    "search",
    "search-links",
    "skill-list",
    "summary",
    "summarize",
    "weather",
    "web-fetch",
    "write-file",
    "exec-command",
}


OPENCLAW_TOOL_ALIASES: dict[str, str] = {
    "web_search": "search",
    "internet_search": "search",
    "search_web": "search",
    "web_lookup": "search",
    "lookup_web": "search",
    "browser_search": "search",
    "image_generate": "image",
    "generate_image": "image",
    "create_image": "image",
    "draw_image": "image",
    "text_to_image": "image",
    "image_caption": "image-caption",
    "image_handoff": "image-caption",
    "deliver_image": "image-caption",
    "image_delivery": "image-caption",
    "handoff_image": "image-caption",
    "read": "read-file",
    "read_file": "read-file",
    "file_read": "read-file",
    "open_file": "read-file",
    "cat_file": "read-file",
    "read_text_file": "read-file",
    "list_files": "list-files",
    "ls": "list-files",
    "dir": "list-files",
    "file_list": "list-files",
    "find_files": "list-files",
    "tree": "list-files",
    "web_fetch": "web-fetch",
    "fetch_url": "web-fetch",
    "fetch": "web-fetch",
    "open_url": "web-fetch",
    "read_url": "web-fetch",
    "fetch_page": "web-fetch",
    "fetch_webpage": "web-fetch",
    "http_get": "web-fetch",
    "write": "write-file",
    "write_file": "write-file",
    "save_file": "write-file",
    "update_file": "write-file",
    "append_file": "write-file",
    "exec": "exec-command",
    "run_command": "exec-command",
    "run": "exec-command",
    "run_process": "exec-command",
    "execute_process": "exec-command",
    "conversation_summary": "summary",
    "chat_summary": "summary",
    "summarize_conversation": "summary",
    "skill_list": "skill-list",
    "skills": "skill-list",
    "abilities": "skill-list",
    "help_tools": "skill-list",
    "summarize_url": "summarize",
    "summarize_file": "summarize",
    "content_summary": "summarize",
    "webpage_summary": "summarize",
    "search_sources": "search-links",
    "search_links": "search-links",
    "sources": "search-links",
    "links": "search-links",
    "source_links": "search-links",
    "references": "search-links",
    "citations": "search-links",
    "related_links": "search-links",
    "forecast": "weather",
    "weather_lookup": "weather",
    "weather_check": "weather",
    "weather_forecast": "weather",
}


SELF_HOSTED_TOOL_IDS: set[str] = set(CANONICAL_TOOL_IDS)

SELF_HOSTED_TOOL_TRIGGERS: dict[str, tuple[str, ...]] = {
    "weather": ("weather", "forecast", "天气", "天气预报", "查天气"),
}


def normalize_tool_like_name(value: str) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
    if not normalized or not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", normalized):
        return ""
    return normalized


def resolve_compatible_tool_id(*values: str) -> str:
    for raw in values:
        normalized = normalize_tool_like_name(raw)
        if not normalized:
            continue
        candidate = normalized
        seen: set[str] = set()
        while candidate and candidate not in seen:
            seen.add(candidate)
            if candidate in CANONICAL_TOOL_IDS:
                return candidate
            candidate = OPENCLAW_TOOL_ALIASES.get(candidate, "")
    return ""
