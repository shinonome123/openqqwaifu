"""Cancellation-safe locks shared by asynchronous callers on different loops."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(eq=False)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    granted: bool = False


class SessionLock:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held = False
        self._waiters: deque[_Waiter] = deque()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop, loop.create_future())
        with self._guard:
            if not self._held:
                self._held = True
                return
            self._waiters.append(waiter)
        try:
            await waiter.future
        except asyncio.CancelledError:
            with self._guard:
                granted = waiter.granted
                if not granted:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
            if granted:
                self.release()
            raise

    def release(self) -> None:
        with self._guard:
            if not self._held:
                raise RuntimeError("release of an unlocked session lock")
            while self._waiters:
                waiter = self._waiters.popleft()
                if waiter.future.cancelled() or waiter.loop.is_closed():
                    continue
                waiter.granted = True
                break
            else:
                self._held = False
                return
        try:
            waiter.loop.call_soon_threadsafe(self._complete, waiter)
        except RuntimeError:
            # A loop can finish between is_closed() and scheduling the handoff.
            self.release()

    @staticmethod
    def _complete(waiter: _Waiter) -> None:
        if not waiter.future.done():
            waiter.future.set_result(None)

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Blocking acquire for worker threads that run outside an event loop."""
        asyncio.run(self.acquire())
        try:
            yield
        finally:
            self.release()

    def locked(self) -> bool:
        with self._guard:
            return self._held
