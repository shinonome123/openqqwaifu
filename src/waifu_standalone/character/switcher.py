"""Switching the bot's character from chat.

"切换人物卡" lists the cards with a one-line intro each; "切换<名字>" (also 切换到、
切换成、切换回、换成) switches to one. In a group the bot must be @-ed. A switch
changes the active character for every chat, as the console does: it waits until no
reply or memory write-back is running, then the new character greets the chat.

A card may describe itself in ``<character>.meta.json``: ``intro`` (its line in the
list), ``aliases`` (other names that pick it) and ``greetings`` (what it says when it
takes over).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..models import InboundEvent, OutboundMessage

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

_SWITCH = re.compile(r"^(?:切换|换成|换到)(?:到|成|为|回)?")
_CARD_WORDS = ("人物卡", "角色卡", "人设", "角色", "人物")
_LIST_ONLY = {"人物卡", "人物卡列表", "角色列表", "有哪些人物卡", "有哪些角色", "人物卡有哪些"}
_IGNORED_CHARS = re.compile(r"[\s，,。.！!？?~～「」『』“”\"'：:]")
_INTRO_MAX_CHARS = 40
# How long a switch waits for running replies and write-backs before giving up.
SWITCH_WAIT_SECONDS = 60.0
# A switch still waiting after this long says so, so the chat is not left guessing.
SWITCH_NOTICE_SECONDS = 3.0


@dataclass(slots=True)
class CharacterChoice:
    character: str
    name: str
    intro: str
    names: set[str] = field(default_factory=set)
    greetings: list[str] = field(default_factory=list)


def _string_list(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


class CharacterSwitcher:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        self._task: asyncio.Task[None] | None = None
        # The switch in progress, as a future callers outside its event loop can wait on.
        self.pending: concurrent.futures.Future[None] | None = None

    def choices(self) -> list[CharacterChoice]:
        svc = self._service
        active = svc._active_character_id()
        choices: list[CharacterChoice] = []
        for item in svc.cards.list_characters():
            character = str(item.get("character") or "")
            sources = item.get("sources") or []
            # The bundled template is listed only while it is the one in use.
            if character != active and all(source.get("kind") == "builtin" for source in sources):
                continue
            meta = svc.cards.character_meta(character)
            name = str(item.get("assistant_name") or character)
            intro = str(meta.get("intro") or "").strip()
            if not intro:
                summary = str(item.get("summary") or "").strip()
                intro = re.sub(rf"^你是{re.escape(name)}[，,]?", "", summary)
            if len(intro) > _INTRO_MAX_CHARS:
                intro = intro[:_INTRO_MAX_CHARS] + "…"
            choices.append(
                CharacterChoice(
                    character=character,
                    name=name,
                    intro=intro,
                    names={value.lower() for value in (character, name, *_string_list(meta.get("aliases"))) if value},
                    greetings=_string_list(meta.get("greetings")),
                )
            )
        return choices

    def parse(self, text: str) -> tuple[str, CharacterChoice | None] | None:
        """("list", None) or ("switch", choice) for a switching command; None for ordinary chat."""
        compact = _IGNORED_CHARS.sub("", str(text or ""))
        if compact in _LIST_ONLY:
            return "list", None
        match = _SWITCH.match(compact)
        if match is None:
            return None
        rest = compact[match.end():]
        for word in _CARD_WORDS:
            if rest.startswith(word):
                rest = rest[len(word):]
                break
        if not rest:
            return "list", None
        choice = self._find(rest)
        # "切换一下话题" names no card: it is ordinary chat.
        return ("switch", choice) if choice is not None else None

    def is_command(self, event: InboundEvent, text: str) -> bool:
        """Whether ``text`` is a card command the bot would act on in this chat."""
        if event.launcher_type == "group" and not event.has_bot_mention(str(self._service.config.bot_account_id or "")):
            return False
        return self.parse(text) is not None

    def _find(self, wanted: str) -> CharacterChoice | None:
        wanted = wanted.lower()
        choices = self.choices()
        for choice in choices:
            if wanted in choice.names:
                return choice
        # "元气雌小鬼" finds the card called "雌小鬼"; "大姐姐" finds "温柔大姐姐".
        if len(wanted) >= 2:
            for choice in choices:
                if any(len(name) >= 2 and (name in wanted or wanted in name) for name in choice.names):
                    return choice
        return None

    def list_text(self, event: InboundEvent) -> str:
        choices = self.choices()
        active = self._service._active_character_id()
        lines = ["可以切换的人物卡："]
        for index, choice in enumerate(choices, start=1):
            current = "（现在）" if choice.character == active else ""
            intro = f"：{choice.intro}" if choice.intro else ""
            lines.append(f"{index}. {choice.name}{current}{intro}")
        how = "@我说" if event.launcher_type == "group" else "说"
        example = next((choice.name for choice in choices if choice.character != active), "")
        lines.append(f"{how}「切换+名字」就换过去" + (f"，比如「切换{example}」。" if example else "。"))
        return "\n".join(lines)

    async def ahandle(self, event: InboundEvent, text: str) -> tuple[bool, OutboundMessage | None]:
        """Answer a switching command. ``(False, None)`` means ``text`` is not one."""
        svc = self._service
        parsed = self.parse(text)
        if parsed is None:
            return False, None
        # In a group only an explicit @ counts, not a follow-up in the reply window.
        if event.launcher_type == "group" and not event.has_bot_mention(str(svc.config.bot_account_id or "")):
            return False, None
        kind, choice = parsed
        if kind == "list" or choice is None:
            return True, await self._say(event, self.list_text(event))
        allowed = {str(uid).strip() for uid in svc.config.character_switch_user_ids if str(uid).strip()}
        if allowed and str(event.sender_id or "").strip() not in allowed:
            return True, await self._say(event, "人物卡只有指定的人能换。")
        if choice.character == svc._active_character_id():
            return True, await self._say(event, f"现在就是「{choice.name}」。")
        self._start(event, choice)
        return True, None

    async def wait_for_switch(self) -> None:
        """Hold a new message while a switch is pending, so the new character answers it.

        Messages that hold back do not count as running replies, so the switch only
        waits for work already under way. Never longer than the switch itself may take.
        """
        task = self._task
        if task is None or task.done():
            return
        deadline = time.monotonic() + SWITCH_WAIT_SECONDS + 5.0
        while not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

    def _start(self, event: InboundEvent, choice: CharacterChoice) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        task = asyncio.get_running_loop().create_task(self._switch_when_idle(event, choice))
        mirror: concurrent.futures.Future[None] = concurrent.futures.Future()

        def _finished(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                mirror.cancel()
            elif completed.exception() is not None:
                _LOGGER.error("character switch to %s failed", choice.character, exc_info=completed.exception())
                mirror.set_exception(completed.exception())
            else:
                mirror.set_result(None)

        task.add_done_callback(_finished)
        self._task, self.pending = task, mirror

    async def _switch_when_idle(self, event: InboundEvent, choice: CharacterChoice) -> None:
        svc = self._service
        started = time.monotonic()
        noticed = False
        while True:
            if svc._closing:
                return
            try:
                await asyncio.to_thread(svc.activate_character, choice.character)
                break
            except ValueError as exc:
                # activate_character refuses while a reply or memory write-back is running.
                if not str(exc).startswith("runtime busy"):
                    raise
                waited = time.monotonic() - started
                if waited >= SWITCH_WAIT_SECONDS:
                    _LOGGER.warning("character switch to %s gave up: still busy", choice.character)
                    await self._say(event, f"现在有点忙，没换成「{choice.name}」，过会儿再说一次吧。")
                    return
                if not noticed and waited >= SWITCH_NOTICE_SECONDS:
                    noticed = True
                    await self._say(event, f"等我一下，马上换成「{choice.name}」。")
                await asyncio.sleep(0.5)
        _LOGGER.info("character switched to %s from chat by %s", choice.character, event.sender_id)
        greeting = random.choice(choice.greetings) if choice.greetings else f"（已切换到「{choice.name}」）"
        await self._say(event, greeting, character_id=choice.character, assistant_name=choice.name)

    async def _say(
        self,
        event: InboundEvent,
        text: str,
        *,
        character_id: str = "",
        assistant_name: str = "",
    ) -> OutboundMessage:
        svc = self._service
        message = OutboundMessage(launcher_id=event.launcher_id, launcher_type=event.launcher_type, text=text)
        return await svc.emitter.aemit(
            event,
            message,
            assistant_name=assistant_name or svc.config.assistant_name,
            behavior_reason="character_switch",
            character_id=character_id,
        )
