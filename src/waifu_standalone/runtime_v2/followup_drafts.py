"""Explicit, durable draft requests. These jobs never create delivery work."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from . import contact_policy, followups, episode_times
from .contracts import ConversationBinding, HistoryMessage, json_text


SCHEMA_V8 = (
    """CREATE TABLE followup_drafts (
        job_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations,
        candidate_id TEXT NOT NULL REFERENCES followup_candidates(candidate_id),
        request_id TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        text TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
        report_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(conversation_id,request_id))""",
    "CREATE INDEX followup_drafts_queue ON followup_drafts(state,created_at)",
    "CREATE INDEX followup_drafts_candidate ON followup_drafts(candidate_id,created_at)",
)
TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class DraftContext:
    job_id: str
    binding: ConversationBinding
    conversation_seq: int
    data_generation: int
    episode_json: str
    source_received_at: float
    requested_at: float
    suppressed_event_ids: tuple[str, ...]
    history: tuple[HistoryMessage, ...]


@dataclass(frozen=True, slots=True)
class DraftResult:
    text: str = ""
    reason: str = ""


def public(row):
    result = {key: row[key] for key in ("job_id", "candidate_id", "actor", "state", "text", "reason", "created_at", "updated_at")}
    result["snapshot"] = json.loads(row["snapshot_json"])
    result["report"] = json.loads(row["report_json"])
    result["note"] = json.loads(row["payload_json"])["note"]
    return result


def invalid_reason(db, job, now, enabled):
    cid = job["conversation_id"]
    snapshot = json.loads(job["snapshot_json"])
    c = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    if not c or c["privacy_state"] != "active" or c["data_generation"] != snapshot["data_generation"]:
        return "conversation_erased"
    if not (enabled(cid) if callable(enabled) else enabled):
        return "runtime_disabled"
    if (c["character_id"], c["character_version"], c["generation"]) != (
            snapshot["character_id"], snapshot["character_version"], snapshot["binding_generation"]):
        return "binding_changed"
    p = contact_policy.policy(db, cid)
    if p["version"] != snapshot["policy_version"] or not p["proactive_enabled"] or p["paused"]:
        return "contact_changed"
    if c["next_seq"] != snapshot["next_seq"]:
        return "conversation_changed"
    task = db.execute("SELECT * FROM followup_candidates WHERE candidate_id=? AND conversation_id=?", (job["candidate_id"], cid)).fetchone()
    if not task or task["state"] != "candidate" or task["version"] != snapshot["candidate_version"]:
        return "candidate_changed"
    if episode_times.version_for(db, task["memory_id"], task["memory_version"]) != snapshot.get("timing_version", 0):
        return "episode_time_changed"
    timing = episode_times.current(db, task["memory_id"], task["memory_version"])
    if timing and timing["timezone"] != p["timezone"]:
        return "episode_time_changed"
    blocked = followups.intrinsic(db, task, now, enabled)
    if blocked:
        return blocked[1]
    if db.execute("SELECT 1 FROM inbox_events WHERE conversation_id=? AND state IN ('queued','generating','outbox_ready','needs_review') LIMIT 1", (cid,)).fetchone():
        return "conversation_busy"
    # A completed draft is deliberately short lived; timing is still reviewed separately.
    if now > job["created_at"] + 86400:
        return "draft_expired"
    return ""


def reconcile(db, now, enabled):
    for job in db.execute("SELECT * FROM followup_drafts WHERE state IN ('queued','generating','ready')").fetchall():
        reason = invalid_reason(db, job, now, enabled)
        if reason:
            db.execute("UPDATE followup_drafts SET state='invalidated',text='',reason=?,updated_at=? WHERE job_id=?", (reason, now, job["job_id"]))


def request(db, payload, actor, now, enabled):
    cid, rid = payload.get("conversation_id"), payload.get("request_id")
    if not all(isinstance(v, str) and 0 < len(v) <= 100 for v in (cid, rid, actor)):
        raise ValueError("conversation, request ID and authenticated actor are required")
    serialized = json_text(payload)
    if db.execute("SELECT 1 FROM followup_requests WHERE conversation_id=? AND request_id=?", (cid, rid)).fetchone():
        raise ValueError("request_id was already used for another follow-up operation")
    previous = db.execute("SELECT * FROM followup_drafts WHERE conversation_id=? AND request_id=?", (cid, rid)).fetchone()
    if previous:
        if previous["payload_json"] != serialized or previous["actor"] != actor:
            raise ValueError("request_id was already used with different content")
        return {"status": "saved", "draft": public(previous), "duplicate": True}
    if payload.get("authorized") is not True or not isinstance(payload.get("note"), str) or not 1 <= len(payload["note"].strip()) <= 1000:
        raise ValueError("请确认用户授权并填写 1—1000 字操作依据")
    c = db.execute("SELECT * FROM conversations WHERE conversation_id=? AND privacy_state='active'", (cid,)).fetchone()
    task = db.execute("SELECT * FROM followup_candidates WHERE conversation_id=? AND candidate_id=?", (cid, str(payload.get("candidate_id", "")))).fetchone()
    if not c or not task:
        raise ValueError("unknown active conversation or follow-up candidate")
    p = contact_policy.policy(db, cid)
    for key, expected in (("expected_version", task["version"]), ("expected_data_generation", c["data_generation"]), ("expected_policy_version", p["version"])):
        if type(payload.get(key)) is not int or payload[key] != expected:
            raise ValueError("候选、联系规则或数据代次已变化，请刷新后操作")
    snapshot = {"candidate_version": task["version"], "memory_id": task["memory_id"], "memory_version": task["memory_version"],
        "source_event_id": task["source_event_id"], "character_id": task["character_id"], "character_version": c["character_version"],
        "binding_generation": c["generation"], "data_generation": c["data_generation"], "policy_version": p["version"], "next_seq": c["next_seq"],
        "timing_version": episode_times.version_for(db, task["memory_id"], task["memory_version"])}
    job = {"job_id": uuid.uuid4().hex, "conversation_id": cid, "candidate_id": task["candidate_id"], "snapshot_json": json_text(snapshot), "created_at": now}
    reason = invalid_reason(db, job, now, enabled)
    if reason:
        raise ValueError("当前不能生成草稿：" + reason)
    if db.execute("SELECT 1 FROM followup_drafts WHERE conversation_id=? AND state IN ('queued','generating')", (cid,)).fetchone():
        raise ValueError("此会话已有草稿正在生成，请等待结果")
    if db.execute("SELECT count(*) FROM followup_drafts WHERE state IN ('queued','generating')").fetchone()[0] >= 32:
        raise ValueError("草稿队列已满，请稍后再试")
    if db.execute("SELECT count(*) FROM followup_drafts WHERE conversation_id=? AND created_at>?", (cid, now - 86400)).fetchone()[0] >= 10:
        raise ValueError("此会话过去 24 小时已请求 10 次草稿，请稍后再试")
    db.execute("INSERT INTO followup_drafts(job_id,conversation_id,candidate_id,request_id,actor,payload_json,snapshot_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (job["job_id"], cid, task["candidate_id"], rid, actor, serialized, job["snapshot_json"], now, now))
    return {"status": "saved", "draft": public(db.execute("SELECT * FROM followup_drafts WHERE job_id=?", (job["job_id"],)).fetchone()), "duplicate": False}


def checked_ready(db, job_id, candidate_id, now, enabled):
    job = db.execute("SELECT * FROM followup_drafts WHERE job_id=? AND candidate_id=?", (job_id, candidate_id)).fetchone()
    if not job or job["state"] != "ready" or invalid_reason(db, job, now, enabled):
        raise ValueError("生成草稿已失效或未完成，请刷新并重新核对")
    return job


def list_jobs(db, candidate_id, limit=20):
    return [public(row) for row in db.execute("SELECT * FROM followup_drafts WHERE candidate_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?", (candidate_id, limit))]
