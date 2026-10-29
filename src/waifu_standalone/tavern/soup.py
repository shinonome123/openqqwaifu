"""海龟汤: lateral-thinking riddles played in a chat.

"海龟汤" (in a group, said to the bot) serves a bowl: the riddle's surface (汤面). Players
ask questions that can be answered yes or no; the chat model, which alone sees the truth
(汤底), answers 是 / 不是 / 是也不是 / 不重要, and says when someone has guessed it.
"提示" gives the next hint, "汤面" repeats the riddle, "揭晓" reveals the truth and ends,
"换一碗" serves another. In a group only lines that ask or guess are taken, so other
talk still reaches the character; a line in brackets "（…）" or starting with "//" is
never taken. A bowl nobody asks about for ``turtle_soup.idle_timeout_seconds`` is put
away quietly when the chat next speaks.

The riddles are the bundled ``templates/turtle_soup.json`` and any ``.json`` under
``<data_root>/turtle_soup/`` (a list of riddles, or one). A chat is served the ones it
has not had yet (``played.json``) until it has had them all. Bowls are kept in memory,
not in the character's history: a restart puts them away.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import InboundEvent, OutboundMessage
from .mode import _RESUME, _wants_start

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

_IGNORED_CHARS = re.compile(r"[\s，,。.！!？?~～「」『』“”\"'：:、]")
_START = re.compile(r"^(?:来|开|喝|玩|上|再来)?(?:一)?(?:碗|个|局|把|道)?海龟汤(?:吧|呗|啊|呀|吗)?$")
_NEXT = re.compile(r"^(?:换|再来|下)(?:一)?(?:碗|个|道|题)(?:海龟汤)?$")
_HINT = re.compile(r"^(?:给|要|求|来)?(?:个|点|一个|一点)?提示(?:一下)?$")
_REPEAT = re.compile(r"^(?:汤面|题目|再说一遍|重复一下|题目是什么|汤面是什么)$")
_REVEAL = re.compile(r"^(?:揭晓|揭晓答案|公布答案|公布汤底|汤底|看答案|看汤底|不猜了|放弃|我放弃|(?:结束|退出|关闭)海龟汤)$")
_MENTIONS = re.compile(r"海龟汤")
# In a group, what a question or a guess looks like; anything else is left to the chat.
_ASKS = re.compile(r"[?？]|吗|嘛|是不是|是否|有没有|会不会|能不能|难道|为什么|为啥|怎么回事|是谁|我猜|猜测|答案是|汤底是|真相是|因为|所以|其实")
_OUT_OF_CHARACTER = ("(", "（", "//")
_QUESTION_MAX_CHARS = 200
_NAME_MAX_CHARS = 16
_ANSWERS = {
    "是": "是。",
    "不是": "不是。",
    "是也不是": "是也不是。",
    "不重要": "不重要。",
    "请问是非题": "这个得换成能用「是」或「不是」回答的问题哦。",
}
_JUDGE = """你是 QQ{place}里一场海龟汤的主持人。只有你知道汤底。

【汤面】{surface}
【汤底】{truth}
【猜中的标准】说出了下面每一点（意思对就行，不用原话）：
{keys}

判断玩家的下一句话，只输出一行 JSON：{{"answer": "…", "solved": false}}
- answer 只能是这五个之一：是、不是、是也不是、不重要、请问是非题。
- 玩家问的是能用「是」「不是」回答的问题：按汤底回答；一半对一半错用「是也不是」；和汤底无关、不影响真相用「不重要」。
- 不是是非题（问为什么、是谁、怎么回事之类）：answer 用「请问是非题」。
- 玩家在猜真相：猜中了上面每一点，solved 为 true；没猜全，就按他说的对不对回答，solved 为 false。
- 别透露汤底，别解释，只输出那一行 JSON。"""
_JSON = re.compile(r"\{[^{}]*\}")


@dataclass(slots=True)
class Riddle:
    riddle_id: str
    title: str
    surface: str
    truth: str
    keys: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    level: str = ""


@dataclass(slots=True)
class Bowl:
    riddle: Riddle
    questions: int = 0
    hints_given: int = 0
    last_activity_at: float = field(default_factory=time.time)


class TurtleSoup:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        self.root = Path(service.config.data_root) / "turtle_soup"
        self.builtin = Path(__file__).resolve().parents[1] / "templates" / "turtle_soup.json"
        self._bowls: dict[tuple[str, str], Bowl] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def ahandle(self, event: InboundEvent, text: str) -> tuple[bool, OutboundMessage | None]:
        """Take a message that belongs to a bowl. ``(False, None)`` leaves it to the character."""
        svc = self._service
        if not svc.config.turtle_soup.enabled:
            return False, None
        stripped = str(text or "").strip()
        compact = _IGNORED_CHARS.sub("", stripped)
        key = (event.launcher_type, event.launcher_id)
        bowl = self._bowl(key)
        if bowl is None:
            if compact and _START.match(compact) and svc.tavern._addressed(event):
                if svc.tavern.running(*key):
                    return True, await self._reply(event, "酒馆还开着呢，这一局结束了再来喝海龟汤。")
                return True, await self._reply(event, self._serve(key))
            return False, None
        if not stripped or stripped.startswith(_OUT_OF_CHARACTER):
            return False, None
        if _REVEAL.match(compact):
            return True, await self._reply(event, self._reveal(key, bowl))
        if svc.tavern._addressed(event) and (_wants_start(compact) or _RESUME.match(compact)):
            return True, await self._reply(event, "这碗海龟汤还没喝完，说「揭晓」看了汤底再开酒馆。")
        if _NEXT.match(compact) or _START.match(compact):
            self._bowls.pop(key, None)
            served = self._serve(key)
            return True, await self._reply(event, f"上一碗的汤底：{bowl.riddle.truth}\n\n{served}")
        if _HINT.match(compact):
            bowl.last_activity_at = time.time()
            return True, await self._reply(event, self._hint(bowl))
        if _REPEAT.match(compact):
            bowl.last_activity_at = time.time()
            return True, await self._reply(event, f"【汤面】{bowl.riddle.surface}")
        if event.launcher_type == "group" and not (_ASKS.search(stripped) or svc.tavern._addressed(event)):
            return False, None
        return True, await self._reply(event, await self._judge(event, key, bowl, stripped))

    def running(self, launcher_type: str, launcher_id: str) -> bool:
        return self._bowl((launcher_type, launcher_id)) is not None

    def prompt_hint(self, event: InboundEvent, text: str) -> str:
        """A note for the character when someone mentions 海龟汤 and no bowl is out."""
        if not self._service.config.turtle_soup.enabled or not _MENTIONS.search(str(text or "")):
            return ""
        if self.running(event.launcher_type, event.launcher_id):
            return ""
        how = "@你说「海龟汤」" if event.launcher_type == "group" else "说「海龟汤」"
        return (
            "[海龟汤]\n你可以出海龟汤给大家猜：有人" + how + "就会出一碗，大家问是非题，你来回答。"
            "有人想玩或者问起来，就告诉他这么开，别说你不会。"
        )

    # ------------------------------------------------------------------
    # A bowl
    # ------------------------------------------------------------------
    def _bowl(self, key: tuple[str, str]) -> Bowl | None:
        bowl = self._bowls.get(key)
        if bowl is None:
            return None
        limit = float(self._service.config.turtle_soup.idle_timeout_seconds)
        if limit > 0 and time.time() - bowl.last_activity_at > limit:
            del self._bowls[key]
            _LOGGER.info("turtle soup put away chat=%s:%s riddle=%s questions=%s", *key, bowl.riddle.riddle_id, bowl.questions)
            return None
        return bowl

    def _serve(self, key: tuple[str, str]) -> str:
        riddles = self.riddles()
        if not riddles:
            return "还没有海龟汤的题目。"
        played = self._played()
        chat = f"{key[0]}:{key[1]}"
        had = set(played.get(chat, []))
        fresh = [riddle for riddle in riddles if riddle.riddle_id not in had]
        if not fresh:  # every riddle served here once: start over
            had = set()
            fresh = riddles
        riddle = random.choice(fresh)
        played[chat] = [*had, riddle.riddle_id]
        self._save_played(played)
        self._bowls[key] = Bowl(riddle)
        _LOGGER.info("turtle soup served chat=%s:%s riddle=%s", *key, riddle.riddle_id)
        level = f"（{riddle.level}）" if riddle.level else ""
        return (
            f"🐢 海龟汤《{riddle.title}》{level}\n【汤面】{riddle.surface}\n\n"
            "问我能用「是」或「不是」回答的问题，猜出真相就算赢。"
            "说「提示」要提示，「揭晓」看汤底，「换一碗」换题；括号（…）里的话我不听。"
        )

    def _hint(self, bowl: Bowl) -> str:
        hints = bowl.riddle.hints
        if bowl.hints_given >= len(hints):
            return "提示用完了。实在想不出来，可以说「揭晓」看汤底。"
        hint = hints[bowl.hints_given]
        bowl.hints_given += 1
        return f"提示 {bowl.hints_given}/{len(hints)}：{hint}"

    def _reveal(self, key: tuple[str, str], bowl: Bowl) -> str:
        self._bowls.pop(key, None)
        _LOGGER.info("turtle soup revealed chat=%s:%s riddle=%s questions=%s", *key, bowl.riddle.riddle_id, bowl.questions)
        return (
            f"【汤底】{bowl.riddle.truth}\n"
            f"这碗汤一共问了 {bowl.questions} 个问题。想再来一碗就说「海龟汤」。"
        )

    async def _judge(self, event: InboundEvent, key: tuple[str, str], bowl: Bowl, question: str) -> str:
        riddle = bowl.riddle
        bowl.last_activity_at = time.time()
        bowl.questions += 1
        place = "群" if event.launcher_type == "group" else "私聊"
        system = _JUDGE.format(
            place=place,
            surface=riddle.surface,
            truth=riddle.truth,
            keys="\n".join(f"{index}. {item}" for index, item in enumerate(riddle.keys or [riddle.truth], start=1)),
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": question[:_QUESTION_MAX_CHARS]},
        ]
        try:
            raw = await self._service.generator.acomplete_chat(messages, user=f"turtle-soup:{key[0]}:{key[1]}")
        except Exception as exc:  # the question is not counted; asking again works
            _LOGGER.warning("turtle soup judge failed chat=%s:%s: %s", *key, exc)
            bowl.questions -= 1
            return "主持人走神了，再问一次？"
        answer, solved = _verdict(raw)
        name = self._name(event)
        if solved and self._bowls.get(key) is bowl:
            del self._bowls[key]
            _LOGGER.info("turtle soup solved chat=%s:%s riddle=%s questions=%s", *key, riddle.riddle_id, bowl.questions)
            who = f"{name}猜中了！" if event.launcher_type == "group" else "猜中了！"
            return (
                f"🎉 {who}（一共问了 {bowl.questions} 个问题）\n【汤底】{riddle.truth}\n"
                "想再来一碗就说「海龟汤」。"
            )
        if answer is None:
            bowl.questions -= 1
            return "主持人没听清，再问一次？"
        reply = _ANSWERS[answer]
        return f"{name}：{reply}" if event.launcher_type == "group" else reply

    # ------------------------------------------------------------------
    # Riddles
    # ------------------------------------------------------------------
    def riddles(self) -> list[Riddle]:
        """The bundled riddles, then the data folder's; one of the folder's replaces a bundled one of its id."""
        found: dict[str, Riddle] = {}
        paths = [self.builtin]
        if self.root.is_dir():
            paths += sorted(path for path in self.root.glob("*.json") if path.name != "played.json")
        for path in paths:
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                _LOGGER.warning("turtle soup file %s skipped: %s", path.name, exc)
                continue
            for item in payload if isinstance(payload, list) else [payload]:
                riddle = _riddle(item)
                if riddle is not None:
                    found[riddle.riddle_id] = riddle
        return list(found.values())

    def _played(self) -> dict[str, list[str]]:
        try:
            payload = json.loads((self.root / "played.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(chat): [str(item) for item in ids] for chat, ids in payload.items() if isinstance(ids, list)}

    def _save_played(self, played: dict[str, list[str]]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "played.json").write_text(json.dumps(played, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:  # the bowl is served anyway; a repeat later is no harm
            _LOGGER.warning("turtle soup could not save what was played: %s", exc)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    @staticmethod
    def _name(event: InboundEvent) -> str:
        name = str(event.sender_name or "").strip()
        if not name or name == "user":
            name = str(event.sender_id)
        return name[:_NAME_MAX_CHARS]

    async def _reply(self, event: InboundEvent, text: str) -> OutboundMessage:
        """Send without writing to the character's history: the game is not her conversation."""
        svc = self._service
        message = OutboundMessage(launcher_id=event.launcher_id, launcher_type=event.launcher_type, text=text)
        await svc.emitter.adispatch(message, delay_seconds=0.0)
        svc._mark_inbound_delivered(event)
        svc._record_outbound(message)
        return message


def _riddle(item: object) -> Riddle | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    surface = str(item.get("surface") or "").strip()
    truth = str(item.get("truth") or "").strip()
    if not (title and surface and truth):
        return None
    return Riddle(
        riddle_id=str(item.get("id") or title).strip(),
        title=title,
        surface=surface,
        truth=truth,
        keys=[str(key).strip() for key in item.get("keys") or [] if str(key).strip()],
        hints=[str(hint).strip() for hint in item.get("hints") or [] if str(hint).strip()],
        level=str(item.get("level") or "").strip(),
    )


def _verdict(raw: str) -> tuple[str | None, bool]:
    """(answer, solved) from the judge's JSON line; a bare word is taken too."""
    text = str(raw or "").strip()
    match = _JSON.search(text)
    if match is not None:
        try:
            payload = json.loads(match.group(0))
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            answer = str(payload.get("answer") or "").strip().strip("。.")
            solved = payload.get("solved") is True
            return (answer if answer in _ANSWERS else None), solved
    word = text.strip("。.「」\"' \n")
    return (word if word in _ANSWERS else None), False
