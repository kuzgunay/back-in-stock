"""Client for the store's stock API — the only source of truth for restocks."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings

log = logging.getLogger("app.stock")


class StockError(Exception):
    """The stock API could not give a trustworthy answer (network, 5xx, junk)."""


@dataclass(frozen=True)
class Product:
    sku: str
    quantity: int | None
    in_stock: bool | None
    name: str | None = None
    price: Any = None
    image: str | None = None
    url: str | None = None

    @property
    def available(self) -> bool:
        if self.in_stock is False:
            return False
        if self.quantity is not None:
            return self.quantity > 0
        return bool(self.in_stock)

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def unknown(cls, sku: str) -> "Product":
        return cls(sku=sku, quantity=0, in_stock=False)


def _parse_quantity(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return None
    return None


def _opt_str(value: Any, limit: int = 500) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text[:limit] or None


def _http_url(value: Any) -> str | None:
    text = _opt_str(value, 2000)
    return text if text and text.startswith(("https://", "http://")) else None


def parse_product(data: Any, expected_sku: str | None = None) -> Product:
    if not isinstance(data, dict):
        raise StockError("stock API returned a non-object product")
    sku = _opt_str(data.get("sku"), 64)
    if expected_sku is not None:
        if sku is not None and sku.upper() != expected_sku:
            raise StockError(f"stock API answered for {sku!r}, expected {expected_sku!r}")
        sku = expected_sku
    if not sku:
        raise StockError("stock API product has no sku")
    in_stock = data.get("in_stock")
    price = data.get("price")
    if isinstance(price, (dict, list, bool)):
        price = None
    return Product(
        sku=sku.upper(),
        quantity=_parse_quantity(data.get("quantity")),
        in_stock=in_stock if isinstance(in_stock, bool) else None,
        name=_opt_str(data.get("name"), 200),
        price=price,
        image=_http_url(data.get("image")),
        url=_http_url(data.get("url") or data.get("product_url")),
    )


class StockClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._base = settings.stock_api_url.rstrip("/")
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(settings.stock_timeout, connect=min(3.0, settings.stock_timeout)),
            headers={
                "Authorization": f"Bearer {settings.stock_api_key}",
                "Accept": "application/json",
                "User-Agent": "back-in-stock/1.0",
            },
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, url: str) -> Any:
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise StockError(f"{type(exc).__name__}: {exc}".rstrip(": ")) from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise StockError(f"stock API returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise StockError("stock API returned invalid JSON") from exc

    async def get_product(self, sku: str) -> Product:
        """GET {STOCK_API_URL}/{sku}. Unknown SKUs count as out of stock."""
        data = await self._get_json(f"{self._base}/{quote(sku, safe='')}")
        if data is None:
            return Product.unknown(sku)
        return parse_product(data, expected_sku=sku)

    async def list_products(self) -> dict[str, Product]:
        """GET {STOCK_API_URL} -> {"products": [...]}, keyed by upper-case SKU."""
        data = await self._get_json(self._base)
        items = data.get("products") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise StockError("stock API catalogue has no product list")
        products: dict[str, Product] = {}
        for item in items:
            try:
                product = parse_product(item)
            except StockError:
                continue
            products[product.sku] = product
        return products
