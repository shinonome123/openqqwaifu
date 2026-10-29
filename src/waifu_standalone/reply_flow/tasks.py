"""Jobs that take several steps, done in the background while the chat goes on.

"把这周群里聊过的游戏整理一下" needs her to read the chat log, sort it out, maybe look a
few things up and write a file: more tool calls than a reply has, and longer than anyone
waits for one. The ``start-task`` tool hands such a job off: she answers at once ("好，我
弄一下"), and a background run, with every tool this person may use and up to
``tasks.max_rounds`` model calls, does it and sends the result to the chat when done.

One task per chat at a time; a run that takes longer than ``tasks.timeout_seconds`` says
it got stuck. The ``chat-log`` tool (any reply may use it) reads this chat's own log.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..models import EmotionState, InboundEvent, OutboundMessage
from ..skills.tool_registry import ToolExecutionResult, ToolInvocation
from .stickers import strip_sticker_tags

if TYPE_CHECKING:
    from ..app import WaifuService

_LOGGER = logging.getLogger(__name__)

_STUCK_REPLY = "刚才那件事没做完，中间卡住了，等会儿再让我试一次吧。"
_NOT_IN_TASKS = {"start-task"}

START_TASK_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start", "status"],
            "description": "start 把一件要好几步的事交给后台去做；status 看这个聊天里正在做的事。",
        },
        "goal": {
            "type": "string",
            "description": "start 时要做成什么，写清楚：要什么结果、范围（例如「这周」「群里」）、要不要写成文件。",
        },
    },
    "required": ["action"],
}
START_TASK_DESCRIPTION = (
    "把一件要好几步才能做完的事交给后台去做：要翻聊天记录、查好几样东西、整理汇总、对比、写成文件这类。"
    "交出去以后你只要简短答应对方，做完了结果会自动发到这个聊天。一两句话就能答、查一次就够的事不要用它。"
)
CHAT_LOG_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "days": {"type": "integer", "description": "往回看几天，默认 1，最多 14。「这周」写 7。"},
        "keyword": {"type": "string", "description": "可选，只看带这个词的消息（几个词用空格隔开，要都带）。"},
        "limit": {"type": "integer", "description": "最多返回多少条，默认 200，最多 400。"},
    },
}
CHAT_LOG_DESCRIPTION = "翻这个聊天的聊天记录（带时间和说话人），看以前谁说过什么、聊过什么。只能看当前这个聊天。"


@dataclass(slots=True)
class RunningTask:
    goal: str
    started_at: float
    task: asyncio.Task[None]


class TaskRunner:
    def __init__(self, service: WaifuService) -> None:
        self._service = service
        self._running: dict[tuple[str, str], RunningTask] = {}

    @property
    def settings(self):  # type: ignore[no-untyped-def]
        return self._service.config.tasks

    # ------------------------------------------------------------------
    # The start-task tool
    # ------------------------------------------------------------------
    async def ause_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return self.use_tool(invocation)

    def use_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        if not self.settings.enabled:
            return ToolExecutionResult(error="disabled", text="后台任务没开，这次就直接回答吧。")
        event = invocation.event
        key = (event.launcher_type, event.launcher_id)
        running = self._running.get(key)
        if running is not None and running.task.done():
            running = None
        action = str(invocation.argument("action", "start") or "start").strip().lower()
        if action == "status":
            if running is None:
                return ToolExecutionResult(text="这个聊天里现在没有在做的事。")
            minutes = max(1, int((time.time() - running.started_at) // 60))
            return ToolExecutionResult(text=f"正在做：{running.goal}（做了大约 {minutes} 分钟）")
        goal = " ".join(str(invocation.argument("goal", "") or "").split())[:400]
        if not goal:
            return ToolExecutionResult(error="missing goal", text="要做成什么？goal 不能空着。")
        if running is not None:
            return ToolExecutionResult(error="busy", text=f"上一件还没做完：{running.goal}。跟对方说做完这件再接着弄。")
        if sum(1 for item in self._running.values() if not item.task.done()) >= max(1, int(self.settings.max_running)):
            return ToolExecutionResult(error="busy", text="手上的事太多了，跟对方说等一会儿再来。")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return ToolExecutionResult(error="no loop", text="现在没法在后台做，这次就直接回答吧。")
        task = loop.create_task(self._run(event, goal, address=invocation.address, assistant_name=invocation.assistant_name))
        self._running[key] = RunningTask(goal=goal, started_at=time.time(), task=task)
        task.add_done_callback(lambda done, key=key: self._finished(key, done))
        self._service._track_async_task("background_task", task)
        _LOGGER.info("task started chat=%s:%s goal=%s", event.launcher_type, event.launcher_id, goal[:80])
        return ToolExecutionResult(text="已经交给后台去做了，做完结果会发到这个聊天。现在只要简短答应对方，别现在就给结果。")

    def running(self, launcher_type: str, launcher_id: str) -> str:
        """The goal of the task running in this chat, or ''."""
        item = self._running.get((launcher_type, launcher_id))
        return item.goal if item is not None and not item.task.done() else ""

    def drain(self, timeout: float = 30.0) -> None:
        """Wait until no task runs (tests)."""
        deadline = time.monotonic() + timeout
        while any(not item.task.done() for item in self._running.values()):
            if time.monotonic() > deadline:
                raise TimeoutError("tasks did not finish")
            time.sleep(0.02)

    def close(self) -> None:
        for item in self._running.values():
            if not item.task.done():
                try:
                    item.task.get_loop().call_soon_threadsafe(item.task.cancel)
                except RuntimeError:
                    pass

    def _finished(self, key: tuple[str, str], done: asyncio.Task[None]) -> None:
        if not done.cancelled() and done.exception() is not None:
            _LOGGER.error("task failed chat=%s:%s", *key, exc_info=done.exception())

    # ------------------------------------------------------------------
    # Doing it
    # ------------------------------------------------------------------
    async def _run(self, event: InboundEvent, goal: str, *, address: str, assistant_name: str) -> None:
        svc = self._service
        character = svc._active_character_id()
        try:
            text, images = await asyncio.wait_for(
                self._work(event, goal, address=address, assistant_name=assistant_name, character=character),
                timeout=max(30.0, float(self.settings.timeout_seconds)),
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("task timed out chat=%s:%s", event.launcher_type, event.launcher_id)
            text, images = _STUCK_REPLY, []
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("task failed chat=%s:%s", event.launcher_type, event.launcher_id)
            text, images = _STUCK_REPLY, []
        if not text and not images:
            text = _STUCK_REPLY
        await svc._process_companion(event.launcher_type, event.launcher_id, lambda: self._send(event, text, images, character))

    async def _work(self, event: InboundEvent, goal: str, *, address: str, assistant_name: str, character: str) -> tuple[str, list[str]]:
        svc = self._service
        session = await svc.memory.aload(event.launcher_id, event.launcher_type, character_id=character)
        latest = event.command_text(svc.config.bot_account_id).strip()
        context = svc.tool_orchestrator.exposure_context(
            event, session, latest_message=latest or goal, address=address, assistant_name=assistant_name, all_tools=True
        )
        context = replace(
            context,
            allowed_tool_ids=set(context.allowed_tool_ids or set()) - _NOT_IN_TASKS,
            max_rounds=max(2, int(self.settings.max_rounds)),
            explicit=True,
        )
        history_turns = await svc.memory.adialogue_turns(
            event.launcher_id,
            event.launcher_type,
            limit=svc.config.history_window_messages,
            max_chars=svc.config.history_window_chars,
            character_id=character,
        )
        hint = (
            "[后台任务]\n"
            f"{address or '对方'}让你做一件要好几步的事：{goal}\n"
            f"（对方原话：「{latest}」）\n"
            "你已经答应了，现在在后台专心把它做完：需要什么就一步步调用工具（翻聊天记录用 chat-log，"
            "查资料用搜索，看网页用 web-fetch，要文件就写文件），只用工具查到的真实内容，别编。\n"
            "做完后直接给出最终结果：先用你平时的语气说一句做好了，再把结果分条写清楚，可以长一点。"
            "有做不到的部分，就说清楚哪里没做到、为什么。"
            "QQ 不显示 markdown：不要用 **加粗**、# 标题或表格线，分条就用「1.」「-」和换行。"
        )
        reply = await svc.generator.agenerate_reply_message(
            event,
            session,
            EmotionState(),
            assistant_name=assistant_name,
            address_override=address,
            history_turns=history_turns,
            tool_context=context,
            allow_fallback=False,
            turn_hint=hint,
        )
        return strip_sticker_tags(reply.text).strip(), list(reply.images)

    async def _send(self, event: InboundEvent, text: str, images: list[str], character: str) -> bool:
        svc = self._service
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=text,
            images=images,
            # In a group, quote what was asked: the answer comes long after.
            reply_to=str(event.message_id or "") if event.launcher_type == "group" else "",
        )
        await svc.emitter.adispatch(message, delay_seconds=0.0)
        svc._record_outbound(message)
        await svc.memory.asave_assistant_message(event.launcher_id, event.launcher_type, text, character_id=character)
        _LOGGER.info("task done chat=%s:%s", event.launcher_type, event.launcher_id)
        return True

    # ------------------------------------------------------------------
    # The chat-log tool
    # ------------------------------------------------------------------
    async def ause_chat_log_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return await asyncio.to_thread(self.use_chat_log_tool, invocation)

    def use_chat_log_tool(self, invocation: ToolInvocation) -> ToolExecutionResult:
        svc = self._service
        event = invocation.event

        def number(name: str, default: int, top: int) -> int:
            try:
                value = int(float(invocation.argument(name, default) or default))
            except (TypeError, ValueError):
                value = default
            return max(1, min(top, value))

        days = number("days", 1, 14)
        items = svc.chat_log.read(
            character_id=svc._active_character_id(),
            launcher_type=event.launcher_type,
            launcher_id=event.launcher_id,
            since=time.time() - days * 86400.0,
            keyword=str(invocation.argument("keyword", "") or ""),
            limit=number("limit", 200, 400),
        )
        if not items:
            return ToolExecutionResult(text=f"最近 {days} 天这个聊天里没有找到这样的记录（记录是从这个功能上线后才开始存的）。")
        name = invocation.assistant_name or svc.config.assistant_name
        for item in items:
            if item.get("who") == ASSISTANT_SPEAKER:
                item["who"] = f"{name}（你）"
        return ToolExecutionResult(
            text=f"最近 {days} 天的 {len(items)} 条：\n" + svc.chat_log.format(items),
            metadata={"model_limit": 20000},
        )


# How the chat log names her own lines; shown as her name when read back.
ASSISTANT_SPEAKER = "__assistant__"
