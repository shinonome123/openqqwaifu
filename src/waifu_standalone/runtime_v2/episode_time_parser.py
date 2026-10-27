"""Conservative Chinese time proposals anchored to the source's receipt date.

This parser never confirms a time, selects an ambiguous option or schedules work.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .reminder_time import resolve_local_time


PARSER_VERSION = "episode-time-zh-v1"
PERIODS = {"凌晨": "dawn", "早上": "morning", "早晨": "morning", "上午": "morning", "中午": "noon",
           "下午": "afternoon", "傍晚": "evening", "晚上": "night", "夜里": "night"}
DATE = re.compile(r"(?<!\d)(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号])(?!\d)"
                  r"|大后天|后天|明天|今天|昨天|前天|(?:下下|上上|下|上|本|这)?(?:周|星期|礼拜)[一二三四五六日天]"
                  r"|(?:\d{1,2}|[一二两三四五六七八九十]{1,3})天后")
CLOCK = re.compile(r"(?<![\d:])(?:\d{1,2}[:：]\d{2}(?!\d)|(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})点"
                   r"(?:半|整|一刻|三刻|(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})分?)?)(?!\d)")
PERIOD = re.compile("|".join(PERIODS))
VAGUE = re.compile(r"周末|月底|月初|月中|下个月|明年|过几天|过两天|改天|近期|最近|下次|有空|待定|不确定|再说|还没定|过一阵")
UNCERTAIN = re.compile(r"可能|大概|也许|不一定|或者|还是|如果|不是|不在|取消|改[到成期]|原定|本来|暂定|还没定|不确定|左右|前后|之前|之后|以前|以后|最早|最晚")
SOURCE_ZONE = re.compile(r"北京时间|东京时间|纽约时间|当地时间|\b(?:UTC|GMT)\b|[A-Za-z_]+/[A-Za-z_/]+|[+-]\d{2}:\d{2}")


def number(value):
    if value.isdecimal():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", value):
        before, after = value.split("十")
        return digits.get(before, 1) * 10 + digits.get(after, 0)
    raise ValueError("unsupported number")


def dates(value, anchor):
    relative = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3, "昨天": -1, "前天": -2}
    if value in relative:
        return [(anchor + timedelta(days=relative[value])).isoformat()], []
    if value.endswith("天后"):
        offset = number(value[:-2])
        if not 0 <= offset <= 31:
            raise ValueError("relative range")
        return [(anchor + timedelta(days=offset)).isoformat()], []
    week = re.fullmatch(r"(下下|上上|下|上|本|这)?(?:周|星期|礼拜)([一二三四五六日天])", value)
    if week:
        day = "一二三四五六日".index(week[2].replace("天", "日"))
        monday = anchor - timedelta(days=anchor.weekday())
        if week[1] is None:
            return [(monday + timedelta(days=day + offset)).isoformat() for offset in (0, 7)], ["weekday_ambiguous"]
        shift = {"上上": -2, "上": -1, "本": 0, "这": 0, "下": 1, "下下": 2}[week[1]]
        return [(monday + timedelta(days=day + 7 * shift)).isoformat()], []
    absolute = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if absolute:
        return [date(*map(int, absolute.groups())).isoformat()], []
    absolute = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]", value)
    if absolute[1]:
        return [date(*map(int, absolute.groups())).isoformat()], []
    options = []
    for year in (anchor.year, anchor.year + 1):
        try:
            options.append(date(year, int(absolute[2]), int(absolute[3])).isoformat())
        except ValueError:
            pass
    if not options:
        raise ValueError("invalid date")
    return options, ["year_missing"]


def clocks(value, period):
    if ":" in value or "：" in value:
        hour, minute = map(int, value.replace("：", ":").split(":"))
        ambiguous = False
    else:
        hour_text, rest = value.split("点")
        hour = number(hour_text)
        minute = {"": 0, "整": 0, "半": 30, "一刻": 15, "三刻": 45}.get(rest)
        if minute is None:
            minute = number(rest.removesuffix("分"))
        ambiguous = not period and 1 <= hour <= 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid clock")
    if period in {"afternoon", "evening", "night"} and 1 <= hour <= 11:
        hour += 12
    elif period == "noon" and hour in (1, 2):
        hour += 12
    if (period in {"dawn", "morning"} and hour >= 12 or period == "night" and hour == 12
            or period in {"afternoon", "evening", "night"} and hour < 12):
        raise ValueError("period/clock conflict")
    hours = ([0, 12] if hour == 12 else [hour, hour + 12]) if ambiguous else [hour]
    return [f"{h:02d}:{minute:02d}" for h in hours], ["clock_ambiguous"] if ambiguous else []


def extract(quote, source_received_at, timezone):
    result = {"parser_version": PARSER_VERSION, "anchor": "source_received_at_in_confirmed_timezone", "source_received_at": source_received_at,
              "timezone": timezone, "source_local_date": "", "mentions": [], "options": [], "reasons": [], "requires_confirmation": True}
    matches = sorted([(m.start(), m.end(), m.group(), kind) for kind, pattern in (("date", DATE), ("clock", CLOCK), ("period", PERIOD), ("vague", VAGUE))
                      for m in pattern.finditer(quote)])
    result["mentions"] = [{"start": start, "end": end, "text": value, "kind": kind} for start, end, value, kind in matches[:12]]
    reasons = result["reasons"]
    if len(matches) > 12:
        reasons.append("too_many_mentions")
    if not matches:
        reasons.append("no_time_expression")
    if VAGUE.search(quote):
        reasons.append("vague_expression")
    if UNCERTAIN.search(quote):
        reasons.append("uncertain_language")
    if SOURCE_ZONE.search(quote):
        reasons.append("timezone_in_source")
    try:
        zone = ZoneInfo(timezone)
        anchor = datetime.fromtimestamp(source_received_at, zone).date()
        result["source_local_date"] = anchor.isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        reasons.append("missing_timezone")
        return result
    if "timezone_in_source" in reasons or "too_many_mentions" in reasons:
        return result
    date_mentions = [m[2] for m in matches if m[3] == "date"]
    clock_mentions = [m[2] for m in matches if m[3] == "clock"]
    period_mentions = [PERIODS[m[2]] for m in matches if m[3] == "period"]
    if not date_mentions:
        if clock_mentions or period_mentions:
            reasons.append("date_missing")
        return result
    if len(date_mentions) > 1 or len(clock_mentions) > 1 or len(period_mentions) > 1:
        reasons.append("multiple_mentions")
        return result  # No guesses about which date, clock or interval belongs together.
    try:
        date_options, extra = dates(date_mentions[0], anchor)
        reasons.extend(extra)
    except ValueError:
        reasons.append("invalid_date")
        return result
    period = period_mentions[0] if period_mentions else ""
    clock_options = [""]
    if clock_mentions:
        try:
            clock_options, extra = clocks(clock_mentions[0], period)
            reasons.extend(extra)
        except ValueError:
            reasons.append("invalid_clock")
            return result
    for day in date_options:
        for clock in clock_options:
            option = {"date": day, "clock": clock, "period": period if not clock else "", "precision": "minute" if clock else "period" if period else "date", "fold": None}
            if clock:
                try:
                    resolve_local_time(day + "T" + clock, timezone)
                except ValueError:
                    try:
                        resolve_local_time(day + "T" + clock, timezone, 0)
                    except ValueError:
                        reasons.append("dst_gap")
                        continue
                    reasons.append("dst_fold")
                    result["options"].extend({**option, "fold": fold} for fold in (0, 1))
                    continue
            result["options"].append(option)
    result["reasons"] = list(dict.fromkeys(reasons))
    return result


def confirm_value(payload, timezone):
    precision, value = payload.get("precision"), payload.get("date")
    if precision not in ("date", "period", "minute") or not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("请确认经历日期和精度")
    try:
        date.fromisoformat(value)
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("请提供有效日期和此会话已确认的 IANA 时区") from None
    period, clock, fold = payload.get("period", ""), payload.get("clock", ""), payload.get("fold")
    if precision == "period" and period not in tuple(set(PERIODS.values())):
        raise ValueError("请选择明确的经历时段")
    if precision != "period" and period or precision != "minute" and (clock or fold is not None):
        raise ValueError("不能为日期或时段补造具体钟点")
    result = {"precision": precision, "date": value, "period": period, "clock": clock, "timezone": timezone, "fold": fold}
    if precision == "minute":
        if not isinstance(clock, str) or not re.fullmatch(r"\d{2}:\d{2}", clock):
            raise ValueError("请提供 HH:MM 经历时间")
        result["utc_at"], _, result["local_time"] = resolve_local_time(value + "T" + clock, timezone, fold)
    return result
