"""Conversation erasure and an independent, content-free restoration barrier."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from .contracts import json_text


SCHEMA_V5 = (
    "ALTER TABLE conversations ADD COLUMN data_generation INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN privacy_state TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE outbox_messages ADD COLUMN erased INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE privacy_meta (journal_id TEXT NOT NULL)",
)


class StaleWork(ValueError):
    """Work started before a conversation's data was erased."""


class PrivacyUnavailable(RuntimeError):
    """No reads, writes or sends may proceed without the deletion ledger."""


class ConversationErased(ValueError):
    """A paused conversation's incoming content must be acknowledged and dropped."""


def journal_path(path):
    return path.with_suffix(".privacy.sqlite3")


def fingerprint(account, conversation, source_id):
    return hashlib.sha256(json_text([account, conversation, source_id]).encode()).hexdigest()


def require_active(db, conversation_id):
    row = db.execute("SELECT privacy_state FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if row and row[0] != "active":
        raise ValueError("此会话已删除并暂停接收，请在管理端明确开始新会话")


def require_current(db, context):
    row = db.execute("SELECT data_generation,privacy_state FROM conversations WHERE conversation_id=?",
                     (context.binding.conversation_id,)).fetchone()
    if not row or row[0] != context.data_generation or row[1] != "active":
        raise StaleWork("conversation data was erased; discard old work")


def preview(db, conversation_id):
    row = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not row:
        raise LookupError("unknown conversation")
    counts = {}
    for table in ("inbox_events", "messages", "memory_records", "reminder_tasks", "reminder_requests",
                  "contact_policies", "contact_requests", "contact_attempts", "followup_candidates", "followup_requests", "followup_drafts",
                  "episode_times", "episode_time_requests", "episode_time_dialogues", "episode_time_dialogue_turns"):
        counts[table] = db.execute(f"SELECT count(*) FROM {table} WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
    for table, join in (("memory_revisions", "JOIN memory_records USING(memory_id)"),
                        ("turn_memory_snapshots", "JOIN turns USING(turn_id) JOIN inbox_events USING(event_id)"),
                        ("turn_contexts", "JOIN turns USING(turn_id) JOIN inbox_events USING(event_id)")):
        counts[table] = db.execute(f"SELECT count(*) FROM {table} {join} WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
    deliveries = [dict(r) for r in db.execute("SELECT o.outbox_id,o.state,o.attempts,o.platform_message_id,o.erased "
        "FROM outbox_messages o JOIN inbox_events e USING(event_id) WHERE e.conversation_id=? ORDER BY o.outbox_id", (conversation_id,))]
    counts["outbox_messages"] = len(deliveries)
    pending = [r for r in deliveries if r["state"] in {"dispatching", "unknown"}]
    token = hashlib.sha256(json_text([row["data_generation"], row["generation"], row["next_seq"], row["privacy_state"], counts, deliveries]).encode()).hexdigest()
    return {"conversation_id": conversation_id, "data_generation": row["data_generation"],
            "state": "needs_review" if row["privacy_state"] == "erased" and pending else row["privacy_state"],
            "counts": counts, "pending_deliveries": pending, "preview_token": token}


class DeletionJournal:
    def __init__(self, path, account, mode, expected_id=None):
        self.path = journal_path(path)
        if expected_id and not self.path.is_file():
            raise PrivacyUnavailable("删除记录库缺失，已停止打开运行库；请恢复最新的 privacy.sqlite3 文件")
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
                if expected_id:
                    raise PrivacyUnavailable("删除记录库为空，无法验证恢复屏障")
                self.db.execute("CREATE TABLE identity (journal_id TEXT,account_id TEXT,mode TEXT)")
                self.db.execute("INSERT INTO identity VALUES(?,?,?)", (uuid.uuid4().hex, account, mode))
                self.db.execute("""CREATE TABLE deletions (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
                    generation INTEGER NOT NULL, cutoff_seq INTEGER NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL,
                    counts_json TEXT NOT NULL, deliveries_json TEXT NOT NULL, UNIQUE(conversation_id,request_id))""")
                self.db.execute("""CREATE TABLE erased_sources (
                    fingerprint TEXT PRIMARY KEY, event_id TEXT NOT NULL, seq INTEGER NOT NULL)""")
                self.db.execute("PRAGMA user_version=1")
            if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise PrivacyUnavailable("unsupported deletion journal version")
            row = self.db.execute("SELECT * FROM identity").fetchone()
            if not row or row["account_id"] != account or row["mode"] != mode or (expected_id and row["journal_id"] != expected_id):
                raise PrivacyUnavailable("删除记录库的账号、模式或身份不匹配")
            self.identity = row["journal_id"]
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def previous(self, conversation_id, request_id):
        return self.db.execute("SELECT * FROM deletions WHERE conversation_id=? AND request_id=?",
                               (conversation_id, request_id)).fetchone()

    def record(self, db, conversation_id, request_id, digest, actor, now, counts, account):
        row = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        deliveries = [dict(r) for r in db.execute("SELECT o.outbox_id,o.event_id,o.turn_id,o.state,o.attempts,"
            "o.platform_message_id,e.seq,e.received_at FROM outbox_messages o JOIN inbox_events e USING(event_id) "
            "WHERE e.conversation_id=? AND o.state IN ('dispatching','unknown')", (conversation_id,))]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO deletions(conversation_id,request_id,digest,generation,cutoff_seq,actor,created_at,counts_json,deliveries_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (conversation_id, request_id, digest, row["data_generation"] + 1, row["next_seq"] - 1,
                actor, now, json_text(counts), json_text(deliveries)))
            for event in db.execute("SELECT event_id,seq,source_id FROM inbox_events WHERE conversation_id=? AND source_id IS NOT NULL", (conversation_id,)):
                self.db.execute("INSERT OR IGNORE INTO erased_sources VALUES(?,?,?)",
                    (fingerprint(account, conversation_id, event["source_id"]), event["event_id"], event["seq"]))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.previous(conversation_id, request_id)

    def duplicate_source(self, account, conversation_id, source_id):
        return self.db.execute("SELECT event_id,seq FROM erased_sources WHERE fingerprint=?",
                               (fingerprint(account, conversation_id, source_id),)).fetchone()

    def suppress_incoming(self, event):
        if event.source_event_id:
            self.db.execute("INSERT OR IGNORE INTO erased_sources VALUES(?,?,0)",
                (fingerprint(event.bot_account_id, event.conversation_id, "message:" + event.source_event_id), event.event_id))

    def records(self, conversation_id):
        return [dict(r) for r in self.db.execute("SELECT generation,actor,created_at,counts_json FROM deletions "
            "WHERE conversation_id=? ORDER BY id DESC LIMIT 30", (conversation_id,))]

    def replay(self, db):
        for record in self.db.execute("SELECT * FROM deletions ORDER BY id"):
            apply_erasure(db, record)
        # A main database with newer deletion generations than its journal is
        # also unsafe: an older copy of the journal must not hide lost intents.
        for row in db.execute("SELECT conversation_id,data_generation FROM conversations WHERE data_generation>1"):
            highest = self.db.execute("SELECT max(generation) FROM deletions WHERE conversation_id=?", (row[0],)).fetchone()[0]
            if highest != row[1]:
                raise PrivacyUnavailable("删除记录库落后于运行库，必须恢复最新删除记录")


def apply_erasure(db, record):
    cid, generation = record["conversation_id"], record["generation"]
    current = db.execute("SELECT data_generation FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    if current and current[0] >= generation:
        return
    db.execute("INSERT OR IGNORE INTO conversations(conversation_id,character_id,character_version,card_json) VALUES(?,'','','{}')", (cid,))
    # Retain only the minimum transport metadata for a send that might already
    # have reached QQ. Its content is erased even while the result is pending.
    events = "SELECT event_id FROM inbox_events WHERE conversation_id=?"
    boxes = f"SELECT outbox_id FROM outbox_messages WHERE event_id IN ({events})"
    turns = f"SELECT turn_id FROM turns WHERE event_id IN ({events})"
    for target in (events, boxes, turns):
        db.execute(f"DELETE FROM audit_events WHERE target_id IN ({target})", (cid,))
    db.execute("DELETE FROM audit_events WHERE target_id=?", (cid,))
    for table in ("contact_attempts", "contact_requests", "contact_policies"):
        db.execute(f"DELETE FROM {table} WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM followup_drafts WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM episode_time_dialogues WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM episode_time_dialogue_turns WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM episode_time_requests WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM episode_times WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM followup_requests WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM followup_occurrences WHERE candidate_id IN (SELECT candidate_id FROM followup_candidates WHERE conversation_id=?)", (cid,))
    db.execute("DELETE FROM followup_candidates WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM reminder_requests WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM reminder_occurrences WHERE task_id IN (SELECT task_id FROM reminder_tasks WHERE conversation_id=?)", (cid,))
    db.execute("DELETE FROM reminder_tasks WHERE conversation_id=?", (cid,))
    db.execute("DELETE FROM memory_revisions WHERE memory_id IN (SELECT memory_id FROM memory_records WHERE conversation_id=?)", (cid,))
    db.execute("DELETE FROM memory_records WHERE conversation_id=?", (cid,))
    for table in ("turn_memory_snapshots", "turn_contexts"):
        db.execute(f"DELETE FROM {table} WHERE turn_id IN ({turns})", (cid,))
    db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
    db.execute(f"DELETE FROM delivery_attempts WHERE outbox_id IN ({boxes})", (cid,))
    db.execute(f"DELETE FROM outbox_messages WHERE event_id IN ({events})", (cid,))
    db.execute(f"DELETE FROM turns WHERE event_id IN ({events})", (cid,))
    db.execute("DELETE FROM inbox_events WHERE conversation_id=?", (cid,))
    for delivery in json.loads(record["deliveries_json"]):
        db.execute("INSERT INTO inbox_events(event_id,conversation_id,seq,kind,payload_json,received_at,state) "
            "VALUES(?,?,?,'erased','{}',?,'needs_review')", (delivery["event_id"], cid, delivery["seq"], delivery["received_at"]))
        db.execute("INSERT INTO turns VALUES(?,?,'','','{}',0,'','needs_review',1)", (delivery["turn_id"], delivery["event_id"]))
        db.execute("INSERT INTO outbox_messages(outbox_id,event_id,turn_id,payload_json,state,attempts,platform_message_id,erased) VALUES(?,?,?,?,?,?,?,1)",
            (delivery["outbox_id"], delivery["event_id"], delivery["turn_id"],
             json_text({"launcher_type": "person", "launcher_id": cid, "text": "", "images": []}),
             delivery["state"], delivery["attempts"], delivery["platform_message_id"]))
        db.execute("INSERT INTO delivery_attempts(outbox_id,attempt,started_at,state) VALUES(?,?,?,?)",
            (delivery["outbox_id"], delivery["attempts"], record["created_at"], delivery["state"]))
    db.execute("UPDATE conversations SET data_generation=?,privacy_state='erased',generation=generation+1,"
        "character_id='',character_version='',card_json='{}',next_seq=max(next_seq,?) WHERE conversation_id=?",
        (generation, record["cutoff_seq"] + 1, cid))


def check_inspection(db, path):
    """Read-only CLI inspection must not expose an unapplied restored backup."""
    if db.execute("PRAGMA user_version").fetchone()[0] < 5:
        if journal_path(path).exists():
            raise PrivacyUnavailable("先通过新版运行时应用删除屏障，再检查恢复的旧库")
        return
    identity = db.execute("SELECT journal_id FROM privacy_meta").fetchone()
    if not identity or not journal_path(path).is_file():
        raise PrivacyUnavailable("删除记录库缺失，拒绝读取")
    journal = sqlite3.connect(journal_path(path).as_uri() + "?mode=ro", uri=True)
    try:
        if journal.execute("SELECT journal_id FROM identity").fetchone()[0] != identity[0]:
            raise PrivacyUnavailable("删除记录库身份不匹配")
        for cid, generation in journal.execute("SELECT conversation_id,max(generation) FROM deletions GROUP BY conversation_id"):
            row = db.execute("SELECT data_generation FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
            if not row or row[0] != generation:
                raise PrivacyUnavailable("恢复的运行库尚未同步删除记录，请先启动新版运行时")
        for cid, generation in db.execute("SELECT conversation_id,data_generation FROM conversations WHERE data_generation>1"):
            row = journal.execute("SELECT max(generation) FROM deletions WHERE conversation_id=?", (cid,)).fetchone()
            if not row or row[0] != generation:
                raise PrivacyUnavailable("删除记录库落后于运行库，拒绝读取")
    finally:
        journal.close()
