from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waifu_standalone.app import build_default_service
from waifu_standalone.config import AppConfig, TavernConfig
from waifu_standalone.infra.llm_clients import LLMClientError, LLMToolResponse
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
        """The game master's rounds, without recaps and the character's own replies."""
        return [
            request
            for request in self.requests
            if str(request[0]["content"]).startswith("你在 QQ ") and "整理笔记" not in str(request[0]["content"])
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
        self.assertEqual(len(recap_requests), 1)
        self.assertIn("【资本狗】我扶起信使", str(recap_requests[0][1]["content"]))
        self._send(_group("我上船"))
        system = str(self.gm.game_requests()[-1][0]["content"])
        self.assertIn("[前情提要]\n前情提要：众人救下信使，往码头去了。", system)
        payload = json.loads(self._session_file().read_text(encoding="utf-8"))
        self.assertLessEqual(sum(len(turn["content"]) for turn in payload["turns"]), 1000 + 400)

    def test_a_sillytavern_card_is_played_as_its_character(self) -> None:
        scenarios = self.root / "tavern" / "scenarios"
        scenarios.mkdir(parents=True)
        card = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "小咪",
                "description": "{{char}}是猫咖里的猫娘店员，管{{user}}叫主人。",
                "personality": "黏人",
                "first_mes": "欢迎光临，{{user}}主人~",
                "mes_example": "<START>\n{{char}}: 喵~",
                "creator_notes": "<p>一只会说话的猫娘</p>",
                "character_book": {"entries": [{"keys": ["猫薄荷"], "content": "猫薄荷会让{{char}}犯迷糊。", "enabled": True}]},
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
        self.assertEqual(self._texts()[-1], "欢迎光临，资本狗主人~")
        self._send(_group("我递给她一把猫薄荷"))
        system = str(self.gm.game_requests()[-1][0]["content"])
        self.assertTrue(system.startswith("你在 QQ 群里和玩家角色扮演：你扮演小咪"))
        self.assertIn("小咪是猫咖里的猫娘店员，管资本狗叫主人。\n性格：黏人", system)
        self.assertIn("[世界书]\n- 猫薄荷会让小咪犯迷糊。", system)
        self.assertIn("[说话风格示例]\n小咪: 喵~", system)

    def test_a_fault_in_tavern_mode_does_not_silence_the_character(self) -> None:
        async def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk full")

        with patch.object(self.service.tavern, "ahandle", broken), self.assertLogs(
            "waifu_standalone.reply_flow.service", "ERROR"
        ):
            reply = self.service.handle_event(_group("你好", mention=True))
        self.assertIsNotNone(reply)
        self.assertTrue(str(self.gm.requests[-1][0]["content"]).startswith("你是"))

    def test_a_cards_status_and_options_tags_reach_qq_as_plain_text(self) -> None:
        scenarios = self.root / "tavern" / "scenarios"
        scenarios.mkdir(parents=True)
        opening = "<场景>舞台边|黄昏|霓虹半熄</场景>\n莉央：「最后一首歌。」\n<状态>30|蓄势待发|别让他们失望</状态>\n<选项>看线索|问来历</选项>"
        (scenarios / "rio.json").write_text(
            json.dumps({"spec": "chara_card_v2", "data": {"name": "莉央", "first_mes": opening}}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.gm.narrate = lambda messages: "莉央：「好。」\n<状态>33|安心|有人在听</状态>\n<选项>上台|等等</选项>"

        self._send(_group("启动酒馆", mention=True), _group("选莉央"))
        self.assertEqual(
            self._texts()[-1],
            "【舞台边 · 黄昏 · 霓虹半熄】\n莉央：「最后一首歌。」\n（30｜蓄势待发｜别让他们失望）\n可以选：① 看线索　② 问来历",
        )
        self._send(_group("1"))
        self.assertEqual(self._texts()[-1], "莉央：「好。」\n（33｜安心｜有人在听）\n可以选：① 上台　② 等等")
        # The model keeps seeing its own tags, so it goes on writing them.
        self.assertEqual(self.gm.game_requests()[-1][2]["content"], opening)

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


if __name__ == "__main__":
    unittest.main()
