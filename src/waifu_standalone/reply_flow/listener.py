"""Listening before answering ("听完再说", ``listen`` in the config).

People send one thought as several messages, and most of a group's talk is not meant
for the bot. With ``listen.enabled``:

- A person is answered once they stop typing: ``quiet_seconds`` after their latest
  message, or ``max_wait_seconds`` after the first one not yet answered. An earlier
  message that a newer one from the same person supersedes gets no reply of its own; its
  pictures go with the newer one, and its text is already in the history.
- In a group the bot answers whoever @s it or calls it by its name (the card's
  ``assistant_name``, or a ``call_names`` entry in its meta file, such as a nickname the
  group uses) and, for ``continuation_seconds`` after a reply, the person it answered.
- Everyone else's talk is weighed once the group has been quiet for
  ``group_quiet_seconds``: how close it is to the character's interests (``interests``
  in the card's meta file, else its persona lines) and to what the character said last.
  Past ``chime_in_threshold``, within the group's chime-in budget and not too soon after
  the character last spoke there, the model is asked to join in, and may still decide
  to say nothing (``SILENCE``). A card can set its own limits in its meta file:
  ``"chime_in": {"threshold": …, "min_gap_seconds": …, "burst": …, "per_hour": …}``.

Arrival times are noted before a message waits for its chat's lock, so a reply that is
waiting can tell that its sender has said more.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..infra.embedding_clients import build_embedding_client
from ..infra.rate_limit import TokenBucketLimiter
from ..models import InboundEvent, MessageSegment, OutboundMessage, SessionMemory
from .stickers import strip_sticker_tags

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

# What the model answers when it joins in on its own but has nothing to say.
SILENCE = "[不说话]"
CHIME_IN_HINT = (
    "[这次是你自己想插话]\n"
    "群里没人叫你，是你看到大家在聊，自己想说一句。只说一句，二十来个字，不点名，"
    "别抢话题、别总结大家说了什么；只是笑、发表情、几个字的随口一句，一般不用接。"
    f"不想说或者不合适插嘴，就只输出「{SILENCE}」。"
)
TALK_ON_HINT = (
    "[接着刚才的话]\n"
    "对方没有 @ 你，只是接着刚才的话在说；回一句就好，不超过三十个字，不点名。"
    "但对方是在接着问要你查的事，就照常去查，把查到的结论说完整，不受三十个字限制。"
    f"要是这句其实是对别人说的，或者用不着你接，就只输出「{SILENCE}」。"
)
# A reply that nobody @-ed asks for one sentence; past this many characters it is cut there.
ONE_SENTENCE_CHARS = 40
# Someone telling her to be quiet ("你别说话了", "闭嘴").
_QUIET = re.compile(r"(?:别|不要|甭|少)(?:再)?(?:说话|说了|插嘴|吵|讲了|讲话|回了|接话|bb|逼逼)|闭嘴|安静(?:点|一点|一会|会儿)|shut ?up", re.IGNORECASE)
# How long a reply to a chime-in still counts as talking to her.
_CHIME_IN_CONTINUATION_SECONDS = 20.0
# Talk with fewer different characters than this ("哈哈哈哈", "6666", "?") is not weighed.
_MIN_DISTINCT_CHARS = 4
_KEYWORD_MAX_CHARS = 8
_KEYWORD_SCORE = 0.6
_BURST_LINES = 6
_STASHED_IMAGES_MAX = 4
# What the character said in a group stops counting as the topic after this long.
_LAST_SAID_SECONDS = 600.0
_ARRIVALS_MAX = 4096
_NAME_CACHE_SECONDS = 60.0
_EMBED_CONCURRENCY = 8


@dataclass(slots=True)
class ChimeInLimits:
    threshold: float
    min_gap_seconds: float
    burst: int
    per_hour: float


@dataclass(slots=True)
class _Arrival:
    message_id: str
    # Monotonic times: the sender's latest message, and the first one not answered yet.
    at: float
    first_at: float
    answered: bool = False


class Listener:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        # (chat type, chat id, sender) -> their latest message.
        self._arrivals: dict[tuple[str, str, str], _Arrival] = {}
        # Group id -> when anyone last spoke there.
        self._group_spoke_at: dict[str, float] = {}
        # (group id, sender) -> until when that person counts as talking to the character.
        self._continuation: dict[tuple[str, str], float] = {}
        self._stashed_images: dict[tuple[str, str, str], list[MessageSegment]] = {}
        # Group id -> what the character said there last, and when.
        self._last_said: dict[str, tuple[str, float]] = {}
        # Group id -> the newest message nobody addressed to the character, and the check waiting on it.
        self._newest: dict[str, InboundEvent] = {}
        self._checks: dict[str, asyncio.Task[None]] = {}
        # The checks as futures that callers outside their event loop can wait on.
        self._check_futures: dict[str, concurrent.futures.Future[None]] = {}
        self._budget = TokenBucketLimiter()
        self._embedder = None
        self._vectors: dict[str, list[float]] = {}
        self._names: tuple[str, tuple[str, ...], float] = ("", (), 0.0)
        # Group id -> until when it keeps quiet there, after being told to.
        self._quiet_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._service.config.listen.enabled)

    # ------------------------------------------------------------------
    # Waiting for a person to finish
    # ------------------------------------------------------------------
    def note_inbound(self, event: InboundEvent) -> None:
        """Note a message's arrival; called for every message before it waits for its chat."""
        if not self.enabled:
            return
        now = time.monotonic()
        if event.launcher_type == "group":
            self._group_spoke_at[event.launcher_id] = now
        message_id = str(event.message_id or "").strip()
        if not message_id or not self._may_be_answered(event):
            return
        key = self._key(event)
        entry = self._arrivals.get(key)
        if entry is None or entry.answered:
            self._arrivals[key] = _Arrival(message_id, now, now)
        else:
            entry.message_id, entry.at = message_id, now
        if len(self._arrivals) > _ARRIVALS_MAX:
            for stale in [key for key, item in self._arrivals.items() if now - item.at > 600.0]:
                del self._arrivals[stale]

    async def await_turn(self, event: InboundEvent) -> bool:
        """Wait until the sender stops typing. ``False``: a newer message of theirs will be answered instead."""
        settings = self._service.config.listen
        message_id = str(event.message_id or "").strip()
        if not self.enabled or not message_id:
            return True
        key = self._key(event)
        while True:
            entry = self._arrivals.get(key)
            if entry is None:
                return True
            now = time.monotonic()
            if entry.message_id != message_id:
                if entry.answered:
                    return True  # what was noted is an earlier message, already answered
                self._stash_images(key, event)
                if event.launcher_type == "group":
                    # Their next message is still meant for the character, @ or not.
                    hold = now + float(settings.max_wait_seconds) + float(settings.quiet_seconds)
                    self._continuation[(event.launcher_id, str(event.sender_id))] = max(
                        hold, self._continuation.get((event.launcher_id, str(event.sender_id)), 0.0)
                    )
                return False
            due = min(entry.at + float(settings.quiet_seconds), entry.first_at + float(settings.max_wait_seconds))
            if now >= due:
                return True
            await asyncio.sleep(max(0.01, min(0.25, due - now)))

    def with_earlier_images(self, event: InboundEvent) -> InboundEvent:
        """``event`` with the pictures of the sender's superseded messages in front of its own."""
        stashed = self._stashed_images.pop(self._key(event), [])
        if not stashed:
            return event
        own = [segment for segment in event.segments if segment.kind == "image"]
        room = max(0, _STASHED_IMAGES_MAX - len(own))
        return dataclasses.replace(event, segments=[*stashed[-room:], *event.segments] if room else event.segments)

    def mark_answered(self, event: InboundEvent, message: OutboundMessage, *, reason: str = "") -> None:
        """A reply to ``event`` went out (``reason`` "chime_in" when nobody asked for it)."""
        if not self.enabled:
            return
        now = time.monotonic()
        entry = self._arrivals.get(self._key(event))
        if entry is not None:
            if entry.message_id == str(event.message_id or "").strip():
                entry.answered = True
            else:
                entry.first_at = entry.at  # newer messages wait from their own arrival
        if event.launcher_type == "group":
            # An @ or her name starts a talk with that person; so, briefly, does joining in on
            # their line. Answering a line of that talk does not extend it: otherwise she
            # answered everything a person said for as long as they kept talking.
            hold = 0.0
            if reason == "chime_in":
                hold = _CHIME_IN_CONTINUATION_SECONDS
            elif self.explicitly_addressed(event):
                hold = float(self._service.config.listen.continuation_seconds)
            key = (event.launcher_id, str(event.sender_id))
            if hold > 0:
                self._continuation[key] = max(self._continuation.get(key, 0.0), now + hold)
            said = strip_sticker_tags(message.text).strip()
            if said:
                self._last_said[event.launcher_id] = (said, now)

    def explicitly_addressed(self, event: InboundEvent) -> bool:
        """An @ of the bot, its name or call name, or a quote-reply to her: not just a line in an ongoing talk."""
        bot = str(self._service.config.bot_account_id or "")
        return (
            event.launcher_type != "group"
            or event.has_bot_mention(bot)
            or self._name_called(event)
            or self.replies_to_her(event)
        )

    def replies_to_her(self, event: InboundEvent) -> bool:
        """A quote-reply (QQ's 回复) to something she said: as much for her as an @."""
        return self._service.emitter.sent_by_her(event.reply_to_id)

    def asks_for_quiet(self, event: InboundEvent) -> bool:
        """"你别说话了", "闭嘴": in a group she then keeps quiet for ``listen.quiet_minutes``.

        Only an @ or her name gets an answer meanwhile: no chime-ins, no talking on.
        """
        if not self.enabled or event.launcher_type != "group":
            return False
        if not _QUIET.search(event.command_text(self._service.config.bot_account_id)):
            return False
        minutes = float(self._service.config.listen.quiet_minutes)
        self._quiet_until[event.launcher_id] = time.monotonic() + minutes * 60.0
        for key in [key for key in self._continuation if key[0] == event.launcher_id]:
            del self._continuation[key]
        self._newest.pop(event.launcher_id, None)
        _LOGGER.info("told to keep quiet group=%s by=%s minutes=%.0f", event.launcher_id, event.sender_id, minutes)
        return True

    def keeping_quiet(self, group: str) -> bool:
        return time.monotonic() < self._quiet_until.get(group, 0.0)

    def spoke_recently(self, group: str) -> bool:
        """Whether the character said something in this group within its minimum gap between chime-ins."""
        last = self._last_said.get(group)
        return last is not None and time.monotonic() - last[1] < self.limits().min_gap_seconds

    def limits(self) -> ChimeInLimits:
        """The ``listen`` settings, with what the active card's meta ``chime_in`` changes."""
        svc = self._service
        settings = svc.config.listen
        custom = svc.cards.character_meta(svc._active_character_id()).get("chime_in")
        custom = custom if isinstance(custom, dict) else {}

        def number(key: str, default: float) -> float:
            try:
                return max(0.0, float(custom.get(key, default)))
            except (TypeError, ValueError):
                return float(default)

        return ChimeInLimits(
            threshold=number("threshold", settings.chime_in_threshold),
            min_gap_seconds=number("min_gap_seconds", settings.chime_in_min_gap_seconds),
            burst=int(number("burst", settings.chime_in_burst)),
            per_hour=number("per_hour", settings.chime_in_per_hour),
        )

    def addresses_bot(self, event: InboundEvent) -> bool:
        """Whether a group message without an @ is still for the character: its name, or its conversation."""
        if self._name_called(event) or self.replies_to_her(event):
            return True
        if self.keeping_quiet(event.launcher_id):
            return False
        bot = str(self._service.config.bot_account_id or "")
        if any(target != bot for target in event.mentioned_targets):
            return False  # they @ someone else: that line is for them
        deadline = self._continuation.get((event.launcher_id, str(event.sender_id)))
        return deadline is not None and time.monotonic() <= deadline

    # ------------------------------------------------------------------
    # Joining in when nobody asked
    # ------------------------------------------------------------------
    def consider_chiming_in(self, event: InboundEvent) -> None:
        """A group message nobody addressed to the character: weigh the talk once the group is quiet."""
        if not self.enabled or event.launcher_type != "group" or not self._may_be_answered(event):
            return
        config = self._service.config
        text = event.command_text(config.bot_account_id).strip()
        if not text:
            return  # a picture or sticker alone is not a topic
        if any(prefix and text.startswith(prefix) for prefix in (*config.explicit_skill_prefixes, config.image_command_prefix)):
            return  # a command nobody addressed to the character
        group = event.launcher_id
        self._newest[group] = event
        task = self._checks.get(group)
        if task is not None and not task.done():
            return
        task = asyncio.get_running_loop().create_task(self._check_when_quiet(group))
        mirror: concurrent.futures.Future[None] = concurrent.futures.Future()

        def _finished(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                mirror.cancel()
            elif completed.exception() is not None:
                _LOGGER.error("chime-in check failed group=%s", group, exc_info=completed.exception())
                mirror.set_exception(completed.exception())
            else:
                mirror.set_result(None)

        task.add_done_callback(_finished)
        self._checks[group], self._check_futures[group] = task, mirror

    def drain(self, timeout: float = 10.0) -> None:
        """Wait until no chime-in check is pending (tests, scripts)."""
        deadline = time.monotonic() + timeout
        while True:
            pending = [future for future in list(self._check_futures.values()) if not future.done()]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("chime-in checks did not finish before timeout")
            concurrent.futures.wait(pending, timeout=remaining)

    def close(self) -> None:
        for task in list(self._checks.values()):
            if not task.done():
                try:
                    task.get_loop().call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # the loop is already closed
        close = getattr(self._embedder, "close", None)
        if callable(close):
            close()

    async def _check_when_quiet(self, group: str) -> None:
        settings = self._service.config.listen
        while True:
            quiet_at = self._group_spoke_at.get(group, 0.0) + float(settings.group_quiet_seconds)
            remaining = quiet_at - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 1.0))
                continue
            event = self._newest.pop(group, None)
            if event is not None:
                await self._consider(event)
            return

    async def _consider(self, event: InboundEvent) -> None:
        svc = self._service
        limits = self.limits()
        group = event.launcher_id
        if self.keeping_quiet(group):
            return  # told to keep quiet
        if self.spoke_recently(group):
            return  # it spoke here a moment ago
        if svc._session_lock_for(event).locked():
            return  # a reply is being written in this group right now
        if svc.tavern.running(event.launcher_type, event.launcher_id):
            return  # it is the game master there now
        lines = svc._peek_ambient_history(event)[-_BURST_LINES:]
        if not lines:
            return
        # What was said, without the "name: " in front: names only blur the comparison.
        talk = "\n".join(line.partition(": ")[2] or line for line in lines)
        if len(set(re.sub(r"[\W_]", "", talk))) < _MIN_DISTINCT_CHARS:
            return  # laughter and one-word noises
        score, reason = await self._score(group, talk)
        if score < limits.threshold:
            # Kept at INFO so the threshold can be tuned from real talk.
            _LOGGER.info("no chime-in group=%s score=%.2f reason=%s", group, score, reason or "-")
            return
        bucket = (f"chime-in:{svc._active_character_id()}:{group}", limits.burst, limits.per_hour / 3600.0)
        if limits.burst <= 0 or self._budget.acquire([bucket]) > 0:
            _LOGGER.info("chime-in skipped group=%s score=%.2f reason=%s: budget used up", group, score, reason)
            return
        if svc.tavern.running(event.launcher_type, event.launcher_id):
            return  # a game started while the talk was being weighed
        _LOGGER.info("chime-in group=%s score=%.2f reason=%s", group, score, reason)
        await svc._process_chime_in(event)

    async def _score(self, group: str, talk: str) -> tuple[float, str]:
        """How much the talk is the character's business, from 0 to 1, and why."""
        interests = self._interests()
        lowered = talk.lower()
        best = (0.0, "")
        for item in interests:
            if len(item) <= _KEYWORD_MAX_CHARS and item.lower() in lowered:
                best = (_KEYWORD_SCORE, f"keyword {item}")
                break
        embedder = self._embedding_client()
        if embedder is None:
            return best
        try:
            talk_vector = await embedder.aembed(talk)
            await self._embed_all(embedder, interests)
            for item in interests:
                similarity = _cosine(talk_vector, await self._vector(embedder, item))
                if similarity > best[0]:
                    best = (similarity, f"interest {item[:16]}")
            last = self._last_said.get(group)
            if last is not None and time.monotonic() - last[1] <= _LAST_SAID_SECONDS:
                similarity = _cosine(talk_vector, await self._vector(embedder, last[0]))
                if similarity > best[0]:
                    best = (similarity, "follows what it said")
        except Exception as exc:  # scoring falls back to keywords; it must not stop the chat
            _LOGGER.warning("chime-in scoring without embeddings: %s", exc)
        return best

    async def _embed_all(self, embedder, texts: list[str]) -> None:  # type: ignore[no-untyped-def]
        """Embed the texts not cached yet a few at a time: one by one, 40 interests took 12 s."""
        missing = [text for text in dict.fromkeys(texts) if text not in self._vectors]
        if not missing:
            return
        if len(self._vectors) + len(missing) > 256:
            self._vectors.clear()
        gate = asyncio.Semaphore(_EMBED_CONCURRENCY)

        async def one(text: str) -> tuple[str, list[float]]:
            async with gate:
                return text, await embedder.aembed(text)

        for text, vector in await asyncio.gather(*(one(text) for text in missing)):
            self._vectors[text] = vector

    async def _vector(self, embedder, text: str) -> list[float]:  # type: ignore[no-untyped-def]
        vector = self._vectors.get(text)
        if vector is None:
            vector = await embedder.aembed(text)
            if len(self._vectors) > 256:
                self._vectors.clear()
            self._vectors[text] = vector
        return vector

    def _embedding_client(self):  # type: ignore[no-untyped-def]
        if self._embedder is None:
            self._embedder = build_embedding_client(self._service.config)
        return self._embedder if getattr(self._embedder, "ready", False) else None

    # ------------------------------------------------------------------
    # The character
    # ------------------------------------------------------------------
    def _interests(self) -> list[str]:
        """The card meta's ``interests``, or else its persona and skill lines."""
        svc = self._service
        character = svc._active_character_id()
        meta = svc.cards.character_meta(character)
        listed = meta.get("interests")
        if isinstance(listed, list):
            items = [str(item).strip() for item in listed if str(item).strip()]
            if items:
                return items[:20]
        card = svc.cards.load("group", SessionMemory(launcher_id="", launcher_type="group", character_id=character))
        return [line for line in (*card.profile, *card.skills) if str(line).strip()][:12]

    def _name_called(self, event: InboundEvent) -> bool:
        text = event.command_text(self._service.config.bot_account_id)
        return any(name in text for name in self._call_names())

    def _call_names(self) -> tuple[str, ...]:
        """The card's name and the meta ``call_names``; aliases are left out ("姐姐" is too common)."""
        svc = self._service
        character = svc._active_character_id()
        cached_character, names, at = self._names
        if cached_character == character and time.monotonic() - at < _NAME_CACHE_SECONDS:
            return names
        card = svc.cards.load("group", SessionMemory(launcher_id="", launcher_type="group", character_id=character))
        extra = svc.cards.character_meta(character).get("call_names")
        candidates = [str(card.assistant_name or ""), *(extra if isinstance(extra, list) else [])]
        names = tuple(dict.fromkeys(name for name in (str(item).strip() for item in candidates) if len(name) >= 2))
        self._names = (character, names, time.monotonic())
        return names

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _may_be_answered(self, event: InboundEvent) -> bool:
        """Messages the reply flow drops at once (ignored senders, "/…") neither wait nor make others wait."""
        config = self._service.config
        sender = str(event.sender_id or "").strip()
        if sender == str(config.bot_account_id or "").strip():
            return False
        if sender in {str(uid).strip() for uid in config.ignore_user_ids}:
            return False
        text = event.command_text(config.bot_account_id).strip()
        if not text:
            return event.image_count > 0
        if any(text.startswith(prefix) for prefix in config.explicit_skill_prefixes if prefix):
            return True
        return not any(prefix and text.startswith(prefix) for prefix in config.ignore_prefixes)

    @staticmethod
    def _key(event: InboundEvent) -> tuple[str, str, str]:
        return (str(event.launcher_type), str(event.launcher_id), str(event.sender_id))

    def _stash_images(self, key: tuple[str, str, str], event: InboundEvent) -> None:
        images = [segment for segment in event.segments if segment.kind == "image"]
        if images:
            stash = self._stashed_images.setdefault(key, [])
            stash.extend(images)
            del stash[:-_STASHED_IMAGES_MAX]


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def one_sentence(text: str, *, names: tuple[str, ...] = (), limit: int = ONE_SENTENCE_CHARS) -> str:
    """The first sentence of ``text``, without a leading "名字，", cut at ``limit`` characters."""
    text = strip_sticker_tags(str(text or "")).strip()
    for name in names:
        name = str(name or "").strip()
        if name and re.match(rf"{re.escape(name)}\s*[，,：:]", text):
            text = text[len(name):].lstrip(" ，,：:")
            break
    match = re.search(r"[。！？!?~～]+[”’」』）)]*|\n", text)
    head = (text[: match.end()] if match else text).strip()
    if len(head) > limit:
        head = head[:limit].rstrip("，,、；; ") + "…"
    return head


def is_silence(text: str) -> bool:
    """Whether a chime-in reply is the model choosing to say nothing ("[不说话]", "【不说话】", …)."""
    compact = re.sub(r"[\s\[\]【】()（）「」]", "", str(text or ""))
    return not compact or "不说话" in compact and len(compact) <= len("不说话") + 2
