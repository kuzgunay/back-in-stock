"""Small pure-ASGI middlewares (no BaseHTTPMiddleware, so no buffering quirks)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

access_log = logging.getLogger("app.http")


async def _send_json(send: Send, status: int, payload: dict[str, Any],
                     extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())] + (extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Reject request bodies above max_bytes with 413, whether or not the
    client sent Content-Length (chunked bodies are counted as they arrive)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = {"error": "payload_too_large",
                     "message": f"Request body must be at most {self.max_bytes} bytes."}
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _send_json(send, 400, {"error": "bad_request",
                                                 "message": "Invalid Content-Length."})
                    return
                if declared > self.max_bytes:
                    await _send_json(send, 413, too_large)
                    return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _send_json(send, 413, too_large)


class ApiCorsMiddleware:
    """CORS for /api/* only: the subscribe endpoint may be called from any
    storefront. Admin and webhook endpoints get no CORS headers."""

    def __init__(self, app: ASGIApp, prefix: str = "/api/") -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS":
            await send({"type": "http.response.start", "status": 204, "headers": [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"POST, OPTIONS"),
                (b"access-control-allow-headers", b"Content-Type, Accept"),
                (b"access-control-max-age", b"86400"),
                (b"content-length", b"0"),
            ]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def cors_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"access-control-allow-origin", b"*")]
            await send(message)

        await self.app(scope, receive, cors_send)


class SecurityHeadersMiddleware:
    """Baseline headers. Pages that set their own Content-Security-Policy
    (e.g. /embed, which must be frameable) keep it; everything else is
    locked down and cannot be framed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                headers.append((b"x-content-type-options", b"nosniff"))
                if b"referrer-policy" not in names:
                    headers.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                if b"content-security-policy" not in names:
                    headers.append((b"content-security-policy",
                                    b"default-src 'none'; frame-ancestors 'none'"))
                    headers.append((b"x-frame-options", b"DENY"))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def logging_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, logging_send)
        finally:
            path = scope["path"]
            level = logging.DEBUG if path == "/healthz" else logging.INFO
            # The query string is deliberately not logged (it can contain emails).
            access_log.log(level, "http_request", extra={
                "method": scope["method"], "path": path, "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1)})
