"""She speaks first: what she was asked to remember, and now and then a word to people she knows.

Reminders. The model has a ``reminder`` tool: "明天早上九点叫我起床" or "回头问问我考得怎么样"
becomes a note with a time, in this chat. When it is due she says it in her own words, at
that time, whatever the hour: the person chose it. A chat keeps at most
``companion.reminders_per_chat`` of them.

Watches. The ``watch`` tool keeps an eye on something for someone ("帮我盯着影之刃零的
发售日"): every few hours, within ``greeting_hours``, she searches for it, the chat model
compares what came up with what she already knew, and only real news is told in the chat.
The first search only learns what is already known. A watch ends after the days it was
set for (at most ``watch_max_days``).

Greetings (``companion.greetings_enabled``). Every few minutes she looks for one chat to
open herself, and keeps to these rules:

- only between ``greeting_hours`` local time, and never while a tavern game or a bowl of
  海龟汤 runs there, or after a group told her to be quiet;
- a private chat: someone she is at least ``person_min_stage`` with, silent for
  ``person_idle_hours``, at most once a day;
- a group: quiet for ``group_quiet_hours``, at most ``group_daily_limit`` times a day;
- no more than ``daily_limit`` of these a day in all;
- nobody answered the last one: she waits ``unanswered_wait_hours``; after
  ``unanswered_stop`` in a row she waits until someone there speaks first;
- an eligible chat is not opened the minute it becomes eligible, but by chance over the
  next hour or so, and the model may still decide to say nothing.

What she said, and when chats last heard from someone, live in
``<data_root>/companion/state.json`` so a restart keeps them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..infra.clock import local_now
from ..models import EmotionState, InboundEvent, MessageSegment, OutboundMessage
from ..skills.tool_registry import ToolExecutionResult, ToolExposureContext, ToolInvocation
from ..systems.value_game import ValueGameEngine
from .listener import is_silence
from .stickers import strip_sticker_tags

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

_TICK_SECONDS = 60.0
# A tick opens an eligible chat with this chance, so it happens some time in the next hour or so.
_GREET_CHANCE = 1 / 30
# Reminders further out than this are refused; a year is more than any chat remembers.
_MAX_AHEAD_SECONDS = 366 * 86400.0
# A reminder this overdue (the bot was down) is still said, saying it is late; older ones are dropped.
_LATE_LIMIT_SECONDS = 12 * 3600.0
_STAGES = ("cold", "new", "warming", "familiar", "close", "devoted")
_OPENED_KEPT = 20
_CUE = "（对方这会儿没有说话。）"
_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M")

REMINDER_TOOL_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "list", "cancel"],
            "description": "add 记下一件到点要说的事；list 看这个聊天里记着的；cancel 取消一件。",
        },
        "at": {
            "type": "string",
            "description": "add 时到点的本地时间，格式 YYYY-MM-DD HH:MM（照提示里的当前时间推算）。",
        },
        "in_minutes": {
            "type": "integer",
            "description": "add 时也可以只给从现在起过几分钟（「半小时后」写 30），给了就不用 at。",
        },
        "note": {
            "type": "string",
            "description": "add 时到点要说或要问的事，用你自己能看懂的话写清楚，例如「叫闵闵起床去面试」「问问他考试考得怎么样」。",
        },
        "id": {"type": "string", "description": "cancel 时要取消的那件事的编号（list 里能看到）。"},
    },
    "required": ["action"],
}
REMINDER_TOOL_DESCRIPTION = (
    "记下到某个时间要主动跟对方说的事，到点了你会自己在这个聊天里说。对方让你提醒、叫他、"
    "到时候告诉他，或者你想回头关心一件对方提过的事（考试、面试、看病），就用它。"
)


WATCH_TOOL_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "list", "cancel"],
            "description": "add 开始帮对方盯一件事；list 看这个聊天里在盯的；cancel 不盯了。",
        },
        "topic": {"type": "string", "description": "add 时要盯的事和对方关心什么，例如「影之刃零的发售日和首发评价」。"},
        "query": {"type": "string", "description": "add 时用来搜索的关键词，例如「影之刃零 发售日」。"},
        "every_hours": {"type": "number", "description": "add 时隔几个小时查一次，默认 12。"},
        "days": {"type": "integer", "description": "add 时盯几天，默认 14。"},
        "id": {"type": "string", "description": "cancel 时要停掉的那件事的编号（list 里能看到）。"},
    },
    "required": ["action"],
}
WATCH_TOOL_DESCRIPTION = (
    "帮对方长期盯着一件会变的事（发售日、比赛结果、某个新闻的进展、价格），你会隔一阵自己去查，"
    "有新消息才在这个聊天里告诉对方。对方说「帮我盯着」「有消息告诉我」「出了通知我」时用；"
    "只是现在想知道的事直接搜，不用它。"
)
_WATCH_DEFAULT_HOURS = 12.0
_WATCH_DEFAULT_DAYS = 14
# The first search of a watch, soon after it is set, only learns what is already out there.
_WATCH_FIRST_CHECK_SECONDS = 300.0
_WATCHES_PER_TICK = 2
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_WATCH_JUDGE = (
    "你在帮一个人长期盯着一件事，判断这次搜到的内容里有没有对方不知道的新消息。\n"
    "只有确实是新的、和这件事有关的进展才算（新的日期、结果、公告、数字变化）；"
    "旧闻、重复的说法、无关的内容、猜测和营销都不算。\n"
    "只回一个 JSON：{\"news\": true 或 false, \"what\": \"新消息是什么，一两句话，没有就空\", "
    "\"source\": \"新消息出处的链接，没有就空\", \"known\": \"到目前为止知道的情况，200 字以内\"}"
)


@dataclass(slots=True)
class Watch:
    id: str
    launcher_type: str
    launcher_id: str
    user_id: str
    user_name: str
    topic: str
    query: str
    every_seconds: float
    next_at: float
    until: float
    # What she knows about it so far; empty until the first search.
    known: str = ""
    created_at: float = field(default_factory=time.time)
    asked: str = ""


@dataclass(slots=True)
class Reminder:
    id: str
    launcher_type: str
    launcher_id: str
    user_id: str
    user_name: str
    note: str
    due_at: float
    created_at: float = field(default_factory=time.time)
    # What the person said when they asked for it.
    asked: str = ""


@dataclass(slots=True)
class ChatState:
    # When someone other than her last spoke there.
    heard_at: float = 0.0
    # When she spoke there first, newest last.
    opened: list[float] = field(default_factory=list)
    # Openings in a row that nobody answered.
    unanswered: int = 0
    # The person a private chat is with, or the last speaker in a group, and their name.
    user_id: str = ""
    user_name: str = ""


class Companion:
    def __init__(self, service: WaifuService, *, rng: random.Random | None = None) -> None:
        self._service = service
        self._rng = rng or random.Random()
        self._path = Path(service.config.data_root) / "companion" / "state.json"
        self._reminders: list[Reminder] = []
        self._watches: list[Watch] = []
        self._chats: dict[str, ChatState] = {}
        self._dirty = False
        self._task: asyncio.Task[None] | None = None
        # Tests turn the clock off and call ``tick`` themselves.
        self.autostart = True
        self._load()

    @property
    def settings(self):  # type: ignore[no-untyped-def]
        return self._service.config.companion

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the clock in the running event loop, once; called with every inbound message."""
        if not self.autostart or (self._task is not None and not self._task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # the loop is already closed
        self._save()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("companion tick failed")

    async def tick(self, now: float | None = None) -> None:
        """Say what is due, then maybe open one chat."""
        now = time.time() if now is None else now
        if self.settings.reminders_enabled:
            for reminder in self._due(now):
                await self._remind(reminder, now)
        if self.settings.watches_enabled:
            await self._check_watches(now)
        if self.settings.greetings_enabled:
            await self._maybe_greet(now)
        self._save()
        if self.settings.diary_enabled:
            await self._service.diary.catch_up(now)

    def note_inbound(self, event: InboundEvent) -> None:
        """Someone spoke in a chat: it answers her last opening, and the clock starts."""
        self.start()
        bot = str(self._service.config.bot_account_id or "").strip()
        if bot and str(event.sender_id) == bot:
            return
        state = self._chats.setdefault(self._key(event.launcher_type, event.launcher_id), ChatState())
        state.heard_at = time.time()
        state.unanswered = 0
        state.user_id, state.user_name = str(event.sender_id), str(event.sender_name or "")
        self._dirty = True

    # ------------------------------------------------------------------
    # The reminder tool
    # ------------------------------------------------------------------
    async def ause_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return self.use_tool(invocation)

    def use_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        if not self.settings.reminders_enabled:
            return ToolExecutionResult(error="disabled", text="提醒功能没开。")
        event = invocation.event
        action = str(invocation.argument("action", "add") or "add").strip().lower()
        mine = [item for item in self._reminders if (item.launcher_type, item.launcher_id) == (event.launcher_type, event.launcher_id)]
        if action == "list":
            if not mine:
                return ToolExecutionResult(text="这个聊天里现在没有记着的事。")
            lines = [f"{item.id}：{self._format_time(item.due_at)} {item.note}（{item.user_name or item.user_id}）" for item in mine]
            return ToolExecutionResult(text="记着的事：\n" + "\n".join(lines))
        if action == "cancel":
            wanted = str(invocation.argument("id", "") or "").strip()
            target = next((item for item in mine if item.id == wanted), None)
            if target is None:
                return ToolExecutionResult(error="not found", text="没找到这件事，可以先 list 看看编号。")
            self._reminders.remove(target)
            self._dirty = True
            self._save()
            return ToolExecutionResult(text=f"取消了：{self._format_time(target.due_at)} {target.note}")
        note = re.sub(r"\s+", " ", str(invocation.argument("note", "") or "")).strip()[:200]
        if not note:
            return ToolExecutionResult(error="missing note", text="要记什么事？note 不能空着。")
        due_at = self._due_at(invocation)
        now = time.time()
        if due_at is None:
            return ToolExecutionResult(error="bad time", text="时间看不懂：at 写成 YYYY-MM-DD HH:MM，或者给 in_minutes。")
        if due_at <= now:
            return ToolExecutionResult(error="past", text=f"{self._format_time(due_at)} 已经过去了，现在是 {self._format_time(now)}。")
        if due_at - now > _MAX_AHEAD_SECONDS:
            return ToolExecutionResult(error="too far", text="最多记一年以内的事。")
        # A retried request, or the model calling twice, must not make her say it twice.
        same = next((item for item in mine if item.note == note and abs(item.due_at - due_at) < 300.0), None)
        if same is not None:
            return ToolExecutionResult(text=f"已经记着了（编号 {same.id}）：{self._format_time(same.due_at)} {note}。不用再记，自然地答应对方就好。")
        if len(mine) >= max(1, int(self.settings.reminders_per_chat)):
            return ToolExecutionResult(error="full", text="这个聊天记着的事太多了，先取消几件再记。")
        reminder = Reminder(
            id=uuid.uuid4().hex[:6],
            launcher_type=event.launcher_type,
            launcher_id=event.launcher_id,
            user_id=str(event.sender_id),
            user_name=str(invocation.address or event.sender_name or ""),
            note=note,
            due_at=due_at,
            asked=event.command_text(self._service.config.bot_account_id).strip()[:200],
        )
        self._reminders.append(reminder)
        self._dirty = True
        self._save()
        _LOGGER.info("reminder set chat=%s:%s at=%s", event.launcher_type, event.launcher_id, self._format_time(due_at))
        return ToolExecutionResult(text=f"记下了（编号 {reminder.id}）：{self._format_time(due_at)} {note}。到点你会自己说，现在只要自然地答应对方。")

    def _due_at(self, invocation: ToolInvocation) -> float | None:
        minutes = invocation.argument("in_minutes")
        if minutes not in (None, ""):
            try:
                return time.time() + max(1.0, float(minutes)) * 60.0
            except (TypeError, ValueError):
                return None
        text = str(invocation.argument("at", "") or "").strip()
        zone = local_now(self._service.config.timezone).tzinfo
        for pattern in _TIME_FORMATS:
            try:
                return datetime.strptime(text, pattern).replace(tzinfo=zone).timestamp()
            except ValueError:
                continue
        return None

    def _format_time(self, at: float) -> str:
        zone = local_now(self._service.config.timezone).tzinfo
        return datetime.fromtimestamp(at, zone).strftime("%Y-%m-%d %H:%M")

    def reminders(self) -> list[Reminder]:
        return list(self._reminders)

    # ------------------------------------------------------------------
    # The watch tool
    # ------------------------------------------------------------------
    async def ause_watch_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return self.use_watch_tool(invocation)

    def use_watch_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        settings = self.settings
        if not settings.watches_enabled:
            return ToolExecutionResult(error="disabled", text="盯事情的功能没开。")
        event = invocation.event
        action = str(invocation.argument("action", "add") or "add").strip().lower()
        mine = [item for item in self._watches if (item.launcher_type, item.launcher_id) == (event.launcher_type, event.launcher_id)]
        if action == "list":
            if not mine:
                return ToolExecutionResult(text="这个聊天里现在没有在盯的事。")
            lines = [
                f"{item.id}：{item.topic}（每 {item.every_seconds / 3600:g} 小时查一次，盯到 {self._format_time(item.until)}）"
                + (f"\n  目前知道：{item.known}" if item.known else "")
                for item in mine
            ]
            return ToolExecutionResult(text="在盯的事：\n" + "\n".join(lines))
        if action == "cancel":
            wanted = str(invocation.argument("id", "") or "").strip()
            target = next((item for item in mine if item.id == wanted), None)
            if target is None:
                return ToolExecutionResult(error="not found", text="没找到这件事，可以先 list 看看编号。")
            self._watches.remove(target)
            self._dirty = True
            self._save()
            return ToolExecutionResult(text=f"不盯了：{target.topic}")
        topic = re.sub(r"\s+", " ", str(invocation.argument("topic", "") or "")).strip()[:120]
        query = re.sub(r"\s+", " ", str(invocation.argument("query", "") or "")).strip()[:80] or topic[:80]
        if not topic:
            return ToolExecutionResult(error="missing topic", text="要盯什么事？topic 不能空着。")
        same = next((item for item in mine if item.query == query or item.topic == topic), None)
        if same is not None:
            return ToolExecutionResult(text=f"已经在盯了（编号 {same.id}）：{same.topic}。自然地答应对方就好。")
        if len(mine) >= max(1, int(settings.watches_per_chat)):
            return ToolExecutionResult(error="full", text="这个聊天在盯的事太多了，先停掉几件再加。")
        hours = self._number(invocation.argument("every_hours"), _WATCH_DEFAULT_HOURS)
        hours = min(72.0, max(float(settings.watch_min_hours), hours))
        days = int(min(float(settings.watch_max_days), max(1.0, self._number(invocation.argument("days"), _WATCH_DEFAULT_DAYS))))
        now = time.time()
        watch = Watch(
            id=uuid.uuid4().hex[:6],
            launcher_type=event.launcher_type,
            launcher_id=event.launcher_id,
            user_id=str(event.sender_id),
            user_name=str(invocation.address or event.sender_name or ""),
            topic=topic,
            query=query,
            every_seconds=hours * 3600.0,
            next_at=now + _WATCH_FIRST_CHECK_SECONDS,
            until=now + days * 86400.0,
            asked=event.command_text(self._service.config.bot_account_id).strip()[:200],
        )
        self._watches.append(watch)
        self._dirty = True
        self._save()
        _LOGGER.info("watch set chat=%s:%s query=%s", event.launcher_type, event.launcher_id, query)
        return ToolExecutionResult(
            text=f"开始盯了（编号 {watch.id}）：{topic}，每 {hours:g} 小时查一次，盯 {days} 天，有新消息你会在这里说。现在只要自然地答应对方。"
        )

    def watches(self) -> list[Watch]:
        return list(self._watches)

    @staticmethod
    def _number(value: object, default: float) -> float:
        try:
            return float(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    async def _check_watches(self, now: float) -> None:
        expired = [item for item in self._watches if item.until <= now]
        if expired:
            self._watches = [item for item in self._watches if item.until > now]
            self._dirty = True
        if not self._greeting_hour(now):
            return
        due = sorted((item for item in self._watches if item.next_at <= now), key=lambda item: item.next_at)
        for watch in due[:_WATCHES_PER_TICK]:
            watch.next_at = now + watch.every_seconds
            self._dirty = True
            try:
                await self._check(watch, now)
            except Exception:
                _LOGGER.exception("watch check failed id=%s", watch.id)

    async def _check(self, watch: Watch, now: float) -> None:
        svc = self._service
        found = await svc.search.asearch_query(watch.query, reason="watch")
        if not found.active:
            return
        results = "\n".join(
            f"- {result.title}（{result.url}）：{result.snippet}" for result in found.results[:6]
        )
        messages = [
            {"role": "system", "content": _WATCH_JUDGE},
            {
                "role": "user",
                "content": (
                    f"要盯的事：{watch.topic}\n"
                    f"之前知道的情况：{watch.known or '（这是第一次查，还什么都不知道）'}\n"
                    f"现在是 {self._format_time(now)}，这次搜「{watch.query}」搜到：\n{results}"
                ),
            },
        ]
        raw = await svc.generator.acomplete_chat(messages, user=f"watch:{watch.launcher_type}:{watch.launcher_id}")
        match = _JSON_OBJECT.search(raw or "")
        try:
            verdict = json.loads(match.group(0)) if match else {}
        except ValueError:
            verdict = {}
        if not isinstance(verdict, dict):
            return
        first = not watch.known
        known = str(verdict.get("known", "") or "").strip()[:300]
        if known:
            watch.known = known
        what = str(verdict.get("what", "") or "").strip()[:300]
        if first or not verdict.get("news") or not what:
            return  # the first search only learns what is already known
        source = str(verdict.get("source", "") or "").strip()
        link = f"出处：{source}（可以附上）。" if source.startswith("http") else ""
        hint = (
            "[盯着的事有消息了]\n"
            f"你之前答应{watch.user_name or '对方'}帮忙盯着：{watch.topic}。刚查到的新消息：{what}。{link}\n"
            f"现在你主动告诉{watch.user_name or '对方'}：照你平时的语气，先说清楚是什么消息，一两句话。"
            "不要说你在执行监控、定时搜索之类的话。"
        )
        await self._speak(watch.launcher_type, watch.launcher_id, watch.user_id, watch.user_name, hint, reason="watch")

    # ------------------------------------------------------------------
    # Saying it
    # ------------------------------------------------------------------
    def _due(self, now: float) -> list[Reminder]:
        due = [item for item in self._reminders if item.due_at <= now]
        if due:
            self._reminders = [item for item in self._reminders if item.due_at > now]
            self._dirty = True
        return sorted(due, key=lambda item: item.due_at)

    async def _remind(self, reminder: Reminder, now: float) -> None:
        late = now - reminder.due_at
        if late > _LATE_LIMIT_SECONDS:
            _LOGGER.info("reminder dropped, %d hours late chat=%s:%s", late // 3600, reminder.launcher_type, reminder.launcher_id)
            return
        lateness = f"你晚了 {int(late // 60)} 分钟才想起来，可以带一句。" if late > 600 else ""
        asked = f"当时对方说的是：「{reminder.asked}」。" if reminder.asked else ""
        hint = (
            "[到点了]\n"
            f"你之前记下了一件事，约在 {self._format_time(reminder.due_at)}：{reminder.note}。{asked}{lateness}\n"
            f"现在你主动开口，直接对{reminder.user_name or '对方'}本人说（用「你」，别像在转告别人）："
            "照你平时的语气，一两句话，自然地提起；是提醒就提醒到，是想关心的事就问一句。"
            "不要说你是在执行提醒、记录或者定时。"
        )
        await self._speak(reminder.launcher_type, reminder.launcher_id, reminder.user_id, reminder.user_name, hint, reason="reminder")

    async def _maybe_greet(self, now: float) -> None:
        settings = self.settings
        if not self._greeting_hour(now) or self._opened_today(now) >= max(0, int(settings.daily_limit)):
            return
        candidates = [item for item in await self._greeting_candidates(now) if self._rng.random() < _GREET_CHANCE]
        if not candidates:
            return
        launcher_type, launcher_id, state, hint = candidates[0]
        sent = await self._speak(launcher_type, launcher_id, state.user_id if launcher_type == "person" else "", state.user_name, hint, reason="greeting")
        state.opened = [*state.opened, now][-_OPENED_KEPT:]
        if sent:
            state.unanswered += 1
        self._dirty = True

    async def _greeting_candidates(self, now: float) -> list[tuple[str, str, ChatState, str]]:
        svc, settings = self._service, self.settings
        only = {str(item).strip() for item in settings.only_ids if str(item).strip()}
        found: list[tuple[str, str, ChatState, str]] = []
        for key, state in self._chats.items():
            launcher_type, _, launcher_id = key.partition(":")
            if only and launcher_id not in only:
                continue
            if not state.heard_at or not self._may_open_again(state, now):
                continue
            if svc.tavern.running(launcher_type, launcher_id) or svc.soup.running(launcher_type, launcher_id):
                continue
            if launcher_type == "person":
                hint = await self._person_hint(launcher_id, state, now)
            elif launcher_type == "group" and settings.greet_groups:
                hint = self._group_hint(launcher_id, state, now)
            else:
                hint = ""
            if hint:
                found.append((launcher_type, launcher_id, state, hint))
        return found

    def _may_open_again(self, state: ChatState, now: float) -> bool:
        if state.unanswered >= max(1, int(self.settings.unanswered_stop)):
            return False
        last = state.opened[-1] if state.opened else 0.0
        if state.unanswered and now - last < float(self.settings.unanswered_wait_hours) * 3600.0:
            return False
        return True

    async def _person_hint(self, user_id: str, state: ChatState, now: float) -> str:
        settings = self.settings
        idle = now - state.heard_at
        if idle < float(settings.person_idle_hours) * 3600.0 or self._opened_since(state, now - 86400.0):
            return ""
        svc = self._service
        member = await asyncio.to_thread(svc.state_store.get_member, group_id="", user_id=user_id, character_id=svc._active_character_id())
        if member is None:
            return ""
        score = ValueGameEngine.effective_score(member.get("affinity_score"), member.get("last_addressed_at"), now)
        stage = ValueGameEngine.bond_stage(score)
        wanted = settings.person_min_stage if settings.person_min_stage in _STAGES else "warming"
        if _STAGES.index(stage) < _STAGES.index(wanted):
            return ""
        return (
            "[你主动开口]\n"
            f"{state.user_name or '对方'}已经 {self._hours(idle)} 没来找你说话了，你想主动找对方说一句。"
            "可以接着上次聊到的事问一句，可以说说你这会儿在干什么、想到了什么，或者随口关心一下；"
            "一两句话，像熟人随手发的消息。别说「好久不见」这类客套，别解释你为什么来找对方，"
            "不要提好感、关系或者你在主动问候。实在没什么想说的，就只回「[不说话]」。"
        )

    def _group_hint(self, group_id: str, state: ChatState, now: float) -> str:
        svc, settings = self._service, self.settings
        idle = now - state.heard_at
        if idle < float(settings.group_quiet_hours) * 3600.0 or svc.listener.keeping_quiet(group_id):
            return ""
        if sum(1 for at in state.opened if at >= now - 86400.0) >= max(0, int(settings.group_daily_limit)):
            return ""
        interests = svc.listener._interests()
        topics = f"你平时爱聊：{'、'.join(interests[:8])}。" if interests else ""
        return (
            "[你主动开口]\n"
            f"群里已经安静了 {self._hours(idle)}，你想主动说点什么让大家聊起来。{topics}"
            "可以抛一个你感兴趣的话题，说说你这会儿想到的事，或者接一下群里之前没聊完的；"
            "一两句话，像群友随口一说，不要点名催人回话，不要说「大家好」「有人吗」这种开场。"
            "不要提你在主动找话题。实在没什么想说的，就只回「[不说话]」。"
        )

    async def _speak(self, launcher_type: str, launcher_id: str, user_id: str, user_name: str, hint: str, *, reason: str) -> bool:
        """Have her write and send one message in a chat nobody is talking in; whether she did."""
        try:
            sent = await self._service._process_companion(
                launcher_type, launcher_id, lambda: self._compose_and_send(launcher_type, launcher_id, user_id, user_name, hint, reason=reason)
            )
        except Exception:
            _LOGGER.exception("companion %s failed chat=%s:%s", reason, launcher_type, launcher_id)
            return False
        return bool(sent)

    async def _compose_and_send(self, launcher_type: str, launcher_id: str, user_id: str, user_name: str, hint: str, *, reason: str) -> bool:
        svc = self._service
        if not svc.generator.llm_ready:
            return False
        character = svc._active_character_id()
        event = InboundEvent(
            launcher_id=launcher_id,
            launcher_type=launcher_type,  # type: ignore[arg-type]
            sender_id=user_id,
            sender_name=user_name,
            segments=[MessageSegment("text", text=_CUE)],
        )
        session = await svc.memory.aload(launcher_id, launcher_type, character_id=character)
        assistant_name = svc.generator.resolve_assistant_name(launcher_type, session) or svc.config.assistant_name
        address = user_name or ("大家" if launcher_type == "group" else "对方")
        if user_id:
            address = await svc._resolve_address_async(event, session) or address
        history_turns = await svc.memory.adialogue_turns(
            launcher_id,
            launcher_type,
            limit=svc.config.history_window_messages,
            max_chars=svc.config.history_window_chars,
            character_id=character,
        )
        speaker_notes: list[str] = []
        if user_id:
            bond = await asyncio.to_thread(
                svc.state_store.get_member,
                group_id=launcher_id if launcher_type == "group" else "",
                user_id=user_id,
                character_id=character,
            )
            tones = svc.cards.character_meta(character).get("tone_by_stage")
            relationship = svc.value_game.relationship_note(bond, address=address, tones=tones)
            if relationship:
                speaker_notes.append(relationship)
        topic = next((content for speaker, content in reversed(history_turns) if speaker != "assistant"), "")
        memory_hints = await asyncio.to_thread(
            svc.knowledge_repository.recall_knowledge,
            scopes=svc.knowledge._knowledge_scopes(event),
            query=topic or hint,
            limit=svc.config.memory_recall_limit,
            character_id=character,
        )
        no_tools = ToolExposureContext(event=event, session=session, address=address, assistant_name=assistant_name, allowed_tool_ids=set())
        reply = await svc.generator.agenerate_reply_message(
            event,
            session,
            EmotionState(),
            assistant_name=assistant_name,
            address_override=address,
            history_turns=history_turns,
            memory_hints=list(memory_hints or []),
            speaker_notes=speaker_notes,
            tool_context=no_tools,
            allow_fallback=False,
            turn_hint=hint,
        )
        text = strip_sticker_tags(reply.text).strip()
        if not text or is_silence(text):
            _LOGGER.info("companion %s: she chose to say nothing chat=%s:%s", reason, launcher_type, launcher_id)
            return False
        message = OutboundMessage(launcher_id=launcher_id, launcher_type=launcher_type, text=text, conversational=True)  # type: ignore[arg-type]
        await svc.emitter.adispatch(message, delay_seconds=0.0)
        svc._record_outbound(message)
        await svc.memory.asave_assistant_message(launcher_id, launcher_type, text, character_id=character)
        if launcher_type == "group" and user_id:
            # Their answer to a reminder counts as talking to her, as after she joins in.
            svc.listener.mark_answered(event, message, reason="chime_in")
        _LOGGER.info("companion %s sent chat=%s:%s", reason, launcher_type, launcher_id)
        return True

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------
    def _greeting_hour(self, now: float) -> bool:
        hours = [int(float(item)) for item in self.settings.greeting_hours[:2] if str(item).strip()] or [10, 22]
        start, end = (hours + [24])[:2]
        hour = datetime.fromtimestamp(now, local_now(self._service.config.timezone).tzinfo).hour
        return start <= hour < end if start <= end else hour >= start or hour < end

    def _opened_today(self, now: float) -> int:
        zone = local_now(self._service.config.timezone).tzinfo
        midnight = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return sum(1 for state in self._chats.values() for at in state.opened if at >= midnight)

    @staticmethod
    def _opened_since(state: ChatState, since: float) -> bool:
        return any(at >= since for at in state.opened)

    @staticmethod
    def _hours(seconds: float) -> str:
        days = seconds / 86400.0
        return f"{int(days)} 天" if days >= 2 else f"{max(1, int(seconds // 3600))} 个小时"

    @staticmethod
    def _key(launcher_type: str, launcher_id: str) -> str:
        return f"{launcher_type}:{launcher_id}"

    def _load(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            _LOGGER.warning("companion state unreadable, starting afresh: %s", self._path)
            return
        fields = Reminder.__dataclass_fields__
        for item in payload.get("reminders", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict):
                try:
                    self._reminders.append(Reminder(**{name: item[name] for name in fields if name in item}))
                except (TypeError, ValueError):
                    continue
        for item in payload.get("watches", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict):
                try:
                    self._watches.append(Watch(**{name: item[name] for name in Watch.__dataclass_fields__ if name in item}))
                except (TypeError, ValueError):
                    continue
        for key, item in (payload.get("chats", {}) if isinstance(payload, dict) else {}).items():
            if isinstance(item, dict):
                self._chats[str(key)] = ChatState(**{name: item[name] for name in ChatState.__dataclass_fields__ if name in item})

    def _save(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        payload = {
            "version": 1,
            "reminders": [asdict(item) for item in self._reminders],
            "watches": [asdict(item) for item in self._watches],
            "chats": {key: asdict(state) for key, state in self._chats.items()},
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix="state", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=1)
            os.replace(temp_name, self._path)
        except OSError:
            self._dirty = True
            _LOGGER.exception("companion state could not be saved")
