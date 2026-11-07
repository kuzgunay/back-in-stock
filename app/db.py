"""SQLite persistence.

All state that matters for "exactly once" lives here, so a restart loses
nothing. One connection is used from one dedicated thread: every public
method runs in that thread (so the event loop never blocks on disk I/O) and
calls are serialised. Correctness does not depend on that serialisation,
though: duplicates are prevented by constraints (event_id PRIMARY KEY, a
partial UNIQUE index on open requests) and by conditional UPDATEs inside
BEGIN IMMEDIATE transactions, which hold across processes too.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .util import Clock, to_iso

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL,
    sku             TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'notifying', 'notified', 'failed')),
    created_at      TEXT    NOT NULL,
    claimed_at      TEXT,
    notified_at     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_until     TEXT,
    last_error      TEXT,
    email_id        TEXT,
    triggered_by    TEXT,
    product_json    TEXT
);

-- At most one open (or in-flight) request per customer and SKU, enforced by
-- the database even under concurrent inserts.
CREATE UNIQUE INDEX IF NOT EXISTS requests_one_open_per_email_sku
    ON requests (email, sku) WHERE status IN ('open', 'notifying');
CREATE INDEX IF NOT EXISTS requests_by_sku_status ON requests (sku, status);
CREATE INDEX IF NOT EXISTS requests_due ON requests (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS requests_by_created ON requests (created_at);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id          TEXT    PRIMARY KEY,
    received_at       TEXT    NOT NULL,
    type              TEXT,
    sku               TEXT    NOT NULL,
    claimed_quantity  INTEGER,
    verified_quantity INTEGER,
    outcome           TEXT    NOT NULL DEFAULT 'pending',
    notified_count    INTEGER NOT NULL DEFAULT 0,
    duplicate_count   INTEGER NOT NULL DEFAULT 0,
    last_duplicate_at TEXT,
    occurred_at       TEXT,
    processed_at      TEXT
);
CREATE INDEX IF NOT EXISTS webhook_events_by_received ON webhook_events (received_at);

-- Outbox of stock verifications that still have to happen (webhook events
-- and new subscriptions). Survives restarts; retried with backoff.
CREATE TABLE IF NOT EXISTS stock_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sku             TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    event_id        TEXT    REFERENCES webhook_events (event_id),
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'done', 'gave_up')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT    NOT NULL,
    lease_until     TEXT,
    last_error      TEXT,
    created_at      TEXT    NOT NULL,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS stock_checks_due ON stock_checks (status, next_attempt_at);
"""

REQUEST_STATUSES = ("open", "notifying", "notified", "failed")


def _in_db_thread(fn: Callable[..., T]) -> Callable[..., Any]:
    """Turn a synchronous Store method into an awaitable run on the DB thread."""

    @functools.wraps(fn)
    async def wrapper(self: "Store", *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, functools.partial(fn, self, *args, **kwargs)
        )

    return wrapper


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


class Store:
    def __init__(self, path: Path, clock: Clock) -> None:
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        conn = sqlite3.connect(
            str(path), timeout=10.0, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        self._conn = conn

    # -- plumbing ---------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _now(self) -> str:
        return to_iso(self._clock())

    def _later(self, seconds: float) -> str:
        return to_iso(self._clock() + timedelta(seconds=seconds))

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._conn.close)
        self._executor.shutdown(wait=True)

    # -- startup ------------------------------------------------------------

    @_in_db_thread
    def release_leases(self) -> int:
        """Forget in-flight markers left by a previous process.

        Safe because exactly one process runs the worker (see README). Rows
        keep their status, so anything that was mid-send is retried with the
        same Idempotency-Key and the provider de-duplicates it.
        """
        with self._tx() as c:
            a = c.execute(
                "UPDATE requests SET lease_until = NULL WHERE lease_until IS NOT NULL"
            ).rowcount
            b = c.execute(
                "UPDATE stock_checks SET lease_until = NULL WHERE lease_until IS NOT NULL"
            ).rowcount
        return a + b

    # -- requests -----------------------------------------------------------

    @_in_db_thread
    def create_request(self, email: str, sku: str) -> tuple[dict[str, Any], bool]:
        """Insert an open request plus a stock check; return (row, created).

        If the customer already has an open request for the SKU, the unique
        index rejects the insert and the existing row is returned instead.
        """
        now = self._now()
        try:
            with self._tx() as c:
                rid = c.execute(
                    "INSERT INTO requests (email, sku, status, created_at) "
                    "VALUES (?, ?, 'open', ?)",
                    (email, sku, now),
                ).lastrowid
                c.execute(
                    "INSERT INTO stock_checks (sku, reason, next_attempt_at, created_at) "
                    "VALUES (?, 'subscribe', ?, ?)",
                    (sku, now, now),
                )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE email = ? AND sku = ? "
                "AND status IN ('open', 'notifying')",
                (email, sku),
            ).fetchone()
            if row is None:  # pragma: no cover - would need a concurrent state change
                raise
            return dict(row), False
        row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        return dict(row), True

    def _claim_open(
        self, c: sqlite3.Connection, sku: str, product_json: str, triggered_by: str
    ) -> list[int]:
        """Move every open request for a SKU to 'notifying', one row at a time.

        The conditional UPDATE is the claim: a row can only go open ->
        notifying once, so two restock signals for the same SKU can never
        both send to the same customer.
        """
        now = self._now()
        ids = [
            r[0]
            for r in c.execute(
                "SELECT id FROM requests WHERE sku = ? AND status = 'open' ORDER BY id",
                (sku,),
            )
        ]
        claimed: list[int] = []
        for rid in ids:
            cur = c.execute(
                "UPDATE requests SET status = 'notifying', claimed_at = ?, "
                "next_attempt_at = ?, lease_until = NULL, product_json = ?, "
                "triggered_by = ? WHERE id = ? AND status = 'open'",
                (now, now, product_json, triggered_by, rid),
            )
            if cur.rowcount == 1:
                claimed.append(rid)
        return claimed

    @_in_db_thread
    def claim_open_for_sku(self, sku: str, product_json: str, triggered_by: str) -> list[int]:
        with self._tx() as c:
            return self._claim_open(c, sku, product_json, triggered_by)

    @_in_db_thread
    def lease_due_sends(self, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        now, lease = self._now(), self._later(lease_seconds)
        with self._tx() as c:
            rows = _rows(
                c.execute(
                    "SELECT * FROM requests WHERE status = 'notifying' "
                    "AND next_attempt_at <= ? AND (lease_until IS NULL OR lease_until <= ?) "
                    "ORDER BY next_attempt_at, id LIMIT ?",
                    (now, now, limit),
                )
            )
            c.executemany(
                "UPDATE requests SET lease_until = ? WHERE id = ?",
                [(lease, r["id"]) for r in rows],
            )
        return rows

    @_in_db_thread
    def mark_sent(self, request_id: int, email_id: str | None) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE requests SET status = 'notified', notified_at = ?, email_id = ?, "
                "attempts = attempts + 1, last_error = NULL, next_attempt_at = NULL, "
                "lease_until = NULL WHERE id = ? AND status = 'notifying'",
                (self._now(), email_id, request_id),
            )
        return cur.rowcount == 1

    @_in_db_thread
    def mark_send_retry(self, request_id: int, error: str, retry_in: float) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE requests SET attempts = attempts + 1, last_error = ?, "
                "next_attempt_at = ?, lease_until = NULL "
                "WHERE id = ? AND status = 'notifying'",
                (error, self._later(retry_in), request_id),
            )

    @_in_db_thread
    def mark_send_failed(self, request_id: int, error: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE requests SET status = 'failed', attempts = attempts + 1, "
                "last_error = ?, next_attempt_at = NULL, lease_until = NULL "
                "WHERE id = ? AND status = 'notifying'",
                (error, request_id),
            )

    @_in_db_thread
    def open_skus(self) -> list[str]:
        """SKUs with customers still waiting, longest-waiting first."""
        cur = self._conn.execute(
            "SELECT sku FROM requests WHERE status = 'open' "
            "GROUP BY sku ORDER BY MIN(created_at)"
        )
        return [r[0] for r in cur.fetchall()]

    @_in_db_thread
    def get_request(self, request_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- webhooks and stock checks -------------------------------------------

    @_in_db_thread
    def record_webhook(
        self,
        *,
        event_id: str,
        sku: str,
        event_type: str | None,
        claimed_quantity: int | None,
        occurred_at: str | None,
        lease_seconds: float,
    ) -> int | None:
        """Record a delivery. Returns a leased stock-check job id, or None if
        this event_id was seen before (a retry or a concurrent duplicate).

        The PRIMARY KEY makes the first insert win; the event row and its
        verification job commit together, so a crash right after this call
        still leaves the verification queued.
        """
        now = self._now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO webhook_events "
                "(event_id, received_at, type, sku, claimed_quantity, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                (event_id, now, event_type, sku, claimed_quantity, occurred_at),
            )
            if cur.rowcount == 0:
                c.execute(
                    "UPDATE webhook_events SET duplicate_count = duplicate_count + 1, "
                    "last_duplicate_at = ? WHERE event_id = ?",
                    (now, event_id),
                )
                return None
            return c.execute(
                "INSERT INTO stock_checks "
                "(sku, reason, event_id, next_attempt_at, lease_until, created_at) "
                "VALUES (?, 'webhook', ?, ?, ?, ?)",
                (sku, event_id, now, self._later(lease_seconds), now),
            ).lastrowid

    @_in_db_thread
    def get_check(self, job_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM stock_checks WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    @_in_db_thread
    def lease_due_checks(self, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        now, lease = self._now(), self._later(lease_seconds)
        with self._tx() as c:
            rows = _rows(
                c.execute(
                    "SELECT * FROM stock_checks WHERE status = 'pending' "
                    "AND next_attempt_at <= ? AND (lease_until IS NULL OR lease_until <= ?) "
                    "ORDER BY next_attempt_at, id LIMIT ?",
                    (now, now, limit),
                )
            )
            c.executemany(
                "UPDATE stock_checks SET lease_until = ? WHERE id = ?",
                [(lease, r["id"]) for r in rows],
            )
        return rows

    @_in_db_thread
    def retry_check(self, job_id: int, error: str, retry_in: float) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE stock_checks SET attempts = attempts + 1, last_error = ?, "
                "next_attempt_at = ?, lease_until = NULL "
                "WHERE id = ? AND status = 'pending'",
                (error, self._later(retry_in), job_id),
            )

    @_in_db_thread
    def give_up_check(self, job_id: int, error: str) -> None:
        now = self._now()
        with self._tx() as c:
            row = c.execute(
                "SELECT event_id FROM stock_checks WHERE id = ? AND status = 'pending'",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            c.execute(
                "UPDATE stock_checks SET status = 'gave_up', attempts = attempts + 1, "
                "last_error = ?, finished_at = ?, lease_until = NULL WHERE id = ?",
                (error, now, job_id),
            )
            if row["event_id"]:
                c.execute(
                    "UPDATE webhook_events SET outcome = 'unverified', processed_at = ? "
                    "WHERE event_id = ? AND outcome = 'pending'",
                    (now, row["event_id"]),
                )

    @_in_db_thread
    def complete_check(
        self,
        job_id: int,
        *,
        verified_quantity: int | None,
        available: bool,
        product_json: str,
    ) -> list[int] | None:
        """Store a stock verification and, if the item really is available,
        claim the waiting requests in the same transaction.

        Returns the claimed request ids, or None if the job had already been
        completed by someone else (nothing is done twice).
        """
        now = self._now()
        with self._tx() as c:
            job = c.execute(
                "SELECT * FROM stock_checks WHERE id = ? AND status = 'pending'", (job_id,)
            ).fetchone()
            if job is None:
                return None
            c.execute(
                "UPDATE stock_checks SET status = 'done', attempts = attempts + 1, "
                "last_error = NULL, finished_at = ?, lease_until = NULL WHERE id = ?",
                (now, job_id),
            )
            trigger = f"webhook:{job['event_id']}" if job["event_id"] else job["reason"]
            claimed = (
                self._claim_open(c, job["sku"], product_json, trigger) if available else []
            )
            if job["event_id"]:
                event = c.execute(
                    "SELECT claimed_quantity FROM webhook_events WHERE event_id = ?",
                    (job["event_id"],),
                ).fetchone()
                claimed_qty = event["claimed_quantity"] if event else None
                if available:
                    outcome = "verified"
                elif claimed_qty is not None and claimed_qty > 0:
                    outcome = "phantom"
                else:
                    outcome = "out_of_stock"
                c.execute(
                    "UPDATE webhook_events SET verified_quantity = ?, outcome = ?, "
                    "notified_count = ?, processed_at = ? WHERE event_id = ?",
                    (verified_quantity, outcome, len(claimed), now, job["event_id"]),
                )
            return claimed

    # -- admin / reporting ------------------------------------------------------

    @_in_db_thread
    def request_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in REQUEST_STATUSES}
        for status, n in self._conn.execute(
            "SELECT status, COUNT(*) FROM requests GROUP BY status"
        ):
            counts[status] = n
        counts["all"] = sum(counts[s] for s in REQUEST_STATUSES)
        return counts

    @_in_db_thread
    def event_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome, n in self._conn.execute(
            "SELECT outcome, COUNT(*) FROM webhook_events GROUP BY outcome"
        ):
            counts[outcome] = n
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(duplicate_count), 0) FROM webhook_events"
        ).fetchone()
        counts["all"], counts["duplicates"] = row[0], row[1]
        counts["pending_checks"] = self._conn.execute(
            "SELECT COUNT(*) FROM stock_checks WHERE status = 'pending'"
        ).fetchone()[0]
        return counts

    @_in_db_thread
    def list_requests(
        self, *, status: str | None, query: str | None, limit: int, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = [], []
        if status in REQUEST_STATUSES:
            where.append("status = ?")
            params.append(status)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(email LIKE ? ESCAPE '\\' OR sku LIKE ? ESCAPE '\\')")
            params += [f"%{escaped}%", f"%{escaped}%"]
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM requests {clause}", params
        ).fetchone()[0]
        rows = _rows(
            self._conn.execute(
                f"SELECT * FROM requests {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        )
        return rows, total

    @_in_db_thread
    def list_events(self, limit: int) -> list[dict[str, Any]]:
        return _rows(
            self._conn.execute(
                "SELECT * FROM webhook_events ORDER BY received_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        )

    @_in_db_thread
    def ping(self) -> bool:
        return self._conn.execute("SELECT 1").fetchone()[0] == 1
