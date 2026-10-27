"""Scoped, source-backed memory revisions in the runtime's single transaction."""
from __future__ import annotations

import hashlib
import json
import re
import time

from .contracts import json_text
from .inspection import page_limit, sequence_cursor


SCHEMA_V3 = (
    """CREATE TABLE memory_records (
        id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL REFERENCES conversations, character_id TEXT NOT NULL,
        kind TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
        title TEXT NOT NULL, text TEXT NOT NULL, source_event_id TEXT NOT NULL REFERENCES inbox_events,
        quote TEXT NOT NULL, provenance TEXT NOT NULL, last_seq INTEGER NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE memory_revisions (
        memory_id TEXT NOT NULL REFERENCES memory_records(memory_id), version INTEGER NOT NULL,
        change_event_id TEXT NOT NULL REFERENCES inbox_events,
        record_json TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(memory_id,version))""",
    "CREATE INDEX memory_scope ON memory_records(conversation_id,character_id,id)",
    "CREATE TABLE turn_memory_snapshots (turn_id TEXT PRIMARY KEY REFERENCES turns,snapshot_json TEXT NOT NULL)",
)


class InvalidMemoryChange(ValueError):
    pass


def _scoped_record(db, row):
    return {**dict(row), "bot_account_id": db.execute("SELECT value FROM runtime_meta WHERE key='account_id'").fetchone()[0],
            "scope": "private"}


def validate_change(change: dict, *, model: bool = False) -> dict:
    keys = {"op", "memory_id", "expected_version", "kind", "title", "text", "quote", "state"}
    if not isinstance(change, dict) or set(change) != keys:
        raise InvalidMemoryChange("memory change has missing or unknown fields")
    if any(not isinstance(change[k], str) for k in keys - {"expected_version"}):
        raise InvalidMemoryChange("memory fields must be strings")
    if change["op"] not in ({"create", "revise"} if model else {"create", "revise", "withdraw"}):
        raise InvalidMemoryChange("unsupported memory operation")
    if type(change["expected_version"]) is not int or change["expected_version"] < 0:
        raise InvalidMemoryChange("expected_version must be a nonnegative integer")
    if change["op"] == "create":
        if change["memory_id"] or change["expected_version"] != 0:
            raise InvalidMemoryChange("new memories must not identify an existing revision")
    elif not change["memory_id"] or change["expected_version"] < 1:
        raise InvalidMemoryChange("revision target and version are required")
    if len(change["memory_id"]) > 64 or change["kind"] not in {"fact", "episode"}:
        raise InvalidMemoryChange("invalid memory identity or kind")
    valid_states = {"active"} if change["kind"] == "fact" else {"open", "completed", "cancelled"}
    if change["op"] == "withdraw":
        valid_states = {"withdrawn"}
    if change["state"] not in valid_states:
        raise InvalidMemoryChange("invalid memory state")
    for key, limit in (("title", 80), ("text", 500), ("quote", 500)):
        if not change[key].strip() or len(change[key]) > limit:
            raise InvalidMemoryChange(f"{key} must contain 1 to {limit} characters")
    return dict(change)


def source_text(db, source_id, conversation_id, character_id, *, before_seq):
    source = db.execute("SELECT e.*,t.character_id FROM inbox_events e JOIN turns t USING(event_id) "
        "WHERE e.event_id=? AND e.conversation_id=? AND t.character_id=? AND e.kind='message' AND e.seq<=?",
        (source_id, conversation_id, character_id, before_seq)).fetchone()
    if source is None:
        raise InvalidMemoryChange("memory source must be a processed user message in this character and conversation")
    payload = json.loads(source["payload_json"])
    if payload.get("sender_id") != conversation_id or payload.get("launcher_type") != "person":
        raise InvalidMemoryChange("memory source must belong to the private user")
    # Quotes, nicknames, mentions and images are never promoted to identity facts.
    texts = [part.get("text", "") for part in payload.get("segments", []) if part.get("kind") == "text"]
    return source, texts


def apply_change(db, *, conversation_id, character_id, event_id, seq, source_id, change,
                 actor, note, model=False, index=0):
    change = validate_change(change, model=model)
    source, texts = source_text(db, source_id, conversation_id, character_id, before_seq=seq)
    if not any(change["quote"] in part for part in texts):
        raise InvalidMemoryChange("memory evidence must be an exact quote from its source message")
    old = None
    if change["op"] != "create":
        old = db.execute("SELECT * FROM memory_records WHERE memory_id=? AND conversation_id=? AND character_id=?",
            (change["memory_id"], conversation_id, character_id)).fetchone()
        if not old or old["version"] != change["expected_version"] or old["state"] == "withdrawn":
            raise InvalidMemoryChange("memory revision changed or is no longer available")
        if old["kind"] != change["kind"]:
            raise InvalidMemoryChange("a memory revision cannot change its kind")
        # Automatic extraction never silently overwrites an operator's correction.
        if model and old["provenance"] == "admin_confirmed":
            raise InvalidMemoryChange("an administrator-confirmed memory requires manual revision")
    now = time.time()
    memory_id = old["memory_id"] if old else hashlib.sha256(f"{event_id}:{index}".encode()).hexdigest()[:32]
    version = old["version"] + 1 if old else 1
    fields = (change["state"], change["title"], change["text"], source_id, change["quote"],
              "model_extracted" if model else "admin_confirmed", seq, now)
    if old:
        db.execute("UPDATE memory_records SET version=?,state=?,title=?,text=?,source_event_id=?,quote=?,provenance=?,last_seq=?,updated_at=? "
                   "WHERE memory_id=?", (version, *fields, memory_id))
    else:
        db.execute("INSERT INTO memory_records(memory_id,conversation_id,character_id,kind,version,state,title,text,source_event_id,quote,"
                   "provenance,last_seq,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (memory_id, conversation_id, character_id, change["kind"], version, *fields, now))
    record = _scoped_record(db, db.execute("SELECT * FROM memory_records WHERE memory_id=?", (memory_id,)).fetchone())
    db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
               (memory_id, version, event_id, json_text(record), actor, note[:1000], now))
    if db.execute("PRAGMA user_version").fetchone()[0] >= 9:
        from .episode_times import sync_record
        sync_record(db, record, now)
    return record


def memory_page(db, conversation_id, character_id, *, limit=30, before_id=None):
    limit, cursor = page_limit(limit), sequence_cursor(before_id)
    if not db.execute("SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone():
        raise LookupError("unknown conversation_id")
    rows = db.execute("SELECT m.*,e.received_at AS source_received_at FROM memory_records m JOIN inbox_events e ON e.event_id=m.source_event_id "
                      "WHERE m.conversation_id=? AND m.character_id=? AND (? IS NULL OR m.id<?) "
                      "ORDER BY m.id DESC LIMIT ?", (conversation_id, character_id, cursor, cursor, limit + 1)).fetchall()
    return {"conversation_id": conversation_id, "character_id": character_id,
            "records": [_scoped_record(db, row) for row in rows[:limit]],
            "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None}


def memory_detail(db, conversation_id, character_id, memory_id):
    record = db.execute("SELECT * FROM memory_records WHERE memory_id=? AND conversation_id=? AND character_id=?",
                         (memory_id, conversation_id, character_id)).fetchone()
    if record is None:
        raise LookupError("unknown memory_id in this scope")
    revisions = []
    for row in db.execute("SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY version DESC LIMIT 100", (memory_id,)):
        revision = dict(row)
        revision["record"] = json.loads(revision.pop("record_json"))
        revisions.append(revision)
    return {"record": _scoped_record(db, record), "revisions": revisions}


def _terms(text):
    text = text.casefold()
    terms = set(re.findall(r"[a-z0-9]+", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return terms


def recall_snapshot(db, conversation_id, character_id, query, *, limit=6):
    # Scope and revision filtering precede lexical ranking. No global memory
    # pool, embedding call, or inferred absolute date is involved here.
    rows = db.execute("SELECT * FROM memory_records WHERE conversation_id=? AND character_id=? AND state!='withdrawn'",
                      (conversation_id, character_id)).fetchall()
    terms = _terms(query)
    ranked = sorted(rows, key=lambda r: (len(terms & _terms(r["title"] + " " + r["text"])),
                    r["kind"] == "fact", r["state"] == "open", r["last_seq"], r["id"]), reverse=True)
    selected = []
    for row in ranked[:limit]:
        record = _scoped_record(db, row)
        source = db.execute("SELECT received_at FROM inbox_events WHERE event_id=?", (row["source_event_id"],)).fetchone()
        record["source_received_at"] = source[0]
        if row["kind"] == "episode" and db.execute("PRAGMA user_version").fetchone()[0] >= 9:
            from .episode_times import context_value, sync_record
            sync_record(db, row, time.time())
            record["time"] = context_value(db, row["memory_id"], row["version"])
        selected.append(record)
    # Suppress whole historical rounds containing superseded statements. This
    # also stops a prior assistant paraphrase from reviving a corrected value.
    suppressed = set()
    for record in selected:
        timing = record.get("time") or {}
        confirmation = timing.get("value", {}).get("confirmation_source_event_id")
        if timing.get("state") == "confirmed" and confirmation and confirmation != record["source_event_id"]:
            suppressed.add(record["source_event_id"])
    for row in db.execute("SELECT r.record_json,m.version,m.state FROM memory_revisions r JOIN memory_records m USING(memory_id) "
            "WHERE m.conversation_id=? AND m.character_id=? AND (r.version<m.version OR m.state='withdrawn')",
            (conversation_id, character_id)):
        suppressed.add(json.loads(row["record_json"])["source_event_id"])
    if db.execute("PRAGMA user_version").fetchone()[0] >= 7:
        # A sent follow-up can quote an older episode too. Do not let the
        # assistant's own historical question reintroduce corrected data.
        for row in db.execute("SELECT o.event_id FROM followup_occurrences o JOIN followup_candidates f USING(candidate_id) "
                "JOIN memory_records m USING(memory_id) WHERE m.conversation_id=? AND m.character_id=? "
                "AND (f.memory_version<m.version OR m.state='withdrawn')", (conversation_id, character_id)):
            suppressed.add(row[0])
    if db.execute("PRAGMA user_version").fetchone()[0] >= 9:
        # A time correction can change the date without revising the episode's
        # prose. Previously sent questions must not revive that old date.
        for row in db.execute("SELECT o.event_id FROM followup_occurrences o JOIN followup_candidates f USING(candidate_id) "
                "JOIN episode_times t ON t.memory_id=f.memory_id AND t.memory_version=f.memory_version "
                "JOIN inbox_events e ON e.event_id=o.event_id WHERE f.conversation_id=? AND f.character_id=? "
                "AND t.updated_at>=e.received_at AND (t.state IN ('confirmed','cleared') OR t.reason='timezone_changed')",
                (conversation_id, character_id)):
            suppressed.add(row[0])
    if db.execute("PRAGMA user_version").fetchone()[0] >= 10:
        # Pure workflow turns are not ordinary conversation evidence. An
        # automatic question appended to a normal reply must not erase the
        # user's emotional context; history() strips only that appended suffix.
        # Superseded memory/time sources above are still suppressed in full.
        for row in db.execute("SELECT d.event_id FROM episode_time_dialogue_turns d JOIN turns t USING(event_id) "
                "WHERE d.conversation_id=? AND t.character_id=? AND d.response_text IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM memory_revisions r WHERE r.change_event_id=d.event_id AND r.actor='extractor:reply-memory-v1')",
                (conversation_id, character_id)):
            suppressed.add(row[0])
    return {"bot_account_id": db.execute("SELECT value FROM runtime_meta WHERE key='account_id'").fetchone()[0],
            "conversation_id": conversation_id, "character_id": character_id,
            "records": selected, "suppressed_event_ids": sorted(suppressed)}
