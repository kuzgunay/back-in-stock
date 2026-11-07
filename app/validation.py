"""Input validation for subscriptions and webhook payloads."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

# Pragmatic address check: one @, a sane local part, and a dotted domain.
_EMAIL_RE = re.compile(
    r"^(?=.{3,254}$)"
    r"(?!\.)(?!.*\.\.)[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}(?<!\.)"
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]$"
)
_SKU_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ValidationError(Exception):
    def __init__(self, fields: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


def normalize_email(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError({"email": "Please enter your email address."})
    email = value.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValidationError({"email": "Please enter a valid email address."})
    return email


def normalize_sku(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError({"sku": "A product SKU is required."})
    sku = value.strip()
    if not _SKU_RE.match(sku):
        raise ValidationError(
            {"sku": "SKU must be 1–64 characters: letters, digits, dot, dash or underscore."}
        )
    return sku.upper()


def validate_subscription(body: Any) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise ValidationError({"body": "Expected a JSON object with email and sku."})
    errors: dict[str, str] = {}
    email = sku = ""
    try:
        email = normalize_email(body.get("email"))
    except ValidationError as exc:
        errors.update(exc.fields)
    try:
        sku = normalize_sku(body.get("sku"))
    except ValidationError as exc:
        errors.update(exc.fields)
    if errors:
        raise ValidationError(errors)
    return email, sku


@dataclass(frozen=True)
class StockEvent:
    event_id: str
    sku: str
    quantity: int | None
    type: str | None
    occurred_at: str | None


def _hint_quantity(value: Any) -> int | None:
    """The webhook quantity is only a hint, so a weird value is not fatal."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def validate_stock_event(body: Any) -> StockEvent:
    if not isinstance(body, dict):
        raise ValidationError({"body": "Expected a JSON object."})
    errors: dict[str, str] = {}
    event_id = body.get("event_id")
    if isinstance(event_id, int) and not isinstance(event_id, bool):
        event_id = str(event_id)
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id.strip()) > 255:
        errors["event_id"] = "event_id must be a non-empty string (max 255 characters)."
    sku = ""
    try:
        sku = normalize_sku(body.get("sku"))
    except ValidationError as exc:
        errors.update(exc.fields)
    if errors:
        raise ValidationError(errors)

    def opt_str(key: str, limit: int) -> str | None:
        value = body.get(key)
        return value.strip()[:limit] or None if isinstance(value, str) else None

    return StockEvent(
        event_id=str(event_id).strip(),
        sku=sku,
        quantity=_hint_quantity(body.get("quantity")),
        type=opt_str("type", 64),
        occurred_at=opt_str("occurred_at", 64),
    )
