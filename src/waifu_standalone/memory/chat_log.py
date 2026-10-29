"""Every line said in a chat, with its time, for looking back over days.

The session history keeps only the last few dozen lines and folds older ones into a
summary, which is right for a reply but useless for "整理一下这周群里聊的游戏". This log
keeps what was said, as said: ``<data_root>/chatlog/<character>/<chat>/<YYYY-MM-DD>.jsonl``,
one line per message, for ``CHAT_LOG_DAYS`` days. Only the ``chat-log`` tool reads it, and
only for the chat the request came from.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..infra.clock import resolve_timezone

_LOGGER = logging.getLogger(__name__)

CHAT_LOG_DAYS = 90
_MAX_TEXT_CHARS = 1000


class ChatLog:
    def __init__(self, root: Path, *, timezone: str) -> None:
        self.root = root
        self._zone = resolve_timezone(timezone)
        self._lock = threading.Lock()
        self._pruned_on = ""

    def append(self, *, character_id: str, launcher_type: str, launcher_id: str, speaker: str, text: str, at: float | None = None) -> None:
        text = str(text or "").strip()
        if not text:
            return
        at = time.time() if at is None else at
        day = datetime.fromtimestamp(at, self._zone).strftime("%Y-%m-%d")
        path = self._chat_dir(character_id, launcher_type, launcher_id) / f"{day}.jsonl"
        line = json.dumps({"at": round(at, 3), "who": str(speaker or "")[:60], "text": text[:_MAX_TEXT_CHARS]}, ensure_ascii=False)
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(line.encode("utf-16", "surrogatepass").decode("utf-16", "replace") + "\n")
            self._prune(day)
        except OSError:
            _LOGGER.exception("chat log write failed")

    def read(
        self,
        *,
        character_id: str,
        launcher_type: str,
        launcher_id: str,
        since: float,
        until: float | None = None,
        keyword: str = "",
        limit: int = 300,
    ) -> list[dict[str, object]]:
        """Lines of one chat between ``since`` and ``until``, oldest first; the newest ``limit`` if more."""
        until = time.time() if until is None else until
        folder = self._chat_dir(character_id, launcher_type, launcher_id)
        words = [word for word in re.split(r"\s+", str(keyword or "").strip().lower()) if word]
        found: list[dict[str, object]] = []
        day = datetime.fromtimestamp(since, self._zone).date()
        last = datetime.fromtimestamp(until, self._zone).date()
        while day <= last:
            path = folder / f"{day:%Y-%m-%d}.jsonl"
            day += timedelta(days=1)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in lines:
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                at = float(item.get("at") or 0.0)
                if not since <= at <= until:
                    continue
                if words and not all(word in f"{item.get('who', '')} {item.get('text', '')}".lower() for word in words):
                    continue
                found.append(item)
        return found[-max(1, int(limit)):]

    def format(self, items: list[dict[str, object]]) -> str:
        lines = []
        for item in items:
            moment = datetime.fromtimestamp(float(item.get("at") or 0.0), self._zone)
            lines.append(f"{moment:%m-%d %H:%M} {item.get('who', '')}：{item.get('text', '')}")
        return "\n".join(lines)

    def _chat_dir(self, character_id: str, launcher_type: str, launcher_id: str) -> Path:
        safe = lambda value: re.sub(r"[^0-9A-Za-z_-]", "_", str(value or "")) or "unknown"  # noqa: E731
        return self.root / safe(character_id or "default") / f"{safe(launcher_type)}_{safe(launcher_id)}"

    def _prune(self, today: str) -> None:
        """Once a day, drop days older than ``CHAT_LOG_DAYS``."""
        if self._pruned_on == today:
            return
        self._pruned_on = today
        cutoff = (datetime.now(self._zone) - timedelta(days=CHAT_LOG_DAYS)).strftime("%Y-%m-%d")
        for path in self.root.glob("*/*/*.jsonl"):
            if path.stem < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass
        for folder in self.root.glob("*/*"):
            if folder.is_dir() and not any(folder.iterdir()):
                shutil.rmtree(folder, ignore_errors=True)
