"""Sticker library: the model may end a reply with "[表情:分类]" to send one.

Stickers live in ``<data_root>/<stickers.directory>/<category>/`` as image files.
The system message lists the categories only when a sticker may be sent, and a
chat gets at most one sticker every ``min_replies_between`` replies, so the bot
does not answer every line with a picture.
"""

from __future__ import annotations

import base64
import random
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import InboundEvent

if TYPE_CHECKING:
    from ..app import WaifuService

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# Larger files are left out rather than sent: QQ is slow with them and they bloat the action.
_MAX_STICKER_BYTES = 2 * 1024 * 1024
STICKER_TAG = re.compile(r"\[\s*表情\s*[:：]\s*([^\]\n]{1,10}?)\s*\]")


def strip_sticker_tags(text: str) -> str:
    """The text without its "[表情:分类]" tags, which are never sent as text."""
    cleaned = STICKER_TAG.sub("", str(text or ""))
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _closest_category(wanted: str, categories: dict[str, list[Path]]) -> str:
    """The category the model meant: its exact name, else one inside it or containing it ("很开心" → "开心")."""
    if wanted in categories:
        return wanted
    return next((name for name in categories if name in wanted or wanted in name), "")


class StickerLibrary:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        self._lock = threading.Lock()
        # (character, chat type, chat id) -> replies since the last sticker there.
        self._since_last: dict[tuple[str, str, str], int] = {}

    def categories(self) -> dict[str, list[Path]]:
        config = self._service.config
        root = Path(config.data_root) / str(config.stickers.directory or "stickers")
        if not root.is_dir():
            return {}
        found: dict[str, list[Path]] = {}
        for folder in sorted(path for path in root.iterdir() if path.is_dir()):
            files = [
                path
                for path in sorted(folder.iterdir())
                if path.suffix.lower() in _IMAGE_SUFFIXES
                and path.is_file()
                and 0 < path.stat().st_size <= _MAX_STICKER_BYTES
            ]
            if files:
                found[folder.name] = files
        return found

    def _key(self, event: InboundEvent) -> tuple[str, str, str]:
        return (self._service._active_character_id(), str(event.launcher_type), str(event.launcher_id))

    def _may_send(self, event: InboundEvent) -> bool:
        config = self._service.config.stickers
        if not config.enabled:
            return False
        with self._lock:
            since_last = self._since_last.get(self._key(event))
        return since_last is None or since_last >= max(0, int(config.min_replies_between or 0))

    def prompt_section(self, event: InboundEvent) -> str:
        """The system-message note offering stickers, or "" when none may be sent now."""
        if not self._may_send(event):
            return ""
        names = list(self.categories())
        if not names:
            return ""
        return (
            "[表情包]\n"
            "想表达情绪时，可以在回复最后单独一行写「[表情:分类]」，会替你发一张那种表情。"
            f"分类只能从这些里选：{'、'.join(names)}。\n"
            "不用每次都发，觉得合适再发，一条回复最多一个。"
        )

    def take(self, text: str, event: InboundEvent) -> tuple[str, str]:
        """Settle the sticker tags in a reply: returns its text and a sticker image to send.

        When a sticker is sent the text ends with its tag, so the chat history shows
        it; otherwise every tag is dropped.
        """
        tags = [tag.strip() for tag in STICKER_TAG.findall(str(text or ""))]
        cleaned = strip_sticker_tags(text)
        category, sticker = "", ""
        if tags and self._may_send(event):
            categories = self.categories()
            category = _closest_category(tags[0], categories)
            if category:
                sticker = "base64://" + base64.b64encode(random.choice(categories[category]).read_bytes()).decode("ascii")
        key = self._key(event)
        with self._lock:
            if sticker:
                self._since_last[key] = 0
            elif key in self._since_last:
                self._since_last[key] += 1
        if not sticker:
            return cleaned, ""
        return (f"{cleaned}\n[表情:{category}]" if cleaned else f"[表情:{category}]"), sticker
