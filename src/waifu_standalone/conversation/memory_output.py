from __future__ import annotations

import json

from ..runtime_v2.memories import InvalidMemoryChange, validate_change


RESPONSE_CONTRACT = """本轮输出一个 JSON 对象，且仅包含 reply（回复正文字符串）和 memories（0—3 条记忆提案数组）。不要使用 Markdown 代码块。
普通闲聊、问题、假设、引用、玩笑、图片内容和角色虚构设定不提取。仅提取当前私聊用户在本轮正文中明确陈述的稳定偏好或具体事件；不推断诊断、身份目录或其他人的资料。
每条提案必须恰好包含：op、memory_id、expected_version、kind、title、text、quote、state。
op 为 create 或 revise。create 使用 memory_id=""、expected_version=0；revise 必须使用可见记忆的原 ID 与版本，不修改 kind。只有明确纠正或同一件事的后续才可修订，不因相似就覆盖；不明确时询问，不提出修订。
kind 为 fact（偏好或事实）或 episode（经历/安排）。fact 的 state 只能 active；episode 为 open/completed/cancelled。
title 最多 80 字，text 为最多 500 字的简短解释，quote 必须是本轮用户正文中逐字连续摘取的非空证据（最多 500 字），不能引用旧历史或自己的回复。
来源是用户的报告，不代表客观核实。保留相对时间的原话，不能自行把明天/周四换成确定日期。记忆里有 source_received_at 可供说明时间来源。
admin_confirmed 表示人工确认，模型不得提出覆盖它的修订；用户本轮的纠正仍优先用于当前回复，并可提示人工复核。
无法确认修订目标或没有值得保存的内容时，memories=[]。reply 只包含要发给用户的自然回复，不暴露 JSON/内部 ID，不声称执行提醒或主动联系。
例如：{"reply":"好，周四。","memories":[]}"""


def parse_response(raw, event, allowed_revisions):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidMemoryChange("duplicate fields in structured reply")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(InvalidMemoryChange("non-finite JSON value")))
    except (ValueError, TypeError) as exc:
        raise InvalidMemoryChange("model did not return a valid structured reply") from exc
    if not isinstance(value, dict) or set(value) != {"reply", "memories"}:
        raise InvalidMemoryChange("structured reply must contain only reply and memories")
    if not isinstance(value["reply"], str) or not value["reply"].strip():
        raise InvalidMemoryChange("structured reply has no reply text")
    if not isinstance(value["memories"], list) or len(value["memories"]) > 3:
        raise InvalidMemoryChange("memories must be an array of at most three proposals")
    changes, seen = [], set()
    for item in value["memories"]:
        change = validate_change(item, model=True)
        if not any(change["quote"] in p.text for p in event.segments if p.kind == "text"):
            raise InvalidMemoryChange("evidence must be quoted from current user text")
        if change["op"] == "revise":
            key = (change["memory_id"], change["expected_version"])
            if key not in allowed_revisions or change["memory_id"] in seen:
                raise InvalidMemoryChange("revision must refer to one visible memory version once")
            seen.add(change["memory_id"])
        changes.append(change)
    return value["reply"].strip(), tuple(changes)
