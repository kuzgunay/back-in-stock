"""The reconciler: catches restocks whose webhook never arrived."""

from __future__ import annotations

import asyncio


async def test_missed_webhook_is_caught_by_reconciler(h, stock, resend):
    await h.subscribe("a@example.com", "ABC-1")
    await h.subscribe("b@example.com", "XYZ-2")
    await h.drain()
    stock.set("ABC-1", 4)
    stock.set("XYZ-2", 0)
    stock.calls.clear()

    result = await h.ctx.notifier.reconcile()
    assert result == {"skus": 2, "available": 1, "claimed": 1}
    assert stock.calls == ["/products"]  # one catalogue call for all SKUs
    await h.drain()
    assert len(resend.to("a@example.com")) == 1
    assert resend.to("b@example.com") == []
    by_email = {r["email"]: r for r in await h.requests()}
    assert by_email["a@example.com"]["triggered_by"] == "reconcile"


async def test_reconciler_makes_no_calls_when_nobody_is_waiting(h, stock):
    stock.calls.clear()
    assert await h.ctx.notifier.reconcile() == {"skus": 0, "available": 0, "claimed": 0}
    assert stock.calls == []


async def test_reconciler_falls_back_to_per_sku_checks(h, stock, resend):
    await h.subscribe("a@example.com", "ABC-1")
    await h.drain()
    stock.set("ABC-1", 2)
    stock.catalogue_down = True
    stock.calls.clear()
    result = await h.ctx.notifier.reconcile()
    assert stock.calls == ["/products", "/products/ABC-1"]
    assert result["claimed"] == 1
    await h.drain()
    assert len(resend.to("a@example.com")) == 1


async def test_reconciler_racing_a_webhook_still_sends_once(h, stock, resend):
    for i in range(4):
        await h.subscribe(f"c{i}@example.com", "ABC-1")
    await h.drain()
    stock.set("ABC-1", 9)
    await asyncio.gather(h.ctx.notifier.reconcile(), h.webhook("evt_race", "ABC-1"),
                         h.ctx.notifier.reconcile())
    await h.drain()
    for i in range(4):
        assert len(resend.to(f"c{i}@example.com")) == 1
    assert len(resend.attempts) == 4
