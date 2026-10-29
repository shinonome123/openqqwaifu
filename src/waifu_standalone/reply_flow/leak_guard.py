"""Keeping what only the model should see out of what she says.

A reply goes out as the model wrote it. A model that recites its instructions ("[规则]
…"), names a tool ("search-command"), or pastes a tool's raw output in a code block
would show the group what is behind her. ``scrub`` drops every line of a reply that
does: a line with an internal marker, or one that repeats a long run of the system
prompt word for word. What is left goes out; if nothing is, she says nothing.
"""

from __future__ import annotations

import re

# Section headers of her system prompt and turn hints, tool ids, tool-call vocabulary,
# and the code fence tool output was once pasted in.
_MARKERS = re.compile(
    r"\[(?:人设|擅长|背景|规则|关于对方|当前情况|工具|联网提示|接着刚才的话|这次是你自己想插话|酒馆模式|海龟汤|表情包|记忆|技能|你主动开口|到点了)\]"
    r"|\b[a-z]+(?:-[a-z]+)*-command\b|display_mode|raw_block|tool_calls?\b|tool_call_id|function_call"
    r"|system prompt|系统提示词|提示词里|工具调用已结束|以下是真实执行结果|```"
    # A search result's timestamp ("网易 2026-06-14 00:00:00") and search-links' "query:" line.
    r"|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|^\s*query:",
    re.IGNORECASE,
)
# A run of her instructions this long, said word for word, is the prompt leaking.
_ECHO_CHARS = 16
_IGNORED = re.compile(r"\s+")
_SECTION = re.compile(r"^\[([^\[\]\n]{1,16})\]\s*$", re.MULTILINE)
# Sections that hold facts she is meant to use (about the person, memories, the time), not instructions.
_FACT_SECTIONS = {"关于对方", "当前情况", "记忆", "相关记忆", "资料", "知识", "群成员"}


def scrub(text: str, system_prompt: str = "") -> str:
    """``text`` without the lines that show her instructions, tools or raw tool output."""
    raw = str(text or "")
    if not raw.strip():
        return raw
    prompt = _IGNORED.sub("", _instructions(str(system_prompt or "")))
    kept: list[str] = []
    inside_fence = False
    for line in raw.split("\n"):
        if line.strip().startswith("```"):
            inside_fence = not inside_fence
            continue
        if inside_fence or _MARKERS.search(line) or _echoes(line, prompt):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def leaks(text: str, system_prompt: str = "") -> bool:
    return scrub(text, system_prompt) != str(text or "").strip()


def _echoes(line: str, prompt: str) -> bool:
    compact = _IGNORED.sub("", line)
    if not prompt or len(compact) < _ECHO_CHARS:
        return False
    step = max(1, _ECHO_CHARS // 2)
    return any(compact[start:start + _ECHO_CHARS] in prompt for start in range(0, len(compact) - _ECHO_CHARS + 1, step))


def _instructions(system_prompt: str) -> str:
    """The system prompt without the sections of facts she may repeat (what she knows of the person)."""
    parts: list[str] = []
    position = 0
    keep = True
    for match in _SECTION.finditer(system_prompt):
        if keep:
            parts.append(system_prompt[position:match.start()])
        keep = match.group(1).strip() not in _FACT_SECTIONS
        position = match.start()
    if keep:
        parts.append(system_prompt[position:])
    return "".join(parts)
