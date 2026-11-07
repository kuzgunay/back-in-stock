"""Exactly-once delivery: retries, idempotency keys, restarts, content."""

from __future__ import annotations

import asyncio
import re

from app.service import LEASE_SECONDS

from .conftest import running


async def _restock(h, stock, sku="ABC-1", quantity=7, event_id="evt_r"):
    stock.set(sku, quantity)
    res = await h.webhook(event_id, sku, quantity=quantity)
    assert res.json()["outcome"] == "verified"
    return res


async def test_failed_send_is_retried_later_with_same_idempotency_key(h, stock, resend, clock):
    rid = (await h.subscribe("a@example.com", "ABC-1")).json()["id"]
    await h.drain()
    resend.fail_next = 1
    await _restock(h, stock)

    await h.drain()
    assert resend.accepted == []
    row = await h.ctx.store.get_request(rid)
    assert row["status"] == "notifying"
    assert row["attempts"] == 1
    assert "500" in row["last_error"]

    await h.drain()  # backoff not elapsed: nothing is resent early
    assert len(resend.attempts) == 1

    clock.advance(10)
    await h.drain()
    assert len(resend.accepted) == 1
    assert [a.idempotency_key for a in resend.attempts] == [f"bis-{h.ctx.store.install_id}-{rid}"] * 2
    row = await h.ctx.store.get_request(rid)
    assert row["status"] == "notified"
    assert row["attempts"] == 2
    assert row["email_id"] == "email_1"
    assert row["notified_at"] is not None
    assert row["last_error"] is None
    # Both attempts carried a byte-identical payload (required for idempotent replay).
    assert resend.attempts[0].payload == resend.attempts[1].payload


async def test_several_consecutive_failures_still_end_in_one_email(h, stock, resend, clock):
    await h.subscribe("a@example.com", "ABC-1")
    await h.subscribe("b@example.com", "ABC-1")
    await h.drain()
    resend.fail_next = 5
    await _restock(h, stock)
    for _ in range(8):
        await h.drain()
        clock.advance(120)
    assert len(resend.to("a@example.com")) == 1
    assert len(resend.to("b@example.com")) == 1


async def test_lost_response_after_provider_accepted_does_not_double_send(h, stock, resend, clock):
    rid = (await h.subscribe("a@example.com", "ABC-1")).json()["id"]
    await h.drain()
    resend.drop_response_next = 1  # provider sends, but we never hear back
    await _restock(h, stock)
    await h.drain()
    assert (await h.ctx.store.get_request(rid))["status"] == "notifying"

    clock.advance(10)
    await h.drain()
    assert len(resend.accepted) == 1  # the retry was de-duplicated by the key
    assert len(resend.attempts) == 2
    row = await h.ctx.store.get_request(rid)
    assert row["status"] == "notified"
    assert row["email_id"] == "email_1"


async def test_restart_resumes_pending_send_exactly_once(settings, stock, resend, clock):
    async with running(settings, stock, resend, clock) as h:
        rid = (await h.subscribe("a@example.com", "ABC-1")).json()["id"]
        await h.drain()
        resend.fail_next = 1
        await _restock(h, stock)
        await h.drain()
        assert (await h.ctx.store.get_request(rid))["status"] == "notifying"
    assert resend.accepted == []

    clock.advance(60)
    # New process, same database file, real background worker loop.
    async with running(settings, stock, resend, clock, run_background=True,
                       worker_poll_interval=0.05, reconcile_initial_delay=3600) as h2:
        for _ in range(100):
            if (await h2.ctx.store.get_request(rid))["status"] == "notified":
                break
            await asyncio.sleep(0.05)
        assert (await h2.ctx.store.get_request(rid))["status"] == "notified"
        await asyncio.sleep(0.2)  # give a buggy worker the chance to send again
    assert len(resend.accepted) == 1
    assert len(resend.to("a@example.com")) == 1


async def test_crash_between_send_and_db_update_is_covered_by_idempotency_key(
        settings, stock, resend, clock):
    async with running(settings, stock, resend, clock) as h:
        rid = (await h.subscribe("a@example.com", "ABC-1")).json()["id"]
        await h.drain()
        await _restock(h, stock)
        # Simulate the process dying mid-delivery: the row is leased and the
        # provider accepted the email, but 'notified' was never written.
        (row,) = await h.ctx.store.lease_due_sends(10, LEASE_SECONDS)
        result = await h.ctx.mailer.send(to=row["email"], subject="s", html="h", text="t",
                                         idempotency_key=f"bis-{h.ctx.store.install_id}-{rid}")
        assert result.ok
    assert len(resend.accepted) == 1

    async with running(settings, stock, resend, clock) as h2:
        await h2.drain()  # leases from the dead process are released at startup
        row = await h2.ctx.store.get_request(rid)
        assert row["status"] == "notified"
        assert row["email_id"] == "email_1"
    assert len(resend.accepted) == 1
    assert len(resend.attempts) == 2


async def test_item_already_in_stock_at_signup_is_emailed_once(h, stock, resend, clock):
    stock.set("ABC-1", 20, name="Linen Overshirt")
    res = await h.subscribe("a@example.com", "ABC-1")
    assert res.status_code == 201
    await h.drain()
    assert len(resend.to("a@example.com")) == 1
    # Asking again while that request is being/has been handled doesn't resend.
    await h.ctx.notifier.reconcile()
    clock.advance(3600)
    await h.drain()
    assert len(resend.to("a@example.com")) == 1
    assert (await h.requests())[0]["triggered_by"] == "subscribe"


async def test_permanent_rejection_marks_failed_without_retrying(h, stock, resend, clock):
    rid = (await h.subscribe("a@example.com", "ABC-1")).json()["id"]
    await h.drain()
    resend.fail_next, resend.fail_status = 1, 422
    await _restock(h, stock)
    await h.drain()
    clock.advance(3600)
    await h.drain()
    row = await h.ctx.store.get_request(rid)
    assert row["status"] == "failed"
    assert len(resend.attempts) == 1
    # A failed request doesn't block the customer from subscribing again.
    assert (await h.subscribe("a@example.com", "ABC-1")).status_code == 201


async def test_email_payload_and_content(h, stock, resend):
    await h.subscribe("a@example.com", "ABC-1")
    await h.drain()
    stock.set("ABC-1", 3, name="Linen <Overshirt> & Co", price=89.5,
              image="https://cdn.example.com/linen.jpg")
    await h.webhook("evt_c", "ABC-1")
    await h.drain()

    (sent,) = resend.accepted
    p = sent.payload
    assert set(p) == {"from", "to", "subject", "html", "text"}
    assert p["from"] == "Back in Stock <alerts@backinstock.example>"
    assert p["to"] == ["a@example.com"]
    assert p["subject"] == "Good news — Linen <Overshirt> & Co is back in stock"
    html = p["html"]
    assert "Linen &lt;Overshirt&gt; &amp; Co" in html
    assert "<Overshirt>" not in html
    assert "$89.50" in html
    assert "Only 3 left" in html
    assert 'src="https://cdn.example.com/linen.jpg"' in html
    assert 'href="https://shop.example.com"' in html
    assert 'name="viewport"' in html and "max-width:560px" in html
    assert re.findall(r"<img\b", html) == ["<img"]  # product photo only, no tracking pixel
    assert "Linen <Overshirt> & Co" in p["text"]
    assert "https://shop.example.com" in p["text"]
    assert "<" not in p["text"].replace("<Overshirt>", "")
