"""Local wall-clock helpers: the prompt's current time and relative-date rewriting."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Shanghai"
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# Used when the host has no tz database (slim containers, Windows without tzdata).
_FIXED_UTC_OFFSETS = {
    "Asia/Shanghai": 8,
    "Asia/Chongqing": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Macau": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "UTC": 0,
    "Etc/UTC": 0,
}

_DAY_OFFSETS = {"大前天": -3, "前天": -2, "昨天": -1, "今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_DAY_PART_OFFSETS = {"昨": -1, "今": 0, "明": 1}
_DAY_PARTS = {"早": "早上", "晚": "晚上"}
_WEEK_OFFSETS = {"上": -1, "这": 0, "本": 0, "下": 1, "下下": 2}
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}
# Bare "周三" is left alone on purpose: it is ambiguous ("每周三" is a habit, not a date).
# The look-arounds skip 马上周末 / 晚上周三 / 看一下周五 / 这周围 / 这周期; "周天" is not
# a weekday here, so "这周天气" is read as "this week", not Sunday.
_RELATIVE_DATE_PATTERN = re.compile(
    r"(?<![马早晚一以之])(下下|上|这|本|下)个?(?:周|星期|礼拜)"
    r"(?:([一二三四五六日末])|(?![围边全到岁年期]))"
    r"|(大前天|大后天|前天|后天|昨天|今天|明天)"
    r"|([昨今明])([早晚])上?"
)


@lru_cache(maxsize=16)
def resolve_timezone(name: str) -> tzinfo:
    key = str(name or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        hours = _FIXED_UTC_OFFSETS.get(key)
        if hours is None:
            _LOGGER.warning("unknown timezone %r; falling back to UTC+8", key)
            hours = 8
        return timezone(timedelta(hours=hours), key)


def local_now(timezone_name: str) -> datetime:
    return datetime.now(resolve_timezone(timezone_name))


def format_prompt_time(moment: datetime, timezone_name: str) -> str:
    """e.g. ``2026-10-26 周一 12:30（Asia/Shanghai）``."""
    label = str(timezone_name or "").strip() or DEFAULT_TIMEZONE
    return f"{moment:%Y-%m-%d} {_WEEKDAY_NAMES[moment.weekday()]} {moment:%H:%M}（{label}）"


def absolutize_relative_dates(text: str, today: date) -> str:
    """Rewrite 今天/明晚/下周三/这周末 … as absolute dates so stored memories stay true later."""

    def replace(match: re.Match[str]) -> str:
        week_prefix, week_day, day_word, part_day, part = match.groups()
        if day_word:
            return f"{today + timedelta(days=_DAY_OFFSETS[day_word]):%Y-%m-%d}"
        if part_day:
            return f"{today + timedelta(days=_DAY_PART_OFFSETS[part_day]):%Y-%m-%d}{_DAY_PARTS[part]}"
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=_WEEK_OFFSETS[week_prefix])
        if week_day is None:
            return f"{monday:%Y-%m-%d}那一周"
        if week_day == "末":
            return f"{monday + timedelta(days=5):%Y-%m-%d}那个周末"
        target = monday + timedelta(days=_WEEKDAY_INDEX[week_day])
        return f"{target:%Y-%m-%d}（{_WEEKDAY_NAMES[target.weekday()]}）"

    return _RELATIVE_DATE_PATTERN.sub(replace, str(text or ""))
