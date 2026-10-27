"""Read projections for the admin console; never feed these into model history."""
from __future__ import annotations

import json


def page_limit(value) -> int:
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise ValueError("limit must be an integer between 1 and 100")
    result = int(value)
    if not 1 <= result <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    return result


def sequence_cursor(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise ValueError("before_seq must be a positive integer")
    result = int(value)
    if not 1 <= result <= 9223372036854775807:
        raise ValueError("before_seq must be a positive SQLite integer")
    return result


def _binding(row):
    return {key: row[key] for key in ("conversation_id", "character_id", "character_version", "generation")} | {
        "assistant_name": json.loads(row["card_json"]).get("assistant_name", ""),
        "data_generation": row["data_generation"], "privacy_state": row["privacy_state"],
    }


def conversations(db, *, limit=30, after_id="") -> dict:
    limit = page_limit(limit)
    rows = db.execute("SELECT * FROM conversations WHERE conversation_id>? ORDER BY conversation_id LIMIT ?",
                      (after_id, limit + 1)).fetchall()
    result = []
    for row in rows[:limit]:
        counts = dict(db.execute("SELECT state,count(*) FROM inbox_events WHERE conversation_id=? GROUP BY state",
                                 (row["conversation_id"],)))
        last = db.execute("SELECT received_at FROM inbox_events WHERE conversation_id=? ORDER BY seq DESC LIMIT 1",
                           (row["conversation_id"],)).fetchone()
        result.append(_binding(row) | {"counts": counts, "last_received_at": last[0] if last else None})
    return {"enabled": True, "meta": dict(db.execute("SELECT key,value FROM runtime_meta")),
            "conversations": result, "next_after_id": rows[limit - 1]["conversation_id"] if len(rows) > limit else None}


def _user_message(payload, received_at):
    # Text-only previews preserve authors and segment order without exposing
    # base64, local file paths, or automatically fetching remote image URLs.
    segments = []
    for part in payload.get("segments", []):
        kind = part.get("kind", "unknown")
        if kind == "text":
            segments.append({"type": kind, "text": part.get("text", "")})
        elif kind == "mention":
            segments.append({"type": kind, "target": part.get("mention_target", ""),
                             "display": part.get("mention_display", "")})
        else:
            segments.append({"type": kind})
    return {"role": "user", "speaker_id": payload.get("sender_id", ""),
            "speaker_name": payload.get("sender_name", ""), "source_message_id": payload.get("message_id", ""),
            "occurred_at": payload.get("occurred_at"), "received_at": received_at, "segments": segments}


def conversation_detail(db, conversation_id, *, limit=30, before_seq=None) -> dict:
    limit, before_seq = page_limit(limit), sequence_cursor(before_seq)
    binding = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if binding is None:
        raise LookupError("unknown conversation_id")
    rows = db.execute("SELECT * FROM inbox_events WHERE conversation_id=? AND (? IS NULL OR seq<?) "
                      "ORDER BY seq DESC LIMIT ?", (conversation_id, before_seq, before_seq, limit + 1)).fetchall()
    items = []
    for row in reversed(rows[:limit]):
        item = {key: row[key] for key in ("event_id", "seq", "kind", "received_at", "state", "error_code")}
        payload = json.loads(row["payload_json"])
        if row["kind"] == "binding":
            item["binding_change"] = {"character_id": payload["character_id"], "character_version": payload["version"],
                                      "assistant_name": json.loads(payload["card_json"]).get("assistant_name", "")}
        elif row["kind"] == "memory":
            item["memory_change"] = {"character_id": payload["character_id"], "source_event_id": payload["source_event_id"],
                                     "operation": payload["change"]["op"], "title": payload["change"]["title"],
                                     "actor": payload["actor"], "note": payload["note"]}
        else:
            if row["kind"] == "erased":
                item["content_erased"] = True
            elif row["kind"] == "reminder_due":
                item["reminder"] = payload
            elif row["kind"] == "followup_due":
                item["followup"] = payload
            else:
                item["user_message"] = _user_message(payload, row["received_at"])
            turn = db.execute("SELECT * FROM turns WHERE event_id=?", (row["event_id"],)).fetchone()
            item["turn"] = None
            item["assistant_message"] = None
            item["outbox"] = None
            if turn:
                item["turn"] = {key: turn[key] for key in ("turn_id", "character_id", "character_version",
                    "binding_generation", "config_version", "state", "attempts")}
                item["turn"]["assistant_name"] = json.loads(turn["card_json"]).get("assistant_name", "")
                report = db.execute("SELECT report_json FROM turn_contexts WHERE turn_id=?", (turn["turn_id"],)).fetchone()
                item["turn"]["context_report"] = json.loads(report[0]) if report else None
            # Only committed messages are represented as assistant history.
            reply = db.execute("SELECT content,delivery_state FROM messages WHERE event_id=? AND role='assistant'",
                                (row["event_id"],)).fetchone()
            if reply:
                item["assistant_message"] = {"role": "assistant", **dict(reply)}
            outbox = db.execute("SELECT * FROM outbox_messages WHERE event_id=?", (row["event_id"],)).fetchone()
            if outbox:
                item["outbox"] = {key: outbox[key] for key in ("outbox_id", "state", "attempts", "platform_message_id", "error_code")}
                if not reply:
                    item["outbox"]["draft_text"] = json.loads(outbox["payload_json"])["text"]
        items.append(item)
    return {"meta": dict(db.execute("SELECT key,value FROM runtime_meta")), "binding": _binding(binding),
            "items": items, "next_before_seq": rows[limit - 1]["seq"] if len(rows) > limit else None}
