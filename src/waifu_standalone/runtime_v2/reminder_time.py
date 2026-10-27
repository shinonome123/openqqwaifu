"""Explicit one-shot times. Never infer a user's zone from the host machine."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_local_time(value, zone_name, fold=None):
    """Resolve a wall clock, including past event times, without scheduling it."""
    if not isinstance(value, str) or not isinstance(zone_name, str) or not zone_name.strip():
        raise ValueError("请提供本地日期时间和明确的 IANA 时区")
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise ValueError("fold must be 0 or 1")
    try:
        zone = ZoneInfo(zone_name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("未知 IANA 时区；请确认时区名称及 tzdata 安装") from None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?", value.strip()):
        raise ValueError("请提供不含偏移的本地日期时间，并单独指定时区")
    try:
        local = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError("请提供 YYYY-MM-DDTHH:MM 和 IANA 时区") from None
    candidates = {}
    for choice in (0, 1):
        instant = local.replace(tzinfo=zone, fold=choice).timestamp()
        if datetime.fromtimestamp(instant, zone).replace(tzinfo=None) == local:
            candidates[choice] = instant
    if not candidates:
        raise ValueError("这个本地时间因夏令时跳转而不存在，请换一个时间")
    if len(set(candidates.values())) > 1 and fold is None:
        raise ValueError("这个时间因夏令时出现两次，请指定 fold=0（第一次）或 fold=1（第二次）")
    due = candidates[fold if fold is not None else min(candidates)]
    return due, zone_name.strip(), datetime.fromtimestamp(due, zone).isoformat()


def resolve_time(value, zone_name, now, fold=None):
    if not isinstance(value, str) or len(value) > 80 or not isinstance(zone_name, str):
        raise ValueError("invalid reminder time")
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise ValueError("fold must be 0 or 1")
    relative = re.fullmatch(r"\+(\d{1,6})(m|h|d)", value.strip())
    zone_name = zone_name.strip() or ("UTC" if relative else "")
    if not zone_name:
        raise ValueError("请提供明确的 IANA 时区，例如 Asia/Shanghai")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("未知 IANA 时区；请确认时区名称及 tzdata 安装") from None
    if relative:
        due = now + int(relative[1]) * {"m": 60, "h": 3600, "d": 86400}[relative[2]]
    else:
        due, _, _ = resolve_local_time(value, zone_name, fold)
    if not now < due <= now + 366 * 86400:
        raise ValueError("提醒时间必须在未来且不超过 366 天")
    return due, zone_name, datetime.fromtimestamp(due, zone).isoformat()


HELP = ("提醒格式：/remind +10m | 喝水；或 /remind 2026-12-01T09:00 Asia/Shanghai | 面试。"
        "查看：/remind list；取消：/remind cancel ID；延期：/remind postpone ID +30m。"
        "也支持“10分钟后提醒我喝水”。请先确认时区；静默时段的单条例外需明确添加 allow-quiet，"
        "例如 /remind +10m Asia/Shanghai allow-quiet | 喝水。仍受暂停和频率上限约束。")


def parse_command(text, default_zone=""):
    natural = re.fullmatch(r"\s*(\d{1,6})\s*(分钟|小时|天)后提醒我\s*(\S.*?)\s*", text, re.S)
    if natural:
        return {"operation": "create", "when": "+" + natural[1] + {"分钟": "m", "小时": "h", "天": "d"}[natural[2]],
                "timezone": default_zone, "text": natural[3]}
    if not re.match(r"^/remind(?:\s|$)", text):
        if re.match(r"^(?:明天|后天|今天|周[一二三四五六日天]).*提醒我", text):
            return {"operation": "help"}
        return None
    value = text[len("/remind"):].strip()
    if value == "list":
        return {"operation": "list"}
    if value.startswith("cancel ") and len(value.split()) == 2:
        return {"operation": "cancel", "task_id": value.split()[1]}
    operation, task_id = "create", ""
    if value.startswith("postpone "):
        parts = value.split(maxsplit=2)
        if len(parts) != 3:
            return {"operation": "help"}
        operation, task_id, value = "postpone", parts[1], parts[2]
    if operation == "create":
        if "|" not in value:
            return {"operation": "help"}
        value, body = value.split("|", 1)
    else:
        body = ""
    parts = value.strip().split()
    if not 1 <= len(parts) <= 4:
        return {"operation": "help"}
    result = {"operation": operation, "when": parts[0], "timezone": default_zone, "task_id": task_id}
    for part in parts[1:]:
        if part == "allow-quiet":
            result["allow_quiet"] = True
        elif part in {"fold=0", "fold=1"}:
            result["fold"] = int(part[-1])
        elif "/" in part or part == "UTC":
            result["timezone"] = part
        else:
            return {"operation": "help"}
    if operation == "create":
        result["text"] = body.strip()
    return result
