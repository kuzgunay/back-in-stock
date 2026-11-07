"""POST /webhooks/stock: auth, verification against the stock API, duplicates."""

from __future__ import annotations

import asyncio

from app.service import CHECK_BACKOFF_CAP, CHECK_MAX_ATTEMPTS


async def _subscribe_and_settle(h, *pairs):
    for email, sku in pairs:
        assert (await h.subscribe(email, sku)).status_code == 201
    await h.drain()  # run the subscription stock checks (item is sold out)


async def test_wrong_or_missing_secret_is_rejected_and_ignored(h, stock, resend):
    await _subscribe_and_settle(h, ("a@example.com", "ABC-1"))
    stock.set("ABC-1", 10)
    calls_before = len(stock.calls)

    missing = await h.webhook("evt_1", "ABC-1", secret=None)
    wrong = await h.webhook("evt_1", "ABC-1", secret="whsec-nope")
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert wrong.json()["error"] == "unauthorized"

    await h.drain()
    assert len(stock.calls) == calls_before  # nothing was even verified
    assert await h.ctx.store.list_events(10) == []
    assert resend.accepted == []
    # The real delivery with the same event_id is still processed afterwards.
    ok = await h.webhook("evt_1", "ABC-1")
    assert ok.json()["status"] == "processed"


async def test_phantom_restock_sends_nothing(h, stock, resend):
    await _subscribe_and_settle(h, ("a@example.com", "ABC-1"))
    stock.set("ABC-1", 0)  # the stock API disagrees with the webhook

    res = await h.webhook("evt_phantom", "ABC-1", quantity=12)
    assert res.status_code == 200
    assert res.json()["outcome"] == "phantom"
    await h.drain()

    assert resend.attempts == []
    (event,) = await h.ctx.store.list_events(10)
    assert event["outcome"] == "phantom"
    assert event["claimed_quantity"] == 12
    assert event["verified_quantity"] == 0
    assert (await h.requests())[0]["status"] == "open"


async def test_sold_out_event_is_recorded_without_email(h, stock, resend):
    await _subscribe_and_settle(h, ("a@example.com", "ABC-1"))
    res = await h.webhook("evt_zero", "ABC-1", quantity=0)
    assert res.json()["outcome"] == "out_of_stock"
    await h.drain()
    assert resend.attempts == []


async def test_verified_restock_emails_every_waiting_customer_once(h, stock, resend):
    await _subscribe_and_settle(h, ("a@example.com", "ABC-1"), ("b@example.com", "ABC-1"),
                                ("c@example.com", "XYZ-9"))
    stock.set("ABC-1", 12)
    res = await h.webhook("evt_ok", "abc-1", quantity=12)
    assert res.json() == {"status": "processed", "event_id": "evt_ok", "outcome": "verified",
                          "verified_quantity": 12, "notifying": 2}
    await h.drain()
    assert len(resend.to("a@example.com")) == 1
    assert len(resend.to("b@example.com")) == 1
    assert resend.to("c@example.com") == []  # different SKU
    statuses = {r["email"]: r["status"] for r in await h.requests()}
    assert statuses == {"a@example.com": "notified", "b@example.com": "notified",
                        "c@example.com": "open"}


async def test_same_event_delivered_twice_concurrently_sends_one_email(h, stock, resend):
    await _subscribe_and_settle(h, ("a@example.com", "ABC-1"))
    stock.set("ABC-1", 5)
    calls_before = stock.sku_calls("ABC-1")

    first, second = await asyncio.gather(h.webhook("evt_dup", "ABC-1"),
                                         h.webhook("evt_dup", "ABC-1"))
    statuses = sorted([first.json()["status"], second.json()["status"]])
    assert statuses == ["duplicate", "processed"]
    assert first.status_code == second.status_code == 200

    # A late retry of the same delivery is also a no-op.
    third = await h.webhook("evt_dup", "ABC-1")
    assert third.json()["status"] == "duplicate"

    await h.drain()
    assert len(resend.accepted) == 1
    assert len(resend.attempts) == 1
    assert stock.sku_calls("ABC-1") - calls_before == 1  # verified exactly once
    (event,) = await h.ctx.store.list_events(10)
    assert event["duplicate_count"] == 2


async def test_two_events_for_same_sku_at_once_email_each_customer_once(h, stock, resend):
    customers = [f"c{i}@example.com" for i in range(5)]
    await _subscribe_and_settle(h, *[(c, "ABC-1") for c in customers])
    stock.set("ABC-1", 50)

    results = await asyncio.gather(*(h.webhook(f"evt_{i}", "ABC-1") for i in range(4)))
    assert all(r.json()["status"] == "processed" for r in results)
    assert sum(r.json()["notifying"] for r in results) == len(customers)

    await h.drain()
    await h.drain()
    for c in customers:
        assert len(resend.to(c)) == 1, c
    assert len(resend.attempts) == len(customers)


async def test_invalid_payloads(h):
    bad_json = await h.client.post("/webhooks/stock", content=b"nope",
                                   headers={"X-Webhook-Secret": "whsec-test"})
    assert bad_json.status_code == 400
    missing = await h.client.post("/webhooks/stock", json={"sku": "ABC-1"},
                                  headers={"X-Webhook-Secret": "whsec-test"})
    assert missing.status_code == 422
    assert "event_id" in missing.json()["fields"]
    bad_sku = await h.client.post("/webhooks/stock", json={"event_id": "e", "sku": "<b>"},
                                  headers={"X-Webhook-Secret": "whsec-test"})
    assert bad_sku.status_code == 422


async def test_stock_api_down_queues_check_then_emails_once(h, stock, resend, clock):
    await _subscribe_and_settle(h, ("a@example.com", "ABC-1"))
    stock.set("ABC-1", 3)
    stock.down = True

    res = await h.webhook("evt_net", "ABC-1")
    assert res.status_code == 202
    assert res.json()["outcome"] == "pending"
    (event,) = await h.ctx.store.list_events(10)
    assert event["outcome"] == "pending"

    # A retry from the sender while we are still unsure is a duplicate, not new work.
    assert (await h.webhook("evt_net", "ABC-1")).json()["status"] == "duplicate"

    await h.drain()  # not due yet (backoff), and the API is still down anyway
    assert resend.attempts == []

    stock.down = False
    clock.advance(10)
    await h.drain()
    assert len(resend.to("a@example.com")) == 1
    (event,) = await h.ctx.store.list_events(10)
    assert event["outcome"] == "verified"
    assert event["notified_count"] == 1

    clock.advance(3600)
    await h.drain()
    assert len(resend.attempts) == 1


async def test_stock_check_gives_up_eventually_and_reconciler_still_covers(h, stock, resend, clock):
    await _subscribe_and_settle(h, ("a@example.com", "ABC-1"))
    stock.down = True
    assert (await h.webhook("evt_gone", "ABC-1")).status_code == 202
    for _ in range(CHECK_MAX_ATTEMPTS + 2):
        clock.advance(CHECK_BACKOFF_CAP + 1)
        await h.drain()
    (event,) = await h.ctx.store.list_events(10)
    assert event["outcome"] == "unverified"

    stock.down = False
    stock.set("ABC-1", 4)
    await h.ctx.notifier.reconcile()
    await h.drain()
    assert len(resend.to("a@example.com")) == 1


async def test_webhook_for_sku_nobody_wants_is_fine(h, stock, resend):
    stock.set("NEW-1", 9)
    res = await h.webhook("evt_lonely", "NEW-1")
    assert res.json()["notifying"] == 0
    await h.drain()
    assert resend.attempts == []
