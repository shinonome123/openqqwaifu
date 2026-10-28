"""The stories a tavern game can be played from.

Scenarios come from the package's ``templates/tavern/`` and from
``<data_root>/tavern/scenarios/``; a file there with the same name replaces a bundled one.

- ``.yaml``: our own format in the card-file syntax: ``title:``, ``intro:``, ``kind:``,
  ``character:``, ``rounds:``, then ``Setting:``, ``Notes:``, ``Twists:``, ``Endings:``,
  ``Opening:``, ``Examples:`` with ``- `` lines. Notes, the twists of each act and the
  endings are for the game master only; ``rounds`` is about how long the story should run.
- ``.json``: the same fields as JSON (plus ``lore``), or a SillyTavern character card.
- ``.png``: a SillyTavern character card, its JSON base64-encoded in a ``ccv3`` or
  ``chara`` text chunk, as SillyTavern exports it.

A SillyTavern card is played the way SillyTavern plays it (``Scenario.card``): its own
prompts, greeting and lorebook, nothing of ours but the content rule. ``kind`` only tells
the menu whether it is a person ("character") or a game that narrates ("story").
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..character.cards import parse_card_file

_LOGGER = logging.getLogger(__name__)

_SUFFIXES = (".yaml", ".yml", ".json", ".png")
ADULT_PREFIX = "adult/"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Where a PNG keeps text, the card among it; a picture sent to QQ goes without them.
_TEXT_CHUNKS = {b"tEXt", b"iTXt", b"zTXt"}
_PICTURE_MAX_BYTES = 10 * 1024 * 1024
_MACROS = (
    (re.compile(r"\{\{\s*(?:char|charIfNotGroup|group)\s*\}\}|<BOT>", re.IGNORECASE), "character"),
    (re.compile(r"\{\{\s*user\s*\}\}|<USER>", re.IGNORECASE), "user"),
)
# Macros that stand for nothing here: {{// a note to the card's reader}}, {{trim}}, {{noop}}.
_EMPTY_MACROS = re.compile(r"\{\{//[\s\S]*?\}\}|\{\{\s*(?:trim|noop)\s*\}\}", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]{1,200}>")
# Sentences cards open with that say nothing about the character ("原创角色卡", "人物均为成年人").
_DISCLAIMER = re.compile(r"角色卡|原创|成年|创作|免责|仅供|NSFW|成人向|creator|\bcard\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[。！？!?\n]")
_SYSTEM_CARD = re.compile(
    r"不是(?:一个)?特定的?角色|无法进行任何对话|交互式电影|叙述者|旁白系统|\bnarrator\b|game master", re.IGNORECASE
)
_CREDITS = re.compile(r"点击跳转|点击右箭头|分享于|请勿倒卖|许可证|Discord|教程", re.IGNORECASE)
# "16岁", "十六岁": someone in the story is under 18 (not "8 岁那年", something in their past).
_PAST = r"(?!那|时|時|的时候|起|开始|以前|之前|前)"
_MINOR = re.compile(
    rf"(?<![\d.])(?:1[0-7]|[1-9])\s*[岁歲]{_PAST}"
    rf"|(?<![一二三四五六七八九十百千两])(?:十[一二三四五六七]?|[一二三四五六七八九])[岁歲]{_PAST}"
    r"|小学生|初中生|幼女|幼童|女童|男童"
)
# "状态栏 · 共鸣": the regex script that draws a card's <状态> bar names its number.
_STATUS_SCRIPT = re.compile(r"^状态栏\s*[·・:：]\s*(?P<label>\S{1,6})")
# The sampling settings a card's preset may carry, with the values an API accepts.
_SAMPLING = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_tokens": (1, 32768),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
}
_INTRO_MAX_CHARS = 30
# Cards from the internet can be long; the game master's prompt has a budget.
_SETTING_MAX_CHARS = 6000
_NOTES_MAX_CHARS = 3000
_OPENING_MAX_CHARS = 2500
_EXAMPLES_MAX_CHARS = 2000
_LORE_ENTRY_MAX_CHARS = 800
# A card's own fields go to the model as they are, up to these sizes.
_CARD_FIELD_MAX_CHARS = 20000
_CARD_GREETING_MAX_CHARS = 8000
_CARD_LORE_ENTRY_MAX_CHARS = 4000


@dataclass(slots=True)
class LoreEntry:
    """World info that joins the prompt when one of its keys comes up (or always, if constant)."""

    keys: list[str]
    content: str
    constant: bool = False
    # Where SillyTavern puts it: secondary keys and how they combine with the keys (``logic``
    # 0 and-any, 1 not-all, 2 not-any, 3 and-all), the position (0 before the character's
    # description, 1 after it, 2 and 3 with the author's note, 4 at ``depth`` in the chat as
    # ``role`` 0 system, 1 user or 2 assistant, 5 and 6 around the examples) and the order.
    secondary: list[str] = field(default_factory=list)
    logic: int = 0
    position: int = 1
    depth: int = 4
    role: int = 0
    order: int = 100

    def matches(self, lowered_text: str) -> bool:
        """Whether a key (and the secondary keys, by ``logic``) is in the already lowered text."""
        if self.constant:
            return True
        if not any(_key_in(key, lowered_text) for key in self.keys):
            return False
        if not self.secondary:
            return True
        found = [_key_in(key, lowered_text) for key in self.secondary]
        return {1: not all(found), 2: not any(found), 3: all(found)}.get(self.logic, any(found))


@dataclass(slots=True)
class Card:
    """A SillyTavern card's own words, from which its prompt is built the way SillyTavern does."""

    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    # Replaces SillyTavern's main prompt ("{{original}}" stands for it); comes after the chat.
    system_prompt: str = ""
    post_history_instructions: str = ""
    # The example chats, one per <START>.
    examples: list[str] = field(default_factory=list)
    # From a preset the card carries (像素酒馆 cards do): extra system prompts and sampling.
    prompts: list[str] = field(default_factory=list)
    sampling: dict[str, float] = field(default_factory=dict)
    # The name of the number in its <状态> bar ("共鸣"), from the script that draws the bar.
    stat: str = ""


@dataclass(slots=True)
class Scenario:
    scenario_id: str
    title: str
    intro: str = ""
    # "story": a game master runs it; "character": the game master plays this character.
    # A card plays from its own prompts either way; this only labels it on the menu.
    kind: str = "story"
    character: str = ""
    setting: str = ""
    notes: str = ""
    opening: str = ""
    examples: str = ""
    # A story's turning points act by act, the ways it can end, and about how many rounds
    # it should take; the game master paces the story by them.
    twists: str = ""
    endings: str = ""
    rounds: int = 0
    lore: list[LoreEntry] = field(default_factory=list)
    source: str = ""
    # From the adult folder: offered only where ``tavern.adult_user_ids`` allows it.
    adult: bool = False
    # Someone in it is under 18, so it is never played with adult content.
    minors: bool = False
    # A SillyTavern card, played from its own prompts; ``opening`` is its greeting.
    card: Card | None = None

    def render(self, text: str, *, user: str) -> str:
        """Fill in SillyTavern's {{char}}, {{user}} and {{newline}}; drop its notes."""
        values = {"character": self.character or self.title, "user": user}
        for pattern, name in _MACROS:
            text = pattern.sub(lambda _match, value=values[name]: value, text)
        return re.sub(r"\{\{\s*newline\s*\}\}", "\n", _EMPTY_MACROS.sub("", text), flags=re.IGNORECASE)

    def lore_for(self, recent_text: str, *, limit_chars: int = 1500) -> list[str]:
        """The lore entries whose keys appear in ``recent_text``, constant ones first."""
        lowered = str(recent_text or "").lower()
        picked: list[str] = []
        used = 0
        for entry in sorted(self.lore, key=lambda item: not item.constant):
            if not entry.constant and not any(key and key.lower() in lowered for key in entry.keys):
                continue
            if used + len(entry.content) > limit_chars:
                continue
            picked.append(entry.content)
            used += len(entry.content)
        return picked


class ScenarioLibrary:
    def __init__(self, workspace_dir: Path, builtin_dir: Path, adult_dir: Path | None = None) -> None:
        self.workspace_dir = workspace_dir
        self.builtin_dir = builtin_dir
        self.adult_dir = adult_dir
        # path -> (mtime, scenario): a PNG card is read again only when it changes.
        self._cache: dict[Path, tuple[float, Scenario | None]] = {}

    def scenarios(self, *, adult: bool = False) -> list[Scenario]:
        """Bundled scenarios first, then the workspace's, each in file-name order; with
        ``adult``, the adult folder's after them (their ids start with "adult/")."""
        files: dict[str, Path] = {}
        for root in (self.builtin_dir, self.workspace_dir):
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if path.is_file() and path.suffix.lower() in _SUFFIXES:
                    files.pop(path.stem, None)  # a workspace file replaces the bundled one
                    files[path.stem] = path
        scenarios = [self._load(path) for path in files.values()]
        if adult and self.adult_dir is not None and self.adult_dir.is_dir():
            for path in sorted(self.adult_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in _SUFFIXES:
                    scenarios.append(self._load(path, adult=True))
        return [scenario for scenario in scenarios if scenario is not None]

    def get(self, scenario_id: str) -> Scenario | None:
        adult = scenario_id.startswith(ADULT_PREFIX)
        return next((item for item in self.scenarios(adult=adult) if item.scenario_id == scenario_id), None)

    def _load(self, path: Path, *, adult: bool = False) -> Scenario | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            scenario = load_scenario_file(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _LOGGER.warning("tavern scenario %s skipped: %s", path.name, exc)
            scenario = None
        if scenario is not None and adult:
            scenario.adult = True
            scenario.scenario_id = ADULT_PREFIX + scenario.scenario_id
        self._cache[path] = (mtime, scenario)
        return scenario


def load_scenario_file(path: Path) -> Scenario:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return _from_fields(parse_card_file(path), path)
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("the JSON is not an object")
        return _from_tavern_card(payload, path) if _is_tavern_card(payload) else _from_fields(payload, path)
    if suffix == ".png":
        return _from_tavern_card(read_png_card(path.read_bytes()), path)
    raise ValueError(f"unsupported file type {suffix}")


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


def card_picture(path: Path) -> str:
    """A PNG card's picture to send to QQ ("base64://…"), without the card in its text chunks.

    ``""`` when the scenario is not a PNG or cannot be read.
    """
    if path.suffix.lower() != ".png":
        return ""
    try:
        data = path.read_bytes()
        if len(data) > _PICTURE_MAX_BYTES:
            return ""
        chunks = [whole for kind, _body, whole in _png_chunks(data) if kind not in _TEXT_CHUNKS]
    except (OSError, ValueError):
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


def _is_tavern_card(payload: dict[str, Any]) -> bool:
    return (
        str(payload.get("spec") or "").startswith("chara_card")
        or "first_mes" in payload
        or "char_greeting" in payload
        or isinstance(payload.get("data"), dict)
    )


def _from_fields(payload: dict[str, Any], path: Path) -> Scenario:
    title = _text(payload.get("title")) or path.stem
    kind = "character" if _text(payload.get("kind")).lower() == "character" else "story"
    lore = [entry for entry in (_lore_entry(item) for item in _items(payload.get("lore"))) if entry is not None]
    scenario = Scenario(
        scenario_id=path.stem,
        title=title,
        intro=_intro(_text(payload.get("intro"))),
        kind=kind,
        character=_text(payload.get("character")),
        setting=_clip(_lines(payload.get("Setting", payload.get("setting"))), _SETTING_MAX_CHARS),
        notes=_clip(_lines(payload.get("Notes", payload.get("notes"))), _NOTES_MAX_CHARS),
        opening=_clip(_lines(payload.get("Opening", payload.get("opening"))), _OPENING_MAX_CHARS),
        examples=_clip(_lines(payload.get("Examples", payload.get("examples"))), _EXAMPLES_MAX_CHARS),
        twists=_clip(_lines(payload.get("Twists", payload.get("twists"))), _NOTES_MAX_CHARS),
        endings=_clip(_lines(payload.get("Endings", payload.get("endings"))), _NOTES_MAX_CHARS),
        rounds=_rounds(payload.get("rounds")),
        lore=lore,
        source=str(path),
    )
    texts = (
        scenario.setting, scenario.notes, scenario.twists, scenario.endings, scenario.opening,
        *(entry.content for entry in lore),
    )
    scenario.minors = any(_MINOR.search(text) for text in texts)
    return scenario


def _from_tavern_card(card: dict[str, Any], path: Path) -> Scenario:
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    name = _text(data.get("name") or data.get("char_name")) or path.stem
    description = _text(data.get("description") or data.get("char_persona"))
    personality = _text(data.get("personality"))
    situation = _text(data.get("scenario") or data.get("world_scenario"))
    book = data.get("character_book")
    entries = book.get("entries") if isinstance(book, dict) else None
    lore = [
        entry
        for entry in (_lore_entry(item, limit=_CARD_LORE_ENTRY_MAX_CHARS) for item in _items(entries))
        if entry is not None
    ]
    intro_source = (
        _character_sentence(name, description, situation)
        or _text(data.get("creator_notes") or card.get("creatorcomment"))
        or description
        or situation
    )
    extensions = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    presets = extensions.get("pixel_tavern_presets")
    preset = presets[0] if isinstance(presets, list) and presets and isinstance(presets[0], dict) else {}
    own = Card(
        name=name,
        description=_clip(description, _CARD_FIELD_MAX_CHARS),
        personality=_clip(personality, _CARD_FIELD_MAX_CHARS),
        scenario=_clip(situation, _CARD_FIELD_MAX_CHARS),
        system_prompt=_clip(_text(data.get("system_prompt")), _CARD_FIELD_MAX_CHARS),
        post_history_instructions=_clip(_text(data.get("post_history_instructions")), _CARD_FIELD_MAX_CHARS),
        examples=_example_chats(_text(data.get("mes_example") or data.get("example_dialogue"))),
        prompts=[
            _clip(_text(item.get("content")), _CARD_FIELD_MAX_CHARS)
            for item in _items(preset.get("prompts"))
            if isinstance(item, dict) and item.get("enabled") is not False and _text(item.get("content"))
        ],
        sampling=_sampling(preset),
        stat=_status_label(extensions),
    )
    opening = _clip(_story_greeting(data), _CARD_GREETING_MAX_CHARS)
    texts = (description, personality, situation, own.system_prompt, opening, *(entry.content for entry in lore))
    return Scenario(
        scenario_id=path.stem,
        title=name,
        intro=_intro(intro_source),
        kind="story" if _plays_a_system(description, personality, situation) else "character",
        character=name,
        opening=opening,
        lore=lore,
        source=str(path),
        minors=any(_MINOR.search(text) for text in texts),
        card=own,
    )


def _rounds(value: object) -> int:
    """``rounds:`` as a number of rounds, 0 when missing or out of reason."""
    try:
        rounds = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return rounds if 0 < rounds <= 500 else 0


def _example_chats(text: str) -> list[str]:
    """``mes_example`` cut at its <START> lines, empty chats dropped."""
    chats = [chat.strip() for chat in re.split(r"<START>", text, flags=re.IGNORECASE)]
    return [_clip(chat, _CARD_FIELD_MAX_CHARS) for chat in chats if chat]


def _sampling(preset: dict[str, Any]) -> dict[str, float]:
    """The preset's temperature, top_p, max_tokens and penalties that an API would accept."""
    picked: dict[str, float] = {}
    for name, (low, high) in _SAMPLING.items():
        value = preset.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
            picked[name] = int(value) if name == "max_tokens" else float(value)
    return picked


def _status_label(extensions: dict[str, Any]) -> str:
    style = extensions.get("pixel_tavern_style")
    if isinstance(style, dict) and _text(style.get("stat")):
        return _text(style.get("stat"))[:6]
    for script in _items(extensions.get("regex_scripts")):
        if isinstance(script, dict) and not script.get("disabled"):
            match = _STATUS_SCRIPT.match(_text(script.get("scriptName")))
            if match:
                return match.group("label")
    return ""


def _plays_a_system(description: str, personality: str, situation: str) -> bool:
    """A card that is a game or a narrator rather than a person ("仙女界不是一个特定的角色，而是一个RPG系统")."""
    text = "\n".join((description, personality, situation))
    return bool(_SYSTEM_CARD.search(text) or re.search(r"RPG", description[:200], re.IGNORECASE))


def _story_greeting(data: dict[str, Any]) -> str:
    """The first greeting that tells a story; some cards open with a credits page ("点击右箭头开始正文内容→")."""
    greetings = [_text(data.get("first_mes") or data.get("char_greeting"))]
    alternates = data.get("alternate_greetings")
    if isinstance(alternates, list):
        greetings.extend(_text(item) for item in alternates)
    for greeting in greetings:
        plain = re.sub(r"\s+", "", _TAGS.sub("", greeting))
        if len(plain) >= 40 and not _CREDITS.search(plain):
            return greeting
    return greetings[0]


def _lore_entry(item: object, *, limit: int = _LORE_ENTRY_MAX_CHARS) -> LoreEntry | None:
    """A SillyTavern lorebook entry (``keys``/``enabled``) or world-info entry (``key``/``disable``)."""
    if not isinstance(item, dict):
        return None
    if item.get("enabled") is False or item.get("disable") is True:
        return None
    content = _clip(_text(item.get("content")), limit)
    if not content:
        return None
    key_list = _key_list(item.get("keys", item.get("key")))
    constant = item.get("constant") is True
    if not key_list and not constant:
        return None
    # A card keeps SillyTavern's own numbers in "extensions"; a world-info export has them at the top.
    extensions = item.get("extensions") if isinstance(item.get("extensions"), dict) else {}

    def number(name: str, default: int) -> int:
        value = extensions.get(name, item.get(name))
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    position = number("position", {"before_char": 0}.get(_text(item.get("position")), 1))
    role = number("role", 0)
    order = item.get("insertion_order", item.get("order"))
    return LoreEntry(
        keys=key_list,
        content=content,
        constant=constant,
        secondary=_key_list(item.get("secondary_keys", item.get("keysecondary"))) if item.get("selective") else [],
        logic=number("selectiveLogic", 0),
        position=position if 0 <= position <= 6 else 1,
        depth=max(0, number("depth", 4)),
        role=role if role in {0, 1, 2} else 0,
        order=order if isinstance(order, int) and not isinstance(order, bool) else 100,
    )


def _key_list(keys: object) -> list[str]:
    return [str(key).strip() for key in keys if str(key).strip()] if isinstance(keys, list) else []


def _key_in(key: str, lowered_text: str) -> bool:
    """A lorebook key in the text; "/…/i" keys are regular expressions, as in SillyTavern."""
    match = re.fullmatch(r"/(?P<pattern>.+)/(?P<flags>[a-z]*)", key)
    if match:
        try:
            return re.search(match.group("pattern"), lowered_text, re.IGNORECASE) is not None
        except re.error:
            pass
    return key.lower() in lowered_text


def _items(value: object) -> list[object]:
    """Lorebook entries come as a list, or (world-info exports) as an object keyed by number."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _text(value: object) -> str:
    # Cards written on Windows end their lines with "\r\n".
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip() if value is not None else ""


def _lines(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if item is not None and str(item).strip())
    return _text(value)


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


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
