from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import Future

from ..models import OutboundMessage
from .contracts import DeliveryRejected, ReplyGenerator, ReplyProposal, Sender
from .memories import InvalidMemoryChange
from .privacy import StaleWork
from .store import RuntimeBusy, RuntimeStore
from .followup_drafts import TIMEOUT_SECONDS


class ConversationEngine:
    """A single step never processes past unresolved delivery in a conversation."""

    def __init__(self, store: RuntimeStore, generator: ReplyGenerator, sender: Sender,
                 config_version: str, *, history_limit: int = 16):
        self.store, self.generator, self.sender = store, generator, sender
        self.config_version, self.history_limit = config_version, history_limit

    async def step(self, conversation_id: str) -> bool:
        item = await asyncio.to_thread(self.store.next_work, conversation_id, self.config_version)
        if item is None:
            return False
        if item["kind"] in {"binding_applied", "memory_applied", "reminder_skipped", "followup_skipped"}:
            return True
        if item["kind"] in {"reminder_ready", "followup_ready"}:
            context, event, outbox = None, None, item["outbox"]
        else:
            context, event, outbox = item["context"], item["event"], item["outbox"]
        if outbox is None:
            try:
                history = await asyncio.to_thread(self.store.history, context, self.history_limit)
                message = await asyncio.to_thread(self.store.episode_time_command, context, event)
                if message is None:
                    message = await asyncio.to_thread(self.store.contact_command, context, event)
                if message is None:
                    message = await asyncio.to_thread(self.store.reminder_command, context, event)
                if message is None:
                    message = await self.generator.generate(context, event, history)
                memory_changes = ()
                if isinstance(message, ReplyProposal):
                    memory_changes, message = message.memory_changes, message.message
                if message is None or (not message.text.strip() and not message.images):
                    await asyncio.to_thread(self.store.end_generation, context.event_id, ignored=True)
                    return True
            except Exception as exc:
                await asyncio.to_thread(self.store.end_generation, context.event_id,
                                        error_code="generation_" + type(exc).__name__)
                return True
            # A persistence failure is fatal to this worker. It must not continue
            # generating later turns against an uncertain local commit.
            try:
                outbox = await asyncio.to_thread(self.store.save_proposal, context, message, memory_changes)
            except StaleWork:
                return True
            except InvalidMemoryChange:
                await asyncio.to_thread(self.store.end_generation, context.event_id, error_code="generation_InvalidMemoryChange")
                return True
        message = OutboundMessage(**json.loads(outbox["payload_json"]))
        ready = getattr(self.sender, "is_ready", None)
        if callable(ready) and not ready():
            return False
        if not await asyncio.to_thread(self.store.begin_dispatch, outbox["outbox_id"]):
            return False
        try:
            receipt = await self.sender.send(message)
        except DeliveryRejected:
            await asyncio.to_thread(self.store.finish_delivery, outbox["outbox_id"], "rejected", error_code="send_rejected")
        except asyncio.CancelledError:
            # Cancellation does not prove a network request was not accepted.
            await asyncio.to_thread(self.store.finish_delivery, outbox["outbox_id"], "unknown", error_code="send_cancelled")
            raise
        except Exception as exc:
            await asyncio.to_thread(self.store.finish_delivery, outbox["outbox_id"], "unknown",
                                    error_code="send_" + type(exc).__name__)
        else:
            # If this transaction fails after an ACK, leave dispatching intact.
            # Recovery will require review, never repeat the external send.
            await asyncio.to_thread(self.store.finish_delivery, outbox["outbox_id"],
                                    receipt.status, receipt.platform_message_id)
        return True

    async def draft_step(self):
        context = await asyncio.to_thread(self.store.claim_followup_draft, self.history_limit)
        if context is None:
            return False
        async def before_call(report):
            if not await asyncio.to_thread(self.store.authorize_draft_call, context.job_id, report):
                raise StaleWork("draft is no longer current")
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                result = await self.generator.generate_followup(context, before_call)
        except StaleWork:
            return True
        except Exception as exc:
            await asyncio.to_thread(self.store.finish_followup_draft, context.job_id, error_code="generation_" + type(exc).__name__)
        else:
            await asyncio.to_thread(self.store.finish_followup_draft, context.job_id, result)
        return True


class RuntimeHost:
    """Owns one event loop, bounded workers, and the lifetime of the writer lock.

    Admission is synchronous durable storage only. Model/transport work happens
    on the owned loop, independent of HTTP request cancellation.
    """

    def __init__(self, engine: ConversationEngine, *, workers: int = 4):
        if not 1 <= workers <= 32:
            raise ValueError("workers must be between 1 and 32")
        self.engine, self.workers = engine, workers
        self._guard = threading.RLock()
        self._stopping = False
        self._closed = False
        self._fatal = ""
        self._active: set[str] = set()
        self._draft_active = False
        self._ready = threading.Event()
        self._done: Future = Future()
        self.engine.store.recover()
        self._thread = threading.Thread(target=self._thread_main, name="openqqwaifu-v2", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("runtime v2 loop did not start")
        if self._done.done():
            self._done.result()

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            with self._guard:
                self._fatal = type(exc).__name__
            self._done.set_exception(exc)
        else:
            self._done.set_result(None)
        finally:
            self._ready.set()

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._ready.set()
        tasks: set[asyncio.Task] = set()
        draft_tasks: set[asyncio.Task] = set()
        last_selected: dict[str, float] = {}
        next_reminder_tick = 0.0
        try:
            while not self._stopping and not self._fatal:
                self._wake.clear()
                if time.monotonic() >= next_reminder_tick:
                    await asyncio.to_thread(self.engine.store.tick_reminders)
                    next_reminder_tick = time.monotonic() + 0.5
                candidates = await asyncio.to_thread(self.engine.store.ready_conversations, self.engine.store.max_pending)
                candidates.sort(key=lambda key: last_selected.get(key, 0.0))
                for conversation_id in candidates:
                    if len(tasks) >= self.workers or self._stopping or self._fatal:
                        break
                    if conversation_id in self._active:
                        continue
                    with self._guard:
                        self._active.add(conversation_id)
                    last_selected[conversation_id] = time.monotonic()
                    task = asyncio.create_task(self._process(conversation_id))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                if not draft_tasks and not self._stopping and not self._fatal and await asyncio.to_thread(self.engine.store.drafts_pending):
                    with self._guard:
                        self._draft_active = True
                    task = asyncio.create_task(self._process_draft())
                    draft_tasks.add(task)
                    task.add_done_callback(draft_tasks.discard)
                try:
                    await asyncio.wait_for(self._wake.wait(), 0.1)
                except TimeoutError:
                    pass
        finally:
            # Do not cancel potentially in-flight sends or thread-backed model
            # calls. A timeout at the caller retains all resources and ownership.
            if tasks or draft_tasks:
                await asyncio.gather(*tasks, *draft_tasks, return_exceptions=True)

    async def _process_draft(self):
        try:
            await self.engine.draft_step()
        except Exception as exc:
            with self._guard:
                self._fatal = type(exc).__name__
        finally:
            with self._guard:
                self._draft_active = False
            self._wake.set()

    async def _process(self, conversation_id):
        progressed = False
        try:
            progressed = await self.engine.step(conversation_id)
        except Exception as exc:
            with self._guard:
                self._fatal = type(exc).__name__
        finally:
            with self._guard:
                self._active.discard(conversation_id)
            if progressed or self._fatal:
                self._wake.set()

    def mutate(self, callback, *args, **kwargs):
        with self._guard:
            if self._stopping or self._fatal or self._done.done():
                raise RuntimeBusy("runtime v2 is stopping or needs recovery")
            result = callback(*args, **kwargs)
            self._loop.call_soon_threadsafe(self._wake.set)
            return result

    def status(self):
        result = self.engine.store.snapshot()
        with self._guard:
            result.update(enabled=True, stopping=self._stopping, worker_error=self._fatal,
                          active_conversations=sorted(self._active), draft_active=self._draft_active)
        return result

    def wait_idle(self, timeout: float = 60):
        deadline = time.monotonic() + timeout
        while True:
            with self._guard:
                if self._fatal and not self._active and not self._draft_active:
                    raise RuntimeError("runtime v2 requires recovery: " + self._fatal)
                active = bool(self._active) or self._draft_active
            if not active and (self._stopping or (not self.engine.store.ready_conversations(1) and not self.engine.store.drafts_pending())):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("runtime v2 has unfinished work")
            time.sleep(0.01)

    def begin_shutdown(self):
        with self._guard:
            self._stopping = True
            if not self._done.done():
                try:
                    self._loop.call_soon_threadsafe(self._wake.set)
                except RuntimeError:
                    if not self._loop.is_closed():
                        raise

    def close(self, timeout: float = 60):
        if self._closed:
            return
        self.begin_shutdown()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("runtime v2 is still using its storage; retry close after work completes")
        close = getattr(self.engine.generator, "close", None)
        try:
            if callable(close):
                close()
        finally:
            self.engine.store.close()
            self._closed = True
