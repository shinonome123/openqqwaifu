from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..models import OutboundMessage
from .inspection import conversations, conversation_detail
from . import memories, reminders, privacy, contact_policy, followups, followup_drafts, episode_times, episode_time_dialogue
from .reminder_time import HELP as REMINDER_HELP, parse_command
from .contracts import (
    Admission, CharacterSnapshot, ConversationBinding, EventEnvelope, HistoryMessage,
    TurnContext, json_text,
)


class RuntimeBusy(RuntimeError):
    pass


class _OwnerLock:
    """OS-held writer ownership. A crash releases it; elapsed time does not."""

    def __init__(self, path: Path):
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeBusy("runtime v2 database already has a writer") from exc

    def close(self):
        if self.file.closed:
            return
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


# Append new versions; do not edit a released migration.
_SCHEMA_V1 = (
    "CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE conversations (
        conversation_id TEXT PRIMARY KEY, next_seq INTEGER NOT NULL DEFAULT 1,
        character_id TEXT NOT NULL, character_version TEXT NOT NULL, card_json TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE inbox_events (
        event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations,
        seq INTEGER NOT NULL, source_id TEXT, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        received_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued', error_code TEXT NOT NULL DEFAULT '',
        UNIQUE(conversation_id, seq), UNIQUE(conversation_id, source_id))""",
    """CREATE TABLE turns (
        turn_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES inbox_events,
        character_id TEXT NOT NULL, character_version TEXT NOT NULL, card_json TEXT NOT NULL,
        binding_generation INTEGER NOT NULL, config_version TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE messages (
        event_id TEXT NOT NULL REFERENCES inbox_events, role TEXT NOT NULL,
        conversation_id TEXT NOT NULL REFERENCES conversations, seq INTEGER NOT NULL,
        character_id TEXT NOT NULL, content TEXT NOT NULL, delivery_state TEXT NOT NULL,
        PRIMARY KEY(event_id, role))""",
    """CREATE TABLE outbox_messages (
        outbox_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES inbox_events,
        turn_id TEXT NOT NULL UNIQUE REFERENCES turns, payload_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', platform_message_id TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE delivery_attempts (
        outbox_id TEXT NOT NULL REFERENCES outbox_messages, attempt INTEGER NOT NULL,
        started_at REAL NOT NULL, finished_at REAL, state TEXT NOT NULL,
        PRIMARY KEY(outbox_id, attempt))""",
    """CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY, created_at REAL NOT NULL, action TEXT NOT NULL,
        target_id TEXT NOT NULL, note TEXT NOT NULL)""",
    "CREATE INDEX inbox_pending ON inbox_events(state, conversation_id, seq)",
    "CREATE INDEX history_scope ON messages(conversation_id, character_id, seq)",
)

_SCHEMA_V2 = (
    """CREATE TABLE turn_contexts (
        turn_id TEXT PRIMARY KEY REFERENCES turns,
        report_json TEXT NOT NULL, updated_at REAL NOT NULL)""",
)


class RuntimeStore:
    """One account and one delivery mode per database for this first slice.

    All SQL mutations, including admission, are short transactions. No network
    calls or model calls run under this lock. Read-only inspection has no lease.
    """

    def __init__(self, path: str | Path, account_id: str, mode: str, *, max_pending: int = 1000,
                 reminders_enabled=False, reminder_timezone="", reminder_grace_seconds=300, reminder_gate=None, followup_gate=None, clock=None):
        if not account_id.strip() or mode not in {"live", "capture"} or max_pending < 1:
            raise ValueError("valid account, delivery mode and queue capacity are required")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account_id, self.mode, self.max_pending = account_id, mode, max_pending
        if type(reminders_enabled) is not bool or type(reminder_grace_seconds) is not int or not 1 <= reminder_grace_seconds <= 86400:
            raise ValueError("invalid reminder switch or grace period (1—86400 seconds)")
        if not isinstance(reminder_timezone, str):
            raise ValueError("reminder_timezone must be a string")
        self.reminders_enabled, self.reminder_timezone = reminders_enabled, reminder_timezone
        self.reminder_grace_seconds, self.reminder_gate = reminder_grace_seconds, reminder_gate
        self.followup_gate = followup_gate
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._closed = False
        self._privacy_failed = False
        self._owner = _OwnerLock(self.path.with_suffix(self.path.suffix + ".lock"))
        try:
            self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=2)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA secure_delete=ON")
            with self._transaction() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    for statement in _SCHEMA_V1:
                        db.execute(statement)
                    db.executemany("INSERT INTO runtime_meta VALUES (?, ?)",
                                   [("account_id", account_id), ("mode", mode)])
                    db.execute("PRAGMA user_version=1")
                    version = 1
                if version == 1:
                    for statement in _SCHEMA_V2:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=2")
                    version = 2
                if version == 2:
                    for statement in memories.SCHEMA_V3:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=3")
                    version = 3
                if version == 3:
                    for statement in reminders.SCHEMA_V4:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=4")
                    version = 4
                if version == 4:
                    for statement in privacy.SCHEMA_V5:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=5")
                    version = 5
                if version == 5:
                    for statement in contact_policy.SCHEMA_V6:
                        db.execute(statement)
                    for row in db.execute("SELECT conversation_id FROM conversations WHERE privacy_state='active'").fetchall():
                        contact_policy.ensure(db, row[0], self.clock(), self.reminder_timezone)
                    # Historical relative UTC values might have been display fallbacks.
                    db.execute("UPDATE reminder_tasks SET timezone_confirmed=1 WHERE timezone<>'UTC' OR ?='UTC'", (self.reminder_timezone,))
                    db.execute("INSERT INTO contact_attempts SELECT e.conversation_id,a.outbox_id,a.attempt,'reminder',a.started_at,1 "
                        "FROM delivery_attempts a JOIN outbox_messages o USING(outbox_id) JOIN inbox_events e USING(event_id) "
                        "WHERE e.kind='reminder_due' AND e.conversation_id IN (SELECT conversation_id FROM contact_policies)")
                    db.execute("PRAGMA user_version=6")
                    version = 6
                if version == 6:
                    for statement in followups.SCHEMA_V7:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=7")
                    version = 7
                if version == 7:
                    for statement in followup_drafts.SCHEMA_V8:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=8")
                    version = 8
                if version == 8:
                    for statement in episode_times.SCHEMA_V9:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=9")
                    version = 9
                if version == 9:
                    for statement in episode_time_dialogue.SCHEMA_V10:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=10")
                    version = 10
                if version != 10:
                    raise ValueError(f"unsupported runtime v2 schema: {version}")
                meta = dict(db.execute("SELECT key, value FROM runtime_meta"))
                if meta != {"account_id": account_id, "mode": mode}:
                    raise ValueError("runtime database account or delivery mode mismatch")
            with self._transaction() as db:
                identity = db.execute("SELECT journal_id FROM privacy_meta").fetchone()
                self._journal = privacy.DeletionJournal(self.path, account_id, mode, identity[0] if identity else None)
                if not identity:
                    db.execute("INSERT INTO privacy_meta VALUES(?)", (self._journal.identity,))
                self._journal.replay(db)
        except BaseException:
            if hasattr(self, "_journal"):
                self._journal.close()
            if hasattr(self, "_db"):
                self._db.close()
            self._owner.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._check_privacy()
            if self._closed:
                raise RuntimeError("runtime store is closed")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _check_privacy(self):
        if self._privacy_failed or (hasattr(self, "_journal") and not self._journal.path.is_file()):
            raise privacy.PrivacyUnavailable("删除屏障未完成或删除记录库丢失，需恢复最新记录并重启；当前拒绝访问")

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._journal.close()
                self._closed = True
                self._owner.close()

    def recover(self):
        with self._transaction() as db:
            # A model call may already have been billed. Only a new explicit
            # draft request can repeat an interrupted generation.
            db.execute("UPDATE followup_drafts SET state='interrupted',reason='interrupted',updated_at=? WHERE state='generating'", (self.clock(),))
            # No tool side effects exist in this slice, so interrupted generation
            # is retryable. External dispatch is deliberately NOT retryable.
            db.execute("UPDATE inbox_events SET state='queued' WHERE state='generating'")
            db.execute("UPDATE turns SET state='planned' WHERE state='generating'")
            db.execute("UPDATE inbox_events SET state='needs_review', error_code='interrupted_send' "
                       "WHERE event_id IN (SELECT event_id FROM outbox_messages WHERE state='dispatching')")
            db.execute("UPDATE turns SET state='needs_review' WHERE event_id IN "
                       "(SELECT event_id FROM outbox_messages WHERE state='dispatching')")
            db.execute("UPDATE delivery_attempts SET state='unknown', finished_at=? WHERE state='dispatching'", (time.time(),))
            db.execute("UPDATE outbox_messages SET state='unknown', error_code='interrupted_send' WHERE state='dispatching'")
            for row in db.execute("SELECT o.event_id,o.state FROM outbox_messages o JOIN reminder_occurrences r USING(event_id) WHERE o.state='unknown'").fetchall():
                reminders.delivery_result(db, row["event_id"], "unknown", self.clock())
            for row in db.execute("SELECT o.event_id FROM outbox_messages o JOIN followup_occurrences f USING(event_id) WHERE o.state='unknown'").fetchall():
                followups.delivery_result(db, row["event_id"], "unknown", self.clock())

    def admit(self, event: EventEnvelope, initial: CharacterSnapshot) -> Admission:
        if event.bot_account_id != self.account_id:
            raise ValueError("account mismatch")
        with self._transaction() as db:
            if event.source_event_id:
                erased = self._journal.duplicate_source(self.account_id, event.conversation_id, "message:" + event.source_event_id)
                if erased:
                    return Admission(erased["event_id"], erased["seq"], True)
            previous = db.execute("SELECT data_generation,privacy_state FROM conversations WHERE conversation_id=?", (event.conversation_id,)).fetchone()
            if previous and previous["privacy_state"] != "active":
                self._journal.suppress_incoming(event)
                raise privacy.ConversationErased("conversation_erased")
            if previous and previous[0] > 1 and not event.source_event_id:
                raise ValueError("删除后重新开始的会话必须提供平台消息 ID，以拦截旧回调重放")
            if event.source_event_id:
                old = db.execute("SELECT event_id, seq FROM inbox_events WHERE conversation_id=? AND source_id=?",
                                 (event.conversation_id, "message:" + event.source_event_id)).fetchone()
                if old:
                    return Admission(old["event_id"], old["seq"], True)
            contact = contact_policy.parse_command(event.inbound().plain_text)
            suppress_confirmation = False
            try:
                self._check_capacity(db)
            except RuntimeBusy:
                if not contact or contact["operation"] != "pause":
                    raise
                # A saturated reply queue must never prevent a safety stop.
                # Persist/deduplicate the command without adding reply backlog.
                suppress_confirmation = True
            db.execute("INSERT OR IGNORE INTO conversations(conversation_id, character_id, character_version, card_json) "
                       "VALUES (?, ?, ?, ?)", (event.conversation_id, initial.character_id, initial.version, initial.card_json))
            contact_policy.ensure(db, event.conversation_id, self.clock(), self.reminder_timezone)
            seq = self._take_seq(db, event.conversation_id)
            db.execute("INSERT INTO inbox_events(event_id, conversation_id, seq, source_id, kind, payload_json, received_at) "
                       "VALUES (?, ?, ?, ?, 'message', ?, ?)",
                       (event.event_id, event.conversation_id, seq,
                        "message:" + event.source_event_id if event.source_event_id else None,
                        event.payload_json, event.received_at))
            command = parse_command(event.inbound().plain_text)
            if command and command["operation"] in {"cancel", "postpone"}:
                # Revocation must not wait behind the reminder it revokes.
                self._user_reminder_command(db, event.event_id, event.conversation_id, command)
            if contact and contact["operation"] not in {"status", "help"}:
                self._user_contact_command(db, event.event_id, event.conversation_id, contact)
            if suppress_confirmation:
                db.execute("UPDATE inbox_events SET state='ignored',error_code='contact_pause_without_reply' WHERE event_id=?", (event.event_id,))
            followups.reconcile(db, self.clock(), self.followups_allowed, event.conversation_id)
            return Admission(event.event_id, seq, False)

    def contact_policy_detail(self, conversation_id):
        with self._lock:
            self._check_privacy()
            privacy.require_active(self._db, conversation_id)
            return contact_policy.detail(self._db, conversation_id, self.clock())

    def contact_request(self, payload, actor):
        with self._transaction() as db:
            privacy.require_active(db, payload.get("conversation_id"))
            result = contact_policy.apply_request(db, payload, actor=actor, now=self.clock())
            episode_times.sync(db, self.clock(), payload["conversation_id"])
            reminders.reconcile_contact(db, payload["conversation_id"], self.clock())
            followups.reconcile(db, self.clock(), self.followups_allowed, payload["conversation_id"])
            return result

    def _user_contact_command(self, db, event_id, conversation_id, command):
        if command["operation"] == "help":
            return contact_policy.HELP
        if command["operation"] == "status":
            detail = contact_policy.detail(db, conversation_id, self.clock())
            return contact_policy.answer({"status": "saved", "policy": detail["policy"]}) + f"\n当前剩余额度：{detail['remaining_24h']}。"
        payload = {**command, "conversation_id": conversation_id, "request_id": "user:" + event_id, "note": "用户明确命令"}
        db.execute("SAVEPOINT contact_command")
        try:
            result = contact_policy.apply_request(db, payload, actor="user:" + conversation_id, now=self.clock(), user=True)
            episode_times.sync(db, self.clock(), conversation_id)
            reminders.reconcile_contact(db, conversation_id, self.clock())
            followups.reconcile(db, self.clock(), self.followups_allowed, conversation_id)
        except ValueError as exc:
            db.execute("ROLLBACK TO contact_command")
            result = {"status": "error", "message": str(exc)}
            db.execute("INSERT OR IGNORE INTO contact_requests VALUES(?,?,?,?,?,?)", (conversation_id, payload["request_id"],
                json_text(payload), "user:" + conversation_id, json_text(result), self.clock()))
        finally:
            db.execute("RELEASE contact_command")
        return contact_policy.answer(result)

    def contact_command(self, context, event):
        command = contact_policy.parse_command(event.plain_text)
        if command is None:
            return None
        with self._transaction() as db:
            privacy.require_current(db, context)
            answer = self._user_contact_command(db, context.event_id, context.binding.conversation_id, command)
        return OutboundMessage(event.launcher_id, "person", answer)

    def reminders_allowed(self, conversation_id=None):
        return self.reminders_enabled and (self.reminder_gate is None or self.reminder_gate(conversation_id))

    def reminder_request(self, payload, actor):
        with self._transaction() as db:
            privacy.require_active(db, payload.get("conversation_id"))
            return reminders.apply_request(db, payload, actor=actor, now=self.clock(),
                enabled=self.reminders_allowed(payload.get("conversation_id")), grace_seconds=self.reminder_grace_seconds,
                default_zone=self.reminder_timezone)

    def reminder_list(self, conversation_id, **kwargs):
        with self._lock:
            self._check_privacy()
            return {**reminders.list_tasks(self._db, conversation_id, **kwargs),
                "enabled": self.reminders_allowed(conversation_id), "default_timezone": (self._db.execute(
                    "SELECT timezone FROM contact_policies WHERE conversation_id=?", (conversation_id,)).fetchone() or [self.reminder_timezone])[0],
                "grace_seconds": self.reminder_grace_seconds, "mode": self.mode}

    def tick_reminders(self):
        with self._transaction() as db:
            episode_times.sync(db, self.clock())
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
            made = reminders.tick(db, now=self.clock(), enabled=self.reminders_allowed,
                                  max_pending=self.max_pending, take_seq=self._take_seq)
            return made + followups.tick(db, now=self.clock(), enabled=self.followups_allowed,
                                        max_pending=self.max_pending, take_seq=self._take_seq)

    def followups_allowed(self, conversation_id=None):
        return self.followup_gate is None or self.followup_gate(conversation_id)

    def followup_request(self, payload, actor):
        with self._transaction() as db:
            privacy.require_active(db, payload.get("conversation_id"))
            episode_times.sync(db, self.clock(), payload.get("conversation_id"))
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
            if payload.get("operation") == "generate":
                return followup_drafts.request(db, payload, actor, self.clock(), self.followups_allowed)
            return followups.apply_request(db, payload, actor=actor, now=self.clock(),
                                          enabled=self.followups_allowed(payload.get("conversation_id")))

    def followup_list(self, conversation_id, **kwargs):
        with self._transaction() as db:
            episode_times.sync(db, self.clock(), conversation_id)
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
            result = {**followups.list_candidates(db, conversation_id, now=self.clock(), **kwargs),
                      "enabled": self.followups_allowed(conversation_id), "mode": self.mode}
            if "candidate" in result:
                result["drafts"] = followup_drafts.list_jobs(db, result["candidate"]["candidate_id"])
                item = result["candidate"]
                result["time_audit"] = episode_times.audit(db, conversation_id, item["memory_id"])
            else:
                for item in result["candidates"]:
                    jobs = followup_drafts.list_jobs(db, item["candidate_id"], 1)
                    item["draft"] = jobs[0] if jobs else None
            for item in [result["candidate"]] if "candidate" in result else result["candidates"]:
                item["timing"] = episode_times.public(episode_times.current(db, item["memory_id"], item["memory_version"]))
            return result

    def episode_time_request(self, payload, actor):
        with self._transaction() as db:
            privacy.require_active(db, payload.get("conversation_id"))
            result = episode_times.request(db, payload, actor, self.clock(), self.followups_allowed(payload.get("conversation_id")))
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
            return result

    def episode_time_command(self, context, event):
        with self._transaction() as db:
            privacy.require_current(db, context)
            row = db.execute("SELECT state FROM inbox_events WHERE event_id=?", (context.event_id,)).fetchone()
            if not row or row[0] != "generating":
                raise ValueError("episode time dialogue requires an active turn")
            answer = episode_time_dialogue.handle(db, context, event, self.clock(),
                                                 self.followups_allowed(context.binding.conversation_id))
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
        return OutboundMessage(event.launcher_id, "person", answer) if answer is not None else None

    def drafts_pending(self):
        with self._lock:
            self._check_privacy()
            return bool(self._db.execute("SELECT 1 FROM followup_drafts WHERE state IN ('queued','generating') LIMIT 1").fetchone())

    def claim_followup_draft(self, history_limit=16):
        with self._transaction() as db:
            episode_times.sync(db, self.clock())
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
            job = db.execute("SELECT * FROM followup_drafts WHERE state='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not job:
                return None
            cid = job["conversation_id"]
            c = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
            snapshot = json.loads(job["snapshot_json"])
            row = db.execute("SELECT * FROM memory_records WHERE memory_id=?", (snapshot["memory_id"],)).fetchone()
            episode = memories._scoped_record(db, row)
            episode["time"] = episode_times.context_value(db, row["memory_id"], row["version"])
            source = db.execute("SELECT received_at FROM inbox_events WHERE event_id=?", (snapshot["source_event_id"],)).fetchone()
            recalled = memories.recall_snapshot(db, cid, c["character_id"], row["text"])
            context = followup_drafts.DraftContext(job["job_id"], ConversationBinding(self.account_id, cid,
                CharacterSnapshot(c["character_id"], c["character_version"], c["card_json"]), c["generation"]),
                c["next_seq"], c["data_generation"], json_text(episode), source[0], job["created_at"],
                tuple(recalled["suppressed_event_ids"]), ())
            context = replace(context, history=tuple(self.history(context, history_limit)))
            db.execute("UPDATE followup_drafts SET state='generating',updated_at=? WHERE job_id=?", (self.clock(), job["job_id"]))
            return context

    def authorize_draft_call(self, job_id, report):
        with self._transaction() as db:
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
            row = db.execute("SELECT state,report_json FROM followup_drafts WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["state"] != "generating" or row["report_json"] != "{}":
                return False
            db.execute("UPDATE followup_drafts SET report_json=?,updated_at=? WHERE job_id=?", (json_text(report), self.clock(), job_id))
            return True

    def finish_followup_draft(self, job_id, result=None, *, error_code=""):
        with self._transaction() as db:
            followup_drafts.reconcile(db, self.clock(), self.followups_allowed)
            job = db.execute("SELECT * FROM followup_drafts WHERE job_id=?", (job_id,)).fetchone()
            if not job or job["state"] != "generating":
                return False  # In particular, never recreate erased rows or audit text.
            text, reason, state = "", error_code, "failed"
            if not error_code:
                if not isinstance(result, followup_drafts.DraftResult):
                    raise ValueError("invalid draft result")
                if result.text and not result.reason and 1 <= len(result.text.strip()) <= 500:
                    text, state = result.text.strip(), "ready"
                elif not result.text and result.reason in ("insufficient_evidence", "not_appropriate", "already_resolved"):
                    state, reason = "abstained", result.reason
                else:
                    raise ValueError("invalid draft result")
            report = json.loads(job["report_json"])
            report["status"] = state
            db.execute("UPDATE followup_drafts SET state=?,text=?,reason=?,report_json=?,updated_at=? WHERE job_id=?",
                (state, text, reason, json_text(report), self.clock(), job_id))
            return True

    def _user_reminder_command(self, db, event_id, conversation_id, command):
        if command["operation"] == "help":
            return REMINDER_HELP
        if command["operation"] == "list":
            records = reminders.list_tasks(db, conversation_id, limit=20)["tasks"]
            return "提醒记录：\n" + "\n".join(f"{r['task_id']} · {r['local_time']} {r['timezone']} · {r['state']} · {r['text']}" for r in records) if records else "当前没有提醒。"
        source = db.execute("SELECT payload_json,received_at FROM inbox_events WHERE event_id=?", (event_id,)).fetchone()
        incoming = json.loads(source["payload_json"])
        quote = next((p["text"] for p in incoming["segments"] if p.get("kind") == "text"), "")
        payload = {**command, "conversation_id": conversation_id, "request_id": "user:" + event_id,
                   "source_event_id": event_id, "evidence": quote, "note": "用户明确命令"}
        # Keep a failed command idempotent too. A later retry of generation must
        # not turn a previously rejected cancellation into a new side effect.
        db.execute("SAVEPOINT reminder_command")
        try:
            result = reminders.apply_request(db, payload, actor="user:" + conversation_id, now=self.clock(),
                enabled=self.reminders_allowed(conversation_id), grace_seconds=self.reminder_grace_seconds,
                default_zone=self.reminder_timezone, user=True, time_base=source["received_at"])
        except ValueError as exc:
            db.execute("ROLLBACK TO reminder_command")
            result = {"status": "error", "message": str(exc)}
            db.execute("INSERT OR IGNORE INTO reminder_requests VALUES(?,?,?,?,?,?)", (conversation_id, payload["request_id"],
                json_text(payload), "user:" + conversation_id, json_text(result), self.clock()))
        finally:
            db.execute("RELEASE reminder_command")
        if result["status"] == "error":
            return "提醒未更改：" + result["message"] + "\n" + REMINDER_HELP
        task = result["task"]
        if result["status"] in {"in_flight", "needs_review"}:
            return f"已停止提醒 {task['task_id']} 的后续发送；本次已开始发送或结果不明，需核对，不能保证撤回。"
        if result["status"] == "cancelled":
            return f"已取消提醒 {task['task_id']}。"
        exception = "本条已明确允许静默时段发送。" if task["allow_quiet"] else "遵守静默时段。"
        return f"提醒已保存：{task['local_time']}（{task['timezone']}），{task['text']}。\nID：{task['task_id']}；{exception}仍受暂停、频率上限及原补发窗口约束。"

    def reminder_command(self, context, event):
        command = parse_command(event.plain_text)
        if command is None:
            return None
        with self._transaction() as db:
            privacy.require_current(db, context)
            answer = self._user_reminder_command(db, context.event_id, context.binding.conversation_id, command)
        return OutboundMessage(event.launcher_id, "person", answer)

    def _check_capacity(self, db):
        pending = db.execute("SELECT count(*) FROM inbox_events WHERE state NOT IN ('completed','ignored','failed','cancelled')").fetchone()[0]
        if pending >= self.max_pending:
            raise RuntimeBusy("runtime v2 inbox capacity reached")

    @staticmethod
    def _take_seq(db, conversation_id):
        row = db.execute("SELECT next_seq FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None:
            raise ValueError("unknown private conversation")
        db.execute("UPDATE conversations SET next_seq=next_seq+1 WHERE conversation_id=?", (conversation_id,))
        return row[0]

    def enqueue_binding(self, conversation_id: str, character: CharacterSnapshot, request_id: str) -> Admission:
        if not request_id.strip():
            raise ValueError("request_id is required")
        payload = json_text({"character_id": character.character_id, "version": character.version, "card_json": character.card_json})
        with self._transaction() as db:
            privacy.require_active(db, conversation_id)
            erased = self._journal.duplicate_source(self.account_id, conversation_id, "binding:" + request_id)
            if erased:
                raise ValueError("此操作属于已删除数据，请使用新的请求 ID")
            old = db.execute("SELECT * FROM inbox_events WHERE conversation_id=? AND source_id=?",
                             (conversation_id, "binding:" + request_id)).fetchone()
            if old:
                if old["payload_json"] != payload:
                    raise ValueError("binding request_id was already used with different content")
                return Admission(old["event_id"], old["seq"], True)
            self._check_capacity(db)
            seq = self._take_seq(db, conversation_id)
            event_id = uuid.uuid4().hex
            db.execute("INSERT INTO inbox_events(event_id,conversation_id,seq,source_id,kind,payload_json,received_at) "
                       "VALUES (?,?,?,?, 'binding', ?,?)", (event_id, conversation_id, seq, "binding:" + request_id, payload, time.time()))
            return Admission(event_id, seq, False)

    def ready_conversations(self, limit: int = 16) -> list[str]:
        with self._lock:
            self._check_privacy()
            rows = self._db.execute("""SELECT e.conversation_id FROM inbox_events e WHERE
                e.state IN ('queued','outbox_ready') AND e.seq=(SELECT min(p.seq) FROM inbox_events p
                WHERE p.conversation_id=e.conversation_id AND p.state NOT IN ('completed','ignored','failed','cancelled'))
                ORDER BY e.received_at, e.seq LIMIT ?""", (limit,)).fetchall()
            return [row[0] for row in rows]

    def enqueue_memory_change(self, conversation_id, character_id, source_event_id, change, request_id, actor, note):
        change = memories.validate_change(change)
        if not all(isinstance(v, str) and v.strip() for v in (character_id, source_event_id, request_id, actor, note)):
            raise ValueError("character, source, request ID, actor and note are required")
        if len(note) > 1000 or len(request_id) > 100:
            raise ValueError("memory note or request ID is too long")
        payload = json_text({"character_id": character_id, "source_event_id": source_event_id,
            "change": change, "actor": actor, "note": note})
        with self._transaction() as db:
            privacy.require_active(db, conversation_id)
            old = db.execute("SELECT * FROM inbox_events WHERE conversation_id=? AND source_id=?",
                             (conversation_id, "memory:" + request_id)).fetchone()
            if old:
                if old["payload_json"] != payload:
                    raise ValueError("memory request_id was already used with different content")
                return Admission(old["event_id"], old["seq"], True)
            self._check_capacity(db)
            seq = self._take_seq(db, conversation_id)
            _, texts = memories.source_text(db, source_event_id, conversation_id, character_id, before_seq=seq)
            if not any(change["quote"] in part for part in texts):
                raise memories.InvalidMemoryChange("evidence quote is not in the source message")
            event_id = uuid.uuid4().hex
            db.execute("INSERT INTO inbox_events(event_id,conversation_id,seq,source_id,kind,payload_json,received_at) "
                "VALUES(?,?,?,?, 'memory', ?,?)", (event_id, conversation_id, seq, "memory:" + request_id, payload, time.time()))
            followups.reconcile(db, self.clock(), self.followups_allowed, conversation_id)
            return Admission(event_id, seq, False)

    def next_work(self, conversation_id: str, config_version: str) -> dict | None:
        with self._transaction() as db:
            episode_times.sync(db, self.clock(), conversation_id)
            current = db.execute("SELECT data_generation,privacy_state FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if not current or current["privacy_state"] != "active":
                return None
            row = db.execute("SELECT * FROM inbox_events WHERE conversation_id=? AND state NOT IN "
                             "('completed','ignored','failed','cancelled') ORDER BY seq LIMIT 1", (conversation_id,)).fetchone()
            if row is None or row["state"] not in {"queued", "outbox_ready"}:
                return None
            item = dict(row)
            if row["kind"] == "reminder_due":
                outbox = reminders.prepare(db, row, now=self.clock(), enabled=self.reminders_allowed, config_version=config_version)
                return {**item, "kind": "reminder_ready" if outbox else "reminder_skipped", "outbox": outbox}
            if row["kind"] == "followup_due":
                outbox = followups.prepare(db, row, now=self.clock(), enabled=self.followups_allowed, config_version=config_version)
                return {**item, "kind": "followup_ready" if outbox else "followup_skipped", "outbox": outbox}
            if row["kind"] == "memory":
                payload = json.loads(row["payload_json"])
                try:
                    memories.apply_change(db, conversation_id=conversation_id, character_id=payload["character_id"],
                        event_id=row["event_id"], seq=row["seq"], source_id=payload["source_event_id"],
                        change=payload["change"], actor=payload["actor"], note=payload["note"])
                except memories.InvalidMemoryChange:
                    db.execute("UPDATE inbox_events SET state='failed',error_code='memory_revision_conflict' WHERE event_id=?", (row["event_id"],))
                else:
                    db.execute("UPDATE inbox_events SET state='completed' WHERE event_id=?", (row["event_id"],))
                    followups.reconcile(db, self.clock(), self.followups_allowed, conversation_id)
                    followups.discover(db, self.clock())
                return {**item, "kind": "memory_applied"}
            if row["kind"] == "binding":
                card = json.loads(row["payload_json"])
                db.execute("UPDATE conversations SET character_id=?,character_version=?,card_json=?,generation=generation+1 "
                           "WHERE conversation_id=?", (card["character_id"], card["version"], card["card_json"], conversation_id))
                db.execute("UPDATE inbox_events SET state='completed' WHERE event_id=?", (row["event_id"],))
                return {**item, "kind": "binding_applied"}
            turn = db.execute("SELECT * FROM turns WHERE event_id=?", (row["event_id"],)).fetchone()
            if turn is None:
                binding = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
                db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,'generating',1)",
                           (uuid.uuid4().hex, row["event_id"], binding["character_id"], binding["character_version"],
                            binding["card_json"], binding["generation"], config_version))
                turn = db.execute("SELECT * FROM turns WHERE event_id=?", (row["event_id"],)).fetchone()
                event = self._envelope(row).inbound()
                db.execute("INSERT INTO messages VALUES(?,'user',?,?,?,?, 'received')",
                           (row["event_id"], conversation_id, row["seq"], turn["character_id"], event.to_memory_text()))
            elif row["state"] == "queued":
                db.execute("INSERT INTO audit_events(created_at,action,target_id,note) VALUES(?, 'resume_generation', ?, ?)",
                           (time.time(), turn["turn_id"], json_text({"previous_config": turn["config_version"], "config": config_version})))
                db.execute("UPDATE turns SET attempts=attempts+1,state='generating',config_version=? WHERE turn_id=?", (config_version, turn["turn_id"]))
                turn = db.execute("SELECT * FROM turns WHERE event_id=?", (row["event_id"],)).fetchone()
            if row["state"] == "queued":
                db.execute("UPDATE inbox_events SET state='generating' WHERE event_id=?", (row["event_id"],))
            memory_snapshot = db.execute("SELECT snapshot_json FROM turn_memory_snapshots WHERE turn_id=?", (turn["turn_id"],)).fetchone()
            if memory_snapshot is None:
                memory_json = json_text(memories.recall_snapshot(db, conversation_id, turn["character_id"], self._envelope(row).inbound().plain_text))
                db.execute("INSERT INTO turn_memory_snapshots VALUES(?,?)", (turn["turn_id"], memory_json))
            else:
                memory_json = memory_snapshot[0]
            item["context"] = TurnContext(turn["turn_id"], row["event_id"], row["seq"],
                ConversationBinding(self.account_id, conversation_id,
                    CharacterSnapshot(turn["character_id"], turn["character_version"], turn["card_json"]), turn["binding_generation"]),
                turn["config_version"], memory_json, current["data_generation"])
            item["event"] = self._envelope(row).inbound()
            outbox = db.execute("SELECT * FROM outbox_messages WHERE event_id=?", (row["event_id"],)).fetchone()
            item["outbox"] = dict(outbox) if outbox else None
            return item

    def _envelope(self, row):
        return EventEnvelope(row["event_id"], self.account_id, row["conversation_id"],
                             row["source_id"] or "", row["received_at"], row["payload_json"])

    def history(self, context: TurnContext, limit: int = 16) -> list[HistoryMessage]:
        with self._lock:
            self._check_privacy()
            privacy.require_current(self._db, context)
            rows = self._db.execute("SELECT m.*,e.payload_json AS event_payload,e.received_at,e.kind AS event_kind,t.card_json AS character_json,"
                                    "d.response_text AS dialogue_response,EXISTS(SELECT 1 FROM memory_revisions r "
                                    "WHERE r.change_event_id=m.event_id AND r.actor='extractor:reply-memory-v1') AS extracted_memory "
                                    "FROM messages m JOIN inbox_events e USING(event_id) JOIN turns t USING(event_id) "
                                    "LEFT JOIN episode_time_dialogue_turns d USING(event_id) "
                                    "WHERE m.conversation_id=? AND m.character_id=? AND m.seq<? "
                                    "ORDER BY m.seq DESC, CASE m.role WHEN 'assistant' THEN 0 ELSE 1 END LIMIT ?",
                                    (context.binding.conversation_id, context.binding.character.character_id,
                                     context.conversation_seq, max(0, min(limit, 100)))).fetchall()
            result = []
            for row in reversed(rows):
                payload = json.loads(row["event_payload"])
                is_user = row["role"] == "user"
                content = row["content"]
                if not is_user and row["extracted_memory"] and row["dialogue_response"]:
                    content = content.removesuffix("\n\n" + row["dialogue_response"])
                result.append(HistoryMessage(row["role"], content, row["event_id"], row["seq"],
                    speaker_id=payload.get("sender_id", "") if is_user else "character:" + row["character_id"],
                    speaker_name=payload.get("sender_name", "") if is_user else json.loads(row["character_json"]).get("assistant_name", ""),
                    source_message_id=payload.get("message_id", "") if is_user else "",
                    occurred_at=payload.get("occurred_at") if is_user else None, received_at=row["received_at"],
                    segments_json=json_text(payload.get("segments", [])) if is_user else "[]",
                    conversation_id=row["conversation_id"], character_id=row["character_id"], delivery_state=row["delivery_state"], event_kind=row["event_kind"]))
            return result

    def record_context(self, context: TurnContext, report: dict):
        with self._transaction() as db:
            privacy.require_current(db, context)
            row = db.execute("SELECT event_id,character_id,state FROM turns WHERE turn_id=?", (context.turn_id,)).fetchone()
            if (not row or row["event_id"] != context.event_id or row["character_id"] != context.binding.character.character_id
                    or row["state"] != "generating"):
                raise ValueError("context report does not belong to an active generation")
            db.execute("INSERT INTO turn_contexts VALUES(?,?,?) ON CONFLICT(turn_id) DO UPDATE SET "
                       "report_json=excluded.report_json,updated_at=excluded.updated_at",
                       (context.turn_id, json_text(report), time.time()))

    def save_proposal(self, context: TurnContext, message: OutboundMessage, memory_changes=()) -> dict:
        if message.launcher_type != "person" or message.launcher_id != context.binding.conversation_id:
            raise ValueError("reply target does not match turn context")
        if message.images:
            raise ValueError("runtime v2 first slice supports text output only")
        payload = json_text({"launcher_type": "person", "launcher_id": message.launcher_id, "text": message.text, "images": []})
        with self._transaction() as db:
            privacy.require_current(db, context)
            state = db.execute("SELECT state FROM inbox_events WHERE event_id=?", (context.event_id,)).fetchone()
            if not state or state[0] != "generating":
                raise ValueError("turn no longer accepts a proposal")
            if len(memory_changes) > 3:
                raise memories.InvalidMemoryChange("at most three memory changes are allowed per turn")
            seen = set()
            records = []
            visible = {r["memory_id"]: r["version"] for r in json.loads(context.memory_snapshot_json).get("records", [])}
            for index, change in enumerate(memory_changes):
                change = memories.validate_change(change, model=True)
                if change["op"] == "revise":
                    if visible.get(change["memory_id"]) != change["expected_version"] or change["memory_id"] in seen:
                        raise memories.InvalidMemoryChange("model revision must target one visible memory version once")
                    seen.add(change["memory_id"])
                records.append(memories.apply_change(db, conversation_id=context.binding.conversation_id,
                    character_id=context.binding.character.character_id, event_id=context.event_id, seq=context.conversation_seq,
                    source_id=context.event_id, change=change, actor="extractor:reply-memory-v1", note="", model=True, index=index))
            if memory_changes:
                followups.reconcile(db, self.clock(), self.followups_allowed, context.binding.conversation_id)
                followups.discover(db, self.clock())
                if self.followups_allowed(context.binding.conversation_id):
                    question = episode_time_dialogue.offer_after_extraction(db, context, records, self.clock())
                    if question:
                        body = json.loads(payload)
                        body["text"] += "\n\n" + question
                        payload = json_text(body)
            db.execute("INSERT INTO outbox_messages(outbox_id,event_id,turn_id,payload_json) VALUES(?,?,?,?)",
                       (uuid.uuid4().hex, context.event_id, context.turn_id, payload))
            db.execute("UPDATE inbox_events SET state='outbox_ready' WHERE event_id=?", (context.event_id,))
            db.execute("UPDATE turns SET state='awaiting_delivery' WHERE turn_id=?", (context.turn_id,))
            return dict(db.execute("SELECT * FROM outbox_messages WHERE event_id=?", (context.event_id,)).fetchone())

    def end_generation(self, event_id: str, *, ignored: bool = False, error_code: str = ""):
        with self._transaction() as db:
            state = "ignored" if ignored else "failed"
            db.execute("UPDATE inbox_events SET state=?,error_code=? WHERE event_id=? AND state='generating'", (state, error_code, event_id))
            db.execute("UPDATE turns SET state=? WHERE event_id=? AND state='generating'", (state, event_id))

    def begin_dispatch(self, outbox_id: str) -> bool:
        with self._transaction() as db:
            outbox = db.execute("SELECT * FROM outbox_messages WHERE outbox_id=? AND state='pending'", (outbox_id,)).fetchone()
            if outbox is None or outbox["erased"] or not reminders.before_dispatch(db, outbox, now=self.clock(), enabled=self.reminders_allowed):
                return False
            if not followups.before_dispatch(db, outbox, now=self.clock(), enabled=self.followups_allowed):
                return False
            changed = db.execute("UPDATE outbox_messages SET state='dispatching',attempts=attempts+1 WHERE outbox_id=? AND state='pending'", (outbox_id,)).rowcount
            if changed:
                db.execute("INSERT INTO delivery_attempts(outbox_id,attempt,started_at,state) "
                           "SELECT outbox_id,attempts,?,'dispatching' FROM outbox_messages WHERE outbox_id=?", (self.clock(), outbox_id))
                task = reminders.task_for_event(db, outbox["event_id"])
                kind = "reminder"
                if task is None:
                    task, kind = followups.for_event(db, outbox["event_id"]), "followup"
                if task:
                    db.execute("INSERT INTO contact_attempts VALUES(?,?,?,?,?,?)", (task["conversation_id"], outbox_id,
                        outbox["attempts"] + 1, kind, self.clock(), contact_policy.policy(db, task["conversation_id"])["version"]))
            return bool(changed)

    def finish_delivery(self, outbox_id: str, status: str, platform_message_id: str = "", error_code: str = ""):
        if status not in {"accepted", "captured", "unknown", "rejected"}:
            raise ValueError("invalid delivery status")
        if status in {"accepted", "captured"} and status != ("captured" if self.mode == "capture" else "accepted"):
            raise ValueError("delivery receipt mode mismatch")
        with self._transaction() as db:
            row = db.execute("SELECT o.*, e.conversation_id,e.seq,t.character_id FROM outbox_messages o "
                             "JOIN inbox_events e USING(event_id) JOIN turns t USING(turn_id) WHERE o.outbox_id=?", (outbox_id,)).fetchone()
            if row is None or row["state"] != "dispatching":
                raise ValueError("outbox is not dispatching")
            self._commit_delivery(db, row, status, platform_message_id, error_code)

    def _commit_delivery(self, db, row, status, platform_id="", error_code=""):
        completed = status in {"accepted", "captured"}
        db.execute("UPDATE outbox_messages SET state=?,platform_message_id=?,error_code=? WHERE outbox_id=?",
                   (status, platform_id, error_code, row["outbox_id"]))
        db.execute("UPDATE delivery_attempts SET state=?,finished_at=? WHERE outbox_id=? AND attempt=?",
                   (status, time.time(), row["outbox_id"], row["attempts"]))
        erased_rejection = row["erased"] and status == "rejected"
        db.execute("UPDATE inbox_events SET state=?,error_code=? WHERE event_id=?",
                   ("completed" if completed else "cancelled" if erased_rejection else "needs_review", error_code, row["event_id"]))
        db.execute("UPDATE turns SET state=? WHERE turn_id=?", ("finished" if completed else "cancelled" if erased_rejection else "needs_review", row["turn_id"]))
        reminders.delivery_result(db, row["event_id"], status, self.clock())
        followups.delivery_result(db, row["event_id"], status, self.clock())
        if completed and not row["erased"]:
            episode_time_dialogue.delivered(db, row["event_id"])
            message = json.loads(row["payload_json"])
            db.execute("INSERT OR IGNORE INTO messages VALUES(?,'assistant',?,?,?,?,?)",
                       (row["event_id"], row["conversation_id"], row["seq"], row["character_id"], message["text"], status))

    def resolve_delivery(self, outbox_id: str, action: str, note: str, *, expected_attempts=None, expected_state=None):
        if action not in {"confirm_sent", "retry", "discard"} or not note.strip():
            raise ValueError("action and a review note are required")
        with self._transaction() as db:
            row = db.execute("SELECT o.*,e.conversation_id,e.seq,t.character_id FROM outbox_messages o "
                             "JOIN inbox_events e USING(event_id) JOIN turns t USING(turn_id) WHERE o.outbox_id=?", (outbox_id,)).fetchone()
            if not row or row["state"] not in {"unknown", "rejected"}:
                raise ValueError("only unknown or rejected delivery can be reviewed")
            if ((expected_attempts is not None and row["attempts"] != expected_attempts)
                    or (expected_state is not None and row["state"] != expected_state)):
                raise ValueError("delivery changed since inspection; refresh before reviewing")
            task = reminders.task_for_event(db, row["event_id"])
            if action == "retry" and row["erased"]:
                raise ValueError("内容已删除，禁止重试此发送")
            if action == "retry" and task is not None:
                if (task["task_version"] != task["version"] or task["cancel_requested"]
                        or reminders.eligibility(db, task, self.reminders_allowed, self.clock())):
                    raise ValueError("提醒已取消、过期或授权条件变化，不能重试；请核对后放弃此草稿")
                contact = contact_policy.for_task(db, task, self.clock())
                if not contact["allowed"]:
                    raise ValueError("当前联系规则阻止重试：" + contact["reason"])
            followup = followups.for_event(db, row["event_id"])
            if action == "retry" and followup is not None:
                if (followup["candidate_version"] != followup["version"]
                        or followups.intrinsic(db, followup, self.clock(), self.followups_allowed)):
                    raise ValueError("跟进已取消、失效或过期，请核对后放弃此草稿")
                allowed = followups.task_decision(db, followup, self.clock())
                if not allowed["allowed"]:
                    raise ValueError("当前规则阻止跟进重试：" + allowed["reason"])
            if action == "confirm_sent":
                if row["state"] == "rejected":
                    raise ValueError("a known rejection cannot be confirmed as sent")
                self._commit_delivery(db, row, "captured" if self.mode == "capture" else "accepted")
            else:
                db.execute("UPDATE outbox_messages SET state=?,error_code='' WHERE outbox_id=?",
                           ("pending" if action == "retry" else "cancelled", outbox_id))
                db.execute("UPDATE inbox_events SET state=?,error_code='' WHERE event_id=?",
                           ("outbox_ready" if action == "retry" else "cancelled", row["event_id"]))
                db.execute("UPDATE turns SET state=? WHERE turn_id=?", ("awaiting_delivery" if action == "retry" else "cancelled", row["turn_id"]))
                reminders.delivery_result(db, row["event_id"], "pending" if action == "retry" else "cancelled", self.clock())
                followups.delivery_result(db, row["event_id"], "pending" if action == "retry" else "cancelled", self.clock())
            db.execute("INSERT INTO audit_events(created_at,action,target_id,note) VALUES(?,?,?,?)",
                       (time.time(), action, outbox_id, "erased_delivery_review" if row["erased"] else note[:1000]))

    def privacy_status(self, conversation_id):
        with self._lock:
            self._check_privacy()
            result = privacy.preview(self._db, conversation_id)
            records = self._journal.records(conversation_id)
            for record in records:
                record["counts"] = json.loads(record.pop("counts_json"))
            return {**result, "deletions": records, "mode": self.mode}

    def erase_conversation(self, payload, actor):
        cid, request_id = payload.get("conversation_id"), payload.get("request_id")
        if not all(isinstance(v, str) and 0 < len(v) <= 100 for v in (cid, request_id, actor)):
            raise ValueError("conversation, unique request ID and authenticated actor are required")
        if payload.get("confirmed") is not True or payload.get("confirm_conversation_id") != cid:
            raise ValueError("必须确认删除范围并输入此私聊对象的完整 ID")
        canonical = {key: payload.get(key) for key in ("conversation_id", "request_id", "preview_token", "confirmed", "confirm_conversation_id")}
        digest = privacy.fingerprint(actor, "erase", json_text(canonical))
        journal_committed = False
        try:
            with self._transaction() as db:
                record = self._journal.previous(cid, request_id)
                duplicate = record is not None
                if record:
                    if record["digest"] != digest:
                        raise ValueError("删除请求 ID 已用于不同的操作或账号")
                else:
                    status = privacy.preview(db, cid)
                    if status["state"] != "active":
                        raise ValueError("此会话已经删除，不能重复删除")
                    if payload.get("preview_token") != status["preview_token"]:
                        raise ValueError("删除预览已变化，请刷新影响范围后重新确认")
                    journal_committed = True
                    record = self._journal.record(db, cid, request_id, digest, actor, self.clock(), status["counts"], self.account_id)
                    privacy.apply_erasure(db, record)
        except BaseException:
            if journal_committed:
                self._privacy_failed = True
            raise
        # SQLite can retain pages while another reader holds a snapshot. This
        # checkpoint is best-effort, not a secure-media-erasure claim.
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {**self.privacy_status(cid), "duplicate": duplicate, "deletion_generation": record["generation"]}

    def resume_conversation(self, conversation_id, expected_generation, character, actor):
        if not isinstance(actor, str) or not actor.strip() or type(expected_generation) is not int:
            raise ValueError("authenticated actor and data generation are required")
        with self._transaction() as db:
            status = privacy.preview(db, conversation_id)
            if status["data_generation"] != expected_generation or expected_generation <= 1:
                raise ValueError("删除代次已变化，请刷新后重新确认")
            if status["state"] == "active":
                return {"state": "active", "duplicate": True}
            if status["pending_deliveries"]:
                raise ValueError("仍有在途或结果不明的发送，请先完成核对")
            db.execute("UPDATE conversations SET privacy_state='active',character_id=?,character_version=?,card_json=?,"
                "generation=generation+1 WHERE conversation_id=?", (character.character_id, character.version, character.card_json, conversation_id))
            contact_policy.ensure(db, conversation_id, self.clock(), self.reminder_timezone)
            db.execute("INSERT INTO audit_events(created_at,action,target_id,note) VALUES(?, 'privacy_resume', ?, ?)",
                (self.clock(), conversation_id, json_text({"actor": actor, "data_generation": expected_generation})))
        return {"state": "active", "duplicate": False}

    def snapshot(self) -> dict:
        with self._lock:
            self._check_privacy()
            return _snapshot(self._db)

    def delivery_detail(self, outbox_id: str) -> dict:
        with self._lock:
            self._check_privacy()
            result = _delivery_detail(self._db, outbox_id)
            if result.get("reminder"):
                result["contact_decision"] = contact_policy.for_task(self._db, result["reminder"], self.clock())
            if result.get("followup"):
                result["contact_decision"] = followups.task_decision(self._db, result["followup"], self.clock())
            return result

    def conversations(self, *, limit=30, after_id="") -> dict:
        with self._lock:
            self._check_privacy()
            return conversations(self._db, limit=limit, after_id=after_id)

    def conversation_detail(self, conversation_id: str, *, limit=30, before_seq=None) -> dict:
        with self._lock:
            self._check_privacy()
            return conversation_detail(self._db, conversation_id, limit=limit, before_seq=before_seq)

    def memories(self, conversation_id, character_id, *, limit=30, before_id=None):
        with self._lock:
            self._check_privacy()
            return memories.memory_page(self._db, conversation_id, character_id, limit=limit, before_id=before_id)

    def memory_detail(self, conversation_id, character_id, memory_id):
        with self._lock:
            self._check_privacy()
            return memories.memory_detail(self._db, conversation_id, character_id, memory_id)

    @staticmethod
    def inspect(path: str | Path, outbox_id: str = "") -> dict:
        path = Path(path).resolve()
        if not path.is_file():
            if privacy.journal_path(path).exists():
                raise privacy.PrivacyUnavailable("运行库缺失但删除记录仍存在，不能回退到旧运行路径")
            if outbox_id:
                raise ValueError("runtime database does not exist")
            return {"enabled": False, "conversations": [], "outbox": [], "counts": {}}
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            privacy.check_inspection(db, path)
            return _delivery_detail(db, outbox_id) if outbox_id else _snapshot(db)
        finally:
            db.close()


def _snapshot(db):
    return {
        "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
        "meta": dict(db.execute("SELECT key,value FROM runtime_meta")),
        "counts": dict(db.execute("SELECT state,count(*) FROM inbox_events GROUP BY state")),
        "conversations": [dict(r) for r in db.execute("SELECT conversation_id,character_id,character_version,generation FROM conversations ORDER BY conversation_id")],
        "outbox": [dict(r) for r in db.execute("SELECT o.outbox_id,o.event_id,e.conversation_id,o.state,o.attempts,o.platform_message_id,o.error_code "
                    "FROM outbox_messages o JOIN inbox_events e USING(event_id) ORDER BY o.rowid DESC LIMIT 50")],
        "failures": [dict(r) for r in db.execute("SELECT event_id,conversation_id,state,error_code FROM inbox_events WHERE state IN ('failed','needs_review') ORDER BY received_at DESC LIMIT 50")],
        "context_reports": _context_reports(db),
    }


def _context_reports(db):
    if db.execute("PRAGMA user_version").fetchone()[0] < 2:
        return []
    return [{"turn_id": row["turn_id"], "event_id": row["event_id"], "updated_at": row["updated_at"],
             "report": json.loads(row["report_json"])} for row in db.execute(
             "SELECT c.*,t.event_id FROM turn_contexts c JOIN turns t USING(turn_id) ORDER BY c.updated_at DESC LIMIT 20")]


def _delivery_detail(db, outbox_id):
    row = db.execute("SELECT o.*,e.conversation_id,e.seq,t.character_id,t.character_version,t.binding_generation FROM outbox_messages o "
                     "JOIN inbox_events e USING(event_id) JOIN turns t USING(turn_id) WHERE o.outbox_id=?", (outbox_id,)).fetchone()
    if row is None:
        raise ValueError("unknown outbox_id")
    result = dict(row)
    result["message"] = json.loads(result.pop("payload_json"))
    result["delivery_attempts"] = [dict(r) for r in db.execute("SELECT attempt,started_at,finished_at,state FROM delivery_attempts WHERE outbox_id=? ORDER BY attempt", (outbox_id,))]
    result["meta"] = dict(db.execute("SELECT key,value FROM runtime_meta"))
    result["reviews"] = [dict(r) for r in db.execute("SELECT id,created_at,action,note FROM audit_events "
        "WHERE target_id=? AND action IN ('confirm_sent','retry','discard') ORDER BY id DESC LIMIT 100", (outbox_id,))]
    if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
        report = db.execute("SELECT report_json FROM turn_contexts WHERE turn_id=?", (row["turn_id"],)).fetchone()
        result["context_report"] = json.loads(report[0]) if report else None
    if db.execute("PRAGMA user_version").fetchone()[0] >= 4:
        task = reminders.task_for_event(db, row["event_id"])
        result["reminder"] = reminders.public_task(task) if task else None
    if db.execute("PRAGMA user_version").fetchone()[0] >= 7:
        task = followups.for_event(db, row["event_id"])
        result["followup"] = followups.public(task) if task else None
    return result
