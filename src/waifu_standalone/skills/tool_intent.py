"""Which built-in tools a chat message could need.

Sending every tool schema with a casual "你好" makes the model behave like a
tool-using assistant and costs tokens on every turn. A message gets a group of
tools only when its wording points at it; skills whose own trigger keywords
match are added by the caller.
"""

from __future__ import annotations

import re

NAMING_TOOL_IDS = ("set-preferred-name", "set-assistant-alias", "reject-third-party-naming", "clarify-naming-intent")
_FILE_TOOLS = ("read-file", "list-files", "write-file", "exec-command")

_INTENTS: tuple[tuple[tuple[str, ...], re.Pattern[str]], ...] = (
    # Self-introductions ("我是阿狗") and "以后就这么叫吧" count too.
    (NAMING_TOOL_IDS, re.compile(r"叫|喊我|喊你|称呼|名字|外号|昵称|改名|我是|call\s+me|call\s+you", re.I)),
    (
        ("weather",),
        re.compile(r"天气|气温|温度|下雨|下雪|带伞|降温|升温|冷不冷|热不热|刮风|台风|雾霾|空气质量|预报|weather", re.I),
    ),
    (
        ("search", "search-links"),
        re.compile(
            r"搜一?下|搜搜|搜索|查一?下|查查|帮我查|最新|新闻|热搜|发生了什么|怎么回事|价格|多少钱|股价|汇率|"
            r"比分|赛程|上映|发布会|官网|search|google|百度",
            re.I,
        ),
    ),
    (("summarize", "web-fetch"), re.compile(r"https?://|www\.|这篇|文章|链接|网页|帮我总结|总结这", re.I)),
    (("summary",), re.compile(r"总结一下|总结下|回顾一下|刚才聊了什么|我们聊了什么|聊天记录")),
    (("image",), re.compile(r"画一|画个|画张|帮我画|生成.{0,6}图|来张|来一张|发张|配图|头像|壁纸")),
    (("skill-list",), re.compile(r"你会什么|你会干什么|你能做什么|有什么技能|有哪些能力|你可以做什么|你都会啥|什么功能")),
    (_FILE_TOOLS, re.compile(r"文件|目录|文件夹|读取|执行|运行|命令|脚本|终端|shell", re.I)),
)


def relevant_tool_ids(text: str, *, has_images: bool = False) -> set[str]:
    """Built-in tool ids the message may need; empty for ordinary chat."""
    wanted: set[str] = set()
    normalized = str(text or "")
    for tool_ids, pattern in _INTENTS:
        if pattern.search(normalized):
            wanted.update(tool_ids)
    if has_images:
        wanted.add("image-caption")
    return wanted
