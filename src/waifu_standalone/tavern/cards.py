"""Character cards sent to the bot as a file in a private chat, put where the tavern finds them.

Someone in ``tavern.admin_user_ids`` or ``tool_policy.authorized_user_ids`` sends a
SillyTavern card (``.png`` or ``.json``) as a QQ file. It is downloaded, read as a card,
and saved under ``<data_root>/tavern/scenarios/``, or ``adult/`` when the card is tagged
or written as an adult one, so it shows on the next "启动酒馆" menu. A PNG sent as a
picture instead of a file has been recompressed by QQ and lost the card in it.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..infra.safe_fetch import afetch_public_url
from ..models import InboundEvent, MessageSegment
from .scenarios import _SUFFIXES, Scenario, _is_tavern_card, load_scenario_file, read_png_card

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

_CARD_SUFFIXES = (".png", ".json")
_MAX_BYTES = 20 * 1024 * 1024
_FILES_PER_MESSAGE = 3
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
# A card's own tags that mark it as an adult one.
_ADULT_TAGS = re.compile(r"nsfw|r-?18|18\+|成人|色情|涩涩|里番|hentai", re.IGNORECASE)
# Words no card meant for a group would carry.
_EXPLICIT = re.compile(
    r"肉棒|阴茎|鸡巴|阴道|阴蒂|阴唇|小穴|蜜穴|乳头|奶头|射精|精液|做爱|性交|口交|肛交|自慰|潮吹|内射|淫水|爱液"
)
_UNSAFE_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_NAME_MAX_CHARS = 40


class CardIntake:
    def __init__(self, service: "WaifuService", scenarios_dir: Path, adult_dir: Path) -> None:
        self._service = service
        self.scenarios_dir = scenarios_dir
        self.adult_dir = adult_dir

    async def take(self, event: InboundEvent) -> str | None:
        """What to answer for the cards in ``event``; ``None`` when it sends none (other files are not ours)."""
        cards = [segment for segment in event.files if Path(segment.file_name).suffix.lower() in _CARD_SUFFIXES]
        if not cards:
            return None
        if not self._may_add(str(event.sender_id).strip()):
            return "只有管理员能往酒馆里放角色卡。"
        lines = [await self._add(segment) for segment in cards[:_FILES_PER_MESSAGE]]
        if len(cards) > _FILES_PER_MESSAGE:
            lines.append(f"一次最多收 {_FILES_PER_MESSAGE} 张，剩下的再发一次吧。")
        return "\n".join(lines)

    def _may_add(self, user_id: str) -> bool:
        config = self._service.config
        allowed = {str(uid).strip() for uid in (*config.tavern.admin_user_ids, *config.tool_policy.authorized_user_ids)}
        return bool(user_id) and user_id in allowed

    async def _add(self, segment: MessageSegment) -> str:
        name = segment.file_name or "角色卡"
        if segment.file_size > _MAX_BYTES:
            return f"「{name}」太大了（{segment.file_size / 1024 / 1024:.1f} MB），角色卡最多 {_MAX_BYTES // 1024 // 1024} MB。"
        try:
            data = await self._download(segment)
        except Exception as exc:  # a dead link, a refused action, a dropped connection
            _LOGGER.warning("tavern card download failed file=%s: %s", name, exc)
            return f"「{name}」没下载下来，再发一次试试。"
        try:
            return await asyncio.to_thread(self._save, name, data)
        except ValueError as exc:
            _LOGGER.info("tavern card refused file=%s: %s", name, exc)
            if Path(name).suffix.lower() == ".png":
                return (
                    f"「{name}」里没读到角色卡数据。PNG 角色卡要用「文件」发；当图片发的话，QQ 会压缩图片，"
                    "卡里的数据就没了。"
                )
            return f"「{name}」不是酒馆能读的角色卡（SillyTavern 的 .json 或 .png）。"

    async def _download(self, segment: MessageSegment) -> bytes:
        url = segment.file_url
        if not url and segment.file_id:
            call = getattr(self._service.outbound, "acall_action", None)
            if not callable(call):
                raise RuntimeError("the outbound port cannot call actions")
            result = await call("get_private_file_url", {"file_id": segment.file_id})
            data = result.get("data") if isinstance(result, dict) else None
            url = str(data.get("url") or "") if isinstance(data, dict) else ""
        if not url:
            raise RuntimeError("no download link for the file")
        page = await afetch_public_url(url, timeout_seconds=_DOWNLOAD_TIMEOUT_SECONDS, max_bytes=_MAX_BYTES + 1)
        if page.truncated:
            raise RuntimeError("the file is larger than the limit")
        return page.content

    def _save(self, file_name: str, data: bytes) -> str:
        """Check ``data`` is a card, save it, and say where; ``ValueError`` if it is not a card."""
        suffix = Path(file_name).suffix.lower()
        raw = _card_payload(suffix, data)
        adult = _looks_adult(raw)
        target_dir = self.adult_dir if adult else self.scenarios_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        # Written beside the card folders (not in them, where a menu would list it half-written),
        # then read back the way the tavern reads it.
        staging = target_dir.parent / ".incoming"
        staging.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=staging, suffix=suffix)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            scenario = load_scenario_file(Path(temp_name))
            stem = _file_stem(scenario, file_name)
            target = target_dir / f"{stem}{suffix}"
            # The menu knows a card by its file's name, so the same card in another format goes.
            earlier = [path for path in target_dir.glob(f"{glob.escape(stem)}.*") if path.suffix.lower() in _SUFFIXES]
            os.replace(temp_name, target)
            for path in earlier:
                if path != target:
                    path.unlink(missing_ok=True)
            replaced = bool(earlier)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        _LOGGER.info("tavern card added file=%s adult=%s replaced=%s", target.name, adult, replaced)
        where = "成人卡库（只在你的私聊菜单里出现）" if adult else "酒馆卡库"
        verb = "更新了" if replaced else "放进了"
        return f"收到角色卡《{scenario.title}》，{verb}{where}。说「启动酒馆」就能在菜单里选到它。"


def _card_payload(suffix: str, data: bytes) -> dict[str, Any]:
    if suffix == ".png":
        return read_png_card(data)
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the JSON is not readable") from exc
    if not isinstance(payload, dict) or not _is_tavern_card(payload):
        raise ValueError("the JSON is not a character card")
    return payload


def _looks_adult(payload: dict[str, Any]) -> bool:
    """Tagged NSFW, or explicit in its own words: such a card stays out of group menus."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    if any(_ADULT_TAGS.search(str(tag)) for tag in tags):
        return True
    return bool(_EXPLICIT.search(json.dumps(payload, ensure_ascii=False)))


def _file_stem(scenario: Scenario, file_name: str) -> str:
    """The card's name as a file name, so a card sent again replaces the earlier copy."""
    for candidate in (scenario.title, Path(file_name).stem):
        stem = _UNSAFE_NAME.sub("_", str(candidate or "")).strip(" ._")[:_NAME_MAX_CHARS]
        if stem:
            return stem
    return "card"
