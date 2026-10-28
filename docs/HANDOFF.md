# 交接说明（2026-10-25，2026-10-27 更新）

给新会话：这份文档只记录工程事实。先读第 1、2、3 节。第 4 节的 A、B、C 三块（PR #27）、阶段 0 的 P0 修复（第 2.1 节，PR #28）和审计修复加新版私聊链路（PR #29）都已合入 main；2026-10-27 起服务在本机 Docker 里真实收发，部署和实测情况见第 3 节。剩下的事在各块的「没做的」、第 2.1 节的「要 owner 决定的」和第 5 节。标注「（审计结论）」的条目来自代码审查，没有逐条复核，改之前先对一下代码。

## 1. 先读这些

- 跑测试：`PYTHONPATH=src .venv/bin/python -B -m unittest discover -s tests`，794 个，约 126 秒，目前全过；测试在临时工作目录里跑（见第 3.1.1 节）。系统 `python3` 没装 `httpx`，必须用项目的 `.venv/bin/python`。
- CLAUDE.md 里的模块表已经过时，代码现在按子包组织：`runtime/`（服务编排）、`reply_flow/`（回复链路、提示词）、`memory/`（会话与知识）、`repositories/`（SQLite 状态）、`skills/`（技能与工具）、`members/`、`character/`、`settings_admin/`、`web/`、`infra/`、`gateways/`、`systems/`、`organs/`。
- 一次回复发给模型的是真正的多轮对话（2026-10-27 起，`feat/prompt-structure`）：
  - system：`character/cards.py::CharacterCard.system_message` 拼出人设、擅长、背景、规则、你记得的事、关于对方、当前情况、更早的聊天摘要（中文标题）；这条消息需要工具时，末尾再加 `[工具]`（`_TOOL_CALLING_SYSTEM_PROMPT`）。
  - 之后是 `Memory.dialogue_turns` 给出的历史，机器人的发言是 assistant，别人的是 user；群聊里 user 消息前带发言人名字。最后一条 user 是当前消息（图片也在这里）。
  - 只支持单条文本提示词的后端（Dify）仍走 `CharacterCard.system_prompt` 拼成的一大段文字。
- 机器人永远是卡片上的角色名；某个用户给它起的别名只作为一句备注（「X 管你叫「大母牛」…」），不会再替换它的名字。
- 工具按需出现（`tools_on_demand`，默认开，`skills/tool_intent.py`）：消息说到天气、搜索、链接、称呼、画图、文件时才带对应工具，或者某个技能的关键词命中；普通闲聊不带工具也不带工具规则。机器人刚问过称呼时，下一条回复带称呼工具。`/skill` 不受影响。
- 防复读：直接回复如果和机器人最近 6 条之一几乎一样、共用 10 字以上的句子、或和前 3 条用同一个开头，就带着「别重复」提示重写一次；前 3 条都以「……」开头而重写后还是，就去掉开头的「……」。
- 每条被回复的消息通常 2 次模型调用：回复、后台知识抽取；需要工具、称呼判断或重写时多 1–2 次。`thinking_mode`（回复前的「内心分析」调用）默认关。每次召回还有 1 次嵌入调用。
- 像真人地发（2026-10-27，`feat/human-touch`，第 3.6 节有实测）：
  - 分段：模型写的聊天回复（`OutboundMessage.conversational`）不超过 `segmented_reply.max_total_chars`（150 字）时，按句末标点和换行拆成至多 `max_parts`（3）条，不到 4 个字的碎句并到相邻一句；条与条之间随机停 `min_delay_seconds`–`max_delay_seconds`（1–2.5 秒），dry_run 不停。列表、代码块、长回复整段发；命令结果、通知、没有模型时的兜底回复不拆。会话历史里仍是完整的一条。
  - 引用：群里回复时，如果那条消息之后群里又有人说过话，第一条带 OneBot `reply` 段引用它；紧接着回的不引用。`OutboundEmitter.note_inbound` 在拿会话锁之前记下每个群最新的消息 id，引用与否在真正发送时判断（群回复延迟之后）。`group_reply_quote=false` 关掉。
  - 表情包：`data/stickers/<分类>/` 里的 png、jpg、gif、webp（单张不超过 2 MB，`reply_flow/stickers.py`）。有可发的分类时，system 里加 `[表情包]` 段列出分类；模型在回复末尾写「[表情:分类]」就从该分类随机挑一张（名字对不上时取互相包含的分类，「很开心」→「开心」），`sub_type=1`（QQ 按表情显示）单独发在文字后面。同一个聊天两张之间至少隔 `stickers.min_replies_between`（3）条回复，冷却期间不出现 `[表情包]` 段。发出的标签留在历史里，没发出的标签删掉；任何消息里的标签都不会当文字发出去。冷却计数在内存里，重启清零。线上的表情包库见第 3.5 节。
  - 看图：收到的图片由 `infra/image_inputs.py` 自己下载（走 `safe_fetch`，最多 10 MB），GIF 和动图取第一帧，长边超过 1536 像素就缩小，以 data URL 发给模型，每条消息最多 4 张。下载或解码失败、后端不支持图片、模型拒收时，当前消息末尾加「（对方发来的图片没能加载出来，你看不到它的内容）」让角色自己回应（模型拒收时去掉图片再请求一次），不再回「图片处理失败…」「当前模型后端不支持图片理解…」这类系统话。QQ 表情包（图片段 `sub_type=1`）在聊天记录里记作「发了一个表情」，模型照样看得到图。
- 切换人物卡（`feat/persona-switch`，`character/switcher.py`）：聊天里说「切换人物卡」列出 `data/cards/` 里的卡（附带的 `default` 模板只在正在用时才列出），说「切换/切换到/切换成/切换回/换成 + 名字」就换。名字可以是卡的 ID、`assistant_name` 或 `<卡>.meta.json` 里的 `aliases`，也认互相包含的写法（「元气雌小鬼」→ 别名「雌小鬼」）；对不上任何卡的（「切换一下话题」）按普通聊天处理。群里必须明确 @，跟进窗口里的不算。换卡和控制台一样是全局的，所有私聊和群都换；`activate_character` 在有回复或后台写回在跑时会拒绝，所以命令先返回，后台每 0.5 秒重试，最多等 60 秒（`SWITCH_WAIT_SECONDS`），换好后新角色从 `greetings` 里随机挑一句打招呼，等不到就用旧角色说一句没换成。列表里每张卡的简介取 meta 的 `intro`，没有就取 Profile 第一句。`character_switch_user_ids`（顶层，默认空）非空时只有这些 QQ 号能换。每张卡的会话、成员目录和知识各自独立，换到新卡后它不认识群友的称呼，要重新认识。
- 技能修复（`feat/persona-switch`，起因见第 3.7 节）：
  - 群里直接 @ 机器人的消息改用每人一个的桶（`rate_limits.mention_reply_burst` / `mention_reply_per_hour`，默认 8 和 120；2026-10-27 从 4 和 30 放宽，原来 owner 连着 @ 五次就被挡），不再占群的共享额度；跟进窗口里的接话仍走 `group_reply_burst`。机器人之间斗嘴用光群额度后，真人 @ 照样有回应。
  - `infra/safe_fetch.py`：解析结果落在 198.18.0.0/15（代理的 fake-IP）时，再用阿里 DoH（`https://dns.alidns.com/resolve`）查真实地址，全是公网才放行；查不到就拦。线上容器里国外域名都解析到 198.18.x.x，原来抓网页、总结链接对国外网站全部失败。
  - 技能列表（「你会什么」）只列这个人能用、能主动要求的技能，每行写一种说法和 `/skill` 写法，交给角色转述，不再贴原始代码块；没授权的人看不到文件工具和抓网页。
  - 文件工具的相对路径按第一个允许目录算（原来按配置文件所在目录），不给路径就列整个目录；默认配置下两者相同。
- 回复变快（`fix/reply-latency`，实测见第 3.8 节）：
  - `llm.reasoning_effort`（默认空 = 模型默认）随每个聊天请求发出；线上设成 `low`。grok-4.7 默认每条回复先想 1,500–2,700 个 token，24–40 秒；`low` 只想 300 个左右，5–8 秒。非推理模型不认这个参数，要留空。
  - 群里没人 @ 的接话，如果轮到它时群里已经有更新的消息（`OutboundEmitter.superseded`），就不单独回，留给最新那条；它还在环境缓冲里，作为上下文进下一次回复。@ 机器人的照常回。原来每条都按顺序回，一条 15–25 秒，群里一热闹就积压几分钟。
  - 有切换在等时，新消息先等切换完成（`CharacterSwitcher.wait_for_switch`，在拿会话锁之前），由新角色回答；切换等了 3 秒还没完成（`SWITCH_NOTICE_SECONDS`），旧角色先说一句「等我一下，马上换成「X」」。
  - 调模型时连接在拿到响应前断开（不是超时、也没有 HTTP 状态码）就重试一次；`TransportError` 的消息里写上异常类型（原来只有「request failed: 」）。发 QQ 消息的动作不重试。
  - 单独的 `/skill`、`/skills` 回技能列表，`/skills weather 上海` 等同 `/skill weather 上海`。共享文件夹为空时，列文件的结果写明「这是机器人的共享文件夹，不是电脑桌面」。
  - Dockerfile 里 `npm i -g @steipete/summarize` 挪到复制代码之前，改代码不再重装（2026-10-27 一次部署因为 npm 超时构建失败）。
  - 没改：新成员第一次说话时的称呼追问仍要调一次模型（群里的起名流程靠它的 `asked_once` 状态衔接），换到新卡后每个人第一次说话会多 2–5 秒。
- `/skill` 和发文件（`feat/send-file`）：
  - `/skill` 后面第一个词不是技能名时（「/skill 在我的电脑上写个笑话保存为 xiaohua.txt」），不再当成 `/` 开头的消息丢掉，而是交给模型，并给出这个人能用的全部工具（`exposure_context(all_tools=True)`，`ToolExposureContext.explicit`）。系统消息的【工具】段多一句「对方用 /skill 明确要你用工具办事」；模型第一轮只说打算、不调工具时，带着提醒再问一次（`_EXPLICIT_TOOL_NUDGE`，只问一次）。真实测试里第一次就出现过只说「我先写进共享文件夹，写好再发」不动手的回复。
  - 「保存为」「存成」「存到」「另存」「写到/进/入」和 `.txt/.md/.json/.csv/.log` 也算文件说法，会带上文件工具。
  - 新工具 `send-file`（技能 `send-file-command`，风险 `filesystem`，和读写文件一样只给 `tool_policy.authorized_user_ids`）：把共享文件夹里的一个文件发到当前聊天，群里用 NapCat 的 `upload_group_file`，私聊用 `upload_private_file`。NapCat 在另一个容器里读不到这个文件夹，所以内容以 `base64://` 内联发过去（NapCat 先写成临时文件），单个文件最多 20 MB（`_SEND_FILE_MAX_BYTES`）。出站端口都加了 `call_action` / `acall_action`；dry_run 的 `CapturingOutboundPort` 把这类动作记在 `actions` 里。
  - dry_run（grok-4.7 low）：owner 的原句写好 `xiaohua.txt` 并作为群文件发出，28.8 秒（两轮工具）；「再写个绕口令存成 raokouling.txt，也发到群里」11 秒；苦力怕的 `/skill 把资本狗电脑里的文件都发到群里` 没有文件工具，被拒绝。

- 酒馆模式（`feat/tavern-mode`，`tavern/`，README「核心能力」里有玩法）：
  - 入口在 `reply_flow/service.py` 最前面（记完入站、判空之后，`/skill` 判断之前）：`TavernMode.ahandle` 返回 `(True, 回复)` 就不再走角色的链路。群里开局要 @，开局后的命令和行动都不用 @。
  - 命令（去掉空白和标点后整句匹配，正则在 `tavern/mode.py` 顶部）：「启动酒馆」「开启酒馆模式」「酒馆」列菜单，说的人当房主；房主回「选 2」「第二个」「2」或本名开本，「随机」随便挑；「加入」「离开」；「酒馆状态」；「退出酒馆模式」「关闭酒馆」「打烊」只有房主、`tavern.admin_user_ids`、`tool_policy.authorized_user_ids` 能用。菜单阶段房主说别的、别人说话，照常交给角色。
  - 口语也能开局（2026-10-28 补）：对她说的话里有「开/启动/打开/进入/玩/来 + (一下/个/局) + 酒馆/跑团」就列菜单（`_START_INTENT`），「不开/别开酒馆」不算（`_START_REFUSED`）；开着「听完再说」时，不 @ 但叫她名字、`call_names`，或她刚回过的人说，也算在对她说（`_addressed` 用 `listener.addresses_bot`）。开局后只认整句命令，免得「我要在镇上开酒馆」这类行动被当成命令。没开局时消息提到酒馆或跑团，回复的 system 里多一段 `[酒馆模式]` 告诉她怎么开（`TavernMode.prompt_hint`）。起因：线上苦力怕说「我让他开酒馆模式」「命令出来他就得开」，原来只认 @ 加整句「启动酒馆」，没认出来，小葵不知道自己有这个功能，按人设一直拒绝。
  - 回合：玩家每句话记成「【名字】原话」（截到 300 字）放进 `pending`。第一条进来后最多等 `tavern.batch_seconds`（10 秒）；所有玩家都说过话、或者只有程序的备注（有人加入、开局）时，安静 `settle_seconds`（2 秒）就提前开；攒到 30 条立即开。一轮调一次 `Generator.acomplete_chat`（新方法，和角色无关，用同一个模型和 `reasoning_effort`）。system 是主持人身份加 [剧本设定][主持要点][世界书][前情提要][玩家][说话风格示例][规则]；后面是剧情回合（玩家的一轮是 user，主持人是 assistant，开场白前补一条「（开场）」），最后是这一轮的行动。模型失败时行动留在 `pending`，群里说一次「主持人卡了一下」，下次有人说话时一起接上。
  - 括号（…）或 `//` 开头的场外话、玩家只发图或表情，都不进剧情。局外人 @ 机器人时提示一次「这里正在玩…想一起玩就说「加入」」，之后不再回；局外人不 @ 的话直接忽略。开局期间角色在这个聊天里不说话；`/skill` 和 `生图` 前缀仍交给角色。
  - 掷骰（`tavern/dice.py`）：`.r`、`。r`、`/r`、`/roll`、`掷骰`、`掷 2d6` 等，最多 20 个骰子、2–1000 面、加减值 ±1000，超出就回一句范围说明。用 `random.SystemRandom` 掷，群里发「🎲 苦力怕为「撬锁」掷 2d6+1：4+5+1 = 10」，主持人收到「（苦力怕为「撬锁」掷了 2d6+1，结果 10）」。「掷出飞刀」「.reset」不算掷骰。
  - 剧情超过 `tavern.history_chars`（8000 字，最小 1000）时，旧回合（保留开场白和最近约一半预算）用一次模型调用压成 300 字内的前情提要（`summary`，上限 600 字）；压缩失败就只在发送时按预算截掉旧回合。
  - 剧本（`tavern/scenarios.py`）：自带 4 个在 `templates/tavern/`（雾港酒馆、午夜便利店、拾荒者号、雨夜沈园，都写了只给主持人看的要点），`data/tavern/scenarios/` 里同名文件覆盖自带的。格式：和人物卡同款语法的 YAML（`title`、`intro`、`kind`、`character`、`Setting`、`Notes`、`Opening`、`Examples`），或同样字段的 JSON。SillyTavern 角色卡 `.json`（V1 的 `char_name`/`char_persona`/`char_greeting`，V2/V3 的 `data`）和 `.png`（`ccv3` 或 `chara` 文本块里的 base64 JSON；自己解析 PNG 块，不依赖 PIL）当 `kind=character`：主持人扮演这个角色。`{{char}}`、`{{user}}`（换成房主名字）、`<BOT>`、`<USER>` 会替换；`character_book` 或世界书条目的关键词出现在这一轮行动或上一段剧情里时进 [世界书]，`constant` 的总是进，合计 1500 字内。菜单最多列 20 个。
  - SillyTavern 卡常要求每轮写 `<场景>地点|时间|氛围</场景>`、`<状态>数值|心情|心声</状态>`、`<选项>行动一|行动二</选项>`（网页上画成状态栏和按钮）。发到 QQ 前由 `render_markup` 转成「【地点 · 时间 · 氛围】」「（数值｜心情｜心声）」「可以选：① 行动一　② 行动二」；剧情记录里保留原样，模型接着按格式写，状态数值接着算。卡在菜单里的简介取描述里第一句讲人物的话（跳过含「原创」「成年」「角色卡」「NSFW」这类声明的句子，去掉开头的名字），找不到才用作者备注。
  - 角色卡用聊天风演（2026-10-28，owner 看了真实输出觉得「不像人说的话」）：`kind=character` 的局，system 换成「你就是X，正在 QQ 群里跟大家对戏」和 `_CHARACTER_RULES`：第一人称、动作写在（）里、一次一到四句，不写场景标题、状态栏、选项、「名字：「台词」」剧本格式，不复述对方、不提规则和编号。导入时去掉卡里「【输出格式…】」开头的整段（`scenarios._without_format_rules`），这类卡（同一作者批量生成）要求每轮写 `<场景>/<状态>/<选项>`，台词里还会冒出「不替你编」这种规则原话。卡的开场白不再原样发：去掉标记和 HTML 后作为参考交给模型（`_CHARACTER_OPENING`），由她用聊天口吻说第一句，顺便去掉「成人向边界」这类字眼；回复发出前用 `_as_chat` 再清一遍标记和剧本格式，剧情记录里存的也是清过的，模型看到的历史保持聊天风。自带的跑团本仍用主持人旁白，规则里加了一条「别在旁白和台词里提规则、选项和编号」。
  - 成人模式（2026-10-28）：`tavern.adult_user_ids`（默认空）里的人**在私聊里**开局时，菜单里多出 `data/tavern/adult/` 的卡（标「（成人）」，id 以 `adult/` 开头），这一局（`TavernSession.adult`）的内容规则换成 `_ADULT_CONTENT`：亲密情节按卡的设定写、不一笔带过，角色都是成年人、双方自愿、对方犹豫/拒绝/说停就停，不写未成年人、非自愿、真实人物。**群里永远不开**：群菜单不列成人区，群里的局永远用 `_SAFE_CONTENT`（一笔带过），成人区的卡在群里选不了也开不了。owner 要过群里也开，没有做：群成员没有都同意、年龄无法确认，群里传播色情内容在国内有法律风险，QQ 也会封号。
  - 酒馆单独用模型（2026-10-28，owner：「现在的grok不对劲，这个ai就专门接酒馆」）：`tavern.llm_base_url`、`llm_api_key`、`llm_model` 都填了，主持、角色卡和前情提要都走这个 OpenAI 兼容接口（`TavernMode._complete`，`OpenAILLMClient`），否则沿用 `llm`。回复里的 `<think>…</think>` 整段去掉。线上接的是 deepseektavern.com（new-api 网关，`https://deepseektavern.com/v1`）的 `deepseek-tavern-v2-pro`；它还列了 `-low`、`-xhigh` 和 `tavern-abliterated-low` 等专做酒馆、限制更少的模型，所以加了输出检查：不是成人局（`TavernSession.adult` 为假，群里永远为假）时，回复里出现 `_EXPLICIT` 里的词就带着「刚才那段写得太露骨了…重写成一笔带过」再要一次，还是有就发「（这一段跳过。）」。dry_run：七濑莉央用聊天风开场「喂，别站那么远嘛，线索又不会长腿跑了……」，每轮 5.6–10.7 秒（grok 是 8–20 秒）；玩家消息里的「【资本狗】」被她当成骂人的话接了，规则里补了一条「【】里是说话人的名字」。
  - 存储（`tavern/session.py`）：进行中的局在 `data/tavern/sessions/<group|person>_<id>.json`，每次变动原子写入；重启后这个聊天来第一条消息时读回并继续（有没答的行动就接着开一轮）。读回时已经超时的局直接归档，不在群里补发通知。结束后移到 `data/tavern/archive/<聊天>-<开局时间>.json`（只开了菜单没开本的不留）。剧情不写进角色的会话历史、知识和好感度。
  - 超时：菜单 `menu_timeout_seconds`（300 秒），开局后 `idle_timeout_seconds`（1800 秒）没人说话就自动结束并说一句；正在写的一轮写完才结束。设成 0 不自动结束。
  - 配置 `tavern`（`config.py::TavernConfig`）：`enabled`（默认开）、上面几个时间、`max_players`（6）、`history_chars`、`starter_user_ids`（空表示谁都能开）、`admin_user_ids`。`settings_admin/config_manager.py::_load_dataclass` 现在也读字符串列表。
  - 主持人规则：不替玩家做决定、说话或掷骰；点到每个人；可以要求掷骰但不编结果；掷骰失败只写角色感知到的；玩家没说过性别就用名字称呼；80–250 字；结尾问接下来做什么；不写露骨性内容和血腥细节；主持要点里的秘密不直接说。
  - `reply_flow/service.py` 里酒馆这一步出异常时记 ERROR 日志，照常交给角色回复，不会让整个聊天没回应。
  - 测试 `tests/test_tavern.py`（21 个）。dry_run（grok-4.7 low，雾港酒馆，两个玩家，跑了两遍）：每轮 10–22 秒（等 2 秒加模型 8–20 秒）；主持人每轮点到两个人，会主动要求掷骰；固定掷出 3 时写成「听不清到底几个人」，没点破真相；加了性别那条规则后不再用「他」称呼玩家；苦力怕「退出酒馆模式」被拒，房主结束成功。
  - 没做：存档和读档命令、主持人自己用工具（掷 NPC 的骰子、给场景生图）、控制台页面；开局期间局外人在群里得不到角色的回复（只能私聊）；`max_players` 满了只拒绝，不排队。
- 听完再说（`feat/listen-first`，`reply_flow/listener.py`，配置 `listen`，默认开；`listen.enabled=false` 回到原来的做法）：
  - 等人说完：每条消息在拿会话锁之前由 `Listener.note_inbound` 记下到达时间（`/` 开头、`ignore_user_ids` 的不记）。要回的消息在存进历史、处理完换卡命令之后，由 `await_turn` 等这个人安静 `quiet_seconds`（2 秒），从第一条没回的算起最多等 `max_wait_seconds`（8 秒）。这期间这个人又发了消息，这条就不单独回：它那句已经在历史里，图片交给新的那条（`with_earlier_images`，最多 4 张），由最新那条一起回。`/skill`、`生图`、换卡命令不等。群回复额度（`acquire_group_reply_slot`）挪到等完之后，被合并的消息不占额度。
  - 群里谁算在跟她说话：@ 她；不 @ 但说了卡上的名字（`assistant_name`）或卡 meta 的 `call_names`（不用别名，「姐姐」「小鬼」太常见）；她刚回过的人在 `continuation_seconds`（60 秒）内说的话；前一条被合并、正在连发的人。原来按群算、谁都能接的跟进窗口（`group_follow_up_window_seconds`）只在 `listen.enabled=false` 时用；待确认搜索的接话照旧。
  - 收敛（2026-10-28，owner：「插嘴插得太勤了，而且一次就是三四句」）：线上看到 owner 说「你别说话了」她照样回了一段；她回过谁，那人之后每句都接，且每次回复都续期；插话两三句被拆成两三条加表情包。改为：只有 @、名字或 `call_names` 开始一段对话，持续 `continuation_seconds`（30 秒，原 60），回这段对话里的话**不续期**（`mark_answered(reason=...)`），插话后对方接话只算 20 秒；对方这时 @ 别人的话不算。「别说话」「闭嘴」「安静点」「别插嘴」等（`_QUIET`）对她说时不回，并在这个群安静 `quiet_minutes`（30 分钟）：不插话、不接没 @ 的话，@ 和名字照常。没 @ 的接话带 `[接着刚才的话]`（`TALK_ON_HINT`，也可以只回「[不说话]」）；接话和插话都截成一句（`one_sentence`：第一句，40 字封顶，去掉开头的「名字，」），不带表情包、不追加称呼追问。插话默认值改为门槛 0.45、桶 1、每小时 3 次、间隔 600 秒。
  - 主动插话：群里没人叫她的消息（不含 `ignore_user_ids`、命令、只有图的）交给 `consider_chiming_in`。群里安静 `group_quiet_seconds`（4 秒）后，看她上次说话以来的环境缓冲（最多 6 行，去掉「名字: 」）：不同的字少于 4 个（笑声、「666」）直接跳过；打分取最大值——卡 meta `interests` 里 8 个字以内的词原样出现算 0.6，和每条兴趣的嵌入相似度（没有 `interests` 就用人设和擅长的句子），和她 10 分钟内说过的上一句的相似度。到了 `chime_in_threshold`（0.40），群的插话桶有余量（`chime_in_burst` 2、`chime_in_per_hour` 6），离她上次在这个群说话超过 `chime_in_min_gap_seconds`（180 秒），没有回复正在写、没开酒馆，才走一次回复链路（`WaifuService._process_chime_in` → `handle_event_async_locked(spontaneous=True)`）。门槛、间隔和桶可以按卡在 meta 的 `chime_in` 里改（`threshold`、`min_gap_seconds`、`burst`、`per_hour`，`Listener.limits()`），桶按角色和群分开。这次 system 末尾多一段 `[这次是你自己想插话]`（`CHIME_IN_HINT`），模型可以只回「[不说话]」（`is_silence` 认几种写法），那样什么都不发；不追加新成员的称呼追问，不执行命令。分数写在 INFO 日志里（`chime-in …` / `no chime-in …`），方便按真实聊天调门槛。
  - 嵌入按 `embedding` 配置单独建一个客户端，兴趣和她说过的话的向量缓存在内存里；没配嵌入时只看关键词。没缓存的兴趣 8 个一起算（`_embed_all`；上线后第一次判断一个个算 39 个，用了约 12 秒）。
  - 正在跟她聊的人（`addresses_bot`：叫她名字、`call_names`、她刚回过的人）和 @ 一样用每人一个的桶（`mention_reply_*`），不占群的共用桶（2026-10-28 补：线上苦力怕跟她聊得热闹时被群桶连着挡掉 5 条）。主动插话仍走群桶。
  - 实测（text-embedding-v4，三张线上卡，都没有 `interests`，用的是人设句子）：「加班到十点好累」0.48–0.49、「被领导骂了」0.47–0.50、「谁来陪我聊聊天」0.57、「群里好安静啊」0.61、接着她上一句说的 0.51–0.54；猫、球赛、天气、吃什么、调 bug 0.30–0.37；「我失恋了……」0.42–0.43。dry_run（grok-4.7 low，极光，把间隔和桶放开、5 个人各说一句）：好累、群里安静、失恋她都插了话且接得上前文，球赛没调模型；私聊「我跟你说个事」「今天面试过了！」只回了一次（「哈？居然过了？」）。这几次模型都没有选「[不说话]」，所以加了「不同的字少于 4 个不判断」。dry_run 期间连 api.x.ai 有两次 ConnectError，和本改动无关。
  - 代价：每次回复至少多等 2 秒；群里有人 @ 她时，同群其他消息要等这 2 秒加生成。全量测试从约 91 秒变成约 115 秒（带消息 id 的 @ 消息都会等）。
  - 测试 `tests/test_listen.py`（14 个）；测跟进窗口的旧测试（`test_group_reply_guard.py`、`test_reply_latency.py` 的 BacklogTests、`test_app.py` 的 3 个）显式用 `listen.enabled=false`。
  - 没做：生成时对方又补了一句就作废重写；QQ 引用回复（`web/http_api.py::_parse_segments` 不解析 `reply` 段：引用她的消息又不 @ 的人，只在她刚回过对方时才算在跟她说话）；每张卡话多话少不同（小葵的人设写着爱起哄）；插话的分数和原因还没进控制台。

## 2. 当前工作区状态

- 2026-10-27 已合入 main：PR #27（下面七组改动）、PR #28（第 2.1 节）、PR #29（`codex/runtime-v2-private-flow` 拆成的三个提交：审计修复、新版私聊链路、文档，见 `docs/AUDIT_FIXES_2026-10-26.md`、`docs/RUNTIME_V2_FIRST_SLICE.md`、`docs/ARCHITECTURE_RESET_PLAN.md`）。
- `fix/deploy-hardening`：群回复限速和忽略名单、空环境变量不覆盖配置、固定 NapCat 版本、本文档，见第 3 节。
- `.env`（NapCat 令牌、账号）和 `output/`（截图和 PR 草稿）已在 `.gitignore` 里。

改动分七组（已按这个顺序提交，下面留作每组改了什么的记录）：

1. **称呼工具更严格**（本轮之前就有的未提交改动）
   - `reply_flow/prompt_assembler.py`：`_TOOL_CALLING_SYSTEM_PROMPT` 抽成常量，新增「保存类请求必须先调工具」。
   - `runtime/container.py`：四个称呼工具的 description 重写。
   - `tests/test_skills.py`：`test_naming_tools_expose_strict_persistence_instructions`。
2. **高风险工具授权闸门**
   - `config.py`：`ToolPolicyConfig.exec_enabled`、`write_enabled` 默认改为 `False`；新增 `authorized_user_ids: list[str]`，默认空，空表示任何人都不可用。
   - `settings_admin/config_manager.py`：`_load_tool_policy` 读取 `authorized_user_ids`；环境变量 `OPENQQWAIFU_TOOL_POLICY_AUTHORIZED_USER_IDS`。
   - `skills/tool_orchestrator.py`：`_allowed_tool_ids` 对未授权发送者去掉 `risk_level` 为 `filesystem`、`command`、`network` 的工具；`_is_tool_authorized` 按 `event.sender_id` 判断。
   - `skills/tool_registry.py`：新增风险等级 `network`；`ToolInvocation.allowed_tool_ids`；`ToolRegistry._execution_blocked` 在 `execute_model`、`aexecute_model` 里强校验，模型即使直接给出工具名，不在允许集合里也会被拒。
   - `skills/registry.py`：技能清单的 `risk_level` 接受 `network`。
   - `runtime/container.py`：`web-fetch` 的风险等级由 `safe` 改为 `network`。
   - `reply_flow/prompt_assembler.py`：两个工具循环创建 `ToolInvocation` 时带上 `tool_context.allowed_tool_ids`。
   - `tests/test_skills.py`：新增 `test_high_risk_tools_require_authorized_user`；另有 4 个依赖旧默认值的测试改成显式开启。
3. **长期记忆检索**
   - `repositories/runtime_state.py`：
     - `_EmbeddingBreaker`：嵌入失败写日志；连续 3 次失败后暂停 300 秒；`set_embedder` 会清除暂停。
     - SQLite 存储写知识时，嵌入在拿写锁之前计算（`_prepare_knowledge_embedding`）。
     - `_extract_terms` 支持中文：汉字连续段切成相邻二字词，并过滤 `_STOP_BIGRAMS`。
     - `_score_knowledge` 改为 0 到 4 的相关度，没有词重合返回 0。
     - 语义相似度低于 `_SEMANTIC_RELEVANCE_MIN = 0.40` 不计分；两个存储的召回都跳过分数不大于 0 的条目，置信度不再单独让条目入选。
   - `runtime/service.py`：`WaifuService._extract_terms` 直接复用上面的分词。
   - `memory/knowledge_service.py`：
     - `_session_knowledge_entries` 需要有词重合才返回。
     - `_is_near_duplicate`：一条是另一条中间多出几个字（长度比不低于 0.7、至少 4 个字、多出的字里没有否定字）就视为同一事实；`_existing_knowledge_entry` 先找完全相同的，再找近似重复；合并时保留更长的表述。
     - `scope_hint == "global"` 不再写成全局条目，落到所在聊天的作用域。
   - `reply_flow/service.py`：`_restates_memory` 去掉图谱高亮里已经出现在 `[Memories]` 的「相关记忆：…」。
   - `tests/test_state_store.py`：新增熔断、中文召回、无关不召回、写入不占写锁等测试；`FakeEmbedder` 的映射键现在按空白归一化，旧的键因含换行从未命中过。
   - `tests/test_memory.py`：新增 `KnowledgeWritebackTests`。
4. **测试隔离**
   - `tests/test_app.py`：两个会保存角色卡的测试（`test_active_character_card_identity_wins_for_person_session`、`test_save_character_panel_can_edit_without_switching_active_character`）改用临时目录；前者的 `set_active_character` 换成 `service.activate_character`。
   - 规则：凡是会保存角色卡、切换角色、写会话的测试，必须用 `tempfile.TemporaryDirectory()` 加 `AppConfig(data_root=...)`。`build_default_service(AppConfig())` 不带 `data_root` 时，角色卡读写落在当前目录的 `data/cards/`。
5. **短期窗口（第 4 节 A 块）**
   - `config.py`、`settings_admin/config_manager.py`、`settings_admin/service.py`、`web/console_panels.py`、`web/assets/js/i18n.js`、`web/assets/js/pages/abilities.js`：`history_window_messages` 默认 8 改 30；新增 `history_window_chars`（默认 2500，0 表示不限），控制台可改。
   - `models.py`：`to_memory_text(bot_account_id=...)` 去掉机器人自己的 @，别人的 @ 保留。
   - `runtime/service.py`：`_ambient_history` 内存环形缓冲（`_remember_ambient_event`、`_peek_ambient_history`、`_clear_ambient_history`）；`_preview_conversation_view` 复用同一套窗口裁剪。
   - `memory/session_service.py`：`save_user_event` 接收 `context_lines`、`bot_account_id`；`format_dialogue` 新增 `max_chars`、`exclude_last`；单行上限 `_DIALOGUE_LINE_MAX_CHARS = 400`。
   - `reply_flow/service.py`：缓冲并入历史、窗口去掉当前消息、群聊机器人标签固定为角色卡名字。
   - `reply_flow/prompt_assembler.py`：知识抽取只看最近 `_KNOWLEDGE_CONTEXT_LINES = 8` 行。
   - 测试：`tests/test_app.py` 5 个、`tests/test_organs.py` 2 个。
6. **当前时间、滚动摘要、提示词质量（第 4 节 B 块）**
   - `infra/clock.py`（新文件）：`resolve_timezone`、`local_now`、`format_prompt_time`、`absolutize_relative_dates`。
   - `config.py`、`settings_admin/config_manager.py`：新增 `timezone`（默认 `Asia/Shanghai`，环境变量 `OPENQQWAIFU_TIMEZONE`）；`max_thinking_words` 默认 30 改 80（`abilities.js` 同步）。
   - `character/cards.py`：`system_prompt` 新增 `current_time`、`earlier_summary`。
   - `memory/session_service.py`、`memory/__init__.py`：`ROLLING_SUMMARY_KEY`、`clip_rolling_summary`；`maybe_archive_history` 改为整批归档并写入 `metadata["rolling_summary"]`，新增 `commit_lock`；删除不再使用的 `amaybe_archive_history`。
   - `runtime/service.py`、`reply_flow/emitter.py`：`_archive_if_needed[_async]` 换成 `_schedule_archive` + `_archive_history`（后台）；新增 `_session_lock_for_key`、`_archive_inflight`。
   - `reply_flow/prompt_assembler.py`：`_prompt_time`、`summarize_rolling`、`_build_rolling_summary_query`、`_clip_at_sentence`；对话、分析、称呼引导、工具渲染提示词带当前时间；抽取提示词带今天日期。
   - `repositories/runtime_state.py`：`excluded_from_recall`；`memory/knowledge_service.py`：存知识前改写相对日期，线上关掉正则回退；抽取结果里的 `memory_type="summary"` 按 `fact` 存（`_normalize_knowledge_item`）。
   - `reply_flow/service.py`：交给 emitter 的名字在群聊里用卡名。
   - 测试：新文件 `tests/test_clock.py`；`tests/test_app.py`（`test_summarization_mode_archives_older_history` 改写为 `test_summarization_mode_folds_overflow_into_rolling_summary`，另加 3 个）、`tests/test_organs.py` 2 个、`tests/test_memory.py` 2 个、`tests/test_state_store.py` 2 个。

7. **技能触发与回复渲染（第 4 节 C 块）**
   - `config.py`、`settings_admin/config_manager.py`：新增 `explicit_skill_prefixes`（默认 `["/skill"]`）、`outbound_message_max_chars`（默认 900，0 不切分）。
   - `reply_flow/service.py`：`/skill` 命令越过 `ignore_prefixes`；`生图` 前缀直接走生图技能；`background_image_delivery` 用形参。
   - `skills/dispatcher.py`：`resolve_explicit_skill_request` 按配置前缀匹配；新增 `resolve_image_command`、`_fill_single_required_argument`；`_build_tool_invocation` 带上授权闸门的 `allowed_tool_ids`；`extract_image_prompt` 对英文前缀要求词边界；`summarize` 本地文件只对授权用户开放；`_compose_tool_display_text` 去重。
   - `skills/registry.py`、`skills/tool_orchestrator.py`：`exposed_manifests` 固定暴露搜索、天气、总结、生图。
   - `reply_flow/prompt_assembler.py`：工具循环按暴露的技能查清单（`_merge_skills`）；`_clean_response` 保留换行；工具渲染提示词用 `_clip_multiline`；`_append_raw_tool_blocks` 去重。
   - `reply_flow/emitter.py`：`split_reply_text`、`message_parts`，长回复分多条发。
   - `runtime/service.py`：有结果的 `last_search` 跨轮保留。
   - `infra/safe_fetch.py`（新文件）：`check_public_url`、`fetch_public_url`、`afetch_public_url`；`skills/dispatcher.py` 的 web-fetch、summarize 内置抓取改用它，summarize CLI 调用前先 `check_public_url`。测试 `tests/test_safe_fetch.py`。
   - 测试：`tests/test_skills.py` 把 `test_slash_skill_command_no_longer_bypasses_tool_orchestrator`、`test_ignored_slash_summarize_does_not_use_legacy_dispatch` 换成 `test_slash_skill_command_runs_with_default_ignore_prefixes`、`test_other_slash_messages_are_still_ignored`、`test_slash_skill_command_respects_tool_authorization`，`test_image_request_keeps_skill_match` 改断言，另加 3 个；`tests/test_app.py` 加 5 个。

### 2.1 阶段 0 的 P0 修复（PR #28，2026-10-26，已合入）

`REFACTOR_ROADMAP.md` 第 1 节第 4 步的剩余项。URL 抓取拦截在 PR #27 里已经做完，这里没再动。每项一个提交，都有测试；全量 362 个测试通过。2026-10-27 实机验证过 `dry_run` 和私聊、群聊的基本收发（第 3 节）。

1. **`dry_run` 生效**（`c28c520`）
   - `runtime/container.py::_build_runtime_outbound`：`qq_sidecar.dry_run` 为真时返回 `CapturingOutboundPort`，配了 `outbound_base_url` 也不发；`reverse_ws` 模式保留网关，NapCat 照常连进来推事件，回复只捕获。控制台改 sidecar 配置后重建出站时同样生效。
   - `settings_admin/config_manager.py`：环境变量 `OPENQQWAIFU_QQ_SIDECAR_DRY_RUN` 以前读了但结果被丢掉，现在会写回配置。
   - `runtime/service.py`：`_requires_live_llm()` 在 `dry_run` 时为真，彩排走线上路径（模型没配好就回「AI 工具编排模型还没连上」，知识抽取不走正则回退、生图前缀要求 `image_ready`）；`_outbound_mode_label` 显示 `dry-run (capture)`。`serve` 的启动日志是 `outbound=capture(dry-run)`（已实测）。
   - CLAUDE.md、AGENTS.md 和第 6 节的说法已改。
   - 测试：`test_runtime_service_dry_run_captures_even_with_sidecar_url`、`test_runtime_service_dry_run_keeps_reverse_ws_gateway_for_inbound`（`tests/test_app.py`）、`test_env_override_turns_on_dry_run`（`tests/test_config_manager.py`）。
2. **启用角色不再清空会话**（`077a26a`）
   - `character/service.py`：`activate_character` 去掉 `reset_sessions` 参数，只切换当前角色；启用的就是已绑定的角色时不重建会话存储和 SQLite 连接。`runtime/service.py` 的 `_rebind_character_scoped_storage`、`_reset_character_runtime_context` 也不再带清空开关（仍会清跟进窗口、环境缓冲这些内存状态）。
   - 清空会话改成单独的操作：`CharacterService.reset_character_sessions` / `POST /api/character/reset-sessions`，`{"character": X, "confirm": X}`，`confirm` 不等于角色代号、或角色不在 `cards.list_characters()` 里都返回 400（后者也挡住 `..` 这类代号去删别的目录）。只删这个角色的会话文件（含滚动摘要），知识库、成员、好感度不动。仅管理员可用。
   - 控制台：人物卡页立绘卡片的按钮行多了「重置会话…」，弹窗说明删什么，要求输入角色代号；输错不发请求。已在浏览器里实测：输错被拒；「保存并启用」同一角色后会话文件还在；输对后会话文件被删、接口 200。
   - 没做：重置时没有拿会话锁，正在生成的回复写回时会把那一个会话再存回来。
   - 测试：`test_save_character_panel_switch_keeps_character_sessions`（原来的 `..._switch_resets_character_sessions` 反过来了）、`test_save_and_activate_same_character_keeps_sessions_and_storage`、`test_reset_character_sessions_requires_matching_confirmation`（`tests/test_app.py`）、`test_character_reset_sessions_endpoint_requires_confirmation`（`tests/test_server_integration.py`）。
3. **入站消息去重**（`f76041e`）
   - 新文件 `gateways/inbound_dedup.py::InboundDeduplicator`；`web/http_api.py::HttpApi` 持有一个实例，`handle_json` 和 `handle_json_async` 在解析出事件后先 `claim`。HTTP 回调和反向 WS（`_handle_reverse_ws_payload` → `handle_json_async`）都经过这里。
   - 有 `message_id`：按（会话类型、会话 ID、`message_id`）记 10 分钟。没有：按（会话、发送者、内容哈希）记 10 秒；载荷里有 OneBot 的 `time` 时把它并进键并记 10 分钟（重试带原来的 `time`，同一句话再发一次 `time` 不同）。最多 4096 条，超出丢最旧的。参数是模块常量，没进配置。
   - 重复的返回 `ignored` / `duplicate message`（HTTP 回调照常回 204），onebot 事件指标里的 outcome 是 `duplicate`。处理抛异常时释放这条记录，之后的重试还能处理。
   - 没做：只在内存里，重启就忘；直接调用 `service.handle_event` 的路径（CLI、测试、控制台预览）不去重。
   - 测试：新文件 `tests/test_inbound_dedup.py`；`test_replayed_event_frame_is_handled_once`（`tests/test_onebot_ws.py`，真实 WS 连接）、`test_http_callback_retry_does_not_reply_twice`（`tests/test_server_integration.py`，真实 HTTP，出站只发一次）。这两个端到端测试在去掉去重后都会失败（已验证）。
4. **付费和外部工具限流**（`964d18a`）
   - 新文件 `infra/rate_limit.py::TokenBucketLimiter`：一次调用要从发送者的桶和（群聊时）群的桶各拿一个令牌，两个都有才扣，否则都不扣并返回要等多久。桶在内存里，初始是满的，最多 1 万个。
   - `config.py::RateLimitConfig`（`AppConfig.rate_limits`），`ConfigManager.load()` 的 `_load_rate_limits` 读取；负数按 0，非数字用默认值。环境变量 `OPENQQWAIFU_RATE_LIMITS_ENABLED`、`OPENQQWAIFU_RATE_LIMITS_EXEMPT_USER_IDS`。默认值：
     - `image`（生图）：每人 3 次连发、每小时回填 10 次；每群 6 次、每小时 30 次。
     - `network`（搜索、web-fetch、summarize、带新查询的 search-links，共用一个桶）：每人 8 次、每小时 60 次；每群 16 次、每小时 180 次。
     - `*_burst` 为 0 关掉那一层；`exempt_user_ids` 里的人不受限；`enabled=false` 全关。
   - `skills/dispatcher.py`：检查放在各个真正执行的函数里（`use_*`/`ause_*` 模型工具、`handle_image_request`/`handle_search_request` 及异步版、summarize 的离线路径），所以模型工具循环、`/skill`、`生图` 前缀、旧关键词分发都覆盖；放在参数校验之后，空请求不扣令牌。被限时回复「X，生图刚才用得有点多，30 分钟后再来吧。」；`/skill` 和 `生图` 直发时这句话原样发出，不再调一次模型来改写；模型工具循环里模型看到的是带 `rate_limited` 的错误结果。
   - 不限：会话总结（`summary`）、天气、生图交付文案、普通聊天的模型调用；控制台的立绘生成。控制台没有改这些参数的界面，只能改配置文件或环境变量。重启后桶回到满的。
   - 测试：新文件 `tests/test_rate_limit.py`（令牌桶、按人、按群、三种网络工具共用一个桶、豁免和关闭、配置读取与环境变量）。
5. **仓库卫生**（`ce48bd2`）
   - `.gitignore` 加上 `.env`、`.env.*`（保留 `!.env.example`，它是被跟踪的）和 `output/`。测试 `tests/test_repo_hygiene.py` 用 `git check-ignore --no-index` 检查，没有 git 或不在工作树里时跳过。
   - 没做：`data/skills/` 下两个旧格式技能文件（路线图里也列在这一步）按约定留给 owner。

要 owner 决定的：

- 限流默认值合不合适，要不要把自己的 QQ 号放进 `rate_limits.exempt_user_ids`。线上的 `data/docker-compose-config.json` 没有 `rate_limits` 段，合入后直接按默认值生效。
- `dry_run` 时 `_requires_live_llm()` 为真（按线上行为彩排）。如果更想要「dry_run = 离线模式」，改回去只需要删掉 `runtime/service.py::_requires_live_llm` 里的 `dry_run` 判断，并改掉 `test_runtime_service_dry_run_captures_even_with_sidecar_url` 的断言。
- 去重的时间窗（10 分钟 / 10 秒）是常量；要调的话再挪进配置。
- 顺带发现、没有修：`settings_admin/config_manager.py::ConfigManager.load()` 里 `assistant_name` 的默认值 `"鐞夌拑"`、`image_command_prefix` 和 `image_command_aliases` 的默认值 `"鐢熷浘"` 是乱码（UTF-8 被当成 GBK 读过一次，原文是「琉璃」「生图」）；`config.py` 里的默认值是对的。配置文件里没写这几项时，生图前缀会变成乱码。线上的 `data/docker-compose-config.json` 三项都写了，目前不受影响。

## 3. 部署、运行数据与配置（`data/`、`napcat/` 被 gitignore，不在 git 里）

### 3.1 部署现状（2026-10-27）

- 本机 Docker Compose（`compose.napcat.yml`）：容器 `openqqwaifu`（8080）和 `napcat`（3000/3001/6099），真实发送，`qq_sidecar.dry_run=false`。2026-10-27 起聊天模型是 `grok-4.7`（xAI，每百万 token 输入 2 美元、输出 6 美元，会先思考再回答，dry_run 实测平均 15 秒、最长 34 秒），`thinking_mode=false`。机器人 QQ `3956638110`（昵称「幸福的奴隶Bot」），好友 2 个，群 1 个（`612475113`）。
- `data/docker-compose-config.json` 当天的改动：`qq_sidecar.gateway_mode=reverse_ws`；`qq_sidecar.access_token` 设成随机令牌，反向 WS 和 NapCat 的 HTTP 动作端口 3000 都用它（3000 映射在宿主机上，原来没有令牌）。HTTP 回调已关闭，所以不需要 `inbound_access_token`。改之前的配置、NapCat 配置和 QQ 数据备份在 `data/backups/before-deploy-20261027-023259/`。
- 只重建 openqqwaifu：`docker compose -f compose.napcat.yml up -d --build --no-deps waifu`。一定要加 `--no-deps`：不加时 compose 会顺带检查 `depends_on` 里的 napcat，它的配置和正在跑的容器对不上就重建它。2026-10-28 部署酒馆模式时用了不带 `--no-deps` 的写法，NapCat 被重建，快速登录报「你的用户身份已失效」，只能重新扫码。改了 `napcat` 服务的定义（例如镜像标签）再 `up`，NapCat 同样会被重建。
- NapCat 要扫码时：`docker cp napcat:/app/napcat/cache/qrcode.png .` 取出二维码，用机器人账号的手机 QQ 扫；二维码约 2 分钟刷新一次，日志里每次都有「二维码已保存」，过期时会先报 `Login Error ... ErrCode: 3`。登录成功的标志是「协议适配器初始化完成」。
- compose 给可选设置传 `${VAR:-}`，没设置时是空字符串。以前 `_env_str` 会用这个空串覆盖配置文件里的令牌；现在空串按未设置处理（`fix/deploy-hardening`）。

### 3.1.1 测试改写线上数据（2026-10-28 发现并修复）

- 一直以来，从仓库根目录跑测试时，凡是加载或保存配置文件的测试（`test_config_manager`、`test_runtime_v2*` 等）都会经 `_napcat_config_dirs` 的「当前目录/napcat/config」候选，用测试配置（http 模式、空令牌）改写**线上** NapCat 的 OneBot 配置：反向 WS 被删，3000 端口令牌变空，多一条 127.0.0.1 的 http 回调。NapCat 不热加载这个文件，而每次部署重启 openqqwaifu 时会按线上配置整理回来，所以一直没出事；如果 NapCat 恰好在两者之间重启，机器人会收不到消息、3000 端口无令牌暴露在宿主机上。2026-10-28 05:34 发现时，NapCat 仍在用正确的内存配置，随即在容器里用线上配置 load 一次写回（令牌、反向 WS 都核对过）。修复：有配置文件路径时 `_napcat_config_dirs` 不再看当前目录，相对的 `data_root` 按配置文件所在目录解析（`test_a_config_elsewhere_leaves_the_working_directorys_napcat_alone`，在旧代码上会失败）。
- 同一次全量测试里，用默认 `AppConfig()`（`data_root="data"`）的测试读到了 owner 正在私聊里玩的酒馆局（`data/tavern/sessions/person_783190298.json`），把测试消息当成行动写了进去（测试只捕获出站，没有发到 QQ）。已从备份恢复到第 1 轮之后。修复：`tests/test__isolated_working_directory.py` 让测试在空的临时工作目录里跑（`discover` 时它的名字排第一、最先导入；单独跑模块时由 `tests/__init__.py` 导入），相对的 `data` 不再指向线上数据。

### 3.2 NapCat 登录

- 镜像固定为 `mlikiowa/napcat-docker:v4.18.41`（NapCat 4.18.41，QQ 3.2.34-53644）。升级要有意为之，升级后准备好扫码。
- 2026-04 的旧镜像（QQ 3.2.25）加旧 QQ 数据目录：能登录，状态 online/good，好友、资料查询都正常，但收不到任何消息，最近会话为空，查聊天记录超时。换新镜像后仍然一样；把数据目录换成空的、重新扫码后恢复。旧数据目录在 `napcat/qq.old-20261027-031156`，稳定后可删。
- 重启 NapCat 后快速登录可能失败（「登录态已失效」），这时要重新扫码。二维码取法：`docker cp napcat:/app/napcat/cache/qrcode.png .`；或用 NapCat 日志里的「二维码解码URL」；或控制台的 QQ 登录页。二维码约 2 分钟过期，会自动刷新。要让重启后自动登录，只能在 `.env` 里配 `NAPCAT_QUICK_PASSWORD`（机器人账号的密码，由 owner 自己填）。
- 反向 WS 断开期间（NapCat 重启、登录过程中）到达的消息不会补推，直接丢失。

### 3.3 群聊防刷屏（`fix/deploy-hardening`）

- 2026-10-27 15:29，群里另一个机器人「大肥鱼」（`3246300953`）和本机器人互相接话：owner @ 一次之后，跟进窗口让机器人接所有人的话，每次回复又把窗口延长，70 秒内发了 6 条，其中两条和前一条一字不差。当时靠会话外的脚本停掉了容器。
- 现在有两道闸门，都在 `reply_flow/reply_gate.py`：
  - `ignore_user_ids`（顶层配置，环境变量 `OPENQQWAIFU_IGNORE_USER_IDS`，逗号分隔）：这些 QQ 号的消息永远不触发回复，私聊也一样，也不能用 `/skill`；消息仍作为群聊上下文进环境缓冲。
  - `rate_limits.group_reply_burst` / `group_reply_per_hour`（默认 8 和 60）：每个群一个令牌桶，每次回复扣一个，用完就不回，只记一条 WARNING 日志，不在群里说明。`rate_limits.exempt_user_ids` 里的人不受限，`rate_limits.enabled=false` 全关。桶在内存里，重启后是满的。
  - 直接 @ 机器人的消息不走上面的群桶，走每个群里每人一个的桶（`mention_reply_burst` / `mention_reply_per_hour`，默认 8 和 120；burst 为 0 时回到群桶）。切换人物卡的命令不调模型，不占任何桶。WARNING 日志里的 `bucket=` 说明是哪个桶。
- 控制台「其他设置」接口（`/api/panels/other`）能读写 `ignore_user_ids`，页面上还没有输入框；限速参数只能改配置文件。

### 3.4 实机测试的发现（2026-10-27，模型 grok-4-fast-non-reasoning）

- 私聊和群 @ 都正常，一条回复 6–9 秒，约 3 次模型调用加 1 次嵌入。群里不 @ 的消息进环境缓冲，下次回复时用上了（回复里引用了前面群聊的话题）。
- 回复会照抄自己上一条的后半句，群里出现过两次连续相同的回复。原因是人设只在 user 消息里、聊天记录是一段文字、别名顶替了角色名，`feat/prompt-structure` 已改；改前改后和换 grok-4.7 的 dry_run 对比见该 PR。
- 用户说「给我讲个故事」时，回复「我就勉强讲一段」然后没有下文：角色卡 Rules 段还是测试夹具的「不超过三句话」。
- 只发 `/skill` 不会有任何回应（`/` 是忽略前缀，只有 `/skill <技能 ID> 参数` 才会执行）。问「你会什么」会触发技能列表。
- 群临时会话的消息（陌生人 `2747277822`）没有被处理，原因没查。
- 群里大肥鱼发的图，机器人有一次看懂了，有一次回了「图片处理失败，暂时无法读取这张图片，请稍后重试。」，原因见第 3.6 节。

### 3.5 运行数据

- `embedding.model` 由 `qwen3-vl-embedding` 改为 `text-embedding-v4`：前者在 DashScope 的 OpenAI 兼容 `/embeddings` 上不可用，每次都返回 404；后者已实测，1024 维。更换嵌入模型要重新校准 `_SEMANTIC_RELEVANCE_MIN`，`text-embedding-v4` 上相关句对约 0.42–0.64，无关句对约 0.19–0.37。
- `llm.reasoning_effort="low"`（2026-10-27 上线 `fix/reply-latency` 时加，原因见第 3.8 节）。
- `tool_policy`（2026-10-27 改）：`authorized_user_ids=["783190298"]`（owner）；`allowed_roots` 和 `write_allowed_roots` 都只有 `/app/data/files`（宿主机上的 `data/files/`），读写文件、列目录只能碰这个文件夹，原来是整个 `data/`（含带密钥的配置）；`write_enabled=true`，`exec_enabled=false`。抓网页（network）也随授权对 owner 打开。改前的配置备份在 `data/backups/before-file-access-20261027-083905/`。`history_window_messages=30`、`max_thinking_words=80`（2026-10-26 改）；没有 `history_window_chars`（缺省 2500）和 `timezone`（缺省 `Asia/Shanghai`）。
- `member_persona_state.profile_summary` 里原来的 4 条画像写的全是机器人自己的傲娇人设（「masks care with complaints」之类），2026-10-27 备份后清空；抽取提示词已不再带人设。
- 2026-10-25 清空过会话记录和知识条目，清空前的备份在系统临时目录，可能已被清理：`/private/tmp/claude-501/-Users-shinonome-openqqwaifu/bd3c61d7-dd79-4eaf-bd72-9fa0d7f1d365/scratchpad/data-backup-20261025-160341`。2026-10-27 的实测写入了新的会话（`person_783190298`、`group_612475113`）和知识。
- 角色卡 `data/cards/aurora_person.yaml`、`aurora_group.yaml`：Profile 段 2026-10-26 已换回 `aurora.meta.json` 里 `portrait_prompt` 的 8 行人设；Skills、Background、Rules、Prologue 仍是测试夹具内容（例如 Rules「不超过三句话」），要由 owner 决定怎么写。
- 人物卡（2026-10-27）：`data/cards/` 有三张卡，极光 `aurora`（傲娇小公主，当前在用）、小葵 `xiaokui`（元气雌小鬼，叫不认识的人「杂鱼」）、晚晴 `wanqing`（温柔大姐姐，叫不认识的人「小家伙」），每张都有私聊和群聊两份；`<卡>.meta.json` 里写了列表用的 `intro`、切换用的 `aliases` 和换上场时说的 `greetings`。加卡前的备份在 `data/backups/before-persona-cards-20261027-081816/`。
- 表情包库 `data/stickers/`（2026-10-27 导入，约 1.1 GB）：AstrBot Meme Manager「表情包广场」索引（`anka-afk/astrbot-meme-pack-index`）的 14 个条目，其中 `ddzs987-semantic-001` 和官方表情包1号是同一批图，实际 13 个包，共 2,359 张不重复的图。各包原来的 50 种英文分类合并成 42 个中文分类（开心、生气、害羞、贴贴、宕机、要钱、早安…），对照表、每个包的来源、维护者、许可和收录张数在 `data/stickers/README.md`。文件名以包 ID 开头，去掉某个包就删这个前缀的文件。超过 2 MB 的 424 张已缩小（动图 240 像素、静图 512 像素）后收录。许可大多是「仅限非商业使用」或 CC BY-NC-SA 4.0，几个包写的是「原素材许可未指定」，owner 决定全部用于本机器人的非商业聊天。下载用的是各包仓库按索引 `ref` 的浅克隆（只取 `memes/`），整理脚本没有进仓库。线上容器扫描一次目录约 35 毫秒（第一次 191 毫秒）。
- 人物卡的插话设置（2026-10-28，「听完再说」上线时）：三张卡的 meta 加了 `interests`、`call_names`（`["奴隶", "奴隶哥"]`，群友按 QQ 昵称这么叫她）和 `chime_in`。按人设拟的兴趣：极光 39 个（霓虹、夜景、像素小游戏、穿搭拍照、被夸、生病熬夜、受委屈、难过、斗嘴输赢…），门槛 0.40、间隔 120 秒、桶 2、每小时 8 次；小葵 38 个（甜食奶茶、游戏上分抽卡、输赢打赌、起哄八卦吃瓜、作业考试摸鱼、没人理…），0.36、60 秒、3、12 次；晚晴 39 个（热茶读书黄昏下雨、累、失眠、生病、难过、失恋、想家、没吃饭、面试升职、吵架…），0.38、90 秒、3、10 次。owner 要求「尽量多接上话而不是少接，根据每个人的性格来」。用 text-embedding-v4 量的 16 句样例：小葵过门槛 14 句，晚晴 13 句（心情类 0.55–0.70），极光 9 句，猫、球赛、调 bug 只有小葵会接。改之前的 meta 备份在 `data/backups/before-card-interests-20261028-044041/`。
- 酒馆角色卡（2026-10-28）：`data/tavern/scenarios/` 放了 owner 上传的两张 SillyTavern V3 卡（作者「磊哥哥」，原创系列）。七濑莉央：「星雾偶像事务所」，27 岁主唱，城市靠演出供电、低人气街区停电的冒险剧情，17 条世界书。顾绮：「午夜绒幕」，35 岁剧院投资人，作者标了成人向，暧昧恋爱剧情，10 条世界书。两张都标了 NSFW，正文没扫到露骨内容；dry_run 各玩一轮正常（莉央推进剧情，顾绮是雨夜后台谈判，都没有露骨描写）。owner 当时不在电脑旁，卡经私有仓库 `shinonome123/tavern-cards` 中转：手机浏览器打开 `https://github.com/shinonome123/tavern-cards/upload/main` 上传，再用 `gh api -H "Accept: application/vnd.github.raw" repos/shinonome123/tavern-cards/contents/<文件名>` 拉下来（文件名要 URL 编码；zsh 里 `?` 会被当通配符，带查询参数的地址要加引号）。聊天里贴的图片只有画面，读不到卡里的数据。
- 人物卡插话设置第二版（2026-10-28 晚，owner 嫌插嘴太勤）：极光门槛 0.46、间隔 600 秒、桶 1、每小时 3 次；小葵 0.42、300 秒、2、6 次；晚晴 0.44、420 秒、1、4 次。兴趣和 `call_names` 没动。改之前的 meta 备份在 `data/backups/before-calmer-chime-in-*/`。
- 酒馆模型（2026-10-28）：线上配置的 `tavern` 段加了 `llm_base_url`（deepseektavern.com）、`llm_api_key`（owner 在聊天里给的，只在这个文件里）、`llm_model=deepseek-tavern-v2-pro`。改之前的配置备份在 `data/backups/before-tavern-model-*/`。
- 线上 `summarization_mode` 是 `true`、`memory_summary_batch_size` 是 12：现在每多出 12 行才调一次摘要。`knowledge_entries` 里如果还有旧的 `memory_type="summary"` 条目，召回会跳过，不会自动删除。

### 3.6 看图的实测（2026-10-27）

- 直接调 xAI：grok-4.7 读 PNG、JPEG、WebP 正常（2–4 秒）；GIF 返回 400 `invalid_image`（「does not contain a valid JPG, PNG, WebP, or ICO image」）。把图片链接交给 xAI 自己下载也不可靠：维基百科的图片链接返回 `image_download_error`。原来的代码把 QQ 图床链接原样交给模型，所以两种情况都会失败。
- 在 openqqwaifu 容器里直接下载 QQ 图床（`multimedia.nt.qq.com.cn`）正常，域名解析到公网地址，`safe_fetch` 放行。群里大肥鱼最近发的 4 张都是 `sub_type=1`、`summary="[动画表情]"` 的表情，3 张 PNG、1 张 JPEG，几十到几百 KB。
- 现在的处理见第 1 节「看图」。dry_run 实测：GIF 测试图转成第一帧后 grok-4.7 能说出「红圆圈旁边一个蓝 42」；标成表情的图片，回复是「这算哪门子表情」。
- 同一次 dry_run 里，新用户的第一条「在吗」有一次没有任何回复（5 秒）；同样的消息重放 7 次都正常。当时这条路径的模型错误不写日志，原因查不到，现在会记 WARNING。

### 3.7 技能实测（2026-10-27，线上容器里 dry_run）

- 天气、搜索、搜索来源链接、`/skill weather 上海`、会话总结都正常。
- 群里 owner @ 机器人问天气没有回复：日志 `group reply rate-limited ... sender=783190298`，群额度在 19:28–19:39 被和大肥鱼斗嘴用光了，同一时刻排队的 7 条消息都被丢掉。
- 总结 `https://www.python.org/about/` 报「不能访问内网或本机地址」：容器里 www.python.org、github.com、wttr.in、duckduckgo 都解析到 198.18.x.x（宿主机代理的 fake-IP），bilibili、baidu 是真实地址。天气和搜索不经过 `safe_fetch`，所以不受影响。
- 「你会什么」回的是带 `keywords=`、`tool=` 的原始清单，还列出了没人能用的写文件、执行命令。
- 「看看 data 目录下有什么文件」没有调工具：`tool_policy.authorized_user_ids` 为空，文件工具对所有人关闭。

### 3.8 回复慢的原因（2026-10-27）

- 群里 20:58–21:01：机器人把每条消息都按顺序回，一条 15–25 秒，owner 20:58:30 的话 129 秒后才回，苦力怕的 149 秒后才回；群友说「现在还在回之前的」。单独 @ 且没人插队时 17 秒左右。
- 每条回复：1 次向量检索 + 1 次 grok-4.7（10–19 秒）+ 新成员时 1 次称呼追问 + 分段之间 1–2.5 秒的停顿；后台还有 1 次记忆抽取。
- 同一个真实请求（16 条消息、约 3,000 输入 token）的耗时：grok-4.7 默认 24–40 秒（思考 1,500–2,700 token），`reasoning_effort=low` 5–8 秒（约 300），grok-4.20-0309-non-reasoning 2–6 秒，grok-4.6 约 20 秒，grok-4.3 5–6 秒。
- 修好后的 dry_run（`low`）：单条 @ 5–6 秒；5 条没 @ 的接话同时进来，只回最新一条，8.6 秒，回复里接住了两个人的话；「切换晚晴」0.7 秒换好并打招呼。

## 4. 三块（A、B、C 已完成）

### A. 短期窗口（已完成，2026-10-27 实机验证过群聊环境缓冲）

原来的问题：群里没 @ 机器人的消息不入库；窗口 8 行且包含当前消息，`[Latest Message]` 又重复一次；历史行里带机器人自己的 `@号码`；机器人行的标签随当前说话人起的别名变化。

现在的做法：

- 群里没触发回复的消息先进内存环形缓冲（`WaifuService._ambient_history`，按 角色、会话类型、会话 ID 分，长度 `max(40, min(120, short_term_memory_limit * 2))`），机器人下次在这个群回复时一次性并入会话历史再保存。没有选「每条都存」，是为了避免每条群消息写一份整份会话快照。代价：服务重启会丢掉还没并入的缓冲；机器人发的消息、`ignore_prefixes` 开头的消息不进缓冲。缓冲的读、并、清都在同一把会话锁里（`_session_lock_for` 的键和缓冲键一致），没有并发丢行。
- `format_dialogue(..., exclude_last=True)` 去掉当前消息，`limit` 仍按「当前消息之前的行数」计。`max_chars` 从最新的行往前装，超出预算就停；每行先截到 `min(预算, 400)` 字，末尾加「…」。
- `to_memory_text(bot_account_id=...)` 只去掉机器人自己的 @；缓冲行、入库行、`[Latest Message]` 都用这个形式。
- 群聊里机器人行一律标成角色卡的 `assistant_name`；当前说话人起的别名仍作为「你是…」的身份（别名功能不变），另在 Speaker Notes 第一行加「X叫你「别名」；对话记录里「卡名」的发言就是你说的」。私聊只有一个人，标签和身份继续用别名，不加备注。知识抽取在群聊里也用卡名。
- 窗口变大后，知识抽取只取对话的最后 8 行，避免把群里其他人的话记到当前说话人头上。

测试：`test_group_ambient_messages_fold_into_next_reply`（3 条未触发 + 1 条 @，前 3 条进 `[Conversation]`，最新消息不重复，历史里没有机器人的 @）、`test_history_window_char_budget_drops_oldest_lines`、`test_to_memory_text_drops_only_bot_self_mention`、`test_group_dialogue_labels_bot_with_card_name_and_passes_alias_as_note`、`test_knowledge_query_only_sees_recent_conversation_lines`（`tests/test_app.py`）；`test_format_dialogue_exclude_last_keeps_full_window`、`test_format_dialogue_truncates_long_line_without_dropping_context`（`tests/test_organs.py`）。

没做的：（审计结论）快照回退（32 行变 31 行）的丢更新没有查。注意：缓冲并入后历史一次会多出很多行，会更快超过 30 行触发归档；归档的问题在 B 块。

### B. 当前时间、摘要、提示词质量（已完成，未在真实 QQ 环境验证）

原来的问题：提示词没有当前时间；每次回复只要超过 30 行就归档约 2 行、调一次摘要模型，摘要写成 `summary` 知识条目和事实抢召回名额，且摘要调用占着会话锁；内心想法被硬截到 30 字；抽取提示词不知道今天几号；模型说「没有值得记的」时线上会掉进正则回退去存东西。

现在的做法：

- 当前时间：`[State]` 第一行「当前时间：2026-10-26 周一 12:30（Asia/Shanghai）」。分析、称呼引导、工具结果渲染的提示词也带。时区来自 `config.timezone`；机器上没有时区库（精简容器、没装 tzdata 的 Windows）时按内置的固定偏移，未知名字按 UTC+8 并写一条警告。历史行仍然没有时间戳。
- 相对日期：抽取提示词给出今天的日期并要求写绝对日期；`_persist_extracted_knowledge` 存之前再用 `absolutize_relative_dates` 兜底改写（今天/明天/后天/大后天/昨天/前天/大前天、今早/今晚/明早/明晚/昨晚、上/这/本/下/下下 + 周/星期/礼拜 + 一到日/末/无）。「每周三」、单独的「周三」、「马上周末」「晚上周三」「看一下周五」「这周围」不改。召回时的查询没有做同样的改写，「下周三干嘛」只能靠话题词或向量召回到「2026-11-04 面试」。
- 滚动摘要：溢出达到 `memory_summary_batch_size` 才归档，一次把全部溢出折进 `session.metadata["rolling_summary"]`（模型要求 300 字内，硬上限 600 字，超了保留后半段）。提示词里是 `[Conversation]` 前的 `[Earlier Conversation]` 段。不再写 `summary` 知识条目；召回（两种存储和 `_session_knowledge_entries`）跳过 `summary` 和 30 天前的 `event`（`_EVENT_RECALL_MAX_AGE_SECONDS`）。归档在后台线程跑，模型调用不占会话锁，只有「重读、核对前缀、裁剪、保存」这一步拿会话锁；同一个会话同时只排一个归档。`skills/dispatcher.py` 里用户主动要的「总结」技能仍用原来的 `summarize_history`，没动。
- 内心想法：默认 80 字，`_clip_at_sentence` 在上限内最后一个句末标点处截断（该位置不到上限一半时才硬截加「…」）。没有做「很短的消息跳过分析调用」。
- 正则回退：`_writeback_knowledge_if_needed` / `_awriteback_knowledge_if_needed` 改为 `allow_fallback=not svc._requires_live_llm()`，线上模型返回空就不存；dry-run 和离线仍走回退。

测试：`test_summarization_mode_folds_overflow_into_rolling_summary`、`test_chat_prompt_has_current_time_and_earlier_conversation_section`、`test_knowledge_query_gives_today_for_absolute_dates`、`test_analysis_is_clipped_at_a_sentence_boundary`（`tests/test_app.py`）；`test_archive_waits_for_full_batch_and_stores_rolling_summary`、`test_archive_summarizes_outside_commit_lock`（`tests/test_organs.py`）；`test_relative_dates_are_stored_as_absolute_dates`、`test_live_runtime_does_not_fall_back_to_regex_extraction`（`tests/test_memory.py`）；`test_chat_summaries_are_not_recalled`、`test_events_older_than_a_month_are_excluded_from_recall`（`tests/test_state_store.py`）；`tests/test_clock.py`。

没做的（原建议第 5 条）：抽取时带上已有记忆、返回 add/update/delete；中文抽取提示词；过滤关于机器人自己、昵称、工具和搜索结果的条目。

### C. 技能触发（已完成，未在真实 QQ 环境验证）

原来的问题：线上 `ignore_prefixes` 含 `/`，`/skill` 命令到不了分发，到了也会因 `svc.config.background_image_delivery` 抛 `AttributeError`；`生图` 前缀只影响排名，仍要模型决定调不调；每轮只暴露排名前 `max_active_skills` 个技能，天气、搜索常常不在里面；`_clean_response` 把回复里的换行压掉；原始结果代码块可能重复；长回复整段发一条；`last_search` 每轮被清掉，跨轮问来源拿不到。

现在的做法：

- 显式命令：`/skill <技能 id/名字/别名> <参数>` 即使 `/` 在 `ignore_prefixes` 里也会执行；不认识的技能、其他 `/` 开头的消息仍然忽略。前缀由 `explicit_skill_prefixes` 配置。参数是 JSON/kv 之外的纯文本时，填进技能唯一的必填字符串字段（`/skill weather-command 上海` → `location`）。**这推翻了提交 a224e96 的约定**（当时的测试要求 `/skill` 被忽略、不绕过工具编排）；现在命令同样经过授权闸门：`_build_tool_invocation` 带上 `tool_orchestrator._allowed_tool_ids(event)`，未授权用户的 `/skill read-file-command …` 会被 `_execution_blocked` 拒绝。
- 生图：去掉对机器人的 @ 后以 `image_command_prefix` 或别名开头，就直接执行 `handler_target == "image"` 的技能，不经模型决策。线上没配好生图（`image_ready` 为假）时不拦截，交给模型。英文别名要求词边界（`draw a cat` 触发，`drawing` 不触发）。
- 暴露：模型可调用的技能 = 排名前 `max_active_skills` 个 + 固定的 `search`、`weather`、`summarize`、`image`（只取 `risk_level == "safe"` 的）；提示词里的技能卡片仍只放排名的那几个。高风险工具照旧由授权闸门控制。
- **安全修复**：`summarize` 工具标为 `safe`，但原来会读取任意本地路径（`_load_summarize_source` 直接 `Path(target).read_text()`，不检查目录），包括仓库里的 `.env` 和含 API key 的配置。现在本地文件只对 `tool_policy.authorized_user_ids` 里的人开放（`_extract_summarize_target(allow_local=...)`，`_resolve_summarize_payload` 再挡一次）；对其他人，路径当普通文字处理，不暴露文件是否存在。URL 抓取的内网拦截见下一条。
- 渲染：`_clean_response` 只合并词间空白、去行尾空白、三个以上换行压成两个，保留换行和行首缩进；它的其他调用点（图片说明、称呼引导、工具回复）都是给用户看的回复，分析和滚动摘要之后会再压平。工具渲染提示词里的结果预览改用 `_clip_multiline`。模型回复里已经整段出现过的原始结果不再追加代码块（工具循环和显式命令两处）。
- 长回复：`outbound_message_max_chars`（默认 900）以上按空行、换行、句末依次找切点（切点不到上限一半就硬切），分多条按顺序发，图片跟最后一条；历史和返回值里仍是完整回复。只作用于经过 emitter 的消息。
- 抓取 URL：web-fetch 和 summarize 原来能访问内网（NapCat WebUI、控制台、Docker 内部服务、169.254.169.254），而 summarize 是固定暴露、任何人都能触发的。现在走 `infra/safe_fetch.py`：只接受 http/https；先用 `getaddrinfo` 解析主机，任一地址不是公网（`ipaddress` 的 `is_global` 为假，或是组播；IPv4 映射的 IPv6 按 IPv4 判断）就拒绝，`127.1`、`2130706433`、`0x7f000001`、`napcat` 这类写法同样会被挡；重定向关掉自动跟随，手动跟最多 5 跳，每跳重新检查；正文流式读取，最多 2 MB（`DEFAULT_MAX_BYTES`，按解压后的字节算），超出截断并在 web-fetch 的 metadata 里标 `truncated`。被拦截时报「不能访问内网或本机地址：<主机>」。对所有人一律拦截，授权用户也不例外。仍有的缺口：检查和 httpx 连接各解析一次域名，DNS rebinding 可以钻这个空子；summarize CLI 只查第一跳，它自己跟的重定向和下载大小管不到；配了 `HTTP_PROXY` 时实际由代理去解析和连接。天气（wttr.in）和搜索（DuckDuckGo）的主机是固定的，没改。
- `last_search`：本轮没有搜索时，带结果的 `last_search` 保留（`LAST_SEARCH_TTL_SECONDS` 30 分钟后自然失效）；没结果的照旧清掉，避免被 `reply_gate._pending_search_payload` 当成待确认搜索。

测试：见第 2 节第 7 组。

没做的：

- `data/skills/skill.md`、`data/skills/summarize/SKILL.md` 两个旧格式文件没删（这轮不碰 `data/`），校验仍会报 `invalid_handler`、`duplicate_id`。
- （审计结论）`skills/intent_router.py`、`dispatcher.dispatch_skill` 的关键词分发、`MemberOnboarding.maybe_handle` 这些旧路径没有清理。
- 图片仍只能单张随最后一段发送；`gateways/onebot_actions.py::build_onebot_message_segments` 没改。

## 5. 其他待办（不在本轮）

- `web/http_api.py::_parse_segments` 只解析 text、image、at：引用回复（不知道对方在回哪条）、QQ 自带的小表情（`face`）、语音、转发、小程序卡片会被丢掉。图片已能看（第 1 节「看图」）。
- 好感度只涨不跌（`systems/value_game.py`：每回一句至少加 0.06），情绪几乎恒定（`systems/emotions.py` 的 `trust 0.55`）；两者都不进聊天提示词（`CharacterCard.system_message` 里没有），所以不影响语气。主动问候没有调度器（`organs/proactive.py` 只在控制台生成草稿），故事模式线上关闭，`memory/graph.py` 的高亮与知识库内容重复。
- ~~激活角色会清空所有会话记录~~（2026-10-26 已修）：`activate_character` 不再带 `reset_sessions`，启用同一个角色时也不重建存储；清空会话改为单独的 `POST /api/character/reset-sessions`（`confirm` 必须等于角色代号），控制台人物卡页「重置会话…」按钮要求输入代号确认。只删会话文件（含滚动摘要），长期记忆、成员和好感度不动。
- `listen.enabled=false` 时，群里 20 秒的跟进窗口（`group_follow_up_window_seconds`）按群算，不按人算（开着「听完再说」时改成按人算，见第 1 节）：机器人回复后接任何人的话，每次回复都延长窗口（2026-10-27 因此和另一个机器人互相刷屏，见第 3.3 节）。（审计结论）LLM 出错时机器人静默不回复；主回复链路现在至少记一条 WARNING「chat reply failed: …」（`feat/human-touch`），其他调用（分析、抽取、摘要）仍不记。
- 召回时同步发起一次嵌入调用，延迟约一次网络往返。

## 6. 验证方法

1. 单测：见第 1 节。新测试一律用临时目录。
2. 不碰真实数据的实机冒烟。它会调用付费的模型和嵌入接口，每条消息约 3–4k 提示词 token、3–7 次调用：
   - 把 `data/docker-compose-config.json` 复制到一个临时目录，把 `data_root` 改成该目录下的 `data`，`qq_sidecar.dry_run` 设为 `true`（或设环境变量 `OPENQQWAIFU_QQ_SIDECAR_DRY_RUN=true`）；同时拷贝 `data/cards` 和 `data/state` 过去。
   - 以这个临时目录作为工作目录运行。`ConfigManager.load()` 会按配置路径和当前目录去找 `napcat/config`，可能改写其中的 OneBot 配置，所以不要在仓库根目录跑。
   - 在 openqqwaifu 容器里也一样：宿主机的 `napcat/config` 挂在容器的 `/app/napcat-sidecar/config`，是 `_napcat_config_dirs` 的候选之一。容器里只用线上配置 `/app/data/docker-compose-config.json` 调 `ConfigManager.load()`；用镜像里的 `examples/config.napcat.compose.json`（没有令牌、默认 http 模式）去 load，会按它改写 NapCat 的 OneBot 配置（2026-10-28 检查时这样跑过一次，随后用线上配置 load 又写回了正确的令牌和反向 WS，NapCat 没有断开）。
   - `dry_run` 现在真的生效（2026-10-26 修）：`build_runtime_service` 在 `dry_run` 为真时出站一律用 `CapturingOutboundPort`，配了 `outbound_base_url` 也不发；`reverse_ws` 模式仍保留网关，NapCat 照常连进来推事件，只是回复不发出去。其余按线上走：`_requires_live_llm()` 为真，模型没配好会回「AI 工具编排模型还没连上」，知识抽取不走正则回退。之前的说法「dry_run 已不改变行为」作废。
   - 也可以直接 `serve`：`dry_run` 为真时启动日志里是 `outbound=capture(dry-run)`，控制台概览的出站模式显示 `dry-run (capture)`。
   - 不想接 NapCat、只想喂几条消息时，用下面的脚本。`build_file_service` 本来就只捕获，但它按离线处理（允许回退）；要和线上一致就用 `build_runtime_service` 加 `dry_run`。

```python
# PYTHONPATH=<repo>/src  <repo>/.venv/bin/python
from waifu_standalone.settings_admin.config_manager import ConfigManager
from waifu_standalone.runtime.container import build_runtime_service
from waifu_standalone.models import InboundEvent, MessageSegment

config = ConfigManager("<tmp>/data/config.json").load()
assert config.qq_sidecar.dry_run  # 否则会真发
service, outbound = build_runtime_service(config)
ev = InboundEvent(launcher_id="20001", launcher_type="person", sender_id="20001",
                  sender_name="测试用户", segments=[MessageSegment(kind="text", text="在吗")])
reply = service.handle_event(ev)
service.flush_background_tasks(timeout=120)
print(reply.text if reply else None)  # outbound.sent 里是被捕获的外发消息
```

3. 想看最终提示词，可以在 `prompt_assembler.py` 的两个工具循环调用 LLM 之前打印 `prompt`，或在 `infra/llm_clients.py` 的请求处记录。

## 7. 工作方式提示

- 一次只改一块，改完跑全量测试。
- 提交前确认没有带上 `.env`、`output/`、`data/`。
- 不要在仓库根目录用默认的 `AppConfig()` 去保存角色卡或会话。
