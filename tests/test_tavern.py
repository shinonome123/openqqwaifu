from __future__ import annotations

import base64
import json
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
from waifu_standalone.infra.llm_clients import LLMClientError, LLMToolResponse
from waifu_standalone.infra.safe_fetch import FetchedPage
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.settings_admin.config_manager import _load_dataclass
from waifu_standalone.tavern import dice as dice_module
from waifu_standalone.tavern.scenarios import ScenarioLibrary, read_png_card

BOT_ID = "3956638110"
GROUP_ID = "612475113"
HOST = "783190298"
PLAYER = "1147026356"
OUTSIDER = "2747277822"
NAMES = {HOST: "资本狗", PLAYER: "苦力怕", OUTSIDER: "路人"}
BUNDLED = ScenarioLibrary(Path("/nonexistent"), Path(__file__).resolve().parents[1] / "src/waifu_standalone/templates/tavern")


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


class _GameMaster:
    """A chat model that records every request and answers from ``narrate``."""

    enabled = True
    supports_tool_calling = True
    supports_images = True

    def __init__(self, narrate=None) -> None:  # type: ignore[no-untyped-def]
        self.requests: list[list[dict[str, object]]] = []
        self.narrate = narrate or (lambda messages: f"第{len(self.requests)}段剧情。")

    def _answer(self, messages):  # type: ignore[no-untyped-def]
        self.requests.append([dict(message) for message in messages])
        text = self.narrate(messages)
        return LLMToolResponse(text=text, assistant_message={"role": "assistant", "content": text})

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

    def game_requests(self) -> list[list[dict[str, object]]]:
        """The game master's rounds (a story's, or a card's), without recaps and the character's own replies."""
        return [
            request
            for request in self.requests
            if str(request[0]["content"]).startswith("你在 QQ ")
            or any(message["content"] == "[Start a new Chat]" for message in request)
        ]


class TavernTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = AppConfig(data_root=temp.name, bot_account_id=BOT_ID, knowledge_auto_extract=False)
        self.config.llm.enabled = True
        # Short waits keep the tests quick; the order of events is what they check.
        self.config.tavern.batch_seconds = 1.0
        self.config.tavern.settle_seconds = 0.3
        self.gm = _GameMaster()
        self.service, self.outbound = self._build()

    def _build(self):  # type: ignore[no-untyped-def]
        service, outbound = build_default_service(self.config)
        self.addCleanup(service.close)
        service.generator._dify_client = self.gm  # type: ignore[assignment]
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

    def _card(self, file_name: str, data: dict[str, object], *, folder: str = "scenarios") -> None:
        """A SillyTavern card in the tavern's scenarios (or adult) folder."""
        target = self.root / "tavern" / folder
        target.mkdir(parents=True, exist_ok=True)
        card = {"spec": "chara_card_v2", "data": data}
        (target / file_name).write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    def test_the_menu_lists_the_scenarios_and_only_the_host_picks(self) -> None:
        reply = self.service.handle_event(_group("启动酒馆", mention=True))

        menu = reply.text
        self.assertTrue(menu.startswith("酒馆开张啦！"))
        self.assertIn("1. 雾港酒馆：沉船、海图和一个倒在门口的信使", menu)
        self.assertIn("2. 午夜便利店：", menu)
        self.assertIn("房主是「资本狗」，回数字就开本，比如「选 1」", menu)

        self.service.handle_event(_group("选 2", sender=PLAYER))  # not the host: ordinary chat
        self.assertEqual(self._texts(), [menu])

        self.service.handle_event(_group("第二个"))
        rules, opening = self._texts()[1:]
        self.assertTrue(rules.startswith("《午夜便利店》开始！房主：资本狗。想一起玩的说「加入」"))
        self.assertEqual(opening, BUNDLED.get("02-midnight-store").opening)
        self.assertEqual(self.gm.requests, [])  # the opening is written in the scenario

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
        system = str(self.gm.requests[-1][0]["content"])
        self.assertIn("[酒馆模式]", system)
        self.assertIn("@你说「启动酒馆」", system)

    def test_one_narration_answers_every_players_actions(self) -> None:
        self._start()
        self._send(_group("加入", sender=PLAYER))
        self.assertIn("苦力怕加入了冒险！现在 2 人：资本狗、苦力怕。", self._texts())

        self._send(_group("我把信使扶到桌边"), _group("我去门口看看外面", sender=PLAYER))

        first, second = self.gm.game_requests()
        self.assertEqual(first[-1]["content"], "（新玩家「苦力怕」加入了队伍，让这位新玩家自然登场，交代一下眼前的情况。）")
        system = str(second[0]["content"])
        self.assertTrue(system.startswith("你在 QQ 群里当跑团主持人，正在主持《雾港酒馆》"))
        self.assertIn("[主持要点]\n倒在门口的信使叫科尔", system)
        self.assertIn("[玩家]\n资本狗（房主）、苦力怕", system)
        self.assertIn("点到每个人的名字", system)
        self.assertEqual(
            [message["role"] for message in second[1:]], ["user", "assistant", "user", "assistant", "user"]
        )
        self.assertEqual(second[2]["content"], BUNDLED.get("01-wugang").opening)
        self.assertEqual(second[-1]["content"], "【资本狗】我把信使扶到桌边\n【苦力怕】我去门口看看外面")
        self.assertEqual(self._texts()[-2:], ["第1段剧情。", "第2段剧情。"])

    def test_the_bundled_stories_have_acts_and_endings(self) -> None:
        for scenario in BUNDLED.scenarios():
            self.assertGreater(scenario.rounds, 0, scenario.title)
            self.assertEqual(len(scenario.twists.splitlines()), 3, scenario.title)  # three acts
            self.assertGreaterEqual(len(scenario.endings.splitlines()), 4, scenario.title)
            self.assertFalse(scenario.minors, scenario.title)

    def test_a_story_is_paced_and_ends_on_its_finale(self) -> None:
        self._start()
        self._send(_group("我把信使扶到桌边"))
        system = str(self.gm.game_requests()[-1][0]["content"])
        self.assertIn("[剧情转折]\n第一幕（前三分之一）：黑潮打手追进酒馆", system)
        self.assertIn("[可能的结局]\n好结局「雾散」", system)
        self.assertIn("这个本计划大约 30 轮，现在是第 1 轮", system)
        self.assertNotIn("已经超过计划的轮数", system)

        self.service.tavern._sessions[("group", GROUP_ID)].rounds = 30
        self.gm.narrate = lambda messages: "灯塔重新亮了起来，雾散了。\n\n（全剧终）"
        self._send(_group("我们把海月石放回灯里"))
        self.assertIn("已经超过计划的轮数", str(self.gm.game_requests()[-1][0]["content"]))
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
        self.assertEqual(self.gm.requests, [])
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
        self.assertEqual(archived["turns"][-2]["content"], "【资本狗】我拔出短刀")

        # Afterwards the character answers the chat again.
        self.service.handle_event(_group("你好", mention=True))
        self.assertTrue(str(self.gm.requests[-1][0]["content"]).startswith("你是"))

        self.config.tool_policy.authorized_user_ids = [OUTSIDER]
        self._start()
        self.service.handle_event(_group("退出酒馆", sender=OUTSIDER))
        self.assertIn("到此结束", self._texts()[-1])

    def test_dice_are_rolled_here_and_passed_to_the_game_master(self) -> None:
        self._start()
        with patch.object(dice_module._RANDOM, "randint", side_effect=[4, 5]):
            reply = self.service.handle_event(_group(".r 2d6+1 撬锁"))
        self.assertEqual(reply.text, "🎲 资本狗为「撬锁」掷 2d6+1：4+5+1 = 10")
        self.service.tavern.drain()
        self.assertEqual(self.gm.game_requests()[-1][-1]["content"], "（资本狗为「撬锁」掷了 2d6+1，结果 10）")

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

        (request,) = self.gm.game_requests()
        self.assertIn("你在 QQ 私聊里当跑团主持人", str(request[0]["content"]))
        self.assertEqual(request[-1]["content"], "【资本狗】我飘到气闸门前\n【资本狗】敲三下")

    def test_a_game_survives_a_restart_and_a_stale_one_closes_quietly(self) -> None:
        self._start()
        self._send(_group("我推开门"))
        self.service.tavern.close()  # the process stops; the full close runs at cleanup

        self.service, self.outbound = self._build()
        self._send(_group("我冲进雾里"))
        request = self.gm.game_requests()[-1]
        self.assertEqual(request[-3]["content"], "【资本狗】我推开门")
        self.assertEqual(request[-1]["content"], "【资本狗】我冲进雾里")
        self.service.tavern.close()

        payload = json.loads(self._session_file().read_text(encoding="utf-8"))
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

    def test_a_failing_game_master_keeps_the_actions_for_the_next_round(self) -> None:
        calls = {"count": 0}

        def narrate(messages):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMClientError("request failed: ConnectError")
            return "雾里亮起一盏灯。"

        self.gm.narrate = narrate
        self._start()
        self._send(_group("我点起火把"))
        self.assertEqual(self._texts()[-1], "（主持人卡了一下。刚才的行动都记着，谁再说一句就接着来。）")

        self._send(_group("我往前走"))
        self.assertEqual(self.gm.game_requests()[-1][-1]["content"], "【资本狗】我点起火把\n【资本狗】我往前走")
        self.assertEqual(self._texts()[-1], "雾里亮起一盏灯。")

    def test_a_long_story_is_folded_into_a_recap(self) -> None:
        self.config.tavern.history_chars = 1000
        self.gm.narrate = lambda messages: (
            "前情提要：众人救下信使，往码头去了。" if "整理笔记" in str(messages[0]["content"]) else "剧情" * 200
        )
        self._start()
        for line in ("我扶起信使", "我们去码头", "我找船"):
            self._send(_group(line))

        recap_requests = [request for request in self.gm.requests if "整理笔记" in str(request[0]["content"])]
        self.assertGreaterEqual(len(recap_requests), 1)  # how many depends on how long the opening is
        self.assertIn("【资本狗】我扶起信使", str(recap_requests[0][1]["content"]))
        self._send(_group("我上船"))
        system = str(self.gm.game_requests()[-1][0]["content"])
        self.assertIn("[前情提要]\n前情提要：众人救下信使，往码头去了。", system)
        payload = json.loads(self._session_file().read_text(encoding="utf-8"))
        self.assertLessEqual(sum(len(turn["content"]) for turn in payload["turns"]), 1000 + 400)

    def test_a_sillytavern_card_is_played_the_way_sillytavern_plays_it(self) -> None:
        scenarios = self.root / "tavern" / "scenarios"
        scenarios.mkdir(parents=True)
        card = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "小咪",
                "description": "{{char}}是猫咖里的猫娘店员，管{{user}}叫主人。",
                "personality": "黏人",
                "scenario": "午后的猫咖。",
                "first_mes": "*小咪扑过来*\n欢迎光临，{{user}}主人~",
                "mes_example": "<START>\n{{user}}: 摸摸头\n{{char}}: 喵~",
                "system_prompt": "{{original}} 用中文写。",
                "post_history_instructions": "只写小咪的反应。",
                "creator_notes": "<p>一只会说话的猫娘</p>",
                "character_book": {
                    "entries": [
                        {"keys": ["猫薄荷"], "content": "猫薄荷会让{{char}}犯迷糊。", "enabled": True, "position": "after_char"},
                        {"keys": ["铃铛"], "content": "铃铛是主人送的。", "enabled": True, "position": "before_char"},
                    ]
                },
            },
        }
        (scenarios / "xiaomi.png").write_bytes(_png_card(card))
        (scenarios / "old.json").write_text(
            json.dumps({"char_name": "老板", "char_persona": "酒馆老板", "char_greeting": "要喝点什么？"}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertEqual(read_png_card((scenarios / "xiaomi.png").read_bytes())["data"]["name"], "小咪")

        menu = self.service.handle_event(_group("开启酒馆模式", mention=True)).text
        self.assertIn("5. 老板（角色卡）：酒馆老板", menu)
        self.assertIn("6. 小咪（角色卡）：猫咖里的猫娘店员，管你叫主人", menu)  # a sentence about her, not the notes

        self._send(_group("我想玩小咪"))
        self.assertEqual(self._texts()[-1], "小咪扑过来\n欢迎光临，资本狗主人~")  # her greeting, as SillyTavern shows it
        self.assertEqual(self.gm.requests, [])  # nobody rewrote it
        self.assertTrue(self._texts()[-3].startswith("《小咪》开始！"))
        (picture,) = self.outbound.sent[-2].images  # then her picture, then her greeting
        png = base64.b64decode(picture.removeprefix("base64://"))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82"))
        self.assertNotIn(b"chara", png)  # without the card in it

        self.gm.narrate = lambda messages: "*小咪眯起眼睛*\n喵呜……"
        self._send(_group("我递给她一把猫薄荷"))
        self.assertEqual(self._texts()[-1], "小咪眯起眼睛\n喵呜……")
        (request,) = self.gm.game_requests()
        roles = [message["role"] for message in request]
        self.assertEqual(roles, ["system", "system", "system", "assistant", "user", "system", "system"])
        self.assertEqual(
            request[0]["content"],
            "Write 小咪's next reply in a fictional chat between 小咪 and 资本狗. 用中文写。\n\n"
            "小咪是猫咖里的猫娘店员，管资本狗叫主人。\n\n[小咪's personality: 黏人]\n\n"
            "[Circumstances and context of the dialogue: 午后的猫咖。]\n\n猫薄荷会让小咪犯迷糊。",  # 铃铛 did not come up
        )
        self.assertEqual(request[1]["content"], "[Example Chat]\n资本狗: 摸摸头\n小咪: 喵~")
        self.assertEqual(request[2]["content"], "[Start a new Chat]")
        self.assertEqual(request[3]["content"], "*小咪扑过来*\n欢迎光临，资本狗主人~")
        self.assertEqual(request[4]["content"], "我递给她一把猫薄荷")  # one player: as typed, without a name
        self.assertEqual(request[5]["content"], "只写小咪的反应。")
        self.assertEqual(request[6]["content"], "不写露骨的性内容，不写血腥细节；剧情走到那里就一笔带过或转场。")  # ours
        # The story keeps her reply as she wrote it, so she goes on writing that way.
        payload = json.loads(self._session_file().read_text(encoding="utf-8"))
        self.assertEqual(payload["turns"][-1]["content"], "*小咪眯起眼睛*\n喵呜……")

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

    def test_a_card_played_by_several_knows_who_says_what(self) -> None:
        self._card("cat.json", {"name": "小咪", "description": "{{char}}管{{user}}叫主人。", "first_mes": "喵？"})
        self._send(_group("启动酒馆", mention=True), _group("选小咪"), _group("加入", sender=PLAYER))
        self._send(_group("我摸摸她"), _group("我给她倒牛奶", sender=PLAYER))

        request = self.gm.game_requests()[-1]
        self.assertIn("小咪管资本狗、苦力怕叫主人。", str(request[0]["content"]))
        self.assertEqual(request[-2]["content"], "【资本狗】我摸摸她\n【苦力怕】我给她倒牛奶")
        self.assertTrue(str(request[-1]["content"]).startswith("这一局是 QQ 群里几位玩家一起玩：资本狗、苦力怕。"))

    def test_a_cards_lorebook_goes_where_sillytavern_puts_it(self) -> None:
        entries = [
            {"keys": [], "constant": True, "content": "每轮末尾写状态。", "position": "at_depth", "extensions": {"position": 4, "depth": 0}},
            {"keys": ["灵石"], "content": "灵石是货币。", "position": "after_char", "extensions": {"position": 3}},
            {"keys": ["剑"], "selective": True, "secondary_keys": ["天阳"], "content": "天阳剑是灵宝。", "extensions": {"position": 0}},
            {"keys": ["剑"], "selective": True, "secondary_keys": ["木"], "content": "一把木剑。", "extensions": {"position": 0}},
            {"keys": ["/丹[药丸]/"], "content": "丹药能疗伤。", "insertion_order": 1},
        ]
        self._card("xian.json", {"name": "仙界", "description": "修仙世界。", "first_mes": "你在山门前。", "character_book": {"entries": entries}})
        self._send(_group("启动酒馆", mention=True), _group("选仙界"), _group("我拿灵石换了天阳剑和一颗丹丸"))

        request = self.gm.game_requests()[-1]
        self.assertEqual(
            request[0]["content"],
            "Write 仙界's next reply in a fictional chat between 仙界 and 资本狗.\n\n天阳剑是灵宝。\n\n修仙世界。\n\n丹药能疗伤。",
        )
        self.assertEqual(
            [message["content"] for message in request[1:6]],
            ["[Start a new Chat]", "灵石是货币。", "你在山门前。", "我拿灵石换了天阳剑和一颗丹丸", "每轮末尾写状态。"],
        )  # with the author's note four deep (here: the top of the chat), and at depth 0 after the last line

    def test_a_fault_in_tavern_mode_does_not_silence_the_character(self) -> None:
        async def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk full")

        with patch.object(self.service.tavern, "ahandle", broken), self.assertLogs(
            "waifu_standalone.reply_flow.service", "ERROR"
        ):
            reply = self.service.handle_event(_group("你好", mention=True))
        self.assertIsNotNone(reply)
        self.assertTrue(str(self.gm.requests[-1][0]["content"]).startswith("你是"))

    def test_a_storys_status_and_options_tags_reach_qq_as_plain_text(self) -> None:
        scenarios = self.root / "tavern" / "scenarios"
        scenarios.mkdir(parents=True)
        opening = "<场景>舞台边|黄昏|霓虹半熄</场景>\n莉央：「最后一首歌。」\n<状态>30|蓄势待发|别让他们失望</状态>\n<选项>看线索|问来历</选项>"
        (scenarios / "rio.json").write_text(
            json.dumps({"title": "莉央", "kind": "story", "opening": opening}, ensure_ascii=False), encoding="utf-8"
        )
        self.gm.narrate = lambda messages: "莉央：「好。」\n<状态>33|安心|有人在听</状态>\n<选项>上台|等等</选项>"

        self._send(_group("启动酒馆", mention=True), _group("选莉央"))
        self.assertEqual(
            self._texts()[-1],
            "【舞台边 · 黄昏 · 霓虹半熄】\n莉央：「最后一首歌。」\n（30｜蓄势待发｜别让他们失望）\n可以选：① 看线索　② 问来历",
        )
        self._send(_group("1"))
        self.assertEqual(self._texts()[-1], "莉央：「好。」\n（33｜安心｜有人在听）\n可以选：① 上台　② 等等")
        # The model keeps seeing its own tags, so it goes on writing them; "1" sent the first option.
        self.assertEqual(self.gm.game_requests()[-1][2]["content"], opening)
        self.assertEqual(self.gm.game_requests()[-1][-1]["content"], "【资本狗】看线索")

    def test_a_cards_status_bar_and_buttons_reach_qq_as_lines(self) -> None:
        greeting = "<场景>后台|雨夜|灯低</场景>\n顾绮：「这么晚还来？」\n<状态>25|试探|看他怎么答</状态>\n<选项>说目的|看证据|转身走</选项>"
        self._card(
            "guqi.json",
            {
                "name": "顾绮",
                "description": "顾绮是剧院投资人，35岁。",
                "first_mes": greeting,
                "system_prompt": "你扮演顾绮。\n【输出格式】回复末尾两行：<状态>心动|心情|心声</状态>",
                "extensions": {"regex_scripts": [{"scriptName": "状态栏 · 心动", "findRegex": "/<状态>/g"}]},
            },
        )
        # A model may get a closing tag wrong, or leave it out.
        self.gm.narrate = lambda messages: "顾绮：「证据在这。」\n<状态>31|松动|他没走</status>\n<选项>接过来|问来历|还给她"

        self._send(_group("启动酒馆", mention=True), _group("选顾绮"))
        rules, sent = self._texts()[-2:]
        self.assertIn("直接说话就是行动，回数字就是选那个选项", rules)
        self.assertEqual([part.images for part in self.outbound.sent[-2:]], [[], []])  # a JSON card has no picture
        self.assertEqual(
            sent, "【后台｜雨夜｜灯低】\n顾绮：「这么晚还来？」\n\n心动 25 ▰▰▱▱▱▱▱▱▱▱｜心情 · 试探\n心声：看他怎么答\n\n① 说目的\n② 看证据\n③ 转身走"
        )

        self._send(_group("选②"))  # the second button
        request = self.gm.game_requests()[-1]
        self.assertEqual(request[-3]["content"], greeting)  # the model sees what it wrote, tags and all
        self.assertEqual(request[-2]["content"], "看证据")
        self.assertEqual(
            self._texts()[-1], "顾绮：「证据在这。」\n\n心动 31 ▰▰▰▱▱▱▱▱▱▱｜心情 · 松动\n心声：他没走\n\n① 接过来\n② 问来历\n③ 还给她"
        )
        self._send(_group("我想想"))
        self.assertEqual(self.gm.game_requests()[-1][-2]["content"], "我想想")

    def test_adult_scenarios_and_content_only_in_an_allowed_private_chat(self) -> None:
        adult = self.root / "tavern" / "adult"
        adult.mkdir(parents=True)
        (adult / "night.json").write_text(
            json.dumps({"title": "夜色", "kind": "story", "opening": "夜深了。"}, ensure_ascii=False), encoding="utf-8"
        )
        self.config.tavern.adult_user_ids = [HOST]

        # A group never sees the adult folder, even when someone on the list opens the tavern.
        self.assertNotIn("夜色", self.service.handle_event(_group("启动酒馆", mention=True)).text)
        self._send(_group("选 1"), _group("我推开门"))
        self.assertIn("一笔带过或转场", str(self.gm.game_requests()[-1][0]["content"]))
        self.service.handle_event(_group("退出酒馆模式"))

        # Someone not on the list does not see it in private either.
        stranger = InboundEvent(PLAYER, "person", PLAYER, NAMES[PLAYER], [MessageSegment(kind="text", text="启动酒馆")])
        self.assertNotIn("夜色", self.service.handle_event(stranger).text)

        # The person on the list, in private: on the menu, marked, and the game drops the fade-out rule.
        self.assertIn("5. 夜色（成人）", self.service.handle_event(_private("启动酒馆")).text)
        self._send(_private("选5"), _private("我点了一支蜡烛"))
        system = str(self.gm.game_requests()[-1][0]["content"])
        self.assertIn("亲密情节可以按角色卡的设定写", system)
        self.assertIn("所有角色都必须是成年人", system)
        self.assertNotIn("一笔带过或转场", system)

    def test_a_cards_html_and_markdown_reach_qq_as_what_they_show(self) -> None:
        greeting = (
            "<div style=text-align:center><img src=https://example.com/a.png /><h1>{{char}}</h1></div>\r\n"
            "*仙门大开，山风扑面。*\r\n---\r\n<details><summary><center>**面板（点击展开）**</center></summary>\r\n"
            "<small>境界：炼气</small>\r\n</details>\r\n\r\n> 📜提示：\r\n<small>\"别回头\"</small>\r\n</plot>"
        )
        self._card("xian.json", {"name": "仙境", "first_mes": greeting})
        self._send(_group("启动酒馆", mention=True), _group("选仙境"))

        self.assertEqual(self._texts()[-1], '仙境\n\n仙门大开，山风扑面。\n──────────\n\n面板\n\n境界：炼气\n\n📜提示：\n"别回头"')
        payload = json.loads(self._session_file().read_text(encoding="utf-8"))
        self.assertTrue(payload["turns"][0]["content"].startswith("<div style=text-align:center>"))

    def test_the_tavern_can_use_a_model_of_its_own(self) -> None:
        calls: list[tuple[dict[str, object], list[dict[str, object]]]] = []

        class TavernModel:
            def __init__(self, **settings):  # type: ignore[no-untyped-def]
                self.settings = settings

            async def ainvoke_with_tools(self, messages, *, tools, user=""):  # type: ignore[no-untyped-def]
                calls.append((self.settings, list(messages)))
                return LLMToolResponse(text="<think>先想想雾港的局势</think>雾里亮起一盏灯。", assistant_message={})

            def close(self) -> None:
                pass

        tavern = self.config.tavern
        tavern.llm_base_url, tavern.llm_api_key, tavern.llm_model = "https://example.com/v1", "sk-test", "deepseek-tavern-v2-pro"
        with patch("waifu_standalone.tavern.mode.OpenAILLMClient", TavernModel):
            self._start()
            self._send(_group("我推开门"))

        (settings, messages), = calls
        self.assertEqual(settings["model"], "deepseek-tavern-v2-pro")
        self.assertEqual(messages[-1]["content"], "【资本狗】我推开门")
        self.assertEqual(self._texts()[-1], "雾里亮起一盏灯。")  # its thinking is not sent
        self.assertEqual(self.gm.game_requests(), [])  # the chat model was not asked

    def test_a_round_too_explicit_for_the_game_is_rewritten_or_skipped(self) -> None:
        replies = iter(["他们开始做爱。", "灯灭了，第二天清晨……", "做爱。", "还是做爱。"])
        self.gm.narrate = lambda messages: next(replies)
        self._start()

        self._send(_group("我关上灯"))
        self.assertEqual(self._texts()[-1], "灯灭了，第二天清晨……")
        self.assertIn("刚才那段写得太露骨了", self.gm.game_requests()[-1][-1]["content"])
        self._send(_group("再来"))
        self.assertEqual(self._texts()[-1], "（这一段跳过。）")

        # An adult game in an allowed private chat is not held back.
        self.service.handle_event(_group("退出酒馆模式"))
        self.config.tavern.adult_user_ids = [HOST]
        self.gm.narrate = lambda messages: "他们开始做爱。"
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
        self.gm.narrate = lambda messages: "<div>*山门前一片寂静。*</div>\n守门仙子：「报上名来。」"

        menu = self.service.handle_event(_group("启动酒馆", mention=True)).text
        self.assertIn("5. 逆天修仙：", menu)
        self.assertNotIn("逆天修仙（角色卡）", menu)
        self._send(_group("选逆天修仙"))
        # The greeting that tells the story, not the credits page.
        self.assertEqual(self._texts()[-1], "天空裂开一道缝，一个少年从云里坠了下来，砸进仙女界的山门前。\n守门的仙子愣住了：「男人？」")

        self._send(_group("我站起来拍拍土"))
        request = self.gm.game_requests()[-1]
        self.assertTrue(str(request[0]["content"]).startswith("Write 逆天修仙's next reply in a fictional chat"))
        self.assertEqual(self._texts()[-1], "山门前一片寂静。\n\n守门仙子：「报上名来。」")  # no HTML, no asterisks

    def test_a_card_with_someone_under_18_is_never_an_adult_game(self) -> None:
        self.config.tavern.adult_user_ids = [HOST]
        self._card(
            "xianxia.json",
            {"name": "仙女界", "description": "修仙 RPG。", "first_mes": "{{user}}，穿越，16岁，唯一男性。你从湖里爬上了岸。"},
            folder="adult",
        )
        self._card("star.json", {"name": "织微", "description": "她 8 岁那年见过流星，如今 300 岁。人类 28~46 岁。"}, folder="adult")
        self.assertFalse(self.service.tavern.library.get("adult/star").minors)  # a past age, and adults

        self.gm.narrate = lambda messages: "他们开始做爱。"
        self._send(_private("启动酒馆"), _private("选仙女界"))
        self.assertIn("（卡里有人未满 18 岁，这一局不写成人内容。）", self._texts()[-2])
        self._send(_private("我往村里走"))
        self.assertTrue(str(self.gm.game_requests()[0][-1]["content"]).startswith("故事里有未成年人：对任何人都不写性感、暧昧"))
        self.assertEqual(self._texts()[-1], "（这一段跳过。）")

    def test_a_cards_preset_reaches_the_tavern_model(self) -> None:
        calls: list[dict[str, float] | None] = []

        class TavernModel:
            def __init__(self, **settings):  # type: ignore[no-untyped-def]
                pass

            async def ainvoke_with_tools(self, messages, *, tools, user="", sampling=None):  # type: ignore[no-untyped-def]
                calls.append(sampling)
                return LLMToolResponse(text="她点点头。", assistant_message={})

            def close(self) -> None:
                pass

        preset = {
            "temperature": 0.9, "max_tokens": 4096, "top_p": "high", "frequency_penalty": 5,
            "prompts": [{"content": "所有角色均为成年人。", "enabled": True}, {"content": "关掉的一条", "enabled": False}],
        }
        self._card("guqi.json", {"name": "顾绮", "first_mes": "雨夜。", "extensions": {"pixel_tavern_presets": [preset]}})
        tavern = self.config.tavern
        tavern.llm_base_url, tavern.llm_api_key, tavern.llm_model = "https://example.com/v1", "sk-test", "deepseek-tavern-v2-pro"
        with patch("waifu_standalone.tavern.mode.OpenAILLMClient", TavernModel):
            self._send(_group("启动酒馆", mention=True), _group("选顾绮"), _group("我收起伞"))

        self.assertEqual(calls, [{"temperature": 0.9, "max_tokens": 4096}])  # what an API accepts
        card = self.service.tavern.library.get("guqi").card
        self.assertEqual(card.prompts, ["所有角色均为成年人。"])

    def test_the_config_file_sets_the_tavern(self) -> None:
        # ConfigManager.load() would also look for napcat/config under the working directory.
        tavern = _load_dataclass(TavernConfig, {"enabled": False, "batch_seconds": 5, "admin_user_ids": ["1", " 2 "]})
        self.assertFalse(tavern.enabled)
        self.assertEqual(tavern.batch_seconds, 5.0)
        self.assertEqual(tavern.admin_user_ids, ["1", "2"])
        self.assertEqual(tavern.idle_timeout_seconds, 1800.0)

        self.config.tavern.enabled = False
        self.service.handle_event(_group("启动酒馆", mention=True))
        self.assertFalse(any(text.startswith("酒馆开张啦") for text in self._texts()))


    def test_half_an_emoji_does_not_stop_a_game_from_being_saved(self) -> None:
        self.gm.narrate = lambda messages: "雾里传来笑声\ud83d，你听见了吗？"
        self._start()
        self._send(_group("我循着笑声走过去"))
        self._send(_group("我停下脚步"))
        saved = json.loads(self._session_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["turns"][-1]["content"], "雾里传来笑声\ufffd，你听见了吗？")
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
        request = self.gm.game_requests()[-1]
        self.assertEqual(request[-3]["content"], "【资本狗】我拔出短刀")
        self.assertEqual(request[-1]["content"], "【资本狗】我冲进雾里")

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
        payload["turns"][-1]["content"] += "\n（全剧终）"
        archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertIn("没有能接着玩的存档", self.service.handle_event(_group("继续酒馆", mention=True)).text)

        # Not an adult game, outside the private chat that may play it.
        payload["turns"][-1]["content"] = "第1段剧情。"
        payload["adult"] = True
        archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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

    def test_an_admin_sends_a_card_as_a_file_and_it_is_on_the_menu(self) -> None:
        card = {"spec": "chara_card_v2", "data": {"name": "艾琳", "description": "温柔但毒舌的青梅竹马。", "first_mes": "怎么还没睡？"}}
        self.assertEqual(self._send_file("艾琳.png", _png_card(card)), "只有管理员能往酒馆里放角色卡。")

        self.config.tool_policy.authorized_user_ids = [HOST]
        self.assertEqual(
            self._send_file("main_card_v2.png", _png_card(card)),
            "收到角色卡《艾琳》，放进了酒馆卡库。说「启动酒馆」就能在菜单里选到它。",
        )
        self.assertTrue((self.root / "tavern" / "scenarios" / "艾琳.png").is_file())
        self.assertIn("艾琳（角色卡）", self.service.handle_event(_private("启动酒馆")).text)
        self.service.handle_event(_private("退出酒馆模式"))

        # Sent again, it replaces the copy; as JSON it is read the same way.
        self.assertIn("更新了酒馆卡库", self._send_file("艾琳.json", json.dumps(card, ensure_ascii=False).encode("utf-8")))
        self.assertEqual(sorted(path.name for path in (self.root / "tavern" / "scenarios").iterdir()), ["艾琳.json"])

        # Tagged NSFW, it goes where only an allowed private chat sees it.
        night = {"spec": "chara_card_v2", "data": {"name": "夜莺", "tags": ["NSFW"], "first_mes": "晚上好。"}}
        self.assertIn("成人卡库", self._send_file("夜莺.png", _png_card(night)))
        self.assertTrue((self.root / "tavern" / "adult" / "夜莺.png").is_file())
        self.assertFalse((self.root / "tavern" / ".incoming").exists() and any((self.root / "tavern" / ".incoming").iterdir()))

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
