"""SillyTavern plays the tavern's cards; this drives it.

SillyTavern builds a card's prompt in its web page, not on its server, so a headless
browser (``tavern.browser_url``, Chrome's DevTools protocol) keeps that page
(``tavern.sillytavern_url``) open, and the tavern calls what ``sillytavern_page.js`` puts
there: open a card's chat, send the players' round as the user's message, and read the
reply SillyTavern wrote. Prompt order, lorebooks, regex scripts, presets and the model call
are all SillyTavern's own. One page plays one chat at a time, so calls take turns.

The first call sets SillyTavern up (``_setup``):
- its chat-completion connection is the tavern's model (``tavern.llm_*``, else an
  OpenAI-compatible ``llm``), sent again only when that changes, so a preset or sampler
  changed in SillyTavern's own page stays; once, its reasoning "Auto-Parse" is turned on,
  so a model's <think> block stays out of the reply and the history;
- the bundled cards (``templates/tavern/*.json``) and the card files in
  ``<data_root>/tavern/scenarios/`` and ``adult/`` are imported, again when a file changes;
- games saved before SillyTavern played them become SillyTavern chats.
What was done is kept in ``<data_root>/tavern/sillytavern.json``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import itertools
import json
import logging
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

PAGE_SCRIPT = (Path(__file__).with_name("sillytavern_page.js")).read_text(encoding="utf-8")
_PAGE_VERSION = 2
_MISSING = "__openqqwaifu_missing__"
BUNDLED_DIR = Path(__file__).resolve().parents[1] / "templates" / "tavern"
_CARD_SUFFIXES = (".png", ".json")
# The values SillyTavern's reasoning-effort setting takes.
_REASONING = {"auto", "min", "low", "medium", "high", "max"}
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
_LOAD_SECONDS = 90.0
_CALL_SECONDS = 60.0
_SETUP_RETRY_SECONDS = 300.0


class BridgeError(RuntimeError):
    """SillyTavern or its browser did not do what was asked."""


class _Tab:
    """One browser tab, driven over Chrome's DevTools protocol."""

    def __init__(self, http: aiohttp.ClientSession, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._http = http
        self._ws = ws
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.closed = False
        self._reader = asyncio.get_running_loop().create_task(self._read())

    @classmethod
    async def open(cls, browser_url: str, page_url: str) -> _Tab:
        http = aiohttp.ClientSession()
        try:
            base = await _address(browser_url)
            async with http.get(f"{base}/json/list", timeout=_HTTP_TIMEOUT) as response:
                targets = await response.json(content_type=None)
            pages = [target for target in targets if target.get("type") == "page"]
            origin = _origin(page_url)
            target = next((item for item in pages if str(item.get("url", "")).startswith(origin)), None)
            if target is None and pages:
                target = pages[0]
            if target is None:
                async with http.put(f"{base}/json/new?about:blank", timeout=_HTTP_TIMEOUT) as response:
                    target = await response.json(content_type=None)
            ws_url = _on_host(str(target["webSocketDebuggerUrl"]), base)
            ws = await http.ws_connect(ws_url, max_msg_size=0, heartbeat=30.0)
        except BaseException:
            await http.close()
            raise
        return cls(http, ws)

    async def _read(self) -> None:
        try:
            async for message in self._ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                future = self._waiting.pop(data["id"], None) if "id" in data else None
                if future is not None and not future.done():
                    future.set_result(data)
                elif data.get("method") in {"Inspector.detached", "Inspector.targetCrashed"}:
                    break
        except (aiohttp.ClientError, ValueError) as exc:
            _LOGGER.warning("sillytavern browser connection broke: %s", exc)
        finally:
            self.closed = True
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(BridgeError("the browser tab went away"))
            self._waiting.clear()

    async def send(self, method: str, timeout: float, **params: Any) -> dict[str, Any]:
        if self.closed:
            raise BridgeError("the browser tab went away")
        message_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[message_id] = future
        await self._ws.send_json({"id": message_id, "method": method, "params": params})
        try:
            data = await asyncio.wait_for(future, timeout)
        finally:
            self._waiting.pop(message_id, None)
        if "error" in data:
            raise BridgeError(f"{method}: {data['error'].get('message', data['error'])}")
        return data.get("result", {})

    async def evaluate(self, expression: str, timeout: float) -> Any:
        result = await self.send(
            "Runtime.evaluate", timeout, expression=expression, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or "script error"
            raise BridgeError(str(text).splitlines()[0][:300])
        return (result.get("result") or {}).get("value")

    async def close(self) -> None:
        self.closed = True
        self._reader.cancel()
        await self._ws.close()
        await self._http.close()


class SillyTavernBridge:
    def __init__(self, service: "WaifuService") -> None:
        self._service = service
        self.root = Path(service.config.data_root) / "tavern"
        self.state_path = self.root / "sillytavern.json"
        self._tab: _Tab | None = None
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loaded = False
        # When to set SillyTavern up (again): at the first call, and a while after a failed try.
        self._setup_due = 0.0

    @property
    def configured(self) -> bool:
        settings = self._service.config.tavern
        return bool(str(settings.sillytavern_url).strip() and str(settings.browser_url).strip())

    # ------------------------------------------------------------------
    # What the tavern asks for
    # ------------------------------------------------------------------
    async def ensure(self) -> None:
        """SillyTavern loaded and set up (the cards imported, old saves moved over)."""
        if not self.configured:
            raise BridgeError("tavern.sillytavern_url and tavern.browser_url are not set")
        async with self._guard():
            await self._ready()

    async def characters(self) -> list[dict[str, Any]]:
        return list(await self._call("characters", {}) or [])

    def card(self, avatar: str) -> dict[str, Any]:
        """What the tavern knows of a card it imported: ``adult``, ``picture``, ``bundled`` and the
        file it came from (``key``); ``{}`` for a card added in SillyTavern itself."""
        cards = self._state().get("cards", {})
        return next(({**entry, "key": key} for key, entry in cards.items() if entry.get("avatar") == avatar), {})

    async def start(self, avatar: str, *, user: str, chat: str, greeting: int = 0) -> dict[str, str]:
        """A new chat with the card; its file name (``chat``) and the greeting it opens with."""
        return dict(await self._call("start", {"avatar": avatar, "user": user, "chat": chat, "greeting": greeting}))

    async def resume(self, avatar: str, chat: str) -> str:
        return str((await self._call("resume", {"avatar": avatar, "chat": chat}) or {}).get("last", ""))

    async def turn(self, avatar: str, chat: str, *, user: str, text: str, note: str) -> str:
        options = {"avatar": avatar, "chat": chat, "user": user, "text": text, "note": note}
        try:
            result = await self._call("turn", options, timeout=self._reply_seconds())
        except asyncio.TimeoutError:
            await self._stop()
            raise BridgeError("SillyTavern took too long to reply") from None
        return str((result or {}).get("reply", ""))

    async def rewrite(self, avatar: str, chat: str, *, user: str, note: str, instruction: str) -> str:
        """The last reply written again by ``instruction``, without adding to the chat."""
        options = {"avatar": avatar, "chat": chat, "user": user, "note": note, "instruction": instruction}
        try:
            result = await self._call("quiet", options, timeout=self._reply_seconds())
        except asyncio.TimeoutError:
            await self._stop()
            raise BridgeError("SillyTavern took too long to reply") from None
        return str((result or {}).get("text", ""))

    async def replace_last(self, avatar: str, chat: str, text: str) -> None:
        await self._call("replaceLast", {"avatar": avatar, "chat": chat, "text": text})

    async def chats(self, avatar: str) -> list[str]:
        return [str(name) for name in await self._call("chats", {"avatar": avatar}) or []]

    async def picture(self, avatar: str) -> bytes:
        data = await self._call("picture", {"avatar": avatar})
        return base64.b64decode(data) if data else b""

    async def import_file(self, path: Path, *, adult: bool) -> dict[str, Any]:
        """Import (or update) a card file kept under ``tavern/``; its catalog entry."""
        async with self._guard():
            await self._ready()
            entry = await self._import(path, adult=adult, bundled=False)
            return dict(entry)

    async def aclose(self) -> None:
        tab, self._tab = self._tab, None
        if tab is not None:
            await tab.close()

    # ------------------------------------------------------------------
    # The page
    # ------------------------------------------------------------------
    def _guard(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            # A tab and its lock belong to one event loop.
            self._lock, self._loop, self._tab, self._loaded = asyncio.Lock(), loop, None, False
        return self._lock

    async def _call(self, name: str, options: dict[str, Any], *, timeout: float = _CALL_SECONDS) -> Any:
        if not self.configured:
            raise BridgeError("tavern.sillytavern_url and tavern.browser_url are not set")
        async with self._guard():
            await self._ready()
            return await self._run(name, options, timeout)

    async def _run(self, name: str, options: dict[str, Any], timeout: float) -> Any:
        expression = (
            f"(window.openqqwaifu && window.openqqwaifu.version === {_PAGE_VERSION})"
            f" ? window.openqqwaifu.{name}({json.dumps(options, ensure_ascii=False)}) : {json.dumps(_MISSING)}"
        )
        assert self._tab is not None
        try:
            result = await self._tab.evaluate(expression, timeout)
            if result == _MISSING:  # the page was reloaded since
                self._loaded = False
                await self._ready()
                result = await self._tab.evaluate(expression, timeout)
        except BridgeError:
            # Load the page afresh next time: SillyTavern may have restarted under it.
            self._loaded = False
            raise
        return result

    async def _ready(self) -> None:
        """An open tab with SillyTavern loaded and set up."""
        settings = self._service.config.tavern
        page_url = str(settings.sillytavern_url).strip()
        if self._tab is None or self._tab.closed:
            if self._tab is not None:
                await self._tab.close()
            self._tab = await _Tab.open(str(settings.browser_url).strip(), page_url)
            self._loaded = False
        if not self._loaded:
            tab = self._tab
            href = await tab.evaluate("location.href", 10)
            fresh = not str(href).startswith(_origin(page_url))
            if not fresh:
                # A page SillyTavern restarted under answers with stale tokens; load it again.
                fresh = not await tab.evaluate("typeof window.SillyTavern === 'object'", 10)
            await tab.send("Page.navigate", 15, url=page_url) if fresh else await tab.send("Page.reload", 15)
            await self._wait_for_page(tab)
            await tab.evaluate(PAGE_SCRIPT, 10)
            await tab.evaluate("window.openqqwaifu.ready()", _LOAD_SECONDS)
            self._loaded = True
            _LOGGER.info("sillytavern page loaded url=%s", page_url)
        if time.monotonic() >= self._setup_due:
            self._setup_due = float("inf")
            if not await self._setup():
                self._setup_due = time.monotonic() + _SETUP_RETRY_SECONDS

    @staticmethod
    async def _wait_for_page(tab: _Tab) -> None:
        deadline = time.monotonic() + _LOAD_SECONDS
        await asyncio.sleep(0.5)
        while time.monotonic() < deadline:
            try:
                if await tab.evaluate(
                    "document.readyState === 'complete' && typeof window.SillyTavern?.getContext === 'function'", 10
                ):
                    return
            except BridgeError:
                pass  # the old document went away mid-call
            await asyncio.sleep(0.5)
        raise BridgeError("SillyTavern's page did not load")

    async def _stop(self) -> None:
        if self._tab is not None and not self._tab.closed:
            try:
                await self._tab.evaluate("window.openqqwaifu && window.openqqwaifu.stop()", 5)
            except (BridgeError, asyncio.TimeoutError):
                pass

    def _reply_seconds(self) -> float:
        return max(30.0, float(self._service.config.tavern.llm_timeout_seconds or 120.0)) + 30.0

    # ------------------------------------------------------------------
    # Setup: the model, the cards, the old saves
    # ------------------------------------------------------------------
    async def _setup(self) -> bool:
        done = True
        for step in (self._connect, self._parse_reasoning, self._sync_cards, self._move_saves):
            try:
                await step()
            except (BridgeError, asyncio.TimeoutError, OSError, ValueError, KeyError) as exc:
                done = False
                _LOGGER.warning("sillytavern setup step %s failed: %s", step.__name__, exc)
        return done

    def _model(self) -> tuple[str, str, str, str]:
        """The tavern's model as (url, key, model, reasoning effort); empty when there is none to send."""
        config = self._service.config
        tavern, llm = config.tavern, config.llm
        if tavern.llm_base_url and tavern.llm_api_key and tavern.llm_model:
            return tavern.llm_base_url, tavern.llm_api_key, tavern.llm_model, tavern.llm_reasoning_effort
        if llm.enabled and llm.base_url and llm.model and str(llm.backend).strip().lower() not in {"dify", "claude"}:
            return llm.base_url, llm.api_key, llm.model, llm.reasoning_effort
        return "", "", "", ""

    async def _connect(self) -> None:
        url, key, model, effort = self._model()
        if not (url and model):
            _LOGGER.info("sillytavern keeps its own connection: no OpenAI-compatible tavern model is set")
            return
        effort = str(effort or "").strip().lower()
        fingerprint = hashlib.sha256("\n".join((url, model, key, effort)).encode("utf-8")).hexdigest()
        state = self._state()
        current = await self._run("connection", {}, _CALL_SECONDS) or {}
        fresh = current.get("api") != "openai" or current.get("source") != "custom" or not current.get("url")
        if state.get("connection") == fingerprint and not fresh:
            return
        options = {
            "url": url,
            "model": model,
            "key": key,
            "reasoning": effort if effort in _REASONING else "",
            "firstTime": not state.get("defaults"),
            "contextTokens": int(self._service.config.tavern.context_tokens),
            "replyTokens": int(self._service.config.tavern.reply_tokens),
        }
        result = await self._run("connect", options, _CALL_SECONDS) or {}
        state["connection"] = fingerprint
        state["defaults"] = True
        self._save_state(state)
        _LOGGER.info("sillytavern connected to the tavern model model=%s status=%s", model, result.get("online"))

    async def _parse_reasoning(self) -> None:
        state = self._state()
        if state.get("reasoning_parsed"):
            return
        await self._run("parseReasoning", {}, _CALL_SECONDS)
        state = self._state()
        state["reasoning_parsed"] = True
        self._save_state(state)
        _LOGGER.info("sillytavern parses reasoning out of replies now")

    async def _sync_cards(self) -> None:
        """Bundled cards, then the card folders; each imported once, and again when its file changes."""
        sources: list[tuple[Path, bool, bool]] = [(path, False, True) for path in _card_files(BUNDLED_DIR)]
        sources += [(path, False, False) for path in _card_files(self.root / "scenarios")]
        sources += [(path, True, False) for path in _card_files(self.root / "adult")]
        for path, adult, bundled in sources:
            try:
                await self._import(path, adult=adult, bundled=bundled)
            except (BridgeError, asyncio.TimeoutError, OSError) as exc:
                _LOGGER.warning("sillytavern card %s not imported: %s", path.name, exc)

    async def _import(self, path: Path, *, adult: bool, bundled: bool) -> dict[str, Any]:
        key = self._card_key(path, bundled=bundled)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        state = self._state()
        cards = state.setdefault("cards", {})
        entry = cards.get(key)
        if entry is None:
            # The same card sent again in another format took the place of the earlier file.
            twin = next((other for other in cards if Path(other).with_suffix("") == Path(key).with_suffix("")), None)
            entry = cards.get(twin) if twin else None
        if entry and entry.get("sha") == digest:
            return entry
        replace = Path(entry["avatar"]).stem if entry and entry.get("avatar") else ""
        options = {
            "data": base64.b64encode(data).decode("ascii"),
            "type": "png" if path.suffix.lower() == ".png" else "json",
            "replace": replace,
        }
        result = await self._run("importCard", options, _CALL_SECONDS)
        entry = {
            "avatar": str(result["avatar"]),
            "name": str(result.get("name", "")),
            "sha": digest,
            "adult": adult,
            # SillyTavern gives a card without a picture of its own a stock one.
            "picture": path.suffix.lower() == ".png",
            "bundled": bundled,
        }
        state = self._state()
        cards = state.setdefault("cards", {})
        for other in [other for other in cards if other != key and cards[other].get("avatar") == entry["avatar"]]:
            del cards[other]
        cards[key] = entry
        self._save_state(state)
        _LOGGER.info("sillytavern card imported file=%s avatar=%s replaced=%s", key, entry["avatar"], bool(replace))
        return entry

    def _card_key(self, path: Path, *, bundled: bool) -> str:
        if bundled:
            return f"bundled/{path.name}"
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.name

    async def _move_saves(self) -> None:
        """Games saved with their story (before SillyTavern played them) become SillyTavern chats."""
        state = self._state()
        cards = state.get("cards", {})
        for folder in (self.root / "sessions", self.root / "archive"):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, dict) or payload.get("chat") or not payload.get("turns"):
                    continue
                avatar = _old_scenario_avatar(str(payload.get("scenario_id", "")), cards)
                if not avatar:
                    continue
                try:
                    await self._move_save(path, payload, avatar)
                except (BridgeError, asyncio.TimeoutError, OSError, KeyError, TypeError) as exc:
                    _LOGGER.warning("sillytavern save %s not moved: %s", path.name, exc)

    async def _move_save(self, path: Path, payload: dict[str, Any], avatar: str) -> None:
        turns = [turn for turn in payload["turns"] if isinstance(turn, dict) and turn.get("role") in {"user", "assistant"}]
        messages = [{"user": turn["role"] == "user", "text": str(turn.get("content", ""))} for turn in turns]
        summary = str(payload.get("summary") or "").strip()
        if summary:
            # The rounds folded into the recap are gone; the recap stands in for them.
            at = 1 if messages and not messages[0]["user"] else 0
            messages.insert(at, {"user": True, "text": f"（前情提要）{summary}"})
        players = dict(payload.get("players") or {})
        user = "、".join(str(name) for name in players.values()) or str(payload.get("host_name") or "玩家")
        started = float(payload.get("started_at") or time.time())
        chat = chat_name(str(payload.get("launcher_type", "")), str(payload.get("launcher_id", "")), started)
        options = {"avatar": avatar, "chat": chat, "user": user, "created": started, "messages": messages}
        await self._run("saveChat", options, _CALL_SECONDS)
        last = next((turn["content"] for turn in reversed(turns) if turn["role"] == "assistant"), "")
        payload.update(scenario_id=avatar, chat=chat, last_reply=str(last))
        for old in ("turns", "summary"):
            payload.pop(old, None)
        _write_json(path, payload)
        _LOGGER.info("sillytavern save moved file=%s chat=%s messages=%s", path.name, chat, len(messages))

    def _state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1}
        return payload if isinstance(payload, dict) else {"version": 1}

    def _save_state(self, state: dict[str, Any]) -> None:
        _write_json(self.state_path, state)


def chat_name(launcher_type: str, launcher_id: str, started_at: float) -> str:
    """The SillyTavern chat a game is played in: "QQ群123456 1029-193005"."""
    place = "QQ群" if launcher_type == "group" else "QQ私聊"
    return f"{place}{launcher_id} {time.strftime('%m%d-%H%M%S', time.localtime(started_at))}"


def _old_scenario_avatar(scenario_id: str, cards: dict[str, Any]) -> str:
    """The SillyTavern card an old save's scenario became: a bundled story, or a card file."""
    adult = scenario_id.startswith("adult/")
    stem = scenario_id.split("/", 1)[1] if adult else scenario_id
    folders = ("adult/",) if adult else ("bundled/", "scenarios/")
    for key, entry in cards.items():
        folder, _, name = key.rpartition("/")
        if f"{folder}/" in folders and Path(name).stem == stem:
            return str(entry.get("avatar", ""))
    return ""


def _card_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [path for path in sorted(folder.iterdir()) if path.is_file() and path.suffix.lower() in _CARD_SUFFIXES]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text.encode("utf-16", "surrogatepass").decode("utf-16", "replace"))
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def _address(browser_url: str) -> str:
    """The browser's DevTools address by IP: Chrome answers only to an IP or "localhost" as the host."""
    parts = urlsplit(browser_url if "://" in browser_url else f"http://{browser_url}")
    host, port = parts.hostname or "localhost", parts.port or 9222
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host != "localhost":
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            host = infos[0][4][0]
    return f"http://{host}:{port}"


def _on_host(ws_url: str, base: str) -> str:
    """The tab's WebSocket address at the host and port we reached the browser on."""
    parts = urlsplit(ws_url)
    return urlunsplit(("ws", urlsplit(base).netloc, parts.path, parts.query, ""))
