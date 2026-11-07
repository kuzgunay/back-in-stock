"""Small helpers shared across the app: time, backoff, masking."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Callable

Clock = Callable[[], datetime]

# Fixed-width UTC timestamps sort lexicographically, so SQLite can compare
# them as plain TEXT (e.g. `next_attempt_at <= ?`).
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_ISO_FORMAT)


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, _ISO_FORMAT).replace(tzinfo=timezone.utc)


def iso_after(clock: Clock, seconds: float) -> str:
    return to_iso(clock() + timedelta(seconds=seconds))


def backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with jitter.

    attempt is 1 for the first retry. The result is never above the nominal
    delay (base * 2^(attempt-1), capped), so callers and tests can rely on
    "advance by the nominal delay and the item is due".
    """
    nominal = min(cap, base * (2 ** max(0, attempt - 1)))
    return nominal * random.uniform(0.8, 1.0)


def mask_email(email: str) -> str:
    """j***@example.com — enough to correlate logs without storing PII in them."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def truncate(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
