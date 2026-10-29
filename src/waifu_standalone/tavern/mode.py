"""Tavern mode: the bot runs a tabletop-style story game in a chat, played by SillyTavern.

"启动酒馆" (in a group, @ the bot) lists SillyTavern's cards and makes whoever asked the
host, who picks one with "选 2". From then on everything a player says is an action.
Actions are gathered for up to ``tavern.batch_seconds`` (less once every player has
spoken) and go to SillyTavern as one user message, so one reply answers everyone.
"加入" joins and "离开" leaves; a line in brackets "（…）" or starting with "//" is
table talk the card does not see; ".r 2d6" rolls dice here in the program and passes the
result on; "酒馆状态" says who plays. Only the host, ``tavern.admin_user_ids`` and
``tool_policy.authorized_user_ids`` end a game ("退出酒馆模式"); a game also ends after
``tavern.idle_timeout_seconds`` without a word.

SillyTavern plays the card (``sillytavern.py``): each game is a chat of that character in
SillyTavern (named the way SillyTavern names a new chat), and the prompt, its lorebook, regex scripts, preset and the model call are
SillyTavern's own. The tavern adds one system note at the end of the chat: who plays,
when there are several, the content rule, and for a bundled story how far along it is.
A bundled story ends by itself when its finale line ("（全剧终）") comes.

A game that ended before its finale (ended by the host, or idle) stays under archive/:
"继续酒馆" / "读档" lists the chat's unfinished ones like a menu, and the pick plays on
in its SillyTavern chat, hosted by whoever picked it. A card sent to the bot as a file in
a private chat by an admin goes into SillyTavern (``cards.py``).

While a game runs in a chat the character does not answer there; ``/skill`` and the
image prefix still reach it. The story is not in the character's history or memories.

Only what is sent is rendered (``render_card``): QQ shows no HTML, Markdown, status bar or
buttons, so those become plain lines, and "2" sends the second button's words.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html
import logging
import random
import re
import time
from typing import TYPE_CHECKING

from ..models import InboundEvent, OutboundMessage
from .cards import CardIntake
from .dice import DiceError, parse_roll
from .scenarios import EXPLICIT, Scenario, card_picture, scenario_from_character
from .session import PendingAction, SessionFiles, TavernSession
from .sillytavern import BridgeError, SillyTavernBridge

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

_IGNORED_CHARS = re.compile(r"[\s，,。.！!？?~～「」『』“”\"'：:、]")
_START = re.compile(r"^(?:启动|开启|开始|打开|进入|开)酒馆(?:模式)?$|^酒馆(?:模式|菜单)?$")
# Said in passing to the character: "我让他开酒馆模式", "来跑团吧", "开一下酒馆".
_START_INTENT = re.compile(r"(?:启动|开启|开始|打开|进入|开|玩|来)(?:一下|一个|一局|个|下|局)?(?:酒馆|跑团)")
_START_REFUSED = re.compile(r"(?:不|别|没|甭)(?:要|想|用|能|会)?(?:启动|开启|打开|进入|开|玩|来)(?:一下|一个|个|下)?(?:酒馆|跑团)")
_MENTIONS_TAVERN = re.compile(r"酒馆|跑团")
# The tags SillyTavern cards ask for, which their web page draws as a scene card, a status
# bar and buttons. A model may leave the closing tag out or get it wrong ("<状态>33|…</status>"),
# or start the next tag on the same line.
_CARD_MARKUP = re.compile(r"<(?P<tag>场景|状态|选项)>[ \t]*(?P<body>[^<\n]*?)[ \t]*(?:</[^<>\n]{1,12}>|(?=<)|(?=\n)|$)")
_OPTIONS = re.compile(r"<选项>([^<\n]*)")
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
# Pick up a game that ended before its finale: "继续酒馆", "读档", "酒馆存档".
_RESUME = re.compile(r"^(?:继续|读取|载入|恢复)(?:酒馆|存档|上一?局)(?:存档)?$|^读档$|^酒馆(?:存档|读档)$")
_SAVE_PREFIX = "save:"
_SAVES_MAX = 9
_END = re.compile(r"^(?:退出|关闭|结束)酒馆(?:模式)?$|^打烊$")
_JOIN = re.compile(r"^我?也?要?(?:加入|参加)(?:酒馆|游戏|冒险|队伍)?$|^(?:带我一个|加我一个|算我一个|我也玩|我也要玩)$")
_LEAVE = re.compile(r"^我?要?(?:离开|退出)(?:游戏|冒险|队伍)?$|^我不玩了$")
_STATUS = re.compile(r"^酒馆(?:状态|成员|玩家)$|^谁在玩$")
_PICK_VERB = re.compile(r"^(?:选择|选|玩|开|就|要)+")
_PICK_NUMBER = re.compile(r"^第?(?P<number>\d{1,2}|[一二三四五六七八九十]{1,3})(?:个|号|本)?吧?$")
_RANDOM_PICK = {"随机", "随便", "随便选", "随便来一个", "都行"}
_OUT_OF_CHARACTER = ("(", "（", "//")
_SPEAKER_PREFIX = re.compile(r"^【?(?:主持人|GM|DM|旁白)】?\s*[:：]\s*", re.IGNORECASE)
_CHINESE_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_MENU_MAX = 20
_NAME_MAX_CHARS = 16
_ACTION_MAX_CHARS = 300
# Beyond this many unanswered lines the oldest are dropped and the round starts at once.
_PENDING_MAX = 30
_START_NOTE = "（游戏开始。请写开场：交代时间、地点和眼前的情况，让每位玩家知道自己能做什么。）"
# A bundled story is paced by its twists and closed on one of its endings.
_STORY_RULES = (
    "这个本计划大约 {rounds} 轮，现在是第 {round} 轮：前三分之一铺垫并放出第一个转折，中段让冲突升级、来一次反转，"
    "最后三分之一收向结局。按【剧情转折】的节奏走，但跟着玩家的选择变。",
    "每两三轮给玩家一个变化：意外、反转、新的威胁或诱惑，别让他们一路顺风；转折要有前面埋下的线索撑着。",
    "玩家的选择决定走向【可能的结局】里的哪一个，也可以是顺着剧情长出来的别的结局。",
    "走到结局时写一段完整的收场：交代每个玩家角色和主要 NPC 的下场，不再问接下来做什么，最后单独一行写「（全剧终）」。"
    "没到结局别写这四个字。",
)
_OVERDUE = "已经超过计划的轮数：这一轮或下一轮就收束到一个结局。"
# The line a finished story ends with; the game ends after it.
_FINALE = re.compile(r"[（(【\[]\s*全剧终\s*[）)】\]]")
_PARTY = "这一局是 QQ 群里几位玩家一起玩：{names}。玩家消息前面【】里是说话人的名字；回应每个人的行动，不替任何一位玩家说话或行动。"
_TOO_EXPLICIT = "（刚才那段写得太露骨了。这一局不写露骨内容：把这一段重写成一笔带过或转场的版本，只输出重写后的正文。）"
_SKIPPED = "（这一段跳过。）"
# An HTML tag (<div style=…>, </details>, <br/>); the <场景> tags are not ASCII and stay.
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_HTML_BLOCK = re.compile(r"<(?:br|hr)\s*/?>|</?(?:div|p|h[1-6]|li|ul|ol|details|summary|center|table|tr)(?:\s[^<>]*)?>", re.IGNORECASE)
_THINKING = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
# What a page drawn from a reply (酒馆助手 cards send a whole ```html page) keeps out of sight.
_UNSEEN = re.compile(r"<(style|script|head)\b[^>]*>[\s\S]*?</\1\s*>|<!--[\s\S]*?-->|<!doctype[^>]*>", re.IGNORECASE)
_FENCE_LINE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.MULTILINE)
_SAFE_CONTENT = "不写露骨的性内容，不写血腥细节；剧情走到那里就一笔带过或转场。"
# A story with someone under 18 in it (``Scenario.minors``), in any chat.
_MINORS_CONTENT = "故事里有未成年人：对任何人都不写性感、暧昧或带性意味的描写，不写露骨内容和血腥细节；剧情走到那里就转场。"
# Only for a game opened in a private chat with someone in tavern.adult_user_ids.
_ADULT_CONTENT = (
    "这是和确认成年的用户的一对一私聊：亲密情节可以按角色卡的设定写，不用一笔带过；"
    "所有角色都必须是成年人，必须双方自愿，对方犹豫、拒绝或说停就立刻停下；"
    "不写涉及未成年人、非自愿或真实人物的内容，也不写血腥细节。"
)
# What a card's web page draws, as lines QQ can show: wrappers some cards put a reply in,
# Markdown rules, quotes and headings, and the hint on a fold that is always open here.
_WRAPPERS = re.compile(r"</?(?:content|response|output|reply|plot)>", re.IGNORECASE)
_RULE_LINE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_QUOTE_OR_HEADING = re.compile(r"^[ \t]*(?:>[ \t]?|#{1,6}[ \t]+)", re.MULTILINE)
_FOLD_HINT = re.compile(r"[（(]\s*点击(?:展开|查看)\s*[)）]")
# SillyTavern ships a sample character or two; they are not on the menu.
_STOCK_AVATAR = "default_"
_UNREACHABLE = (BridgeError, asyncio.TimeoutError, OSError)


class TavernMode:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        self.bridge = SillyTavernBridge(service)
        self.files = SessionFiles(self.bridge.root)
        # Character cards someone sends the bot as a file in a private chat.
        self._cards = CardIntake(service, self.bridge)
        self._sessions: dict[tuple[str, str], TavernSession] = {}
        # Chats whose session file was looked at since the start; others are read on first use.
        self._checked: set[tuple[str, str]] = set()
        # The card each running game plays, as it was when the game (or this run) started.
        self._playing: dict[tuple[str, str], Scenario] = {}
        self._runners: dict[tuple[str, str], asyncio.Task[None]] = {}
        # The runners as futures that callers outside their event loop can wait on.
        self._runner_futures: dict[tuple[str, str], concurrent.futures.Future[None]] = {}
        self._watchdogs: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Chats already told that the game is stuck, so a failing SillyTavern is not announced twice.
        self._stalled: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def ahandle(self, event: InboundEvent, text: str) -> tuple[bool, OutboundMessage | None]:
        """Take a message that belongs to a tavern game. ``(False, None)`` leaves it to the character."""
        svc = self._service
        if not svc.config.tavern.enabled or svc._closing:
            return False, None
        ignored = {str(uid).strip() for uid in svc.config.ignore_user_ids if str(uid).strip()}
        if str(event.sender_id or "").strip() in ignored:
            return False, None
        stripped = str(text or "").strip()
        # "/skill …" and "生图 …" are commands for the character even during a game.
        prefixes = [*svc.config.explicit_skill_prefixes, svc.config.image_command_prefix]
        if stripped and any(prefix and stripped.startswith(prefix) for prefix in prefixes):
            return False, None
        if event.files and event.launcher_type == "person":
            taken = await self._cards.take(event)
            if taken is not None:
                return True, await self._reply(event, taken)
        compact = _IGNORED_CHARS.sub("", stripped)
        session = self._session(event)
        if session is None:
            if svc.soup.running(event.launcher_type, event.launcher_id):
                return False, None  # a bowl of 海龟汤 is out; it takes this chat's games for now
            if self._addressed(event) and _RESUME.match(compact):
                return True, await self._open_saves(event)
            if self._addressed(event) and _wants_start(compact):
                return True, await self._open_menu(event)
            return False, None
        if session.state == "menu":
            return await self._handle_menu(event, session, compact)
        return await self._handle_play(event, session, stripped, compact)

    def running(self, launcher_type: str, launcher_id: str) -> bool:
        """Whether a game (or its menu) is open in this chat."""
        return (launcher_type, launcher_id) in self._sessions

    def drain(self, timeout: float = 10.0) -> None:
        """Wait until no round is gathering or being written (tests, scripts)."""
        deadline = time.monotonic() + timeout
        while True:
            pending = [future for future in list(self._runner_futures.values()) if not future.done()]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("tavern rounds did not finish before timeout")
            concurrent.futures.wait(pending, timeout=remaining)

    def close(self) -> None:
        """Stop the round runners and idle timers; running games stay on disk for the next start."""
        for task in [*self._runners.values(), *self._watchdogs.values()]:
            if not task.done():
                try:
                    task.get_loop().call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # the loop is already closed
        loop = getattr(self.bridge, "_loop", None)
        if loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.bridge.aclose(), loop)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # The cards
    # ------------------------------------------------------------------
    async def _scenarios(self, *, adult: bool) -> list[Scenario]:
        """SillyTavern's cards for a menu: the bundled stories, then the others by name; adult ones
        last and only with ``adult``."""
        listed = [
            (character, self.bridge.card(str(character.get("avatar", ""))))
            for character in await self.bridge.characters()
            if not str(character.get("avatar", "")).startswith(_STOCK_AVATAR)
        ]
        scenarios = [(scenario_from_character(character, known), known) for character, known in listed]
        scenarios = [(item, known) for item, known in scenarios if item.scenario_id and (adult or not item.adult)]
        scenarios.sort(key=lambda pair: (pair[0].adult, not pair[0].bundled, str(pair[1].get("key") or ""), pair[0].title))
        return [item for item, _known in scenarios]

    async def _scenario(self, session: TavernSession) -> Scenario | None:
        """The card a running game plays; ``None`` when it is gone from SillyTavern or not for this game."""
        scenario = self._playing.get(session.key)
        if scenario is None:
            scenario = next(
                (item for item in await self._scenarios(adult=True) if item.scenario_id == session.scenario_id), None
            )
            if scenario is None:
                return None
            self._playing[session.key] = scenario
        return None if scenario.adult and not session.adult else scenario

    # ------------------------------------------------------------------
    # The menu
    # ------------------------------------------------------------------
    async def _open_menu(self, event: InboundEvent) -> OutboundMessage:
        settings = self._service.config.tavern
        allowed = {str(uid).strip() for uid in settings.starter_user_ids if str(uid).strip()}
        if allowed and str(event.sender_id).strip() not in allowed:
            return await self._reply(event, "酒馆只有指定的人能开。")
        if not self.bridge.configured:
            return await self._reply(event, "酒馆还没接上 SillyTavern，等管理员配好再来吧。")
        adult = self._adult_allowed(event)
        try:
            scenarios = await self._scenarios(adult=adult)
        except _UNREACHABLE as exc:
            _LOGGER.warning("tavern menu: sillytavern unreachable: %s", exc)
            return await self._reply(event, "酒馆这会儿连不上 SillyTavern，过一会儿再试试。")
        if not scenarios:
            return await self._reply(event, "酒馆里还没有角色卡。把卡（.png 或 .json）当文件私聊发给我，或者在 SillyTavern 里加一张再来吧。")
        shown = scenarios[:_MENU_MAX]
        session = TavernSession(
            launcher_type=event.launcher_type,
            launcher_id=event.launcher_id,
            host_id=str(event.sender_id),
            host_name=self._name(event),
            menu=[scenario.scenario_id for scenario in shown],
            adult=adult,
        )
        self._sessions[session.key] = session
        self._checked.add(session.key)
        self.files.save(session)
        self._watch(session)
        _LOGGER.info("tavern menu chat=%s:%s host=%s cards=%s", *session.key, session.host_id, len(scenarios))
        lines = ["酒馆开张啦！今天能玩这些本："]
        for index, scenario in enumerate(shown, start=1):
            kind = "（成人）" if scenario.adult else ""
            intro = f"：{scenario.intro}" if scenario.intro else ""
            lines.append(f"{index}. {scenario.title}{kind}{intro}")
        if len(scenarios) > len(shown):
            lines.append(f"……还有 {len(scenarios) - len(shown)} 个没列出来。")
        host = f"房主是「{session.host_name}」，" if event.launcher_type == "group" else ""
        lines.append(f"{host}回数字就开本，比如「选 1」；不玩了就说「退出酒馆模式」。")
        return await self._reply(event, "\n".join(lines))

    async def _handle_menu(
        self, event: InboundEvent, session: TavernSession, compact: str
    ) -> tuple[bool, OutboundMessage | None]:
        sender = str(event.sender_id)
        if _END.match(compact):
            if not self._can_end(session, sender):
                return True, await self._reply(event, f"只有房主「{session.host_name}」能关酒馆。")
            return True, await self._end(session, reason="cancelled", notice="酒馆先关了，想玩再叫我。", event=event)
        if _STATUS.match(compact) or (self._addressed(event) and _wants_start(compact)):
            return True, await self._reply(event, self._status(session))
        if sender != session.host_id:
            return False, None
        if session.menu and session.menu[0].startswith(_SAVE_PREFIX):
            game = self._pick_save(session, compact)
            if game is None:
                return False, None
            if isinstance(game, str):
                return True, await self._reply(event, game)
            return True, await self._resume(event, session, game)
        try:
            choice = await self._pick(session, compact)
        except _UNREACHABLE as exc:
            _LOGGER.warning("tavern pick: sillytavern unreachable: %s", exc)
            return True, await self._reply(event, "酒馆这会儿连不上 SillyTavern，过一会儿再选一次试试。")
        if choice is None:
            return False, None  # the host said something else; the character answers as usual
        if isinstance(choice, str):
            return True, await self._reply(event, choice)
        return True, await self._start_game(event, session, choice)

    async def _pick(self, session: TavernSession, compact: str) -> Scenario | str | None:
        """The card the host picked, a reason the pick failed, or ``None`` for other talk."""
        wanted = _PICK_VERB.sub("", compact)
        match = _PICK_NUMBER.match(wanted)
        if compact in _RANDOM_PICK or wanted in _RANDOM_PICK:
            scenario_id = random.choice(session.menu)
        elif match is not None:
            number = _number(match.group("number"))
            if not 1 <= number <= len(session.menu):
                return f"没有第 {number} 个本，回 1 到 {len(session.menu)} 的数字。"
            scenario_id = session.menu[number - 1]
        elif wanted:
            scenario_id = ""
        else:
            return None
        try:
            cards = {item.scenario_id: item for item in await self._scenarios(adult=session.adult)}
        except _UNREACHABLE:
            if not scenario_id:
                return None  # other talk, as far as anyone can tell now
            raise
        if not scenario_id:
            titles = {key: item.title for key, item in cards.items() if key in session.menu}
            scenario_id = next((key for key, title in titles.items() if wanted == title), "")
            if not scenario_id and len(wanted) >= 2:
                # "便利店" and "我想玩雾港酒馆" both name a card.
                scenario_id = next(
                    (key for key, title in titles.items() if wanted in title or (len(title) >= 2 and title in wanted)), ""
                )
            if not scenario_id:
                return None
        scenario = cards.get(scenario_id)
        return scenario if scenario is not None else "这张卡在 SillyTavern 里不见了，换一个吧。"

    async def _start_game(self, event: InboundEvent, session: TavernSession, scenario: Scenario) -> OutboundMessage:
        session.state = "playing"
        session.scenario_id = scenario.scenario_id
        session.scenario_title = scenario.title
        session.players = {session.host_id: session.host_name}
        session.menu = []
        session.started_at = time.time()
        session.touch()
        try:
            opened = await self.bridge.start(scenario.scenario_id, user=self._user_name(session), greeting=scenario.greeting)
        except _UNREACHABLE as exc:
            _LOGGER.warning("tavern start failed chat=%s:%s card=%s: %s", *session.key, scenario.scenario_id, exc)
            return await self._end(session, reason="failed", notice="SillyTavern 没能开这一局，过一会儿再试试。", event=event)
        session.chat = str(opened.get("chat", ""))
        opening = str(opened.get("greeting", "")).strip()
        session.last_reply = opening
        self._playing[session.key] = scenario
        self.files.save(session)
        self._watch(session, restart=True)  # the idle limit replaces the menu's
        _LOGGER.info(
            "tavern started chat=%s:%s card=%s st_chat=%s host=%s", *session.key, scenario.scenario_id, session.chat, session.host_id
        )
        message = await self._reply(event, self._how_to_play(session, scenario, f"《{scenario.title}》开始！", opening))
        # The card's picture comes next, as SillyTavern shows the character beside the chat.
        if scenario.picture:
            try:
                picture = card_picture(await self.bridge.picture(scenario.scenario_id))
                if picture:
                    await self._send(session.launcher_type, session.launcher_id, "", images=[picture])
            except Exception as exc:  # the game goes on without it
                _LOGGER.warning("tavern picture not sent chat=%s:%s: %s", *session.key, exc)
        if opening:
            await self._send(session.launcher_type, session.launcher_id, self._render(scenario, opening))
        else:
            self._queue(session, PendingAction("", _START_NOTE))
        return message

    def _how_to_play(self, session: TavernSession, scenario: Scenario, head: str, last: str) -> str:
        """The line that opens play: who hosts, how to act, join and end; ``last`` is the card's latest reply."""
        how = "直接说话就是行动" + ("，回数字就是选那个选项" if _OPTIONS.search(last) else "")
        if session.launcher_type == "group":
            rules = (
                f"{head}房主：{session.host_name}。想一起玩的说「加入」；{how}，"
                "括号（…）里是场外话；掷骰用 .r d20；房主说「退出酒馆模式」结束。"
            )
        else:
            rules = f"{head}{how}，括号（…）里是场外话；掷骰用 .r d20；说「退出酒馆模式」结束。"
        if session.adult and scenario.minors:
            _LOGGER.info("tavern card %s has someone under 18; no adult content", scenario.scenario_id)
            rules += "（卡里有人未满 18 岁，这一局不写成人内容。）"
        return rules

    # ------------------------------------------------------------------
    # Saves: a game that ended before its finale, picked up where it stopped
    # ------------------------------------------------------------------
    async def _open_saves(self, event: InboundEvent) -> OutboundMessage:
        """"继续酒馆": this chat's unfinished games, newest first; the asker picks one, like a menu."""
        settings = self._service.config.tavern
        allowed = {str(uid).strip() for uid in settings.starter_user_ids if str(uid).strip()}
        if allowed and str(event.sender_id).strip() not in allowed:
            return await self._reply(event, "酒馆只有指定的人能开。")
        if not self.bridge.configured:
            return await self._reply(event, "酒馆还没接上 SillyTavern，等管理员配好再来吧。")
        adult = self._adult_allowed(event)
        try:
            await self.bridge.ensure()  # games saved before SillyTavern played them are moved over first
            saves = await self._playable_saves(event.launcher_type, event.launcher_id, adult=adult)
        except _UNREACHABLE as exc:
            _LOGGER.warning("tavern saves: sillytavern unreachable: %s", exc)
            return await self._reply(event, "酒馆这会儿连不上 SillyTavern，过一会儿再试试。")
        if not saves:
            return await self._reply(event, "这里没有能接着玩的存档。说「启动酒馆」开一局新的吧。")
        shown = saves[:_SAVES_MAX]
        session = TavernSession(
            launcher_type=event.launcher_type,
            launcher_id=event.launcher_id,
            host_id=str(event.sender_id),
            host_name=self._name(event),
            menu=[_SAVE_PREFIX + name for name, _ in shown],
            adult=adult,
        )
        self._sessions[session.key] = session
        self._checked.add(session.key)
        self.files.save(session)
        self._watch(session)
        _LOGGER.info("tavern saves chat=%s:%s host=%s saves=%s", *session.key, session.host_id, len(saves))
        lines = ["能接着玩的存档："]
        for index, (_, game) in enumerate(shown, start=1):
            when = time.strftime("%m月%d日 %H:%M", time.localtime(game.last_activity_at))
            lines.append(f"{index}. 《{game.scenario_title}》玩到第 {game.rounds} 轮，{when}停下的")
        lines.append("回数字就从停下的地方接着玩，比如「选 1」；不读了就说「退出酒馆模式」。")
        return await self._reply(event, "\n".join(lines))

    async def _playable_saves(self, launcher_type: str, launcher_id: str, *, adult: bool) -> list[tuple[str, TavernSession]]:
        """Saves that can go on here: not at their finale, their card and chat still in SillyTavern,
        adult ones only where allowed."""
        games = [
            (name, game)
            for name, game in await asyncio.to_thread(self.files.saves, launcher_type, launcher_id)
            if game.state == "playing" and not (game.adult and not adult) and not _FINALE.search(game.last_reply)
        ]
        if not games:
            return []
        cards = {item.scenario_id for item in await self._scenarios(adult=adult)}
        chats: dict[str, set[str]] = {}
        playable = []
        for name, game in games:
            if game.scenario_id not in cards:
                continue
            if game.scenario_id not in chats:
                chats[game.scenario_id] = set(await self.bridge.chats(game.scenario_id))
            if game.chat in chats[game.scenario_id]:
                playable.append((name, game))
        return playable

    def _pick_save(self, session: TavernSession, compact: str) -> TavernSession | str | None:
        """The save the host picked, a reason the pick failed, or ``None`` for other talk."""
        match = _PICK_NUMBER.match(_PICK_VERB.sub("", compact))
        if match is None:
            return None
        number = _number(match.group("number"))
        if not 1 <= number <= len(session.menu):
            return f"没有第 {number} 个存档，回 1 到 {len(session.menu)} 的数字。"
        game = self.files.load_save(*session.key, session.menu[number - 1][len(_SAVE_PREFIX):])
        return game if game is not None else "这个存档不见了，换一个吧。"

    async def _resume(self, event: InboundEvent, menu: TavernSession, game: TavernSession) -> OutboundMessage:
        """Play ``game`` on in its SillyTavern chat, hosted by whoever picked it."""
        game.adult = game.adult and menu.adult
        try:
            scenario = await self._scenario(game)
            last = await self.bridge.resume(game.scenario_id, game.chat) if scenario is not None else ""
        except _UNREACHABLE as exc:
            _LOGGER.warning("tavern resume failed chat=%s:%s: %s", *game.key, exc)
            return await self._reply(event, "酒馆这会儿连不上 SillyTavern，过一会儿再读一次试试。")
        if scenario is None:
            return await self._reply(event, "这个存档的角色卡在 SillyTavern 里不见了，没法接着玩。")
        game.state = "playing"
        game.menu = []
        game.pending = []
        game.hinted = []
        game.host_id, game.host_name = menu.host_id, menu.host_name
        game.players = {menu.host_id: menu.host_name, **{k: v for k, v in game.players.items() if k != menu.host_id}}
        game.last_reply = last or game.last_reply
        game.touch()
        self._sessions[game.key] = game
        self._stalled.discard(game.key)
        self.files.save(game)
        self._watch(game, restart=True)
        _LOGGER.info("tavern resumed a save chat=%s:%s card=%s rounds=%s", *game.key, game.scenario_id, game.rounds)
        head = f"读档成功：《{game.scenario_title}》，已经玩了 {game.rounds} 轮。"
        message = await self._reply(event, self._how_to_play(game, scenario, head, game.last_reply))
        if game.last_reply:
            await self._send(*game.key, "上回说到——\n" + self._render(scenario, game.last_reply))
        return message

    # ------------------------------------------------------------------
    # During a game
    # ------------------------------------------------------------------
    async def _handle_play(
        self, event: InboundEvent, session: TavernSession, stripped: str, compact: str
    ) -> tuple[bool, OutboundMessage | None]:
        sender = str(event.sender_id)
        player = session.players.get(sender, "")
        if _END.match(compact):
            if not self._can_end(session, sender):
                return True, await self._reply(event, f"只有房主「{session.host_name}」能结束这一局；不想玩了可以说「离开」。")
            return True, await self._end(session, reason="host", notice=self._ending(session), event=event)
        # "继续酒馆" or "开始酒馆" while a game runs asks about the game; it is not something the character does.
        if _STATUS.match(compact) or _START.match(compact) or _RESUME.match(compact):
            return True, await self._reply(event, self._status(session))
        if event.launcher_type == "group" and _JOIN.match(compact):
            return True, await self._join(event, session)
        if _LEAVE.match(compact) and player:
            if sender == session.host_id:
                return True, await self._reply(event, "你是房主，要结束这一局就说「退出酒馆模式」。")
            session.players.pop(sender, None)
            self._queue(session, PendingAction("", f"（「{player}」离开了队伍，让这个角色自然退场。）"))
            return True, await self._reply(event, f"{player}离开了队伍。")
        if not player:
            return True, await self._hint(event, session)
        if not stripped or stripped.startswith(_OUT_OF_CHARACTER):
            return True, None
        try:
            roll = parse_roll(stripped)
        except DiceError as exc:
            return True, await self._reply(event, str(exc))
        if roll is not None:
            message = await self._reply(event, roll.announcement(player))
            self._queue(session, PendingAction(sender, roll.story_line(player)))
            return True, message
        action = self._option(session, compact) or stripped
        self._queue(session, PendingAction(sender, f"【{player}】{_clip(action, _ACTION_MAX_CHARS)}"))
        return True, None

    async def _join(self, event: InboundEvent, session: TavernSession) -> OutboundMessage:
        sender = str(event.sender_id)
        if sender in session.players:
            return await self._reply(event, f"{session.players[sender]}已经在队伍里了。")
        limit = max(1, int(self._service.config.tavern.max_players))
        if len(session.players) >= limit:
            return await self._reply(event, f"队伍满了（最多 {limit} 人），下一局早点来。")
        name = self._name(event)
        session.players[sender] = name
        self._queue(session, PendingAction("", f"（新玩家「{name}」加入了队伍，让这位新玩家自然登场，交代一下眼前的情况。）"))
        names = "、".join(session.players.values())
        return await self._reply(event, f"{name}加入了冒险！现在 {len(session.players)} 人：{names}。")

    async def _hint(self, event: InboundEvent, session: TavernSession) -> OutboundMessage | None:
        """Tell someone outside the game, once, why the character is not answering them."""
        if not self._addressed(event) or str(event.sender_id) in session.hinted:
            return None
        session.hinted.append(str(event.sender_id))
        self.files.save(session)
        return await self._reply(
            event,
            f"这里正在玩《{session.scenario_title}》（房主：{session.host_name}）。想一起玩就说「加入」；"
            "我这会儿在当主持人，别的事等散场，或者私聊我。",
        )

    @staticmethod
    def _option(session: TavernSession, compact: str) -> str:
        """"2" or "选②" for the second <选项> of the last reply: the words its button would send."""
        offered = _OPTIONS.findall(session.last_reply)
        if not offered:
            return ""
        options = [part.strip() for part in re.split(r"[|｜]", offered[-1]) if part.strip()]
        wanted = _PICK_VERB.sub("", compact)
        if len(wanted) == 1 and wanted in _CIRCLED:
            number = _CIRCLED.index(wanted) + 1
        else:
            match = _PICK_NUMBER.match(wanted)
            number = _number(match.group("number")) if match else 0
        return options[number - 1] if 1 <= number <= len(options) else ""

    def _queue(self, session: TavernSession, action: PendingAction) -> None:
        session.pending.append(action)
        del session.pending[:-_PENDING_MAX]
        session.touch()
        self.files.save(session)
        self._schedule(session)

    def _status(self, session: TavernSession) -> str:
        if session.state == "menu":
            what = "选存档" if session.menu and session.menu[0].startswith(_SAVE_PREFIX) else "选本"
            return f"酒馆在等「{session.host_name}」{what}。"
        names = "、".join(session.players.values())
        if session.launcher_type != "group":
            return f"正在玩《{session.scenario_title}》，已经玩了 {session.rounds} 轮，直接说话就是行动；说「退出酒馆模式」结束。"
        text = f"正在玩《{session.scenario_title}》，已经玩了 {session.rounds} 轮。房主：{session.host_name}；玩家：{names}。"
        if session.launcher_type == "group":
            text += "想一起玩就说「加入」，不想玩了说「离开」。"
        return text

    # ------------------------------------------------------------------
    # Rounds: gather the players' actions, then one reply answers them all
    # ------------------------------------------------------------------
    def _schedule(self, session: TavernSession) -> None:
        key = session.key
        task = self._runners.get(key)
        if task is not None and not task.done():
            return
        task = asyncio.get_running_loop().create_task(self._run_rounds(key))
        mirror: concurrent.futures.Future[None] = concurrent.futures.Future()

        def _finished(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                mirror.cancel()
            elif completed.exception() is not None:
                _LOGGER.error("tavern rounds failed chat=%s:%s", *key, exc_info=completed.exception())
                mirror.set_exception(completed.exception())
            else:
                mirror.set_result(None)

        task.add_done_callback(_finished)
        self._runners[key], self._runner_futures[key] = task, mirror

    async def _run_rounds(self, key: tuple[str, str]) -> None:
        while await self._round_ready(key):
            session = self._sessions.get(key)
            if session is None or not await self._narrate(session, list(session.pending)):
                return

    async def _round_ready(self, key: tuple[str, str]) -> bool:
        """Wait until the round is due; ``False`` when there is nothing left to answer.

        A round is due ``batch_seconds`` after its first action, or ``settle_seconds``
        after the latest one once every player has acted (or only notes are waiting).
        """
        while True:
            session = self._sessions.get(key)
            if session is None or session.state != "playing" or not session.pending:
                return False
            settings = self._service.config.tavern
            now = time.time()
            acted = {action.actor_id for action in session.pending if action.actor_id}
            everyone = not acted or set(session.players) <= acted
            due = session.pending[0].at + float(settings.batch_seconds)
            if everyone:
                due = min(due, session.pending[-1].at + float(settings.settle_seconds))
            if now >= due or len(session.pending) >= _PENDING_MAX:
                return True
            await asyncio.sleep(max(0.05, min(0.5, due - now)))

    async def _narrate(self, session: TavernSession, batch: list[PendingAction]) -> bool:
        """One SillyTavern turn for ``batch``; ``False`` stops the rounds until someone speaks again."""
        started = time.monotonic()
        try:
            scenario = await self._scenario(session)
            if scenario is None:
                await self._end(
                    session, reason="missing", notice=f"《{session.scenario_title}》的角色卡在 SillyTavern 里不见了，这一局只能先停在这里。"
                )
                return False
            round_text = _quote_macros("\n".join(self._line(session, action) for action in batch))
            user, note = self._user_name(session), self._note(session, scenario)
            reply = (await self.bridge.turn(session.scenario_id, session.chat, user=user, text=round_text, note=note)).strip()
            if not self._adult_content(session, scenario) and EXPLICIT.search(reply):
                # The note asks for a fade-out, but a model made for taverns may not keep to it.
                _LOGGER.warning("tavern round too explicit for this game, rewriting chat=%s:%s", *session.key)
                rewritten = await self.bridge.rewrite(
                    session.scenario_id, session.chat, user=user, note=note, instruction=_TOO_EXPLICIT
                )
                rewritten = _THINKING.sub("", rewritten).strip()
                reply = rewritten if rewritten and not EXPLICIT.search(rewritten) else _SKIPPED
                await self.bridge.replace_last(session.scenario_id, session.chat, reply)
            if not reply:
                raise BridgeError("SillyTavern wrote nothing")
        except _UNREACHABLE as exc:  # the actions stay pending and go with the next round
            _LOGGER.warning("tavern round failed chat=%s:%s: %s", *session.key, exc)
            if session.key not in self._stalled and self._sessions.get(session.key) is session:
                self._stalled.add(session.key)
                await self._send(*session.key, "（主持人卡了一下。刚才的行动都记着，谁再说一句就接着来。）")
            return False
        if self._sessions.get(session.key) is not session:
            return False  # the game ended while SillyTavern was writing
        self._stalled.discard(session.key)
        answered = {id(action) for action in batch}
        session.pending = [action for action in session.pending if id(action) not in answered]
        session.last_reply = reply
        session.rounds += 1
        session.touch()
        self.files.save(session)
        _LOGGER.info(
            "tavern round chat=%s:%s round=%s actions=%s chars=%s seconds=%.1f",
            *session.key, session.rounds, len(batch), len(reply), time.monotonic() - started,
        )
        await self._send(*session.key, self._render(scenario, reply))
        if scenario.rounds and _FINALE.search(reply):
            await self._end(session, reason="finished", notice=self._ending(session, finished=True))
            return False
        return True

    @staticmethod
    def _line(session: TavernSession, action: PendingAction) -> str:
        """An action as SillyTavern gets it; played alone, without the name, as typed."""
        name = session.players.get(action.actor_id)
        if len(session.players) == 1 and name and action.text.startswith(f"【{name}】"):
            return action.text[len(name) + 2:]
        return action.text

    @staticmethod
    def _render(scenario: Scenario, text: str) -> str:
        return _SPEAKER_PREFIX.sub("", render_card(_THINKING.sub("", text), stat=scenario.stat), count=1).strip()

    @staticmethod
    def _adult_content(session: TavernSession, scenario: Scenario) -> bool:
        """A private game opened by someone in ``tavern.adult_user_ids``, with nobody under 18 in it."""
        return session.adult and session.launcher_type == "person" and not scenario.minors

    def _content_rule(self, session: TavernSession, scenario: Scenario) -> str:
        if self._adult_content(session, scenario):
            return _ADULT_CONTENT
        return _MINORS_CONTENT if scenario.minors else _SAFE_CONTENT

    def _note(self, session: TavernSession, scenario: Scenario) -> str:
        """The system note at the end of SillyTavern's prompt: who plays, the content rule, the story's pace."""
        rules = [_PARTY.format(names="、".join(session.players.values()))] if len(session.players) > 1 else []
        rules.append(self._content_rule(session, scenario))
        if scenario.rounds:
            planned, now = scenario.rounds, session.rounds + 1
            rules.extend(rule.format(rounds=planned, round=now) for rule in _STORY_RULES)
            if now > planned:
                rules.append(_OVERDUE)
        return "\n".join(rules)

    # ------------------------------------------------------------------
    # Ending
    # ------------------------------------------------------------------
    def _watch(self, session: TavernSession, *, restart: bool = False) -> None:
        key = session.key
        task = self._watchdogs.get(key)
        if task is not None and not task.done():
            if not restart:
                return
            task.cancel()
        self._watchdogs[key] = asyncio.get_running_loop().create_task(self._watch_idle(key))

    async def _watch_idle(self, key: tuple[str, str]) -> None:
        while True:
            session = self._sessions.get(key)
            if session is None:
                return
            settings = self._service.config.tavern
            limit = float(settings.menu_timeout_seconds if session.state == "menu" else settings.idle_timeout_seconds)
            remaining = session.last_activity_at + limit - time.time()
            if limit <= 0 or remaining > 0:
                await asyncio.sleep(60.0 if limit <= 0 else min(remaining, 60.0))
                continue
            runner = self._runners.get(key)
            if runner is not None and not runner.done():
                await asyncio.sleep(5.0)  # a round is still being written
                continue
            if session.state == "menu":
                notice = "一直没选本，酒馆先关了，想玩再叫我。"
            else:
                notice = (
                    f"这一桌 {max(1, round(limit / 60))} 分钟没人说话，《{session.scenario_title}》先收摊了，"
                    f"一共玩了 {session.rounds} 轮。（酒馆模式已关闭）"
                )
            await self._end(session, reason="idle", notice=notice)
            return

    async def _end(
        self, session: TavernSession, *, reason: str, notice: str, event: InboundEvent | None = None
    ) -> OutboundMessage | None:
        key = session.key
        if self._sessions.get(key) is session:
            del self._sessions[key]
        self._playing.pop(key, None)
        current = asyncio.current_task()
        for tasks in (self._runners, self._watchdogs):
            task = tasks.pop(key, None)
            if task is not None and task is not current and not task.done():
                task.cancel()
        self._stalled.discard(key)
        archived = self.files.archive(session)
        _LOGGER.info("tavern ended chat=%s:%s reason=%s rounds=%s archive=%s", *key, reason, session.rounds, archived)
        if event is not None:
            return await self._reply(event, notice)
        return await self._send(*key, notice)

    def _ending(self, session: TavernSession, *, finished: bool = False) -> str:
        """The notice that closes a game; ``finished`` when the story reached one of its endings."""
        thanks = "谢谢大家！" if session.launcher_type == "group" else "下次再来玩！"
        how = "完结" if finished else "到此结束"
        return f"《{session.scenario_title}》{how}，一共玩了 {session.rounds} 轮，{thanks}（酒馆模式已关闭）"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _session(self, event: InboundEvent) -> TavernSession | None:
        """The chat's game; one left on disk by an earlier run is picked up, or closed if it went stale."""
        key = (event.launcher_type, event.launcher_id)
        session = self._sessions.get(key)
        if session is not None or key in self._checked:
            return session
        self._checked.add(key)
        session = self.files.load(*key)
        if session is None:
            return None
        settings = self._service.config.tavern
        limit = float(settings.menu_timeout_seconds if session.state == "menu" else settings.idle_timeout_seconds)
        if limit > 0 and time.time() - session.last_activity_at > limit:
            # It went stale while the bot was down; announcing it now would come out of nowhere.
            self.files.archive(session)
            _LOGGER.info("tavern closed a stale game chat=%s:%s rounds=%s", *key, session.rounds)
            return None
        if session.state == "playing" and not session.chat:
            self.files.archive(session)  # from before SillyTavern played the games, and not moved over
            return None
        self._sessions[key] = session
        self._watch(session)
        if session.state == "playing" and session.pending:
            self._schedule(session)
        _LOGGER.info("tavern resumed chat=%s:%s state=%s rounds=%s", *key, session.state, session.rounds)
        return session

    def prompt_hint(self, event: InboundEvent, text: str) -> str:
        """A note for the character when someone mentions the tavern and no game is open in the chat.

        Without it the character does not know it can run games, and turns such requests down.
        """
        if not self._service.config.tavern.enabled or not _MENTIONS_TAVERN.search(str(text or "")):
            return ""
        if self.running(event.launcher_type, event.launcher_id):
            return ""
        how = "@你说「启动酒馆」" if event.launcher_type == "group" else "说「启动酒馆」"
        return (
            "[酒馆模式]\n你可以开酒馆模式：有人" + how + "，就会列出剧本让他选，选好后你来当主持人带大家玩。"
            "有人想玩或者问起来，就告诉他这么开，别说你不会、不肯开。"
        )

    def _adult_allowed(self, event: InboundEvent) -> bool:
        """A private chat with someone in ``tavern.adult_user_ids``; never a group."""
        allowed = {str(uid).strip() for uid in self._service.config.tavern.adult_user_ids if str(uid).strip()}
        return event.launcher_type == "person" and str(event.sender_id).strip() in allowed

    def _addressed(self, event: InboundEvent) -> bool:
        """A private chat, an @, or (with listen on) her name or the person she is talking to."""
        svc = self._service
        if event.launcher_type != "group" or event.has_bot_mention(str(svc.config.bot_account_id or "")):
            return True
        return bool(svc.config.listen.enabled and svc.listener.addresses_bot(event))

    def _can_end(self, session: TavernSession, user_id: str) -> bool:
        config = self._service.config
        admins = {str(uid).strip() for uid in (*config.tavern.admin_user_ids, *config.tool_policy.authorized_user_ids)}
        return user_id == session.host_id or user_id in admins

    @staticmethod
    def _name(event: InboundEvent) -> str:
        name = str(event.sender_name or "").strip()
        if not name or name == "user":
            name = str(event.sender_id)
        return _clip(name, _NAME_MAX_CHARS)

    @staticmethod
    def _user_name(session: TavernSession) -> str:
        """SillyTavern's {{user}}: the players."""
        return "、".join(session.players.values()) or session.host_name

    async def _reply(self, event: InboundEvent, text: str) -> OutboundMessage:
        return await self._send(event.launcher_type, event.launcher_id, text, event=event)

    async def _send(
        self,
        launcher_type: str,
        launcher_id: str,
        text: str,
        *,
        event: InboundEvent | None = None,
        images: list[str] | None = None,
    ) -> OutboundMessage:
        """Send without writing to the character's history: the game is not her conversation."""
        svc = self._service
        message = OutboundMessage(
            launcher_id=launcher_id, launcher_type=launcher_type, text=text, images=list(images or [])  # type: ignore[arg-type]
        )
        await svc.emitter.adispatch(message, delay_seconds=0.0)
        if event is not None:
            svc._mark_inbound_delivered(event)
        svc._record_outbound(message)
        return message


def _wants_start(compact: str) -> bool:
    """"启动酒馆", or a start said in passing ("我让他开酒馆模式"), but not "不开酒馆"."""
    if _START.match(compact):
        return True
    return bool(_START_INTENT.search(compact)) and not _START_REFUSED.search(compact)


def _quote_macros(text: str) -> str:
    """Players' words reach SillyTavern as words: "{{…}}" would be one of its macros."""
    return str(text or "").replace("{{", "{​{").replace("}}", "}​}")


def render_card(text: str, *, stat: str = "") -> str:
    """A card's reply as SillyTavern shows it, in the plain text QQ has.

    The scene card, status bar and buttons its web page draws from <场景>, <状态> and <选项>
    become a 【scene】 line, the bar's number and words, and numbered options; HTML and
    Markdown become what they show (a fold is always open, a rule is a line).
    """

    def one(match: re.Match[str]) -> str:
        tag = match.group("tag")
        parts = [part.strip() for part in re.split(r"[|｜]", match.group("body")) if part.strip()]
        if not parts:
            return ""
        if tag == "场景":
            return "【" + "｜".join(parts) + "】"
        if tag == "选项":
            return "\n" + "\n".join(f"{_CIRCLED[index]} {part}" for index, part in enumerate(parts[: len(_CIRCLED)])) + "\n"
        return "\n" + _status_bar(parts, stat) + "\n"

    text = _CARD_MARKUP.sub(one, _WRAPPERS.sub("", str(text or "")))
    text = _QUOTE_OR_HEADING.sub("", _FOLD_HINT.sub("", _plain(text)))
    text = _RULE_LINE.sub("──────────", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _status_bar(parts: list[str], stat: str) -> str:
    """<状态>30|蓄势待发|心声</状态> the way its bar reads: "共鸣 30 ▰▰▰▱▱▱▱▱▱▱｜心情 · 蓄势待发", then the 心声."""
    number = re.fullmatch(r"\D{0,6}?(\d{1,3})", parts[0])
    if not stat or number is None:
        return "（" + "｜".join(parts) + "）"
    value = min(100, int(number.group(1)))
    bar = f"{stat} {value} " + "▰" * round(value / 10) + "▱" * (10 - round(value / 10))
    mood = f"｜心情 · {parts[1]}" if len(parts) > 1 else ""
    heart = f"\n心声：{'｜'.join(parts[2:])}" if len(parts) > 2 else ""
    return bar + mood + heart


def _plain(text: str) -> str:
    """Text without HTML (cards decorate with it) or Markdown asterisks, as QQ shows plain text."""
    text = _FENCE_LINE.sub("", _UNSEEN.sub("", str(text or "")))
    text = _HTML_BLOCK.sub("\n", text)  # a block ends a line
    text = html.unescape(_HTML_TAG.sub("", text)).replace(" ", " ")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    text = re.sub(r"[ \t ]+\n", "\n", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _number(token: str) -> int:
    """1–99 from "2", "十二" or "二十"."""
    if token.isdigit():
        return int(token)
    tens, separator, ones = token.partition("十")
    if not separator:
        return _CHINESE_DIGITS.get(token, 0)
    return (_CHINESE_DIGITS.get(tens, 0) if tens else 1) * 10 + _CHINESE_DIGITS.get(ones, 0)
