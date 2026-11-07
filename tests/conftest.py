from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.main import AppContext, create_app

from .fakes import FakeClock, FakeResend, FakeStockApi

WEBHOOK_SECRET = "whsec-test"


@pytest.fixture
def stock() -> FakeStockApi:
    return FakeStockApi()


@pytest.fixture
def resend() -> FakeResend:
    return FakeResend()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        stock_api_url="http://stock.test/products",
        stock_api_key="stock-key",
        resend_api_key="resend-key",
        resend_base_url="http://resend.test",
        webhook_secret=WEBHOOK_SECRET,
        data_dir=tmp_path / "data",
        store_url="https://shop.example.com",
        run_background=False,  # tests drive the worker explicitly
    )


class Harness:
    def __init__(self, client: httpx.AsyncClient, ctx: AppContext) -> None:
        self.client = client
        self.ctx = ctx

    async def subscribe(self, email: str, sku: str) -> httpx.Response:
        return await self.client.post("/api/requests", json={"email": email, "sku": sku})

    async def webhook(self, event_id: str, sku: str, quantity: int = 12,
                      secret: str | None = WEBHOOK_SECRET) -> httpx.Response:
        headers = {"X-Webhook-Secret": secret} if secret is not None else {}
        body = {"event_id": event_id, "type": "stock.updated", "sku": sku,
                "quantity": quantity, "occurred_at": "2026-01-01T12:00:00Z"}
        return await self.client.post("/webhooks/stock", json=body, headers=headers)

    async def drain(self) -> int:
        return await self.ctx.worker.drain()

    async def requests(self) -> list[dict]:
        rows, _ = await self.ctx.store.list_requests(status=None, query=None, limit=1000)
        return rows


@asynccontextmanager
async def running(settings: Settings, stock: FakeStockApi, resend: FakeResend,
                  clock: FakeClock, **overrides: object) -> AsyncIterator[Harness]:
    app = create_app(replace(settings, **overrides) if overrides else settings, clock=clock,
                     stock_transport=stock.transport(), email_transport=resend.transport())
    ctx: AppContext = app.state.ctx
    await ctx.startup()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://app.test") as client:
            yield Harness(client, ctx)
    finally:
        await ctx.shutdown()


@pytest.fixture
async def h(settings, stock, resend, clock) -> AsyncIterator[Harness]:
    async with running(settings, stock, resend, clock) as harness:
        yield harness
