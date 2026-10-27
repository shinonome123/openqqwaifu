"""Per-conversation authorization and dispatch boundaries."""
from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import json_text


SCHEMA_V6 = (
    """CREATE TABLE contact_policies (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations,
        proactive_enabled INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
        timezone TEXT NOT NULL DEFAULT '', quiet_enabled INTEGER NOT NULL DEFAULT 1,
        quiet_start TEXT NOT NULL DEFAULT '22:00', quiet_end TEXT NOT NULL DEFAULT '08:00',
        daily_limit INTEGER NOT NULL DEFAULT 20, min_interval_minutes INTEGER NOT NULL DEFAULT 1,
        version INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE contact_requests (
        conversation_id TEXT NOT NULL REFERENCES conversations, request_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, actor TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(conversation_id,request_id))""",
    """CREATE TABLE contact_attempts (
        conversation_id TEXT NOT NULL REFERENCES conversations, outbox_id TEXT NOT NULL REFERENCES outbox_messages,
        attempt INTEGER NOT NULL, kind TEXT NOT NULL, started_at REAL NOT NULL, policy_version INTEGER NOT NULL,
        PRIMARY KEY(outbox_id,attempt))""",
    "CREATE INDEX contact_attempts_scope ON contact_attempts(conversation_id,started_at)",
    "ALTER TABLE reminder_tasks ADD COLUMN allow_quiet INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reminder_tasks ADD COLUMN timezone_confirmed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reminder_tasks ADD COLUMN deferred_until REAL",
)


def validate_zone(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError("请提供明确的 IANA 时区，例如 Asia/Shanghai")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("未知 IANA 时区；请确认时区名称及 tzdata 安装") from None
    return value


def ensure(db, conversation_id, now, default_zone=""):
    if default_zone:
        validate_zone(default_zone)
    db.execute("INSERT OR IGNORE INTO contact_policies(conversation_id,timezone,created_at,updated_at) VALUES(?,?,?,?)",
               (conversation_id, default_zone, now, now))


def policy(db, conversation_id):
    row = db.execute("SELECT * FROM contact_policies WHERE conversation_id=?", (conversation_id,)).fetchone()
    if row is None:
        raise LookupError("contact policy is unavailable for this conversation")
    result = dict(row)
    for field in ("proactive_enabled", "paused", "quiet_enabled"):
        result[field] = bool(result[field])
    return result


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("静默时间需为 HH:MM，例如 22:00")
    return int(value[:2]) * 60 + int(value[3:])


def in_quiet(zone, start, end, instant):
    local = datetime.fromtimestamp(instant, ZoneInfo(zone))
    current, first, last = local.hour * 60 + local.minute, minute(start), minute(end)
    return first <= current < last if first < last else current >= first or current < last


@lru_cache(maxsize=2048)
def next_open(zone, start, end, utc_minute):
    # Walk real instants, so skipped/repeated local hours need no guessed fold.
    for offset in range(1, 26 * 60 + 1):
        instant = (utc_minute + offset) * 60
        if not in_quiet(zone, start, end, instant):
            return instant
    return None


def attempts(db, conversation_id, now):
    return [row[0] for row in db.execute("SELECT started_at FROM contact_attempts WHERE conversation_id=? AND started_at>? ORDER BY started_at",
                                        (conversation_id, now - 86400))]


def decision(db, conversation_id, now, *, kind="reminder", allow_quiet=False, task_zone=""):
    p = policy(db, conversation_id)
    result = {"allowed": False, "reason": "", "next_at": None, "policy_version": p["version"]}
    if p["paused"]:
        return {**result, "reason": "contact_paused"}
    if kind != "reminder" and not p["proactive_enabled"]:
        return {**result, "reason": "proactive_disabled"}
    if p["quiet_enabled"] and not allow_quiet:
        zone = p["timezone"] or task_zone
        if not zone:
            return {**result, "reason": "contact_timezone_required"}
        if in_quiet(zone, p["quiet_start"], p["quiet_end"], now):
            return {**result, "reason": "contact_quiet", "next_at": next_open(zone, p["quiet_start"], p["quiet_end"], int(now // 60))}
    used = attempts(db, conversation_id, now)
    if len(used) >= p["daily_limit"]:
        return {**result, "reason": "contact_daily_limit", "next_at": used[-p["daily_limit"]] + 86400}
    if used and now < used[-1] + p["min_interval_minutes"] * 60:
        return {**result, "reason": "contact_interval", "next_at": used[-1] + p["min_interval_minutes"] * 60}
    return {**result, "allowed": True}


def for_task(db, task, now):
    return decision(db, task["conversation_id"], now, allow_quiet=bool(task["allow_quiet"]),
                    task_zone=task["timezone"] if task["timezone_confirmed"] else "")


def check_requested_time(db, conversation_id, due, zone, allow_quiet):
    if type(allow_quiet) is not bool:
        raise ValueError("allow_quiet 必须是布尔值")
    p = policy(db, conversation_id)
    if p["quiet_enabled"] and not allow_quiet:
        zone = p["timezone"] or zone
        if not zone:
            raise ValueError("请先用 /contact timezone Asia/Shanghai 确认时区，或在提醒中明确指定时区")
        if in_quiet(zone, p["quiet_start"], p["quiet_end"], due):
            raise ValueError("指定时间处于静默时段；如确需此时提醒，请为这条提醒明确添加 allow-quiet")


def detail(db, conversation_id, now):
    p = policy(db, conversation_id)
    used = attempts(db, conversation_id, now)
    audit = []
    for row in db.execute("SELECT * FROM contact_requests WHERE conversation_id=? ORDER BY rowid DESC LIMIT 30", (conversation_id,)):
        payload, response = json.loads(row["payload_json"]), json.loads(row["response_json"])
        audit.append({"actor": row["actor"], "created_at": row["created_at"], "operation": payload["operation"],
                      "changes": payload.get("changes", {}), "note": payload.get("note", ""),
                      "status": response["status"], "version": response.get("policy", {}).get("version")})
    generation = db.execute("SELECT data_generation FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
    return {"policy": p, "data_generation": generation, "attempts_24h": len(used), "remaining_24h": max(0, p["daily_limit"] - len(used)),
            "reminder_decision": decision(db, conversation_id, now), "proactive_decision": decision(db, conversation_id, now, kind="proactive"),
            "followups_implemented": True, "followup_mode": "reviewed_candidates", "audit": audit}


def apply_request(db, payload, *, actor, now, user=False):
    cid, request_id = payload.get("conversation_id"), payload.get("request_id")
    if not all(isinstance(v, str) and 0 < len(v) <= 100 for v in (cid, request_id, actor)):
        raise ValueError("conversation, unique request ID and authenticated actor are required")
    serialized = json_text(payload)
    old = db.execute("SELECT * FROM contact_requests WHERE conversation_id=? AND request_id=?", (cid, request_id)).fetchone()
    if old:
        if old["payload_json"] != serialized or old["actor"] != actor:
            raise ValueError("request_id was already used with different content")
        return {**json.loads(old["response_json"]), "duplicate": True}
    if not db.execute("SELECT 1 FROM contact_policies WHERE conversation_id=?", (cid,)).fetchone():
        raise ValueError("unknown active conversation")
    p = policy(db, cid)
    operation = payload.get("operation")
    if operation not in ("update", "pause", "resume"):
        raise ValueError("unsupported contact operation")
    if not user:
        if payload.get("authorized") is not True or not isinstance(payload.get("note"), str) or not payload["note"].strip():
            raise ValueError("需要明确授权和操作依据")
        generation = db.execute("SELECT data_generation FROM conversations WHERE conversation_id=?", (cid,)).fetchone()[0]
        if type(payload.get("expected_data_generation")) is not int or payload["expected_data_generation"] != generation:
            raise ValueError("会话数据代次已变化，请刷新后操作")
        # An emergency stop remains valid even when preferences changed meanwhile.
        if operation != "pause" and (type(payload.get("expected_version")) is not int or payload["expected_version"] != p["version"]):
            raise ValueError("联系规则版本已变化，请刷新后操作")
    if len(str(payload.get("note", ""))) > 1000:
        raise ValueError("操作依据最多 1000 字")
    changes = payload.get("changes", {})
    fields = {"proactive_enabled", "timezone", "quiet_enabled", "quiet_start", "quiet_end", "daily_limit", "min_interval_minutes"}
    if not isinstance(changes, dict) or set(changes) - fields or (operation != "update" and changes):
        raise ValueError("invalid contact policy fields")
    for field in ("proactive_enabled", "quiet_enabled"):
        if field in changes and type(changes[field]) is not bool:
            raise ValueError(field + " 必须是布尔值")
    if "timezone" in changes:
        validate_zone(changes["timezone"])
    p.update(changes)
    if p["proactive_enabled"] and not p["timezone"]:
        raise ValueError("开启主动联系前请确认时区，例如 /contact on Asia/Shanghai")
    if minute(p["quiet_start"]) == minute(p["quiet_end"]):
        raise ValueError("静默开始和结束不能相同；全天暂停请使用 /contact pause")
    for field, maximum in (("daily_limit", 100), ("min_interval_minutes", 1440)):
        if type(p[field]) is not int or not 1 <= p[field] <= maximum:
            raise ValueError(f"{field} 必须为 1—{maximum} 的整数")
    if operation in {"pause", "resume"}:
        p["paused"] = operation == "pause"
    fields = sorted(fields | {"paused"})
    db.execute("UPDATE contact_policies SET " + ",".join(f"{field}=?" for field in fields) +
               ",version=version+1,updated_at=? WHERE conversation_id=?", [p[field] for field in fields] + [now, cid])
    response = {"status": "saved", "policy": policy(db, cid), "duplicate": False}
    db.execute("INSERT INTO contact_requests VALUES(?,?,?,?,?,?)", (cid, request_id, serialized, actor, json_text(response), now))
    return response


HELP = ("联系规则：/contact status；暂停所有计划联系（含提醒）：/contact pause；恢复：/contact resume。"
        "主动跟进授权：/contact on Asia/Shanghai 或 /contact off（候选仍需在控制台核对正文与时间）。"
        "时区：/contact timezone Asia/Shanghai；静默：/contact quiet 22:00 08:00 或 /contact quiet off；"
        "每 24 小时最多 5 次且间隔 30 分钟：/contact limit 5 30m。普通聊天回复不受影响。")


def parse_command(text):
    if text.strip() in {"暂停主动联系", "不要主动联系我", "别主动联系我"}:
        return {"operation": "pause"}
    if not re.match(r"^/contact(?:\s|$)", text):
        return None
    parts = text.split()[1:]
    if parts in (["status"], ["pause"], ["resume"]):
        return {"operation": parts[0]}
    changes = None
    if parts == ["off"]:
        changes = {"proactive_enabled": False}
    elif parts[:1] == ["on"] and len(parts) in (1, 2):
        changes = {"proactive_enabled": True, **({"timezone": parts[1]} if len(parts) == 2 else {})}
    elif parts[:1] == ["timezone"] and len(parts) == 2:
        changes = {"timezone": parts[1]}
    elif parts == ["quiet", "off"]:
        changes = {"quiet_enabled": False}
    elif parts[:1] == ["quiet"] and len(parts) == 3:
        changes = {"quiet_enabled": True, "quiet_start": parts[1], "quiet_end": parts[2]}
    elif parts[:1] == ["limit"] and len(parts) == 3 and re.fullmatch(r"\d{1,4}", parts[1]) and re.fullmatch(r"\d{1,4}m", parts[2]):
        changes = {"daily_limit": int(parts[1]), "min_interval_minutes": int(parts[2][:-1])}
    return {"operation": "update", "changes": changes} if changes is not None else {"operation": "help"}


def answer(result):
    if result["status"] == "error":
        return "联系规则未更改：" + result["message"] + "\n" + HELP
    p = result["policy"]
    quiet = f"{p['quiet_start']}–{p['quiet_end']}" if p["quiet_enabled"] else "关闭"
    return (f"联系规则 v{p['version']}：{'已暂停所有计划联系（含提醒）' if p['paused'] else '未暂停'}；"
            f"主动跟进授权{'开启' if p['proactive_enabled'] else '关闭'}（仅发送控制台已核对的经历跟进）。"
            f"时区：{p['timezone'] or '未确认'}；静默：{quiet}；滚动 24 小时最多 {p['daily_limit']} 次，间隔至少 {p['min_interval_minutes']} 分钟。"
            "普通聊天回复不受影响；已开始发送的消息可能仍会到达。")
