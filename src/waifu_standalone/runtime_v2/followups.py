"""Source-backed episode follow-ups. Candidates need a reviewed time and draft."""
from __future__ import annotations

import hashlib
import json
import uuid

from . import contact_policy
from .contracts import json_text
from .inspection import page_limit, sequence_cursor
from .reminder_time import resolve_time


SCHEMA_V7 = (
    """CREATE TABLE followup_candidates (
        id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL REFERENCES conversations, character_id TEXT NOT NULL,
        memory_id TEXT NOT NULL REFERENCES memory_records(memory_id), memory_version INTEGER NOT NULL,
        source_event_id TEXT NOT NULL REFERENCES inbox_events, title TEXT NOT NULL, evidence TEXT NOT NULL,
        text TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'candidate',
        reason TEXT NOT NULL DEFAULT 'needs_time_confirmation', cancel_requested INTEGER NOT NULL DEFAULT 0,
        character_version TEXT NOT NULL DEFAULT '', card_json TEXT NOT NULL DEFAULT '{}', binding_generation INTEGER,
        due_at REAL, expires_at REAL, timezone TEXT NOT NULL DEFAULT '', local_time TEXT NOT NULL DEFAULT '',
        deferred_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(memory_id,memory_version))""",
    """CREATE TABLE followup_occurrences (
        candidate_id TEXT NOT NULL REFERENCES followup_candidates(candidate_id), candidate_version INTEGER NOT NULL,
        event_id TEXT NOT NULL UNIQUE REFERENCES inbox_events, PRIMARY KEY(candidate_id,candidate_version))""",
    """CREATE TABLE followup_requests (
        conversation_id TEXT NOT NULL REFERENCES conversations, request_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, actor TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(conversation_id,request_id))""",
    "CREATE INDEX followups_scope ON followup_candidates(conversation_id,id)",
    "CREATE INDEX followups_due ON followup_candidates(state,due_at)",
)

WINDOW_SECONDS = 2 * 3600
RECENT_SECONDS = 30 * 60
SOURCE_MAX_AGE = 30 * 86400
ACTIVE = ("candidate", "scheduled", "enqueued", "dispatching", "needs_review")


def public(row):
    return {key: row[key] for key in row.keys() if key != "card_json"}


def for_event(db, event_id):
    return db.execute("SELECT f.*,o.candidate_version FROM followup_occurrences o JOIN followup_candidates f USING(candidate_id) WHERE o.event_id=?",
                      (event_id,)).fetchone()


def invalidate(db, task, state, reason, now):
    if task["state"] in {"dispatching", "needs_review"}:
        if not task["cancel_requested"]:
            db.execute("UPDATE followup_candidates SET cancel_requested=1,version=version+1,reason=?,updated_at=? WHERE candidate_id=?",
                       (reason, now, task["candidate_id"]))
        return
    params = (task["candidate_id"], task["version"])
    events = "SELECT event_id FROM followup_occurrences WHERE candidate_id=? AND candidate_version=?"
    db.execute(f"UPDATE outbox_messages SET state='cancelled',error_code=? WHERE state='pending' AND event_id IN ({events})", (reason, *params))
    db.execute(f"UPDATE turns SET state='cancelled' WHERE state IN ('generating','planned','awaiting_delivery') AND event_id IN ({events})", params)
    db.execute(f"UPDATE inbox_events SET state='cancelled',error_code=? WHERE state IN ('queued','generating','outbox_ready') AND event_id IN ({events})", (reason, *params))
    db.execute("UPDATE followup_candidates SET state=?,reason=?,version=version+1,deferred_until=NULL,updated_at=? WHERE candidate_id=? AND version=?",
               (state, reason, now, *params))


def intrinsic(db, task, now, enabled=True):
    if task["cancel_requested"]:
        return "cancelled", "cancellation_requested"
    binding = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (task["conversation_id"],)).fetchone()
    if not binding or binding["privacy_state"] != "active":
        return "cancelled", "conversation_erased"
    memory = db.execute("SELECT * FROM memory_records WHERE memory_id=? AND conversation_id=? AND character_id=?",
                        (task["memory_id"], task["conversation_id"], task["character_id"])).fetchone()
    if not memory or memory["state"] != "open":
        return "invalidated", "episode_closed"
    if memory["version"] != task["memory_version"]:
        return "invalidated", "episode_changed"
    source = db.execute("SELECT received_at FROM inbox_events WHERE event_id=?", (task["source_event_id"],)).fetchone()
    if not source or now > source[0] + SOURCE_MAX_AGE:
        return "expired", "source_too_old"
    if task["state"] == "candidate":
        return None
    if task["expires_at"] is not None and now > task["expires_at"]:
        return "expired", "missed_window"
    if not (enabled(task["conversation_id"]) if callable(enabled) else enabled):
        return "cancelled", "runtime_disabled"
    if not contact_policy.policy(db, task["conversation_id"])["proactive_enabled"]:
        return "cancelled", "authorization_revoked"
    if (binding["character_id"], binding["generation"]) != (task["character_id"], task["binding_generation"]):
        return "invalidated", "binding_changed"
    return None


def decision(db, conversation_id, now):
    result = contact_policy.decision(db, conversation_id, now, kind="followup")
    if not result["allowed"]:
        return result
    attempts = db.execute("SELECT started_at FROM contact_attempts WHERE conversation_id=? AND kind='followup' AND started_at>? ORDER BY started_at",
                          (conversation_id, now - 86400)).fetchall()
    if attempts:
        return {**result, "allowed": False, "reason": "followup_daily_limit", "next_at": attempts[-1][0] + 86400}
    latest = db.execute("SELECT coalesce(max(seq),0),max(received_at) FROM inbox_events WHERE conversation_id=? AND kind='message'", (conversation_id,)).fetchone()
    unanswered = db.execute("SELECT count(DISTINCT a.outbox_id) FROM contact_attempts a JOIN outbox_messages o USING(outbox_id) "
        "JOIN inbox_events e USING(event_id) WHERE a.conversation_id=? AND a.kind='followup' AND e.seq>?", (conversation_id, latest[0])).fetchone()[0]
    if unanswered >= 2:
        return {**result, "allowed": False, "reason": "awaiting_interaction", "next_at": None}
    if latest[1] is not None and now < latest[1] + RECENT_SECONDS:
        return {**result, "allowed": False, "reason": "recent_interaction", "next_at": latest[1] + RECENT_SECONDS}
    return result


def task_decision(db, task, now):
    # New user input must be processed before revisiting an episode, including
    # input admitted after an occurrence already reached the queue head.
    pending = db.execute("SELECT 1 FROM inbox_events WHERE conversation_id=? AND kind IN ('message','binding','memory') "
        "AND state IN ('queued','generating','outbox_ready','needs_review') LIMIT 1", (task["conversation_id"],)).fetchone()
    if pending:
        return {"allowed": False, "reason": "conversation_busy", "next_at": None}
    return decision(db, task["conversation_id"], now)


def defer(db, task, result, now):
    if task["state"] == "enqueued":
        invalidate(db, task, "scheduled", result["reason"], now)
    db.execute("UPDATE followup_candidates SET reason=?,deferred_until=?,updated_at=? WHERE candidate_id=? "
               "AND (reason<>? OR deferred_until IS NOT ?)",
               (result["reason"], result["next_at"], now, task["candidate_id"], result["reason"], result["next_at"]))


def reconcile(db, now, enabled=True, conversation_id=None):
    rows = db.execute("SELECT * FROM followup_candidates WHERE state IN ('candidate','scheduled','enqueued','dispatching','needs_review') "
                      "AND (? IS NULL OR conversation_id=?)", (conversation_id, conversation_id)).fetchall()
    for task in rows:
        blocked = intrinsic(db, task, now, enabled)
        if blocked:
            invalidate(db, task, *blocked, now)
        elif task["state"] in {"scheduled", "enqueued"} and task["due_at"] <= now:
            result = task_decision(db, task, now)
            if not result["allowed"]:
                defer(db, task, result, now)


def discover(db, now):
    # Discovery is deterministic and makes no model calls. No clock is inferred
    # from prose; candidates stay unscheduled until an operator verifies time.
    rows = db.execute("SELECT m.* FROM memory_records m JOIN conversations c USING(conversation_id) "
        "JOIN inbox_events e ON e.event_id=m.source_event_id WHERE m.kind='episode' AND m.state='open' AND c.privacy_state='active' "
        "AND e.received_at>=? AND NOT EXISTS (SELECT 1 FROM followup_candidates f WHERE f.memory_id=m.memory_id "
        "AND (f.memory_version=m.version OR f.reason='dismissed_by_admin' OR f.state IN ('dismissed','completed','dispatching','needs_review'))) "
        "AND (SELECT count(*) FROM followup_candidates f WHERE f.conversation_id=m.conversation_id "
        "AND f.state IN ('candidate','scheduled','enqueued','dispatching','needs_review'))<20 ORDER BY m.last_seq DESC LIMIT 32",
        (now - SOURCE_MAX_AGE,)).fetchall()
    for row in rows:
        count = db.execute("SELECT count(*) FROM followup_candidates WHERE conversation_id=? "
            "AND state IN ('candidate','scheduled','enqueued','dispatching','needs_review')", (row["conversation_id"],)).fetchone()[0]
        if count >= 20:
            continue
        candidate_id = hashlib.sha256(f"followup:{row['memory_id']}:{row['version']}".encode()).hexdigest()[:24]
        draft = f"之前你提到“{row['title']}”，最近怎么样了？如果暂时不想聊，也没关系。"
        db.execute("INSERT OR IGNORE INTO followup_candidates(candidate_id,conversation_id,character_id,memory_id,memory_version,"
            "source_event_id,title,evidence,text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, row["conversation_id"], row["character_id"], row["memory_id"], row["version"], row["source_event_id"],
             row["title"], row["quote"], draft, now, now))


def apply_request(db, payload, *, actor, now, enabled=True):
    cid, request_id = payload.get("conversation_id"), payload.get("request_id")
    if not all(isinstance(v, str) and 0 < len(v) <= 100 for v in (cid, request_id, actor)):
        raise ValueError("conversation, request ID and authenticated actor are required")
    serialized = json_text(payload)
    old = db.execute("SELECT * FROM followup_requests WHERE conversation_id=? AND request_id=?", (cid, request_id)).fetchone()
    if old:
        if old["payload_json"] != serialized or old["actor"] != actor:
            raise ValueError("request_id was already used with different content")
        return {**json.loads(old["response_json"]), "duplicate": True}
    if db.execute("SELECT 1 FROM followup_drafts WHERE conversation_id=? AND request_id=?", (cid, request_id)).fetchone():
        raise ValueError("request_id was already used for draft generation")
    operation = payload.get("operation")
    if operation not in ("schedule", "dismiss", "cancel"):
        raise ValueError("unsupported follow-up operation")
    if payload.get("authorized") is not True or not isinstance(payload.get("note"), str) or not 1 <= len(payload["note"].strip()) <= 1000:
        raise ValueError("请确认用户授权并填写 1—1000 字操作依据")
    binding = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    if not binding or binding["privacy_state"] != "active":
        raise ValueError("unknown active conversation")
    if type(payload.get("expected_data_generation")) is not int or payload["expected_data_generation"] != binding["data_generation"]:
        raise ValueError("会话数据代次已变化，请刷新后操作")
    task = db.execute("SELECT * FROM followup_candidates WHERE candidate_id=? AND conversation_id=?",
                      (str(payload.get("candidate_id", "")), cid)).fetchone()
    if not task:
        raise ValueError("此会话中没有该跟进候选")
    if type(payload.get("expected_version")) is not int or payload["expected_version"] != task["version"]:
        raise ValueError("跟进候选版本已变化，请刷新后操作")
    if operation in {"cancel", "dismiss"}:
        if task["state"] not in ACTIVE:
            raise ValueError("该候选已结束")
        invalidate(db, task, "dismissed" if operation == "dismiss" else "cancelled", "dismissed_by_admin" if operation == "dismiss" else "cancelled_by_admin", now)
    else:
        if payload.get("draft_job_id"):
            from .followup_drafts import checked_ready
            checked_ready(db, str(payload["draft_job_id"]), task["candidate_id"], now, enabled)
        if task["state"] not in {"candidate", "scheduled", "enqueued", "expired"}:
            raise ValueError("此跟进已结束、失效或需要核对，不能重新安排")
        # For rescheduling, validate source independently of the old deadline.
        source_check = intrinsic(db, {**dict(task), "state": "candidate"}, now, enabled)
        if source_check:
            raise ValueError("经历已变化、结束或来源过旧，不能安排此候选")
        p = contact_policy.policy(db, cid)
        if type(payload.get("expected_policy_version")) is not int or payload["expected_policy_version"] != p["version"]:
            raise ValueError("联系规则或时区已变化，请刷新并重新核对")
        if not enabled or not p["proactive_enabled"] or p["paused"]:
            raise ValueError("需先开启此私聊的主动跟进授权并解除暂停")
        if task["character_id"] != binding["character_id"]:
            raise ValueError("只能安排当前绑定角色的跟进")
        if db.execute("SELECT 1 FROM followup_candidates WHERE memory_id=? AND candidate_id<>? "
                      "AND (reason='dismissed_by_admin' OR state IN ('scheduled','enqueued','dispatching','needs_review','completed','dismissed'))",
                      (task["memory_id"], task["candidate_id"])).fetchone():
            raise ValueError("该经历已有跟进或已被忽略，不重复安排")
        if db.execute("SELECT 1 FROM inbox_events WHERE conversation_id=? AND kind IN ('memory','binding') AND state='queued'", (cid,)).fetchone():
            raise ValueError("有待处理的经历修订或角色切换，请处理后刷新")
        text = payload.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            raise ValueError("请核对并提供 1—500 字的跟进正文")
        due, zone, local = resolve_time(payload.get("when"), p["timezone"], now, payload.get("fold"))
        if due > now + 7 * 86400:
            raise ValueError("本阶段跟进只能安排在未来 7 天内")
        contact_policy.check_requested_time(db, cid, due, zone, False)
        invalidate(db, task, "scheduled", "", now)
        db.execute("UPDATE followup_candidates SET text=?,due_at=?,expires_at=?,timezone=?,local_time=?,"
            "character_version=?,card_json=?,binding_generation=? WHERE candidate_id=?",
            (text.strip(), due, due + WINDOW_SECONDS, zone, local, binding["character_version"], binding["card_json"], binding["generation"], task["candidate_id"]))
        if payload.get("draft_job_id"):
            db.execute("UPDATE followup_drafts SET state='used',updated_at=? WHERE job_id=?", (now, payload["draft_job_id"]))
    current = db.execute("SELECT * FROM followup_candidates WHERE candidate_id=?", (task["candidate_id"],)).fetchone()
    response = {"status": "needs_review" if current["state"] in {"dispatching", "needs_review"} else "saved", "candidate": public(current), "duplicate": False}
    db.execute("INSERT INTO followup_requests VALUES(?,?,?,?,?,?)", (cid, request_id, serialized, actor, json_text(response), now))
    return response


def tick(db, *, now, enabled, max_pending, take_seq):
    reconcile(db, now, enabled)
    discover(db, now)
    pending = db.execute("SELECT count(*) FROM inbox_events WHERE state NOT IN ('completed','ignored','failed','cancelled')").fetchone()[0]
    made = 0
    for task in db.execute("SELECT * FROM followup_candidates WHERE state='scheduled' AND due_at<=? ORDER BY due_at,id", (now,)).fetchall():
        if pending >= max_pending or made >= 32:
            break
        if intrinsic(db, task, now, enabled):
            continue
        # Only one active scheduled send per conversation; explicit reminders
        # get first chance in the scheduler and remain independently authorized.
        active = db.execute("SELECT 1 FROM inbox_events WHERE conversation_id=? AND state NOT IN ('completed','ignored','failed','cancelled') LIMIT 1",
                            (task["conversation_id"],)).fetchone()
        result = task_decision(db, task, now)
        if active or not result["allowed"]:
            defer(db, task, {"reason": "conversation_busy", "next_at": None} if active else result, now)
            continue
        event_id, seq = uuid.uuid4().hex, take_seq(db, task["conversation_id"])
        db.execute("INSERT INTO inbox_events(event_id,conversation_id,seq,source_id,kind,payload_json,received_at) VALUES(?,?,?,?, 'followup_due',?,?)",
            (event_id, task["conversation_id"], seq, f"followup:{task['candidate_id']}:{task['version']}",
             json_text({"candidate_id": task["candidate_id"], "memory_id": task["memory_id"], "memory_version": task["memory_version"],
                        "title": task["title"], "local_time": task["local_time"], "timezone": task["timezone"]}), now))
        db.execute("INSERT INTO followup_occurrences VALUES(?,?,?)", (task["candidate_id"], task["version"], event_id))
        db.execute("UPDATE followup_candidates SET state='enqueued',reason='',deferred_until=NULL,updated_at=? WHERE candidate_id=?", (now, task["candidate_id"]))
        pending += 1
        made += 1
    return made


def ready(db, task, now, enabled):
    if task["candidate_version"] != task["version"] or task["state"] != "enqueued":
        return False
    blocked = intrinsic(db, task, now, enabled)
    if blocked:
        invalidate(db, task, *blocked, now)
        return False
    result = task_decision(db, task, now)
    if not result["allowed"]:
        defer(db, task, result, now)
        return False
    return True


def prepare(db, row, *, now, enabled, config_version):
    task = for_event(db, row["event_id"])
    if task is None:
        raise RuntimeError("follow-up occurrence is missing")
    sync_episode_time(db, task, now)
    task = for_event(db, row["event_id"])
    if not ready(db, task, now, enabled):
        db.execute("UPDATE inbox_events SET state='cancelled',error_code='stale_followup' WHERE event_id=? AND state IN ('queued','outbox_ready')", (row["event_id"],))
        return None
    existing = db.execute("SELECT * FROM outbox_messages WHERE event_id=?", (row["event_id"],)).fetchone()
    if existing:
        return dict(existing)
    turn_id, outbox_id = uuid.uuid4().hex, uuid.uuid4().hex
    db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,'awaiting_delivery',1)", (turn_id, row["event_id"], task["character_id"],
        task["character_version"], task["card_json"], task["binding_generation"], config_version))
    db.execute("INSERT INTO outbox_messages(outbox_id,event_id,turn_id,payload_json) VALUES(?,?,?,?)", (outbox_id, row["event_id"], turn_id,
        json_text({"launcher_id": row["conversation_id"], "launcher_type": "person", "text": task["text"], "images": []})))
    db.execute("UPDATE inbox_events SET state='outbox_ready' WHERE event_id=?", (row["event_id"],))
    return dict(db.execute("SELECT * FROM outbox_messages WHERE outbox_id=?", (outbox_id,)).fetchone())


def before_dispatch(db, outbox, *, now, enabled):
    task = for_event(db, outbox["event_id"])
    if task is None:
        return True
    sync_episode_time(db, task, now)
    task = for_event(db, outbox["event_id"])
    if not ready(db, task, now, enabled):
        return False
    db.execute("UPDATE followup_candidates SET state='dispatching',updated_at=? WHERE candidate_id=?", (now, task["candidate_id"]))
    return True


def sync_episode_time(db, task, now):
    from .episode_times import sync_record
    memory = db.execute("SELECT * FROM memory_records WHERE memory_id=?", (task["memory_id"],)).fetchone()
    if memory:
        sync_record(db, memory, now)


def delivery_result(db, event_id, status, now):
    state = {"accepted": "completed", "captured": "completed", "unknown": "needs_review", "rejected": "needs_review", "cancelled": "cancelled", "pending": "enqueued"}[status]
    db.execute("UPDATE followup_candidates SET state=?,updated_at=? WHERE candidate_id IN (SELECT candidate_id FROM followup_occurrences WHERE event_id=?)",
               (state, now, event_id))


def list_candidates(db, conversation_id, *, now, limit=30, before_id=None, candidate_id=""):
    limit, cursor = page_limit(limit), sequence_cursor(before_id)
    binding = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not binding:
        raise LookupError("unknown conversation")
    extra = {"data_generation": binding["data_generation"], "binding_generation": binding["generation"], "next_seq": binding["next_seq"], "window_seconds": WINDOW_SECONDS,
             "decision": decision(db, conversation_id, now) if binding["privacy_state"] == "active" else None}
    if binding["privacy_state"] == "active":
        p = contact_policy.policy(db, conversation_id)
        extra.update(timezone=p["timezone"], proactive_enabled=p["proactive_enabled"], paused=p["paused"], policy_version=p["version"])
    if candidate_id:
        row = db.execute("SELECT * FROM followup_candidates WHERE conversation_id=? AND candidate_id=?", (conversation_id, candidate_id)).fetchone()
        if not row:
            raise LookupError("unknown follow-up in this conversation")
        requests = []
        for r in db.execute("SELECT * FROM followup_requests WHERE conversation_id=? ORDER BY rowid DESC", (conversation_id,)):
            payload = json.loads(r["payload_json"])
            if payload.get("candidate_id") == candidate_id:
                requests.append({"actor": r["actor"], "created_at": r["created_at"], "operation": payload["operation"], "note": payload["note"],
                                 "candidate": json.loads(r["response_json"])["candidate"], "draft_job_id": payload.get("draft_job_id", "")})
                if len(requests) >= 100:
                    break
        occurrences = [dict(r) for r in db.execute("SELECT f.candidate_version,f.event_id,e.state,o.outbox_id,o.state AS delivery_state FROM followup_occurrences f "
            "JOIN inbox_events e USING(event_id) LEFT JOIN outbox_messages o USING(event_id) WHERE candidate_id=? ORDER BY candidate_version DESC", (candidate_id,))]
        return {**extra, "candidate": public(row), "requests": requests, "occurrences": occurrences}
    rows = db.execute("SELECT * FROM followup_candidates WHERE conversation_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                      (conversation_id, cursor, cursor, limit + 1)).fetchall()
    return {**extra, "candidates": [public(r) for r in rows[:limit]], "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None}
