"""Background loops: the delivery worker and the periodic reconciler.

Both run as asyncio tasks inside the single uvicorn worker process. All their
state is in SQLite, so stopping them at any point is safe: on the next start
they pick up whatever is still pending.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .service import BATCH_SIZE, Notifier

log = logging.getLogger("app.worker")


class Worker:
    def __init__(self, notifier: Notifier, settings: Settings) -> None:
        self.notifier = notifier
        self.settings = settings
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def wake(self) -> None:
        self._wake.set()

    async def run_once(self) -> int:
        """Process everything that is due right now. Returns items handled."""
        handled = await self.notifier.process_due_checks()
        handled += await self.notifier.process_due_sends()
        return handled

    async def drain(self, max_rounds: int = 20) -> int:
        """Run until nothing is due (used at shutdown-free call sites and tests)."""
        total = 0
        for _ in range(max_rounds):
            handled = await self.run_once()
            total += handled
            if handled == 0:
                break
        return total

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._delivery_loop(), name="delivery-worker"),
            asyncio.create_task(self._reconcile_loop(), name="reconciler"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _delivery_loop(self) -> None:
        log.info("worker_started")
        while True:
            self._wake.clear()
            handled = 0
            try:
                handled = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker_iteration_failed")
            if handled >= BATCH_SIZE:
                continue  # more may be due right away
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.worker_poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _reconcile_loop(self) -> None:
        await asyncio.sleep(self.settings.reconcile_initial_delay)
        while True:
            try:
                result = await self.notifier.reconcile()
                log.debug("reconcile_done", extra=result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile_failed")
            await asyncio.sleep(self.settings.reconcile_interval)
