"""Builds the restock email (HTML + plain text) from a stored product snapshot.

The snapshot is taken when the request is claimed and never changes, so every
retry sends a byte-identical payload under the same Idempotency-Key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .templating import render


@dataclass(frozen=True)
class EmailMessage:
    subject: str
    html: str
    text: str


def format_price(price: Any) -> str | None:
    if price is None or isinstance(price, bool):
        return None
    if isinstance(price, (int, float)):
        return f"${price:,.2f}"
    text = str(price).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return f"${float(text):,.2f}"
    return text[:40] or None


def build_email(request: dict[str, Any], product: dict[str, Any], settings: Settings) -> EmailMessage:
    sku = request["sku"]
    name = (product.get("name") or "").strip() or sku
    quantity = product.get("quantity")
    low_stock = isinstance(quantity, int) and 0 < quantity <= 5
    shop_url = product.get("url") or settings.store_url
    context = {
        "name": name,
        "sku": sku,
        "price": format_price(product.get("price")),
        "image": product.get("image"),
        "quantity": quantity if low_stock else None,
        "shop_url": shop_url,
        "store_name": settings.store_name,
        "email": request["email"],
        "request_id": request["id"],
    }
    subject = f"Good news — {name} is back in stock"
    return EmailMessage(
        subject=subject,
        html=render("email.html", subject=subject, **context),
        text=render("email.txt", **context),
    )
