"""The cards a tavern game can be played from, as the menu and the content rule see them.

The cards live in SillyTavern (``sillytavern.py``), which plays them; this reads what the
tavern itself needs from a character SillyTavern lists: a line for the menu, whether it is
an adult card, whether someone in it is under 18, the name of the number in its <状态> bar,
the greeting that tells its story, and, for the bundled stories, how many rounds they plan.

The bundled stories (``templates/tavern/*.json``) are cards for a game master. Their
``extensions.openqqwaifu.rounds`` paces the story, and the game ends on their finale line.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Where a PNG keeps text, the card among it; a picture sent to QQ goes without them.
_TEXT_CHUNKS = {b"tEXt", b"iTXt", b"zTXt"}
_PICTURE_MAX_BYTES = 10 * 1024 * 1024
_TAGS = re.compile(r"<[^>]{1,200}>")
# Sentences cards open with that say nothing about the character ("原创角色卡", "人物均为成年人").
_DISCLAIMER = re.compile(r"角色卡|原创|成年|创作|免责|仅供|NSFW|成人向|creator|\bcard\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[。！？!?\n]")
_CREDITS = re.compile(r"点击跳转|点击右箭头|分享于|请勿倒卖|许可证|Discord|教程", re.IGNORECASE)
# "16岁", "十六岁": someone in the story is under 18 (not "8 岁那年", something in their past).
_PAST = r"(?!那|时|時|的时候|起|开始|以前|之前|前)"
_MINOR = re.compile(
    rf"(?<![\d.])(?:1[0-7]|[1-9])\s*[岁歲]{_PAST}"
    rf"|(?<![一二三四五六七八九十百千两])(?:十[一二三四五六七]?|[一二三四五六七八九])[岁歲]{_PAST}"
    r"|小学生|初中生|幼女|幼童|女童|男童"
)
# A card's own tags that mark it as an adult one.
_ADULT_TAGS = re.compile(r"nsfw|r-?18|18\+|成人|色情|涩涩|里番|hentai", re.IGNORECASE)
# Words no card meant for a group would carry.
EXPLICIT = re.compile(
    r"肉棒|阴茎|鸡巴|阴道|阴蒂|阴唇|小穴|蜜穴|乳头|奶头|射精|精液|做爱|性交|口交|肛交|自慰|潮吹|内射|淫水|爱液"
)
# "状态栏 · 共鸣": the regex script that draws a card's <状态> bar names its number.
_STATUS_SCRIPT = re.compile(r"^状态栏\s*[·・:：]\s*(?P<label>\S{1,6})")
_INTRO_MAX_CHARS = 30


@dataclass(slots=True)
class Scenario:
    # SillyTavern's name for the character: its avatar file ("雾港酒馆.png").
    scenario_id: str
    title: str
    intro: str = ""
    # Offered only where ``tavern.adult_user_ids`` allows it.
    adult: bool = False
    # Someone in it is under 18, so it is never played with adult content.
    minors: bool = False
    # The name of the number in its <状态> bar ("共鸣"), from the script that draws the bar.
    stat: str = ""
    # A bundled story: about how many rounds it plans, and it ends on its finale line.
    rounds: int = 0
    # Which of its greetings opens the game (0 is ``first_mes``; then the alternates).
    greeting: int = 0
    # It has a picture of its own to show at the start.
    picture: bool = True
    bundled: bool = False


def scenario_from_character(character: dict[str, Any], known: dict[str, Any]) -> Scenario:
    """A character as SillyTavern lists it; ``known`` is the tavern's record of a card it imported."""
    name = _text(character.get("name")) or _text(character.get("avatar"))
    description = _text(character.get("description"))
    personality = _text(character.get("personality"))
    situation = _text(character.get("scenario"))
    first = _text(character.get("first_mes"))
    extensions = character.get("extensions") if isinstance(character.get("extensions"), dict) else {}
    own = extensions.get("openqqwaifu") if isinstance(extensions.get("openqqwaifu"), dict) else {}
    texts = (description, personality, situation, _text(character.get("system_prompt")), first, _text(character.get("lore")))
    intro = (
        _text(own.get("intro"))
        or _character_sentence(name, description, situation)
        or _text(character.get("creator_notes"))
        or description
        or situation
    )
    alternates = character.get("alternate_greetings")
    return Scenario(
        scenario_id=_text(character.get("avatar")),
        title=name,
        intro=_intro(intro),
        # A card the tavern imported is adult by its folder; one added in SillyTavern by what it says.
        adult=bool(known.get("adult")) if known else looks_adult(character),
        minors=any(_MINOR.search(text) for text in texts),
        stat=_status_label(extensions),
        rounds=_rounds(own.get("rounds")),
        greeting=story_greeting([first, *(_text(item) for item in alternates)] if isinstance(alternates, list) else [first]),
        picture=bool(known.get("picture", True)) if known else True,
        bundled=bool(known.get("bundled")),
    )


def looks_adult(card: dict[str, Any]) -> bool:
    """Tagged NSFW, or explicit in its own words: such a card stays out of group menus."""
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    if any(_ADULT_TAGS.search(str(tag)) for tag in tags):
        return True
    return bool(EXPLICIT.search(json.dumps(card, ensure_ascii=False)))


def is_tavern_card(payload: dict[str, Any]) -> bool:
    return (
        str(payload.get("spec") or "").startswith("chara_card")
        or "first_mes" in payload
        or "char_greeting" in payload
        or isinstance(payload.get("data"), dict)
    )


def story_greeting(greetings: list[str]) -> int:
    """The first greeting that tells a story; some cards open with a credits page ("点击右箭头开始正文内容→")."""
    for index, greeting in enumerate(greetings):
        plain = re.sub(r"\s+", "", _TAGS.sub("", greeting))
        if len(plain) >= 40 and not _CREDITS.search(plain):
            return index
    return 0


def read_png_card(data: bytes) -> dict[str, Any]:
    """The character card JSON embedded in a SillyTavern PNG."""
    texts = _png_text_chunks(data)
    raw = texts.get("ccv3") or texts.get("chara")
    if not raw:
        raise ValueError("the PNG carries no character card")
    try:
        payload = json.loads(base64.b64decode(raw).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the PNG's character card is not readable") from exc
    if not isinstance(payload, dict):
        raise ValueError("the PNG's character card is not an object")
    return payload


def card_picture(data: bytes) -> str:
    """A card's picture to send to QQ ("base64://…"), without the card in its text chunks.

    ``""`` when it is not a PNG or cannot be read.
    """
    if not data or len(data) > _PICTURE_MAX_BYTES:
        return ""
    try:
        chunks = [whole for kind, _body, whole in _png_chunks(data) if kind not in _TEXT_CHUNKS]
    except ValueError:
        return ""
    return "base64://" + base64.b64encode(_PNG_SIGNATURE + b"".join(chunks)).decode("ascii")


def _png_chunks(data: bytes) -> Iterator[tuple[bytes, bytes, bytes]]:
    """Each chunk of a PNG up to IEND: its type, its data, and the whole chunk as stored."""
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + length + 12  # length, type, data, CRC
        yield kind, data[offset + 8:offset + 8 + length], data[offset:end]
        if kind == b"IEND":
            return
        offset = end


def _png_text_chunks(data: bytes) -> dict[str, str]:
    texts: dict[str, str] = {}
    for kind, body, _whole in _png_chunks(data):
        try:
            if kind == b"tEXt":
                keyword, _, text = body.partition(b"\x00")
                texts[keyword.decode("latin-1")] = text.decode("latin-1")
            elif kind == b"zTXt":
                keyword, _, rest = body.partition(b"\x00")
                texts[keyword.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
            elif kind == b"iTXt":
                keyword, _, rest = body.partition(b"\x00")
                compressed, rest = rest[:1] == b"\x01", rest[2:]
                _language, _, rest = rest.partition(b"\x00")
                _translated, _, text = rest.partition(b"\x00")
                texts[keyword.decode("latin-1")] = (zlib.decompress(text) if compressed else text).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            continue
    return texts


def _rounds(value: object) -> int:
    """``rounds`` as a number of rounds, 0 when missing or out of reason."""
    try:
        rounds = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return rounds if 0 < rounds <= 500 else 0


def _status_label(extensions: dict[str, Any]) -> str:
    style = extensions.get("pixel_tavern_style")
    if isinstance(style, dict) and _text(style.get("stat")):
        return _text(style.get("stat"))[:6]
    scripts = extensions.get("regex_scripts")
    for script in scripts if isinstance(scripts, list) else []:
        if isinstance(script, dict) and not script.get("disabled"):
            match = _STATUS_SCRIPT.match(_text(script.get("scriptName")))
            if match:
                return match.group("label")
    return ""


def _text(value: object) -> str:
    # Cards written on Windows end their lines with "\r\n".
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip() if value is not None else ""


def _character_sentence(name: str, *texts: str) -> str:
    """The first sentence that is about the character, without its name in front ("27岁，主唱…")."""
    for text in texts:
        plain = _TAGS.sub("", re.sub(r"\{\{\s*char\s*\}\}", name, str(text or ""), flags=re.I))
        plain = re.sub(r"\{\{\s*user\s*\}\}", "你", plain, flags=re.I)
        for sentence in _SENTENCE_END.split(plain):
            sentence = sentence.strip(" #*>-_，,；;：:")
            if not sentence or _DISCLAIMER.search(sentence):
                continue
            if name and sentence.startswith(name):
                sentence = re.sub(r"^[，,：:是]+", "", sentence[len(name):]).strip()
            if sentence:
                return sentence
    return ""


def _intro(text: str) -> str:
    """The first line of a description, without HTML or Markdown, short enough for a menu."""
    plain = _TAGS.sub("", str(text or ""))
    first = next((line.strip(" #*>-_") for line in plain.splitlines() if line.strip(" #*>-_")), "")
    return first if len(first) <= _INTRO_MAX_CHARS else first[:_INTRO_MAX_CHARS].rstrip() + "…"
