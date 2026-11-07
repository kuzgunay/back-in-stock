"""Application factory.

Run with:  uvicorn app.main:create_app --factory --workers 1
(one worker on purpose: the delivery worker and reconciler live in-process
and SQLite has a single writer; see README "Why one process").
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from . import __version__
from .config import Settings
from .db import Store
from .logging_setup import configure_logging
from .mailer import Mailer
from .middleware import (
    AccessLogMiddleware,
    ApiCorsMiddleware,
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
)
from .service import Notifier
from .stock import StockClient
from .util import Clock, utcnow
from .web import router
from .worker import Worker

log = logging.getLogger("app")


class AppContext:
    """Everything the app holds while running; built on startup."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock = utcnow,
        stock_transport: httpx.AsyncBaseTransport | None = None,
        email_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._stock_transport = stock_transport
        self._email_transport = email_transport

    async def startup(self) -> None:
        s = self.settings
        self.store = Store(s.db_path, self.clock)
        released = await self.store.release_leases()
        self.stock = StockClient(s, transport=self._stock_transport)
        self.mailer = Mailer(s, transport=self._email_transport)
        self.notifier = Notifier(s, self.store, self.stock, self.mailer, self.clock)
        self.worker = Worker(self.notifier, s)
        self.notifier.wake = self.worker.wake
        if s.run_background:
            self.worker.start()
        log.info("startup", extra={
            "version": __version__, "db": str(s.db_path), "sqlite": sqlite3.sqlite_version,
            "resumed_in_flight": released, "reconcile_interval_s": s.reconcile_interval,
            "admin_password_source": "ADMIN_PASSWORD" if s.admin_password else "WEBHOOK_SECRET",
        })

    async def shutdown(self) -> None:
        await self.worker.stop()
        await self.stock.aclose()
        await self.mailer.aclose()
        await self.store.close()
        log.info("shutdown")


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock = utcnow,
    stock_transport: httpx.AsyncBaseTransport | None = None,
    email_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()
        configure_logging(settings.log_level)

    ctx = AppContext(settings, clock=clock, stock_transport=stock_transport,
                     email_transport=email_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await ctx.startup()
        try:
            yield
        finally:
            await ctx.shutdown()

    app = FastAPI(title="Back in Stock", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.include_router(router)
    # Added innermost first; the last one added wraps everything.
    # Resulting order: access log -> security headers -> CORS -> body limit -> routes.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(ApiCorsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AccessLogMiddleware)
    return app
