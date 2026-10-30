from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, TavernConfig
from waifu_standalone.infra.llm_clients import LLMToolResponse
from waifu_standalone.infra.safe_fetch import FetchedPage
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.settings_admin.config_manager import _load_dataclass
from waifu_standalone.tavern import dice as dice_module
from waifu_standalone.tavern.scenarios import read_png_card, scenario_from_character
from waifu_standalone.tavern.sillytavern import BUNDLED_DIR, BridgeError, _Tab

BOT_ID = "3956638110"
GROUP_ID = "612475113"
HOST = "783190298"
PLAYER = "1147026356"
OUTSIDER = "2747277822"
NAMES = {HOST: "资本狗", PLAYER: "苦力怕", OUTSIDER: "路人"}
_CALL = re.compile(r"\? window\.openqqwaifu\.(?P<name>\w+)\((?P<options>.*)\) : \"__openqqwaifu_missing__\"$", re.DOTALL)


def _bundled(stem: str) -> dict[str, object]:
    return json.loads((BUNDLED_DIR / f"{stem}.json").read_text(encoding="utf-8"))["data"]


def _group(text: str, *, sender: str = HOST, mention: bool = False) -> InboundEvent:
    segments = [MessageSegment(kind="mention", mention_target=BOT_ID)] if mention else []
    segments.append(MessageSegment(kind="text", text=text))
    return InboundEvent(GROUP_ID, "group", sender, NAMES[sender], segments)


def _private(text: str) -> InboundEvent:
    return InboundEvent(HOST, "person", HOST, NAMES[HOST], [MessageSegment(kind="text", text=text)])


def _png_card(card: dict[str, object]) -> bytes:
    """A 1x1 PNG with a SillyTavern card in its ``chara`` text chunk."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    text = b"chara\x00" + base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"tEXt", text)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b"")
    )


class _SillyTavern:
    """SillyTavern's page as the tavern's browser tab reaches it: what ``sillytavern_page.js`` does,
    done on a few dicts. ``narrate`` writes each reply from the turn's options."""

    def __init__(self) -> None:
        self.closed = False
        self.web_revision = 1
        self.characters: dict[str, dict[str, object]] = {}
        self.pictures: dict[str, bytes] = {}
        self.chats: dict[str, dict[str, list[dict[str, object]]]] = {}
        self.connection = {"api": "koboldhorde", "source": "openai", "url": "", "model": ""}
        self.connects: list[dict[str, object]] = []
        self.reasoning_parsed = 0
        self.extensions: set[str] = set()
        self.installs: list[dict[str, object]] = []
        self.deletes: list[str] = []
        self.kept: list[int] = []
        self.loads = 0
        self.imports: list[dict[str, object]] = []
        self.turns: list[dict[str, object]] = []
        self.quiet: list[dict[str, object]] = []
        self.narrate = lambda options: f"第{len(self.turns)}段剧情。"
        self.down = False

    def add(self, avatar: str, data: dict[str, object], picture: bytes = b"") -> None:
        """A character added in SillyTavern's own page."""
        self.characters[avatar] = dict(data)
        self.pictures[avatar] = picture

    async def send(self, method: str, timeout: float, **params: object) -> dict[str, object]:
        if method in {"Page.navigate", "Page.reload"}:
            self.loads += 1
        return {}

    async def evaluate(self, expression: str, timeout: float) -> object:
        if self.down:
            raise BridgeError("the browser tab went away")
        if expression == "location.href":
            return "http://sillytavern:8000/"
        call = _CALL.search(expression)
        if call is None:
            return True  # loading the page, putting the script in, waiting for it
        options = json.loads(call.group("options"))
        return getattr(self, "do_" + call.group("name"))(options)

    async def close(self) -> None:
        self.closed = True

    def do_webState(self, options):  # type: ignore[no-untyped-def]
        return {"revision": self.web_revision}

    def do_connection(self, options):  # type: ignore[no-untyped-def]
        return dict(self.connection)

    def do_connect(self, options):  # type: ignore[no-untyped-def]
        self.connects.append(options)
        self.connection = {"api": "openai", "source": "custom", "url": options["url"], "model": options["model"]}
        return {"online": "Valid"}

    def do_parseReasoning(self, options):  # type: ignore[no-untyped-def]
        self.reasoning_parsed += 1
        return True

    def do_extensions(self, options):  # type: ignore[no-untyped-def]
        return sorted(self.extensions)

    def do_installExtension(self, options):  # type: ignore[no-untyped-def]
        self.installs.append(options)
        self.extensions.add("third-party/" + str(options["url"]).rsplit("/", 1)[-1])
        return True

    def do_deleteExtension(self, options):  # type: ignore[no-untyped-def]
        self.deletes.append(options["name"])
        self.extensions.discard("third-party/" + options["name"])
        return True

    def do_keepContext(self, options):  # type: ignore[no-untyped-def]
        self.kept.append(options["contextTokens"])
        return {"context": options["contextTokens"], "helper": True}

    def do_characters(self, options):  # type: ignore[no-untyped-def]
        return [{"avatar": avatar, **data, "lore": ""} for avatar, data in self.characters.items()]

    def do_importCard(self, options):  # type: ignore[no-untyped-def]
        raw = base64.b64decode(options["data"])
        card = read_png_card(raw) if options["type"] == "png" else json.loads(raw.decode("utf-8"))
        data = card.get("data") if isinstance(card.get("data"), dict) else card
        name = str(data.get("name") or data.get("char_name"))
        avatar = f"{options['replace'] or name}.png"
        self.imports.append({"avatar": avatar, "replace": options["replace"]})
        self.add(avatar, data, raw if options["type"] == "png" else b"")
        return {"avatar": avatar, "name": name}

    def _greeting(self, avatar: str, user: str, index: int) -> str:
        data = self.characters[avatar]
        greetings = [str(data.get("first_mes", "")), *[str(item) for item in data.get("alternate_greetings", []) or []]]
        greeting = greetings[index] if index < len(greetings) else greetings[0]
        return greeting.replace("{{user}}", user).replace("{{char}}", str(data.get("name")))

    def do_start(self, options):  # type: ignore[no-untyped-def]
        greeting = self._greeting(options["avatar"], options["user"], options["greeting"])
        chats = self.chats.setdefault(options["avatar"], {})
        name = f"{self.characters[options['avatar']]['name']} - {len(chats) + 1}"  # SillyTavern's own name
        chats[name] = [{"user": False, "text": greeting}] if greeting else []
        return {"chat": name, "greeting": greeting}

    def _chat(self, options):  # type: ignore[no-untyped-def]
        if options["avatar"] not in self.characters:
            raise BridgeError(f"Error: no character {options['avatar']}")
        return self.chats[options["avatar"]][options["chat"]]

    def do_resume(self, options):  # type: ignore[no-untyped-def]
        chat = self._chat(options)
        return {"last": next((message["text"] for message in reversed(chat) if not message["user"]), "")}

    def do_turn(self, options):  # type: ignore[no-untyped-def]
        chat = self._chat(options)
        self.turns.append({**options, "history": [dict(message) for message in chat]})
        reply = self.narrate(options)  # may raise, as a model that fails does
        chat.extend([{"user": True, "text": options["text"]}, {"user": False, "text": reply}])
        return {"reply": reply}

    def do_quiet(self, options):  # type: ignore[no-untyped-def]
        self.quiet.append(options)
        return {"text": self.narrate(options)}

    def do_replaceLast(self, options):  # type: ignore[no-untyped-def]
        self._chat(options)[-1]["text"] = options["text"]
        return True

    def do_chats(self, options):  # type: ignore[no-untyped-def]
        return list(self.chats.get(options["avatar"], {}))

    def do_saveChat(self, options):  # type: ignore[no-untyped-def]
        self.chats.setdefault(options["avatar"], {})[options["chat"]] = [
            {"user": message["user"], "text": message["text"]} for message in options["messages"]
        ]
        return True

    def do_picture(self, options):  # type: ignore[no-untyped-def]
        return base64.b64encode(self.pictures.get(options["avatar"], b"")).decode("ascii")

    def do_stop(self, options):  # type: ignore[no-untyped-def]
        return True


class _ChatModel:
    """The character's own chat model (outside the tavern), recording what it is asked."""

    enabled = True
    supports_tool_calling = True
    supports_images = True

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []

    def _answer(self, messages):  # type: ignore[no-untyped-def]
        self.requests.append([dict(message) for message in messages])
        return LLMToolResponse(text="嗯嗯。", assistant_message={"role": "assistant", "content": "嗯嗯。"})

    def invoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        return self._answer(messages)

    async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
        return self._answer(messages)

    def invoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return "{}"

    async def ainvoke(self, query, *, user=""):  # type: ignore[no-untyped-def]
        return "{}"

    def close(self) -> None:
        pass


class TavernTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False)
        self.config.llm.enabled = True
        self.config.tavern.sillytavern_url = "http://sillytavern:8000/"
        self.config.tavern.browser_url = "http://browser:9222"
        tavern = self.config.tavern
        tavern.llm_base_url, tavern.llm_api_key, tavern.llm_model = "https://example.com/v1", "sk-test", "deepseek-tavern-v2-pro"
        # Short waits keep the tests quick; the order of events is what they check.
        tavern.batch_seconds = 1.0
        tavern.settle_seconds = 0.3
        self.st = _SillyTavern()
        opening = patch.object(_Tab, "open", AsyncMock(side_effect=lambda *args: self.st))
        opening.start()
        self.addCleanup(opening.stop)
        self.chat_model = _ChatModel()
        self.service, self.outbound = self._build()

    def _build(self):  # type: ignore[no-untyped-def]
        service, outbound = build_default_service(self.config)
        self.addCleanup(service.close)
        service.generator._dify_client = self.chat_model  # type: ignore[assignment]
        return service, outbound

    def _texts(self) -> list[str]:
        return [part.text for part in self.outbound.sent]

    def _send(self, *events: InboundEvent) -> None:
        for event in events:
            self.service.handle_event(event)
        self.service.tavern.drain()

    def _start(self, choice: str = "选 1") -> None:
        self._send(_group("启动酒馆", mention=True), _group(choice))

    def _session_file(self) -> Path:
        return self.root / "tavern" / "sessions" / f"group_{GROUP_ID}.json"

    def _session(self) -> dict[str, object]:
        return json.loads(self._session_file().read_text(encoding="utf-8"))

    def _card(self, file_name: str, data: dict[str, object], *, folder: str = "scenarios") -> None:
        """A SillyTavern card in the tavern's scenarios (or adult) folder, imported at the first call."""
        target = self.root / "tavern" / folder
        target.mkdir(parents=True, exist_ok=True)
        card = {"spec": "chara_card_v2", "data": data}
        (target / file_name).write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # The menu and a game
    # ------------------------------------------------------------------
    def test_the_menu_lists_the_cards_and_only_the_host_picks(self) -> None:
        self.st.add("default_Seraphina.png", {"name": "Seraphina", "first_mes": "Hi."})  # SillyTavern's sample
        reply = self.service.handle_event(_group("启动酒馆", mention=True))

        menu = reply.text
        self.assertTrue(menu.startswith("酒馆开张啦！"))
        self.assertIn("1. 雾港酒馆：沉船、海图和一个倒在门口的信使", menu)
        self.assertIn("2. 午夜便利店：", menu)
        self.assertNotIn("Seraphina", menu)
        self.assertIn("房主是「资本狗」，回数字就开本，比如「选 1」", menu)

        self.service.handle_event(_group("选 2", sender=PLAYER))  # not the host: ordinary chat
        self.assertEqual(self._texts(), [menu])

        self.service.handle_event(_group("第二个"))
        rules, opening = self._texts()[1:]
        self.assertTrue(rules.startswith("《午夜便利店》开始！房主：资本狗。想一起玩的说「加入」"))
        self.assertEqual(opening, _bundled("02-midnight-store")["first_mes"])
        self.assertEqual(self.st.turns, [])  # the opening is the card's greeting
        session = self._session()
        self.assertEqual(session["scenario_id"], "午夜便利店.png")
        self.assertEqual(session["chat"], "午夜便利店 - 1")
        self.assertIn(session["chat"], self.st.chats["午夜便利店.png"])

    def test_a_start_said_in_passing_opens_the_menu(self) -> None:
        reply = self.service.handle_event(_group("我让他开酒馆模式", mention=True))
        self.assertTrue(reply.text.startswith("酒馆开张啦！"))
        self.service.handle_event(_group("退出酒馆模式"))

        # Not @-ed, but the person she just answered: still talking to her.
        self.service.handle_event(_group("你好", sender=PLAYER, mention=True))
        reply = self.service.handle_event(_group("那来跑团吧", sender=PLAYER))
        self.assertTrue(reply.text.startswith("酒馆开张啦！"))
        self.assertIn("房主是「苦力怕」", reply.text)

    def test_a_refusal_or_a_question_reaches_the_character_who_knows_the_tavern(self) -> None:
        self.service.handle_event(_group("不开酒馆，聊点别的", mention=True))
        self.service.handle_event(_group("酒馆模式是什么", mention=True))

        self.assertFalse(any(text.startswith("酒馆开张啦") for text in self._texts()))
        system = str(self.chat_model.requests[-1][0]["content"])
        self.assertIn("[酒馆模式]", system)
        self.assertIn("@你说「启动酒馆」", system)

    def test_one_reply_answers_every_players_actions(self) -> None:
        self._start()
        self._send(_group("加入", sender=PLAYER))
        self.assertIn("苦力怕加入了冒险！现在 2 人：资本狗、苦力怕。", self._texts())

        self._send(_group("我把信使扶到桌边"), _group("我去门口看看外面", sender=PLAYER))

        first, second = self.st.turns
        self.assertEqual(first["text"], "（新玩家「苦力怕」加入了队伍，让这位新玩家自然登场，交代一下眼前的情况。）")
        self.assertEqual(second["text"], "【资本狗】我把信使扶到桌边\n【苦力怕】我去门口看看外面")
        self.assertEqual(second["user"], "资本狗、苦力怕")  # SillyTavern's {{user}}
        self.assertEqual(second["avatar"], "雾港酒馆.png")
        note = str(second["note"])
        self.assertTrue(note.startswith("这一局是 QQ 群里几位玩家一起玩：资本狗、苦力怕。"))
        self.assertIn("不写露骨的性内容", note)
        self.assertEqual(second["history"][0]["text"], _bundled("01-wugang")["first_mes"])
        self.assertEqual(self._texts()[-2:], ["第1段剧情。", "第2段剧情。"])

    def test_the_bundled_stories_are_game_master_cards_with_acts_and_endings(self) -> None:
        for path in sorted(BUNDLED_DIR.glob("*.json")):
            data = _bundled(path.stem)
            self.assertGreater(data["extensions"]["openqqwaifu"]["rounds"], 0, path.name)
            description = str(data["description"])
            twists = description.split("【剧情转折】\n")[1].split("\n\n")[0]
            endings = description.split("【可能的结局】\n")[1]
            self.assertEqual(len(twists.splitlines()), 3, path.name)  # three acts
            self.assertGreaterEqual(len(endings.splitlines()), 4, path.name)
            self.assertIn("【主持要点】里的秘密不要直接说出来", str(data["system_prompt"]))
        self._send(_group("启动酒馆", mention=True))
        self.assertEqual(
            [entry["avatar"] for entry in self.st.imports], ["雾港酒馆.png", "午夜便利店.png", "拾荒者号.png", "雨夜沈园.png"]
        )

    def test_a_story_is_paced_and_ends_on_its_finale(self) -> None:
        self._start()
        self._send(_group("我把信使扶到桌边"))
        note = str(self.st.turns[-1]["note"])
        self.assertIn("这个本计划大约 30 轮，现在是第 1 轮", note)
        self.assertNotIn("已经超过计划的轮数", note)

        self.service.tavern._sessions[("group", GROUP_ID)].rounds = 30
        self.st.narrate = lambda options: "灯塔重新亮了起来，雾散了。\n\n（全剧终）"
        self._send(_group("我们把海月石放回灯里"))
        self.assertIn("已经超过计划的轮数", str(self.st.turns[-1]["note"]))
        self.assertEqual(self._texts()[-2:], [
            "灯塔重新亮了起来，雾散了。\n\n（全剧终）",
            "《雾港酒馆》完结，一共玩了 31 轮，谢谢大家！（酒馆模式已关闭）",
        ])
        self.assertFalse(self._session_file().exists())
        self.assertTrue(next((self.root / "tavern" / "archive").glob("group_*.json")).exists())

    def test_table_talk_and_outsiders_stay_out_of_the_story(self) -> None:
        self._start()
        self._send(_group("（我去倒杯水）"), _group("// 等我一下"), _group("这是在干嘛", sender=OUTSIDER))
        sent = len(self._texts())

        hint = self.service.handle_event(_group("你好", sender=OUTSIDER, mention=True))
        self.assertIn("这里正在玩《雾港酒馆》（房主：资本狗）。想一起玩就说「加入」", hint.text)
        self.assertIsNone(self.service.handle_event(_group("在吗", sender=OUTSIDER, mention=True)))  # told once

        self.service.tavern.drain()
        self.assertEqual(len(self._texts()), sent + 1)
        self.assertEqual(self.st.turns, [])
        # Nothing of the game went into the character's own chat history.
        self.assertEqual(self.service.memory.load(GROUP_ID, "group").history, [])

    def test_only_the_host_or_an_admin_ends_a_game_and_it_is_archived(self) -> None:
        self._start()
        self._send(_group("加入", sender=PLAYER))
        self._send(_group("我拔出短刀"))
        self.service.handle_event(_group("退出酒馆模式", sender=PLAYER))
        self.assertEqual(self._texts()[-1], "只有房主「资本狗」能结束这一局；不想玩了可以说「离开」。")
        self.assertTrue(self._session_file().exists())

        self.service.handle_event(_group("退出酒馆模式"))
        self.assertEqual(self._texts()[-1], "《雾港酒馆》到此结束，一共玩了 2 轮，谢谢大家！（酒馆模式已关闭）")
        self.assertFalse(self._session_file().exists())
        archived = json.loads(next((self.root / "tavern" / "archive").glob("group_*.json")).read_text(encoding="utf-8"))
        self.assertEqual(archived["scenario_title"], "雾港酒馆")
        self.assertEqual(archived["last_reply"], "第2段剧情。")
        self.assertEqual(self.st.chats["雾港酒馆.png"][archived["chat"]][-2]["text"], "【资本狗】我拔出短刀")

        # Afterwards the character answers the chat again.
        self.service.handle_event(_group("你好", mention=True))
        self.assertTrue(str(self.chat_model.requests[-1][0]["content"]).startswith("你是"))

        self.config.tool_policy.authorized_user_ids = [OUTSIDER]
        self._start()
        self.service.handle_event(_group("退出酒馆", sender=OUTSIDER))
        self.assertIn("到此结束", self._texts()[-1])

    def test_dice_are_rolled_here_and_passed_on(self) -> None:
        self._start()
        with patch.object(dice_module._RANDOM, "randint", side_effect=[4, 5]):
            reply = self.service.handle_event(_group(".r 2d6+1 撬锁"))
        self.assertEqual(reply.text, "🎲 资本狗为「撬锁」掷 2d6+1：4+5+1 = 10")
        self.service.tavern.drain()
        self.assertEqual(self.st.turns[-1]["text"], "（资本狗为「撬锁」掷了 2d6+1，结果 10）")

        self.service.handle_event(_group(".r 100d6"))
        self.assertEqual(self._texts()[-1], "骰子最多 20 个、每个 2 到 1000 面，加减值不超过 1000。")
        self.assertIsNone(dice_module.parse_roll("掷出飞刀"))
        self.assertIsNone(dice_module.parse_roll(".reset"))

    def test_a_solo_game_in_private_chat_merges_quick_lines(self) -> None:
        self._send(_private("启动酒馆"))
        self.assertNotIn("房主", self._texts()[-1])
        self._send(_private("选3"))
        self.assertTrue(self._texts()[-2].startswith("《拾荒者号》开始！直接说话就是行动"))

        self._send(_private("我飘到气闸门前"), _private("敲三下"))

        (turn,) = self.st.turns
        self.assertEqual(turn["text"], "我飘到气闸门前\n敲三下")  # one player: as typed, without a name
        self.assertEqual(turn["user"], "资本狗")
        self.assertNotIn("几位玩家", str(turn["note"]))

    def test_players_words_are_not_sillytaverns_macros(self) -> None:
        self._start()
        self._send(_group("我念出咒语 {{random:a,b}} 和 {{char}}"))
        self.assertEqual(self.st.turns[-1]["text"], "我念出咒语 {\u200b{random:a,b}\u200b} 和 {\u200b{char}\u200b}")

    def test_a_game_survives_a_restart_and_a_stale_one_closes_quietly(self) -> None:
        self._start()
        self._send(_group("我推开门"))
        self.service.tavern.close()  # the process stops; the full close runs at cleanup

        self.service, self.outbound = self._build()
        self._send(_group("我冲进雾里"))
        turn = self.st.turns[-1]
        self.assertEqual(turn["history"][-2]["text"], "我推开门")
        self.assertEqual(turn["text"], "我冲进雾里")  # one player: as typed
        self.assertEqual(turn["chat"], self._session()["chat"])
        self.service.tavern.close()

        payload = self._session()
        payload["last_activity_at"] = time.time() - 2 * 3600
        self._session_file().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.service, self.outbound = self._build()
        self.service.handle_event(_group("我还在吗"))
        self.assertFalse(self._session_file().exists())
        self.assertEqual(self._texts(), [])  # closed without announcing it hours later

    def test_an_idle_game_closes_itself(self) -> None:
        self.config.tavern.idle_timeout_seconds = 0.5
        self._start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "收摊" not in self._texts()[-1]:
            time.sleep(0.05)
        self.assertEqual(self._texts()[-1], "这一桌 1 分钟没人说话，《雾港酒馆》先收摊了，一共玩了 0 轮。（酒馆模式已关闭）")
        self.assertFalse(self._session_file().exists())

    def test_a_failing_sillytavern_keeps_the_actions_for_the_next_round(self) -> None:
        calls = {"count": 0}

        def narrate(options):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise BridgeError("no reply: Chat Completion API returned an error")
            return "雾里亮起一盏灯。"

        self.st.narrate = narrate
        self._start()
        with self.assertLogs("waifu_standalone.tavern.mode", "WARNING"):
            self._send(_group("我点起火把"))
        self.assertEqual(self._texts()[-1], "（主持人卡了一下。刚才的行动都记着，谁再说一句就接着来。）")

        self._send(_group("我往前走"))
        self.assertEqual(self.st.turns[-1]["text"], "我点起火把\n我往前走")
        self.assertEqual(self._texts()[-1], "雾里亮起一盏灯。")

    def test_without_sillytavern_the_tavern_says_so(self) -> None:
        self.config.tavern.browser_url = ""
        self.assertEqual(
            self.service.handle_event(_group("启动酒馆", mention=True)).text, "酒馆还没接上 SillyTavern，等管理员配好再来吧。"
        )
        self.config.tavern.browser_url = "http://browser:9222"
        self.st.down = True
        with self.assertLogs("waifu_standalone.tavern", "WARNING"):
            reply = self.service.handle_event(_group("启动酒馆", mention=True))
        self.assertEqual(reply.text, "酒馆这会儿连不上 SillyTavern，过一会儿再试试。")
        self.assertFalse(self.service.tavern.running("group", GROUP_ID))

    # ------------------------------------------------------------------
    # Cards
    # ------------------------------------------------------------------
    def test_a_card_is_opened_in_sillytavern_with_its_picture_and_greeting(self) -> None:
        scenarios = self.root / "tavern" / "scenarios"
        scenarios.mkdir(parents=True)
        card = {
            "spec": "chara_card_v2",
            "data": {
                "name": "小咪",
                "description": "{{char}}是猫咖里的猫娘店员，管{{user}}叫主人。",
                "first_mes": "*小咪扑过来*\n欢迎光临，{{user}}主人~",
                "creator_notes": "<p>一只会说话的猫娘</p>",
            },
        }
        (scenarios / "xiaomi.png").write_bytes(_png_card(card))

        menu = self.service.handle_event(_group("开启酒馆模式", mention=True)).text
        self.assertIn("5. 小咪：猫咖里的猫娘店员，管你叫主人", menu)  # a sentence about her, not the notes

        self._send(_group("我想玩小咪"))
        self.assertEqual(self._texts()[-1], "小咪扑过来\n欢迎光临，资本狗主人~")  # her greeting, as SillyTavern shows it
        self.assertTrue(self._texts()[-3].startswith("《小咪》开始！"))
        (picture,) = self.outbound.sent[-2].images  # then her picture, then her greeting
        png = base64.b64decode(picture.removeprefix("base64://"))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82"))
        self.assertNotIn(b"chara", png)  # without the card in it

        self.st.narrate = lambda options: "*小咪眯起眼睛*\n喵呜……"
        self._send(_group("我递给她一把猫薄荷"))
        self.assertEqual(self._texts()[-1], "小咪眯起眼睛\n喵呜……")
        (turn,) = self.st.turns
        self.assertEqual(turn["text"], "我递给她一把猫薄荷")
        self.assertEqual(turn["note"], "不写露骨的性内容，不写血腥细节；剧情走到那里就一笔带过或转场。")  # all we add
        # SillyTavern keeps her reply as she wrote it; only what goes to QQ is plain.
        self.assertEqual(self._session()["last_reply"], "*小咪眯起眼睛*\n喵呜……")

    def test_a_card_whose_picture_cannot_be_sent_still_opens(self) -> None:
        scenarios = self.root / "tavern" / "scenarios"
        scenarios.mkdir(parents=True)
        (scenarios / "cat.png").write_bytes(_png_card({"spec": "chara_card_v2", "data": {"name": "小咪", "first_mes": "喵？"}}))
        dispatch = self.service.emitter.adispatch

        async def no_pictures(message, *, delay_seconds):  # type: ignore[no-untyped-def]
            if message.images:
                raise ConnectionError("timed out uploading the picture")
            await dispatch(message, delay_seconds=delay_seconds)

        with patch.object(self.service.emitter, "adispatch", no_pictures), self.assertLogs("waifu_standalone.tavern.mode", "WARNING"):
            self._send(_group("启动酒馆", mention=True), _group("选小咪"))
        self.assertTrue(self._texts()[-2].startswith("《小咪》开始！"))
        self.assertEqual(self._texts()[-1], "喵？")

    def test_a_card_added_in_sillytavern_itself_is_on_the_menu(self) -> None:
        self.st.add("艾琳.png", {"name": "艾琳", "description": "温柔但毒舌的青梅竹马。", "first_mes": "怎么还没睡？"}, b"")
        self.st.add("夜莺.png", {"name": "夜莺", "tags": ["NSFW"], "first_mes": "晚上好。"})
        self.config.tavern.adult_user_ids = [HOST]

        menu = self.service.handle_event(_group("启动酒馆", mention=True)).text
        self.assertIn("5. 艾琳：温柔但毒舌的青梅竹马", menu)
        self.assertNotIn("夜莺", menu)  # tagged NSFW: only where adult cards are allowed
        self.service.handle_event(_group("退出酒馆模式"))
        self.assertIn("6. 夜莺（成人）", self.service.handle_event(_private("启动酒馆")).text)

    def test_a_card_played_by_several_knows_who_says_what(self) -> None:
        self._card("cat.json", {"name": "小咪", "description": "{{char}}管{{user}}叫主人。", "first_mes": "喵？"})
        self._send(_group("启动酒馆", mention=True), _group("选小咪"), _group("加入", sender=PLAYER))
        self._send(_group("我摸摸她"), _group("我给她倒牛奶", sender=PLAYER))

        turn = self.st.turns[-1]
        self.assertEqual(turn["user"], "资本狗、苦力怕")
        self.assertEqual(turn["text"], "【资本狗】我摸摸她\n【苦力怕】我给她倒牛奶")
        self.assertTrue(str(turn["note"]).startswith("这一局是 QQ 群里几位玩家一起玩：资本狗、苦力怕。"))

    def test_a_fault_in_tavern_mode_does_not_silence_the_character(self) -> None:
        async def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk full")

        with patch.object(self.service.tavern, "ahandle", broken), self.assertLogs(
            "waifu_standalone.reply_flow.service", "ERROR"
        ):
            reply = self.service.handle_event(_group("你好", mention=True))
        self.assertIsNotNone(reply)
        self.assertTrue(str(self.chat_model.requests[-1][0]["content"]).startswith("你是"))

    def test_a_cards_status_bar_and_buttons_reach_qq_as_lines(self) -> None:
        greeting = "<场景>后台|雨夜|灯低</场景>\n顾绮：「这么晚还来？」\n<状态>25|试探|看他怎么答</状态>\n<选项>说目的|看证据|转身走</选项>"
        self._card(
            "guqi.json",
            {
                "name": "顾绮",
                "description": "顾绮是剧院投资人，35岁。",
                "first_mes": greeting,
                "extensions": {"regex_scripts": [{"scriptName": "状态栏 · 心动", "findRegex": "/<状态>/g"}]},
            },
        )
        # A model may get a closing tag wrong, or leave it out.
        self.st.narrate = lambda options: "顾绮：「证据在这。」\n<状态>31|松动|他没走</status>\n<选项>接过来|问来历|还给她"

        self._send(_group("启动酒馆", mention=True), _group("选顾绮"))
        rules, sent = self._texts()[-2:]
        self.assertIn("直接说话就是行动，回数字就是选那个选项", rules)
        self.assertEqual([part.images for part in self.outbound.sent[-2:]], [[], []])  # a JSON card has no picture
        self.assertEqual(
            sent, "【后台｜雨夜｜灯低】\n顾绮：「这么晚还来？」\n\n心动 25 ▰▰▱▱▱▱▱▱▱▱｜心情 · 试探\n心声：看他怎么答\n\n① 说目的\n② 看证据\n③ 转身走"
        )

        self._send(_group("选②"))  # the second button
        turn = self.st.turns[-1]
        self.assertEqual(turn["history"][-1]["text"], greeting)  # SillyTavern keeps what was written, tags and all
        self.assertEqual(turn["text"], "看证据")
        self.assertEqual(
            self._texts()[-1], "顾绮：「证据在这。」\n\n心动 31 ▰▰▰▱▱▱▱▱▱▱｜心情 · 松动\n心声：他没走\n\n① 接过来\n② 问来历\n③ 还给她"
        )
        self._send(_group("我想想"))
        self.assertEqual(self.st.turns[-1]["text"], "我想想")

    def test_an_age_under_18_anywhere_in_a_card_keeps_it_from_adult_content(self) -> None:
        card = {"avatar": "仙.png", "name": "仙", "description": "修仙 RPG。", "first_mes": "点击右箭头开始正文内容→"}
        self.assertFalse(scenario_from_character(card, {}).minors)
        for field, text in (
            ("alternate_greetings", ["{{user}}，穿越，16岁，唯一男性。"]),
            ("mes_example", "<START>\n{{char}}: 她才十五岁。"),
            ("post_history_instructions", "主角是初中生。"),
            ("lore", "女主 17 岁。"),
        ):
            self.assertTrue(scenario_from_character({**card, field: text}, {}).minors, field)

    def test_adult_group_mode_is_opt_in_and_requires_a_listed_host(self) -> None:
        self.assertFalse(TavernConfig().adult_in_groups)
        self.assertTrue(_load_dataclass(TavernConfig, {"adult_in_groups": True}).adult_in_groups)
        self.config.tavern.adult_in_groups = True
        self.assertFalse(self.service.tavern._adult_allowed(_group("启动酒馆")))
        self.config.tavern.adult_user_ids = [" " + HOST + " "]
        self.assertTrue(self.service.tavern._adult_allowed(_group("启动酒馆")))
        self.assertFalse(self.service.tavern._adult_allowed(_group("启动酒馆", sender=PLAYER)))
        self.assertTrue(self.service.tavern._adult_allowed(_private("启动酒馆")))

    def test_adult_group_card_uses_the_adult_note_and_is_not_rewritten(self) -> None:
        self.config.tavern.adult_user_ids = [HOST]
        self.config.tavern.adult_in_groups = True
        self._card("adult-fixture.json", {"name":"成年测试", "description":"所有角色都是成年人。", "first_mes":"山门打开。"}, folder="adult")
        self.st.narrate = lambda options: "FILTER_SENTINEL"
        with patch("waifu_standalone.tavern.mode.EXPLICIT", re.compile("FILTER_SENTINEL")):
            menu = self.service.handle_event(_group("启动酒馆", mention=True)).text
            self.assertIn("成年测试（成人）", menu)
            self._send(_group("选成年测试"), _group("我推开门"))
        self.assertEqual(self._texts()[-1], "FILTER_SENTINEL")
        self.assertFalse(self.st.quiet)
        note = str(self.st.turns[-1]["note"])
        self.assertIn("所有角色都必须是成年人", note)
        self.assertIn("必须双方自愿", note)
        self.assertNotIn("一对一私聊", note)
        self.assertNotIn("一笔带过或转场", note)
        self.assertTrue(any("本局已开启成人模式" in text for text in self._texts()))

    def test_minor_group_card_keeps_strict_rules_and_rewrite(self) -> None:
        self.config.tavern.adult_user_ids = [HOST]
        self.config.tavern.adult_in_groups = True
        self._card("minor-fixture.json", {"name":"未成年测试", "first_mes":"你今年16岁，抵达山门。"}, folder="adult")
        self.st.narrate = lambda options: "FILTER_SENTINEL"
        with patch("waifu_standalone.tavern.mode.EXPLICIT", re.compile("FILTER_SENTINEL")), self.assertLogs(
            "waifu_standalone.tavern.mode", "WARNING"
        ):
            self._send(_group("启动酒馆", mention=True), _group("选未成年测试"), _group("我看看四周"))
        self.assertEqual(self._texts()[-1], "（这一段跳过。）")
        self.assertTrue(self.st.quiet)
        self.assertIn("故事里有未成年人", str(self.st.turns[-1]["note"]))
        self.assertFalse(any("本局已开启成人模式" in text for text in self._texts()))

    def test_group_adult_authorization_survives_restart_but_can_be_revoked(self) -> None:
        self.config.tavern.adult_user_ids = [HOST]
        self.config.tavern.adult_in_groups = True
        self._card("adult-fixture.json", {"name":"成年测试", "first_mes":"山门打开。"}, folder="adult")
        self._send(_group("启动酒馆", mention=True), _group("选成年测试"))
        self.service.tavern.close()
        self.service, self.outbound = self._build()
        self._send(_group("我看看四周"))
        session = self.service.tavern._sessions[("group", GROUP_ID)]
        scenario = self.service.tavern._playing[session.key]
        self.assertTrue(self.service.tavern._adult_content(session, scenario))
        self.config.tavern.adult_user_ids = []
        self.assertFalse(self.service.tavern._adult_content(session, scenario))
        self.config.tavern.adult_user_ids = [HOST]
        self.config.tavern.adult_in_groups = False
        self.assertFalse(self.service.tavern._adult_content(session, scenario))

    def test_adult_cards_and_content_only_in_an_allowed_private_chat(self) -> None:
        self._card("night.json", {"name": "夜色", "description": "成年人的夜晚。", "first_mes": "夜深了。"}, folder="adult")
        self.config.tavern.adult_user_ids = [HOST]

        # A group never sees the adult folder, even when someone on the list opens the tavern.
        self.assertNotIn("夜色", self.service.handle_event(_group("启动酒馆", mention=True)).text)
        self._send(_group("选 1"), _group("我推开门"))
        self.assertIn("一笔带过或转场", str(self.st.turns[-1]["note"]))
        self.service.handle_event(_group("退出酒馆模式"))

        # Someone not on the list does not see it in private either.
        stranger = InboundEvent(PLAYER, "person", PLAYER, NAMES[PLAYER], [MessageSegment(kind="text", text="启动酒馆")])
        self.assertNotIn("夜色", self.service.handle_event(stranger).text)

        # The person on the list, in private: on the menu, marked, and the game drops the fade-out rule.
        self.assertIn("5. 夜色（成人）", self.service.handle_event(_private("启动酒馆")).text)
        self._send(_private("选5"), _private("我点了一支蜡烛"))
        note = str(self.st.turns[-1]["note"])
        self.assertIn("亲密情节可以按角色卡的设定写", note)
        self.assertIn("所有角色都必须是成年人", note)
        self.assertNotIn("一笔带过或转场", note)

    def test_a_cards_html_and_markdown_reach_qq_as_what_they_show(self) -> None:
        greeting = (
            "<div style=text-align:center><img src=https://example.com/a.png /><h1>{{char}}</h1></div>\n"
            "*仙门大开，山风扑面。*\n---\n<details><summary><center>**面板（点击展开）**</center></summary>\n"
            "<small>境界：炼气</small>\n</details>\n\n> 📜提示：\n<small>\"别回头\"</small>\n</plot>"
        )
        self._card("xian.json", {"name": "仙境", "first_mes": greeting})
        self._send(_group("启动酒馆", mention=True), _group("选仙境"))

        self.assertEqual(self._texts()[-1], '仙境\n\n仙门大开，山风扑面。\n──────────\n\n面板\n\n境界：炼气\n\n📜提示：\n"别回头"')
        self.assertTrue(str(self._session()["last_reply"]).startswith("<div style=text-align:center>"))

    def test_a_round_too_explicit_for_the_game_is_rewritten_or_skipped(self) -> None:
        replies = iter(["他们开始做爱。", "灯灭了，第二天清晨……", "做爱。", "还是做爱。"])
        self.st.narrate = lambda options: next(replies)
        self._start()

        with self.assertLogs("waifu_standalone.tavern.mode", "WARNING"):
            self._send(_group("我关上灯"))
        self.assertEqual(self._texts()[-1], "灯灭了，第二天清晨……")
        self.assertIn("刚才那段写得太露骨了", str(self.st.quiet[-1]["instruction"]))
        chat = self.st.chats["雾港酒馆.png"][self._session()["chat"]]
        self.assertEqual(chat[-1]["text"], "灯灭了，第二天清晨……")  # what SillyTavern keeps, too
        with self.assertLogs("waifu_standalone.tavern.mode", "WARNING"):
            self._send(_group("再来"))
        self.assertEqual(self._texts()[-1], "（这一段跳过。）")
        self.assertEqual(chat[-1]["text"], "（这一段跳过。）")

        # An adult game in an allowed private chat is not held back.
        self.service.handle_event(_group("退出酒馆模式"))
        self.config.tavern.adult_user_ids = [HOST]
        self.st.narrate = lambda options: "他们开始做爱。"
        self._send(_private("启动酒馆"), _private("选 1"), _private("我关上灯"))
        self.assertEqual(self._texts()[-1], "他们开始做爱。")

    def test_a_card_that_is_a_game_opens_with_the_greeting_that_tells_its_story(self) -> None:
        self._card(
            "xianxia.json",
            {
                "name": "逆天修仙",
                "description": "这是一个修仙RPG游戏，仙女界不是一个特定的角色，而是一个RPG系统。",
                "first_mes": "<div>本角色卡分享于以下：Discord 社区 点击跳转。请勿倒卖。点击右箭头开始正文内容→</div>",
                "alternate_greetings": ["*天空裂开一道缝，一个少年从云里坠了下来，砸进仙女界的山门前。*\n守门的仙子愣住了：「男人？」"],
            },
        )
        self.st.narrate = lambda options: "<div>*山门前一片寂静。*</div>\n守门仙子：「报上名来。」"

        menu = self.service.handle_event(_group("启动酒馆", mention=True)).text
        self.assertIn("5. 逆天修仙：", menu)
        self._send(_group("选逆天修仙"))
        # The greeting that tells the story, not the credits page.
        self.assertEqual(self._texts()[-1], "天空裂开一道缝，一个少年从云里坠了下来，砸进仙女界的山门前。\n守门的仙子愣住了：「男人？」")

        self._send(_group("我站起来拍拍土"))
        self.assertEqual(self._texts()[-1], "山门前一片寂静。\n\n守门仙子：「报上名来。」")  # no HTML, no asterisks

    def test_a_card_with_someone_under_18_is_never_an_adult_game(self) -> None:
        self.config.tavern.adult_user_ids = [HOST]
        self._card(
            "xianxia.json",
            {"name": "仙女界", "description": "修仙 RPG。", "first_mes": "{{user}}，穿越，16岁，唯一男性。你从湖里爬上了岸。"},
            folder="adult",
        )

        star = {"avatar": "织微.png", "name": "织微", "description": "她 8 岁那年见过流星，如今 300 岁。人类 28~46 岁。"}
        self.assertFalse(scenario_from_character(star, {}).minors)  # a past age, and adults

        self.st.narrate = lambda options: "他们开始做爱。"
        self._send(_private("启动酒馆"), _private("选仙女界"))
        self.assertIn("（卡里有人未满 18 岁，这一局不写成人内容。）", self._texts()[-2])
        with self.assertLogs("waifu_standalone.tavern.mode", "WARNING"):
            self._send(_private("我往村里走"))
        self.assertTrue(str(self.st.turns[0]["note"]).startswith("故事里有未成年人：对任何人都不写性感、暧昧"))
        self.assertEqual(self._texts()[-1], "（这一段跳过。）")

    # ------------------------------------------------------------------
    # SillyTavern's setup
    # ------------------------------------------------------------------
    def test_saved_web_changes_reload_before_the_next_qq_turn(self) -> None:
        self._start()
        reloads = self.st.loads
        self.st.web_revision += 1
        self.service.tavern.bridge._web_sync_due = 0
        self._send(_group("我推开门"))
        self.assertEqual(self.st.loads, reloads + 1)
        self.service.tavern.bridge._web_sync_due = 0
        self._send(_group("我看看四周"))
        self.assertEqual(self.st.loads, reloads + 1)
        self.assertEqual(len(self.st.connects), 1)  # Reload does not overwrite a web-selected model/preset.

    def test_a_web_save_during_reload_is_not_acknowledged_before_it_is_loaded(self) -> None:
        self._start()
        reloads = self.st.loads
        original_send = self.st.send
        async def concurrent_save(method, timeout, **params):
            if method == "Page.reload" and self.st.web_revision == 2:
                self.st.web_revision = 3
            return await original_send(method, timeout, **params)
        self.st.send = concurrent_save
        self.st.web_revision = 2
        self.service.tavern.bridge._web_sync_due = 0
        self._send(_group("我推开门"))
        self.assertEqual(self.st.loads, reloads + 1)
        self.service.tavern.bridge._web_sync_due = 0
        self._send(_group("我看看四周"))
        self.assertEqual(self.st.loads, reloads + 2)

    def test_web_sync_can_be_disabled_or_throttled(self) -> None:
        self._start()
        reloads = self.st.loads
        self.st.web_revision += 1
        self.config.tavern.sync_web_settings = False
        self.service.tavern.bridge._web_sync_due = 0
        self._send(_group("我推开门"))
        self.assertEqual(self.st.loads, reloads)
        self.config.tavern.sync_web_settings = True
        self.service.tavern.bridge._web_sync_due = float("inf")
        self._send(_group("我看看四周"))
        self.assertEqual(self.st.loads, reloads)
        self.service.tavern.bridge._web_sync_due = 0
        self._send(_group("我点亮灯"))
        self.assertEqual(self.st.loads, reloads + 1)

    def test_failed_web_sync_does_not_generate_with_stale_settings(self) -> None:
        self._start()
        self.service.tavern.bridge._web_sync_due = 0
        def unavailable(options):
            raise BridgeError("saved settings unavailable")
        self.st.do_webState = unavailable
        with self.assertLogs("waifu_standalone.tavern.mode", "WARNING"):
            self._send(_group("我推开门"))
        self.assertFalse(self.st.turns)
        self.assertIn("主持人卡了一下", self._texts()[-1])

    def test_sillytavern_plays_on_the_tavern_model_set_once(self) -> None:
        self._send(_group("启动酒馆", mention=True))
        (connect,) = self.st.connects
        self.assertEqual(
            {key: connect[key] for key in ("url", "model", "key", "firstTime", "contextTokens", "replyTokens")},
            {
                "url": "https://example.com/v1", "model": "deepseek-tavern-v2-pro", "key": "sk-test",
                "firstTime": True, "contextTokens": 32000, "replyTokens": 2000,
            },
        )
        state = (self.root / "tavern" / "sillytavern.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-test", state)  # only a fingerprint of it
        self.assertEqual(self.st.reasoning_parsed, 1)  # <think> blocks go into SillyTavern's reasoning

        # The next start leaves SillyTavern's settings as they are, until the tavern model changes.
        self.service.tavern.close()
        self.service, self.outbound = self._build()
        self._send(_private("启动酒馆"), _private("退出酒馆模式"))
        self.assertEqual(len(self.st.connects), 1)
        self.service.tavern.close()
        self.config.tavern.llm_api_key = "sk-new"
        self.service, self.outbound = self._build()
        self._send(_private("启动酒馆"))
        self.assertEqual([(item["key"], item["firstTime"]) for item in self.st.connects[1:]], [("sk-new", False)])
        self.assertEqual(self.st.reasoning_parsed, 1)  # once

    def test_without_a_tavern_model_sillytavern_uses_the_chat_model_or_its_own(self) -> None:
        tavern = self.config.tavern
        tavern.llm_base_url = tavern.llm_api_key = tavern.llm_model = ""
        llm = self.config.llm
        llm.backend, llm.base_url, llm.api_key, llm.model, llm.reasoning_effort = "xai", "https://api.x.ai/v1", "xai-key", "grok-4.7", "low"
        self._send(_group("启动酒馆", mention=True))
        self.assertEqual(
            [(item["url"], item["model"], item["reasoning"]) for item in self.st.connects], [("https://api.x.ai/v1", "grok-4.7", "low")]
        )

        self.st.connects.clear()
        llm.backend = "dify"  # not something SillyTavern can call
        self.service.tavern.close()
        self.service, self.outbound = self._build()
        (self.root / "tavern" / "sillytavern.json").unlink()
        self._send(_private("启动酒馆"))
        self.assertEqual(self.st.connects, [])

    def test_sillytavern_gets_its_extensions_at_their_tags(self) -> None:
        self._send(_group("启动酒馆", mention=True))
        self.assertEqual(self.st.installs, [{"url": "https://github.com/N0VI028/JS-Slash-Runner", "branch": "4.11.3"}])
        # 酒馆助手's 2M-token context is turned off, and kept off once its page is loaded again.
        self.assertEqual(self.st.kept, [32000, 32000])
        self.assertEqual(self.st.loads, 3)  # the first load, after the install, after the context
        self.service.tavern.close()

        # The next start leaves it as it is; another tag replaces it.
        self.service, self.outbound = self._build()
        self._send(_private("启动酒馆"), _private("退出酒馆模式"))
        self.assertEqual((len(self.st.installs), len(self.st.kept)), (1, 2))
        self.service.tavern.close()
        self.config.tavern.sillytavern_extensions = ["https://github.com/N0VI028/JS-Slash-Runner@4.12.0"]
        self.service, self.outbound = self._build()
        self._send(_private("启动酒馆"))
        self.assertEqual(self.st.deletes, ["JS-Slash-Runner"])
        self.assertEqual(self.st.installs[-1], {"url": "https://github.com/N0VI028/JS-Slash-Runner", "branch": "4.12.0"})
        self.assertEqual(len(self.st.kept), 2)

    def test_an_extension_installed_by_hand_is_left_as_it_is(self) -> None:
        self.st.extensions.add("third-party/JS-Slash-Runner")
        self._send(_group("启动酒馆", mention=True))
        self.assertEqual((self.st.installs, self.st.deletes), ([], []))
        self.assertEqual(self.st.kept, [32000, 32000])  # its context default is still turned off

        self.service.tavern.close()
        (self.root / "tavern" / "sillytavern.json").unlink()
        self.st.extensions.clear()
        self.config.tavern.sillytavern_extensions = []
        self.service, self.outbound = self._build()
        self._send(_private("启动酒馆"))
        self.assertEqual((self.st.installs, len(self.st.kept)), ([], 2))

    def test_a_page_drawn_from_a_reply_reaches_qq_as_its_words(self) -> None:
        self._card("rain.json", {"name": "雨夜", "first_mes": "雨下着。"})
        self.st.narrate = lambda options: (
            "雨还在下。\n```html\n<!DOCTYPE html><html><head><title>状态</title><style>.a{color:red}</style></head>"
            "<body><div class=a>好感 30</div><script>console.log(1)</script></body></html>\n```\n<!-- 注释 -->她转身走了。"
        )
        self._send(_group("启动酒馆", mention=True), _group("选雨夜"), _group("我撑开伞"))
        self.assertEqual(self._texts()[-1], "雨还在下。\n\n好感 30\n\n她转身走了。")

    def test_a_bundled_card_that_changed_is_imported_again_in_place(self) -> None:
        self._send(_group("启动酒馆", mention=True))
        self.assertEqual(len(self.st.imports), 4)
        self.service.tavern.close()

        state_path = self.root / "tavern" / "sillytavern.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["cards"]["bundled/01-wugang.json"]["sha"] = "an older version"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.service, self.outbound = self._build()
        self._send(_private("启动酒馆"))
        self.assertEqual(self.st.imports[4:], [{"avatar": "雾港酒馆.png", "replace": "雾港酒馆"}])

    def test_games_saved_before_sillytavern_become_its_chats(self) -> None:
        self._card("cat.json", {"name": "小咪", "first_mes": "喵？"})
        archive = self.root / "tavern" / "archive"
        archive.mkdir(parents=True)
        old = {
            "version": 1, "launcher_type": "group", "launcher_id": GROUP_ID, "host_id": HOST, "host_name": "资本狗",
            "state": "playing", "scenario_id": "01-wugang", "scenario_title": "雾港酒馆", "players": {HOST: "资本狗"},
            "turns": [
                {"role": "assistant", "content": "夜里十一点。"},
                {"role": "user", "content": "【资本狗】我扶起信使"},
                {"role": "assistant", "content": "信使醒了。"},
            ],
            "summary": "众人在酒馆落脚。", "rounds": 1, "started_at": time.time() - 3600, "last_activity_at": time.time() - 600,
        }
        (archive / f"group_{GROUP_ID}-20261028-080241.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        card_game = {**old, "scenario_id": "cat", "scenario_title": "小咪", "turns": [{"role": "assistant", "content": "喵？"}]}
        card_game["started_at"] -= 60
        card_game["last_activity_at"] -= 60
        (archive / f"group_{GROUP_ID}-20261028-070000.json").write_text(json.dumps(card_game, ensure_ascii=False), encoding="utf-8")

        listing = self.service.handle_event(_group("继续酒馆", mention=True)).text
        self.assertIn("1. 《雾港酒馆》玩到第 1 轮", listing)
        self.assertIn("2. 《小咪》玩到第 1 轮", listing)
        moved = json.loads((archive / f"group_{GROUP_ID}-20261028-080241.json").read_text(encoding="utf-8"))
        self.assertNotIn("turns", moved)
        self.assertEqual((moved["scenario_id"], moved["last_reply"]), ("雾港酒馆.png", "信使醒了。"))
        self.assertEqual(
            [(message["user"], message["text"]) for message in self.st.chats["雾港酒馆.png"][moved["chat"]]],
            [(False, "夜里十一点。"), (True, "（前情提要）众人在酒馆落脚。"), (True, "【资本狗】我扶起信使"), (False, "信使醒了。")],
        )

        self._send(_group("选 1"))
        self.assertEqual(self._texts()[-1], "上回说到——\n信使醒了。")
        self._send(_group("我们去码头"))
        self.assertEqual((self.st.turns[-1]["chat"], self.st.turns[-1]["text"]), (moved["chat"], "我们去码头"))

    # ------------------------------------------------------------------
    # Config and talk around a game
    # ------------------------------------------------------------------
    def test_the_config_file_sets_the_tavern(self) -> None:
        # ConfigManager.load() would also look for napcat/config under the working directory.
        tavern = _load_dataclass(
            TavernConfig,
            {"enabled": False, "batch_seconds": 5, "admin_user_ids": ["1", " 2 "], "sillytavern_url": "http://st:8000/", "history_chars": 1},
        )
        self.assertFalse(tavern.enabled)
        self.assertEqual(tavern.batch_seconds, 5.0)
        self.assertEqual(tavern.admin_user_ids, ["1", "2"])
        self.assertEqual(tavern.idle_timeout_seconds, 1800.0)
        self.assertEqual((tavern.sillytavern_url, tavern.browser_url), ("http://st:8000/", ""))

        self.config.tavern.enabled = False
        self.service.handle_event(_group("启动酒馆", mention=True))
        self.assertFalse(any(text.startswith("酒馆开张啦") for text in self._texts()))

    def test_asking_for_the_tavern_during_a_game_is_not_an_action(self) -> None:
        self._send(_private("开始酒馆"))
        self.assertTrue(self._texts()[-1].startswith("酒馆开张啦"))
        self._send(_private("选3"))
        for text in ("继续酒馆", "开始酒馆", "启动酒馆", "酒馆状态"):
            reply = self.service.handle_event(_private(text))
            self.assertEqual(reply.text, "正在玩《拾荒者号》，已经玩了 0 轮，直接说话就是行动；说「退出酒馆模式」结束。")
        self.service.tavern.drain()
        self.assertEqual(self.st.turns, [])  # nothing was queued as an action

    def test_half_an_emoji_does_not_stop_a_game_from_being_saved(self) -> None:
        self.st.narrate = lambda options: "雾里传来笑声\ud83d，你听见了吗？"
        self._start()
        self._send(_group("我循着笑声走过去"))
        self._send(_group("我停下脚步"))
        saved = self._session()
        self.assertEqual(saved["last_reply"], "雾里传来笑声�，你听见了吗？")
        self.assertEqual(saved["rounds"], 2)

    # ------------------------------------------------------------------
    # Saves
    # ------------------------------------------------------------------
    def test_an_ended_game_is_picked_up_where_it_stopped(self) -> None:
        self._start()
        self._send(_group("我拔出短刀"))
        self.service.handle_event(_group("退出酒馆模式"))
        self.assertFalse(self._session_file().exists())

        listing = self.service.handle_event(_group("继续酒馆", mention=True)).text
        self.assertTrue(listing.startswith("能接着玩的存档：\n1. 《雾港酒馆》玩到第 1 轮，"), listing)
        self.assertEqual(self.service.handle_event(_group("酒馆状态")).text, "酒馆在等「资本狗」选存档。")

        # Like the menu, only the one who asked picks; anyone else is talking to the character.
        self.service.handle_event(_group("选 1", sender=PLAYER))
        self.assertTrue(self.service.tavern._sessions[("group", GROUP_ID)].menu)
        self.service.handle_event(_group("选 3"))
        self.assertEqual(self._texts()[-1], "没有第 3 个存档，回 1 到 1 的数字。")

        self._send(_group("选 1"))
        self.assertTrue(self._texts()[-2].startswith("读档成功：《雾港酒馆》，已经玩了 1 轮。房主：资本狗。"))
        self.assertEqual(self._texts()[-1], "上回说到——\n第1段剧情。")
        self.assertTrue(self._session_file().exists())

        self._send(_group("我冲进雾里"))
        turn = self.st.turns[-1]
        self.assertEqual(turn["history"][-2]["text"], "我拔出短刀")  # the same SillyTavern chat
        self.assertEqual(turn["text"], "我冲进雾里")

        # Ended again, it is still one save, now two rounds in.
        self.service.handle_event(_group("退出酒馆模式"))
        self.assertEqual(len(list((self.root / "tavern" / "archive").glob("group_*.json"))), 1)
        self.assertIn("玩到第 2 轮", self.service.handle_event(_group("读档", mention=True)).text)

    def test_only_unfinished_games_of_this_chat_are_offered(self) -> None:
        self.assertEqual(
            self.service.handle_event(_group("继续酒馆", mention=True)).text,
            "这里没有能接着玩的存档。说「启动酒馆」开一局新的吧。",
        )
        self._start()
        self._send(_group("我拔出短刀"))
        self.service.handle_event(_group("退出酒馆模式"))

        # Not in another chat.
        self.assertIn("没有能接着玩的存档", self.service.handle_event(_private("继续酒馆")).text)

        # Not once the story reached its finale.
        archive = next((self.root / "tavern" / "archive").glob("group_*.json"))
        payload = json.loads(archive.read_text(encoding="utf-8"))
        payload["last_reply"] += "\n（全剧终）"
        archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertIn("没有能接着玩的存档", self.service.handle_event(_group("继续酒馆", mention=True)).text)

        # Not an adult game, outside the private chat that may play it.
        payload["last_reply"] = "第1段剧情。"
        payload["adult"] = True
        archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertIn("没有能接着玩的存档", self.service.handle_event(_group("继续酒馆", mention=True)).text)

        # Not when its chat is gone from SillyTavern.
        payload["adult"] = False
        archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.st.chats["雾港酒馆.png"].clear()
        self.assertIn("没有能接着玩的存档", self.service.handle_event(_group("继续酒馆", mention=True)).text)

    # ------------------------------------------------------------------
    # Cards sent as files
    # ------------------------------------------------------------------
    def _send_file(self, name: str, data: bytes, *, sender: str = HOST, url: str = "http://qq.example/file") -> str:
        event = InboundEvent(
            sender, "person", sender, NAMES[sender],
            [MessageSegment(kind="file", file_name=name, file_id="fid", file_url=url, file_size=len(data))],
        )
        page = FetchedPage(url=url, status_code=200, text="", headers={}, content=data)
        with patch("waifu_standalone.tavern.cards.afetch_public_url", AsyncMock(return_value=page)) as fetch:
            reply = self.service.handle_event(event)
        self.fetched = fetch
        return reply.text if reply is not None else ""

    def test_an_admin_sends_a_card_as_a_file_and_it_goes_into_sillytavern(self) -> None:
        card = {"spec": "chara_card_v2", "data": {"name": "艾琳", "description": "温柔但毒舌的青梅竹马。", "first_mes": "怎么还没睡？"}}
        self.assertEqual(self._send_file("艾琳.png", _png_card(card)), "只有管理员能往酒馆里放角色卡。")

        self.config.tool_policy.authorized_user_ids = [HOST]
        self.assertEqual(
            self._send_file("main_card_v2.png", _png_card(card)),
            "收到角色卡《艾琳》，放进了酒馆。说「启动酒馆」就能在菜单里选到它。",
        )
        self.assertTrue((self.root / "tavern" / "scenarios" / "艾琳.png").is_file())
        self.assertEqual(self.st.imports[-1], {"avatar": "艾琳.png", "replace": ""})
        self.assertIn("5. 艾琳：温柔但毒舌的青梅竹马", self.service.handle_event(_private("启动酒馆")).text)
        self.service.handle_event(_private("退出酒馆模式"))

        # Sent again, it replaces the copy, in SillyTavern too; as JSON it is read the same way.
        self.assertIn("换下了酒馆里的旧版", self._send_file("艾琳.json", json.dumps(card, ensure_ascii=False).encode("utf-8")))
        self.assertEqual(sorted(path.name for path in (self.root / "tavern" / "scenarios").iterdir()), ["艾琳.json"])
        self.assertEqual(self.st.imports[-1], {"avatar": "艾琳.png", "replace": "艾琳"})

        # Tagged NSFW, it goes where only an allowed private chat sees it.
        night = {"spec": "chara_card_v2", "data": {"name": "夜莺", "tags": ["NSFW"], "first_mes": "晚上好。"}}
        self.assertIn("成人卡", self._send_file("夜莺.png", _png_card(night)))
        self.assertTrue((self.root / "tavern" / "adult" / "夜莺.png").is_file())
        self.assertNotIn("夜莺", self.service.handle_event(_private("启动酒馆")).text)  # not on this account's list

    def test_a_card_kept_while_sillytavern_is_down_is_imported_later(self) -> None:
        self.config.tool_policy.authorized_user_ids = [HOST]
        self.st.down = True
        card = {"spec": "chara_card_v2", "data": {"name": "艾琳", "first_mes": "怎么还没睡？"}}
        with self.assertLogs("waifu_standalone.tavern", "WARNING"):
            reply = self._send_file("艾琳.png", _png_card(card))
        self.assertEqual(reply, "收到角色卡《艾琳》，先存下了；SillyTavern 这会儿连不上，下次连上时会自动导入。")

        self.st.down = False
        self.service.tavern.close()
        self.service, self.outbound = self._build()
        self.assertIn("艾琳", self.service.handle_event(_private("启动酒馆")).text)

    def test_a_file_that_is_not_a_card_is_turned_down_and_others_are_not_ours(self) -> None:
        self.config.tool_policy.authorized_user_ids = [HOST]
        plain = _png_card({}).replace(b"tEXtchara", b"tEXtother")
        self.assertIn("PNG 角色卡要用「文件」发", self._send_file("立绘.png", plain))
        self.assertIn("不是酒馆能读的角色卡", self._send_file("notes.json", b'{"hello": 1}'))
        self.assertFalse(any((self.root / "tavern" / "scenarios").glob("*")) if (self.root / "tavern" / "scenarios").exists() else False)

        # Any other file is left alone: no answer from the tavern or the character.
        before = len(self.outbound.sent)
        self.assertEqual(self._send_file("报告.pdf", b"%PDF"), "")
        self.assertEqual(len(self.outbound.sent), before)
        self.fetched.assert_not_called()

    def test_a_file_without_a_link_is_fetched_through_napcat(self) -> None:
        self.config.tool_policy.authorized_user_ids = [HOST]
        card = {"spec": "chara_card_v2", "data": {"name": "艾琳", "first_mes": "怎么还没睡？"}}
        with patch.object(
            type(self.outbound), "acall_action",
            AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"url": "http://qq.example/real"}}),
        ) as call:
            self.assertIn("收到角色卡《艾琳》", self._send_file("艾琳.png", _png_card(card), url=""))
        call.assert_awaited_once_with("get_private_file_url", {"file_id": "fid"})
        self.assertEqual(self.fetched.await_args.args[0], "http://qq.example/real")

    def test_napcat_file_segments_are_read(self) -> None:
        from waifu_standalone.web.http_api import _parse_segments

        (segment,) = _parse_segments({"message": [
            {"type": "file", "data": {"file": "艾琳.png", "file_id": "abc", "file_size": "2048", "url": "http://x/y"}}
        ]})
        self.assertEqual(
            (segment.kind, segment.file_name, segment.file_id, segment.file_size, segment.file_url),
            ("file", "艾琳.png", "abc", 2048, "http://x/y"),
        )


if __name__ == "__main__":
    unittest.main()
