"""Durable private-chat time review. Only explicit, delivered questions accept answers.

The model may create an episode, but cannot authorize its time or choose a target.
All functions run inside RuntimeStore transactions; no network calls occur here.
"""
from __future__ import annotations

import json
import re

from . import episode_times
from .contracts import json_text
from .episode_time_parser import DATE, CLOCK, PERIOD, VAGUE, extract, confirm_value
from .memories import source_text


SCHEMA_V10 = (
    """CREATE TABLE episode_time_dialogues (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations, state_json TEXT NOT NULL,
        prompt_event_id TEXT NOT NULL REFERENCES inbox_events, prompt_seq INTEGER NOT NULL,
        min_reply_seq INTEGER, expires_at REAL NOT NULL)""",
    """CREATE TABLE episode_time_dialogue_turns (
        event_id TEXT PRIMARY KEY REFERENCES inbox_events, conversation_id TEXT NOT NULL REFERENCES conversations,
        response_text TEXT, created_at REAL NOT NULL)""",
    "CREATE INDEX episode_time_dialogue_scope ON episode_time_dialogue_turns(conversation_id)",
)
TTL = 30 * 60
AUTO_REVIEW_COOLDOWN = 30 * 60
# A bounded courtesy guard for automatic questions, not a natural-language
# consent parser. Explicit time-review commands always remain available.
LISTENING_BOUNDARY = re.compile(r"只想(?:吐槽|聊聊|说说|倾诉)|(?:先|只)(?:听我说|听我讲)"
    r"|(?:别|不要|不用|不想)(?:再)?(?:帮我|替我|帮忙)?(?:追问|问|核对|确认)"
    r"|(?:时间|日期)(?:先|暂时)?(?:不说|不聊|别问)|(?:先|暂时)?不聊(?:时间|日期)")
CONFIRM = {"确认", "确认时间", "确认这个时间", "对，就是这个时间"}
CANCEL = {"取消", "取消时间确认", "先不确认", "不记了", "算了", "先不聊这个"}
NEGATIVE = {"不对", "不是", "错了", "时间不对"}
START = {"核对经历时间", "确认经历时间", "修改经历时间", "/episode-time"}
CLEAR = {"清除经历时间", "/episode-time clear"}
PERIOD_NAMES = {"dawn": "凌晨", "morning": "上午", "noon": "中午", "afternoon": "下午", "evening": "傍晚", "night": "晚上"}
STALE = "刚才的时间核对已失效，这次没有保存。想继续的话，可以说“核对经历时间”重新开始。"


def clean(text):
    return text.strip().rstrip("。！!").strip()


def _text(event):
    # Forwarded messages, quoted replies, images and mixed segments cannot act
    # as confirmation. Ordinary processing still receives the original event.
    if len(event.segments) != 1 or event.segments[0].kind != "text" or len(event.segments[0].text) > 500:
        return None
    return clean("".join(p.text for p in event.segments))


def _label(option, zone):
    precision = option["precision"]
    suffix = ("（只确定日期）" if precision == "date" else " " + PERIOD_NAMES[option["period"]]
              if precision == "period" else " " + option["clock"])
    if option.get("fold") is not None:
        suffix += "（重复钟点的" + ("第一次" if option["fold"] == 0 else "第二次") + "）"
    return option["date"] + suffix + "，时区 " + zone


def _scope(db, cid):
    c = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    p = db.execute("SELECT * FROM contact_policies WHERE conversation_id=?", (cid,)).fetchone()
    return {"character_id": c["character_id"], "binding_generation": c["generation"],
            "data_generation": c["data_generation"], "policy_version": p["version"], "timezone": p["timezone"]}


def _records(db, context):
    return db.execute("SELECT * FROM memory_records WHERE conversation_id=? AND character_id=? "
        "AND kind='episode' AND state='open' ORDER BY id DESC LIMIT 21",
        (context.binding.conversation_id, context.binding.character.character_id)).fetchall()


def _target(db, memory, now):
    episode_times.sync_record(db, memory, now)
    t = episode_times.current(db, memory["memory_id"], memory["version"])
    return {"memory_id": memory["memory_id"], "memory_version": memory["version"],
            "timing_version": t["version"], "title": memory["title"]}


def _valid_target(db, state, target, cid):
    m = db.execute("SELECT * FROM memory_records WHERE memory_id=? AND conversation_id=? AND character_id=? "
        "AND kind='episode' AND state='open' AND version=?",
        (target["memory_id"], cid, state["character_id"], target["memory_version"])).fetchone()
    if not m:
        return None
    t = episode_times.current(db, m["memory_id"], m["version"])
    return m if t and t["version"] == target["timing_version"] else None


def _put(db, context, state, now):
    db.execute("INSERT INTO episode_time_dialogues VALUES(?,?,?,?,NULL,?) ON CONFLICT(conversation_id) DO UPDATE SET "
        "state_json=excluded.state_json,prompt_event_id=excluded.prompt_event_id,prompt_seq=excluded.prompt_seq,"
        "min_reply_seq=NULL,expires_at=excluded.expires_at",
        (context.binding.conversation_id, json_text(state), context.event_id, context.conversation_seq, now + TTL))


def _drop(db, cid):
    db.execute("DELETE FROM episode_time_dialogues WHERE conversation_id=?", (cid,))


def delivered(db, event_id):
    # Record an admission watermark, not a wall-clock comparison. Answers
    # queued before the send receipt cannot approve a question not yet shown.
    db.execute("UPDATE episode_time_dialogues SET min_reply_seq=(SELECT next_seq FROM conversations c "
        "WHERE c.conversation_id=episode_time_dialogues.conversation_id) WHERE prompt_event_id=? AND min_reply_seq IS NULL",
        (event_id,))


def _review(state):
    state["stage"] = "confirm"
    return (f'我理解的是「{state["target"]["title"]}」：{_label(state["options"][0], state["timezone"])}。'
            '对的话请回复“确认时间”；不对可以直接说新的日期和时段，也可以说“取消”。'
            '这里只确认经历时间，不会安排提醒或跟进发送。')


def _proposal(state, proposal, source_id, quote):
    state.update(source_event_id=source_id, source_quote=quote, options=[])
    state.pop("selection_event_id", None)
    state.pop("selection_quote", None)
    # Ambiguous dates/clocks can be offered for explicit selection. Vague,
    # conditional or contradictory assertions cannot become a confirm button.
    allowed = {"weekday_ambiguous", "year_missing", "clock_ambiguous", "dst_fold"}
    if set(proposal["reasons"]) <= allowed:
        state["options"] = proposal["options"]
    if len(state["options"]) == 1:
        return _review(state)
    state["stage"] = "time"
    if state["options"]:
        options = "\n".join(f'{i}. {_label(o, state["timezone"])}' for i, o in enumerate(state["options"], 1))
        return f'「{state["target"]["title"]}」的时间有几种解释：\n{options}\n请回复选项编号，或直接说完整日期和时段；也可以说“取消”。'
    return f'「{state["target"]["title"]}」具体是哪一天？只确定日期或上午、下午也可以，比如“明天下午”；还没定可以说“取消”。'


def _select(db, state, target, cid):
    memory = _valid_target(db, state, target, cid)
    if not memory:
        return None
    state["target"] = target
    if state["operation"] == "clear":
        state["stage"] = "clear"
        return f'要清除「{target["title"]}」已确认的时间吗？请回复“确认清除”，或说“取消”。经历本身会保留。'
    timing = episode_times.current(db, memory["memory_id"], memory["version"])
    # A correction of a confirmed time starts blank, never reuses an old guess.
    if timing["state"] == "confirmed":
        state.update(stage="time", options=[])
        return f'「{target["title"]}」要改成哪一天、哪个时段？可以直接说“明天下午”，也可以说“取消”。'
    return _proposal(state, json.loads(timing["proposal_json"]), memory["source_event_id"], memory["quote"])


def _start(db, context, now, *, operation="confirm", targets=None):
    cid = context.binding.conversation_id
    _drop(db, cid)
    state = {**_scope(db, cid), "operation": operation}
    if not state["timezone"]:
        return "先告诉我这次私聊使用的时区，例如 /contact timezone Asia/Shanghai，然后再说“核对经历时间”。"
    targets = targets if targets is not None else [_target(db, m, now) for m in _records(db, context)]
    if not targets:
        return "当前角色还没有可核对的未结束经历。先聊聊这件事，保存为经历后再来核对时间。"
    if len(targets) > 20:
        return "未结束的经历较多，请说“核对〈经历标题〉的时间”指定一件，或在会话记录里选择。"
    state["targets"] = targets
    if len(targets) == 1:
        answer = _select(db, state, targets[0], cid)
    else:
        state["stage"] = "select"
        answer = "想核对哪件事？\n" + "\n".join(f'{i}. {t["title"]}' for i, t in enumerate(targets, 1)) + "\n请回复编号，也可以说“取消”。"
    if answer is not None:
        _put(db, context, state, now)
    return answer or STALE


def _time_input(text):
    value = re.sub(r"^(?:时间)?(?:改成|改为|改到|就是|是在|是|在)", "", text).strip()
    # Match the whole utterance; a date mentioned in a different request must
    # never be silently consumed as this episode's answer.
    rest = value
    for pattern in (DATE, CLOCK, PERIOD, VAGUE):
        rest = pattern.sub("", rest)
    if value and not re.sub(r"[\s,，的]", "", rest):
        return value
    return None


def _save(db, context, state, now, text):
    cid = context.binding.conversation_id
    memory = _valid_target(db, state, state["target"], cid)
    c = db.execute("SELECT next_seq FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    if not memory or c[0] != context.conversation_seq + 1:
        return STALE
    # A later pending revision must win over an answer to an older question.
    if db.execute("SELECT 1 FROM inbox_events WHERE conversation_id=? AND kind IN ('memory','binding') "
                  "AND state IN ('queued','generating','outbox_ready','needs_review')", (cid,)).fetchone():
        return STALE
    confirmed = {}
    if state["operation"] == "confirm":
        source, texts = source_text(db, state["source_event_id"], cid, state["character_id"], before_seq=context.conversation_seq)
        if not any(state["source_quote"] in part for part in texts):
            return STALE
        confirmed = confirm_value(state["options"][0], state["timezone"])
        confirmed.update(confirmation_source_event_id=state["source_event_id"], confirmation_quote=state["source_quote"],
            source_received_at=source["received_at"], provenance="user_confirmed_in_dialogue",
            approval_event_id=context.event_id, approval_quote=text)
        if state.get("selection_event_id"):
            confirmed.update(selection_event_id=state["selection_event_id"], selection_quote=state["selection_quote"])
    timing = episode_times.current(db, memory["memory_id"], memory["version"])
    payload = {"conversation_id": cid, "request_id": "dialogue:" + context.event_id, "memory_id": memory["memory_id"],
        "operation": state["operation"], "note": "用户在私聊中核对复述并明确确认", "dialogue": state,
        "approval_event_id": context.event_id, "approval_quote": text}
    episode_times.save_confirmation(db, memory, timing, confirmed, payload, "user:" + cid, now)
    return (f'已清除「{memory["title"]}」的时间确认。' if not confirmed else
            f'已确认「{memory["title"]}」的时间：{_label(confirmed, state["timezone"])}。') + "没有安排提醒或跟进发送。"


def handle(db, context, event, now, enabled):
    cid = context.binding.conversation_id
    cached = db.execute("SELECT response_text FROM episode_time_dialogue_turns WHERE event_id=?", (context.event_id,)).fetchone()
    if cached:
        return cached[0]
    text = _text(event)
    pending = db.execute("SELECT * FROM episode_time_dialogues WHERE conversation_id=?", (cid,)).fetchone()
    answer = None
    touched = bool(pending)
    if not enabled or text is None:
        _drop(db, cid)
    elif text in START | CLEAR:
        touched = True
        answer = _start(db, context, now, operation="clear" if text in CLEAR else "confirm")
    elif (match := re.fullmatch(r"(?:核对|确认|修改|清除)(.{1,80}?)的时间", text)):
        touched = True
        # Exact title only; duplicates require an explicit numbered selection.
        rows = db.execute("SELECT * FROM memory_records WHERE conversation_id=? AND character_id=? AND title=? "
            "AND kind='episode' AND state='open' ORDER BY id DESC LIMIT 21", (cid, context.binding.character.character_id, match[1])).fetchall()
        answer = _start(db, context, now, operation="clear" if text.startswith("清除") else "confirm",
                        targets=[_target(db, m, now) for m in rows])
    elif (match := re.fullmatch(r"(?:把)?(.{1,80}?)的时间(?:改成|改为|改到)(.{1,100})", text)):
        touched = True
        rows = db.execute("SELECT * FROM memory_records WHERE conversation_id=? AND character_id=? AND title=? "
            "AND kind='episode' AND state='open' ORDER BY id DESC LIMIT 21", (cid, context.binding.character.character_id, match[1])).fetchall()
        answer = _start(db, context, now, targets=[_target(db, m, now) for m in rows])
        value = _time_input(match[2])
        if len(rows) == 1 and value is not None:
            opened = db.execute("SELECT state_json FROM episode_time_dialogues WHERE conversation_id=?", (cid,)).fetchone()
            if opened:  # A missing timezone must remain a timezone request.
                state = json.loads(opened[0])
                source = db.execute("SELECT received_at FROM inbox_events WHERE event_id=?", (context.event_id,)).fetchone()
                answer = _proposal(state, extract(value, source[0], state["timezone"]), context.event_id, value)
                _put(db, context, state, now)
    elif pending:
        state = json.loads(pending["state_json"])
        valid = (now < pending["expires_at"] and pending["min_reply_seq"] is not None
                 and context.conversation_seq >= pending["min_reply_seq"]
                 and context.conversation_seq == pending["prompt_seq"] + 1
                 and all(state[k] == v for k, v in _scope(db, cid).items()))
        if "target" in state:
            valid = valid and bool(_valid_target(db, state, state["target"], cid))
        if not valid:
            _drop(db, cid)
            if text in CONFIRM | {"确认清除"} or text.isdecimal() or _time_input(text) is not None:
                answer = STALE
        elif text in CANCEL:
            _drop(db, cid)
            answer = "好，停止这次时间核对，原有记录没有改变。"
        elif (state["stage"] == "clear" and text == "确认清除") or (state["stage"] == "confirm" and text in CONFIRM):
            answer = _save(db, context, state, now, text)
            _drop(db, cid)
        elif state["stage"] == "select" and text.isdecimal() and len(text) <= 2 and 1 <= int(text) <= len(state["targets"]):
            answer = _select(db, state, state["targets"][int(text) - 1], cid)
            if answer is None:
                _drop(db, cid)
                answer = STALE
            else:
                _put(db, context, state, now)
        elif state["stage"] in {"time", "confirm"}:
            value = _time_input(text)
            if state["stage"] == "time" and text.isdecimal() and len(text) <= 2 and 1 <= int(text) <= len(state.get("options", [])):
                state["options"] = [state["options"][int(text) - 1]]
                state.update(selection_event_id=context.event_id, selection_quote=text)
                answer = _review(state)
            elif value is not None:
                source = db.execute("SELECT received_at FROM inbox_events WHERE event_id=?", (context.event_id,)).fetchone()
                answer = _proposal(state, extract(value, source[0], state["timezone"]), context.event_id, value)
            elif text in NEGATIVE:
                state.update(stage="time", options=[])
                answer = "那请重新说一下日期和时段，比如“后天下午”。这次还没有保存。"
            elif text in CONFIRM:
                answer = "还没有确定要保存的时间，请先回复选项编号或明确日期。"
            if answer:
                _put(db, context, state, now)
            else:
                _drop(db, cid)  # Topic change: normal reply, no hidden pending yes/no.
        else:
            _drop(db, cid)
    elif text in {"确认时间", "确认这个时间", "确认清除"}:
        touched = True
        answer = "目前没有正在核对的经历时间，没有保存。可以先说“核对经历时间”。"
    if touched:
        db.execute("INSERT INTO episode_time_dialogue_turns VALUES(?,?,?,?)", (context.event_id, cid, answer, now))
    return answer


def offer_after_extraction(db, context, records, now):
    """Ask once, in the reply to a single newly saved, time-bearing episode."""
    if db.execute("SELECT 1 FROM episode_time_dialogue_turns WHERE event_id=?", (context.event_id,)).fetchone():
        return None
    cid = context.binding.conversation_id
    if db.execute("SELECT 1 FROM episode_time_dialogue_turns WHERE conversation_id=? "
                  "AND response_text IS NOT NULL AND created_at>=? LIMIT 1", (cid, now - AUTO_REVIEW_COOLDOWN)).fetchone():
        return None
    # Inspect actual recent user messages, including the whole current message;
    # a model's shorter evidence quote can omit a request to just listen.
    recent = db.execute("SELECT payload_json FROM inbox_events WHERE conversation_id=? AND kind='message' "
        "AND seq<=? AND received_at>=? ORDER BY seq DESC LIMIT 8", (cid, context.conversation_seq, now - AUTO_REVIEW_COOLDOWN))
    for row in recent:
        payload = json.loads(row[0])
        if any(LISTENING_BOUNDARY.search(p.get("text", "")) for p in payload.get("segments", []) if p.get("kind") == "text"):
            return None
    episodes = [r for r in records if r["kind"] == "episode" and r["state"] == "open"]
    if len(episodes) != 1 or not _scope(db, context.binding.conversation_id)["timezone"]:
        return None
    memory = episodes[0]
    t = episode_times.current(db, memory["memory_id"], memory["version"])
    if not json.loads(t["proposal_json"])["mentions"]:
        return None
    answer = _start(db, context, now, targets=[_target(db, memory, now)])
    db.execute("INSERT INTO episode_time_dialogue_turns VALUES(?,?,?,?)",
        (context.event_id, context.binding.conversation_id, answer, now))
    return answer
