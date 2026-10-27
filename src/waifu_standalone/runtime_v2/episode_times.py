"""Source-backed episode time proposals and explicit, versioned confirmation."""
from __future__ import annotations

import hashlib
import json

from .contracts import json_text
from .episode_time_parser import confirm_value, extract


SCHEMA_V9 = (
    """CREATE TABLE episode_times (
        timing_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations, character_id TEXT NOT NULL,
        memory_id TEXT NOT NULL REFERENCES memory_records(memory_id), memory_version INTEGER NOT NULL,
        source_event_id TEXT NOT NULL REFERENCES inbox_events, source_received_at REAL NOT NULL,
        timezone TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'proposed',
        reason TEXT NOT NULL DEFAULT '', proposal_json TEXT NOT NULL, confirmed_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(memory_id,memory_version))""",
    """CREATE TABLE episode_time_requests (
        conversation_id TEXT NOT NULL REFERENCES conversations, request_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, actor TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(conversation_id,request_id))""",
    "CREATE INDEX episode_times_scope ON episode_times(conversation_id,memory_id,memory_version)",
)


def public(row):
    if row is None:
        return None
    result = {key: row[key] for key in row.keys() if key not in {"proposal_json", "confirmed_json"}}
    result["proposal"] = json.loads(row["proposal_json"])
    result["confirmed"] = json.loads(row["confirmed_json"])
    return result


def current(db, memory_id, memory_version):
    return db.execute("SELECT * FROM episode_times WHERE memory_id=? AND memory_version=?", (memory_id, memory_version)).fetchone()


def version_for(db, memory_id, memory_version):
    row = current(db, memory_id, memory_version)
    return row["version"] if row else 0


def context_value(db, memory_id, memory_version):
    row = current(db, memory_id, memory_version)
    if not row:
        return None
    # Unconfirmed calendar guesses are deliberately absent from model context.
    result = {"timing_id": row["timing_id"], "version": row["version"], "state": row["state"], "reason": row["reason"]}
    if row["state"] == "confirmed":
        result["value"] = json.loads(row["confirmed_json"])
        result["provenance"] = result["value"].get("provenance", "admin_confirmed_from_user_source")
    else:
        result["requires_clarification"] = True
    return result


def reset_followups(db, memory_id, memory_version, now):
    from . import followups
    rows = db.execute("SELECT * FROM followup_candidates WHERE memory_id=? AND memory_version=? "
        "AND state IN ('candidate','scheduled','enqueued','dispatching','needs_review')", (memory_id, memory_version)).fetchall()
    for row in rows:
        followups.invalidate(db, row, "candidate", "episode_time_changed", now)
        if row["state"] not in {"dispatching", "needs_review"}:
            db.execute("UPDATE followup_candidates SET due_at=NULL,expires_at=NULL,timezone='',local_time='',"
                "character_version='',card_json='{}',binding_generation=NULL WHERE candidate_id=?", (row["candidate_id"],))


def sync_record(db, memory, now):
    cid, mid, revision = memory["conversation_id"], memory["memory_id"], memory["version"]
    db.execute("UPDATE episode_times SET state='invalidated',reason='episode_changed',confirmed_json='{}',version=version+1,updated_at=? "
        "WHERE memory_id=? AND state<>'invalidated' AND (memory_version<>? OR ?<>'open')", (now, mid, revision, memory["state"]))
    if memory["kind"] != "episode" or memory["state"] != "open":
        return
    source = db.execute("SELECT received_at FROM inbox_events WHERE event_id=?", (memory["source_event_id"],)).fetchone()
    policy = db.execute("SELECT timezone FROM contact_policies WHERE conversation_id=?", (cid,)).fetchone()
    zone = policy[0] if policy else ""
    old = current(db, mid, revision)
    if old and old["timezone"] == zone:
        return
    proposal = extract(memory["quote"], source[0], zone)
    if old:
        if old["state"] == "confirmed":
            reset_followups(db, mid, revision, now)
        db.execute("UPDATE episode_times SET version=version+1,state='proposed',reason='timezone_changed',timezone=?,"
            "proposal_json=?,confirmed_json='{}',updated_at=? WHERE timing_id=?", (zone, json_text(proposal), now, old["timing_id"]))
    else:
        key = hashlib.sha256(f"episode-time:{mid}:{revision}".encode()).hexdigest()[:24]
        db.execute("INSERT INTO episode_times(timing_id,conversation_id,character_id,memory_id,memory_version,source_event_id,"
            "source_received_at,timezone,proposal_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (key, cid, memory["character_id"], mid, revision, memory["source_event_id"], source[0], zone, json_text(proposal), now, now))


def sync(db, now, conversation_id=None):
    # Bounded catch-up for migrated records; normal revisions are synchronized
    # in their original transaction. Read paths may also synchronize their scope.
    rows = db.execute("SELECT m.* FROM memory_records m JOIN conversations c USING(conversation_id) "
        "LEFT JOIN contact_policies p USING(conversation_id) LEFT JOIN episode_times t ON t.memory_id=m.memory_id AND t.memory_version=m.version "
        "WHERE c.privacy_state='active' AND m.kind='episode' AND m.state='open' AND (? IS NULL OR m.conversation_id=?) "
        "AND (t.timing_id IS NULL OR t.timezone<>coalesce(p.timezone,'')) ORDER BY m.id DESC LIMIT 32", (conversation_id, conversation_id)).fetchall()
    for row in rows:
        sync_record(db, row, now)


def request(db, payload, actor, now, enabled):
    from .memories import source_text
    cid, rid = payload.get("conversation_id"), payload.get("request_id")
    if not all(isinstance(value, str) and 0 < len(value) <= 100 for value in (cid, rid, actor)):
        raise ValueError("conversation, request ID and authenticated actor are required")
    serialized = json_text(payload)
    previous = db.execute("SELECT * FROM episode_time_requests WHERE conversation_id=? AND request_id=?", (cid, rid)).fetchone()
    if previous:
        if previous["payload_json"] != serialized or previous["actor"] != actor:
            raise ValueError("request_id was already used with different content")
        return {**json.loads(previous["response_json"]), "duplicate": True}
    operation = payload.get("operation")
    if operation not in ("confirm", "clear") or payload.get("authorized") is not True:
        raise ValueError("请明确确认此次经历时间操作")
    note = payload.get("note")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
        raise ValueError("请填写 1—1000 字时间核对依据")
    c = db.execute("SELECT * FROM conversations WHERE conversation_id=? AND privacy_state='active'", (cid,)).fetchone()
    memory = db.execute("SELECT * FROM memory_records WHERE memory_id=? AND conversation_id=? AND kind='episode' AND state='open'",
        (str(payload.get("memory_id", "")), cid)).fetchone()
    if not c or not memory or not enabled:
        raise ValueError("此私聊或未结束经历当前不可操作")
    if memory["character_id"] != c["character_id"]:
        raise ValueError("只能确认当前绑定角色的经历时间")
    sync_record(db, memory, now)
    timing = current(db, memory["memory_id"], memory["version"])
    policy = db.execute("SELECT * FROM contact_policies WHERE conversation_id=?", (cid,)).fetchone()
    for key, value in (("expected_memory_version", memory["version"]), ("expected_timing_version", timing["version"]),
                       ("expected_data_generation", c["data_generation"]), ("expected_policy_version", policy["version"]),
                       ("expected_binding_generation", c["generation"]), ("expected_next_seq", c["next_seq"])):
        if type(payload.get(key)) is not int or payload[key] != value:
            raise ValueError("经历、会话、角色或时区已变化，请刷新并重新核对时间")
    if db.execute("SELECT 1 FROM inbox_events WHERE conversation_id=? AND kind IN ('message','memory','binding') "
                  "AND state IN ('queued','generating','outbox_ready','needs_review') LIMIT 1", (cid,)).fetchone():
        raise ValueError("请先处理当前会话消息、修订或角色切换，再核对经历时间")
    confirmed = {}
    if operation == "confirm":
        confirmed = confirm_value(payload, policy["timezone"])
        source_id, quote = payload.get("confirmation_source_event_id"), payload.get("confirmation_quote")
        if not isinstance(source_id, str) or not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 500:
            raise ValueError("请提供确认时间所依据的用户消息及逐字引文")
        source, texts = source_text(db, source_id, cid, memory["character_id"], before_seq=c["next_seq"] - 1)
        if not any(quote in text for text in texts):
            raise ValueError("时间确认引文必须来自此会话此角色的用户原文")
        confirmed.update(confirmation_source_event_id=source_id, confirmation_quote=quote, source_received_at=source["received_at"])
    return save_confirmation(db, memory, timing, confirmed, payload, actor, now)


def save_confirmation(db, memory, timing, confirmed, payload, actor, now):
    """Commit a prevalidated admin or user confirmation and invalidate dependents."""
    state = "confirmed" if confirmed else "cleared"
    db.execute("UPDATE episode_times SET version=version+1,state=?,reason='',confirmed_json=?,updated_at=? WHERE timing_id=?",
        (state, json_text(confirmed), now, timing["timing_id"]))
    reset_followups(db, memory["memory_id"], memory["version"], now)
    response = {"status": "saved", "timing": public(current(db, memory["memory_id"], memory["version"])), "duplicate": False}
    db.execute("INSERT INTO episode_time_requests VALUES(?,?,?,?,?,?)",
        (payload["conversation_id"], payload["request_id"], json_text(payload), actor, json_text(response), now))
    return response


def audit(db, conversation_id, memory_id):
    rows = db.execute("SELECT * FROM episode_time_requests WHERE conversation_id=? ORDER BY rowid DESC", (conversation_id,))
    result = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("memory_id") == memory_id:
            result.append({"actor": row["actor"], "created_at": row["created_at"], "note": payload["note"],
                           "operation": payload["operation"], "timing": json.loads(row["response_json"])["timing"],
                           "approval_event_id": payload.get("approval_event_id", ""), "approval_quote": payload.get("approval_quote", "")})
            if len(result) >= 30:
                break
    return result
