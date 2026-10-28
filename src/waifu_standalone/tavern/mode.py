"""Tavern mode: the bot runs a tabletop-style story game in a chat.

"启动酒馆" (in a group, @ the bot) lists the scenarios and makes whoever asked the host,
who picks one with "选 2". The bot then acts as game master. Everything a player says is
an action. Actions are gathered for up to ``tavern.batch_seconds`` (less once every
player has spoken), so one narration answers everyone instead of one reply per line.
"加入" joins and "离开" leaves; a line in brackets "（…）" or starting with "//" is
table talk the game master does not see; ".r 2d6" rolls dice here in the program and
passes the result on; "酒馆状态" says who plays. Only the host,
``tavern.admin_user_ids`` and ``tool_policy.authorized_user_ids`` end a game
("退出酒馆模式"); a game also ends after ``tavern.idle_timeout_seconds`` without a word.

While a game runs in a chat the character does not answer there; ``/skill`` and the
image prefix still reach it. The story is kept in its own files (``session.py``), not in
the character's history or memories.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..infra.llm_clients import OpenAILLMClient
from ..models import InboundEvent, OutboundMessage
from ..reply_flow.prompt_assembler import Generator
from ..reply_flow.stickers import strip_sticker_tags
from .dice import DiceError, parse_roll
from .scenarios import Scenario, ScenarioLibrary
from .session import PendingAction, SessionFiles, TavernSession

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

_IGNORED_CHARS = re.compile(r"[\s，,。.！!？?~～「」『』“”\"'：:、]")
_START = re.compile(r"^(?:启动|开启|打开|进入|开)酒馆(?:模式)?$|^酒馆(?:模式|菜单)?$")
# Said in passing to the character: "我让他开酒馆模式", "来跑团吧", "开一下酒馆".
_START_INTENT = re.compile(r"(?:启动|开启|打开|进入|开|玩|来)(?:一下|一个|一局|个|下|局)?(?:酒馆|跑团)")
_START_REFUSED = re.compile(r"(?:不|别|没|甭)(?:要|想|用|能|会)?(?:启动|开启|打开|进入|开|玩|来)(?:一下|一个|个|下)?(?:酒馆|跑团)")
_MENTIONS_TAVERN = re.compile(r"酒馆|跑团")
# Tags SillyTavern cards ask for, which their web page draws as a status bar and buttons.
_MARKUP = re.compile(r"<(?P<tag>[^<>/\s]{1,8})>(?P<body>[^<>]*)</(?P=tag)>")
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_END = re.compile(r"^(?:退出|关闭|结束)酒馆(?:模式)?$|^打烊$")
_JOIN = re.compile(r"^我?也?要?(?:加入|参加)(?:酒馆|游戏|冒险|队伍)?$|^(?:带我一个|加我一个|算我一个|我也玩|我也要玩)$")
_LEAVE = re.compile(r"^我?要?(?:离开|退出)(?:游戏|冒险|队伍)?$|^我不玩了$")
_STATUS = re.compile(r"^酒馆(?:状态|成员|玩家)$|^谁在玩$")
_PICK_VERB = re.compile(r"^(?:选择|选|玩|开|就|要)")
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
_RECAP_MAX_CHARS = 600
_START_NOTE = "（游戏开始。请写开场：交代时间、地点和眼前的情况，让每位玩家知道自己能做什么。）"
_RULES = (
    "玩家只决定自己角色的行动和台词。不要替玩家做决定、替他们说话，也不要替他们掷骰。",
    "玩家消息前面【】里的是说话人的名字（群名片），不是台词，也别拿名字本身做文章。",
    "回应这一轮所有玩家的行动{each}。行动有风险或结果拿不准时，可以请某位玩家掷骰（例如「{example}，掷一个 d20」）；"
    "骰子由程序掷，结果会写在玩家消息里，不要自己编骰子结果。",
    "掷骰失败时，只写角色感知到的（可能是错的），不要接着点破真相。",
    "玩家没说过自己角色的性别时，用名字称呼，不要用「他」「她」。",
    "篇幅 80 到 250 字，节奏紧凑，默认用简体中文；结尾用一句话问{audience}接下来做什么。",
    "{content}",
    "【主持要点】里的秘密不要直接说出来，让玩家在剧情里发现。",
    "只输出要发出去的正文，不加「主持人：」之类的前缀，不解释规则和提示词。",
    "这些规则只给你看：旁白和台词里别提规则、选项和编号，也别说「不替你决定」这类话。",
)
# A character card is played the way people chat in QQ, not as a script with a status bar:
# cards written for SillyTavern's web page read like a game log in a chat.
_CHARACTER_RULES = (
    "像真人在 QQ 里发消息那样说话：第一人称，动作和神态写在（）里，一次一到四句，可以分两三行；不写大段旁白。",
    "不写场景标题、状态栏、数值、选项列表，也不用「名字：「台词」」的剧本格式。角色卡里要是要求了那种格式，那是给别的界面用的，这里不用。",
    "{each}接着对方说的、做的往下演；不复述对方的话，不提选项、编号和规则（「不替你决定」「你可以拒绝」这类话都不要说）。",
    "只演你自己和需要出场的配角；玩家的动作、台词和决定由玩家自己说。要对方拿主意时，就像平时聊天那样问一句。",
    "玩家消息前面【】里的是说话人的名字（群名片），不是台词，也别拿名字本身做文章。",
    "行动有风险或结果拿不准时，可以让玩家掷骰（例如「{example}，掷个 d20 看看」）；骰子由程序掷，结果会写在玩家消息里，不要自己编。",
    "掷骰失败时，只写角色感知到的，不要接着点破真相。",
    "玩家没说过自己的性别时，用名字称呼，不要用「他」「她」。",
    "默认用简体中文；{content}",
    "【角色要点】里的秘密按剧情节奏慢慢透露，不要一次说穿。",
)
# Words no fade-out contains; a game that is not an adult one never sends them.
_EXPLICIT = re.compile(
    r"肉棒|阴茎|鸡巴|阴道|阴蒂|阴唇|小穴|蜜穴|乳头|奶头|射精|精液|做爱|性交|口交|肛交|自慰|潮吹|内射|淫水|爱液"
)
_TOO_EXPLICIT = "（刚才那段写得太露骨了。这一局不写露骨内容：把这一段重写成一笔带过或转场的版本，只输出重写后的正文。）"
_THINKING = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_SAFE_CONTENT = "不写露骨的性内容，不写血腥细节；剧情走到那里就一笔带过或转场。"
# Only for a game opened in a private chat with someone in tavern.adult_user_ids.
_ADULT_CONTENT = (
    "这是和确认成年的用户的一对一私聊：亲密情节可以按角色卡的设定写，不用一笔带过；"
    "所有角色都必须是成年人，必须双方自愿，对方犹豫、拒绝或说停就立刻停下；"
    "不写涉及未成年人、非自愿或真实人物的内容，也不写血腥细节。"
)
_CHARACTER_OPENING = (
    "（开场。这张卡给网页版写的开场白如下，只用来知道你在哪、正在发生什么：\n{reference}\n"
    "现在用你在 QQ 里的口吻发第一条消息：两三句，像真人那样开口，让对方知道可以怎么接；"
    "别照抄，去掉里面关于分级、规则、选项的字眼。）"
)


class TavernMode:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        root = Path(service.config.data_root) / "tavern"
        self.files = SessionFiles(root)
        self.library = ScenarioLibrary(
            root / "scenarios", Path(__file__).resolve().parents[1] / "templates" / "tavern", adult_dir=root / "adult"
        )
        self._sessions: dict[tuple[str, str], TavernSession] = {}
        # Chats whose session file was looked at since the start; others are read on first use.
        self._checked: set[tuple[str, str]] = set()
        self._runners: dict[tuple[str, str], asyncio.Task[None]] = {}
        # The runners as futures that callers outside their event loop can wait on.
        self._runner_futures: dict[tuple[str, str], concurrent.futures.Future[None]] = {}
        self._watchdogs: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Chats already told that the game master is stuck, so a failing model is not announced twice.
        self._stalled: set[tuple[str, str]] = set()
        # The tavern's own model (tavern.llm_*), built when first needed: (settings, client).
        self._model: tuple[tuple[object, ...], OpenAILLMClient] | None = None

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
        compact = _IGNORED_CHARS.sub("", stripped)
        session = self._session(event)
        if session is None:
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
        if self._model is not None:
            self._model[1].close()

    async def _complete(self, messages: list[dict[str, object]], *, user: str) -> str:
        """One completion from the tavern's own model if ``tavern.llm_*`` is set, else the chat model."""
        settings = self._service.config.tavern
        if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
            return await self._service.generator.acomplete_chat(messages, user=user)
        key = (settings.llm_base_url, settings.llm_api_key, settings.llm_model,
               float(settings.llm_timeout_seconds), settings.llm_reasoning_effort)
        if self._model is None or self._model[0] != key:
            if self._model is not None:
                self._model[1].close()
            client = OpenAILLMClient(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout_seconds=float(settings.llm_timeout_seconds) or 120.0,
                reasoning_effort=settings.llm_reasoning_effort,
            )
            self._model = (key, client)
        response = await self._model[1].ainvoke_with_tools(messages, tools=[], user=user)
        # Reasoning models may put their thoughts in the text; only the answer is wanted.
        return Generator._clean_response(_THINKING.sub("", response.text))

    # ------------------------------------------------------------------
    # The menu
    # ------------------------------------------------------------------
    async def _open_menu(self, event: InboundEvent) -> OutboundMessage:
        settings = self._service.config.tavern
        allowed = {str(uid).strip() for uid in settings.starter_user_ids if str(uid).strip()}
        if allowed and str(event.sender_id).strip() not in allowed:
            return await self._reply(event, "酒馆只有指定的人能开。")
        adult = self._adult_allowed(event)
        scenarios = await asyncio.to_thread(self.library.scenarios, adult=adult)
        if not scenarios:
            return await self._reply(event, "酒馆里还没有剧本，把剧本放进 tavern/scenarios 文件夹再来吧。")
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
        _LOGGER.info("tavern menu chat=%s:%s host=%s scenarios=%s", *session.key, session.host_id, len(scenarios))
        lines = ["酒馆开张啦！今天能玩这些本："]
        for index, scenario in enumerate(shown, start=1):
            kind = ("（成人）" if scenario.adult else "") + ("（角色卡）" if scenario.kind == "character" else "")
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
        if self._addressed(event) and _wants_start(compact):
            return True, await self._reply(event, f"酒馆在等「{session.host_name}」选本。")
        if sender != session.host_id:
            return False, None
        choice = self._pick(session, compact)
        if choice is None:
            return False, None  # the host said something else; the character answers as usual
        if isinstance(choice, str):
            return True, await self._reply(event, choice)
        return True, await self._start_game(event, session, choice)

    def _pick(self, session: TavernSession, compact: str) -> Scenario | str | None:
        """The scenario the host picked, a reason the pick failed, or ``None`` for other talk."""
        wanted = _PICK_VERB.sub("", compact)
        if compact in _RANDOM_PICK or wanted in _RANDOM_PICK:
            scenario_id = random.choice(session.menu)
        else:
            match = _PICK_NUMBER.match(wanted)
            if match is not None:
                number = _number(match.group("number"))
                if not 1 <= number <= len(session.menu):
                    return f"没有第 {number} 个本，回 1 到 {len(session.menu)} 的数字。"
                scenario_id = session.menu[number - 1]
            else:
                titles = {
                    item.scenario_id: item.title
                    for item in self.library.scenarios(adult=session.adult)
                    if item.scenario_id in session.menu
                }
                scenario_id = next((key for key, title in titles.items() if wanted and wanted == title), "")
                if not scenario_id and len(wanted) >= 2:
                    # "便利店" and "我想玩雾港酒馆" both name a scenario.
                    scenario_id = next(
                        (key for key, title in titles.items() if wanted in title or (len(title) >= 2 and title in wanted)), ""
                    )
                if not scenario_id:
                    return None
        scenario = self.library.get(scenario_id)
        if scenario is not None and scenario.adult and not session.adult:
            return "这个本这里不能开。"
        return scenario if scenario is not None else "这个本的文件不见了，换一个吧。"

    async def _start_game(self, event: InboundEvent, session: TavernSession, scenario: Scenario) -> OutboundMessage:
        session.state = "playing"
        session.scenario_id = scenario.scenario_id
        session.scenario_title = scenario.title
        session.players = {session.host_id: session.host_name}
        session.menu = []
        session.started_at = time.time()
        session.touch()
        opening = scenario.render(scenario.opening, user=session.host_name).strip()
        acting = scenario.kind == "character"
        if opening and not acting:
            session.turns = [{"role": "assistant", "content": opening}]
        self.files.save(session)
        self._watch(session, restart=True)  # the idle limit replaces the menu's
        _LOGGER.info("tavern started chat=%s:%s scenario=%s host=%s", *session.key, scenario.scenario_id, session.host_id)
        if event.launcher_type == "group":
            rules = (
                f"《{scenario.title}》开始！房主：{session.host_name}。想一起玩的说「加入」；直接说话就是行动，"
                "括号（…）里是场外话；掷骰用 .r d20；房主说「退出酒馆模式」结束。"
            )
        else:
            rules = f"《{scenario.title}》开始！直接说话就是行动，括号（…）里是场外话；掷骰用 .r d20；说「退出酒馆模式」结束。"
        message = await self._reply(event, rules)
        if acting:
            # Cards also open with HTML (banners, images, links); only the words are a reference.
            reference = re.sub(r"<[^<>]{0,500}>", "", render_markup(opening, chat=True))
            reference = re.sub(r"\n\s*\n+", "\n", reference).strip() or "（卡里没有开场白，自己按人物设定开场。）"
            self._queue(session, PendingAction("", _CHARACTER_OPENING.format(reference=reference)))
        elif opening:
            await self._send(session.launcher_type, session.launcher_id, render_markup(opening))
        else:
            self._queue(session, PendingAction("", _START_NOTE))
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
        if _STATUS.match(compact) or (self._addressed(event) and _START.match(compact)):
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
        self._queue(session, PendingAction(sender, f"【{player}】{_clip(stripped, _ACTION_MAX_CHARS)}"))
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

    def _queue(self, session: TavernSession, action: PendingAction) -> None:
        session.pending.append(action)
        del session.pending[:-_PENDING_MAX]
        session.touch()
        self.files.save(session)
        self._schedule(session)

    def _status(self, session: TavernSession) -> str:
        if session.state == "menu":
            return f"酒馆在等「{session.host_name}」选本。"
        names = "、".join(session.players.values())
        text = f"正在玩《{session.scenario_title}》，已经玩了 {session.rounds} 轮。房主：{session.host_name}；玩家：{names}。"
        if session.launcher_type == "group":
            text += "想一起玩就说「加入」，不想玩了说「离开」。"
        return text

    # ------------------------------------------------------------------
    # Rounds: gather the players' actions, then one narration answers them all
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
        """One game-master turn for ``batch``; ``False`` stops the rounds until someone speaks again."""
        scenario = await asyncio.to_thread(self.library.get, session.scenario_id)
        if scenario is None or (scenario.adult and not session.adult):
            await self._end(session, reason="missing", notice=f"《{session.scenario_title}》的剧本文件不见了，这一局只能先停在这里。")
            return False
        round_text = "\n".join(action.text for action in batch)
        started = time.monotonic()
        try:
            messages = self._messages(session, scenario, round_text)
            reply = await self._write(session, scenario, messages)
            if not session.adult and _EXPLICIT.search(reply):
                # The prompt asks for a fade-out, but a model made for taverns may not keep to it.
                _LOGGER.warning("tavern round too explicit for this game, rewriting chat=%s:%s", *session.key)
                retry = [*messages, {"role": "assistant", "content": reply}, {"role": "user", "content": _TOO_EXPLICIT}]
                reply = await self._write(session, scenario, retry)
                if _EXPLICIT.search(reply):
                    reply = "（这一段跳过。）"
            if not reply:
                raise ValueError("the game master wrote nothing")
        except Exception as exc:  # the actions stay pending and go with the next round
            _LOGGER.warning("tavern round failed chat=%s:%s: %s", *session.key, exc)
            if session.key not in self._stalled and self._sessions.get(session.key) is session:
                self._stalled.add(session.key)
                await self._send(*session.key, "（主持人卡了一下。刚才的行动都记着，谁再说一句就接着来。）")
            return False
        if self._sessions.get(session.key) is not session:
            return False  # the game ended while the model was writing
        self._stalled.discard(session.key)
        answered = {id(action) for action in batch}
        session.pending = [action for action in session.pending if id(action) not in answered]
        session.turns.append({"role": "user", "content": round_text})
        session.turns.append({"role": "assistant", "content": reply})
        session.rounds += 1
        session.touch()
        self.files.save(session)
        _LOGGER.info(
            "tavern round chat=%s:%s round=%s actions=%s chars=%s seconds=%.1f",
            *session.key, session.rounds, len(batch), len(reply), time.monotonic() - started,
        )
        await self._send(*session.key, reply if scenario.kind == "character" else render_markup(reply))
        await self._fold_story(session, scenario)
        return True

    async def _write(self, session: TavernSession, scenario: Scenario, messages: list[dict[str, object]]) -> str:
        reply = _clean_narration(await self._complete(messages, user=self._user_key(session)))
        if scenario.kind == "character":
            # Stored as sent, so the story the model sees keeps the chat style.
            reply = _as_chat(reply, scenario.character or scenario.title)
        return reply

    def _messages(self, session: TavernSession, scenario: Scenario, round_text: str) -> list[dict[str, object]]:
        recent = "\n".join([session.turns[-1]["content"] if session.turns else "", round_text])
        messages: list[dict[str, object]] = [{"role": "system", "content": self._system_prompt(session, scenario, recent)}]
        turns = self._turns_within_budget(session)
        if turns and turns[0]["role"] == "assistant":
            # Chat turns start with the user's; the opening answers the start of the game.
            messages.append({"role": "user", "content": "（开场）"})
        messages.extend(turns)
        messages.append({"role": "user", "content": round_text})
        return messages

    def _system_prompt(self, session: TavernSession, scenario: Scenario, recent: str) -> str:
        place = "群里" if session.launcher_type == "group" else "私聊里"
        acting = scenario.kind == "character"
        if acting:
            partner = "跟大家" if session.launcher_type == "group" else "跟对方"
            role = f"你就是{scenario.character or scenario.title}，正在 QQ {place}{partner}对戏。"
        else:
            role = f"你在 QQ {place}当跑团主持人，正在主持《{scenario.title}》：你描述世界、扮演所有 NPC、推进剧情。"
        host = session.host_name
        sections: list[tuple[str, str]] = []
        if scenario.setting:
            sections.append(("人物设定" if acting else "剧本设定", scenario.render(scenario.setting, user=host)))
        if scenario.notes:
            sections.append(("角色要点" if acting else "主持要点", scenario.render(scenario.notes, user=host)))
        lore = scenario.lore_for(recent)
        if lore:
            sections.append(("世界书", "\n".join(f"- {scenario.render(item, user=host)}" for item in lore)))
        if session.summary:
            sections.append(("前情提要", session.summary))
        players = [f"{name}（房主）" if uid == session.host_id else name for uid, name in session.players.items()]
        sections.append(("玩家", "、".join(players)))
        if scenario.examples:
            title = "说话感觉参考（格式别照抄）" if acting else "说话风格示例"
            sections.append((title, scenario.render(scenario.examples, user=host)))
        group_game = session.launcher_type == "group" and len(session.players) > 1
        if acting:
            each = "好几个人一起玩时，自然地回应每个人（可以叫名字）；" if group_game else ""
        else:
            each = "，点到每个人的名字" if group_game else ""
        # Adult content only in a private game opened by someone in tavern.adult_user_ids.
        content = _ADULT_CONTENT if session.adult and session.launcher_type == "person" else _SAFE_CONTENT
        rules = [
            rule.format(
                each=each,
                example=next(iter(session.players.values()), host),
                audience="大家" if group_game else "玩家",
                content=content,
            )
            for rule in (_CHARACTER_RULES if acting else _RULES)
        ]
        sections.append(("规则", "\n".join(f"- {rule}" for rule in rules)))
        return "\n\n".join([role, *(f"[{title}]\n{body}" for title, body in sections)])

    def _turns_within_budget(self, session: TavernSession) -> list[dict[str, str]]:
        """The opening plus as many recent turns as fit ``tavern.history_chars``."""
        turns = session.turns
        if not turns:
            return []
        head = turns[:1] if turns[0]["role"] == "assistant" else []
        budget = max(1000, int(self._service.config.tavern.history_chars))
        used = sum(len(turn["content"]) for turn in head)
        kept: list[dict[str, str]] = []
        for turn in reversed(turns[len(head):]):
            used += len(turn["content"])
            if used > budget and kept:
                break
            kept.append(turn)
        kept.reverse()
        # Turns keep alternating: the kept part starts with a players' round.
        while kept and kept[0]["role"] != "user":
            kept.pop(0)
        return [*head, *kept]

    async def _fold_story(self, session: TavernSession, scenario: Scenario) -> None:
        """Fold the oldest rounds into the recap once the story outgrows its budget."""
        budget = max(1000, int(self._service.config.tavern.history_chars))
        turns = session.turns
        if sum(len(turn["content"]) for turn in turns) <= budget:
            return
        head = 1 if turns[0]["role"] == "assistant" else 0
        cut, kept_chars = len(turns), 0
        while cut > head and kept_chars + len(turns[cut - 1]["content"]) <= budget // 2:
            cut -= 1
            kept_chars += len(turns[cut]["content"])
        while cut < len(turns) and turns[cut]["role"] != "user":
            cut += 1
        folded = turns[head:cut]
        if not folded:
            return
        story = "\n".join(("玩家：" if turn["role"] == "user" else "主持人：") + turn["content"] for turn in folded)
        earlier = f"之前的前情提要：\n{session.summary}\n\n" if session.summary else ""
        prompt = (
            f"下面是跑团《{scenario.title}》的一段剧情。\n{earlier}新的剧情：\n{story}\n\n"
            "把它们合成一段新的前情提要，300 字以内，按时间顺序写，保留人物、关系、线索、物品、伤势和还没解决的事。只输出提要本身。"
        )
        try:
            recap = await self._complete(
                [{"role": "system", "content": "你在帮跑团主持人整理笔记。"}, {"role": "user", "content": prompt}],
                user=self._user_key(session),
            )
        except Exception as exc:  # the story is then only cut to its budget when it is sent
            _LOGGER.warning("tavern recap failed chat=%s:%s: %s", *session.key, exc)
            return
        if not recap.strip() or self._sessions.get(session.key) is not session:
            return
        session.summary = _clip(recap, _RECAP_MAX_CHARS)
        del session.turns[head:cut]
        self.files.save(session)

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

    def _ending(self, session: TavernSession) -> str:
        thanks = "谢谢大家！" if session.launcher_type == "group" else "下次再来玩！"
        return f"《{session.scenario_title}》到此结束，一共玩了 {session.rounds} 轮，{thanks}（酒馆模式已关闭）"

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
    def _user_key(session: TavernSession) -> str:
        return f"tavern:{session.launcher_type}:{session.launcher_id}"

    async def _reply(self, event: InboundEvent, text: str) -> OutboundMessage:
        return await self._send(event.launcher_type, event.launcher_id, text, event=event)

    async def _send(
        self, launcher_type: str, launcher_id: str, text: str, *, event: InboundEvent | None = None
    ) -> OutboundMessage:
        """Send without writing to the character's history: the game is not her conversation."""
        svc = self._service
        message = OutboundMessage(launcher_id=launcher_id, launcher_type=launcher_type, text=text)  # type: ignore[arg-type]
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


def render_markup(text: str, *, chat: bool = False) -> str:
    """A scenario's <场景>, <状态> and <选项> lines as plain text for QQ; ``chat`` drops them.

    In a story the tags stay in what the model sees, so it goes on writing them (and the
    numbers in <状态> carry over); only what is sent is rewritten.
    """

    def one(match: re.Match[str]) -> str:
        tag = match.group("tag")
        parts = [part.strip() for part in match.group("body").split("|") if part.strip()]
        if not parts or (chat and tag in {"场景", "状态", "选项"}):
            return ""
        if tag == "场景":
            return "【" + " · ".join(parts) + "】"
        if tag == "状态":
            return "（" + "｜".join(parts) + "）"
        if tag == "选项":
            return "可以选：" + "　".join(f"{_CIRCLED[index]} {part}" for index, part in enumerate(parts[:10]))
        return " · ".join(parts)

    return re.sub(r"\n{3,}", "\n\n", _MARKUP.sub(one, str(text or ""))).strip()


def _as_chat(text: str, name: str) -> str:
    """A character's reply as a chat message: no tags, no "名字：「台词」" script lines."""
    text = render_markup(text, chat=True)
    if name:
        script = re.compile(rf"^{re.escape(name)}[：:]\s*「(.*)」\s*$")
        text = "\n".join(script.sub(r"\1", line.strip()) for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_narration(text: str) -> str:
    text = strip_sticker_tags(str(text or "")).strip()
    return _SPEAKER_PREFIX.sub("", text, count=1).strip()


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
