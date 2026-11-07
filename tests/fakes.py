"""In-process fakes for the stock API and the Resend API (httpx MockTransport)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeStockApi:
    """GET /products and GET /products/{sku}, Bearer auth, unknown SKU -> quantity 0."""

    def __init__(self, api_key: str = "stock-key") -> None:
        self.api_key = api_key
        self.products: dict[str, dict[str, Any]] = {}
        self.down = False
        self.catalogue_down = False
        self.calls: list[str] = []
        self.latency = 0.01

    def set(self, sku: str, quantity: int, **extra: Any) -> None:
        product = {
            "sku": sku.upper(),
            "name": extra.pop("name", f"Product {sku.upper()}"),
            "price": extra.pop("price", 49.0),
            "image": extra.pop("image", f"https://cdn.example.com/{sku.lower()}.jpg"),
            "quantity": quantity,
            "in_stock": quantity > 0,
        }
        product.update(extra)
        self.products[sku.upper()] = product

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        await asyncio.sleep(self.latency)  # let concurrent requests interleave
        if self.down:
            raise httpx.ConnectError("stock API unreachable", request=request)
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"error": "unauthorized"})
        parts = [p for p in request.url.path.split("/") if p]
        if parts == ["products"]:
            if self.catalogue_down:
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(200, json={"products": list(self.products.values())})
        if len(parts) == 2 and parts[0] == "products":
            sku = parts[1].upper()
            product = self.products.get(sku) or {
                "sku": sku, "name": None, "price": None, "image": None,
                "quantity": 0, "in_stock": False,
            }
            return httpx.Response(200, json=product)
        return httpx.Response(404, json={"error": "not found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sku_calls(self, sku: str) -> int:
        return sum(1 for path in self.calls if path.rstrip("/").upper().endswith("/" + sku.upper()))


@dataclass
class SentEmail:
    idempotency_key: str | None
    payload: dict[str, Any]

    @property
    def to(self) -> str:
        to = self.payload["to"]
        return to[0] if isinstance(to, list) else to


@dataclass
class FakeResend:
    """POST /emails. Honours Idempotency-Key like Resend: a repeated key returns
    the original response without sending again. fail_next makes the next N
    calls return HTTP 500 (nothing is sent and the key is not consumed)."""

    api_key: str = "resend-key"
    accepted: list[SentEmail] = field(default_factory=list)
    attempts: list[SentEmail] = field(default_factory=list)
    fail_next: int = 0
    fail_status: int = 500
    drop_response_next: int = 0  # accept the email, then lose the response
    _by_key: dict[str, str] = field(default_factory=dict)
    latency: float = 0.01

    async def handler(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.latency)
        if request.url.path != "/emails" or request.method != "POST":
            return httpx.Response(404, json={"message": "not found"})
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"message": "invalid api key"})
        payload = json.loads(request.content)
        key = request.headers.get("idempotency-key")
        record = SentEmail(idempotency_key=key, payload=payload)
        self.attempts.append(record)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(self.fail_status, json={"message": "simulated failure"})
        if key and key in self._by_key:
            return httpx.Response(200, json={"id": self._by_key[key]})
        email_id = f"email_{len(self.accepted) + 1}"
        if key:
            self._by_key[key] = email_id
        self.accepted.append(record)
        if self.drop_response_next > 0:
            self.drop_response_next -= 1
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"id": email_id})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def to(self, address: str) -> list[SentEmail]:
        return [e for e in self.accepted if e.to == address]
