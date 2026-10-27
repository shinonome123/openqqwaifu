"""Durable, explicitly authorized one-shot reminders; all writes use RuntimeStore's transaction."""
from __future__ import annotations

import hashlib
import json
import uuid

from .contracts import json_text
from .inspection import page_limit, sequence_cursor
from .memories import source_text
from .reminder_time import resolve_time
from . import contact_policy


SCHEMA_V4 = (
    """CREATE TABLE reminder_tasks (
        id INTEGER PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, conversation_id TEXT NOT NULL REFERENCES conversations,
        character_id TEXT NOT NULL, character_version TEXT NOT NULL, card_json TEXT NOT NULL, binding_generation INTEGER NOT NULL,
        source_event_id TEXT NOT NULL REFERENCES inbox_events, evidence TEXT NOT NULL, authorization_kind TEXT NOT NULL, actor TEXT NOT NULL,
        text TEXT NOT NULL, due_at REAL NOT NULL, expires_at REAL NOT NULL, timezone TEXT NOT NULL, local_time TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'scheduled', reason TEXT NOT NULL DEFAULT '',
        cancel_requested INTEGER NOT NULL DEFAULT 0, linked_memory_id TEXT NOT NULL DEFAULT '', linked_memory_version INTEGER,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE reminder_occurrences (
        task_id TEXT NOT NULL REFERENCES reminder_tasks(task_id), task_version INTEGER NOT NULL,
        event_id TEXT NOT NULL UNIQUE REFERENCES inbox_events, PRIMARY KEY(task_id,task_version))""",
    """CREATE TABLE reminder_requests (
        conversation_id TEXT NOT NULL REFERENCES conversations, request_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, actor TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(conversation_id,request_id))""",
    "CREATE INDEX reminders_due ON reminder_tasks(state,due_at)",
    "CREATE INDEX reminders_scope ON reminder_tasks(conversation_id,id)",
)


def public_task(row):
    return {key: row[key] for key in row.keys() if key != "card_json"}


def task_for_event(db, event_id):
    return db.execute("SELECT t.*,r.task_version FROM reminder_occurrences r JOIN reminder_tasks t USING(task_id) WHERE r.event_id=?",
                      (event_id,)).fetchone()


def invalidate(db, task, state, reason, now):
    # Unknown/in-flight sends retain their review barrier. They cannot be
    # declared cancelled merely because the operator requested cancellation.
    db.execute("UPDATE outbox_messages SET state='cancelled',error_code=? WHERE state='pending' AND event_id IN "
               "(SELECT event_id FROM reminder_occurrences WHERE task_id=? AND task_version=?)", (reason, task["task_id"], task["version"]))
    db.execute("UPDATE turns SET state='cancelled' WHERE event_id IN (SELECT event_id FROM reminder_occurrences WHERE task_id=? AND task_version=?) "
               "AND state IN ('generating','planned','awaiting_delivery')", (task["task_id"], task["version"]))
    db.execute("UPDATE inbox_events SET state='cancelled',error_code=? WHERE event_id IN "
               "(SELECT event_id FROM reminder_occurrences WHERE task_id=? AND task_version=?) AND state IN ('queued','generating','outbox_ready')",
               (reason, task["task_id"], task["version"]))
    db.execute("UPDATE reminder_tasks SET state=?,reason=?,version=version+1,updated_at=? WHERE task_id=? AND version=?",
               (state, reason, now, task["task_id"], task["version"]))


def eligibility(db, task, enabled, now):
    if task["cancel_requested"]:
        return "cancelled", "cancellation_requested"
    if now > task["expires_at"]:
        return "expired", "missed_window"
    if not (enabled(task["conversation_id"]) if callable(enabled) else enabled):
        return "paused", "reminders_disabled"
    binding = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (task["conversation_id"],)).fetchone()
    if not binding or (binding["character_id"], binding["generation"]) != (task["character_id"], task["binding_generation"]):
        return "paused", "binding_changed"
    if task["linked_memory_id"]:
        for pending in db.execute("SELECT payload_json FROM inbox_events WHERE conversation_id=? AND kind='memory' AND state='queued'", (task["conversation_id"],)):
            if json.loads(pending[0]).get("change", {}).get("memory_id") == task["linked_memory_id"]:
                return "paused", "linked_episode_change_pending"
        memory = db.execute("SELECT * FROM memory_records WHERE memory_id=? AND conversation_id=? AND character_id=?",
                            (task["linked_memory_id"], task["conversation_id"], task["character_id"])).fetchone()
        if not memory or memory["state"] in {"withdrawn", "completed", "cancelled"}:
            return "cancelled", "linked_episode_closed"
        if memory["version"] != task["linked_memory_version"]:
            return "paused", "linked_episode_changed"
    return None


def contact_blocked(db, task, now):
    result = contact_policy.for_task(db, task, now)
    if result["allowed"]:
        if task["reason"].startswith("contact_"):
            db.execute("UPDATE reminder_tasks SET reason='',deferred_until=NULL WHERE task_id=?", (task["task_id"],))
        return False
    if task["state"] == "enqueued":
        # Retire the occurrence to free the ordered conversation queue. A later
        # window gets a new version; unknown/in-flight sends never enter here.
        invalidate(db, task, "scheduled", result["reason"], now)
    db.execute("UPDATE reminder_tasks SET reason=?,deferred_until=?,updated_at=? WHERE task_id=? AND "
               "(reason<>? OR deferred_until IS NOT ?)",
               (result["reason"], result["next_at"], now, task["task_id"], result["reason"], result["next_at"]))
    return True


def reconcile_contact(db, conversation_id, now):
    for task in db.execute("SELECT * FROM reminder_tasks WHERE conversation_id=? AND state IN ('scheduled','enqueued')",
                           (conversation_id,)).fetchall():
        if now > task["expires_at"]:
            invalidate(db, task, "expired", "missed_window", now)
        elif task["due_at"] <= now:
            contact_blocked(db, task, now)


def _linked_version(db, memory_id, conversation_id, character_id):
    if not memory_id:
        return None
    row = db.execute("SELECT * FROM memory_records WHERE memory_id=? AND conversation_id=? AND character_id=?",
                      (memory_id, conversation_id, character_id)).fetchone()
    if not row or row["kind"] != "episode" or row["state"] != "open":
        raise ValueError("关联经历必须属于此会话和角色，且尚未结束或撤回")
    return row["version"]


def apply_request(db, payload, *, actor, now, enabled, grace_seconds, default_zone, user=False, time_base=None):
    conversation_id, request_id = payload.get("conversation_id"), payload.get("request_id")
    if not all(isinstance(v, str) and 0 < len(v) <= 100 for v in (conversation_id, request_id, actor)):
        raise ValueError("conversation, request ID and authenticated actor are required")
    serialized = json_text(payload)
    previous = db.execute("SELECT * FROM reminder_requests WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
    if previous:
        if previous["payload_json"] != serialized or previous["actor"] != actor:
            raise ValueError("request_id was already used with different content")
        return {**json.loads(previous["response_json"]), "duplicate": True}
    operation = payload.get("operation")
    if operation not in {"create", "postpone", "cancel"}:
        raise ValueError("unsupported reminder operation")
    if operation != "cancel" and not enabled:
        raise ValueError("当前实例尚未启用提醒")
    if not user and (payload.get("authorized") is not True or not isinstance(payload.get("note"), str) or not payload["note"].strip()):
        raise ValueError("需要明确授权和操作依据")
    if len(str(payload.get("note", ""))) > 1000:
        raise ValueError("操作依据最多 1000 字")
    binding = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if binding is None:
        raise ValueError("unknown conversation")
    task = None
    if operation != "create":
        task = db.execute("SELECT * FROM reminder_tasks WHERE task_id=? AND conversation_id=?", (str(payload.get("task_id", "")), conversation_id)).fetchone()
        if task is None:
            raise ValueError("此会话中没有该提醒")
        if not user and (type(payload.get("expected_version")) is not int or payload["expected_version"] != task["version"]):
            raise ValueError("提醒版本已变化，请刷新后操作")
    if operation == "cancel":
        if task["state"] in {"completed", "cancelled"}:
            raise ValueError("该提醒已经结束")
        if task["state"] in {"dispatching", "needs_review"}:
            db.execute("UPDATE reminder_tasks SET cancel_requested=1,version=version+1,reason='cancellation_requested',updated_at=? WHERE task_id=?",
                       (now, task["task_id"]))
            status = "in_flight" if task["state"] == "dispatching" else "needs_review"
        else:
            invalidate(db, task, "cancelled", "cancelled_by_user" if user else "cancelled_by_admin", now)
            status = "cancelled"
    else:
        if task and task["state"] not in {"scheduled", "enqueued", "paused", "expired"}:
            raise ValueError("该提醒已发送、已取消或需要核对，不能直接延期")
        character_id = task["character_id"] if task else payload.get("character_id", binding["character_id"])
        if character_id != binding["character_id"]:
            raise ValueError("请先切回此提醒所属角色，再明确重新安排")
        p = contact_policy.policy(db, conversation_id)
        explicit_zone = payload.get("timezone", "")
        if not isinstance(explicit_zone, str):
            raise ValueError("提醒时区必须是 IANA 时区名称")
        requested_zone = explicit_zone or p["timezone"] or default_zone
        due, zone, local = resolve_time(payload.get("when"), requested_zone,
                                       time_base if user and time_base is not None else now, payload.get("fold"))
        # Postponing is a new time authorization; quiet exceptions must be
        # explicitly confirmed again, not inherited from the old deadline.
        allow_quiet = payload.get("allow_quiet", False)
        contact_policy.check_requested_time(db, conversation_id, due, requested_zone, allow_quiet)
        if due + grace_seconds < now:
            raise ValueError("收到命令后等待过久，提醒时间已超过补发窗口，请重新指定时间")
        memory_id = task["linked_memory_id"] if task else payload.get("linked_memory_id", "")
        if not isinstance(memory_id, str):
            raise ValueError("invalid linked episode")
        linked_version = _linked_version(db, memory_id, conversation_id, character_id)
        if task:
            invalidate(db, task, "scheduled", "rescheduled", now)
            db.execute("UPDATE reminder_tasks SET due_at=?,expires_at=?,timezone=?,local_time=?,binding_generation=?,"
                       "character_version=?,card_json=?,linked_memory_version=?,cancel_requested=0,reason='',allow_quiet=?,"
                       "timezone_confirmed=?,deferred_until=NULL WHERE task_id=?",
                (due, due + grace_seconds, zone, local, binding["generation"], binding["character_version"], binding["card_json"], linked_version,
                 allow_quiet, bool(requested_zone), task["task_id"]))
            status = "postponed"
        else:
            text, quote = payload.get("text"), payload.get("evidence")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500 or not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 1000:
                raise ValueError("提醒内容需为 1—500 字，并提供来源原文")
            _, texts = source_text(db, payload.get("source_event_id"), conversation_id, character_id, before_seq=binding["next_seq"])
            if not any(quote in part for part in texts):
                raise ValueError("授权依据必须逐字引用此角色处理过的用户消息")
            pending = db.execute("SELECT count(*) FROM reminder_tasks WHERE conversation_id=? AND state NOT IN ('completed','cancelled','expired')", (conversation_id,)).fetchone()[0]
            if pending >= 20:
                raise ValueError("每个私聊最多保留 20 条未结束提醒")
            task_id = hashlib.sha256((conversation_id + "\0" + request_id).encode()).hexdigest()[:16]
            db.execute("INSERT INTO reminder_tasks(task_id,conversation_id,character_id,character_version,card_json,binding_generation,"
                       "source_event_id,evidence,authorization_kind,actor,text,due_at,expires_at,timezone,local_time,"
                       "linked_memory_id,linked_memory_version,created_at,updated_at,allow_quiet,timezone_confirmed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, conversation_id, character_id, binding["character_version"], binding["card_json"], binding["generation"],
                 payload["source_event_id"], quote, "user_command" if user else "admin_confirmed", actor, text.strip(), due,
                 due + grace_seconds, zone, local, memory_id, linked_version, now, now, allow_quiet, bool(requested_zone)))
            task = {"task_id": task_id}
            status = "created"
    current = db.execute("SELECT * FROM reminder_tasks WHERE task_id=?", (task["task_id"],)).fetchone()
    response = {"status": status, "task": public_task(current), "duplicate": False}
    db.execute("INSERT INTO reminder_requests VALUES(?,?,?,?,?,?)", (conversation_id, request_id, serialized, actor, json_text(response), now))
    return response


def tick(db, *, now, enabled, max_pending, take_seq):
    tasks = db.execute("SELECT * FROM reminder_tasks WHERE state IN ('scheduled','enqueued') ORDER BY due_at,id").fetchall()
    pending = db.execute("SELECT count(*) FROM inbox_events WHERE state NOT IN ('completed','ignored','failed','cancelled')").fetchone()[0]
    active = {r[0] for r in db.execute("SELECT conversation_id FROM reminder_tasks WHERE state IN ('enqueued','dispatching','needs_review')")}
    made = 0
    for task in tasks:
        blocked = eligibility(db, task, enabled, now)
        if blocked:
            invalidate(db, task, *blocked, now)
            if task["state"] == "enqueued":
                active.discard(task["conversation_id"])
                pending -= 1
            continue
        if task["due_at"] <= now and contact_blocked(db, task, now):
            if task["state"] == "enqueued":
                active.discard(task["conversation_id"])
                pending -= 1
            continue
        if task["state"] != "scheduled" or task["due_at"] > now or task["conversation_id"] in active:
            continue
        if pending >= max_pending or made >= 32:
            continue
        event_id = uuid.uuid4().hex
        seq = take_seq(db, task["conversation_id"])
        db.execute("INSERT INTO inbox_events(event_id,conversation_id,seq,source_id,kind,payload_json,received_at) VALUES(?,?,?,?, 'reminder_due',?,?)",
            (event_id, task["conversation_id"], seq, f"reminder:{task['task_id']}:{task['version']}",
             json_text({"task_id": task["task_id"], "task_version": task["version"], "text": task["text"], "local_time": task["local_time"], "timezone": task["timezone"]}), now))
        db.execute("INSERT INTO reminder_occurrences VALUES(?,?,?)", (task["task_id"], task["version"], event_id))
        db.execute("UPDATE reminder_tasks SET state='enqueued',reason='',deferred_until=NULL,updated_at=? WHERE task_id=?", (now, task["task_id"]))
        active.add(task["conversation_id"])
        pending += 1
        made += 1
    return made


def prepare(db, row, *, now, enabled, config_version):
    task = task_for_event(db, row["event_id"])
    if task is None:
        raise RuntimeError("reminder occurrence is missing")
    if task["task_version"] != task["version"] or task["state"] != "enqueued":
        db.execute("UPDATE inbox_events SET state='cancelled',error_code='stale_reminder' WHERE event_id=?", (row["event_id"],))
        return None
    blocked = eligibility(db, task, enabled, now)
    if blocked:
        invalidate(db, task, *blocked, now)
        return None
    if contact_blocked(db, task, now):
        return None
    existing = db.execute("SELECT * FROM outbox_messages WHERE event_id=?", (row["event_id"],)).fetchone()
    if existing:
        return dict(existing)
    turn_id, outbox_id = uuid.uuid4().hex, uuid.uuid4().hex
    late = max(0, int(now - task["due_at"]))
    prefix = f"提醒（已延迟 {late // 60} 分钟）：" if late >= 60 else "提醒："
    payload = json_text({"launcher_id": row["conversation_id"], "launcher_type": "person", "text": prefix + task["text"], "images": []})
    db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,'awaiting_delivery',1)",
        (turn_id, row["event_id"], task["character_id"], task["character_version"], task["card_json"], task["binding_generation"], config_version))
    db.execute("INSERT INTO outbox_messages(outbox_id,event_id,turn_id,payload_json) VALUES(?,?,?,?)", (outbox_id, row["event_id"], turn_id, payload))
    db.execute("UPDATE inbox_events SET state='outbox_ready' WHERE event_id=?", (row["event_id"],))
    return dict(db.execute("SELECT * FROM outbox_messages WHERE outbox_id=?", (outbox_id,)).fetchone())


def before_dispatch(db, outbox, *, now, enabled):
    task = task_for_event(db, outbox["event_id"])
    if task is None:
        return True
    if task["task_version"] != task["version"] or task["state"] != "enqueued":
        return False
    blocked = eligibility(db, task, enabled, now)
    if blocked:
        invalidate(db, task, *blocked, now)
        return False
    if contact_blocked(db, task, now):
        return False
    db.execute("UPDATE reminder_tasks SET state='dispatching',updated_at=? WHERE task_id=?", (now, task["task_id"]))
    return True


def delivery_result(db, event_id, status, now):
    state = {"accepted": "completed", "captured": "completed", "unknown": "needs_review", "rejected": "needs_review", "cancelled": "cancelled", "pending": "enqueued"}[status]
    db.execute("UPDATE reminder_tasks SET state=?,updated_at=? WHERE task_id IN (SELECT task_id FROM reminder_occurrences WHERE event_id=?)",
               (state, now, event_id))


def list_tasks(db, conversation_id, *, limit=30, before_id=None, task_id=""):
    limit, cursor = page_limit(limit), sequence_cursor(before_id)
    if not db.execute("SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone():
        raise LookupError("unknown conversation")
    if task_id:
        task = db.execute("SELECT * FROM reminder_tasks WHERE task_id=? AND conversation_id=?", (task_id, conversation_id)).fetchone()
        if not task:
            raise LookupError("unknown reminder in this conversation")
        requests = []
        for row in db.execute("SELECT * FROM reminder_requests WHERE conversation_id=? ORDER BY created_at DESC", (conversation_id,)):
            result = json.loads(row["response_json"])
            if result.get("task", {}).get("task_id") == task_id:
                request = json.loads(row["payload_json"])
                requests.append({"actor": row["actor"], "created_at": row["created_at"], "operation": request.get("operation"),
                                 "note": request.get("note", ""), "state": result["task"]["state"], "local_time": result["task"]["local_time"]})
                if len(requests) == 100:
                    break
        occurrences = [dict(r) for r in db.execute("SELECT r.task_version,r.event_id,e.state,e.error_code,o.outbox_id,o.state AS delivery_state "
            "FROM reminder_occurrences r JOIN inbox_events e USING(event_id) LEFT JOIN outbox_messages o USING(event_id) WHERE r.task_id=? ORDER BY r.task_version DESC", (task_id,))]
        return {"task": public_task(task), "requests": requests, "occurrences": occurrences}
    rows = db.execute("SELECT * FROM reminder_tasks WHERE conversation_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                       (conversation_id, cursor, cursor, limit + 1)).fetchall()
    return {"tasks": [public_task(row) for row in rows[:limit]], "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None}
