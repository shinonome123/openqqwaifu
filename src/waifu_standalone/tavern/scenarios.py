"""The stories a tavern game can be played from.

Scenarios come from the package's ``templates/tavern/`` and from
``<data_root>/tavern/scenarios/``; a file there with the same name replaces a bundled one.

- ``.yaml``: our own format in the card-file syntax: ``title:``, ``intro:``, ``kind:``,
  ``character:``, then ``Setting:``, ``Notes:``, ``Opening:``, ``Examples:`` with ``- `` lines.
  Notes are for the game master only.
- ``.json``: the same fields as JSON (plus ``lore``), or a SillyTavern character card.
- ``.png``: a SillyTavern character card, its JSON base64-encoded in a ``ccv3`` or
  ``chara`` text chunk, as SillyTavern exports it.

A SillyTavern card is a character to role-play rather than a story to run: the game
master plays that character and narrates around it (``kind == "character"``).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..character.cards import parse_card_file

_LOGGER = logging.getLogger(__name__)

_SUFFIXES = (".yaml", ".yml", ".json", ".png")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MACROS = (
    (re.compile(r"\{\{\s*char\s*\}\}|<BOT>", re.IGNORECASE), "character"),
    (re.compile(r"\{\{\s*user\s*\}\}|<USER>", re.IGNORECASE), "user"),
)
_TAGS = re.compile(r"<[^>]{1,200}>")
# Sentences cards open with that say nothing about the character ("原创角色卡", "人物均为成年人").
_DISCLAIMER = re.compile(r"角色卡|原创|成年|创作|免责|仅供|NSFW|成人向|creator|\bcard\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[。！？!?\n]")
_INTRO_MAX_CHARS = 30
# Cards from the internet can be long; the game master's prompt has a budget.
_SETTING_MAX_CHARS = 6000
_NOTES_MAX_CHARS = 3000
_OPENING_MAX_CHARS = 2500
_EXAMPLES_MAX_CHARS = 2000
_LORE_ENTRY_MAX_CHARS = 800


@dataclass(slots=True)
class LoreEntry:
    """World info that joins the prompt when one of its keys comes up (or always, if constant)."""

    keys: list[str]
    content: str
    constant: bool = False


@dataclass(slots=True)
class Scenario:
    scenario_id: str
    title: str
    intro: str = ""
    # "story": a game master runs it; "character": the game master plays this character.
    kind: str = "story"
    character: str = ""
    setting: str = ""
    notes: str = ""
    opening: str = ""
    examples: str = ""
    lore: list[LoreEntry] = field(default_factory=list)
    source: str = ""

    def render(self, text: str, *, user: str) -> str:
        """Fill in SillyTavern's {{char}} and {{user}}."""
        values = {"character": self.character or self.title, "user": user}
        for pattern, name in _MACROS:
            text = pattern.sub(values[name], text)
        return text

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
    def __init__(self, workspace_dir: Path, builtin_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.builtin_dir = builtin_dir
        # path -> (mtime, scenario): a PNG card is read again only when it changes.
        self._cache: dict[Path, tuple[float, Scenario | None]] = {}

    def scenarios(self) -> list[Scenario]:
        """Bundled scenarios first, then the workspace's, each in file-name order."""
        files: dict[str, Path] = {}
        for root in (self.builtin_dir, self.workspace_dir):
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if path.is_file() and path.suffix.lower() in _SUFFIXES:
                    files.pop(path.stem, None)  # a workspace file replaces the bundled one
                    files[path.stem] = path
        scenarios = [self._load(path) for path in files.values()]
        return [scenario for scenario in scenarios if scenario is not None]

    def get(self, scenario_id: str) -> Scenario | None:
        return next((item for item in self.scenarios() if item.scenario_id == scenario_id), None)

    def _load(self, path: Path) -> Scenario | None:
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


def _png_text_chunks(data: bytes) -> dict[str, str]:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    texts: dict[str, str] = {}
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        offset += length + 12  # length, type, data, CRC
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
            elif kind == b"IEND":
                break
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
    return Scenario(
        scenario_id=path.stem,
        title=title,
        intro=_intro(_text(payload.get("intro"))),
        kind=kind,
        character=_text(payload.get("character")),
        setting=_clip(_lines(payload.get("Setting", payload.get("setting"))), _SETTING_MAX_CHARS),
        notes=_clip(_lines(payload.get("Notes", payload.get("notes"))), _NOTES_MAX_CHARS),
        opening=_clip(_lines(payload.get("Opening", payload.get("opening"))), _OPENING_MAX_CHARS),
        examples=_clip(_lines(payload.get("Examples", payload.get("examples"))), _EXAMPLES_MAX_CHARS),
        lore=lore,
        source=str(path),
    )


def _from_tavern_card(card: dict[str, Any], path: Path) -> Scenario:
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    name = _text(data.get("name") or data.get("char_name")) or path.stem
    description = _text(data.get("description") or data.get("char_persona"))
    personality = _text(data.get("personality"))
    situation = _text(data.get("scenario") or data.get("world_scenario"))
    setting = "\n".join(
        part
        for part in (
            description,
            f"性格：{personality}" if personality else "",
            f"场景：{situation}" if situation else "",
        )
        if part
    )
    notes = "\n".join(
        part
        for part in (
            _text(data.get("system_prompt")).replace("{{original}}", "").strip(),
            _text(data.get("post_history_instructions")).replace("{{original}}", "").strip(),
        )
        if part
    )
    book = data.get("character_book")
    entries = book.get("entries") if isinstance(book, dict) else None
    lore = [entry for entry in (_lore_entry(item) for item in _items(entries)) if entry is not None]
    intro_source = (
        _character_sentence(name, description, situation)
        or _text(data.get("creator_notes") or card.get("creatorcomment"))
        or description
        or situation
    )
    examples = re.sub(r"<START>\s*", "", _text(data.get("mes_example") or data.get("example_dialogue")), flags=re.I)
    return Scenario(
        scenario_id=path.stem,
        title=name,
        intro=_intro(intro_source),
        kind="character",
        character=name,
        setting=_clip(setting, _SETTING_MAX_CHARS),
        notes=_clip(notes, _NOTES_MAX_CHARS),
        opening=_clip(_text(data.get("first_mes") or data.get("char_greeting")), _OPENING_MAX_CHARS),
        examples=_clip(examples.strip(), _EXAMPLES_MAX_CHARS),
        lore=lore,
        source=str(path),
    )


def _lore_entry(item: object) -> LoreEntry | None:
    """A SillyTavern lorebook entry (``keys``/``enabled``) or world-info entry (``key``/``disable``)."""
    if not isinstance(item, dict):
        return None
    if item.get("enabled") is False or item.get("disable") is True:
        return None
    content = _clip(_text(item.get("content")), _LORE_ENTRY_MAX_CHARS)
    if not content:
        return None
    keys = item.get("keys", item.get("key"))
    key_list = [str(key).strip() for key in keys if str(key).strip()] if isinstance(keys, list) else []
    constant = item.get("constant") is True
    if not key_list and not constant:
        return None
    return LoreEntry(keys=key_list, content=content, constant=constant)


def _items(value: object) -> list[object]:
    """Lorebook entries come as a list, or (world-info exports) as an object keyed by number."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


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
