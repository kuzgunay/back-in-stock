"""HTTP routes: public API, webhook receiver, embeddable form, admin, health."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .templating import render
from .validation import (
    ValidationError,
    normalize_sku,
    validate_stock_event,
    validate_subscription,
)

if TYPE_CHECKING:
    from .main import AppContext

log = logging.getLogger("app.web")
router = APIRouter()

PAGE_SIZE = 50


def _ctx(request: Request) -> "AppContext":
    return request.app.state.ctx


def _error(status: int, code: str, message: str, fields: dict[str, str] | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": code, "message": message}
    if fields:
        payload["fields"] = fields
    return JSONResponse(payload, status_code=status)


class _BadJson(Exception):
    pass


async def _read_json(request: Request) -> Any:
    raw = await request.body()
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _BadJson() from exc


def _public_request(row: dict[str, Any], already: bool) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "sku": row["sku"],
        "status": row["status"],
        "created_at": row["created_at"],
        "notified_at": row["notified_at"],
        "already_subscribed": already,
    }


# -- API ---------------------------------------------------------------------


@router.post("/api/requests")
async def create_request(request: Request) -> Response:
    try:
        body = await _read_json(request)
    except _BadJson:
        return _error(400, "invalid_json", "Request body must be valid JSON.")
    try:
        email, sku = validate_subscription(body)
    except ValidationError as exc:
        return _error(422, "validation_error", next(iter(exc.fields.values())), exc.fields)
    row, created = await _ctx(request).notifier.subscribe(email, sku)
    return JSONResponse(_public_request(row, not created), status_code=201 if created else 200)


@router.post("/webhooks/stock")
async def stock_webhook(request: Request) -> Response:
    expected = _ctx(request).settings.webhook_secret.encode()
    provided = request.headers.get("x-webhook-secret", "").encode("utf-8", "replace")
    if not provided or not hmac.compare_digest(provided, expected):
        log.warning("webhook_unauthorized", extra={"has_header": bool(provided)})
        return _error(401, "unauthorized", "Missing or invalid X-Webhook-Secret header.")
    try:
        body = await _read_json(request)
    except _BadJson:
        return _error(400, "invalid_json", "Request body must be valid JSON.")
    try:
        event = validate_stock_event(body)
    except ValidationError as exc:
        return _error(422, "validation_error", next(iter(exc.fields.values())), exc.fields)
    status, payload = await _ctx(request).notifier.handle_webhook(event)
    return JSONResponse(payload, status_code=status)


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


# -- embeddable form -------------------------------------------------------------

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _accent(value: str | None) -> tuple[str, str]:
    """Validated accent colour and a readable text colour on top of it."""
    match = _HEX_RE.match(value or "")
    if not match:
        return "#18181b", "#ffffff"
    hex6 = match.group(1)
    if len(hex6) == 3:
        hex6 = "".join(ch * 2 for ch in hex6)
    r, g, b = (int(hex6[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return f"#{hex6.lower()}", ("#111111" if luminance > 0.45 else "#ffffff")


def _embed_options(request: Request) -> dict[str, Any]:
    params = request.query_params
    try:
        sku = normalize_sku(params.get("sku")) if params.get("sku") else ""
    except ValidationError:
        sku = ""
    product = " ".join((params.get("product") or "").split())[:120]
    theme = params.get("theme", "light")
    if theme not in ("light", "dark", "auto"):
        theme = "light"
    accent, accent_ink = _accent(params.get("accent"))
    query = {k: v for k, v in (("sku", sku), ("product", product), ("theme", theme),
                               ("accent", params.get("accent") if accent != "#18181b" else ""))
             if v}
    return {"sku": sku, "product": product, "theme": theme, "accent": accent,
            "accent_ink": accent_ink, "action": "/embed" + ("?" + urlencode(query) if query else "")}


def _embed_response(request: Request, *, state: str = "form", email: str = "",
                    sku_value: str | None = None, errors: dict[str, str] | None = None,
                    status_code: int = 200) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    options = _embed_options(request)
    html = render("embed.html", nonce=nonce, state=state, email=email, errors=errors or {},
                  sku_value=options["sku"] if sku_value is None else sku_value, **options)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
           "connect-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; "
           "frame-ancestors *")
    # Deliberately no X-Frame-Options: this page is meant to be framed anywhere.
    return HTMLResponse(html, status_code=status_code, headers={
        "Content-Security-Policy": csp,
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-cache",
    })


@router.get("/embed")
async def embed(request: Request) -> Response:
    return _embed_response(request)


@router.post("/embed")
async def embed_submit(request: Request) -> Response:
    """No-JavaScript fallback: a plain form post renders the result page."""
    raw = (await request.body()).decode("utf-8", "replace")
    form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
    email_in, sku_in = form.get("email", ""), form.get("sku", "")
    try:
        email, sku = validate_subscription({"email": email_in, "sku": sku_in})
    except ValidationError as exc:
        return _embed_response(request, state="form", email=email_in[:254],
                               sku_value=sku_in[:64], errors=exc.fields, status_code=422)
    row, created = await _ctx(request).notifier.subscribe(email, sku)
    return _embed_response(request, state="success" if created else "already", email=email,
                           status_code=201 if created else 200)


# -- landing ------------------------------------------------------------------------


@router.get("/")
async def landing(request: Request) -> Response:
    nonce = secrets.token_urlsafe(16)
    base = str(request.base_url).rstrip("/")
    html = render("landing.html", nonce=nonce, base_url=base,
                  store_name=_ctx(request).settings.store_name)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
           "frame-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; "
           "frame-ancestors 'none'")
    return HTMLResponse(html, headers={"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})


# -- admin ------------------------------------------------------------------------------


def _authorized(request: Request, password: str) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not value:
        return False
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    _, sep, supplied = decoded.partition(":")
    return bool(sep) and hmac.compare_digest(supplied.encode(), password.encode())


_ADMIN_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY"}


@router.get("/admin")
async def admin(request: Request) -> Response:
    ctx = _ctx(request)
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; img-src 'self' data:; "
           "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    headers = {**_ADMIN_HEADERS, "Content-Security-Policy": csp}
    if not _authorized(request, ctx.settings.effective_admin_password):
        return HTMLResponse(render("admin_login.html", nonce=nonce), status_code=401, headers={
            **headers, "WWW-Authenticate": 'Basic realm="Back in Stock admin", charset="UTF-8"'})

    params = request.query_params
    status = params.get("status") or ""
    if status not in ("open", "notifying", "notified", "failed"):
        status = ""
    query = " ".join((params.get("q") or "").split())[:100]
    try:
        page = max(1, min(10_000, int(params.get("page") or 1)))
    except ValueError:
        page = 1

    store = ctx.store
    counts = await store.request_counts()
    event_counts = await store.event_counts()
    rows, total = await store.list_requests(status=status or None, query=query or None,
                                            limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    events = await store.list_events(25)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    def link(**changes: Any) -> str:
        current = {"status": status, "q": query, "page": page if page > 1 else ""}
        current.update(changes)
        clean = {k: v for k, v in current.items() if v not in ("", None, 1)}
        return "/admin" + ("?" + urlencode(clean) if clean else "")

    html = render("admin.html", nonce=nonce, counts=counts, event_counts=event_counts,
                  rows=rows, total=total, events=events, status=status, query=query,
                  page=page, pages=pages, link=link, now=ctx.clock(),
                  store_name=ctx.settings.store_name)
    return HTMLResponse(html, headers=headers)
