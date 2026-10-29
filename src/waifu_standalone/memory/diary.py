"""What happened in a chat on each day, kept for good: her memory of "那天" and "上个月".

Once a day is over, the chat model writes that day of a chat's log (``memory/chat_log.py``)
into a short diary entry: who talked about what, what was agreed, how it went. Entries live
next to the log in ``digests.json`` and are kept after the log's own days are gone, so she
can still answer "上个月我们聊过什么" in a few lines. The ``recall`` tool reads them by date
and keyword, for the chat the request came from only.

Entries are written a few a minute by the companion clock, oldest unwritten day first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..infra.clock import resolve_timezone
from ..skills.tool_registry import ToolExecutionResult, ToolInvocation
from .chat_log import CHAT_LOG_DAYS

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

# Days with fewer lines than this are kept as they were said.
_SHORT_DAY_LINES = 4
_ENTRIES_PER_TICK = 2
_DAY_CHARS = 24000
_DIGEST_FILE = "digests.json"
_WRITER = (
    "你在替一个 QQ 聊天机器人（她叫{name}）写日记，记下某个聊天里某一天发生的事，方便她以后回想。\n"
    "根据聊天记录，写一份简短的中文摘要：谁聊了什么话题、说了哪些具体的事（打算、经历、喜好、约定、"
    "结果），气氛怎样；她自己说过、答应过的事也记上。人名照记录里的写。\n"
    "分几条写，每条一行，用「- 」开头，最多 8 条，总共不超过 300 字。没什么值得记的就只写一行「- 闲聊」。\n"
    "只写记录里真有的事，不要评价，不要编。"
)

RECALL_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "from_date": {"type": "string", "description": "从哪天开始，YYYY-MM-DD（照提示里的当前时间推算「上个月」「上周三」）。"},
        "to_date": {"type": "string", "description": "到哪天为止，YYYY-MM-DD，默认和 from_date 同一天；不给 from_date 就是最近 30 天。"},
        "keyword": {"type": "string", "description": "可选，只看提到这个词的日子（几个词用空格隔开，要都提到）。"},
    },
}
RECALL_DESCRIPTION = (
    "回想这个聊天以前某段时间发生过的事（你每天记的日记）：对方问「上个月我们聊过什么」「那次我说的…」"
    "「之前谁提过…」，或者你要确认以前的约定时用。只能看当前这个聊天；最近几天的原话可以用 chat-log。"
)


class Diary:
    def __init__(self, service: WaifuService) -> None:
        self._service = service
        self._zone = resolve_timezone(service.config.timezone)
        # Chats whose diary has nothing left to write, until this local date.
        self._done_on: dict[Path, str] = {}

    @property
    def root(self) -> Path:
        return self._service.chat_log.root

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    async def catch_up(self, now: float | None = None, *, limit: int = _ENTRIES_PER_TICK) -> int:
        """Write up to ``limit`` entries for days that are over; how many were written."""
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, self._zone).date()
        oldest = today - timedelta(days=CHAT_LOG_DAYS)
        written = 0
        for folder in sorted(path for path in self.root.glob("*/*") if path.is_dir()):
            if written >= limit:
                break
            if self._done_on.get(folder) == today.isoformat():
                continue
            entries = self._read(folder)
            pending = sorted(
                path for path in folder.glob("*.jsonl")
                if oldest.isoformat() <= path.stem < today.isoformat() and path.stem not in entries
            )
            if not pending:
                self._done_on[folder] = today.isoformat()
                continue
            for path in pending[: limit - written]:
                try:
                    entries[path.stem] = await self._write_day(folder, path)
                except Exception:
                    # The model is down or out of credit: try this chat again tomorrow, not every minute.
                    self._done_on[folder] = today.isoformat()
                    break
                self._store(folder, entries)
                written += 1
        return written

    async def _write_day(self, folder: Path, path: Path) -> str:
        svc = self._service
        try:
            lines = [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]
        except (OSError, ValueError):
            return "- （这天的记录读不出来）"
        name = svc.config.assistant_name
        character = folder.parent.name
        try:
            name = svc.cards.load("group", _session(character)).assistant_name or name
        except Exception:
            pass
        text = self._format(lines, name)
        if len(lines) < _SHORT_DAY_LINES:
            return "\n".join(f"- {line}" for line in text.splitlines())
        messages = [
            {"role": "system", "content": _WRITER.format(name=name)},
            {"role": "user", "content": f"{path.stem} 这一天的聊天记录：\n{text[-_DAY_CHARS:]}"},
        ]
        try:
            entry = await svc.generator.acomplete_chat(messages, user=f"diary:{folder.name}")
        except Exception as exc:
            _LOGGER.warning("diary entry failed day=%s chat=%s: %s", path.stem, folder.name, exc)
            raise
        entry = "\n".join(line.strip() for line in str(entry or "").splitlines() if line.strip())[:600]
        _LOGGER.info("diary entry written day=%s chat=%s", path.stem, folder.name)
        return entry or "- 闲聊"

    def _format(self, lines: list[dict[str, object]], name: str) -> str:
        from ..reply_flow.tasks import ASSISTANT_SPEAKER

        out = []
        for item in lines:
            moment = datetime.fromtimestamp(float(item.get("at") or 0.0), self._zone)
            who = f"{name}" if item.get("who") == ASSISTANT_SPEAKER else str(item.get("who", ""))
            out.append(f"{moment:%H:%M} {who}：{item.get('text', '')}")
        return "\n".join(out)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def entries(self, *, character_id: str, launcher_type: str, launcher_id: str) -> dict[str, str]:
        folder = self._service.chat_log._chat_dir(character_id, launcher_type, launcher_id)
        return self._read(folder)

    async def ause_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_tool, invocation)

    def use_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        svc = self._service
        event = invocation.event
        today = datetime.now(self._zone).date()
        start = _date(invocation.argument("from_date"))
        end = _date(invocation.argument("to_date")) or start
        if start is None:
            start, end = today - timedelta(days=30), today
        if end < start:
            start, end = end, start
        words = [word for word in re.split(r"\s+", str(invocation.argument("keyword", "") or "").strip().lower()) if word]
        entries = self.entries(character_id=svc._active_character_id(), launcher_type=event.launcher_type, launcher_id=event.launcher_id)
        found = [
            (day, text) for day, text in sorted(entries.items())
            if start.isoformat() <= day <= end.isoformat() and all(word in text.lower() for word in words)
        ]
        span = f"{start:%Y-%m-%d}" + ("" if start == end else f" 到 {end:%Y-%m-%d}")
        if not found:
            note = "（日记是从这个功能上线后才开始记的，当天的事第二天才记上；今天和最近几天的原话可以用 chat-log 看。）"
            return ToolExecutionResult(text=f"{span} 这个聊天的日记里没有找到{'提到「' + ' '.join(words) + '」的' if words else ''}记录。{note}")
        body = "\n\n".join(f"{day}（{_WEEKDAYS[date.fromisoformat(day).weekday()]}）\n{text}" for day, text in found[-40:])
        return ToolExecutionResult(text=f"{span} 的日记：\n{body}", metadata={"model_limit": 16000})

    # ------------------------------------------------------------------
    @staticmethod
    def _read(folder: Path) -> dict[str, str]:
        try:
            payload = json.loads((folder / _DIGEST_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}

    @staticmethod
    def _store(folder: Path, entries: dict[str, str]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=folder, prefix="digests", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(dict(sorted(entries.items())), stream, ensure_ascii=False, indent=1)
            os.replace(temp_name, folder / _DIGEST_FILE)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _session(character_id: str):  # type: ignore[no-untyped-def]
    from ..models import SessionMemory

    return SessionMemory(launcher_id="", launcher_type="group", character_id=character_id)
