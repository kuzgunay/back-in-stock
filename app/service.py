"""The notification pipeline.

    subscribe ─┐                         ┌─> claim open requests ──> send (worker)
    webhook ───┼─> stock check (outbox) ─┤      open -> notifying      notifying -> notified
    reconcile ─┘   verified by stock API └─> not in stock: nothing

Every signal (webhook, new subscription, periodic reconcile) ends in the same
place: the stock API is asked, and only a confirmed restock claims requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings
from .db import Store
from .emails import build_email
from .mailer import Mailer
from .stock import Product, StockClient, StockError
from .util import Clock, backoff_seconds, mask_email, truncate
from .validation import StockEvent

log = logging.getLogger("app.service")

LEASE_SECONDS = 60.0  # longer than any single outbound call can take

SEND_BACKOFF_BASE = 5.0
SEND_BACKOFF_CAP = 900.0  # transient send failures retry every <=15 min, indefinitely

CHECK_BACKOFF_BASE = 2.0
CHECK_BACKOFF_CAP = 300.0
CHECK_MAX_ATTEMPTS = 12  # ~30 min; after that the reconciler still covers the SKU

BATCH_SIZE = 50
CONCURRENCY = 5
RECONCILE_FALLBACK_SKUS = 25  # per-SKU lookups per cycle if the catalogue call fails


@dataclass(frozen=True)
class CheckOutcome:
    ok: bool
    product: Product | None = None
    claimed: int = 0
    error: str | None = None


class Notifier:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        stock: StockClient,
        mailer: Mailer,
        clock: Clock,
    ) -> None:
        self.settings = settings
        self.store = store
        self.stock = stock
        self.mailer = mailer
        self.clock = clock
        self.wake: Callable[[], None] = lambda: None

    # -- entry points -----------------------------------------------------------

    async def subscribe(self, email: str, sku: str) -> tuple[dict[str, Any], bool]:
        """Create (or find) the open request. The stock check is queued in the
        same transaction and done by the worker, so this returns fast and a
        stock API outage can't fail the customer's request."""
        row, created = await self.store.create_request(email, sku)
        if created:
            log.info("request_created", extra={"request_id": row["id"], "sku": sku,
                                               "email": mask_email(email)})
            self.wake()
        else:
            log.info("request_duplicate", extra={"request_id": row["id"], "sku": sku,
                                                 "email": mask_email(email)})
        return row, created

    async def handle_webhook(self, event: StockEvent) -> tuple[int, dict[str, Any]]:
        job_id = await self.store.record_webhook(
            event_id=event.event_id,
            sku=event.sku,
            event_type=event.type,
            claimed_quantity=event.quantity,
            occurred_at=event.occurred_at,
            lease_seconds=LEASE_SECONDS,
        )
        if job_id is None:
            log.info("webhook_duplicate", extra={"event_id": event.event_id, "sku": event.sku})
            return 200, {"status": "duplicate", "event_id": event.event_id}

        log.info("webhook_received", extra={"event_id": event.event_id, "sku": event.sku,
                                            "claimed_quantity": event.quantity})
        # The event and its verification job are committed. Try the check
        # inline so the sender gets a real answer; if the stock API is down the
        # job stays queued and the worker retries it with backoff.
        job = await self.store.get_check(job_id)
        outcome = await self._check_sku(event.sku, [job] if job else [])
        if not outcome.ok:
            return 202, {"status": "accepted", "event_id": event.event_id,
                         "outcome": "pending", "detail": "stock check queued for retry"}
        product = outcome.product
        assert product is not None
        result = "verified" if product.available else (
            "phantom" if (event.quantity or 0) > 0 else "out_of_stock")
        return 200, {"status": "processed", "event_id": event.event_id, "outcome": result,
                     "verified_quantity": product.quantity, "notifying": outcome.claimed}

    async def process_due_checks(self) -> int:
        jobs = await self.store.lease_due_checks(BATCH_SIZE, LEASE_SECONDS)
        if not jobs:
            return 0
        by_sku: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for job in jobs:
            by_sku[job["sku"]].append(job)
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def run(sku: str, sku_jobs: list[dict[str, Any]]) -> None:
            async with semaphore:
                await self._check_sku(sku, sku_jobs)

        # One stock API call per SKU, however many jobs are queued for it.
        await asyncio.gather(*(run(sku, js) for sku, js in by_sku.items()))
        return len(jobs)

    async def process_due_sends(self) -> int:
        rows = await self.store.lease_due_sends(BATCH_SIZE, LEASE_SECONDS)
        if not rows:
            return 0
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def run(row: dict[str, Any]) -> None:
            async with semaphore:
                await self._deliver(row)

        await asyncio.gather(*(run(row) for row in rows))
        return len(rows)

    async def reconcile(self) -> dict[str, int]:
        """Safety net for lost webhooks: look at every SKU somebody is waiting
        for and notify if the stock API says it is available. One catalogue
        call per cycle; no calls at all when nobody is waiting."""
        skus = await self.store.open_skus()
        if not skus:
            return {"skus": 0, "available": 0, "claimed": 0}
        products: dict[str, Product] = {}
        try:
            catalogue = await self.stock.list_products()
            for sku in skus:
                products[sku] = catalogue.get(sku) or Product.unknown(sku)
        except StockError as exc:
            log.warning("reconcile_catalogue_failed", extra={"error": truncate(str(exc))})
            for sku in skus[:RECONCILE_FALLBACK_SKUS]:
                try:
                    products[sku] = await self.stock.get_product(sku)
                except StockError:
                    continue
        available = claimed = 0
        for sku, product in products.items():
            if not product.available:
                continue
            available += 1
            ids = await self.store.claim_open_for_sku(
                sku, json.dumps(product.snapshot()), "reconcile")
            claimed += len(ids)
        if claimed:
            log.info("reconcile_claimed", extra={"claimed": claimed, "skus": available})
            self.wake()
        return {"skus": len(skus), "available": available, "claimed": claimed}

    # -- internals ------------------------------------------------------------------

    async def _check_sku(self, sku: str, jobs: list[dict[str, Any]]) -> CheckOutcome:
        try:
            product = await self.stock.get_product(sku)
        except StockError as exc:
            error = truncate(str(exc))
            for job in jobs:
                attempt = job["attempts"] + 1
                if attempt >= CHECK_MAX_ATTEMPTS:
                    await self.store.give_up_check(job["id"], error)
                    log.error("stock_check_gave_up", extra={"job_id": job["id"], "sku": sku,
                                                            "error": error})
                else:
                    delay = backoff_seconds(attempt, CHECK_BACKOFF_BASE, CHECK_BACKOFF_CAP)
                    await self.store.retry_check(job["id"], error, delay)
                    log.warning("stock_check_failed", extra={"job_id": job["id"], "sku": sku,
                                                             "attempt": attempt, "error": error,
                                                             "retry_in_s": round(delay, 1)})
            return CheckOutcome(ok=False, error=error)

        snapshot = json.dumps(product.snapshot())
        claimed = 0
        for job in jobs:
            ids = await self.store.complete_check(
                job["id"], verified_quantity=product.quantity,
                available=product.available, product_json=snapshot)
            claimed += len(ids or [])
            log.info("stock_checked", extra={"job_id": job["id"], "sku": sku,
                                             "reason": job["reason"],
                                             "event_id": job["event_id"],
                                             "quantity": product.quantity,
                                             "available": product.available,
                                             "claimed": len(ids or [])})
        if claimed:
            self.wake()
        return CheckOutcome(ok=True, product=product, claimed=claimed)

    async def _deliver(self, row: dict[str, Any]) -> None:
        request_id = row["id"]
        # No stock re-check here: the restock was verified when the row was
        # claimed. Re-checking could strand a customer if the item sold out
        # again between a failed attempt and its retry.
        try:
            product = json.loads(row["product_json"] or "{}")
            message = build_email(row, product, self.settings)
            result = await self.mailer.send(
                to=row["email"], subject=message.subject, html=message.html,
                text=message.text, idempotency_key=f"bis-{self.store.install_id}-{request_id}")
        except Exception as exc:  # never lose a row to an unexpected bug
            log.exception("send_crashed", extra={"request_id": request_id})
            await self.store.mark_send_retry(
                request_id, truncate(f"internal error: {exc}"),
                backoff_seconds(row["attempts"] + 1, SEND_BACKOFF_BASE, SEND_BACKOFF_CAP))
            return

        if result.ok:
            await self.store.mark_sent(request_id, result.email_id)
            log.info("email_sent", extra={"request_id": request_id, "sku": row["sku"],
                                          "email": mask_email(row["email"]),
                                          "email_id": result.email_id,
                                          "attempt": row["attempts"] + 1})
        elif result.permanent:
            await self.store.mark_send_failed(request_id, truncate(result.error or "rejected"))
            log.error("email_rejected", extra={"request_id": request_id,
                                               "error": result.error})
        else:
            attempt = row["attempts"] + 1
            delay = backoff_seconds(attempt, SEND_BACKOFF_BASE, SEND_BACKOFF_CAP)
            await self.store.mark_send_retry(request_id, truncate(result.error or "failed"), delay)
            log.warning("email_failed", extra={"request_id": request_id, "attempt": attempt,
                                               "error": result.error,
                                               "retry_in_s": round(delay, 1)})
